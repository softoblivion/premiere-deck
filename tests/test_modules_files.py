"""Files module with FakeHost: render states, intercept, bin mirror, collect, pack, undo, suppression, settings."""

import json
import os
import time
import unittest

from premiere_deck.host import protocol as P
from premiere_deck.modules.base import ActionResult
from premiere_deck.modules.files import journal as J
from premiere_deck.modules.files.layout import norm
from premiere_deck.modules.files.module import FilesModule
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable, ProjectItem
from tests.test_modules_files_support import FilesCase, info, write_file

BLOCK_TYPES = {"cards", "table", "list", "form", "actions", "text", "progress"}


def check_block(test, block):
    """The block shapes of docs/BRIDGE_API.md 5."""
    kind = block["type"]
    test.assertIn(kind, BLOCK_TYPES)
    test.assertIsInstance(block.get("id"), str)

    def check_button(node):
        test.assertEqual(node["kind"], "button")
        test.assertTrue(node["id"].startswith("files."), node["id"])

    if kind in ("cards", "actions"):
        test.assertIsInstance(block["items"], list)
        for item in block["items"]:
            if kind == "actions":
                check_button(item)
            else:
                test.assertTrue({"id", "title", "value", "detail", "tone"} <= set(item))
                for action in item["actions"]:
                    check_button(action)
    elif kind == "table":
        keys = [c["key"] for c in block["columns"]]
        for row in block["rows"]:
            test.assertIsInstance(row["id"], str)
            test.assertNotIn("cells", row)
            for key in keys:
                test.assertIn(key, row)
            for action in row.get("actions", []):
                check_button(action)
        for control in block.get("filters", []):
            test.assertEqual(control["kind"], "select")
        if block.get("search"):
            test.assertEqual(block["search"]["kind"], "input")
    elif kind == "list":
        for row in block["rows"]:
            test.assertTrue({"id", "title", "subtitle"} <= set(row))
    elif kind == "form":
        for field in block["fields"]:
            if "kind" in field and field["kind"] in ("input", "toggle", "select") and "id" in field:
                test.assertTrue(field["id"].startswith("files."))
            else:
                test.assertTrue({"key", "label", "kind", "value"} <= set(field), field)
                test.assertIn(field["kind"], ("bool", "int", "text", "select", "path"))
        if block.get("submit"):
            check_button(block["submit"])


def node_ids(nodes):
    out = []
    for node in nodes:
        if "id" in node:
            out.append(node["id"])
        if node.get("kind") == "row":
            out.extend(node_ids(node["items"]))
    return out


def texts(nodes):
    out = []
    for node in nodes:
        if node.get("kind") == "text":
            out.append(node["text"])
        if node.get("kind") == "row":
            out.extend(texts(node["items"]))
        if node.get("kind") == "button":
            out.append(node["label"])
    return out


def find(nodes, node_id):
    for node in nodes:
        if node.get("id") == node_id:
            return node
        if node.get("kind") == "row":
            hit = find(node["items"], node_id)
            if hit:
                return hit
    return None


class Base(FilesCase):
    def check_render(self):
        sec = self.module.section()  # validates the tree against the protocol
        P.validate_tree({"type": "ui", "revision": 1, "title": "", "status": None, "sections": [sec], "footer": None})
        page = self.module.render_host()
        self.assertIsInstance(page, dict)
        self.assertTrue(page["blocks"])
        for block in page["blocks"]:
            check_block(self, block)
        json.dumps(page)
        return sec


