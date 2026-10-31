"""Audio module (notes/docs/AUDIO_EXPORT.md): plan rules, rendering, choices, the export job with FakeHost, the
silence check on ffmpeg-made media, and the export script's mute restore run under Node against a fake DOM."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from premiere_deck.host import protocol as P
from premiere_deck.modules.audio_export import plan
from premiere_deck.modules.audio_export.module import JSX_DIR, SOLO_DETAIL, AudioExportModule
from premiere_deck.modules.base import FakeHost
from premiere_deck.premiere.model import AudioTrack, PremiereError, SequenceInfo

SEQUENCE = SequenceInfo("seq1", "Spring: cut", 1920, 1080, 25.0, 60.0)
IN_OUT = SequenceInfo("seq1", "Spring: cut", 1920, 1080, 25.0, 60.0, in_point=10.0, out_point=40.0)
TRACKS = [AudioTrack(0, "Audio 1", False, 4), AudioTrack(1, "Dialogue", False, 2), AudioTrack(2, "Music", True, 1),
          AudioTrack(3, "Audio 4", False, 0)]
FFMPEG = shutil.which("ffmpeg")


def make_wav(path: Path, silent: bool, seconds: float = 0.5) -> None:
    source = "anullsrc=r=48000:cl=stereo" if silent else "sine=frequency=440:sample_rate=48000"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-t", str(seconds), "-i", source,
                    "-c:a", "pcm_s16le", str(path)], check=True)


class RealFfmpeg:
    """`Tools` with the real ffmpeg on PATH: `ffmpeg(args, on_line=...)` feeds stderr lines."""

    def has(self, tool: str) -> bool:
        return tool == "ffmpeg"

    def ffmpeg(self, args, *, timeout=None, on_line=None) -> int:
        done = subprocess.run([FFMPEG, *args], capture_output=True, text=True, timeout=timeout)
        for line in (done.stdout + done.stderr).splitlines():
            if on_line:
                on_line(line)
        return done.returncode


class AudioCase(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("PREMIERE_DECK_HOME")
        if base:
            os.makedirs(base, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="deck-audio-", dir=base or None))
        patcher = mock.patch.dict(os.environ, {"PREMIERE_DECK_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host, self.module = self.make()

    def make(self, *, sequence=SEQUENCE, tracks=TRACKS, project=True, manual_jobs=False, settings=None):
        host = FakeHost(manual_jobs=manual_jobs, settings=settings, module_id="audio")
        if project:
            host.make_project(root=self.home / "projects" / "Spring", name="Spring")
        host.premiere.active_sequence_value = sequence
        host.premiere.audio_tracks_value = {"seq1": list(tracks)}
        host.premiere.eval_results["audio.preset"] = {"path": "C:\\Premiere\\MediaIO\\systempresets\\x\\y.epr"}
        module = AudioExportModule(host.context())
        module.start()
        return host, module

    def writer(self, silent_tracks=()):
        """The export script stand-in: writes a WAV to the temp path (silent when every audible track is in
        `silent_tracks`) and records the arguments."""
        self.exports: list[dict] = []

        def run(args: dict) -> dict:
            self.exports.append(args)
            silent = all(index in silent_tracks for index in args["audible"])
            if FFMPEG:
                make_wav(Path(args["temp_path"]), silent)
            else:
                Path(args["temp_path"]).write_bytes(b"RIFF")
            return {"written": True, "bytes": 1, "restored": True, "returned": "0"}

        return run

    def press(self, action_id, value=None, module=None, host=None):
        module, host = module or self.module, host or self.host
        before = len(host.ui.results)
        result = module.on_action(f"audio.{action_id}", value)
        self.assertTrue(result is None or not result.toast)
        if len(host.ui.results) == before:
            return None
        button, outcome, text, self.detail = host.ui.results[-1]
        return outcome, text

    def folder(self):
        return self.home / "projects" / "Spring" / "Exports" / "Audio"


class PlanRules(AudioCase):
    def test_formats_are_the_system_presets(self):
        self.assertEqual([fmt.id for fmt in plan.FORMATS], ["wav", "mp3", "aiff"])
        self.assertEqual(plan.FORMAT_BY_ID["wav"].file, "Waveform Audio 48kHz 16-bit.epr")
        self.assertEqual(plan.FORMAT_BY_ID["aiff"].ext, "aif")

    def test_track_labels_and_default_choice(self):
        self.assertEqual([plan.track_label(t) for t in TRACKS], ["A1", "A2 Dialogue", "A3 Music", "A4"])
        self.assertEqual(plan.default_choice(TRACKS), {0, 1}, "tracks with clips that are not muted")

    def test_range(self):
        self.assertEqual(plan.work_area(IN_OUT, plan.RANGE_INOUT), plan.WORK_AREA_IN_TO_OUT)
        self.assertEqual(plan.work_area(SEQUENCE, plan.RANGE_INOUT), plan.WORK_AREA_ENTIRE, "no marks: whole")
        self.assertEqual(plan.work_area(IN_OUT, plan.RANGE_ENTIRE), plan.WORK_AREA_ENTIRE)
        bad = SequenceInfo("s", "S", 1, 1, 25.0, 60.0, in_point=40.0, out_point=10.0)
        self.assertEqual(plan.work_area(bad, plan.RANGE_INOUT), plan.WORK_AREA_ENTIRE)
        self.assertEqual(plan.range_seconds(IN_OUT, plan.WORK_AREA_IN_TO_OUT), 30.0)
        self.assertEqual(plan.range_seconds(IN_OUT, plan.WORK_AREA_ENTIRE), 60.0)
        self.assertEqual(plan.timeout_ms(60.0), 210000)
        self.assertEqual(plan.timeout_ms(10 ** 6), 4 * 3600 * 1000)

    def test_passes_and_names(self):
        wav = plan.FORMAT_BY_ID["wav"]
        separate = plan.passes(SEQUENCE, TRACKS, [1, 0, 9], plan.MODE_SEPARATE, wav)
        self.assertEqual([(p.tracks, p.name, p.label) for p in separate],
                         [((0,), "Spring_ cut - A1.wav", "A1"), ((1,), "Spring_ cut - A2 Dialogue.wav", "A2")])
        mix = plan.passes(SEQUENCE, TRACKS, [2, 0], plan.MODE_MIX, plan.FORMAT_BY_ID["mp3"])
        self.assertEqual([(p.tracks, p.name, p.label) for p in mix], [((0, 2), "Spring_ cut - Mix A1-A3.mp3", "Mix")])
        self.assertEqual(plan.passes(SEQUENCE, TRACKS, [], plan.MODE_SEPARATE, wav), [])
        odd = [AudioTrack(0, 'VO: "take" 2.', False, 1)]
        self.assertEqual(plan.passes(SEQUENCE, odd, [0], plan.MODE_SEPARATE, wav)[0].name,
                         "Spring_ cut - A1 VO_ _take_ 2.wav")

    def test_audio_folder_follows_the_layout_and_stays_inside(self):
        root = self.home / "root"
        (root / ".premiere-deck").mkdir(parents=True)
        self.assertEqual(plan.audio_folder(root), root / "Exports" / "Audio")
        for value, expected in (("Deliveries/Out", root / "Deliveries" / "Out" / "Audio"),
                                ("..", root / "Exports" / "Audio"), ("C:/x", root / "Exports" / "Audio"),
                                ("\\\\server\\x", root / "Exports" / "Audio"), ("", root / "Exports" / "Audio"),
                                (5, root / "Exports" / "Audio")):
            with self.subTest(value=value):
                (root / ".premiere-deck" / "layout.json").write_text(json.dumps({"exports_dir": value}), "utf-8")
                self.assertEqual(plan.audio_folder(root), expected)
        (root / ".premiere-deck" / "layout.json").write_text("{broken", "utf-8")
        self.assertEqual(plan.audio_folder(root), root / "Exports" / "Audio")

    def test_free_path_never_overwrites(self):
        folder = self.home / "free"
        folder.mkdir()
        self.assertEqual(plan.free_path(folder, "a.wav").name, "a.wav")
        (folder / "a.wav").write_bytes(b"x")
        (folder / "a (2).wav").write_bytes(b"x")
        self.assertEqual(plan.free_path(folder, "a.wav").name, "a (3).wav")
        temp = plan.temp_path(folder, "17.3-x", plan.FORMAT_BY_ID["wav"])
        self.assertEqual(temp.name, "deckaudio173x.wav", "a dot-free temporary stem")

    def test_error_sentences(self):
        self.assertEqual(plan.error_sentence("Error: deck: The WAV preset of Premiere was not found"),
                         "The WAV preset of Premiere was not found.")
        self.assertEqual(plan.error_sentence("timeout"), "Premiere is waiting for a dialog.")
        self.assertEqual(plan.error_sentence("ReferenceError: x"), "Premiere reported a script error.")

    def test_max_volume_parsing(self):
        self.assertEqual(plan.max_volume(["x", "[Parsed_volumedetect_0 @ 0] max_volume: -91.0 dB"]), -91.0)
        self.assertEqual(plan.max_volume(["max_volume: -inf dB"]), float("-inf"))
        self.assertIsNone(plan.max_volume(["nothing"]))
        self.assertTrue(plan.is_silent(-60.0) and plan.is_silent(float("-inf")))
        self.assertFalse(plan.is_silent(-3.0) or plan.is_silent(None))

    @unittest.skipUnless(FFMPEG, "ffmpeg is not on PATH")
    def test_silence_check_on_real_media(self):
        tone, quiet = self.home / "tone.wav", self.home / "quiet.wav"
        make_wav(tone, silent=False)
        make_wav(quiet, silent=True)
        tools = RealFfmpeg()
        for path, silent in ((tone, False), (quiet, True)):
            lines: list[str] = []
            self.assertEqual(tools.ffmpeg(plan.silence_args(str(path)), on_line=lines.append), 0)
            self.assertEqual(plan.is_silent(plan.max_volume(lines)), silent, path.name)


class Rendering(AudioCase):
    def tree(self, module=None):
        section = (module or self.module).section()
        P.validate_tree({"type": "ui", "revision": 1, "title": "", "status": None, "footer": None,
                         "sections": [section]})
        return section

    def test_manifest(self):
        self.assertEqual((AudioExportModule.id, AudioExportModule.title, AudioExportModule.icon), ("audio", "Audio", "audio"))
        self.assertEqual(AudioExportModule.order, 7)
        from premiere_deck.host.registry import MODULES
        self.assertIn("audio_export", MODULES)

    def test_tracks_lane_and_export_lane(self):
        section = self.tree()
        self.assertEqual(section["icon"], "audio")
        tracks, export = section["items"]
        self.assertEqual((tracks["title"], tracks["short"], export["title"], export["short"]),
                         ("Tracks", "TRKS", "Export", "OUT"))
        switches = tracks["items"]
        self.assertEqual([n["kind"] for n in switches], ["switch"] * 4)
        self.assertEqual([n["short"] for n in switches], ["A1", "A2", "A3", "A4"])
        self.assertEqual([n["label"] for n in switches], ["A1", "A2 · Dialogue", "A3 · Music", "A4"])
        self.assertEqual([n["value"] for n in switches], [True, True, False, False], "default: clips and not muted")
        self.assertEqual(switches[2]["hint"], "1 clip · muted in Premiere")
        self.assertTrue(switches[3]["disabled"] and switches[3]["hint"] == "Empty")
        tiles = export["items"]
        self.assertEqual([n["id"] for n in tiles], ["audio.mode", "audio.format", "audio.range", "audio.export",
                                                    "audio.open_folder"])
        self.assertEqual([n["menu"]["badge"] for n in tiles[:3]], ["SEP", "WAV", "I/O"])
        self.assertEqual([n["short"] for n in tiles], ["SEP", "WAV", "I/O", "EXP", "DIR"], "codes show the setup")
        self.assertEqual([o["label"] for o in tiles[2]["menu"]["options"]], ["In to Out", "Entire sequence"])
        self.assertEqual([o["label"] for o in tiles[0]["menu"]["options"]], ["Separate files", "One file"])
        self.assertEqual([o["label"] for o in tiles[1]["menu"]["options"]],
                         ["WAV 48 kHz 16-bit", "MP3 256 kbps", "AIFF 48 kHz"])
        self.assertEqual(tiles[3]["hint"], "Renders the chosen tracks into Exports\\Audio in the project folder.")

    def test_export_hint_names_the_layout_folder(self):
        deck = self.home / "projects" / "Spring" / ".premiere-deck"
        deck.mkdir(parents=True)
        (deck / "layout.json").write_text(json.dumps({"exports_dir": "Out/Renders"}), "utf-8")
        tiles = self.tree()["items"][1]["items"]
        self.assertEqual(tiles[3]["hint"], "Renders the chosen tracks into Out\\Renders\\Audio in the project folder.")

    def test_states_without_a_sequence_or_tracks(self):
        host, module = self.make(sequence=None)
        self.assertIn("Open a sequence", self.tree(module)["items"][0]["text"])
        host, module = self.make(tracks=[])
        items = self.tree(module)["items"]
        self.assertIn("no audio tracks", items[0]["text"])
        host.premiere.connected = False
        host, module = self.make()
        module.sequence, module.loaded = None, False
        self.assertIn("Reading", self.tree(module)["items"][0]["text"])

    def test_choices_are_stored_and_fall_back(self):
        self.assertIsNone(self.press("format.choice", "mp3"))
        self.assertEqual(self.host.settings.get("choices"), {"format": "mp3"})
        self.press("format.choice", "flac")
        self.assertEqual(self.module.choice("format"), "mp3", "an unknown value is ignored")
        self.press("mode")
        self.assertEqual(self.module.choice("mode"), plan.MODE_MIX, "a press steps to the next option")
        self.press("mode")
        self.assertEqual(self.module.choice("mode"), plan.MODE_SEPARATE)
        for stored in ("bad", {"format": "ogg", "mode": 3, "range": "work"}, None):
            with self.subTest(stored=stored):
                host, module = self.make(settings={"choices": stored})
                self.assertEqual((module.choice("mode"), module.choice("format"), module.choice("range")),
                                 ("separate", "wav", "inout"))

    def test_track_choice_is_kept_per_sequence_for_the_session(self):
        self.press("track.0", False)
        self.press("track.2", True)
        self.press("track.3", True)   # an index of an empty track is accepted, it renders disabled
        self.press("track.9", True)   # unknown index: ignored
        self.press("track.x", True)
        self.assertEqual(self.module.chosen_tracks(), {1, 2, 3})
        self.host.premiere.active_sequence_value = SequenceInfo("seq2", "Other", 1, 1, 25.0, 10.0)
        self.host.premiere.audio_tracks_value["seq2"] = [AudioTrack(0, "A", False, 1)]
        self.module.on_event("sequence.activated", {"sequence_id": "seq2"})
        self.assertEqual(self.module.chosen_tracks(), {0})
        self.host.premiere.active_sequence_value = SEQUENCE
        self.module.on_event("sequence.activated", {"sequence_id": "seq1"})
        self.assertEqual(self.module.chosen_tracks(), {1, 2, 3}, "remembered for the session")
        self.module.project_closed()
        self.assertEqual(self.module.chosen, {})

    def test_refresh_keeps_state_when_premiere_cannot_answer(self):
        self.host.premiere.connected = False
        self.module.on_event("panel.opened", {})
        self.assertEqual(len(self.module.tracks), 4)

    def test_host_page(self):
        page = self.module.render_host()
        self.assertEqual(page["blocks"][0]["type"], "text")


class ExportJob(AudioCase):
    def test_separate_files(self):
        self.host.premiere.eval_results["audio.export_audio"] = self.writer()
        self.assertEqual(self.press("export"), ("ok", "Exported 2 files · WAV"))
        folder = self.folder()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Spring_ cut - A1.wav", "Spring_ cut - A2 Dialogue.wav"])
        self.assertEqual([e["audible"] for e in self.exports], [[0], [1]])
        first = self.exports[0]
        self.assertEqual((first["sequence_id"], first["work_area"], first["preset"]),
                         ("seq1", plan.WORK_AREA_ENTIRE, "C:\\Premiere\\MediaIO\\systempresets\\x\\y.epr"))
        self.assertEqual(Path(first["temp_path"]).parent, folder)
        self.assertTrue(Path(first["temp_path"]).name.startswith("deckaudio"))
        preset = self.host.premiere.calls_named("eval")[0]
        self.assertEqual(preset[0][0], "audio.preset")
        self.assertEqual(preset[1]["args"]["file"], "Waveform Audio 48kHz 16-bit.epr")
        timeouts = {kw["timeout_ms"] for args, kw in self.host.premiere.calls_named("eval") if args[0] == "audio.export_audio"}
        self.assertEqual(timeouts, {plan.timeout_ms(60.0)})
        self.assertEqual(self.host.host.revealed, [str(folder / "Spring_ cut - A2 Dialogue.wav")], "the last file")

    def test_mix_in_to_out_and_never_overwrite(self):
        host, module = self.make(sequence=IN_OUT)
        host.premiere.eval_results["audio.export_audio"] = self.writer()
        module.on_action("audio.mode.choice", "mix")
        module.on_action("audio.format.choice", "aiff")
        folder = self.folder()
        folder.mkdir(parents=True)
        (folder / "Spring_ cut - Mix A1-A2.aif").write_bytes(b"theirs")
        self.assertEqual(self.press("export", module=module, host=host), ("ok", "Exported mix of 2 tracks · AIFF"))
        self.assertEqual(self.exports[0]["audible"], [0, 1])
        self.assertEqual(self.exports[0]["work_area"], plan.WORK_AREA_IN_TO_OUT)
        self.assertEqual((folder / "Spring_ cut - Mix A1-A2.aif").read_bytes(), b"theirs", "the existing file stays")
        self.assertTrue((folder / "Spring_ cut - Mix A1-A2 (2).aif").is_file())
        module.on_action("audio.range.choice", "entire")
        self.press("export", module=module, host=host)
        self.assertEqual(self.exports[-1]["work_area"], plan.WORK_AREA_ENTIRE)
        self.assertTrue((folder / "Spring_ cut - Mix A1-A2 (3).aif").is_file())

    def test_custom_exports_folder_of_the_layout(self):
        deck = self.home / "projects" / "Spring" / ".premiere-deck"
        deck.mkdir(parents=True)
        (deck / "layout.json").write_text(json.dumps({"exports_dir": "Renders"}), "utf-8")
        self.host.premiere.eval_results["audio.export_audio"] = self.writer()
        self.press("export")
        self.assertEqual(Path(self.exports[0]["temp_path"]).parent, self.home / "projects" / "Spring" / "Renders" / "Audio")

    @unittest.skipUnless(FFMPEG, "ffmpeg is not on PATH")
    def test_silent_stem_warns_about_solo(self):
        self.host.tools = RealFfmpeg()
        host, module = self.host, AudioExportModule(self.host.context())
        module.start()
        host.premiere.eval_results["audio.export_audio"] = self.writer(silent_tracks={1})
        self.assertEqual(self.press("export", module=module), ("warn", "Exported 2 files · WAV · A2 is silent"))
        self.assertEqual(self.detail, SOLO_DETAIL)

    def test_preconditions(self):
        host, module = self.make(project=False)
        host.premiere.project_value = None
        self.assertEqual(self.press("export", module=module, host=host), ("warn", "No project open"))
        host, module = self.make(sequence=None)
        self.assertEqual(self.press("export", module=module, host=host), ("warn", "No active sequence"))
        host, module = self.make(tracks=[])
        self.assertEqual(self.press("export", module=module, host=host), ("warn", "No audio tracks"))
        host, module = self.make()
        module.on_action("audio.track.0", False)
        module.on_action("audio.track.1", False)
        self.assertEqual(self.press("export", module=module, host=host), ("warn", "Choose an audio track"))
        host, module = self.make()
        host.premiere.connected = False
        self.assertEqual(self.press("export", module=module, host=host), ("warn", "Premiere Pro is not connected"))
        self.assertEqual(host.premiere.calls_named("eval"), [])

    def test_missing_preset(self):
        self.host.premiere.eval_errors["audio.preset"] = PremiereError(
            "Error: deck: The WAV preset of Premiere was not found.", 20, "")
        with self.assertLogs("module.audio.test", level="WARNING"):
            self.assertEqual(self.press("export"), ("error", "Audio export failed"))
        self.assertEqual(self.detail, "The WAV preset of Premiere was not found.")
        self.assertFalse(self.folder().exists())

    def test_failure_on_the_second_file_names_what_was_written_and_cleans_its_temp(self):
        writer = self.writer()

        def run(args):
            if len(self.exports) == 1:
                Path(args["temp_path"]).write_bytes(b"partial")
                self.exports.append(args)
                raise PremiereError("timeout")
            return writer(args)

        self.host.premiere.eval_results["audio.export_audio"] = run
        with self.assertLogs("module.audio.test", level="WARNING"):
            self.assertEqual(self.press("export"), ("error", "Audio export failed"))
        self.assertEqual(self.detail, "Premiere is waiting for a dialog. 1 file was written before.")
        self.assertEqual([p.name for p in self.folder().iterdir()], ["Spring_ cut - A1.wav"], "the temp file is gone")

    def test_no_file_written(self):
        self.host.premiere.eval_results["audio.export_audio"] = {"written": False}
        with self.assertLogs("module.audio.test", level="WARNING"):
            self.assertEqual(self.press("export"), ("error", "Audio export failed"))
        self.assertEqual(self.detail, "Premiere wrote no audio file.")

    def test_stop_between_files(self):
        host, module = self.make(manual_jobs=True)
        host.premiere.eval_results["audio.export_audio"] = self.writer()
        self.assertIsNone(self.press("export", module=module, host=host))
        section = module.section()
        progress = [n for n in section["items"][1]["items"] if n["kind"] == "progress"][0]
        self.assertEqual((progress["cancel"], progress["detail"]), ("audio.export.stop", "A1 · 1 of 2"))
        self.assertTrue(all(n.get("disabled") for n in section["items"][0]["items"]), "tracks are locked while running")
        self.assertIsNone(module.on_action("audio.export").toast, "a second press is ignored")
        original = host.premiere.eval_results["audio.export_audio"]

        def first_then_stop(args):
            out = original(args)
            module.on_action("audio.export.stop")
            return out

        host.premiere.eval_results["audio.export_audio"] = first_then_stop
        host.jobs.step()
        _, outcome, text, _ = host.ui.results[-1]
        self.assertEqual((outcome, text), ("neutral", "Audio export stopped · 1 file written"))
        self.assertEqual(len(self.exports), 1)
        self.assertIsNone(module.job)

    def test_mute_restore_flag_is_logged(self):
        writer = self.writer()
        self.host.premiere.eval_results["audio.export_audio"] = lambda args: dict(writer(args), restored=False)
        with self.assertLogs("module.audio.test", level="WARNING"):
            self.assertEqual(self.press("export"), ("warn", "Exported 2 files · WAV"))
        self.assertEqual(self.detail, "The track mutes of Spring: cut may not be restored. Check them in the timeline.")

    def test_restored_mutes_keep_the_ok_outcome(self):
        self.host.premiere.eval_results["audio.export_audio"] = self.writer()
        self.assertEqual(self.press("export")[0], "ok")
        self.assertIsNone(self.detail)

    def test_stale_own_temps_are_removed_before_an_export(self):
        folder = self.folder()
        folder.mkdir(parents=True)
        old = time.time() - plan.STALE_TEMP_SECONDS - 60
        stale, fresh = folder / "deckaudio17000001.wav", folder / "deckaudio17000002.wav"
        theirs, near = folder / "my take.wav", folder / "deckaudio-notes.wav"
        for path in (stale, fresh, theirs, near):
            path.write_bytes(b"RIFF")
        for path in (stale, theirs, near):
            os.utime(path, (old, old))
        self.assertEqual(plan.stale_temps(folder, time.time()), [stale])
        self.host.premiere.eval_results["audio.export_audio"] = self.writer()
        self.press("export")
        self.assertFalse(stale.exists(), "the module's own leftover is removed")
        for path in (fresh, theirs, near):
            self.assertTrue(path.exists(), f"{path.name} stays")

    def test_open_folder(self):
        self.assertEqual(self.press("open_folder"), ("neutral", "No audio exported yet"))
        self.folder().mkdir(parents=True)
        self.assertEqual(self.press("open_folder"), ("neutral", "Opened Audio"))
        self.assertEqual(self.host.host.opened, [str(self.folder())])
        host, module = self.make(project=False)
        host.premiere.project_value = None
        self.assertEqual(self.press("open_folder", module=module, host=host), ("warn", "No project open"))


class Scripts(unittest.TestCase):
    FORBIDDEN = (r"\blet\s", r"\bconst\s", r"=>", r"`", r"\.forEach\(", r"\.indexOf\(", r"\bclass\s")

    def scripts(self):
        return sorted(JSX_DIR.glob("*.jsx"))

    def test_es3_and_no_dialogs(self):
        import re
        self.assertEqual([p.name for p in self.scripts()], ["export_audio.jsx", "preset.jsx"])
        for path in self.scripts():
            code = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
            for pattern in self.FORBIDDEN + (r"\b(alert|confirm|prompt)\(", r"\$\.writeln"):
                with self.subTest(path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_scripts_parse(self):
        home = Path(tempfile.mkdtemp(prefix="deck-audio-jsx-", dir=os.environ.get("PREMIERE_DECK_HOME") or None))
        for path in self.scripts():
            with self.subTest(path.name):
                wrapped = home / f"{path.stem}.check.js"
                wrapped.write_text("(function(){ var __args = {};\n" + path.read_text(encoding="utf-8") + "\n});\n",
                                   encoding="utf-8")
                done = subprocess.run(["node", "--check", str(wrapped)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_mutes_are_restored_when_the_export_throws(self):
        """Runs export_audio.jsx under Node against a fake sequence: the export sees only the audible tracks
        unmuted, and every mute state is back afterwards, also when exportAsMediaDirect throws."""
        home = Path(tempfile.mkdtemp(prefix="deck-audio-run-", dir=os.environ.get("PREMIERE_DECK_HOME") or None))
        harness = home / "harness.js"
        body = (JSX_DIR / "export_audio.jsx").read_text(encoding="utf-8")
        harness.write_text(r"""
