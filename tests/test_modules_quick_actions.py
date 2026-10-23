import importlib
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
from premiere_deck.modules.quick_actions import easing, premiere_window
from premiere_deck.modules.quick_actions.action import JSX_DIR, error_sentence, safe_file_stem, script_text, timecode
from premiere_deck.modules.quick_actions.actions.colour import MARKER_COLOURS, MARKER_FOR, SWATCHES, VIA_ITEMS
from premiere_deck.modules.quick_actions.actions.create_16_9 import free_name, target_size
from premiere_deck.modules.quick_actions.actions.create_custom import parse_size, ratio
from premiere_deck.modules.quick_actions.actions.duplicate_sequence import next_sequence_name
from premiere_deck.modules.quick_actions.actions.ease_slow import NO_PAIR
from premiere_deck.modules.quick_actions.actions.label_by_bin import plan_labels
from premiere_deck.modules.quick_actions.actions.save_version import next_version_path, version_folder
from premiere_deck.modules.quick_actions.module import QuickActionsModule, _ModuleThreadPremiere
from premiere_deck.premiere.model import (
    COLOR_LABELS, PremiereError, ProjectItem, Selection, SequenceInfo, TrackItem,
)

SEQUENCE = SequenceInfo("seq1", "Spring", 1920, 1080, 25.0, 60.0)
V1 = TrackItem("seq1", "video", 0, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True)
A2 = TrackItem("seq1", "audio", 1, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True)
TPS = easing.TICKS_PER_SECOND
REAL_MENU_RUN = premiere_window.run
FRAME = TPS // 25

EASES = ("ease_slow", "ease_med", "ease_fast", "ease_linear")
TRACKS = ("add_video_track", "add_audio_track", "remove_video_track", "remove_audio_track")
REFRAMES = ("create_16_9", "create_9_16", "create_custom")
NEEDS_SEQUENCE = ("adjustment_layer", "razor_all", "delete_empty_tracks") + TRACKS + (
    "solo_track", "lock_others", "select_same_source", "select_offline", "scale_to_frame", "reveal_clip",
    "duplicate_sequence") + REFRAMES + ("export_open", "export_frame") + EASES
NEEDS_SELECTION = ("select_same_source", "scale_to_frame", "solo_track", "lock_others", "reveal_clip") + EASES
# action id -> the script its first eval runs (docs/QUICK_ACTIONS.md 5)
SCRIPT_OF = {
    "adjustment_layer": "adjustment_layer", "razor_all": "razor_all", "delete_empty_tracks": "delete_empty_tracks",
    "add_video_track": "add_track", "add_audio_track": "add_track", "remove_video_track": "remove_track",
    "remove_audio_track": "remove_track", "solo_track": "solo_track", "lock_others": "lock_others",
    "select_same_source": "select_same_source", "select_offline": "select_offline",
    "scale_to_frame": "scale_to_frame", "colour": "colour", "consolidate_duplicates": "consolidate_duplicates",
    "duplicate_sequence": "duplicate_sequence", "create_16_9": "reframe", "create_9_16": "reframe",
    "create_custom": "reframe", "export_open": "export_open", "export_frame": "export_frame",
    "ease_slow": "ease_read", "ease_med": "ease_read", "ease_fast": "ease_read", "ease_linear": "ease_read",
}
# docs/QUICK_ACTIONS.md 1, in panel order: id -> (group, icon, short)
TILES = {
    "adjustment_layer": ("Timeline", "adjustment-layer", "ADJ"), "razor_all": ("Timeline", "razor-all", "RZR"),
    "delete_empty_tracks": ("Timeline", "delete-empty-tracks", "EMPT"),
    "add_video_track": ("Tracks", "track-video-add", "+V"), "add_audio_track": ("Tracks", "track-audio-add", "+A"),
    "remove_video_track": ("Tracks", "track-video-remove", "\u2212V"),
    "remove_audio_track": ("Tracks", "track-audio-remove", "\u2212A"),
    "solo_track": ("Tracks", "solo-track", "SOLO"), "lock_others": ("Tracks", "lock-others", "LOCK"),
    "select_same_source": ("Selection", "select-same-source", "SRC"),
    "select_offline": ("Selection", "select-offline", "OFF"),
    "scale_to_frame": ("Clips", "scale-to-frame", "FIT"), "colour": ("Clips", "colour", "CLR"),
    "label_by_bin": ("Clips", "label-by-bin", "BIN"),
    "save_version": ("Project", "save-version", "SAVE"), "consolidate_duplicates": ("Project", "consolidate", "CONS"),
    "open_project_folder": ("Project", "open-folder", "DIR"), "reveal_clip": ("Project", "reveal-clip", "REV"),
    "duplicate_sequence": ("Sequence", "duplicate-sequence", "DUP"), "create_16_9": ("Sequence", "ratio-16-9", "16:9"),
    "create_9_16": ("Sequence", "ratio-9-16", "9:16"), "create_custom": ("Sequence", "ratio-custom", "CUST"),
    "export_open": ("Export", "export", "EXP"), "export_frame": ("Export", "export-frame", "FRM"),
    "ease_slow": ("Keys", "ease-slow", "SLOW"), "ease_med": ("Keys", "ease-med", "MED"),
    "ease_fast": ("Keys", "ease-fast", "FAST"), "ease_linear": ("Keys", "ease-linear", "LIN"),
}
ALL_IDS = tuple(TILES)
GROUPS = ("Timeline", "Tracks", "Selection", "Clips", "Project", "Sequence", "Export", "Keys")
SCRIPTS = {"adjustment_layer", "razor_all", "delete_empty_tracks", "add_track", "remove_track", "solo_track",
           "lock_others", "select_same_source", "select_offline", "scale_to_frame", "colour",
           "consolidate_duplicates", "duplicate_sequence", "reframe", "export_open", "export_frame", "ease_read",
           "ease_apply"}


def flat(nodes):
    """The nodes of a rendered section with the contents of groups and rows spliced in, in order."""
    out = []
    for node in nodes:
        out.append(node)
        if node.get("kind") in ("group", "row"):
            out.extend(flat(node["items"]))
    return out


def rendered(module, node_id):
    return next(n for n in flat(module.render()) if n.get("id") == node_id)