class RenderStates(Base):
    def test_no_project(self):
        module = FilesModule(self.host.context())
        self.module = module
        module.start()
        sec = self.check_render()
        self.assertEqual(sec["status"]["text"], "No project")
        self.assertIn("Open a project", sec["items"][0]["text"])
        self.assertEqual(module.render_host()["status"]["text"], "No project open")
        result = module.on_action("files.collect")
        self.assertFalse(result.rerender)

    def test_not_set_up_observes_only(self):
        self.make_module()
        sec = self.check_render()
        self.assertEqual(sec["status"], {"text": "Set up this project", "tone": "warn"})
        self.assertEqual(node_ids(sec["items"]), ["files.setup"])
        clip = self.ext("clip.mp4")
        self.user_import(clip)
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertFalse(os.path.exists(self.in_root("Footage", "clip.mp4")))
        self.assertIn("1 items are outside the project folder.", texts(self.module.render()))
        for action in ("files.collect", "files.pack", "files.undo_last", "files.watch"):
            self.assertEqual(self.module.on_action(action).toast[1], "Set up this project first")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".premiere-deck", "journal.jsonl")))

    def test_setup_creates_folders_bins_and_journal(self):
        self.make_module(enabled=True, watch=False)
        for folder in ("Footage", "Audio/Music", "Audio/SFX", "Audio/Voice", "Graphics", "Overlays", "Motion",
                       "Exports/Frames", "Project"):
            self.assertTrue(os.path.isdir(self.in_root(*folder.split("/"))), folder)
        bins = [args[0] for args, _ in self.pr.calls_named("create_bin_path")]
        self.assertIn("Audio/SFX", bins)
        self.assertNotIn("Project", bins)
        self.assertTrue(self.host.settings.project.values["enabled"])
        self.assertEqual(self.host.settings.project.values["layout"], "default")
        self.assertEqual(self.journal()[-1]["op"], "setup")
        sec = self.check_render()
        self.assertEqual(sec["status"]["text"], "Paused")
        self.assertEqual(self.module.on_action("files.setup").toast[1], "This project is already set up")

    def test_watching_status(self):
        self.make_module(enabled=True)
        try:
            sec = self.check_render()
            self.assertEqual(sec["status"], {"text": "Watching · Sample", "tone": "ok"})
            watch = find(sec["items"], "files.watch")
            self.assertTrue(watch["value"])
        finally:
            self.module.on_action("files.watch", False)
        self.assertEqual(self.module.section()["status"]["text"], "Paused")

    def test_enabled_render_has_every_control(self):
        self.make_module(enabled=True, watch=False)
        sec = self.check_render()
        ids = node_ids(sec["items"])
        for node_id in ("files.collect", "files.pack", "files.undo_last", "files.watch", "files.recent"):
            self.assertIn(node_id, ids)
        self.assertTrue(find(sec["items"], "files.undo_last")["disabled"])  # setup itself is not undoable
        self.assertEqual(find(sec["items"], "files.collect")["label"], "Collect project")

    def test_render_offline_layout_error_and_premiere_error(self):
        os.makedirs(os.path.join(self.root, ".premiere-deck"))
        with open(os.path.join(self.root, ".premiere-deck", "layout.json"), "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        gone = ProjectItem("g1", "gone.mp4", "clip", tree_path="Footage", media_path=self.in_root("Footage", "gone.mp4"),
                           is_offline=True)
        self.pr.set_items([gone])
        self.make_module(enabled=True, watch=False, layout="custom")
        sec = self.check_render()
        all_text = texts(sec["items"])
        self.assertTrue(any("layout.json could not be read" in t for t in all_text))
        offline = find(sec["items"], "files.offline")
        self.assertEqual(offline["rows"][0]["id"], "g1")
        self.assertEqual([a["id"] for a in offline["rows"][0]["actions"]], ["files.locate", "files.remove"])
        self.assertTrue(offline["rows"][0]["actions"][1]["confirm"])
        self.pr.connected = False
        self.fire("project.changed")
        self.assertTrue(any("did not answer" in t for t in texts(self.module.render())))

    def test_render_while_a_job_runs_and_with_a_plan(self):
        self.host.jobs.manual = True
        self.make_module(enabled=True, watch=False)
        self.user_import(self.ext("a.mp4"))
        sec = self.check_render()
        self.assertIn("files.cancel", node_ids(sec["items"]))
        self.assertEqual(sec["status"]["tone"], "accent")
        self.assertEqual(self.host.jobs.jobs[-1].priority, "high")
        self.host.jobs.step()
        self.assertNotIn("files.cancel", node_ids(self.module.section()["items"]))

    def test_settings_schema(self):
        self.make_module()
        fields = {f.key: f for f in self.module.settings_schema()}
        self.assertEqual({k for k, f in fields.items() if f.scope == "host"},
                         {"toast_filed", "copy_buffer_mb", "stable_seconds", "default_layout"})
        self.assertEqual({k for k, f in fields.items() if f.scope == "project"},
                         {"enforce_bins", "mirror_bins", "import_exports", "watch", "pack_zip"})
        self.assertEqual(fields["default_layout"].options[0]["value"], "default")


class Intercept(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.host.ui.toasts.clear()

    def test_external_import_is_copied_relinked_and_binned(self):
        src = self.ext("A001.mp4", 50_000)
        self.host.tools.infos[src] = info(src, "video", duration=3.0)
        item = self.user_import(src)
        target = self.in_root("Footage", "A001.mp4")
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.path.isfile(src))  # original untouched
        self.assertEqual(self.item(item.node_id).media_path, target)
        self.assertEqual(self.item(item.node_id).tree_path, "Footage")
        entry = self.journal()[-1]
        self.assertEqual((entry["op"], entry["status"], entry["copied"]), ("filed", "done", True))
        self.assertEqual(entry["from"], src)
        self.assertEqual(entry["bin_from"], "")
        self.assertTrue(entry["hash"])
        self.assertEqual(self.toasts(), ["Filed A001.mp4 into Footage"])
        name, data = self.host.events.emitted[-1]
        self.assertEqual((name, data["op"], data["text"]), ("journal.appended", "filed", "Filed A001.mp4 into Footage"))
        self.assertEqual(self.host.jobs.jobs[-1].priority, "high")
        # the module's own bin move does not trigger the mirror
        self.fire("project.changed")
        self.assertEqual(len(self.pr.calls_named("relink")), 1)

    def test_alpha_video_goes_to_overlays_and_toasts_merge(self):
        files = [self.ext(f"f{i}.mov", 1000 + i) for i in range(3)]
        self.host.tools.infos[files[0]] = info(files[0], "video", alpha=True)
        for path in files:
            self.user_import(path)
        self.assertTrue(os.path.isfile(self.in_root("Overlays", "f0.mov")))
        self.assertEqual(self.toasts(), ["Filed f0.mov into Overlays", "Filed 2 files", "Filed 3 files"])

    def test_toast_setting_off(self):
        self.host.settings.values["toast_filed"] = False
        self.user_import(self.ext("quiet.mp4"))
        self.assertEqual(self.toasts(), [])

    def test_collision_equal_reuses_the_existing_file(self):
        src = self.ext("same.mp4", 30_000, "same")
        write_file(self.in_root("Footage", "same.mp4"), 30_000, "same")
        item = self.user_import(src)
        self.assertEqual(sorted(os.listdir(self.in_root("Footage"))), ["same.mp4"])
        self.assertEqual(self.item(item.node_id).media_path, self.in_root("Footage", "same.mp4"))
        self.assertFalse(self.journal()[-1]["copied"])

    def test_collision_different_gets_a_suffix(self):
        src = self.ext("take.mp4", 30_000, "new")
        write_file(self.in_root("Footage", "take.mp4"), 30_000, "old")
        item = self.user_import(src)
        self.assertEqual(self.item(item.node_id).media_path, self.in_root("Footage", "take (2).mp4"))
        with open(self.in_root("Footage", "take.mp4"), "rb") as handle:
            self.assertTrue(handle.read(3) == b"old")

    def test_relink_failure_rolls_back(self):
        self.pr.relink_result = False
        src = self.ext("bad.mp4")
        item = self.user_import(src)
        self.assertFalse(os.path.exists(self.in_root("Footage", "bad.mp4")))
        self.assertTrue(os.path.isfile(src))
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertEqual(self.item(item.node_id).tree_path, "")
        self.assertEqual(self.journal()[-1]["status"], "failed")
        self.assertIn("Could not relink bad.mp4", self.toasts())

    def test_relink_failure_keeps_a_reused_file(self):
        self.pr.relink_result = False
        src = self.ext("keep.mp4", 10_000, "k")
        existing = write_file(self.in_root("Footage", "keep.mp4"), 10_000, "k")
        self.user_import(src)
        self.assertTrue(os.path.isfile(existing))

    def test_loose_file_in_root_is_filed(self):
        loose = write_file(self.in_root("music.mp3"), 2000)
        self.host.tools.infos[loose] = info(loose, "audio", duration=120.0)
        item = self.user_import(loose)
        self.assertEqual(self.item(item.node_id).media_path, self.in_root("Audio", "Music", "music.mp3"))
        self.assertTrue(os.path.isfile(loose))

    def test_file_in_layout_in_wrong_bin_is_moved_without_disk_change(self):
        path = write_file(self.in_root("Graphics", "logo.png"))
        item = self.user_import(path, "Misc")
        self.assertEqual(self.item(item.node_id).tree_path, "Graphics")
        self.assertEqual(self.item(item.node_id).media_path, path)
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertEqual(self.journal()[-1]["op"], "binned")
        self.assertEqual(sorted(os.listdir(self.in_root("Graphics"))), ["logo.png"])

    def test_enforce_bins_off(self):
        self.host.settings.project.values["enforce_bins"] = False
        path = write_file(self.in_root("Graphics", "logo.png"))
        item = self.user_import(path, "Misc")
        self.assertEqual(self.item(item.node_id).tree_path, "Misc")

    def test_exclusions_offline_and_sequences_are_ignored(self):
        autosave = write_file(self.in_root("Adobe Premiere Pro Auto-Save", "x.mp4"))
        temp = self.ext("download.mp4.crdownload")
        self.user_import(autosave)
        self.user_import(temp)
        self.user_import(os.path.join(self.downloads, "missing.mp4"), offline=True)
        seq = ProjectItem("s1", "Sequence 01", "sequence", is_sequence=True)
        self.pr.set_items(list(self.pr.snapshot_value.items) + [seq])
        self.fire("project.items_added")
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertEqual(self.pr.calls_named("move_to_bin"), [])

    def test_probe_failure_classifies_by_extension(self):
        src = self.ext("song.wav")

        def broken(path):
            raise RuntimeError("ffprobe crashed")

        self.host.tools.ffprobe = broken
        item = self.user_import(src)
        self.assertEqual(self.item(item.node_id).tree_path, "Audio/Music")

    def test_failed_first_snapshot_does_not_file_everything(self):
        self.module.project_closed()
        external = [self.user_import(self.ext(f"old{i}.mp4"), fire=False) for i in range(3)]
        self.pr.connected = False
        self.module.project_opened(self.project)
        self.pr.connected = True
        self.fire("project.changed")
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertEqual(len(self.module._state.external), len(external))

    def test_copy_failure_is_reported(self):
        from unittest import mock

        src = self.ext("locked.mp4")
        with mock.patch("premiere_deck.modules.files.module.copy_file", side_effect=PermissionError("locked")):
            item = self.user_import(src)
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertEqual(self.toasts(), ["Could not copy locked.mp4"])
        self.assertEqual(self.journal()[-1]["status"], "failed")
        self.assertFalse(os.path.exists(self.in_root("Footage", "locked.mp4")))


