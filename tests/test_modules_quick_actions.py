import os
import re
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from premiere_deck.host import protocol as P
from premiere_deck.modules.base import FakeHost
from premiere_deck.modules.quick_actions import presets as presets_mod
from premiere_deck.modules.quick_actions.action import JSX_DIR, error_sentence, safe_file_stem, script_text, timecode
from premiere_deck.modules.quick_actions.actions.label_by_bin import plan_labels
from premiere_deck.modules.quick_actions.actions.save_version import next_version_path, version_folder
from premiere_deck.modules.quick_actions.actions.social_copy import target_size
from premiere_deck.modules.quick_actions.module import QuickActionsModule, _ModuleThreadPremiere
from premiere_deck.premiere.model import (
    COLOR_LABELS, PremiereError, ProjectItem, Selection, SequenceInfo, TrackItem,
)

SEQUENCE = SequenceInfo("seq1", "Spring", 1920, 1080, 25.0, 60.0)
V1 = TrackItem("seq1", "video", 0, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True)
A2 = TrackItem("seq1", "audio", 1, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True)

NEEDS_SEQUENCE = ("adjustment_layer", "remove_gaps", "razor_all", "delete_empty_tracks", "select_same_source",
                  "select_offline", "scale_to_frame", "solo_track", "lock_others", "reveal_clip", "social_copy",
                  "export_frame", "export_sequence")
NEEDS_SELECTION = ("select_same_source", "scale_to_frame", "solo_track", "lock_others", "reveal_clip")
SCRIPTED = ("adjustment_layer", "remove_gaps", "razor_all", "delete_empty_tracks", "select_same_source",
            "select_offline", "scale_to_frame", "solo_track", "lock_others", "consolidate_duplicates", "social_copy",
            "export_frame", "export_sequence")
ALL_IDS = ("adjustment_layer", "remove_gaps", "razor_all", "delete_empty_tracks", "select_same_source",
           "select_offline", "scale_to_frame", "label_by_bin", "solo_track", "lock_others", "save_version",
           "consolidate_duplicates", "open_project_folder", "reveal_clip", "social_copy", "export_frame",
           "export_sequence")