class QuickActionsCase(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("PREMIERE_DECK_HOME")
        if base:
            os.makedirs(base, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="deck-actions-", dir=base or None))
        patcher = mock.patch.dict(os.environ, {"PREMIERE_DECK_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Premiere's menu bar is never reached from a test: commands are recorded and succeed unless told otherwise.
        self.menu_runs = []
        self.menu_ok = True

        def run(commands):
            self.menu_runs.append(list(commands))
            return self.menu_ok

        menu = mock.patch.object(premiere_window, "run", side_effect=run)
        menu.start()
        self.addCleanup(menu.stop)
        self.host, self.module = self.make()

    def make(self, *, manual_jobs=False, settings=None, sequence=SEQUENCE, selection=(V1, A2), project=True):
        host = FakeHost(manual_jobs=manual_jobs, settings=settings, module_id="actions")
        if project:
            host.make_project(name="Spring")
        host.premiere.active_sequence_value = sequence
        host.premiere.selection_value = Selection("seq1", tuple(selection))
        module = QuickActionsModule(host.context())
        module.start()
        return host, module

    def click(self, action_id, module=None, host=None, value=None):
        """Presses a tile (or sends `value` to a switch, menu or input id); returns the reported (outcome, text) and
        keeps its detail in `self.detail`. A press never sends a toast (docs/MODULES.md 4)."""
        module = module or self.module
        host = host or self.host
        results, toasts = len(host.ui.results), len(host.ui.toasts)
        result = module.on_action(f"actions.{action_id}", value)
        self.assertTrue(result is None or not result.toast, "a press reports through ctx.ui.result, not a toast")
        self.assertEqual(len(host.ui.toasts), toasts)
        self.detail = None
        if len(host.ui.results) == results:
            return None
        button_id, outcome, text, self.detail = host.ui.results[-1]
        self.assertEqual(button_id, action_id.split(".")[0])
        return outcome, text

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
                          "export_frame"):
            with self.subTest(action_id):
                host, module = self.make(project=False)
                self.assertEqual(self.click(action_id, module, host), ("warn", "No project open"))

    def test_project_actions_work_without_sequence_or_selection(self):
        host, module = self.make(sequence=None, selection=())
        self.assertEqual(self.click("open_project_folder", module, host)[0], "neutral")
        self.assertEqual(self.click("consolidate_duplicates", module, host), ("ok", "Duplicates consolidated"))
        self.assertEqual(self.click("save_version", module, host)[0], "ok")
        host.premiere.eval_results["actions.colour"] = {"items": 1}
        self.assertEqual(self.click("colour", module, host), ("ok", "Coloured 1 item Cerulean"))

    def test_not_connected(self):
        for action_id in ALL_IDS:
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.connected = False
                self.assertEqual(self.click(action_id, module, host), ("warn", "Premiere Pro is not connected"))


class ScriptErrors(QuickActionsCase):
    def test_user_sentence_is_shown(self):
        for action_id, script in SCRIPT_OF.items():
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.eval_errors[f"actions.{script}"] = PremiereError(
                    "Error: deck: No free video track above the selection.", 12, "var seq")
                label = module.actions[action_id].label
                with self.assertLogs("module.actions.test", level="WARNING"):
                    toast = self.click(action_id, module, host)
                self.assertEqual(toast, ("error", f"{label} failed"))
                self.assertEqual(self.detail, "No free video track above the selection.")

    def test_raw_errors_are_not_shown(self):
        for action_id, script in SCRIPT_OF.items():
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.eval_errors[f"actions.{script}"] = PremiereError("undefined is not an object", 3)
                label = module.actions[action_id].label
                with self.assertLogs("module.actions.test", level="WARNING"):
                    toast = self.click(action_id, module, host)
                self.assertEqual(toast, ("error", f"{label} failed"))
                self.assertEqual(self.detail, "Premiere reported a script error.")

    def test_timeout_sentence(self):
        self.host.premiere.eval_errors["actions.reframe"] = PremiereError("timeout")
        with self.assertLogs("module.actions.test", level="WARNING"):
            self.assertEqual(self.click("create_9_16"), ("error", "Reframe 9:16 failed"))
        self.assertEqual(self.detail, "Premiere is waiting for a dialog.")

    def test_facade_errors_of_host_side_actions(self):
        self.host.premiere.save_project_as = mock.Mock(side_effect=PremiereError("deck: The disk is full."))
        self.host.premiere.snapshot = mock.Mock(side_effect=PremiereError("timeout"))
        with self.assertLogs("module.actions.test", level="WARNING"):
            self.assertEqual(self.click("save_version"), ("error", "Save version failed"))
            self.assertEqual(self.detail, "The disk is full.")
            self.assertEqual(self.click("label_by_bin"), ("error", "Colour labels by bin failed"))
            self.assertEqual(self.detail, "Premiere is waiting for a dialog.")

    def test_python_bug_becomes_sentence(self):
        self.host.premiere.eval_results["actions.razor_all"] = lambda args: 1 / 0
        with self.assertLogs("module.actions.test", level="ERROR"):
            toast = self.click("razor_all")
        self.assertEqual(toast, ("error", "Razor all tracks failed"))
        self.assertEqual(self.detail, "An internal error occurred. Details are in the log.")

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

    def test_razor_all(self):
        results = self.host.premiere.eval_results
        results["actions.razor_all"] = {"cuts": 3, "locked": 0, "failed": 0, "seconds": 62.4}
        self.assertEqual(self.click("razor_all"), ("ok", "Cut 3 tracks at 00:01:02"))
        results["actions.razor_all"] = {"cuts": 2, "locked": 1, "failed": 0, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("ok", "Cut 2 tracks at 00:00:01 · 1 locked track skipped"))
        results["actions.razor_all"] = {"cuts": 0, "locked": 0, "failed": 0, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("neutral", "No clip under the playhead"))
        results["actions.razor_all"] = {"cuts": 0, "locked": 2, "failed": 0, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("warn", "Nothing cut"))
        self.assertEqual(self.detail, "The clips under the playhead are on locked tracks.")
        results["actions.razor_all"] = {"cuts": 0, "locked": 0, "failed": 2, "seconds": 1}
        self.assertEqual(self.click("razor_all"), ("error", "Razor all tracks failed"))
        self.assertEqual(self.detail, "Premiere did not cut at the playhead.")

    def test_delete_empty_tracks(self):
        results = self.host.premiere.eval_results
        results["actions.delete_empty_tracks"] = {"video": 2, "audio": 1}
        self.assertEqual(self.click("delete_empty_tracks"), ("ok", "Deleted 2 video tracks and 1 audio track"))
        results["actions.delete_empty_tracks"] = {"video": 0, "audio": 0}
        self.assertEqual(self.click("delete_empty_tracks"), ("neutral", "No empty tracks"))


class TrackActions(QuickActionsCase):
    def test_add_tracks(self):
        self.host.premiere.eval_results["actions.add_track"] = lambda a: {
            "kind": a["kind"], "index": 3 if a["kind"] == "video" else 2, "count": 4}
        self.assertEqual(self.click("add_video_track"), ("ok", "Added V4"))
        self.assertEqual(self.click("add_audio_track"), ("ok", "Added A3"))
        self.assertEqual([args for _, args, _ in self.evals(name="actions.add_track")],
                         [{"kind": "video", "audio_type": 1}, {"kind": "audio", "audio_type": 1}])

    def test_remove_tracks(self):
        results = self.host.premiere.eval_results
        cases = (({"removed": True, "index": 2}, ("ok", "Removed V3"), ("ok", "Removed A3")),
                 ({"removed": False, "reason": "not_empty", "index": 2}, ("warn", "V3 is not empty"),
                  ("warn", "A3 is not empty")),
                 ({"removed": False, "reason": "locked", "index": 1}, ("warn", "V2 is locked"),
                  ("warn", "A2 is locked")),
                 ({"removed": False, "reason": "last", "index": 0}, ("neutral", "V1 is the only video track"),
                  ("neutral", "A1 is the only audio track")))
        for answer, video, audio in cases:
            with self.subTest(answer=answer):
                results["actions.remove_track"] = answer
                self.assertEqual(self.click("remove_video_track"), video)
                self.assertEqual(self.click("remove_audio_track"), audio)
        results["actions.remove_track"] = {"removed": False, "reason": "odd"}
        self.assertEqual(self.click("remove_video_track"), ("error", "Remove video track failed"))
        self.assertEqual(self.detail, "Premiere did not remove the track.")
        self.assertEqual({args["kind"] for _, args, _ in self.evals(name="actions.remove_track")}, {"video", "audio"})


class SwitchActions(QuickActionsCase):
    def solo(self):
        self.host.premiere.eval_results["actions.solo_track"] = lambda a: (
            {"states": [0, 1, 0], "count": 3} if a["mode"] == "solo" else {"restored": 3})

    def test_solo_is_a_switch_with_state(self):
        self.solo()
        node = rendered(self.module, "actions.solo_track")
        self.assertEqual((node["kind"], node["value"], node["short"], node["icon"]),
                         ("switch", False, "SOLO", "solo-track"))
        self.assertEqual(self.click("solo_track", value=True), ("ok", "Soloed A2"))
        self.assertEqual(self.evals()[-1][1], {"mode": "solo", "sequence_id": "seq1", "keep_index": 1})
        node = rendered(self.module, "actions.solo_track")
        self.assertEqual((node["value"], node["label"]), (True, "Solo audio track"))
        self.assertIsNone(self.click("solo_track", value=True), "already on: nothing runs")
        self.assertEqual(len(self.evals()), 1)
        self.assertEqual(self.click("solo_track", value=False), ("neutral", "Audio tracks restored"))
        self.assertEqual(self.evals()[-1][1], {"mode": "restore", "sequence_id": "seq1", "states": [0, 1, 0]})
        self.assertFalse(rendered(self.module, "actions.solo_track")["value"])
        self.assertIsNone(self.click("solo_track", value=False), "already off: nothing runs")

    def test_refused_switch_stays_off(self):
        host, module = self.make(selection=(V1,))
        self.assertEqual(self.click("solo_track", module, host, value=True), ("warn", "Select a clip with audio"))
        self.assertFalse(rendered(module, "actions.solo_track")["value"])
        self.assertEqual(self.evals(host), [])

    def test_lock_round_trip(self):
        self.host.premiere.eval_results["actions.lock_others"] = lambda a: (
            {"states": {"video": [0, 0, 1], "audio": [0, 0]}, "locked": 2} if a["mode"] == "lock" else {"restored": 5})
        self.assertEqual(self.click("lock_others", value=True), ("ok", "Locked 2 tracks"))
        self.assertEqual(self.evals()[-1][1]["keep"], [{"kind": "audio", "index": 1}, {"kind": "video", "index": 0}])
        self.assertTrue(rendered(self.module, "actions.lock_others")["value"])
        self.assertEqual(self.click("lock_others", value=False), ("neutral", "Track locks restored"))
        self.assertEqual(self.evals()[-1][1]["states"], {"video": [0, 0, 1], "audio": [0, 0]})
        self.assertFalse(rendered(self.module, "actions.lock_others")["value"])

    def test_lock_nothing_to_lock_stays_off(self):
        self.host.premiere.eval_results["actions.lock_others"] = {"states": {}, "locked": 0}
        self.assertEqual(self.click("lock_others", value=True), ("neutral", "No other tracks to lock"))
        self.assertFalse(rendered(self.module, "actions.lock_others")["value"])

    def test_project_closed_forgets_state(self):
        self.solo()
        self.click("solo_track", value=True)
        self.module.project_closed()
        self.assertFalse(rendered(self.module, "actions.solo_track")["value"])

    def test_switch_is_busy_while_running(self):
        node = self.module._tile(self.module.actions["solo_track"])
        self.assertFalse(node["busy"])
        self.module._running["solo_track"] = object()
        self.assertTrue(self.module._tile(self.module.actions["solo_track"])["busy"])


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


class ColourAction(QuickActionsCase):
    def test_menu_of_the_sixteen_labels(self):
        node = rendered(self.module, "actions.colour")
        self.assertEqual(node["menu"]["id"], "actions.colour.label")
        self.assertEqual([o["value"] for o in node["menu"]["options"]], list(COLOR_LABELS))
        self.assertEqual([o["label"] for o in node["menu"]["options"]], list(COLOR_LABELS))
        self.assertEqual((node["menu"]["value"], node["menu"]["badge"]), ("Cerulean", "Cerulean"))
        self.assertEqual(len(COLOR_LABELS), 16)
        self.assertEqual(set(MARKER_FOR), set(COLOR_LABELS))
        self.assertTrue(set(MARKER_FOR.values()) <= set(MARKER_COLOURS))
        self.assertEqual([o["swatch"] for o in node["menu"]["options"]], [SWATCHES[name] for name in COLOR_LABELS])
        self.assertTrue(all(re.fullmatch(r"#[0-9a-f]{6}", colour) for colour in SWATCHES.values()))

    def test_pick_colours_the_timeline_selection(self):
        self.host.premiere.eval_results["actions.colour"] = {"clips": 2, "markers": 1, "sequence": "Spring"}
        self.assertEqual(self.click("colour.label", value="Rose"), ("ok", "Coloured 2 clips Rose · 1 marker Red"))
        self.assertIsNone(self.detail)
        self.assertEqual(self.evals()[0][1], {"label": 6, "marker": 1, "mode": "select"})
        self.assertEqual(self.menu_runs, [premiere_window.label_commands("Spring", 6, "Rose")])
        self.assertEqual(self.module.choice(self.module.actions["colour"], "label"), "Rose")
        self.assertEqual(rendered(self.module, "actions.colour")["menu"]["badge"], "Rose")
        self.assertEqual(self.click("colour")[1], "Coloured 2 clips Rose · 1 marker Red", "a press reuses the pick")

    def test_menu_unreachable_falls_back_to_project_items(self):
        self.menu_ok = False
        self.host.premiere.eval_results["actions.colour"] = {"clips": 3, "sequence": "Spring"}
        self.assertEqual(self.click("colour"), ("warn", "Coloured 3 clips Cerulean"))
        self.assertEqual(self.detail, VIA_ITEMS)
        self.assertEqual([args["mode"] for _, args, _ in self.evals()], ["select", "clip_items"])

    def test_project_items_markers_and_nothing(self):
        results = self.host.premiere.eval_results
        results["actions.colour"] = {"items": 2, "bins": 1, "markers": 0}
        self.assertEqual(self.click("colour"), ("ok", "Coloured 2 items and 1 bin Cerulean"))
        results["actions.colour"] = {"markers": 1}
        self.assertEqual(self.click("colour"), ("ok", "Coloured 1 marker Blue"))
        results["actions.colour"] = {}
        self.assertEqual(self.click("colour"), ("warn", "Nothing selected"))
        self.assertEqual(self.menu_runs, [], "no timeline clips: Premiere's menu is not used")

    def test_unknown_colour_is_ignored(self):
        self.assertIsNone(self.click("colour.label", value="Pink"))
        self.assertEqual(self.evals(), [])
        self.assertEqual(self.module.choice(self.module.actions["colour"], "label"), "Cerulean")

    def test_menu_commands(self):
        commands = premiere_window.label_commands("Spring", 6, "Rose")
        self.assertEqual([(c.command, c.path, c.required) for c in commands],
                         [(None, ("Window", "Timelines", "Spring"), True),
                          (13130, ("Edit", "Label", "Rose"), True)])
        export = premiere_window.export_commands("Spring")
        self.assertEqual([(c.command, c.path, c.required) for c in export],
                         [(None, ("Window", "Timelines", "Spring"), False),
                          (11240, ("File", "Export", "Media..."), True)])
        self.assertEqual(premiere_window.menu_label("&Media...\tCtrl+M"), "Media...")
        self.assertEqual(premiere_window.menu_label("Rose"), "Rose")

    def test_menu_run_outside_windows(self):
        with mock.patch.object(premiere_window, "sys") as fake_sys,                 mock.patch.object(premiere_window, "_post") as post:
            fake_sys.platform = "linux"
            self.assertFalse(REAL_MENU_RUN(premiere_window.export_commands("Spring")))
        post.assert_not_called()


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


class SequenceActions(QuickActionsCase):
    def reframe_echo(self):
        self.host.premiere.eval_results["actions.reframe"] = lambda a: {
            "name": a["name"], "sequence_id": "seq2", "width": a["width"], "height": a["height"]}

    def test_duplicate_sequence_naming(self):
        self.assertEqual(next_sequence_name("Spring", ["Spring"]), "Spring v2")
        self.assertEqual(next_sequence_name("Spring v2", ["Spring", "Spring v2"]), "Spring v3")
        self.assertEqual(next_sequence_name("Spring v2", ["Spring", "Spring v2", "Spring v3"]), "Spring v4")
        self.assertEqual(next_sequence_name("Spring", ["Spring", "Spring v2", "SPRING V3"]), "Spring v4")
        self.assertEqual(next_sequence_name("Spring v9", ["Spring v9"]), "Spring v10")
        self.assertEqual(next_sequence_name("Spring v007", []), "Spring v8")
        self.assertEqual(next_sequence_name("Mix v", []), "Mix v v2")
        self.assertEqual(next_sequence_name("v2", []), "v2 v2")
        self.assertEqual(next_sequence_name(" v2", []), " v2 v2")
        self.assertEqual(next_sequence_name("Take 2", ["Take 2 v2"]), "Take 2 v3")

    def test_duplicate_sequence(self):
        pr = self.host.premiere
        pr.sequences_value = [SEQUENCE, SequenceInfo("seq9", "Spring v2", 1920, 1080, 25.0, 10.0)]
        pr.eval_results["actions.duplicate_sequence"] = lambda a: {"name": a["name"], "sequence_id": "seq10"}
        self.assertEqual(self.click("duplicate_sequence"), ("ok", "Duplicated as Spring v3"))
        (name, args, timeout), = self.evals()
        self.assertEqual((name, args), ("actions.duplicate_sequence", {"sequence_id": "seq1", "name": "Spring v3"}))

    def test_duplicate_sequence_script_finds_the_copy_and_keeps_the_original_active(self):
        code = (JSX_DIR / "duplicate_sequence.jsx").read_text(encoding="utf-8")
        for fragment in ("seq.clone()", "before[String(seqs[i].sequenceID)]", "copy.name = __args.name",
                         "app.project.openSequence(seq.sequenceID)"):
            self.assertIn(fragment, code)

    def test_target_sizes(self):
        self.assertEqual(target_size(1080, 1920, 16, 9), (1920, 1080))
        self.assertEqual(target_size(1920, 1080, 16, 9), (1920, 1080))
        self.assertEqual(target_size(1920, 1080, 9, 16), (1080, 1920))
        self.assertEqual(target_size(1080, 1920, 9, 16), (1080, 1920))
        self.assertEqual(target_size(3840, 2160, 9, 16), (2160, 3840))
        self.assertEqual(target_size(1280, 720, 9, 16), (720, 1280))
        self.assertEqual(target_size(1080, 1080, 16, 9), (1920, 1080))

    def test_free_names(self):
        self.assertEqual(free_name("Spring 9x16", ["Spring"]), "Spring 9x16")
        self.assertEqual(free_name("Spring 9x16", ["Spring 9x16"]), "Spring 9x16 2")
        self.assertEqual(free_name("Spring 9x16", ["spring 9X16", "Spring 9x16 2"]), "Spring 9x16 3")

    def test_create_16_9_and_9_16(self):
        self.reframe_echo()
        host, module = self.make(sequence=SequenceInfo("seq1", "Spring", 1080, 1920, 25.0, 60.0))
        host.premiere.eval_results["actions.reframe"] = self.host.premiere.eval_results["actions.reframe"]
        self.assertEqual(self.click("create_16_9", module, host), ("ok", "Created Spring 16x9 · 1920x1080"))
        name, args, timeout = self.evals(host)[0]
        self.assertEqual(name, "actions.reframe")
        self.assertEqual(args, {"sequence_id": "seq1", "numerator": 16, "denominator": 9, "motion": "default",
                                "name": "Spring 16x9", "width": 1920, "height": 1080})
        self.assertGreaterEqual(timeout, 600000)
        self.assertEqual(self.click("create_9_16"), ("ok", "Created Spring 9x16 · 1080x1920"))
        self.assertEqual(self.evals()[-1][1]["numerator"], 9)
        self.host.premiere.sequences_value = [SEQUENCE, SequenceInfo("s2", "Spring 9x16", 1080, 1920, 25.0, 1.0)]
        self.assertEqual(self.click("create_9_16"), ("ok", "Created Spring 9x16 2 · 1080x1920"))

    def test_reframe_size_not_applied(self):
        self.host.premiere.eval_results["actions.reframe"] = {"name": "Spring 9x16", "width": 607, "height": 1080}
        self.assertEqual(self.click("create_9_16"),
                         ("warn", "Created Spring 9x16 · 607x1080 · frame size not changed to 1080x1920"))

    def test_custom_size_input(self):
        self.reframe_echo()
        field = rendered(self.module, "actions.create_custom.size")
        self.assertEqual((field["kind"], field["value"], field["submit_label"], field["placeholder"]),
                         ("input", "1080x1350", "Create", "Width x height"))
        self.assertEqual(self.click("create_custom"), ("ok", "Created Spring 1080x1350 · 1080x1350"))
        self.assertEqual({k: self.evals()[-1][1][k] for k in ("numerator", "denominator", "width", "height")},
                         {"numerator": 4, "denominator": 5, "width": 1080, "height": 1350})
        self.assertEqual(self.click("create_custom.size", value="1920 x 800"),
                         ("ok", "Created Spring 1920x800 · 1920x800"))
        self.assertEqual(self.evals()[-1][1]["numerator"], 12)
        self.assertEqual(self.host.settings.get("choices")["create_custom.size"], "1920x800")
        self.assertEqual(rendered(self.module, "actions.create_custom.size")["value"], "1920x800")

    def test_custom_size_refused(self):
        for bad in ("abc", "10x10", "1080", "99999x1080", None, 5):
            with self.subTest(bad=bad):
                self.assertEqual(self.click("create_custom.size", value=bad), ("warn", "Enter a size like 1080x1350"))
                self.assertEqual(self.detail, "Width and height between 16 and 16384 pixels.")
        self.assertEqual(self.evals(), [])
        self.assertNotIn("create_custom.size", self.host.settings.get("choices", {}))

    def test_parse_size_and_ratio(self):
        self.assertEqual(parse_size("1080x1350"), (1080, 1350))
        self.assertEqual(parse_size(" 1080 X 1350 "), (1080, 1350))
        self.assertEqual(parse_size("1080\u00d71350"), (1080, 1350))
        self.assertEqual(parse_size("1080*1350"), (1080, 1350))
        self.assertIsNone(parse_size("1080x"))
        self.assertIsNone(parse_size("0x100"))
        self.assertEqual(ratio(1080, 1350), (4, 5))
        self.assertEqual(ratio(1920, 1080), (16, 9))
        self.assertEqual(ratio(1001, 1000), (1001, 1000))

    def test_custom_size_hidden_with_its_action(self):
        host, module = self.make(settings={"hidden": ["create_custom"]})
        self.assertNotIn("actions.create_custom.size", [n.get("id") for n in flat(module.render())])


class ExportActions(QuickActionsCase):
    def test_export_open(self):
        self.host.premiere.eval_results["actions.export_open"] = {"name": "Spring", "sequence_id": "seq1"}
        self.assertEqual(self.click("export_open"), ("ok", "Export opened · Spring"))
        self.assertEqual(self.evals()[0][:2], ("actions.export_open", {"sequence_id": "seq1"}))
        self.assertEqual(self.menu_runs, [premiere_window.export_commands("Spring")])
        self.menu_ok = False
        self.assertEqual(self.click("export_open"), ("error", "Export failed"))
        self.assertEqual(self.detail, "Premiere's File > Export > Media command was not found or is unavailable.")

    def test_export_frame(self):
        frames = Path(self.host.project.root) / "Exports" / "Frames"
        out = str(frames / "Spring_00-00-12-05.png")
        self.host.premiere.eval_results["actions.export_frame"] = {"path": out, "timecode": "00:00:12:05"}
        self.assertEqual(self.click("export_frame"), ("ok", "Frame saved · Spring_00-00-12-05.png"))
        self.assertEqual(self.evals()[0][1], {"out_dir": str(frames), "base_name": "Spring"})
        self.host.premiere.eval_results["actions.export_frame"] = {}
        self.assertEqual(self.click("export_frame"), ("error", "Export frame failed"))
        self.assertEqual(self.detail, "Premiere wrote no frame file.")
        self.assertTrue(frames.is_dir())
        self.assertEqual(self.host.host.revealed, [out])

    def test_export_frame_sanitises_name(self):
        host, module = self.make(sequence=SequenceInfo("seq1", 'Cut: "final"?', 1920, 1080, 25.0, 60.0))
        host.premiere.eval_results["actions.export_frame"] = {"path": r"D:\x.png"}
        self.click("export_frame", module, host)
        self.assertEqual(self.evals(host)[0][1]["base_name"], "Cut_ _final__")


class EasingMath(unittest.TestCase):
    def test_bezier_matches_the_parametric_curve(self):
        for name, curve in easing.CURVES.items():
            x1, y1, x2, y2 = curve
            for s in (0.05, 0.2, 0.37, 0.5, 0.81, 0.97):
                x = 3 * x1 * s * (1 - s) ** 2 + 3 * x2 * s * s * (1 - s) + s ** 3
                y = 3 * y1 * s * (1 - s) ** 2 + 3 * y2 * s * s * (1 - s) + s ** 3
                with self.subTest(name, s=s):
                    self.assertAlmostEqual(easing.bezier_y(x, *curve), y, places=6)
            self.assertAlmostEqual(easing.bezier_y(0, *curve), 0.0)
            self.assertAlmostEqual(easing.bezier_y(1, *curve), 1.0)
            self.assertAlmostEqual(easing.bezier_y(0.5, *curve), 0.5, places=6)  # the presets are symmetric

    def test_strength_order(self):
        at = {name: easing.bezier_y(0.25, *curve) for name, curve in easing.CURVES.items()}
        self.assertGreater(at["slow"], at["med"])
        self.assertGreater(at["med"], at["fast"])
        self.assertLess(at["slow"], 0.25, "every preset starts slower than linear")

    def test_bake_one_key_per_frame(self):
        keys = easing.bake(TPS, 100.0, 3 * TPS, 150.0, FRAME, easing.CURVES["med"])
        self.assertEqual(len(keys), 49)
        self.assertEqual([t for t, _ in keys], [TPS + k * FRAME for k in range(1, 50)])
        values = [v for _, v in keys]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(dict(keys)[2 * TPS], 125.0)
        self.assertAlmostEqual(keys[0][1], 100 + 50 * easing.bezier_y(1 / 50, *easing.CURVES["med"]))
        # values measured live after MED on Premiere 26.5.2
        self.assertAlmostEqual(keys[0][1], 100.027, places=3)
        self.assertAlmostEqual(keys[-1][1], 149.973, places=3)

    def test_bake_lists_and_a_short_last_step(self):
        keys = easing.bake(0, [0.5, 0.5], 2 * TPS + FRAME // 4, [0.3, 0.7], FRAME, easing.CURVES["fast"])
        self.assertEqual(len(keys), 50)
        self.assertLess(2 * TPS + FRAME // 4 - keys[-1][0], FRAME)
        self.assertTrue(all(isinstance(v, list) and len(v) == 2 for _, v in keys))
        self.assertAlmostEqual(sum(keys[24][1]), 1.0)
        self.assertEqual(easing.bake(0, "a", TPS, "b", FRAME, easing.CURVES["med"]), [])
        self.assertEqual(easing.bake(TPS, 1.0, TPS, 2.0, FRAME, easing.CURVES["med"]), [])

    def test_mix(self):
        self.assertEqual(easing.mix(0, 10, 0.5), 5)
        self.assertEqual(easing.mix([0, 1], [2, 3], 0.5), [1, 2])
        self.assertIsNone(easing.mix([0, 1], [2], 0.5))
        self.assertIsNone(easing.mix(True, 1, 0.5))
        self.assertIsNone(easing.mix(None, 1, 0.5))

    def test_find_pair(self):
        times = [0, TPS, 3 * TPS, 5 * TPS]
        self.assertEqual(easing.find_pair(times, 2 * TPS, FRAME), (1, 2))
        self.assertEqual(easing.find_pair(times, TPS, FRAME), (1, 2), "on a key: the pair that starts there")
        self.assertIsNone(easing.find_pair(times, 5 * TPS, FRAME))
        self.assertIsNone(easing.find_pair(times, 6 * TPS, FRAME))
        self.assertIsNone(easing.find_pair([TPS], TPS, FRAME))

    def test_find_pair_widens_over_a_bake(self):
        baked = [t for t, _ in easing.bake(TPS, 0.0, 3 * TPS, 1.0, FRAME, easing.CURVES["med"])]
        times = [0, TPS] + baked + [3 * TPS, 5 * TPS]
        for at in (TPS, TPS + FRAME // 2, 2 * TPS, 3 * TPS - 1):
            with self.subTest(at=at):
                self.assertEqual(easing.find_pair(times, at, FRAME), (1, len(times) - 2))
        odd = [t for t, _ in easing.bake(0, 0.0, 2 * TPS + FRAME // 3, 1.0, FRAME, easing.CURVES["med"])]
        times = [0] + odd + [2 * TPS + FRAME // 3]
        self.assertEqual(easing.find_pair(times, times[-2] + 1, FRAME), (0, len(times) - 1), "from the short end")
        self.assertEqual(easing.find_pair(times, FRAME // 2, FRAME), (0, len(times) - 1), "from the start")

    def test_plan(self):
        times, values = [TPS, 3 * TPS], [100.0, 150.0]
        step = easing.plan(times, values, 2 * TPS, FRAME, "slow")
        self.assertEqual(step.remove, [])
        self.assertEqual(len(step.add), 49)
        self.assertEqual(step.linear, [TPS] + [t for t, _ in step.add] + [3 * TPS])
        baked = [TPS] + [t for t, _ in step.add] + [3 * TPS]
        relinear = easing.plan(baked, [0.0] * len(baked), 2 * TPS, FRAME, "linear")
        self.assertEqual(relinear.remove, [t for t, _ in step.add])
        self.assertEqual((relinear.add, relinear.linear), ([], [TPS, 3 * TPS]))
        again = easing.plan(baked, [100.0] + [0.0] * 49 + [150.0], 2 * TPS, FRAME, "fast")
        self.assertEqual(again.remove, [t for t, _ in step.add], "a new preset replaces the old bake")
        self.assertIsNone(easing.plan(times, values, 4 * TPS, FRAME, "med"))
        self.assertIsNone(easing.plan(times, ["a", "b"], 2 * TPS, FRAME, "med"))


class EaseActions(QuickActionsCase):
    def read_result(self, at=2 * TPS):
        return {"clip": "a.mp4", "kind": "video", "track": 0, "start": str(12 * TPS), "frame": str(FRAME),
                "at": str(at), "inside": True, "skipped": [],
                "props": [{"c": 1, "p": 1, "name": "Motion · Scale", "keys": [str(TPS), str(3 * TPS)],
                           "values": [100, 150]},
                          {"c": 1, "p": 0, "name": "Motion · Position", "keys": [str(TPS), str(3 * TPS)],
                           "values": [[0.5, 0.5], [0.3, 0.7]]}]}

    def apply_echo(self, args):
        return {"removed": sum(len(p["remove"]) for p in args["props"]),
                "added": sum(len(p["add"]) for p in args["props"]), "props": len(args["props"])}

    def test_ease_bakes_every_animated_property(self):
        results = self.host.premiere.eval_results
        results["actions.ease_read"] = self.read_result()
        results["actions.ease_apply"] = self.apply_echo
        self.assertEqual(self.click("ease_med"), ("ok", "Eased 2 properties · 98 keys"))
        self.assertEqual(self.detail, "Motion · Scale, Motion · Position")
        (_, read, timeout), (_, apply, _) = self.evals()
        self.assertIsNone(read)
        self.assertEqual(timeout, 60000)
        self.assertEqual((apply["kind"], apply["track"], apply["start"]), ("video", 0, str(12 * TPS)))
        scale = apply["props"][0]
        self.assertEqual((scale["c"], scale["p"], scale["remove"]), (1, 1, []))
        self.assertEqual(len(scale["add"]), 49)
        self.assertEqual(scale["add"][24], {"t": str(2 * TPS), "v": 125.0})
        self.assertEqual(scale["linear"][0], str(TPS))
        self.assertEqual(scale["linear"][-1], str(3 * TPS))
        self.assertEqual(apply["props"][1]["add"][24]["v"], [0.4, 0.6])

    def test_presets_use_their_curves(self):
        for action_id, preset in (("ease_slow", "slow"), ("ease_med", "med"), ("ease_fast", "fast")):
            with self.subTest(action_id):
                host, module = self.make()
                host.premiere.eval_results["actions.ease_read"] = self.read_result()
                host.premiere.eval_results["actions.ease_apply"] = self.apply_echo
                self.click(action_id, module, host)
                first = self.evals(host, "actions.ease_apply")[0][1]["props"][0]["add"][0]["v"]
                self.assertAlmostEqual(first, 100 + 50 * easing.bezier_y(1 / 50, *easing.CURVES[preset]))

    def test_linear_removes_the_bake(self):
        read = self.read_result()
        baked = [TPS + k * FRAME for k in range(0, 51)]
        read["props"] = [{"c": 1, "p": 1, "name": "Motion · Scale", "keys": [str(t) for t in baked],
                          "values": [100.0] * 51}]
        self.host.premiere.eval_results["actions.ease_read"] = read
        self.host.premiere.eval_results["actions.ease_apply"] = self.apply_echo
        self.assertEqual(self.click("ease_linear"), ("ok", "Linear · 1 property · 49 keys removed"))
        plan = self.evals(name="actions.ease_apply")[0][1]["props"][0]
        self.assertEqual((plan["add"], plan["linear"]), ([], [str(TPS), str(3 * TPS)]))
        self.host.premiere.eval_results["actions.ease_read"] = self.read_result()
        self.assertEqual(self.click("ease_linear"), ("ok", "Linear · 2 properties"))

    def test_no_keys_and_no_pair(self):
        results = self.host.premiere.eval_results
        results["actions.ease_read"] = dict(self.read_result(), props=[])
        self.assertEqual(self.click("ease_fast"), ("warn", "The selected clip has no keyframes"))
        results["actions.ease_read"] = self.read_result(at=4 * TPS)
        self.assertEqual(self.click("ease_fast"), ("warn", NO_PAIR))
        results["actions.ease_read"] = dict(self.read_result(), inside=False)
        self.assertEqual(self.click("ease_fast"), ("warn", NO_PAIR))
        self.assertEqual(self.evals(name="actions.ease_apply"), [])

    def test_odd_property_is_skipped(self):
        read = self.read_result()
        read["props"].append({"c": 2, "p": 0, "name": "Odd", "keys": [str(TPS)], "values": [1, 2]})
        self.host.premiere.eval_results["actions.ease_read"] = read
        self.host.premiere.eval_results["actions.ease_apply"] = self.apply_echo
        self.assertEqual(self.click("ease_slow")[1], "Eased 2 properties · 98 keys")


class JobsAndRendering(QuickActionsCase):
    def test_long_action_is_busy_until_done(self):
        host, module = self.make(manual_jobs=True)
        host.premiere.eval_results["actions.export_frame"] = {"path": r"D:\f.png"}
        result = module.on_action("actions.export_frame")
        self.assertIsNone(result.toast)
        self.assertEqual(host.ui.results, [], "the result comes when the job ends")
        button = rendered(module, "actions.export_frame")
        self.assertTrue(button["busy"])
        self.assertEqual(module.section_status(), {"text": "Running · Export frame", "tone": "accent"})
        self.assertFalse(module.on_action("actions.export_frame").rerender)  # a second press is ignored
        self.assertEqual(len(host.jobs.jobs), 1)
        host.jobs.step()
        self.assertEqual(host.ui.results, [("export_frame", "ok", "Frame saved · f.png", None)])
        self.assertEqual(host.ui.toasts, [])
        self.assertGreaterEqual(host.ui.invalidations, 1)
        button = rendered(module, "actions.export_frame")
        self.assertFalse(button["busy"])
        self.assertIsNone(module.section_status())

    def test_custom_input_is_disabled_while_its_job_runs(self):
        host, module = self.make(manual_jobs=True)
        module.on_action("actions.create_custom")
        self.assertTrue(rendered(module, "actions.create_custom.size")["disabled"])
        host.jobs.step()
        self.assertFalse(rendered(module, "actions.create_custom.size").get("disabled", False))

    def test_long_actions_are_jobs(self):
        long_ids = sorted(aid for aid, action in self.module.actions.items() if action.long)
        self.assertEqual(long_ids, sorted(("export_frame", "label_by_bin") + REFRAMES + EASES))

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
        self.assertEqual(section["icon"], "actions")
        items = section["items"]
        self.assertTrue(all(n["kind"] == "group" for n in items), "no loose headings, dividers or rows")
        self.assertEqual([n["title"] for n in items], list(GROUPS))
        self.assertEqual([n["id"] for n in items], [f"actions.{t.lower()}" for t in GROUPS])
        self.assertTrue(all(n["tiles"] and n["collapsible"] for n in items))
        for group in items:
            tiles = [n for n in group["items"] if n["kind"] != "input"]
            expected = [f"actions.{aid}" for aid in ALL_IDS if TILES[aid][0] == group["title"]]
            self.assertEqual([n["id"] for n in tiles], expected, group["title"])
            kinds = {n["id"]: n["kind"] for n in tiles}
            for node_id, kind in kinds.items():
                self.assertEqual(kind, "switch" if node_id in ("actions.solo_track", "actions.lock_others")
                                 else "button", node_id)
            inputs = [n["id"] for n in group["items"] if n["kind"] == "input"]
            self.assertEqual(inputs, ["actions.create_custom.size"] if group["title"] == "Sequence" else [])
            kinds_in_order = [n["kind"] == "input" for n in group["items"]]
            self.assertEqual(kinds_in_order, sorted(kinds_in_order), "inputs follow the tiles")
        all_ids = [n["id"] for n in flat(items) if n.get("id")]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_every_action_declares_icon_and_code(self):
        for action_id, (group, icon, short) in TILES.items():
            with self.subTest(action_id):
                node = rendered(self.module, f"actions.{action_id}")
                self.assertEqual((node["icon"], node["short"]), (icon, short))
                self.assertLessEqual(len(node["short"]), P.SHORT_MAX)
                self.assertTrue(node["label"] and node["hint"])
                self.assertEqual(self.module.actions[action_id].group, group)
        self.assertEqual(list(self.module.actions), list(ALL_IDS), "panel order is the table order")

    def test_menus(self):
        self.assertEqual([n["id"] for n in flat(self.module.render()) if n.get("menu")], ["actions.colour"])
        self.assertEqual([n["id"] for n in flat(self.module.render()) if n.get("kind") == "select"], [])

    def test_removed_actions_are_gone(self):
        for gone in ("social_copy", "export_sequence", "remove_gaps", "nest_selection"):
            with self.subTest(gone):
                self.assertNotIn(gone, self.module.actions)
                self.assertFalse((JSX_DIR / f"{gone}.jsx").exists())
                self.assertEqual(self.module.on_action(f"actions.{gone}").toast[0], "warn")  # an unknown id
        package = JSX_DIR.parent
        self.assertFalse((package / "presets.py").exists())
        self.assertFalse((package / "presets").exists())
        with self.assertRaises(ImportError):
            importlib.import_module("premiere_deck.modules.quick_actions.presets")
        self.assertEqual(self.module.on_action("actions.host:open_presets").toast[0], "warn")
        self.assertFalse((self.home / "presets").exists())
        self.assertEqual(self.host.premiere.calls_named("eval"), [])

    def test_render_with_hidden_actions(self):
        host, module = self.make(settings={"hidden": ["select_same_source", "select_offline", "export_frame"]})
        section = module.section()
        titles = [n["title"] for n in section["items"] if n["kind"] == "group"]
        self.assertNotIn("Selection", titles, "a group without visible actions is not rendered")
        self.assertEqual(titles, [g for g in GROUPS if g != "Selection"])
        ids = [n.get("id") for n in flat(section["items"])]
        self.assertNotIn("actions.export_frame", ids)
        self.assertIn("actions.export_open", ids)
        module.on_action("actions.host:visible.export_frame", True)
        module.on_action("actions.host:visible.razor_all", False)
        self.assertEqual(host.settings.get("hidden"), ["razor_all", "select_same_source", "select_offline"])

    def test_all_hidden(self):
        host, module = self.make(settings={"hidden": list(ALL_IDS)})
        nodes = module.section()["items"]
        self.assertEqual(len(nodes), 1)
        self.assertIn("hidden", nodes[0]["text"])

    def test_settings_fallback_on_broken_values(self):
        for hidden, choices in (("oops", "bad"), ([1, None, "razor_all", "gone"], {"colour.label": 5}),
                                ({"x": 1}, {"colour.label": "Pink", "create_custom.size": "huge"}), (None, None)):
            with self.subTest(hidden=hidden, choices=choices):
                host, module = self.make(settings={"hidden": hidden, "choices": choices})
                module.section()
                expected = {"razor_all"} if isinstance(hidden, list) else set()
                self.assertEqual(module.hidden_ids(), expected)
                self.assertEqual(module.choice(module.actions["colour"], "label"), "Cerulean")
                self.assertEqual(module.actions["create_custom"].size(module), (1080, 1350))

    def test_the_module_renders_no_result_line(self):
        self.host.premiere.eval_results["actions.select_offline"] = {"count": 0}
        self.assertEqual(self.click("select_offline"), ("neutral", "No offline clips"))
        self.assertTrue(all(n["kind"] == "group" for n in self.module.render()), "the panel keeps the result line")

    def test_render_host_page(self):
        page = self.module.render_host()
        self.assertEqual([block["id"] for block in page["blocks"]], ["visibility"])
        visibility = page["blocks"][0]
        self.assertEqual([row["id"] for row in visibility["rows"]], list(ALL_IDS))
        self.assertEqual([row["icon"] for row in visibility["rows"]], [TILES[aid][1] for aid in ALL_IDS])
        self.assertTrue(all(len(row["title"]) <= 16 for row in visibility["rows"]), "titles fit the column")
        titles = {row["id"]: row["title"] for row in visibility["rows"]}
        self.assertEqual((titles["remove_video_track"], titles["add_audio_track"], titles["create_custom"],
                          titles["ease_slow"]), ("Remove V track", "Add A track", "Reframe custom", "Ease slow"))
        self.assertTrue(all(row["toggle"]["value"] for row in visibility["rows"]))
        self.assertEqual(visibility["rows"][0]["toggle"]["id"], "actions.host:visible.adjustment_layer")
        self.assertEqual(visibility["rows"][-1]["subtitle"].split(" · ")[0], "Keys")

    def test_actions_come_before_files(self):
        from premiere_deck.modules.files.module import FilesModule
        self.assertLess(QuickActionsModule.order, FilesModule.order)
        self.assertEqual(QuickActionsModule.order, 5)

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
                self.assertTrue(action.icon and action.short)
                self.assertLessEqual(len(action.short), P.SHORT_MAX)
                self.assertIn(action.group, GROUPS)

    def test_helpers(self):
        self.assertEqual(timecode(3725.9), "01:02:05")
        self.assertEqual(safe_file_stem("a/b. "), "a_b")
        self.assertEqual(safe_file_stem("..."), "Sequence")


class Scripts(QuickActionsCase):
    FORBIDDEN = (r"\blet\s", r"\bconst\s", r"=>", r"`", r"\bclass\s", r"\bJSON\.", r"\balert\(", r"\bconfirm\(",
                 r"\bprompt\(", r"\$\.writeln", r"\.forEach\(", r"\.map\(", r"\.filter\(", r"\bfunction\s+\w+\s*\(")

    def scripts(self):
        return sorted(JSX_DIR.glob("*.jsx"))

    def test_every_script_is_used(self):
        names = {path.stem for path in self.scripts()}
        self.assertEqual(names - {"_common"}, SCRIPTS)
        self.assertEqual(set(SCRIPT_OF.values()) | {"ease_apply"}, SCRIPTS)

    def test_scripts_are_es3(self):
        for path in self.scripts():
            code = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
            for pattern in self.FORBIDDEN:
                with self.subTest(path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))

    def test_scripts_return_values(self):
        for name in SCRIPTS:
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

    def test_interpolation_and_marker_calls(self):
        apply = (JSX_DIR / "ease_apply.jsx").read_text(encoding="utf-8")
        self.assertIn("setInterpolationTypeAtKey(at(plan.linear[l]), 0,", apply)
        colour = (JSX_DIR / "colour.jsx").read_text(encoding="utf-8")
        self.assertIn("marker.setColorByIndex(__args.marker)", colour)
        self.assertIn("app.getCurrentProjectViewSelection()", colour)

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