class BinMirror(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_user_file_moved_to_another_layout_bin_is_copied_and_kept(self):
        path = write_file(self.in_root("Footage", "grain.mov"), 5000)
        item = ProjectItem("b1", "grain.mov", "clip", tree_path="Footage", media_path=path)
        self.pr.set_items(list(self.pr.snapshot_value.items) + [item])
        self.fire("project.changed")
        self.user_move_bin("b1", "Overlays")
        target = self.in_root("Overlays", "grain.mov")
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.path.isfile(path))  # the user's file is never deleted
        self.assertEqual(self.item("b1").media_path, target)
        entry = self.journal()[-1]
        self.assertEqual((entry["op"], entry["status"], entry["deleted_from"]), ("moved_bin", "done", False))

    def test_module_copy_moved_to_another_bin_removes_the_old_copy(self):
        src = self.ext("clip.mp4", 8000)
        item = self.user_import(src)
        first = self.in_root("Footage", "clip.mp4")
        self.assertTrue(os.path.isfile(first))
        self.user_move_bin(item.node_id, "Footage/Day1")
        moved = self.in_root("Footage", "Day1", "clip.mp4")
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.isfile(src))
        self.assertTrue(self.journal()[-1]["deleted_from"])

    def test_old_copy_referenced_by_another_item_stays(self):
        src = self.ext("shared.mp4", 8000)
        first = self.user_import(src)
        second = self.user_import(src)  # same source again: reuses the copy
        copy = self.in_root("Footage", "shared.mp4")
        self.assertEqual(self.item(second.node_id).media_path, copy)
        self.user_move_bin(first.node_id, "Overlays")
        self.assertTrue(os.path.isfile(copy))

    def test_bin_without_folder_and_mirror_off(self):
        path = write_file(self.in_root("Footage", "keep.mp4"))
        self.pr.set_items([ProjectItem("k1", "keep.mp4", "clip", tree_path="Footage", media_path=path)])
        self.fire("project.changed")
        self.user_move_bin("k1", "Selects")
        self.assertFalse(os.path.exists(self.in_root("Selects")))
        self.assertEqual(self.item("k1").media_path, path)
        self.host.settings.project.values["mirror_bins"] = False
        self.user_move_bin("k1", "Overlays")
        self.assertFalse(os.path.exists(self.in_root("Overlays", "keep.mp4")))

    def test_external_item_moved_between_bins_is_left_alone(self):
        self.host.settings.project.values["enforce_bins"] = True
        src = self.ext("ext.mp4")
        item = ProjectItem("e1", "ext.mp4", "clip", tree_path="", media_path=src)
        self.pr.set_items(list(self.pr.snapshot_value.items) + [item])
        self.module._take_snapshot(self.module._state)  # already known: not a new import
        self.user_move_bin("e1", "Overlays")
        self.assertEqual(self.item("e1").media_path, src)