class QuickActionsCase(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("PREMIERE_DECK_HOME")
        if base:
            os.makedirs(base, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="deck-actions-", dir=base or None))
        patcher = mock.patch.dict(os.environ, {"PREMIERE_DECK_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.system_root = self.home / "adobe" / "systempresets"
        h264 = self.system_root / "4E49434B_48323634"
        h264.mkdir(parents=True)
        for name in ("YouTube 1080p HD.epr", "01 - Match Source - High bitrate.epr"):
            (h264 / name).write_text("<PremiereData/>", encoding="utf-8")
        self.host, self.module = self.make()

    def make(self, *, manual_jobs=False, settings=None, sequence=SEQUENCE, selection=(V1, A2), project=True):
        host = FakeHost(manual_jobs=manual_jobs, settings=settings, module_id="actions")
        if project:
            host.make_project(name="Spring")
        host.premiere.active_sequence_value = sequence
        host.premiere.selection_value = Selection("seq1", tuple(selection))
        module = QuickActionsModule(host.context())
        module.preset_roots = [self.system_root]
        module.start()
        return host, module

    def click(self, action_id, module=None, host=None, value=None):
        module = module or self.module
        host = host or self.host
        before = len(host.ui.toasts)
        result = module.on_action(f"actions.{action_id}", value)
        if result is not None and result.toast:
            return result.toast
        return host.ui.toasts[-1] if len(host.ui.toasts) > before else None

    def evals(self, host=None, name=None):
        calls = (host or self.host).premiere.calls_named("eval")
        return [(args[0], kw["args"], kw["timeout_ms"]) for args, kw in calls if name is None or args[0] == name]


class Preconditions(QuickActionsCase):
    def test_no_active_sequence(self):
        for action_id in NEEDS_SEQUENCE:
            with self.subTest(action_id):
                host, module = self.make(sequence=None)
                self.assertEqual(self.click(action_id, module, host), ("warn", "No active sequence"))
                self.assertEqual(self.evals(host), [])

    def test_nothing_selected(self):
        for action_id in NEEDS_SELECTION:
            with self.subTest(action_id):
                host, module = self.make(selection=())
                self.assertEqual(self.click(action_id, module, host), ("warn", "Nothing selected"))
                self.assertEqual(self.evals(host), [])

    def test_no_project(self):
        for action_id in ("save_version", "open_project_folder", "label_by_bin", "consolidate_duplicates",
                          "export_frame", "export_sequence"):
            with self.subTest(action_id):
                host, module = self.make(project=False)
                self.assertEqual(self.click(action_id, module, host), ("warn", "No project open"))

    def test_project_actions_work_without_sequence_or_selection(self):
        host, module = self.make(sequence=None, selection=())
        self.assertEqual(self.click("open_project_folder", module, host)[0], "neutral")
        self.assertEqual(self.click("consolidate_duplicates", module, host), ("ok", "Duplicates consolidated"))
        self.assertEqual(self.click("save_version", module, host)[0], "ok")

    def test_not_connected(self):
        for action_id in ALL_IDS:
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.connected = False
                self.assertEqual(self.click(action_id, module, host), ("warn", "Premiere Pro is not connected"))


class ScriptErrors(QuickActionsCase):
    def test_user_sentence_is_shown(self):
        for action_id in SCRIPTED:
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.eval_errors[f"actions.{action_id}"] = PremiereError(
                    "Error: deck: No free video track above the selection.", 12, "var seq")
                label = module.actions[action_id].label
                with self.assertLogs("module.actions.test", level="WARNING"):
                    toast = self.click(action_id, module, host)
                self.assertEqual(toast, ("error", f"{label} failed: No free video track above the selection."))

    def test_raw_errors_are_not_shown(self):
        for action_id in SCRIPTED:
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.eval_errors[f"actions.{action_id}"] = PremiereError("undefined is not an object", 3)
                label = module.actions[action_id].label
                with self.assertLogs("module.actions.test", level="WARNING"):
                    toast = self.click(action_id, module, host)
                self.assertEqual(toast, ("error", f"{label} failed: Premiere reported a script error."))

    def test_facade_errors_of_host_side_actions(self):
        self.host.premiere.save_project_as = mock.Mock(side_effect=PremiereError("deck: The disk is full."))
        self.host.premiere.snapshot = mock.Mock(side_effect=PremiereError("timeout"))
        with self.assertLogs("module.actions.test", level="WARNING"):
            self.assertEqual(self.click("save_version"), ("error", "Save version failed: The disk is full."))
            self.assertEqual(self.click("label_by_bin"),
                             ("error", "Colour labels by bin failed: Premiere is waiting for a dialog."))

    def test_python_bug_becomes_sentence(self):
        self.host.premiere.eval_results["actions.remove_gaps"] = lambda args: 1 / 0
        with self.assertLogs("module.actions.test", level="ERROR"):
            toast = self.click("remove_gaps")
        self.assertEqual(toast, ("error", "Remove all gaps failed: an internal error occurred. Details are in the log."))

    def test_error_sentence(self):
        self.assertEqual(error_sentence("deck: Select a video clip"), "Select a video clip.")
        self.assertEqual(error_sentence("timeout"), "Premiere is waiting for a dialog.")
        self.assertEqual(error_sentence(""), "Premiere reported a script error.")


class TimelineActions(QuickActionsCase):
    def test_adjustment_layer_over_selection(self):
        self.host.premiere.eval_results["actions.adjustment_layer"] = {
            "track_index": 2, "start": 12.0, "end": 18.0, "created": False, "name": "Adjustment 1920x1080"}
        self.assertEqual(self.click("adjustment_layer"), ("ok", "Adjustment layer added · V3 · 00:00:12 to 00:00:18"))
        (_, args, _), = self.evals()
        self.assertEqual(args, {"start": 12.0, "end": 18.0, "min_track": 1, "bin": "Deck",
                                "name": "Adjustment 1920x1080"})

    def test_adjustment_layer_at_playhead_when_nothing_selected(self):
        host, module = self.make(selection=())
        host.premiere.playhead_value = 30.0
        host.premiere.eval_results["actions.adjustment_layer"] = lambda a: {
            "track_index": 0, "start": a["start"], "end": a["end"], "created": True}
        toast = self.click("adjustment_layer", module, host)
        self.assertEqual(toast, ("ok", "Adjustment layer added · V1 · 00:00:30 to 00:00:35 · new item in the Deck bin"))
        self.assertEqual(self.evals(host)[0][1]["min_track"], 0)

    def test_adjustment_layer_audio_only_selection_starts_at_v1(self):
        host, module = self.make(selection=(A2,))
        host.premiere.eval_results["actions.adjustment_layer"] = {"track_index": 0, "start": 12, "end": 18}
        self.click("adjustment_layer", module, host)
        self.assertEqual(self.evals(host)[0][1]["min_track"], 0)

    def test_remove_gaps(self):
        results = self.host.premiere.eval_results
        results["actions.remove_gaps"] = {"closed": 4, "blocked": 0, "removed": 7.2}
        self.assertEqual(self.click("remove_gaps"), ("ok", "Removed 4 gaps · 00:00:07"))
        results["actions.remove_gaps"] = {"closed": 1, "blocked": 2, "removed": 1}
        self.assertEqual(self.click("remove_gaps"), ("ok", "Removed 1 gap · 00:00:01 · 2 kept by a locked track"))
        results["actions.remove_gaps"] = {"closed": 0, "blocked": 0, "removed": 0}
        self.assertEqual(self.click("remove_gaps"), ("neutral", "No gaps found"))
        results["actions.remove_gaps"] = {"closed": 0, "blocked": 1, "removed": 0}
        self.assertEqual(self.click("remove_gaps")[0], "warn")

    def test_razor_all(self):
        results = self.host.premiere.eval_results
        results["actions.razor_all"] = {"cuts": 3, "locked": 0, "failed": 0, "seconds": 62.4}
        self.assertEqual(self.click("razor_all"), ("ok", "Cut 3 tracks at 00:01:02"))
        results["actions.razor_all"] = {"cuts": 2, "locked": 1, "failed": 0, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("ok", "Cut 2 tracks at 00:00:01 · 1 locked track skipped"))
        results["actions.razor_all"] = {"cuts": 0, "locked": 0, "failed": 0, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("neutral", "No clip under the playhead"))
        results["actions.razor_all"] = {"cuts": 0, "locked": 0, "failed": 2, "seconds": 1}
        self.assertEqual(self.click("razor_all"),
                         ("error", "Razor all tracks failed: Premiere did not cut at the playhead."))

    def test_delete_empty_tracks(self):
        results = self.host.premiere.eval_results
        results["actions.delete_empty_tracks"] = {"video": 2, "audio": 1}
        self.assertEqual(self.click("delete_empty_tracks"), ("ok", "Deleted 2 video tracks and 1 audio track"))
        results["actions.delete_empty_tracks"] = {"video": 0, "audio": 0}
        self.assertEqual(self.click("delete_empty_tracks"), ("neutral", "No empty tracks"))


class SelectionAndClipActions(QuickActionsCase):
    def test_select_same_source(self):
        self.host.premiere.eval_results["actions.select_same_source"] = {"count": 7, "name": "interview.mp4"}
        self.assertEqual(self.click("select_same_source"), ("ok", "Selected 7 clips of interview.mp4"))
        self.assertEqual(self.evals()[0][1], {"node_id": "n1"})

    def test_select_same_source_without_source_item(self):
        host, module = self.make(selection=(TrackItem("seq1", "video", 0, 0, "Title", None, 0, 1, 0, 1, True),))
        self.assertEqual(self.click("select_same_source", module, host),
                         ("warn", "The selected clip has no source item"))

    def test_select_offline(self):
        self.host.premiere.eval_results["actions.select_offline"] = {"count": 2}
        self.assertEqual(self.click("select_offline"), ("warn", "Selected 2 offline clips"))
        self.host.premiere.eval_results["actions.select_offline"] = {"count": 0}
        self.assertEqual(self.click("select_offline"), ("neutral", "No offline clips"))

    def test_scale_to_frame(self):
        self.host.premiere.eval_results["actions.scale_to_frame"] = {"clips": 3, "method": "clip"}
        self.assertEqual(self.click("scale_to_frame"), ("ok", "Set to frame size · 3 clips"))
        self.host.premiere.eval_results["actions.scale_to_frame"] = {"clips": 1, "method": "item"}
        self.assertEqual(self.click("scale_to_frame"), ("ok", "Set to frame size · 1 clip · set on the source items"))

    def test_scale_to_frame_needs_video(self):
        host, module = self.make(selection=(A2,))
        self.assertEqual(self.click("scale_to_frame", module, host), ("warn", "Select a video clip"))

    def test_label_by_bin(self):
        items = [ProjectItem("b1", "Footage", "bin"),
                 ProjectItem("c1", "a.mp4", "clip", "Footage", r"D:\a.mp4", color_label=1),
                 ProjectItem("c2", "b.mp4", "clip", "Footage/Day1", r"D:\b.mp4"),
                 ProjectItem("c3", "song.wav", "clip", "Audio/Music", r"D:\song.wav", color_label=5),
                 ProjectItem("c4", "x.png", "clip", "Refs", r"D:\x.png"),
                 ProjectItem("c5", "loose.mp4", "clip", "", r"D:\loose.mp4"),
                 ProjectItem("s1", "Spring", "sequence", "Footage", is_sequence=True)]
        self.host.premiere.set_items(items)
        toast = self.click("label_by_bin")
        self.assertEqual(toast, ("ok", "Labelled 3 items in 2 bins"))
        labels = {call[0][1]: set(call[0][0]) for call in self.host.premiere.calls_named("set_color_label")}
        self.assertEqual(labels, {COLOR_LABELS.index("Cerulean"): {"c1", "c2"}, COLOR_LABELS.index("Teal"): {"c4"}})
        job = self.host.jobs.jobs[-1]
        self.assertEqual(job.progress_log[-1], (1.0, "3 of 3"))
        self.assertEqual(self.click("label_by_bin"), ("neutral", "Labels already match the bins"))

    def test_label_by_bin_chunks_and_empty(self):
        self.host.premiere.set_items([ProjectItem(f"c{i}", f"{i}.mp4", "clip", "Footage") for i in range(120)])
        self.click("label_by_bin")
        sizes = [len(call[0][0]) for call in self.host.premiere.calls_named("set_color_label")]
        self.assertEqual(sizes, [50, 50, 20])
        host, module = self.make()
        self.assertEqual(self.click("label_by_bin", module, host), ("neutral", "No items in bins"))

    def test_label_plan_cycles_palette(self):
        items = [ProjectItem(f"c{i}", "x", "clip", f"Bin{i:02d}") for i in range(13)]
        plan = plan_labels(items)
        self.assertEqual(sum(len(ids) for ids in plan.values()), 13)
        self.assertIn("c0", plan[COLOR_LABELS.index("Teal")])
        self.assertIn("c11", plan[COLOR_LABELS.index("Teal")])


class TrackToggles(QuickActionsCase):
    def test_solo_then_restore(self):
        self.host.premiere.eval_results["actions.solo_track"] = lambda a: (
            {"states": [0, 1, 0], "count": 3} if a["mode"] == "solo" else {"restored": 3})
        self.assertEqual(self.click("solo_track"), ("ok", "Soloed A2"))
        self.assertEqual(self.evals()[-1][1], {"mode": "solo", "sequence_id": "seq1", "keep_index": 1})
        button = self._button("solo_track")
        self.assertEqual((button["label"], button["tone"]), ("Restore audio", "accent"))
        self.assertEqual(self.click("solo_track"), ("neutral", "Audio tracks restored"))
        self.assertEqual(self.evals()[-1][1], {"mode": "restore", "sequence_id": "seq1", "states": [0, 1, 0]})
        self.assertEqual(self._button("solo_track")["label"], "Solo audio track")

    def test_solo_needs_audio(self):
        host, module = self.make(selection=(V1,))
        self.assertEqual(self.click("solo_track", module, host), ("warn", "Select a clip with audio"))

    def test_lock_then_restore(self):
        self.host.premiere.eval_results["actions.lock_others"] = lambda a: (
            {"states": {"video": [0, 0, 1], "audio": [0, 0]}, "locked": 2} if a["mode"] == "lock" else {"restored": 5})
        self.assertEqual(self.click("lock_others"), ("ok", "Locked 2 tracks"))
        self.assertEqual(self.evals()[-1][1]["keep"], [{"kind": "audio", "index": 1}, {"kind": "video", "index": 0}])
        self.assertEqual(self._button("lock_others")["label"], "Restore locks")
        self.assertEqual(self.click("lock_others"), ("neutral", "Track locks restored"))
        self.assertEqual(self.evals()[-1][1]["states"], {"video": [0, 0, 1], "audio": [0, 0]})

    def test_lock_nothing_to_lock_keeps_no_state(self):
        self.host.premiere.eval_results["actions.lock_others"] = {"states": {}, "locked": 0}
        self.assertEqual(self.click("lock_others"), ("neutral", "No other tracks to lock"))
        self.assertNotIn("lock_others", self.module.toggles)

    def test_project_closed_forgets_toggles(self):
        self.host.premiere.eval_results["actions.solo_track"] = {"states": [0, 0], "count": 2}
        self.click("solo_track")
        self.module.project_closed()
        self.assertEqual(self._button("solo_track")["label"], "Solo audio track")

    def _button(self, action_id):
        for node in self.module.render():
            for candidate in node.get("items", [node]):
                if candidate.get("id") == f"actions.{action_id}":
                    return candidate
        raise AssertionError(action_id)


class ProjectActions(QuickActionsCase):
    def test_version_numbering(self):
        folder = self.home / "versions"
        folder.mkdir()
        self.assertEqual(next_version_path(folder / "Spring.prproj", folder).name, "Spring_v001.prproj")
        self.assertEqual(next_version_path(folder / "Spring_v003.prproj", folder).name, "Spring_v004.prproj")
        (folder / "Spring_v004.prproj").write_bytes(b"")
        (folder / "Spring_v005.prproj").write_bytes(b"")
        self.assertEqual(next_version_path(folder / "Spring_v003.prproj", folder).name, "Spring_v006.prproj")
        (folder / "Spring_v001.prproj").write_bytes(b"")
        self.assertEqual(next_version_path(folder / "Spring.prproj", folder).name, "Spring_v002.prproj")
        self.assertEqual(next_version_path(folder / "Cut_v999.prproj", folder).name, "Cut_v1000.prproj")
        self.assertEqual(next_version_path(folder / "Cut_v12.prproj", folder).name, "Cut_v12_v001.prproj")

    def test_save_version_in_project_folder(self):
        self.assertEqual(self.click("save_version"), ("ok", "Saved as Spring_v001.prproj"))
        (path,), _ = self.host.premiere.calls_named("save_project_as")[0]
        self.assertEqual(Path(path), Path(self.host.project.root) / "Spring_v001.prproj")
        Path(path).write_bytes(b"")
        self.assertEqual(self.click("save_version"), ("ok", "Saved as Spring_v002.prproj"))

    def test_save_version_uses_layout_project_folder(self):
        root = Path(self.host.project.root)
        (root / ".premiere-deck").mkdir()
        (root / ".premiere-deck" / "project.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.click("save_version"), ("ok", "Saved as Spring_v001.prproj"))
        (path,), _ = self.host.premiere.calls_named("save_project_as")[-1]
        self.assertEqual(Path(path).parent, root / "Project")
        self.assertTrue((root / "Project").is_dir())

    def test_version_folder_layout_values(self):
        root = self.home / "p"
        deck = root / ".premiere-deck"
        deck.mkdir(parents=True)
        project_file = root / "Edit" / "Spring.prproj"
        self.assertEqual(version_folder(project_file, root), root / "Edit")
        (deck / "project.json").write_text("{}", encoding="utf-8")
        (deck / "layout.json").write_text('{"project_dir": "Projects/Main"}', encoding="utf-8")
        self.assertEqual(version_folder(project_file, root), root / "Projects/Main")
        for broken in ('{"project_dir": "..\\\\up"}', '{"project_dir": "C:\\\\x"}', '{"project_dir": 3}', "not json"):
            (deck / "layout.json").write_text(broken, encoding="utf-8")
            self.assertEqual(version_folder(project_file, root), root / "Project", broken)

    def test_open_folder_and_reveal(self):
        self.assertEqual(self.click("open_project_folder"), ("neutral", "Opened Spring"))
        self.assertEqual(self.host.host.opened, [self.host.project.root])
        media = self.home / "a.mp4"
        self.host.premiere.set_items([ProjectItem("n1", "a.mp4", "clip", "Footage", str(media))])
        self.assertEqual(self.click("reveal_clip"), ("warn", "File not found: a.mp4"))
        media.write_bytes(b"x")
        self.assertEqual(self.click("reveal_clip"), ("neutral", "Revealed a.mp4"))
        self.assertEqual(self.host.host.revealed, [str(media)])
        self.host.premiere.set_items([ProjectItem("n1", "Title", "clip", "", None)])
        self.assertEqual(self.click("reveal_clip"), ("warn", "The selected clip has no media file"))

    def test_consolidate(self):
        self.assertEqual(self.click("consolidate_duplicates"), ("ok", "Duplicates consolidated"))
        self.assertEqual(self.evals()[0][0], "actions.consolidate_duplicates")


class SequenceAndExport(QuickActionsCase):
    def test_social_copy_arguments(self):
        self.host.premiere.eval_results["actions.social_copy"] = lambda a: {
            "name": a["name"], "sequence_id": "seq2", "width": a["width"], "height": a["height"]}
        self.assertEqual(self.click("social_copy"), ("ok", "Created Spring 9x16 · 1080x1920"))
        name, args, timeout = self.evals()[0]
        self.assertEqual(args, {"sequence_id": "seq1", "numerator": 9, "denominator": 16, "motion": "default",
                                "name": "Spring 9x16", "width": 1080, "height": 1920})
        self.assertGreaterEqual(timeout, 600000)
        self.module.on_action("actions.social_copy.format", "4x5")
        self.assertEqual(self.click("social_copy"), ("ok", "Created Spring 4x5 · 1080x1350"))
        self.module.on_action("actions.social_copy.format", "16x9")  # not an option: ignored
        self.assertEqual(self.module.choice(self.module.actions["social_copy"], "format"), "4x5")

    def test_social_copy_size_not_applied(self):
        self.host.premiere.eval_results["actions.social_copy"] = {"name": "Spring 9x16", "width": 607, "height": 1080}
        self.assertEqual(self.click("social_copy"),
                         ("warn", "Created Spring 9x16 · 607x1080 · frame size not changed to 1080x1920"))

    def test_target_sizes(self):
        self.assertEqual(target_size(1920, 1080, 9, 16), (1080, 1920))
        self.assertEqual(target_size(1920, 1080, 1, 1), (1080, 1080))
        self.assertEqual(target_size(3840, 2160, 4, 5), (2160, 2700))
        self.assertEqual(target_size(1280, 720, 9, 16), (720, 1280))
        self.assertEqual(target_size(1080, 1920, 9, 16), (1080, 1920))

    def test_export_frame(self):
        frames = Path(self.host.project.root) / "Exports" / "Frames"
        out = str(frames / "Spring_00-00-12-05.png")
        self.host.premiere.eval_results["actions.export_frame"] = {"path": out, "timecode": "00:00:12:05"}
        self.assertEqual(self.click("export_frame"), ("ok", "Frame saved · Spring_00-00-12-05.png"))
        self.assertEqual(self.evals()[0][1], {"out_dir": str(frames), "base_name": "Spring"})
        self.assertTrue(frames.is_dir())
        self.assertEqual(self.host.host.revealed, [out])

    def test_export_frame_sanitises_name(self):
        host, module = self.make(sequence=SequenceInfo("seq1", 'Cut: "final"?', 1920, 1080, 25.0, 60.0))
        host.premiere.eval_results["actions.export_frame"] = {"path": r"D:\x.png"}
        self.click("export_frame", module, host)
        self.assertEqual(self.evals(host)[0][1]["base_name"], "Cut_ _final__")

    def test_export_sequence_arguments(self):
        self.host.premiere.eval_results["actions.export_sequence"] = lambda a: {
            "path": a["out_dir"] + "\\" + a["base_name"] + ".mp4", "job_id": "7"}
        self.assertEqual(self.click("export_sequence"), ("ok", "Queued in Media Encoder · Spring.mp4"))
        _, args, timeout = self.evals()[0]
        self.assertEqual(args["preset"], str(self.system_root / "4E49434B_48323634" / "YouTube 1080p HD.epr"))
        self.assertEqual(args["out_dir"], str(Path(self.host.project.root) / "Exports"))
        self.assertEqual((args["sequence_id"], args["base_name"], args["in_to_out"]), ("seq1", "Spring", False))
        self.assertGreaterEqual(timeout, 60000)
        self.module.on_action("actions.export_sequence.preset", "tiktok_9x16")
        host_seq = SequenceInfo("seq1", "Spring", 1080, 1920, 25.0, 60.0, in_point=2.0, out_point=10.0)
        self.host.premiere.active_sequence_value = host_seq
        self.click("export_sequence")
        _, args, _ = self.evals()[-1]
        self.assertTrue(args["preset"].endswith("01 - Match Source - High bitrate.epr"))
        self.assertTrue(args["in_to_out"])

    def test_export_sequence_without_presets(self):
        empty = self.home / "nothing"
        empty.mkdir()
        self.module.preset_roots = [empty]
        self.module.refresh_presets()
        self.assertEqual(self.click("export_sequence"), ("warn", "No export presets found"))
        nodes = {n.get("id"): n for n in self.module.render() if n.get("id")}
        self.assertTrue(nodes["actions.export_sequence"]["disabled"])
        self.assertTrue(nodes["actions.export_sequence.preset"]["disabled"])
        self.module.section()  # still a valid tree

    def test_user_and_shipped_presets(self):
        user = self.module.user_presets_dir()
        user.mkdir(parents=True)
        (user / "ProRes 422.epr").write_text("x", encoding="utf-8")
        (user / "notes.txt").write_text("x", encoding="utf-8")
        shipped = self.home / "shipped"
        shipped.mkdir()
        (shipped / "TikTok 9x16.epr").write_text("x", encoding="utf-8")
        found = presets_mod.available_presets(user, shipped_dir=shipped, system_roots=[self.system_root])
        self.assertEqual([(p.id, p.source) for p in found],
                         [("youtube_1080p", "system"), ("instagram_9x16", "system"), ("tiktok_9x16", "shipped"),
                          ("match_h264", "system"), ("user:ProRes 422", "user")])
        result = self.module.on_action("actions.host:refresh_presets")
        self.assertEqual(result.toast, ("neutral", "5 export presets found"))
        self.module.on_action("actions.export_sequence.preset", "user:ProRes 422")
        self.host.premiere.eval_results["actions.export_sequence"] = {"path": r"D:\Spring.mov"}
        self.assertEqual(self.click("export_sequence"), ("ok", "Queued in Media Encoder · Spring.mov"))
        self.assertEqual(self.evals()[-1][1]["preset"], str(user / "ProRes 422.epr"))

    def test_system_roots_order(self):
        program_files = self.home / "pf"
        for app in ("Adobe Premiere Pro 2025", "Adobe Premiere Pro 2026", "Adobe Media Encoder 2026"):
            (program_files / "Adobe" / app / "MediaIO" / "systempresets").mkdir(parents=True)
        roots = [root.parent.parent.name for root in presets_mod.system_preset_roots(str(program_files))]
        self.assertEqual(roots, ["Adobe Premiere Pro 2026", "Adobe Media Encoder 2026", "Adobe Premiere Pro 2025"])
        self.assertEqual(presets_mod.system_preset_roots(str(self.home / "none")), [])


class JobsAndRendering(QuickActionsCase):
    def test_long_action_is_busy_until_done(self):
        host, module = self.make(manual_jobs=True)
        host.premiere.eval_results["actions.export_frame"] = {"path": r"D:\f.png"}
        result = module.on_action("actions.export_frame")
        self.assertIsNone(result.toast)
        button = next(n for n in module.render() if n.get("id") == "actions.export_frame")
        self.assertTrue(button["busy"])
        self.assertEqual(module.section_status(), {"text": "Running · Export frame", "tone": "accent"})
        self.assertFalse(module.on_action("actions.export_frame").rerender)  # a second press is ignored
        self.assertEqual(len(host.jobs.jobs), 1)
        host.jobs.step()
        self.assertEqual(host.ui.toasts, [("ok", "Frame saved · f.png")])
        self.assertGreaterEqual(host.ui.invalidations, 1)
        button = next(n for n in module.render() if n.get("id") == "actions.export_frame")
        self.assertFalse(button["busy"])
        self.assertIsNone(module.section_status())

    def test_long_actions_are_jobs(self):
        long_ids = sorted(aid for aid, action in self.module.actions.items() if action.long)
        self.assertEqual(long_ids, ["export_frame", "export_sequence", "label_by_bin", "social_copy"])

    def test_job_calls_go_through_module_thread(self):
        calls = []

        class Facade:
            def run_on_module_thread(self, fn, *args, **kw):
                calls.append(("marshalled", args, kw))
                future = Future()
                future.set_result(fn(*args, **kw))
                return future

            def eval(self, name, code, args=None, *, timeout_ms=30000):
                return {"name": name, "timeout_ms": timeout_ms}

        proxy = _ModuleThreadPremiere(Facade())
        self.assertEqual(proxy.eval("actions.x", "return 1;", timeout_ms=5), {"name": "actions.x", "timeout_ms": 5})
        self.assertEqual(calls, [("marshalled", ("actions.x", "return 1;"), {"timeout_ms": 5})])

        class Failing(Facade):
            def eval(self, name, code, args=None, *, timeout_ms=30000):
                raise PremiereError("deck: Nope.")

        with self.assertRaises(PremiereError):
            _ModuleThreadPremiere(Failing()).eval("actions.x", "")

    def test_render_groups_in_order_and_validates(self):
        section = self.module.section()
        groups = [n["text"] for n in section["items"] if n["kind"] == "text"]
        self.assertEqual(groups, ["Timeline", "Selection", "Clips", "Tracks", "Project", "Sequence", "Export"])
        ids = []
        for node in section["items"]:
            for item in node.get("items", [node]):
                if item.get("id"):
                    ids.append(item["id"])
        self.assertEqual(ids.index("actions.social_copy.format") + 1, ids.index("actions.social_copy"))
        self.assertEqual(ids.index("actions.export_sequence.preset") + 1, ids.index("actions.export_sequence"))
        self.assertEqual(sorted(i for i in ids if i.count(".") == 1), sorted(f"actions.{a}" for a in ALL_IDS))

    def test_render_with_hidden_actions(self):
        host, module = self.make(settings={"hidden": ["select_same_source", "select_offline", "export_frame"]})
        section = module.section()
        texts = [n["text"] for n in section["items"] if n["kind"] == "text"]
        self.assertNotIn("Selection", texts)
        flat = [i.get("id") for n in section["items"] for i in n.get("items", [n])]
        self.assertNotIn("actions.export_frame", flat)
        self.assertIn("actions.export_sequence", flat)
        module.on_action("actions.host:visible.export_frame", True)
        module.on_action("actions.host:visible.razor_all", False)
        self.assertEqual(host.settings.get("hidden"), ["razor_all", "select_same_source", "select_offline"])

    def test_all_hidden(self):
        host, module = self.make(settings={"hidden": list(ALL_IDS)})
        nodes = module.section()["items"]
        self.assertEqual(len(nodes), 1)
        self.assertIn("hidden", nodes[0]["text"])

    def test_settings_fallback_on_broken_values(self):
        for hidden, choices in (("oops", "bad"), ([1, None, "remove_gaps", "gone"], {"social_copy.format": 5}),
                                ({"x": 1}, {"export_sequence.preset": "missing"}), (None, None)):
            with self.subTest(hidden=hidden, choices=choices):
                host, module = self.make(settings={"hidden": hidden, "choices": choices})
                module.section()
                expected = {"remove_gaps"} if isinstance(hidden, list) else set()
                self.assertEqual(module.hidden_ids(), expected)
                self.assertEqual(module.choice(module.actions["social_copy"], "format"), "9x16")
                self.assertEqual(module.choice(module.actions["export_sequence"], "preset"), "youtube_1080p")

    def test_last_result_is_rendered(self):
        self.host.premiere.eval_results["actions.select_offline"] = {"count": 0}
        self.click("select_offline")
        last = self.module.render()[-1]
        self.assertEqual((last["kind"], last["text"], last["tone"]), ("text", "No offline clips", "neutral"))

    def test_render_host_page(self):
        page = self.module.render_host()
        visibility, presets = page["blocks"]
        self.assertEqual([row["id"] for row in visibility["rows"]], list(ALL_IDS))
        self.assertTrue(all(row["toggle"]["value"] for row in visibility["rows"]))
        self.assertEqual(visibility["rows"][0]["toggle"]["id"], "actions.host:visible.adjustment_layer")
        self.assertEqual(len(presets["rows"]), 4)
        self.module.on_action("actions.host:open_presets")
        self.assertEqual(self.host.host.opened, [str(self.module.user_presets_dir())])
        self.assertTrue(self.module.user_presets_dir().is_dir())

    def test_unknown_action(self):
        result = self.module.on_action("actions.nothing")
        self.assertEqual(result.toast[0], "warn")
        self.assertEqual(self.module.on_action("actions.host:nothing").toast[0], "warn")

    def test_copy_rules(self):
        for action in self.module.actions.values():
            with self.subTest(action.id):
                self.assertLessEqual(len(action.label.split()), 4)
                self.assertTrue(action.hint.endswith("."))
                self.assertNotIn("!", action.hint + action.label)
                self.assertTrue(action.feasibility)
                self.assertIn(action.group, ("Timeline", "Selection", "Clips", "Tracks", "Project", "Sequence",
                                             "Export"))

    def test_helpers(self):
        self.assertEqual(timecode(3725.9), "01:02:05")
        self.assertEqual(safe_file_stem("a/b. "), "a_b")
        self.assertEqual(safe_file_stem("..."), "Sequence")


class Scripts(QuickActionsCase):
    FORBIDDEN = (r"\blet\s", r"\bconst\s", r"=>", r"`", r"\bclass\s", r"\bJSON\.", r"\balert\(", r"\bconfirm\(",
                 r"\bprompt\(", r"\$\.writeln", r"\.forEach\(", r"\.map\(", r"\.filter\(", r"\bfunction\s+\w+\s*\(")

    def scripts(self):
        return sorted(JSX_DIR.glob("*.jsx"))

    def test_every_scripted_action_has_a_script(self):
        names = {path.stem for path in self.scripts()}
        self.assertEqual(names - {"_common"}, set(SCRIPTED))

    def test_scripts_are_es3(self):
        for path in self.scripts():
            code = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
            for pattern in self.FORBIDDEN:
                with self.subTest(path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))

    def test_actions_return_values(self):
        for name in SCRIPTED:
            with self.subTest(name):
                self.assertIn("return", (JSX_DIR / f"{name}.jsx").read_text(encoding="utf-8"))

    def test_script_text_prepends_common(self):
        ctx = self.host.context()
        text = script_text(ctx, "razor_all")
        self.assertTrue(text.startswith("// Helpers shared by the Quick actions scripts"))
        self.assertIn("deckQeTrack(qeSeq, entry.kind, entry.index).razor(tc)", text)
        lib = self.home / "uses_lib.jsx"
        lib.write_text("// @uses lib\nreturn 1;\n", encoding="utf-8")
        with mock.patch("premiere_deck.modules.quick_actions.action.JSX_DIR", self.home):
            shutil.copy(JSX_DIR / "_common.jsx", self.home / "_common.jsx")
            self.assertTrue(script_text(ctx, "uses_lib").startswith("// @uses lib\n"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_scripts_parse(self):
        common = (JSX_DIR / "_common.jsx").read_text(encoding="utf-8")
        for path in self.scripts():
            if path.stem == "_common":
                continue
            with self.subTest(path.name):
                wrapped = self.home / f"{path.stem}.check.js"
                wrapped.write_text("(function(){ var __args = {};\n" + common + "\n" + path.read_text(encoding="utf-8")
                                   + "\n});\n", encoding="utf-8")
                done = subprocess.run(["node", "--check", str(wrapped)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
