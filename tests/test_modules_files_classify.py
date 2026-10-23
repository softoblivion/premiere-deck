"""The classifier of docs/FILES.md 3.8: video source signals one by one and in order, the audio class (decision,
decode arguments, failures, cache) with a fake scorer, and the bundled YAMNet model on the CPU with media made by
ffmpeg (and a spoken sentence from the Windows speech synthesizer when it is available)."""

import os
import subprocess
import time
import unittest
from dataclasses import replace

from premiere_deck.core.media import MediaInfo
from premiere_deck.core.probe import probe
from premiere_deck.modules.files import media_class as MC
from premiere_deck.modules.files.classify import classify, parse_overrides
from premiere_deck.modules.files.layout import default_layout
from tests.test_modules_files_support import (
    ffmpeg_available, make_classifier_media, make_speech, remove_tree, scratch, write_file,
)


def video(name, **kw):
    kw.setdefault("has_audio", True)
    kw.setdefault("nb_streams", 2)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return MediaInfo(path=os.path.join("C:\\media", name), kind="video", ext=ext, size=1, **kw)


GENERATOR = dict(has_audio=False, nb_streams=1, duration=5.0, fps=24.0, width=1280, height=720)


def real_pipe(args):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(["ffmpeg", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)


class VideoSource(unittest.TestCase):
    def source(self, name, **kw):
        return MC.video_source(video(name, **kw))

    def test_generator_names_are_whole_words(self):
        for name in ("kling_0012.mp4", "veo3 shot.mp4", "Runway Gen-3 test.mp4", "my gen4 take.mp4", "SORA-city.mp4",
                     "hailuo_02.mp4", "minimax.mp4", "wan 2.1 clip.mp4", "luma-dream.mp4", "pika.mp4",
                     "midjourney video.mp4", "generated_01.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.source(name), "generated")
        for name in ("wanderlust.mp4", "swan_lake.mp4", "veolia.mp4", "pikachu.mp4", "luminous.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.source(name), "unknown")

    def test_stock_site_names(self):
        for name in ("pexels-forest.mp4", "Pixabay_123.mp4", "shutterstock_1234.mov", "iStock-99.mp4",
                     "AdobeStock_55.mov", "artgrid city.mp4", "envato_elements.mp4", "storyblocks-x.mp4",
                     "videvo.mp4", "videezy - y.mp4", "mixkit-sea.mp4", "coverr-night.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.source(name), "stock")

    def test_camera_tags(self):
        for tags in ({"make": "Canon"}, {"model": "ILCE-7SM3"}, {"com.apple.quicktime.make": "Apple"},
                     {"com.apple.quicktime.model": "iPhone 15"}, {"com.android.manufacturer": "Google"},
                     {"encoder": "DJI Mavic"}, {"handler_name": "GoPro AVC"}, {"major_brand": "XAVC"},
                     {"major_brand": "CAEP"}):
            with self.subTest(tags=tags):
                self.assertEqual(self.source("clip.mp4", tags=tags), "camera")
        self.assertEqual(self.source("clip.mp4", tags={"handler_name": "VideoHandler"}), "unknown")
        self.assertEqual(self.source("clip.mp4", tags={"encoder": "Credits"}), "unknown")  # "red" is a whole word

    def test_camera_names_and_formats(self):
        for name in ("C0042.MP4", "DSC_0012.MOV", "IMG_1234.MOV", "MVI_0001.MOV", "MVI0001.MOV", "DJI_0001.MP4",
                     "GX010042.MP4", "GH011234.MP4", "A001_C002_0101XY.mov", "A001C002.mov", "20260910_C1195.MP4",
                     "2026-09-10_DSC_0001.MOV", "clip.mxf", "clip.braw", "clip.r3d", "clip.mts", "clip.m2ts"):
            with self.subTest(name=name):
                self.assertEqual(self.source(name), "camera")
        self.assertEqual(self.source("CC0042.mp4"), "unknown")
        self.assertEqual(self.source("C00421.mp4"), "unknown")  # C + 5 digits is no camera name, nor a numeric id

    def test_generator_heuristic(self):
        self.assertEqual(self.source("take.mp4", **GENERATOR), "generated")
        self.assertEqual(self.source("take.mp4", **{**GENERATOR, "width": 720, "height": 1280}), "generated")
        for change in ({"fps": 23.976}, {"fps": 25.0}, {"duration": 3.9}, {"duration": 15.1}, {"has_audio": True},
                       {"nb_streams": 2}, {"width": 3840, "height": 2160}, {"tags": {"make": "Sony"}}):
            with self.subTest(change=change):
                self.assertNotEqual(self.source("take.mp4", **{**GENERATOR, **change}), "generated")
        self.assertEqual(self.source("take.mp4", **{**GENERATOR, "nb_streams": None}), "generated")

    def test_numeric_ids_and_lavf(self):
        self.assertEqual(self.source("3571264-hd_1920_1080_30fps.mp4"), "stock")
        self.assertEqual(self.source("12345.mp4"), "stock")
        self.assertEqual(self.source("1234.mp4"), "unknown")
        self.assertEqual(self.source("20240101_123456.mp4"), "unknown")  # a phone's date name
        self.assertEqual(self.source("clip.mp4", tags={"encoder": "Lavf61.7.100"}), "stock")
        self.assertEqual(self.source("clip.mp4", tags={"encoder": "HandBrake 1.8.0 2024061200"}), "stock")
        self.assertEqual(self.source("clip.mp4", tags={"encoder": "Lavf61.7.100", "make": "Sony"}), "camera")
        self.assertEqual(self.source("12345.mp4", tags={"model": "X-T5"}), "camera")

    def test_order_first_match_wins(self):
        self.assertEqual(self.source("kling pexels.mp4"), "generated")  # 1 before 2
        self.assertEqual(self.source("pexels C0001.mp4", tags={"make": "Sony"}), "stock")  # 2 before 3
        self.assertEqual(self.source("C0001.mp4", **GENERATOR), "camera")  # 4 before 5
        self.assertEqual(self.source("12345.mp4", **GENERATOR), "generated")  # 5 before 6
        self.assertEqual(self.source("holiday.mp4"), "unknown")


class AudioClass(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("files-classify-")
        self.wav = write_file(os.path.join(self.dir, "a.wav"), 1000)

    def tearDown(self):
        remove_tree(self.dir)

    def info(self, path=None, **kw):
        path = path or self.wav
        return MediaInfo(path=path, kind="audio", ext="wav", size=os.path.getsize(path), duration=30.0, **kw)

    def test_decision(self):
        self.assertEqual(MC.decide(0.9, 0.1), "speech")
        self.assertEqual(MC.decide(0.2, 0.2), "speech")
        self.assertEqual(MC.decide(0.1, 1.2), "music")
        self.assertEqual(MC.decide(0.19, 0.05), "other")
        self.assertEqual(MC.decide(0.05, 0.19), "other")
        self.assertEqual(MC.decide(0.0, 0.0), "other")

    def test_decode_arguments(self):
        args = MC.decode_args("C:\\x\\a.wav")
        self.assertEqual(args[args.index("-t") + 1], "20")
        self.assertLess(args.index("-t"), args.index("-i"))  # an input option: only 20 s are read
        self.assertEqual(args[args.index("-i") + 1], "C:\\x\\a.wav")
        self.assertEqual(args[args.index("-ac") + 1], "1")
        self.assertEqual(args[args.index("-ar") + 1], "16000")
        self.assertEqual(args[args.index("-f") + 1], "f32le")
        self.assertEqual(args[-1], "pipe:1")

    def test_fake_scorer_and_cache(self):
        import numpy as np

        calls, seen = [], []

        class Pipe:
            returncode = 0

            def communicate(self, timeout=None):
                return np.full(32000, 0.25, dtype="<f4").tobytes(), b""

        def pipe(args):
            calls.append(list(args))
            return Pipe()

        def scorer(waveform):
            seen.append(waveform)
            return 0.1, 0.9

        classifier = MC.MediaClassifier(pipe, scorer=scorer)
        enriched = classifier.enrich(self.info())
        self.assertEqual(enriched.audio_class, "music")
        self.assertEqual(seen[0].dtype, np.float32)
        self.assertEqual(seen[0].shape, (32000,))
        classifier.enrich(self.info())
        self.assertEqual(len(calls), 1)  # cached per (path, size, mtime)
        write_file(self.wav, 2000, "changed")
        classifier.enrich(self.info())
        self.assertEqual(len(calls), 2)

    def test_failures_leave_the_class_unknown(self):
        def missing(args):
            raise FileNotFoundError("ffmpeg is not available")

        class Failing:
            returncode = 1

            def communicate(self, timeout=None):
                return b"", b"Invalid data found when processing input\n"

        class Empty:
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        for pipe in (missing, lambda args: Failing(), lambda args: Empty()):
            with self.subTest(pipe=pipe):
                classifier = MC.MediaClassifier(pipe, scorer=lambda w: (1.0, 0.0))
                with self.assertLogs("module.files.media_class", "INFO"):
                    self.assertIsNone(classifier.enrich(self.info()).audio_class)

        def model_error(waveform):
            raise MC.ClassifierError("the audio model failed")

        classifier = MC.MediaClassifier(lambda args: Empty(), scorer=model_error)
        self.assertIsNone(classifier.audio_class(self.wav))
        missing_model = MC.AudioModel(model=os.path.join(self.dir, "none.onnx"))
        self.assertFalse(missing_model.available())
        with self.assertRaises(MC.ClassifierError):
            missing_model.scores(None)

    def test_probe_failure_and_other_kinds(self):
        classifier = MC.MediaClassifier(lambda args: self.fail("no decode for a failed probe"))
        self.assertIsNone(classifier.enrich(self.info(probe_error="boom")).audio_class)
        bad_video = video("kling.mp4", probe_error="boom")
        self.assertEqual(classifier.enrich(bad_video).source, "unknown")
        image = MediaInfo(path="C:\\x.png", kind="image", ext="png", size=1)
        self.assertIs(classifier.enrich(image), image)
        self.assertEqual(classifier.enrich(video("pexels.mp4")).source, "stock")

    def test_explicit_rules_win_over_the_classifier(self):
        layout = default_layout()
        classifier = MC.MediaClassifier(lambda args: self.fail("decoded"), scorer=lambda w: (1.0, 0.0))
        whoosh = self.info(write_file(os.path.join(self.dir, "whoosh_long.wav"), 100))
        self.assertEqual(classify(whoosh, layout, enrich=classifier.enrich).folder, "Audio/SFX")
        overrides = parse_overrides([{"name": "a.wav", "to": "Audio/Music"}])
        self.assertEqual(classify(self.info(), layout, overrides, enrich=classifier.enrich).folder, "Audio/Music")


@unittest.skipUnless(MC.MODEL_FILE.is_file() and ffmpeg_available(), "the bundled model and ffmpeg are needed")
class BundledModel(unittest.TestCase):
    """The real YAMNet ONNX model on the CPU execution provider."""

    @classmethod
    def setUpClass(cls):
        cls.dir = scratch("files-model-")
        cls.media = make_classifier_media(os.path.join(cls.dir, "src"))
        cls.classifier = MC.MediaClassifier(real_pipe, scorer=MC.AudioModel().scores)

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.dir)

    def audio(self, key):
        info = probe(self.media[key])
        self.assertIsNone(info.probe_error)
        return self.classifier.enrich(info).audio_class

    def test_model_file_loads_on_the_cpu(self):
        size = MC.MODEL_FILE.stat().st_size
        self.assertLess(size, 20 * 1024 * 1024)
        self.assertTrue((MC.MODELS_DIR / "LICENSE-yamnet.txt").is_file())
        classes = MC.load_class_map()
        self.assertEqual(len(classes), 521)
        model = MC.AudioModel()
        model._load()
        self.assertEqual(model._session.get_providers(), ["CPUExecutionProvider"])
        self.assertEqual(classes[model.speech_idx[0]][1], "Speech")
        self.assertEqual(len(model.speech_idx), 6)
        self.assertEqual(classes[model.music_idx[0]][1], "Music")
        self.assertEqual(classes[model.music_idx[-1]][1], "Scary music")

    def test_music_like_chord_is_music(self):
        self.assertEqual(self.audio("chord"), "music")

    def test_noise_tone_and_silence_are_other(self):
        for key in ("noise", "tone", "silence"):
            with self.subTest(key=key):
                self.assertEqual(self.audio(key), "other")

    def test_spoken_sentence_is_speech(self):
        path = os.path.join(self.dir, "src", "speech.wav")
        if not make_speech(path):
            self.skipTest("the Windows speech synthesizer is not available")
        self.assertEqual(self.classifier.enrich(probe(path)).audio_class, "speech")

    def test_one_file_takes_about_a_second_or_less(self):
        info = probe(self.media["chord"])
        classifier = MC.MediaClassifier(real_pipe, scorer=MC.AudioModel().scores)
        classifier.enrich(replace(info))  # session start-up is not part of the per-file time
        os.utime(self.media["chord"])  # a new mtime: not cached
        start = time.perf_counter()
        classifier.enrich(probe(self.media["chord"]))
        self.assertLess(time.perf_counter() - start, 3.0)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg and ffprobe are needed on PATH")
class RealVideoSources(unittest.TestCase):
    """Tags written by ffmpeg `-metadata` and stream facts read back through the real ffprobe."""

    @classmethod
    def setUpClass(cls):
        cls.dir = scratch("files-sources-")
        cls.media = make_classifier_media(os.path.join(cls.dir, "src"))

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.dir)

    def source(self, key):
        info = probe(self.media[key])
        self.assertIsNone(info.probe_error)
        return info, MC.video_source(info)

    def test_camera_make_and_model_tags(self):
        info, source = self.source("camera_tag")
        self.assertEqual(info.tags.get("make"), "Sony")
        self.assertEqual(source, "camera")

    def test_lavf_encoder_tag_is_stock(self):
        info, source = self.source("lavf")
        self.assertTrue(info.tags.get("encoder", "").startswith(("Lavf", "Lavc")), info.tags)
        self.assertEqual(source, "stock")

    def test_generator_heuristic_on_real_files(self):
        info, source = self.source("generator")
        self.assertEqual((info.fps, info.width, info.height, info.has_audio, info.nb_streams), (24.0, 1280, 720, False, 1))
        self.assertEqual(source, "generated")
        info, source = self.source("generator_23976")
        self.assertEqual(info.fps, 23.976)
        self.assertEqual(source, "unknown")

    def test_layout_files_them(self):
        layout = default_layout()
        classifier = MC.MediaClassifier(real_pipe, scorer=lambda w: (0.0, 0.0))
        folders = {key: classify(probe(self.media[key]), layout, enrich=classifier.enrich).folder
                   for key in ("camera_tag", "lavf", "generator", "generator_23976")}
        self.assertEqual(folders, {"camera_tag": "Footage", "lavf": "Stock", "generator": "Generated",
                                   "generator_23976": "Footage"})


if __name__ == "__main__":
    unittest.main()