class Collect(Base):
    manual_jobs = True

    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.host.jobs.step()
        self.sources = [self.ext("a.mp4", 1000), self.ext("b.wav", 2000), write_file(self.in_root("loose.png"), 300)]
        self.host.tools.infos[self.sources[1]] = info(self.sources[1], "audio", duration=60.0)
        items = [ProjectItem(f"c{i}", os.path.basename(p), "clip", media_path=p) for i, p in enumerate(self.sources)]
        items.append(ProjectItem("off", "gone.mp4", "clip", media_path=os.path.join(self.downloads, "gone.mp4"), is_offline=True))
        self.pr.set_items(items)
        self.module._take_snapshot(self.module._state)

    def test_plan_then_run(self):
        sec = self.module.section()
        self.assertEqual(find(sec["items"], "files.collect")["label"], "Collect project · 3 external")
        self.assertIsNone(self.module.on_action("files.collect"))
        self.assertTrue(find(self.module.section()["items"], "files.collect")["busy"])
        self.host.jobs.step()
        plan = self.module._state.plan
        self.assertEqual([e.folder for e in plan.entries], ["Footage", "Audio/Music", "Graphics"])
        self.assertEqual(plan.total_bytes, 3300)
        self.assertEqual([m["node_id"] for m in plan.missing], ["off"])
        sec = self.check_render()
        self.assertIn("3 files · 3.2 KB · Run", texts(sec["items"]))
        run = find(sec["items"], "files.collect_run")
        self.assertIn("Copy 3 files", run["confirm"])
        page = self.module.render_host()
        totals = next(b for b in page["blocks"] if b["id"] == "collect_totals")
        self.assertEqual([r["folder"] for r in totals["rows"]], ["Audio/Music", "Footage", "Graphics"])
        self.assertIsNone(self.module.on_action("files.collect_run"))
        self.host.jobs.step()
        self.assertIsNone(self.module._state.plan)
        self.assertEqual(self.item("c0").media_path, self.in_root("Footage", "a.mp4"))
        self.assertEqual(self.item("c1").tree_path, "Audio/Music")
        self.assertEqual(self.item("c2").media_path, self.in_root("Graphics", "loose.png"))
        self.assertTrue(all(os.path.isfile(p) for p in self.sources))
        self.assertEqual([e["op"] for e in self.journal()[-3:]], ["collected"] * 3)
        self.assertIn("Collected 3 files", self.toasts())
        self.assertEqual(self.module._state.external, [])

    def test_cancel_keeps_finished_items(self):
        self.module.on_action("files.collect")
        self.host.jobs.step()
        original = self.module._copy_one

        def copy_then_cancel(state, job, task, op, on_bytes):
            result = original(state, job, task, op, on_bytes)
            job.cancel()
            return result

        self.module._copy_one = copy_then_cancel
        self.module.on_action("files.collect_run")
        self.host.jobs.step()
        self.assertEqual(self.item("c0").media_path, self.in_root("Footage", "a.mp4"))
        self.assertEqual(self.item("c1").media_path, self.sources[1])
        self.assertIn("Collect stopped · 1 of 3 filed", self.toasts())
        self.assertEqual(len(self.module._state.external), 2)

    def test_cancel_action_and_discard(self):
        self.module.on_action("files.collect")
        self.assertEqual(self.module.on_action("files.cancel").toast[1], "Stopping")
        self.host.jobs.step()
        self.assertIsNone(self.module._state.plan)
        self.module.on_action("files.collect")
        self.host.jobs.step()
        self.assertIsNotNone(self.module._state.plan)
        self.module.on_action("files.collect_discard")
        self.assertIsNone(self.module._state.plan)
        self.assertEqual(self.module.on_action("files.cancel").toast, None)

    def test_run_without_plan_plans_first(self):
        self.module.on_action("files.collect_run")
        self.host.jobs.step()
        self.host.jobs.step()
        self.assertEqual(self.item("c0").media_path, self.in_root("Footage", "a.mp4"))

    def test_busy_guard(self):
        self.module.on_action("files.collect")
        self.assertEqual(self.module.on_action("files.collect").toast[1], "Collect is already running")
        self.assertEqual(self.module.on_action("files.pack").toast[1], "Collect or pack is already running")
        self.host.jobs.step()