var throwIt = process.argv[2] === "throw";
function Track(m) { this.m = m; }
Track.prototype.isMuted = function () { return this.m; };
Track.prototype.setMute = function (v) { this.m = !!v; };
var tracks = [new Track(false), new Track(true), new Track(false)];
tracks.numTracks = 3;
var seen = null;
var sequence = {sequenceID: "s1", audioTracks: tracks, exportAsMediaDirect: function (path, preset, type) {
    seen = {muted: [tracks[0].m, tracks[1].m, tracks[2].m], path: path, preset: preset, type: type};
    if (throwIt) { throw new Error("export failed"); }
    files[path] = 10;
    return true;
}};
var seqs = [sequence]; seqs.numSequences = 1;
var files = {};
function File(p) { this.fsName = p; this.exists = files.hasOwnProperty(p); this.length = files[p] || 0; }
File.prototype.remove = function () { delete files[this.fsName]; return true; };
var app = {project: {sequences: seqs}, encoder: {ENCODE_ENTIRE: 0, ENCODE_IN_TO_OUT: 1}};
var __args = {sequence_id: "s1", audible: [1], preset: "p.epr", temp_path: "T.wav", work_area: 1};
var out = null, error = null;
try { out = (function () {
""" + body + r"""
})(); } catch (e) { error = String(e.message); }
console.log(JSON.stringify({out: out, error: error, seen: seen, after: [tracks[0].m, tracks[1].m, tracks[2].m]}));
""", encoding="utf-8")
        for mode in ("ok", "throw"):
            with self.subTest(mode):
                done = subprocess.run(["node", str(harness), mode], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                data = json.loads(done.stdout)
                self.assertEqual(data["seen"]["muted"], [True, False, True], "only the audible track plays")
                self.assertEqual((data["seen"]["preset"], data["seen"]["type"]), ("p.epr", 1))
                self.assertEqual(data["after"], [False, True, False], "every mute state is restored")
                if mode == "ok":
                    self.assertEqual(data["out"], {"written": True, "bytes": 10, "restored": True, "returned": "true"})
                else:
                    self.assertEqual(data["error"], "export failed")


if __name__ == "__main__":
    unittest.main()
