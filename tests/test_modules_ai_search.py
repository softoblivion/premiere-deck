"""AI search module tests (docs/AI_SEARCH.md 8). No network: the model is a tiny random-weight ONNX graph written
by tests/test_modules_ai_fixtures.py; media is generated with ffmpeg and Pillow in a scratch folder."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import numpy as np

from premiere_deck.host import protocol as P
from premiere_deck.modules.ai_search import embed as E
from premiere_deck.modules.ai_search import frames as FR
from premiere_deck.modules.ai_search import index as IX
from premiere_deck.modules.ai_search import models as MD
from premiere_deck.modules.ai_search import query as Q
from premiere_deck.modules.ai_search.module import AiSearchModule
from premiere_deck.modules.base import FakeHost
from premiere_deck.premiere.model import PremiereError, ProjectItem
from tests import test_modules_ai_fixtures as fx


def scratch(prefix: str) -> Path:
    base = os.environ.get("PREMIERE_DECK_HOME")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base or None))


class _Media:
    """Clips shared by the test classes (generated once)."""

    root: Path = None

    @classmethod
    def ensure(cls) -> Path:
        if cls.root is None:
            cls.root = scratch("deck-ai-media-")
            fx.ffmpeg_clip(cls.root / "red_blue.mp4", ["red", "blue"])  # 3 s, cut at 1.5 s
            fx.ffmpeg_clip(cls.root / "green.mp4", ["green"], seconds_each=3.0)
            fx.ffmpeg_clip(cls.root / "yellow.mp4", ["yellow"], seconds_each=2.0)
            from PIL import Image
            Image.new("RGB", (400, 300), (250, 250, 250)).save(cls.root / "white.png")
        return cls.root


class _Job:
    def __init__(self, cancel_after_progress: int = -1) -> None:
        self.cancelled = False
        self.log = []
        self._cancel_after = cancel_after_progress

    def progress(self, value, label=""):
        self.log.append((value, label))
        if self._cancel_after >= 0 and len(self.log) > self._cancel_after:
            self.cancelled = True

    def cancel(self):
        self.cancelled = True


# --- models ------------------------------------------------------------------------------------------------------


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self.status = status


class ModelFiles(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("deck-ai-models-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.models = self.dir / "models"
        self.data = fx.write_tiny_model(self.models)
        self.manifest = fx.manifest_from(self.data)
        self.source = Path(self.data["_source"])

    def opener(self, calls, *, status_for_range=206, corrupt=None):
        def urlopen(request, timeout=30):
            calls.append((request.full_url, request.headers.get("Range")))
            name = request.full_url.rsplit("/", 1)[1]
            data = (self.source / name).read_bytes()
            if corrupt == name:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            rng = request.headers.get("Range")
            if rng and status_for_range == 206:
                start = int(rng.split("=")[1].rstrip("-"))
                return _Resp(data[start:], 206)
            return _Resp(data, 200)
        return urlopen

    def test_real_manifest_is_pinned(self):
        manifest = MD.load_manifest()
        self.assertEqual(manifest.id, "clip-vit-b32")
        self.assertRegex(manifest.revision, r"^[0-9a-f]{40}$")
        self.assertEqual({f.role for f in manifest.files}, {"vision", "text", "tokenizer"})
        for f in manifest.files:
            self.assertRegex(f.sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(f.size, 1000)
            with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: ""}):
                url = manifest.url_of(f)
            self.assertTrue(url.startswith("https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/" +
                                           manifest.revision + "/"), url)
        self.assertLess(manifest.total_size, 500_000_000)

    def test_https_only_without_override(self):
        data = dict(self.data, url="http://example.com/{source}")
        manifest = fx.manifest_from(data)
        with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: ""}):
            with self.assertRaises(MD.ModelError):
                manifest.url_of(manifest.files[0])
        with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: "http://127.0.0.1:9/m"}):
            self.assertEqual(manifest.url_of(manifest.files[0]), "http://127.0.0.1:9/m/" + manifest.files[0].source)

    def test_download_verify_and_status(self):
        self.assertEqual(MD.status(self.models, self.manifest).state, MD.STATE_MISSING)
        calls, progress = [], []
        result = MD.download(self.models, self.manifest, urlopen=self.opener(calls),
                             on_progress=lambda d, t: progress.append((d, t)))
        self.assertTrue(result.ready)
        self.assertEqual(len(calls), 3)
        self.assertEqual(progress[-1], (self.manifest.total_size, self.manifest.total_size))
        record = json.loads((self.models / "models.json").read_text(encoding="utf-8"))
        self.assertEqual(set(record[self.manifest.id]["files"]), {f.name for f in self.manifest.files})
        # second run: nothing fetched
        MD.download(self.models, self.manifest, urlopen=self.opener(calls))
        self.assertEqual(len(calls), 3)
        MD.remove(self.models, self.manifest)
        self.assertEqual(MD.status(self.models, self.manifest).state, MD.STATE_MISSING)
        self.assertNotIn(self.manifest.id, json.loads((self.models / "models.json").read_text(encoding="utf-8")))

    def test_resume_from_part_with_range(self):
        folder = MD.model_dir(self.models, self.manifest)
        folder.mkdir(parents=True)
        first = self.manifest.files[0]
        data = (self.source / first.source).read_bytes()
        (folder / (first.name + ".part")).write_bytes(data[:100])
        self.assertEqual(MD.status(self.models, self.manifest).present_bytes, 100)
        calls = []
        self.assertTrue(MD.download(self.models, self.manifest, urlopen=self.opener(calls)).ready)
        self.assertEqual(calls[0][1], "bytes=100-")
        self.assertEqual((folder / first.name).read_bytes(), data)
        # a server that ignores Range (200) restarts the file
        MD.remove(self.models, self.manifest)
        (folder / (first.name + ".part")).write_bytes(b"garbage!")
        self.assertTrue(MD.download(self.models, self.manifest, urlopen=self.opener([], status_for_range=200)).ready)
        self.assertEqual((folder / first.name).read_bytes(), data)

    def test_checksum_mismatch_deletes_part(self):
        name = self.manifest.files[1].name
        with self.assertRaises(MD.ModelError):
            MD.download(self.models, self.manifest, urlopen=self.opener([], corrupt=name))
        folder = MD.model_dir(self.models, self.manifest)
        self.assertFalse((folder / name).exists())
        self.assertFalse((folder / (name + ".part")).exists())
        self.assertTrue((folder / self.manifest.files[0].name).exists())  # the verified first file stays

    def test_cancel_keeps_part(self):
        flags = {"n": 0}

        def cancelled():
            flags["n"] += 1
            return flags["n"] > 1

        with self.assertRaises(MD.DownloadCancelled):
            MD.download(self.models, self.manifest, urlopen=self.opener([]), cancelled=cancelled)
        folder = MD.model_dir(self.models, self.manifest)
        self.assertFalse((folder / self.manifest.files[0].name).exists())

    def test_copied_files_are_verified_once(self):
        folder = MD.model_dir(self.models, self.manifest)
        folder.mkdir(parents=True)
        for f in self.manifest.files:
            shutil.copyfile(self.source / f.source, folder / f.name)
        self.assertEqual(MD.status(self.models, self.manifest).state, MD.STATE_MISSING)  # no record yet
        calls = []
        self.assertTrue(MD.download(self.models, self.manifest, urlopen=self.opener(calls)).ready)
        self.assertEqual(calls, [])


# --- embedder ----------------------------------------------------------------------------------------------------


class Embedding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = scratch("deck-ai-embed-")
        cls.models = cls.dir / "models"
        cls.data = fx.write_tiny_model(cls.models)
        fx.install_tiny_model(cls.models, cls.data)
        cls.manifest = fx.manifest_from(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def test_preprocess_normalises_and_crops(self):
        frames = np.full((2, 224, 224, 3), 255, np.uint8)
        x = E.preprocess(frames)
        self.assertEqual(x.shape, (2, 3, 224, 224))
        self.assertAlmostEqual(float(x[0, 0, 0, 0]), (1 - 0.48145466) / 0.26862954, places=4)
        self.assertEqual(E.preprocess(frames, np.float16).dtype, np.float16)
        wide = np.zeros((1, 100, 400, 3), np.uint8)
        wide[:, :, 180:220] = 255  # centre band survives the centre crop
        cropped = E.center_crop_resize(wide[0])
        self.assertEqual(cropped.shape, (224, 224, 3))
        self.assertGreater(cropped[112, 112, 0], 200)
        self.assertLess(cropped[112, 5, 0], 50)
        with self.assertRaises(ValueError):
            E.preprocess(np.zeros((2, 224, 224), np.uint8))

    def test_cpu_embeddings_are_normalised_and_discriminative(self):
        embedder = E.ClipOnnxEmbedder(self.manifest, self.models, E.PROVIDER_CPU)
        frames = np.zeros((40, 224, 224, 3), np.uint8)
        frames[:20, ..., 0] = 255
        frames[20:, ..., 2] = 255
        vectors = embedder.embed_images(frames)  # two batches (32 + 8)
        self.assertEqual(vectors.shape, (40, 512))
        self.assertEqual(embedder.device, E.PROVIDER_CPU)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-4)
        self.assertGreater(float(vectors[0] @ vectors[19]), 0.999)
        self.assertLess(float(vectors[0] @ vectors[39]), 0.9)
        text = embedder.embed_text("a photo of a red car")
        self.assertEqual(text.shape, (512,))
        self.assertAlmostEqual(float(np.linalg.norm(text)), 1.0, places=4)
        self.assertEqual(embedder.embed_images(np.zeros((0, 224, 224, 3), np.uint8)).shape, (0, 512))

    def test_auto_provider_matches_cpu(self):
        frames = np.zeros((3, 224, 224, 3), np.uint8)
        frames[0, ..., 1] = 200
        cpu = E.ClipOnnxEmbedder(self.manifest, self.models, E.PROVIDER_CPU).embed_images(frames)
        auto = E.ClipOnnxEmbedder(self.manifest, self.models, E.PROVIDER_AUTO)
        np.testing.assert_allclose(auto.embed_images(frames), cpu, atol=2e-2)
        self.assertIn(auto.device, E.available_devices())
        self.assertEqual(E.resolve_device("cpu"), "cpu")
        self.assertIn(E.device_label(auto.device), ("GPU (DirectML)", "CPU · slow"))

    def test_missing_file_raises_embed_error(self):
        broken = dict(self.data)
        embedder = E.ClipOnnxEmbedder(fx.manifest_from(broken), self.dir / "nowhere", E.PROVIDER_CPU)
        with self.assertRaises(E.EmbedError):
            embedder.embed_images(np.zeros((1, 224, 224, 3), np.uint8))
        with self.assertRaises(E.EmbedError):
            embedder.embed_text("car")


# --- frames ------------------------------------------------------------------------------------------------------


class Frames(unittest.TestCase):
    def setUp(self):
        self.media = _Media.ensure()

    def test_step_plus_scene_cut(self):
        plan = FR.plan_for(3.0, 1.0, 600)
        got = list(FR.iter_frames("ffmpeg", str(self.media / "red_blue.mp4"), plan))
        times = [round(t, 2) for t, _ in got]
        self.assertEqual(times[0], 0.0)
        self.assertTrue(any(abs(t - 1.5) <= 0.05 for t in times), times)  # the cut
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertTrue(all(f.shape == (224, 224, 3) and f.dtype == np.uint8 for _, f in got))
        first, last = got[0][1], got[-1][1]
        self.assertGreater(int(first[112, 112, 0]), 200)  # red
        self.assertGreater(int(last[112, 112, 2]), 200)  # blue

    def test_cap_and_widened_step(self):
        plan = FR.plan_for(3000.0, 2.0, 100)
        self.assertGreater(plan.step, 30.0)
        capped = list(FR.iter_frames("ffmpeg", str(self.media / "green.mp4"), FR.FramePlan(0.1, 4)))
        self.assertEqual(len(capped), 4)

    def test_image_and_errors(self):
        got = list(FR.iter_frames("ffmpeg", str(self.media / "white.png"), FR.plan_for(None, 2, 600, image=True)))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], 0.0)
        with self.assertRaises(FR.FrameError):
            list(FR.iter_frames("ffmpeg", str(self.media / "missing.mp4"), FR.plan_for(3, 1, 10)))
        with self.assertRaises(FR.FrameError):
            list(FR.iter_frames(str(self.media / "no-ffmpeg.exe"), str(self.media / "green.mp4"),
                                FR.plan_for(3, 1, 10)))

    def test_cancel_stops_iteration(self):
        flag = {"n": 0}

        def cancelled():
            flag["n"] += 1
            return flag["n"] > 2

        got = list(FR.iter_frames("ffmpeg", str(self.media / "green.mp4"), FR.FramePlan(0.1, 600), cancelled=cancelled))
        self.assertEqual(len(got), 2)

    def test_pipe_start_failure_and_cancel(self):
        def broken(args):
            raise FileNotFoundError("ffmpeg is not available")

        with self.assertRaises(FR.FrameError):
            list(FR.iter_frames("ffmpeg", str(self.media / "green.mp4"), FR.plan_for(3, 1, 10), pipe=broken))
        started = []

        def pipe(args):
            proc = subprocess.Popen(["ffmpeg", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            started.append(proc)
            return proc

        got = list(FR.iter_frames("ffmpeg", str(self.media / "green.mp4"), FR.FramePlan(0.1, 600),
                                  cancelled=lambda: len(started) > 0, pipe=pipe))
        self.assertEqual(got, [])
        self.assertIsNotNone(started[0].poll(), "a cancel ends the process")

    def test_thumbnail(self):
        jpeg = FR.thumbnail_jpeg("ffmpeg", str(self.media / "red_blue.mp4"), 2.0)
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        self.assertTrue(FR.thumbnail_jpeg("ffmpeg", str(self.media / "white.png"), 0, image=True).startswith(b"\xff\xd8"))
        with self.assertRaises(FR.FrameError):
            FR.thumbnail_jpeg("ffmpeg", str(self.media / "missing.mp4"), 1.0)


# --- catalogue ---------------------------------------------------------------------------------------------------


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("deck-ai-cat-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.deck = self.dir / ".premiere-deck"

    def _put(self, catalog, key, n=3):
        emb = E.normalize(np.random.default_rng(len(key)).standard_normal((n, 512)))
        catalog.put(key, {"path": str(self.dir / f"{key}.mp4"), "node_id": "n" + key, "name": key,
                          "kind": "video", "duration": 3.0}, np.arange(n, dtype=np.float32), emb)
        return emb

    def test_round_trip_and_matrix_cache(self):
        catalog = IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(catalog.count(), 0)
        emb = self._put(catalog, "aa")
        self._put(catalog, "bb", 2)
        catalog.remember_path(str(self.dir / "aa.mp4"), "aa", 10, 1.5)
        catalog.save()
        again = IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(again.count(), 2)
        self.assertEqual(again.frame_count(), 5)
        self.assertEqual(again.known_hash(str(self.dir / "aa.mp4"), 10, 1.5), "aa")
        self.assertIsNone(again.known_hash(str(self.dir / "aa.mp4"), 11, 1.5))
        times, stored = again.load_clip("aa")
        self.assertEqual(stored.dtype, np.float16)
        np.testing.assert_allclose(stored.astype(np.float32), emb, atol=1e-3)
        m1 = again.matrix()
        self.assertEqual(m1.emb.shape, (5, 512))
        self.assertIs(again.matrix(), m1)
        self._put(again, "cc", 1)
        self.assertEqual(again.matrix().emb.shape, (6, 512))
        data = json.loads((self.deck / "index" / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual((data["version"], data["model"], data["dim"]), (2, "clip-vit-b32", 512))

    def test_migration_from_version_1(self):
        folder = self.deck / "index"
        folder.mkdir(parents=True)
        (folder / "catalog.json").write_text(json.dumps({
            "model": "clip-vit-b32", "dim": 512,
            "clips": {"aa": {"path": "D:/x/a.mp4", "node_id": "n1", "duration": 3, "frames": 2,
                             "indexed_at": "2026-10-01T10:00:00"}, "bad": "not a dict"}}), encoding="utf-8")
        catalog = IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(list(catalog.clips), ["aa"])
        self.assertEqual(catalog.clips["aa"]["name"], "a.mp4")
        self.assertEqual(catalog.clips["aa"]["kind"], "video")
        self.assertTrue(any("migrated" in note for note in catalog.notes))
        data = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["paths"], {})

    def test_model_change_resets_and_removes_npz(self):
        catalog = IX.Catalog.for_project(str(self.deck), "old-model", 512)
        self._put(catalog, "aa")
        self.assertTrue(catalog.npz_path("aa").exists())
        fresh = IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(fresh.count(), 0)
        self.assertFalse(fresh.npz_path("aa").exists())
        self.assertTrue(any("model changed" in note for note in fresh.notes))

    def test_broken_and_future_catalogues_are_set_aside(self):
        folder = self.deck / "index"
        folder.mkdir(parents=True)
        (folder / "catalog.json").write_text("{not json", encoding="utf-8")
        catalog = IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(catalog.count(), 0)
        self.assertTrue(list(folder.glob("catalog.json.bad-*")))
        (folder / "catalog.json").write_text(json.dumps({"version": 99, "model": "clip-vit-b32", "clips": {}}),
                                             encoding="utf-8")
        time.sleep(1.1)  # distinct quarantine name
        IX.Catalog.for_project(str(self.deck), "clip-vit-b32", 512)
        self.assertEqual(len(list(folder.glob("catalog.json.bad-*"))), 2)

    def test_candidates_and_exclusions(self):
        items = [
            ProjectItem("b1", "Footage", "bin"),
            ProjectItem("1", "a.mp4", "clip", "Footage", "D:/m/a.mp4", duration=10, has_video=True),
            ProjectItem("2", "a copy", "clip", "Other", "D:/m/A.mp4", duration=10, has_video=True),
            ProjectItem("3", "song.wav", "clip", "Audio", "D:/m/song.wav", duration=100),
            ProjectItem("4", "off.mp4", "clip", "Footage", "D:/m/off.mp4", is_offline=True),
            ProjectItem("5", "lt.mov", "clip", "Graphics/Lower thirds", "D:/m/lt.mov", duration=5),
            ProjectItem("6", "o.mp4", "clip", "Footage/Overlays", "D:/m/o.mp4", duration=5),
            ProjectItem("7", "short.mp4", "clip", "Footage", "D:/m/short.mp4", duration=0.4),
            ProjectItem("8", "still.png", "clip", "Stills", "D:/m/still.png"),
            ProjectItem("9", "Seq", "sequence", "", None, is_sequence=True),
            ProjectItem("10", "voice.mp4", "clip", "Footage", "D:/m/voice.mp4", has_video=False),
            ProjectItem("11", "keep.mp4", "clip", "Footage/Day1", "D:/m/keep.mp4", duration=5),
        ]
        chosen, skipped = IX.select_candidates(items, exclude_bins=["Overlays", "graphics"], min_duration=1.0)
        self.assertEqual([c.node_id for c in chosen], ["1", "8", "11"])
        self.assertEqual(chosen[1].kind, "image")
        reasons = {s.node_id: s.reason for s in skipped}
        self.assertEqual(reasons, {"3": "No picture", "4": "Offline", "5": "Excluded bin", "6": "Excluded bin",
                                   "7": "Too short", "10": "No picture"})
        self.assertTrue(IX.bin_excluded("Footage/Day1", ["footage/day1"]))
        self.assertFalse(IX.bin_excluded("Footage/Day10", ["Footage/Day1"]))
        self.assertFalse(IX.bin_excluded("", ["Overlays"]))


# --- indexing job ------------------------------------------------------------------------------------------------


class IndexJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = _Media.ensure()
        cls.dir = scratch("deck-ai-job-")
        models = cls.dir / "models"
        data = fx.write_tiny_model(models)
        fx.install_tiny_model(models, data)
        cls.embedder = E.ClipOnnxEmbedder(fx.manifest_from(data), models, E.PROVIDER_CPU)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def setUp(self):
        self.deck = scratch("deck-ai-proj-") / ".premiere-deck"
        self.catalog = IX.Catalog.for_project(str(self.deck), "clip-vit-b32-tiny", 512)
        self.cands = [IX.Candidate("n1", "red_blue.mp4", str(self.media / "red_blue.mp4"), "video", 3.0),
                      IX.Candidate("n2", "green.mp4", str(self.media / "green.mp4"), "video", 3.0),
                      IX.Candidate("n3", "white.png", str(self.media / "white.png"), "image", None),
                      IX.Candidate("n4", "gone.mp4", str(self.media / "gone.mp4"), "video", 3.0)]

    def run_index(self, job, cands, **kw):
        return IX.run_index(job, catalog=self.catalog, candidates=cands, embed=self.embedder.embed_images,
                            ffmpeg="ffmpeg", step=1.0, max_frames=600, **kw)

    def test_index_all_and_skip_known(self):
        job = _Job()
        result = self.run_index(job, self.cands)
        self.assertEqual((result.indexed, result.known, result.cancelled), (3, 0, False))
        self.assertEqual(result.failed, {"n4": "File not found"})
        self.assertEqual(self.catalog.count(), 3)
        self.assertTrue(any(label.startswith("Indexing 2 of 4 clips") for _, label in job.log))
        self.assertEqual(job.log[-1], (1.0, "Index up to date"))
        self.assertEqual(IX.pending(self.catalog, self.cands[:3]), [])
        again = self.run_index(_Job(), self.cands[:3])
        self.assertEqual((again.indexed, again.known), (0, 3))

    def test_resume_after_cancel(self):
        first = self.run_index(_Job(cancel_after_progress=1), self.cands[:3])  # cancelled before clip 2 starts
        self.assertTrue(first.cancelled)
        self.assertEqual(first.indexed, 1)
        self.assertEqual(self.catalog.count(), 1)
        reloaded = IX.Catalog.for_project(str(self.deck), "clip-vit-b32-tiny", 512)
        self.assertEqual(reloaded.count(), 1)
        self.catalog = reloaded
        pending = IX.pending(reloaded, self.cands[:3])
        self.assertEqual([c.node_id for c in pending], ["n2", "n3"])
        second = self.run_index(_Job(), pending)
        self.assertEqual((second.indexed, second.cancelled), (2, False))
        self.assertEqual(reloaded.count(), 3)

    def test_relinked_copy_reuses_index(self):
        self.run_index(_Job(), self.cands[:1])
        copy = scratch("deck-ai-copy-") / "moved.mp4"
        shutil.copyfile(self.cands[0].path, copy)
        result = self.run_index(_Job(), [IX.Candidate("n9", "moved.mp4", str(copy), "video", 3.0)])
        self.assertEqual((result.indexed, result.known), (0, 1))
        key = next(iter(self.catalog.clips))
        self.assertEqual(self.catalog.clips[key]["node_id"], "n9")

    def test_pauses_while_high_priority_job_runs(self):
        answers = iter([True, True, False])
        job = _Job()
        self.run_index(job, self.cands[:1], paused=lambda: next(answers, False), pause_poll=0.0)
        labels = [label for _, label in job.log]
        self.assertEqual(labels.count("Paused while files are copied"), 2)

    def test_frames_come_through_the_host_pipe(self):
        calls = []

        def pipe(args):
            calls.append(list(args))
            return subprocess.Popen(["ffmpeg", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

        result = self.run_index(_Job(), self.cands[:1], pipe=pipe)
        self.assertEqual(result.indexed, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("-i") + 1], self.cands[0].path)
        self.assertNotIn("ffmpeg", calls[0][:1], "the host prepends the executable")

    def test_frames_match_times(self):
        self.run_index(_Job(), self.cands[:1])
        key = next(iter(self.catalog.clips))
        times, emb = self.catalog.load_clip(key)
        self.assertEqual(len(times), len(emb))
        self.assertTrue(any(abs(float(t) - 1.5) <= 0.05 for t in times))
        red, blue = emb[0].astype(np.float32), emb[-1].astype(np.float32)
        self.assertLess(float(red @ blue), 0.95)


# --- query -------------------------------------------------------------------------------------------------------


class Query(unittest.TestCase):
    def matrix(self):
        rng = np.random.default_rng(7)
        q = E.normalize(rng.standard_normal(512))
        noise = E.normalize(rng.standard_normal((12, 512)))

        def towards(strength):
            v = E.normalize(noise[0] * (1 - strength) + q * strength)
            return v

        # clip 0: strong at 1.0, also strong at 1.5 (suppressed, < 2 s), 4.0 and 8.0; clip 1: medium; clip 2: weak
        rows = [towards(0.9), towards(0.85), towards(0.7), towards(0.6), towards(0.5),
                towards(0.8), towards(0.1),
                E.normalize(noise[5])]
        owner = np.array([0, 0, 0, 0, 0, 1, 1, 2], np.int32)
        times = np.array([1.0, 1.5, 4.0, 8.0, 12.0, 2.0, 9.0, 0.0], np.float32)
        return IX.Matrix(["c0", "c1", "c2"], np.stack(rows).astype(np.float32), owner, times), q

    def test_nms_ranking_threshold(self):
        matrix, q = self.matrix()
        ranked = Q.rank(matrix, q, threshold=0.3, per_clip=3)
        self.assertEqual([r.key for r in ranked], ["c0", "c1"])
        self.assertEqual(ranked[0].times, (1.0, 4.0, 8.0))  # 1.5 suppressed by the 2 s window
        self.assertEqual(len(ranked[1].times), 1)
        self.assertTrue(all(a >= b for a, b in zip(ranked[0].scores, ranked[0].scores[1:])))
        self.assertGreater(ranked[0].score, ranked[1].score)
        self.assertEqual([r.key for r in Q.rank(matrix, q, threshold=0.3, top=1)], ["c0"])
        self.assertEqual(len(Q.rank(matrix, q, threshold=0.3, per_clip=1)[0].times), 1)
        self.assertEqual(Q.rank(matrix, q, threshold=1.01), [])
        empty = IX.Matrix([], np.zeros((0, 512), np.float32), np.zeros(0, np.int32), np.zeros(0, np.float32))
        self.assertEqual(Q.rank(empty, q), [])

    def test_bars_and_timecode(self):
        self.assertEqual(Q.bar_value(0.3, 0.3, 0.18), 1.0)
        self.assertEqual(Q.bar_value(0.18, 0.3, 0.18), 0.2)
        self.assertEqual(Q.timecode(72.9), "00:01:12")
        self.assertEqual(Q.timecode(3725), "01:02:05")

    def test_prompt_template(self):
        self.assertEqual(Q.prompt_for("car"), "a photo of car")
        self.assertEqual(Q.prompt_for("  a red  car "), "a red car")
        self.assertEqual(Q.prompt_for("the word STOP on a sign"), "the word STOP on a sign")

    def test_text_cache(self):
        calls = []

        def embed(text):
            calls.append(text)
            return np.full(4, len(calls), np.float32)

        cache = Q.TextCache(embed, size=2)
        first = cache.get("Car")
        self.assertIs(cache.get(" car "), first)
        self.assertEqual(calls, ["a photo of Car"])
        cache.get("sky")
        cache.get("sea")  # evicts "car"
        cache.get("car")
        self.assertEqual(cache.misses, 4)
        cache.clear()
        cache.get("sky")
        self.assertEqual(cache.misses, 5)

    def test_thumb_cache_eviction(self):
        folder = scratch("deck-ai-thumbs-")
        self.addCleanup(shutil.rmtree, folder, True)
        cache = Q.ThumbCache(folder, max_bytes=2500)
        made = []

        def maker(tag):
            def make():
                made.append(tag)
                return bytes(1000)
            return make

        cache.get("a", 1.0, 160, maker("a"))
        past = time.time() - 100
        os.utime(cache.path_for("a", 1.0, 160), (past, past))
        cache.get("b", 1.0, 160, maker("b"))
        os.utime(cache.path_for("b", 1.0, 160), (past + 10, past + 10))
        cache.get("a", 1.0, 160, maker("a2"))  # hit: refreshes a
        self.assertEqual(made, ["a", "b"])
        cache.get("c", 1.0, 160, maker("c"))  # 3000 bytes > 2500: evicts b (oldest)
        self.assertTrue(cache.path_for("a", 1.0, 160).exists())
        self.assertFalse(cache.path_for("b", 1.0, 160).exists())
        self.assertTrue(cache.path_for("c", 1.0, 160).exists())
        self.assertLessEqual(cache.size(), 2500)
        cache.clear()
        self.assertEqual(cache.size(), 0)
        self.assertTrue(Q.data_url(b"\xff\xd8x").startswith("data:image/jpeg;base64,"))


# --- the module --------------------------------------------------------------------------------------------------


def real_ffmpeg_pipe(args):
    """What HostTools.ffmpeg_pipe does, without a host: ffmpeg from PATH with stdout and stderr as pipes."""
    return subprocess.Popen(["ffmpeg", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


class ModuleCase(unittest.TestCase):
    manual_jobs = False

    def setUp(self):
        self.media = _Media.ensure()
        self.home = os.environ.get("PREMIERE_DECK_HOME")
        self.addCleanup(self._restore_home)
        self.make()

    def _restore_home(self):
        if self.home is None:
            os.environ.pop("PREMIERE_DECK_HOME", None)
        else:
            os.environ["PREMIERE_DECK_HOME"] = self.home

    def make(self, settings=None, project_settings=None, manual=None, install=True):
        os.environ["PREMIERE_DECK_HOME"] = str(scratch("deck-ai-home-"))
        self.host = FakeHost(settings=settings, project_settings=project_settings,
                             manual_jobs=self.manual_jobs if manual is None else manual, module_id="search")
        self.host.tools.pipe_factory = real_ffmpeg_pipe  # the module reads frames through Tools.ffmpeg_pipe
        self.data = fx.write_tiny_model(self.host.paths.models_dir)
        if install:
            fx.install_tiny_model(self.host.paths.models_dir, self.data)
        self.project = self.host.make_project()
        self.host.premiere.set_items([
            ProjectItem("b1", "Footage", "bin"),
            ProjectItem("n1", "red_blue.mp4", "clip", "Footage", str(self.media / "red_blue.mp4"), duration=3.0,
                        has_video=True),
            ProjectItem("n2", "green.mp4", "clip", "Footage", str(self.media / "green.mp4"), duration=3.0,
                        has_video=True),
            ProjectItem("n3", "white.png", "clip", "Stills", str(self.media / "white.png")),
            ProjectItem("n4", "logo.mp4", "clip", "Graphics", str(self.media / "yellow.mp4"), duration=2.0),
        ])
        self.module = AiSearchModule(self.host.context())
        self.module.manifest = fx.manifest_from(self.data)
        self.module.AUTO_INDEX_DELAY = 0.0
        self.module.start()
        return self.module

    def validate(self):
        sec = self.module.section()
        P.validate_tree({"type": "ui", "revision": 1, "title": "", "status": None, "sections": [sec], "footer": None})
        page = self.module.render_host()
        json.dumps(page)
        for block in page["blocks"]:
            self.assertIn(block["type"], ("cards", "grid", "table", "form", "actions"))
        return sec, page

    def ids(self, nodes):
        out = []
        for n in nodes:
            if "id" in n:
                out.append(n["id"])
            out += self.ids(n.get("items", []))
        return out

    def texts(self, nodes):
        out = []
        for n in nodes:
            out += [n.get("text", ""), n.get("label", ""), n.get("empty", "")]
            out += self.texts(n.get("items", []))
        return " | ".join(out)


class ModuleStates(ModuleCase):
    def test_model_missing(self):
        self.make(install=False)
        self.module.project_opened(self.project)
        sec, page = self.validate()
        self.assertEqual(sec["status"]["text"], "Model not downloaded")
        self.assertIn("Enable AI search in the Premiere Deck window", self.texts(sec["items"]))
        self.assertIn("search.download", self.ids(sec["items"]))
        card = page["blocks"][0]["items"][0]
        self.assertEqual((card["value"], card["actions"][0]["id"]), ("Not downloaded", "search.download"))
        self.assertEqual(self.module.on_action("search.query", "car").toast[0], "warn")
        self.assertEqual(self.module.on_action("search.index").toast[0], "warn")

    def test_download_flow_through_override(self):
        self.make(install=False, manual=True)
        self.module.project_opened(self.project)
        source = Path(self.data["_source"]).as_uri()
        with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: source}):
            self.assertIsNotNone(self.module.on_action("search.download"))
            sec, page = self.validate()
            self.assertEqual(sec["status"]["text"], "Downloading model")
            self.assertTrue(any(n["kind"] == "progress" for n in sec["items"]))
            self.assertEqual(page["blocks"][0]["items"][0]["value"], "Downloading")
            self.assertIn("search.cancel_download", self.ids(sec["items"]))
            self.host.jobs.step()
        self.assertTrue(self.module.model_ready)
        self.assertIn(("ok", "Search model ready"), self.host.ui.toasts)
        # auto_index (default on) indexed the open project right after the download (same step() drain)
        self.assertEqual([j.title for j in self.host.jobs.jobs], ["Downloading the search model", "Indexing footage"])
        self.assertEqual(self.module._catalog.count(), 3)

    def test_download_cancel_and_failure(self):
        self.make(install=False, manual=True)
        source = Path(self.data["_source"]).as_uri()
        with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: source}):
            self.module.on_action("search.download")
            self.module.on_action("search.cancel_download")
            self.host.jobs.step()
        self.assertFalse(self.module.model_ready)
        self.assertEqual(self.host.ui.toasts[-1][0], "neutral")
        with mock.patch.dict(os.environ, {MD.URL_OVERRIDE_ENV: (self.host.root / "nothing").as_uri()}):
            self.module.on_action("search.download")
            self.host.jobs.step()
        self.assertEqual(self.host.ui.toasts[-1][0], "error")
        sec, _ = self.validate()
        self.assertTrue(any(n.get("tone") == "error" for n in sec["items"]))

    def test_no_project_and_off(self):
        self.module.project_closed()
        sec, page = self.validate()
        self.assertEqual(sec["status"]["text"], "No project")
        self.assertEqual(page["blocks"][0]["items"][1]["value"], "No project")
        self.module.project_opened(self.project)
        self.host.settings.project.set("enabled", False)
        sec, _ = self.validate()
        self.assertEqual(sec["status"]["text"], "Off for this project")
        self.assertEqual(self.ids(sec["items"]), ["search.enabled"])
        self.module.on_action("search.enabled", True)
        self.assertTrue(self.host.settings.project.get("enabled"))

    def test_ready_index_search_results_and_actions(self):
        self.module.project_opened(self.project)  # auto index runs inline
        sec, page = self.validate()
        self.assertEqual(self.module._catalog.count(), 3)  # logo.mp4 sits in Graphics (excluded)
        self.assertEqual(sec["status"]["text"], "3 clips indexed · 0 pending" +
                         (" · CPU" if E.resolve_device("auto") == "cpu" else ""))
        self.assertIn("Describe what the frame shows", self.texts(sec["items"]))
        table = next(b for b in page["blocks"] if b["type"] == "table")
        states = {r["id"]: r["state"] for r in table["rows"]}
        self.assertEqual(states, {"n1": "Indexed", "n2": "Indexed", "n3": "Indexed", "n4": "Skipped: Excluded bin"})
        self.module.threshold = -1.0
        self.module.on_action("search.query", "red frame")
        sec, page = self.validate()
        rows = next(n for n in sec["items"] if n["kind"] == "list")["rows"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["thumb"] and r["thumb"].startswith("data:image/jpeg;base64,") for r in rows))
        self.assertEqual(rows[0]["bar"], 1.0)
        self.assertTrue(all(0.2 <= r["bar"] <= 1.0 for r in rows))
        self.assertNotIn("\u25b0", rows[0]["subtitle"])  # the bar is a field, not glyphs
        self.assertNotRegex(rows[0]["subtitle"], r"0\.\d")  # no score numbers
        self.assertEqual([a["id"] for a in rows[0]["actions"]], ["search.open", "search.insert", "search.mark"])
        grid = next(b for b in page["blocks"] if b["type"] == "grid")
        self.assertEqual(len(grid["items"]), 3)
        self.assertEqual(grid["items"][0]["bar"], 1.0)
        red_blue = next(r for r in rows if r["title"] == "red_blue.mp4")
        hit = self.module._hit(red_blue["id"])
        self.assertEqual(hit.node_id, "n1")
        pr = self.host.premiere
        self.assertEqual(self.module.on_action("search.open", None, red_blue["id"]).toast[0], "ok")
        self.assertEqual(pr.calls_named("open_in_source")[-1], (("n1",), {"seconds": hit.time}))
        self.module.on_action("search.open", 2.5, red_blue["id"])
        self.assertEqual(pr.calls_named("open_in_source")[-1][1]["seconds"], 2.5)
        self.module.on_action("search.insert", None, red_blue["id"])
        args, kw = pr.calls_named("insert_clip")[-1]
        self.assertEqual(args, ("n1",))
        self.assertAlmostEqual(kw["start"], max(0.0, hit.time - 2.0))
        self.assertAlmostEqual(kw["end"], min(3.0, hit.time + 2.0))
        self.module.on_action("search.mark", None, red_blue["id"])
        self.assertEqual(pr.calls_named("add_marker")[-1][0], ("n1", hit.time, "Deck: red frame", ""))
        still = next(r for r in rows if r["title"] == "white.png")
        self.module.on_action("search.insert", None, still["id"])
        self.assertEqual(pr.calls_named("insert_clip")[-1][1]["start"], 0.0)
        self.assertEqual(pr.calls_named("insert_clip")[-1][1]["end"], 4.0)
        pr.eval_errors = {}
        with mock.patch.object(pr, "open_in_source", side_effect=PremiereError("No such item")):
            self.assertEqual(self.module.on_action("search.open", None, red_blue["id"]).toast,
                             ("error", "Premiere: No such item"))
        self.assertEqual(self.module.on_action("search.open", None, "nope").toast[0], "warn")
        self.module.on_action("search.clear")
        sec, _ = self.validate()
        self.assertEqual(next(n for n in sec["items"] if n["kind"] == "list")["rows"], [])
        # the query cache served the repeated text without a second model call
        self.module.on_action("search.query", "red frame")
        self.module.on_action("search.query", "RED  frame")
        self.assertEqual(self.module._text_cache.misses, 1)

    def test_empty_results(self):
        self.module.project_opened(self.project)
        self.module.threshold = 1.01
        self.module.on_action("search.query", "car")
        sec, page = self.validate()
        lst = next(n for n in sec["items"] if n["kind"] == "list")
        self.assertEqual((lst["rows"], lst["empty"]), ([], "No matching frames"))
        self.assertEqual(next(b for b in page["blocks"] if b["type"] == "grid")["empty"], "No matching frames")
        self.assertIsNone(self.module.on_action("search.query", "   ").toast)
        self.assertEqual(self.module._query, "")

    def test_nothing_indexed_yet(self):
        self.make(project_settings={"auto_index": False})
        self.module.project_opened(self.project)
        self.assertEqual(self.module._catalog.count(), 0)
        sec, _ = self.validate()
        self.assertEqual(sec["status"]["text"].split(" · ")[:2], ["0 clips indexed", "3 pending"])
        self.assertEqual(self.module.on_action("search.query", "car").toast[0], "neutral")
        result = self.module.on_action("search.index")
        self.assertIsNone(result.toast)
        self.assertEqual(self.module._catalog.count(), 3)
        self.assertTrue(self.host.tools.pipe_calls, "frames are read through ctx.tools.ffmpeg_pipe")
        self.assertEqual(self.host.ui.toasts[-1], ("ok", "Indexed 3 clips"))
        self.assertEqual(self.module.on_action("search.index").toast, ("ok", "The index is up to date"))
        self.module.on_action("search.rebuild")
        self.assertEqual(self.module._catalog.count(), 3)

    def test_busy_follows_the_jobs_flag(self):
        self.assertFalse(self.module._high_priority_busy())
        self.host.jobs.high_priority = True
        self.assertTrue(self.module._high_priority_busy())

    def test_failed_clip_is_reported(self):
        self.make(project_settings={"auto_index": False})
        broken = self.host.root / "broken.mp4"
        broken.write_bytes(b"not a video at all" * 100)
        items = list(self.host.premiere.snapshot_value.items) + [
            ProjectItem("n5", "broken.mp4", "clip", "Footage", str(broken), duration=3.0)]
        self.host.premiere.set_items(items)
        self.module.project_opened(self.project)
        self.module.on_action("search.index")
        self.assertEqual(self.host.ui.toasts[-1], ("warn", "Indexed 3 clips · 1 could not be read"))
        _, page = self.validate()
        table = next(b for b in page["blocks"] if b["type"] == "table")
        self.assertEqual(next(r for r in table["rows"] if r["id"] == "n5")["tone"], "error")


class ModuleJobsManual(ModuleCase):
    manual_jobs = True

    def test_indexing_state_and_cancel(self):
        self.module.project_opened(self.project)  # auto index queued, not run
        sec, page = self.validate()
        self.assertTrue(sec["status"]["text"].startswith("Indexing"))
        self.assertIn("search.cancel", self.ids(sec["items"]))
        index_button = next(n for n in self.ids(sec["items"]) if n == "search.index")
        self.assertTrue(index_button)
        self.assertEqual(page["blocks"][0]["items"][1]["tone"], "accent")
        self.assertEqual(self.module.on_action("search.index").toast, ("neutral", "Indexing is already running"))
        self.module.on_action("search.cancel")
        self.host.jobs.step()
        self.assertIsNone(self.module._index_job)
        self.assertEqual(self.module._catalog.count(), 0)
        self.assertEqual(self.host.ui.toasts[-1], ("neutral", "Indexing stopped · 0 clips indexed"))
        self.module.on_action("search.index")
        self.host.jobs.step()
        self.assertEqual(self.module._catalog.count(), 3)

    def test_searching_state(self):
        self.module.project_opened(self.project)
        self.host.jobs.step()
        self.module.threshold = -1.0
        self.module.on_action("search.query", "green")
        sec, page = self.validate()
        self.assertTrue(any(n["kind"] == "progress" and n["label"] == "Searching" for n in sec["items"]))
        self.assertTrue(next(b for b in page["blocks"] if b["type"] == "grid")["busy"])
        self.module.on_action("search.query", "blue")  # supersedes the first search
        self.host.jobs.step()
        self.assertEqual(self.module._searched, "blue")
        self.assertEqual(len(self.module._hits), 3)

    def test_items_added_is_debounced_then_indexed(self):
        self.module.project_opened(self.project)
        self.host.jobs.step()
        self.assertEqual(self.module._catalog.count(), 3)
        items = list(self.host.premiere.snapshot_value.items) + [
            ProjectItem("n6", "yellow.mp4", "clip", "Footage", str(self.media / "yellow.mp4"), duration=2.0)]
        self.host.premiere.set_items(items)
        self.module.on_event("project.items_added", None)
        first = self.module._refresh_job
        self.module.on_event("project.items_added", None)  # restarts the wait
        self.assertTrue(first.cancelled)
        self.host.jobs.step()
        self.assertEqual(self.module._catalog.count(), 4)

    def test_items_added_while_indexing_rescans_after(self):
        self.module.project_opened(self.project)  # index queued
        items = list(self.host.premiere.snapshot_value.items) + [
            ProjectItem("n6", "yellow.mp4", "clip", "Footage", str(self.media / "yellow.mp4"), duration=2.0)]
        self.host.premiere.set_items(items)
        self.module.on_event("project.items_added", None)
        self.assertIsNone(self.module._refresh_job)  # no waiting job next to the running index
        self.host.jobs.step()  # index (3 clips), then the rescan wait, then the index of the new clip
        self.assertEqual(self.module._catalog.count(), 4)
        self.assertEqual([j.title for j in self.host.jobs.jobs],
                         ["Indexing footage", "Waiting for new clips", "Indexing footage"])


class ModuleSettings(ModuleCase):
    def test_bad_values_fall_back(self):
        with self.assertLogs("module.search.test", "WARNING") as logs:
            self._bad_values()
        self.assertTrue(any("step_seconds" in line for line in logs.output))

    def _bad_values(self):
        self.make(settings={"provider": "gpu9", "step_seconds": "abc", "max_frames_per_clip": -5,
                            "clip_pad": True, "thumb_cache_mb": "big"},
                  project_settings={"enabled": "maybe", "auto_index": None, "exclude_bins": 5, "min_duration": -1})
        m = self.module
        self.assertEqual(m.setting("provider"), "auto")
        self.assertEqual(m.setting("step_seconds"), 2.0)
        self.assertEqual(m.setting("max_frames_per_clip"), 600)
        self.assertEqual(m.setting("clip_pad"), 2.0)
        self.assertEqual(m.setting("thumb_cache_mb"), 200)
        self.assertIs(m.setting("enabled"), True)
        self.assertIs(m.setting("auto_index"), True)
        self.assertEqual(m.setting("exclude_bins"), ["Overlays", "Graphics"])
        self.assertEqual(m.setting("min_duration"), 1.0)

    def test_good_values_and_setting_action(self):
        self.make(settings={"provider": "cpu", "step_seconds": "4", "clip_pad": "3"},
                  project_settings={"exclude_bins": "B-roll, Footage/Old/", "auto_index": "off"})
        m = self.module
        self.assertEqual((m.setting("provider"), m.setting("step_seconds"), m.setting("clip_pad")), ("cpu", 4.0, 3.0))
        self.assertEqual(m.setting("exclude_bins"), ["B-roll", "Footage/Old"])
        self.assertIs(m.setting("auto_index"), False)
        m.project_opened(self.project)
        self.assertIsNone(m.on_action("search.setting", {"key": "step_seconds", "value": 5}).toast)
        self.assertEqual(self.host.settings.get("step_seconds"), 5.0)
        self.assertEqual(m.on_action("search.setting", {"key": "step_seconds", "value": 0}).toast[0], "warn")
        self.assertEqual(m.on_action("search.setting", {"key": "nope", "value": 1}).toast[0], "warn")
        self.assertEqual(m.on_action("search.setting", "junk").toast[0], "warn")
        m.on_action("search.setting", {"key": "exclude_bins", "value": "Footage"})
        self.assertEqual([c.node_id for c in m._candidates], ["n3", "n4"])
        m._get_embedder()
        m.on_action("search.setting", {"key": "provider", "value": "directml"})
        self.assertIsNone(m.embedder)  # rebuilt with the new provider on next use
        m.on_action("search.auto_index", True)
        self.assertIs(self.host.settings.project.get("auto_index"), True)
        self.assertEqual(m.on_action("search.auto_index", "maybe").toast[0], "warn")
        fields = next(b for b in m.render_host()["blocks"] if b["id"] == "search.settings")["fields"]
        by_key = {f["key"]: f for f in fields}
        self.assertEqual(by_key["exclude_bins"]["value"], "Footage")
        self.assertEqual(by_key["clip_pad"]["value"], "3")
        self.assertEqual(by_key["step_seconds"]["value"], 5)
        self.assertEqual(len(m.settings_schema()), 9)
        m.project_closed()
        self.assertEqual(m.on_action("search.setting", {"key": "enabled", "value": True}).toast[0], "warn")
        self.assertNotIn("enabled", {f["key"] for f in
                                     next(b for b in m.render_host()["blocks"] if b["id"] == "search.settings")["fields"]})


class ModuleActions(ModuleCase):
    def test_every_action_id(self):
        m = self.module
        m.project_opened(self.project)
        m.threshold = -1.0
        handled = {}
        for action, value in [("query", "red"), ("clear", None), ("index", None), ("rebuild", None),
                              ("cancel", None), ("download", None), ("cancel_download", None),
                              ("clear_thumbs", None), ("auto_index", False), ("enabled", True),
                              ("setting", {"key": "thumb_cache_mb", "value": 50})]:
            result = m.on_action(f"search.{action}", value)
            handled[action] = result
            self.assertFalse(result is not None and result.toast and "unknown action" in result.toast[1], action)
        self.assertEqual(handled["download"].toast, ("neutral", "The search model is already downloaded"))
        m.on_action("search.query", "red")
        key = m._hits[0].key
        for action in ("open", "insert", "mark"):
            self.assertEqual(m.on_action(f"search.{action}", None, key).toast[0], "ok", action)
        # window-only ids arrive with a host: prefix
        self.assertEqual(m.on_action("host:search.open", None, key).toast[0], "ok")
        self.assertEqual(m.on_action("search.host:mark", None, key).toast[0], "ok")
        self.assertEqual(m.on_action("search.remove_model").toast, ("ok", "Search model removed"))
        self.assertFalse(m.model_ready)
        unknown = m.on_action("search.nothing")
        self.assertEqual(unknown.toast[0], "warn")
        self.assertFalse(unknown.rerender)
        m.stop()

    def test_removed_clip_is_not_a_hit(self):
        m = self.module
        m.project_opened(self.project)
        m.threshold = -1.0
        items = [i for i in self.host.premiere.snapshot_value.items if i.node_id != "n2"]
        self.host.premiere.set_items(items)
        m.on_event("project.changed", None)  # debounce 0 -> refresh
        m.on_action("search.query", "green")
        self.assertEqual(sorted(h.name for h in m._hits), ["red_blue.mp4", "white.png"])

    def test_hit_with_stale_node(self):
        m = self.module
        m.project_opened(self.project)
        m.threshold = -1.0
        m.on_action("search.query", "red")
        m._hits[0].node_id = ""
        self.assertEqual(m.on_action("search.open", None, m._hits[0].key).toast[0], "warn")


if __name__ == "__main__":
    unittest.main()