class PackProject(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.inside = write_file(self.in_root("Footage", "in.mp4"), 1000)
        self.external = self.ext("out.mp4", 1000)
        self.pr.set_items([
            ProjectItem("p1", "in.mp4", "clip", tree_path="Footage", media_path=self.inside),
            ProjectItem("p2", "out.mp4", "clip", media_path=self.external),
            ProjectItem("p3", "gone.wav", "clip", media_path=self.in_root("Audio", "gone.wav"), is_offline=True),
        ])
        self.module._take_snapshot(self.module._state)
        self.host.ui.toasts.clear()

    def test_pack_collects_reports_offline_and_zips(self):
        self.host.settings.project.values["pack_zip"] = True
        self.assertIn("Copy 1 file from outside", find(self.module.render(), "files.pack")["confirm"])
        self.assertIsNone(self.module.on_action("files.pack"))
        self.assertEqual(self.item("p2").media_path, self.in_root("Footage", "out.mp4"))
        report_path = os.path.join(self.root, ".premiere-deck", "pack-report.txt")
        with open(report_path, encoding="utf-8") as handle:
            report = handle.read()
        self.assertIn("Offline: 1", report)
        self.assertIn("gone.wav", report)
        self.assertIn("Outside the project folder: 0", report)
        zip_path = self.root + ".zip"
        self.assertIn(f"Archive: {zip_path}", report)
        self.assertTrue(os.path.isfile(zip_path))
        line = self.module._state.pack_report.line()
        self.assertIn("gone.wav", line)
        self.assertEqual(self.host.ui.toasts[-1], ("warn", line))
        self.assertIn(line, texts(self.module.render()))
        self.check_render()

    def test_pack_ok_without_zip(self):
        self.pr.set_items([ProjectItem("p1", "in.mp4", "clip", tree_path="Footage", media_path=self.inside)])
        self.module._take_snapshot(self.module._state)
        self.module.on_action("files.pack")
        self.assertTrue(self.module._state.pack_report.ok)
        self.assertEqual(self.host.ui.toasts[-1][0], "ok")
        self.assertFalse(os.path.exists(self.root + ".zip"))


class Undo(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_nothing_to_undo(self):
        self.assertEqual(self.module.on_action("files.undo_last").toast, ("neutral", "Nothing to undo"))
        self.assertTrue(find(self.module.render(), "files.undo_last")["disabled"])

    def test_undo_filed(self):
        src = self.ext("u.mp4", 5000)
        item = self.user_import(src, "Inbox")
        copy = self.in_root("Footage", "u.mp4")
        result = self.module.on_action("files.undo_last")
        self.assertEqual(result.toast[0], "ok", result.toast)
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertEqual(self.item(item.node_id).tree_path, "Inbox")
        self.assertFalse(os.path.exists(copy))
        self.assertTrue(os.path.isfile(src))
        last = self.journal()[-1]
        self.assertEqual((last["status"], last["undo_of"], last["deleted"]), ("undone", self.journal()[-2]["id"], True))
        self.assertEqual(self.module.on_action("files.undo_last").toast, ("neutral", "Nothing to undo"))
        # the module's own changes during undo do not re-file the item
        self.fire("project.changed")
        self.assertEqual(self.item(item.node_id).media_path, src)

    def test_undo_filed_keeps_a_copy_the_user_changed(self):
        src = self.ext("v.mp4", 5000)
        self.user_import(src)
        write_file(self.in_root("Footage", "v.mp4"), 5000, "edited by the user")
        self.module.on_action("files.undo_last")
        self.assertTrue(os.path.isfile(self.in_root("Footage", "v.mp4")))
        self.assertFalse(self.journal()[-1]["deleted"])

    def test_undo_filed_when_original_is_gone(self):
        src = self.ext("w.mp4")
        self.user_import(src)
        os.remove(src)
        result = self.module.on_action("files.undo_last")
        self.assertEqual(result.toast[0], "warn")
        self.assertIn("no longer at", result.toast[1])

    def test_undo_binned(self):
        path = write_file(self.in_root("Graphics", "g.png"))
        item = self.user_import(path, "Misc")
        self.module.on_action("files.undo_last")
        self.assertEqual(self.item(item.node_id).tree_path, "Misc")

    def test_undo_imported_removes_the_item_and_keeps_the_file(self):
        self.host.settings.project.values["watch"] = True
        path = write_file(self.in_root("Footage", "drop.mp4"))
        self.module._apply_disk_change(self.module._state, {"type": "created", "path": path})
        created = [i for i in self.pr.snapshot_value.items if i.media_path == path]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].tree_path, "Footage")
        self.module.on_action("files.undo_last")
        self.assertEqual(self.pr.calls_named("remove_items")[-1][0], ((created[0].node_id,),))
        self.assertEqual([i for i in self.pr.snapshot_value.items if i.media_path == path], [])
        self.assertTrue(os.path.isfile(path))

    def test_undo_renamed(self):
        self.host.settings.project.values["watch"] = True
        old = write_file(self.in_root("Footage", "old.mp4"))
        self.pr.set_items([ProjectItem("r1", "old.mp4", "clip", tree_path="Footage", media_path=old)])
        self.module._take_snapshot(self.module._state)
        new = self.in_root("Footage", "new.mp4")
        os.rename(old, new)
        self.module._apply_disk_change(self.module._state, {"type": "moved", "src": old, "path": new})
        self.assertEqual((self.item("r1").name, self.item("r1").media_path), ("new.mp4", new))
        self.assertEqual(self.journal()[-1]["op"], "renamed")
        self.module.on_action("files.undo_last")
        self.assertEqual(self.item("r1").name, "old.mp4")
        self.assertEqual(self.item("r1").media_path, new)  # the old file name no longer exists

    def test_undo_moved_disk(self):
        self.host.settings.project.values["watch"] = True
        old = write_file(self.in_root("Footage", "m.mp4"))
        self.pr.set_items([ProjectItem("m1", "m.mp4", "clip", tree_path="Footage", media_path=old)])
        self.module._take_snapshot(self.module._state)
        new = self.in_root("Overlays", "m.mp4")
        os.replace(old, new)
        self.module._apply_disk_change(self.module._state, {"type": "moved", "src": old, "path": new})
        self.assertEqual((self.item("m1").tree_path, self.item("m1").media_path), ("Overlays", new))
        self.assertEqual(self.journal()[-1]["op"], "moved_disk")
        write_file(old)  # the user put a file back at the old place
        self.module.on_action("files.undo_last")
        self.assertEqual((self.item("m1").tree_path, self.item("m1").media_path), ("Footage", old))

    def test_undo_moved_bin_recreates_the_deleted_copy(self):
        src = self.ext("mb.mp4", 7000)
        item = self.user_import(src)
        first = self.in_root("Footage", "mb.mp4")
        self.user_move_bin(item.node_id, "Overlays")
        self.assertFalse(os.path.exists(first))
        moved = self.in_root("Overlays", "mb.mp4")
        result = self.module.on_action("files.undo_last")
        self.assertEqual(result.toast[0], "ok", result.toast)
        self.assertTrue(os.path.isfile(first))
        self.assertFalse(os.path.exists(moved))
        self.assertEqual((self.item(item.node_id).media_path, self.item(item.node_id).tree_path), (first, "Footage"))
        undo = self.journal()[-1]
        self.assertTrue(undo["recreated"])
        # and the filed entry below can still be undone: back to the original, the recreated copy goes
        self.module.on_action("files.undo_last")
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertFalse(os.path.exists(first))

    def test_undo_by_row_from_the_host_window_and_limit(self):
        paths = [write_file(self.in_root("Graphics", f"g{i}.png")) for i in range(3)]
        items = [self.user_import(p, "Misc") for p in paths]
        entries = [e for e in self.journal() if e["op"] == "binned"]
        result = self.module.on_action("files.undo", row=entries[0]["id"])
        self.assertEqual(result.toast[0], "ok")
        self.assertEqual(self.item(items[0].node_id).tree_path, "Misc")
        self.assertEqual(self.item(items[1].node_id).tree_path, "Graphics")
        self.assertEqual(self.module.on_action("files.undo", row="nope").toast[0], "warn")
        page = self.module.render_host()
        journal = next(b for b in page["blocks"] if b["id"] == "journal")
        undo_rows = [r["id"] for r in journal["rows"] if r["actions"]]
        self.assertEqual(set(undo_rows), {entries[1]["id"], entries[2]["id"]})
        self.module.UNDO_LIMIT = 1
        self.assertEqual(self.module.on_action("files.undo", row=entries[1]["id"]).toast[0], "warn")

    def test_undo_when_the_item_was_deleted(self):
        src = self.ext("del.mp4")
        item = self.user_import(src)
        self.pr.set_items([i for i in self.pr.snapshot_value.items if i.node_id != item.node_id])
        self.assertEqual(self.module.on_action("files.undo_last").toast[1], "The item is no longer in the project")


class Suppression(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_own_import_is_not_intercepted_and_own_files_do_not_reach_the_watcher(self):
        self.host.settings.project.values["watch"] = True
        path = write_file(self.in_root("Audio", "SFX", "hit.wav"))
        self.module._apply_disk_change(self.module._state, {"type": "created", "path": path})
        created = self.pr.calls_named("import_files")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][0][1], "Audio/SFX")
        self.fire("project.items_added")
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertEqual(len(self.pr.calls_named("move_to_bin")), 0)
        # a second watcher hit for the same file: the path is known, nothing happens
        self.module._apply_disk_change(self.module._state, {"type": "created", "path": path})
        self.assertEqual(len(self.pr.calls_named("import_files")), 1)

    def test_disk_events_of_own_copies_are_consumed(self):
        src = self.ext("copy.mp4")
        self.module._suppress.add(("disk", norm(self.in_root("Footage", "copy.mp4"))))
        before = len(self.host.jobs.jobs)
        self.module._on_disk_change(self.module._state, {"type": "created", "path": self.in_root("Footage", "copy.mp4")})
        self.assertEqual(len(self.host.jobs.jobs), before)
        self.user_import(src)
        # the copy just made was registered before the copy: the watcher hit is dropped
        jobs = len(self.host.jobs.jobs)
        self.module._on_disk_change(self.module._state, {"type": "created", "path": self.in_root("Footage", "copy (2).mp4")})
        self.assertEqual(len(self.host.jobs.jobs), jobs + 1)  # a different path is not suppressed

    def test_suppression_ttl(self):
        self.module._suppress.ttl = 0.05
        self.module._suppress.add(("disk", "x"))
        time.sleep(0.1)
        self.assertFalse(self.module._suppress.consume(("disk", "x")))

    def test_exports_and_exclusions_in_the_watcher(self):
        state = self.module._state
        self.assertTrue(self.module._watch_ignored(state, self.in_root("Exports", "final.mp4")))
        self.assertTrue(self.module._watch_ignored(state, self.in_root("Sample.prproj")))
        self.assertTrue(self.module._watch_ignored(state, self.in_root(".premiere-deck", "journal.jsonl")))
        self.assertTrue(self.module._watch_ignored(state, self.in_root("Footage", "a.mp4.part")))
        self.assertFalse(self.module._watch_ignored(state, self.in_root("Footage", "a.mp4")))
        self.host.settings.project.values["import_exports"] = True
        self.assertFalse(self.module._watch_ignored(state, self.in_root("Exports", "final.mp4")))


class SettingsAndRecovery(Base):
    def test_hand_edited_broken_project_settings_fall_back(self):
        self.make_module(enabled=False, enforce_bins="yes", mirror_bins=None, import_exports=3, watch="on",
                         pack_zip=[], overrides={"bad": 1}, layout="nonsense")
        self.host.settings.values.update(copy_buffer_mb=-4, stable_seconds="2", toast_filed="no",
                                         default_layout="missing")
        m = self.module
        self.assertEqual([m._proj(k) for k in ("enforce_bins", "mirror_bins", "import_exports", "watch", "pack_zip")],
                         [True, True, False, True, False])
        self.assertEqual(m._proj("overrides"), [])
        self.assertEqual(m._proj("layout"), "default")
        self.assertEqual(m._host("copy_buffer_mb"), 16)
        self.assertEqual(m._host("stable_seconds"), 2)
        self.assertIs(m._host("toast_filed"), True)
        self.assertEqual(m._host("default_layout"), "default")
        self.host.settings.project.values["enabled"] = "true"
        self.assertFalse(m._enabled())
        self.check_render()

    def test_settings_store_that_raises(self):
        self.make_module()

        class Broken:
            def get(self, key, default=None):
                raise ValueError("project.json is not JSON")

        self.host.settings.project = Broken()
        self.assertIs(self.module._proj("watch"), True)
        self.assertFalse(self.module._enabled())

    def test_project_file_in_project_folder_uses_the_parent_as_root(self):
        top = os.path.join(self.scratch, "projects", "Nested")
        os.makedirs(os.path.join(top, "Footage"))
        write_file(os.path.join(top, "Project", "Nested.prproj"), 10)
        from premiere_deck.premiere.model import ProjectInfo
        folder = os.path.join(top, "Project")
        self.project = ProjectInfo(path=os.path.join(folder, "Nested.prproj"), root=folder, name="Nested",
                                   deck_dir=os.path.join(folder, ".premiere-deck"))
        self.make_module(enabled=True, watch=False)
        self.assertEqual(self.module._state.root, top)
        self.assertTrue(os.path.isdir(os.path.join(top, "Audio", "SFX")))
        self.assertFalse(os.path.exists(os.path.join(folder, "Footage")))
        with open(os.path.join(top, ".premiere-deck", "project.json"), encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertTrue(marker["modules"]["files"]["enabled"])
        self.assertEqual(self.module.section()["status"]["text"], "Paused")

    def test_custom_layout_is_used(self):
        deck = os.path.join(self.root, ".premiere-deck")
        os.makedirs(deck)
        custom = {"id": "custom", "name": "Mine", "folders": ["Video", "Sound"], "project_dir": "Project",
                  "rules": [{"match": {"kind": "audio"}, "to": "Sound"}, {"match": {}, "to": "Video"}]}
        with open(os.path.join(deck, "layout.json"), "w", encoding="utf-8") as handle:
            json.dump(custom, handle)
        self.make_module(enabled=True, watch=False, layout="custom")
        item = self.user_import(self.ext("x.wav"))
        self.assertEqual(self.item(item.node_id).media_path, self.in_root("Sound", "x.wav"))

    def test_recovery_of_pending_entries(self):
        self.make_module(enabled=True, watch=False)
        self.module.project_closed()
        journal = J.Journal(os.path.join(self.root, ".premiere-deck", "journal.jsonl"))
        src = self.ext("half.mp4", 3000, "h")
        done_copy = write_file(self.in_root("Footage", "half.mp4"), 3000, "h")
        stale = self.ext("stale.mp4", 3000, "s")
        write_file(self.in_root("Footage", "stale.mp4.part"), 100)
        relinked_src = self.ext("relinked.mp4")
        relinked = write_file(self.in_root("Footage", "relinked.mp4"))
        self.pr.set_items([ProjectItem("h1", "half.mp4", "clip", media_path=src),
                           ProjectItem("s1", "stale.mp4", "clip", media_path=stale),
                           ProjectItem("r1", "relinked.mp4", "clip", tree_path="Footage", media_path=relinked)])
        e1 = journal.append("filed", node_id="h1", name="half.mp4", **{"from": src}, to=done_copy, bin_from="",
                            bin_to="Footage", copied=True)
        e2 = journal.append("filed", node_id="s1", name="stale.mp4", **{"from": stale}, to=self.in_root("Footage", "stale.mp4"),
                            bin_from="", bin_to="Footage", copied=True)
        e3 = journal.append("filed", node_id="r1", name="relinked.mp4", **{"from": relinked_src}, to=relinked,
                            bin_from="", bin_to="Footage", copied=True)
        self.module.project_opened(self.project)
        state = self.module._state.journal
        self.assertEqual(state.get(e1["id"])["status"], "done")
        self.assertTrue(state.get(e1["id"])["resumed"])
        self.assertEqual((self.item("h1").media_path, self.item("h1").tree_path), (done_copy, "Footage"))
        self.assertEqual(state.get(e2["id"])["status"], "failed")
        self.assertFalse(os.path.exists(self.in_root("Footage", "stale.mp4.part")))
        self.assertEqual(state.get(e3["id"])["status"], "done")
        self.assertTrue(state.get(e3["id"])["recovered"])

    def test_closing_cancels_a_queued_copy(self):
        self.host.jobs.manual = True
        self.make_module(enabled=True, watch=False)
        self.host.jobs.step()
        src = self.ext("queued.mp4")
        item = self.user_import(src)
        self.module.project_closed()
        self.host.jobs.step()
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertFalse(os.path.exists(self.in_root("Footage", "queued.mp4")))

    def test_late_job_callback_after_close_is_resumed_on_next_open(self):
        self.make_module(enabled=True, watch=False)
        held = []

        class SplitJobs:
            """Runs the job function at once and keeps the callback for later."""

            def submit(self, fn, *, title, priority="normal", on_progress=None, on_done=None, on_error=None):
                from premiere_deck.modules.base import FakeJob

                job = FakeJob("late", title)
                held.append((job, fn(job), on_done))
                return job

            def running(self):
                return []

        self.module.ctx.jobs = SplitJobs()
        src = self.ext("late.mp4")
        item = self.user_import(src)
        self.module.project_closed()
        job, value, on_done = held[0]
        on_done(job, value)  # arrives after the project closed: ignored
        self.assertEqual(self.item(item.node_id).media_path, src)
        copy = self.in_root("Footage", "late.mp4")
        self.assertTrue(os.path.isfile(copy))
        self.module.ctx.jobs = self.host.jobs
        self.module.project_opened(self.project)  # the pending entry is resumed
        self.assertEqual(self.item(item.node_id).media_path, copy)
        self.assertEqual(self.journal()[-1]["status"], "done")


class Actions(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_locate_and_remove_offline(self):
        missing = self.in_root("Footage", "Day9", "lost.mp4")
        self.pr.set_items([ProjectItem("o1", "lost.mp4", "clip", tree_path="Footage", media_path=missing, is_offline=True)])
        self.fire("project.changed")
        result = self.module.on_action("files.locate", row="o1")
        self.assertFalse(result.rerender)
        self.assertEqual(self.host.host.revealed, [self.in_root("Footage")])
        self.assertEqual(self.module.on_action("files.locate", row="zz").toast[0], "warn")
        result = self.module.on_action("files.remove", row="o1")
        self.assertEqual(result.toast, ("ok", "Removed lost.mp4 from the project"))
        args, _ = self.pr.calls_named("remove_items")[-1]
        self.assertEqual(args, (("o1",),))
        self.assertNotIn("o1", self.pr.snapshot_value.by_node_id())
        self.assertEqual(self.journal()[-1]["op"], "removed")
        self.assertEqual(self.module._state.offline, [])

        def boom(node_ids):
            raise PremiereError("boom")

        self.pr.remove_items = boom
        self.pr.set_items([ProjectItem("o2", "x.mp4", "clip", media_path=missing, is_offline=True)])
        self.fire("project.changed")
        self.assertEqual(self.module.on_action("files.remove", row="o2").toast[0], "error")

    def test_watch_toggle(self):
        self.module.on_action("files.watch", True)
        self.assertIsNotNone(self.module._state.watcher)
        self.assertTrue(self.host.settings.project.values["watch"])
        self.module.on_action("files.watch", False)
        self.assertIsNone(self.module._state.watcher)
        self.assertFalse(self.host.settings.project.values["watch"])

    def test_host_project_setting(self):
        self.assertIsNone(self.module.on_action("files.host:project_setting", {"key": "enforce_bins", "value": False}))
        self.assertFalse(self.host.settings.project.values["enforce_bins"])
        self.module.on_action("files.host:project_setting", {"mirror_bins": False, "pack_zip": True})
        self.assertEqual((self.host.settings.project.values["mirror_bins"], self.host.settings.project.values["pack_zip"]),
                         (False, True))
        self.assertEqual(self.module.on_action("files.host:project_setting", {"key": "enabled", "value": False}).toast[0], "warn")
        self.assertTrue(self.host.settings.project.values["enabled"])

    def test_host_layout_actions(self):
        layout = self.module._state.layout.to_json()
        layout["folders"].append("B-Roll")
        layout["rules"].insert(0, {"match": {"name": "broll"}, "to": "B-Roll"})
        result = self.module.on_action("files.host:layout_save", {"layout": layout})
        self.assertEqual(result.toast, ("ok", "Layout saved"))
        self.assertEqual(self.host.settings.project.values["layout"], "custom")
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".premiere-deck", "layout.json")))
        item = self.user_import(self.ext("broll_city.mp4"))
        self.assertEqual(self.item(item.node_id).tree_path, "B-Roll")
        self.assertEqual(self.module.on_action("files.host:layout_save", "{nope").toast[0], "error")
        self.assertEqual(self.module.on_action("files.host:layout_save", {"layout": {"folders": []}}).toast[0], "error")
        self.assertEqual(self.module.on_action("files.host:layout_reset").toast[0], "ok")
        self.assertEqual(self.module._state.layout.id, "default")
        self.assertIsNone(self.module.on_action("files.host:layout_select", {"value": "custom"}))
        self.assertIn("B-Roll", self.module._state.layout.folders)
        self.assertEqual(self.module.on_action("files.host:layout_select", "unknown").toast[0], "warn")

    def test_host_overrides_journal_filter_reveal(self):
        result = self.module.on_action("files.host:overrides_save",
                                       [{"source": self.downloads, "to": "Audio/SFX"}, {"to": "x"}])
        self.assertEqual(result.toast, ("warn", "Overrides saved · 1 invalid skipped"))
        item = self.user_import(self.ext("long_track.wav"))
        self.assertEqual(self.item(item.node_id).tree_path, "Audio/SFX")
        self.assertEqual(self.module.on_action("files.host:overrides_save", "x").toast[0], "warn")
        self.module.on_action("files.host:journal_filter", {"op": "filed", "query": "long"})
        journal = next(b for b in self.module.render_host()["blocks"] if b["id"] == "journal")
        self.assertEqual([r["name"] for r in journal["rows"]], ["long_track.wav"])
        self.module.on_action("host:journal_op", "")  # the registry passes window ids without the prefix
        self.module.on_action("host:journal_query", "")
        journal = next(b for b in self.module.render_host()["blocks"] if b["id"] == "journal")
        self.assertGreater(len(journal["rows"]), 1)
        self.assertEqual(journal["filters"][0]["id"], "files.host:journal_op")
        self.assertEqual(journal["search"]["id"], "files.host:journal_query")
        self.assertFalse(self.module.on_action("files.host:reveal").rerender)
        self.assertEqual(self.host.host.revealed[-1], self.root)

    def test_unknown_actions(self):
        self.assertEqual(self.module.on_action("files.nothing").toast[0], "warn")
        self.assertEqual(self.module.on_action("files.host:nothing").toast[0], "warn")

    def test_render_host_blocks(self):
        page = self.module.render_host()
        ids = [b["id"] for b in page["blocks"]]
        for block_id in ("status", "main", "journal", "collect_plan", "pack_options", "project_settings", "layout",
                         "layout_folders", "layout_rules", "overrides"):
            self.assertIn(block_id, ids)
        main = next(b for b in page["blocks"] if b["id"] == "main")
        self.assertIn("files.collect", [b["id"] for b in main["items"]])
        pack = next(b for b in page["blocks"] if b["id"] == "pack_options")
        self.assertEqual(pack["submit"]["id"], "files.pack")
        self.check_render()

    def test_override_add_and_remove(self):
        result = self.module.on_action("host:override_add", {"match": self.downloads, "to": "Audio/SFX"})
        self.assertEqual(result.toast[0], "ok")
        self.module.on_action("host:override_add", {"match": "*_vo*", "to": "Audio/Voice"})
        self.assertEqual(self.host.settings.project.values["overrides"],
                         [{"source": self.downloads, "to": "Audio/SFX"}, {"name": "*_vo*", "to": "Audio/Voice"}])
        self.assertEqual(self.module.on_action("host:override_add", {"match": "", "to": "x"}).toast[0], "warn")
        self.assertEqual(self.module.on_action("host:override_add", {"match": "*a*", "to": "../x"}).toast[0], "warn")
        table = next(b for b in self.module.render_host()["blocks"] if b["id"] == "overrides")
        self.assertEqual([r["match"] for r in table["rows"]], [self.downloads, "*_vo*"])
        self.assertEqual(self.module.on_action("host:override_remove", row="0").toast, ("ok", "Override removed"))
        self.assertEqual(self.host.settings.project.values["overrides"], [{"name": "*_vo*", "to": "Audio/Voice"}])
        self.assertEqual(self.module.on_action("host:override_remove", row="7").toast[0], "warn")

    def test_pack_form_submit_sets_the_zip_option(self):
        self.module.on_action("pack", {"pack_zip": True})
        self.assertTrue(self.host.settings.project.values["pack_zip"])
        self.assertTrue(os.path.isfile(self.root + ".zip"))


if __name__ == "__main__":
    unittest.main()
