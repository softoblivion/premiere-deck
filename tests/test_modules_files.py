"""Files module with FakeHost: render states, intercept, bin mirror, Sort, Sync, Make ZIP, undo, suppression,
settings, recovery and the journal clean-up (docs/FILES.md 3 to 8)."""

import json
import os
import shutil
import time
import unittest
from unittest import mock

from premiere_deck.host import protocol as P
from premiere_deck.modules.files import journal as J
from premiere_deck.modules.files.layout import norm
from premiere_deck.modules.files.module import FilesModule
from premiere_deck.premiere.model import PremiereError, ProjectItem, SequenceInfo, TrackItem
from tests.test_modules_files_support import FilesCase, info, write_file

BLOCK_TYPES = {"cards", "table", "list", "form", "actions", "text", "progress", "chips"}
TILES = ["files.sort", "files.sync_all", "files.sync_used", "files.zip"]


def check_block(test, block):
    """The block shapes of docs/BRIDGE_API.md 5."""
    kind = block["type"]
    test.assertIn(kind, BLOCK_TYPES)
    test.assertIsInstance(block.get("id"), str)

    def check_button(node):
        test.assertEqual(node["kind"], "button")
        test.assertTrue(node["id"].startswith("files."), node["id"])

    if kind == "actions":
        for item in block["items"]:
            check_button(item)
    elif kind == "chips":
        for item in block["items"]:
            test.assertTrue({"id", "text", "tone"} <= set(item))
    elif kind == "table":
        keys = [c["key"] for c in block["columns"]]
        for row in block["rows"]:
            test.assertIsInstance(row["id"], str)
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
            for action in row.get("actions", []):
                check_button(action)
    elif kind == "form":
        for field in block["fields"]:
            if "kind" in field and field["kind"] in ("input", "toggle", "select") and "id" in field:
                test.assertTrue(field["id"].startswith("files."))
            else:
                test.assertTrue({"key", "label", "kind", "value"} <= set(field), field)
                test.assertIn(field["kind"], ("bool", "int", "text", "select", "path"))
        if block.get("submit"):
            check_button(block["submit"])
    elif kind == "progress":
        test.assertIn("label", block)


def walk(nodes):
    for node in nodes:
        yield node
        if node.get("kind") in ("row", "group"):
            yield from walk(node["items"])


def node_ids(nodes):
    return [node["id"] for node in walk(nodes) if "id" in node and node.get("kind") != "group"]


def texts(nodes):
    out = []
    for node in walk(nodes):
        if node.get("kind") == "text":
            out.append(node["text"])
        if node.get("kind") == "button":
            out.append(node["label"])
    return out


def find(nodes, node_id):
    return next((node for node in walk(nodes) if node.get("id") == node_id), None)


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

    def block(self, block_id):
        return next((b for b in self.module.render_host()["blocks"] if b["id"] == block_id), None)

    def place(self, *items):
        """Items the project already has (no intercept): the snapshot is taken without an event."""
        self.pr.set_items(list(self.pr.snapshot_value.items) + list(items))
        self.module._take_snapshot(self.module._state)


class RenderStates(Base):
    def test_no_project(self):
        module = FilesModule(self.host.context())
        self.module = module
        module.start()
        sec = self.check_render()
        self.assertIsNone(sec["status"])  # the panel draws no section status (3.0)
        self.assertEqual(module.render_host()["status"]["text"], "No project open")
        self.assertIn("Open a project", sec["items"][0]["text"])
        self.assertEqual(module.render_host()["status"]["text"], "No project open")
        result = module.on_action("files.sort")
        self.assertFalse(result.rerender)
        self.assertEqual(self.host.ui.results[-1][:2], ("sort", "neutral"))

    def test_not_set_up_observes_only(self):
        self.make_module()
        sec = self.check_render()
        self.assertIsNone(sec["status"])
        self.assertEqual(self.module.render_host()["status"], {"text": "Set up this project", "tone": "warn"})
        self.assertEqual(node_ids(sec["items"]), ["files.setup"])
        self.assertEqual(find(sec["items"], "files.setup")["tone"], "accent")
        clip = self.ext("clip.mp4")
        self.user_import(clip)
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertFalse(os.path.exists(self.in_root("Footage", "clip.mp4")))
        self.assertIn("1 item outside the project folder.", texts(self.module.render()))
        for action in ("sort", "sync_all", "sync_used", "zip", "undo_last", "journal", "stop"):
            self.assertEqual(self.press(f"files.{action}"), (action, "warn", "Set up this project first", None))
        self.assertEqual(self.press("files.open_folder")[1], "ok")
        self.assertEqual(self.host.host.opened, [self.root])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".premiere-deck", "journal.jsonl")))
        page = self.module.render_host()
        self.assertEqual([b["id"] for b in page["blocks"][:2]], ["setup_actions", "auto"])
        self.assertEqual([b["id"] for b in page["blocks"][:3]][2], "status_chips")
        self.assertEqual(page["subtitle"], self.root)
        self.assertEqual(page["header_actions"][0]["id"], "files.open_folder")

    def test_setup_creates_folders_bins_and_journal(self):
        self.make_module(enabled=True, watch=False)
        for folder in ("Footage", "Stock", "Generated", "Media", "Audio/Music", "Audio/SFX", "Audio/Voice", "Graphics",
                       "Overlays", "Motion", "Exports/Frames", "Project"):
            self.assertTrue(os.path.isdir(self.in_root(*folder.split("/"))), folder)
        bins = [args[0] for args, _ in self.pr.calls_named("create_bin_path")]
        for name in ("Audio/SFX", "Stock", "Generated", "Media"):
            self.assertIn(name, bins)
        self.assertNotIn("Project", bins)
        self.assertTrue(self.host.settings.project.values["enabled"])
        self.assertEqual(self.host.settings.project.values["layout"], "default")
        self.assertEqual(self.journal()[-1]["op"], "setup")
        self.assertEqual(self.host.ui.results[-1], ("setup", "ok", "Project set up · 13 folders", None))
        self.assertEqual(self.host.ui.toasts, [])
        sec = self.check_render()
        self.assertIsNone(sec["status"])
        self.assertEqual(self.module.render_host()["status"]["text"], "Paused")
        self.assertEqual(os.listdir(os.path.join(self.root, ".premiere-deck")), ["journal.jsonl"])  # check file gone

    def test_setup_tile_stays_and_rechecks_folders_and_bins(self):
        self.make_module(enabled=True, watch=False)
        tile = find(self.module.render(), "files.setup")
        self.assertEqual((tile["short"], tile["icon"], tile["tone"]), ("SETUP", "folder", "neutral"))
        self.assertEqual(tile["label"], "Check project folders")
        self.assertTrue(tile["hint"])
        journal = len(self.journal())
        bins_before = len(self.pr.calls_named("create_bin_path"))
        self.assertEqual(self.press("files.setup"), ("setup", "ok", "Project folders checked", None))
        self.assertEqual(len(self.pr.calls_named("create_bin_path")), 2 * bins_before)  # every bin checked again
        shutil.rmtree(self.in_root("Audio"))
        os.rmdir(self.in_root("Motion"))
        self.assertEqual(self.press("files.setup"),
                         ("setup", "ok", "Project folders checked · 5 folders created again", None))
        for folder in ("Audio/Music", "Audio/SFX", "Audio/Voice", "Motion"):
            self.assertTrue(os.path.isdir(self.in_root(*folder.split("/"))), folder)
        self.assertEqual(len(self.journal()), journal)  # a re-check is not journaled
        self.assertEqual(self.host.ui.toasts, [])
        self.pr.eval_errors.clear()
        self.pr.connected = False
        result = self.press("files.setup")
        self.assertEqual(result[:3], ("setup", "warn", "Project folders checked"))
        self.assertEqual(result[3], "Bins not checked · Premiere Pro is not connected")

    def test_recheck_without_write_access(self):
        self.make_module(enabled=True, watch=False)
        with mock.patch("premiere_deck.modules.files.module.write_check", return_value="access denied"):
            result = self.press("files.setup")
        self.assertEqual(result, ("setup", "error", "No write access to the project folder", "access denied"))
        self.assertTrue(self.host.settings.project.values["enabled"])

    def test_setup_without_write_access(self):
        self.make_module()
        with mock.patch("premiere_deck.modules.files.module.write_check", return_value="access denied"):
            result = self.press("files.setup")
        self.assertEqual(result, ("setup", "error", "No write access to the project folder", "access denied"))
        self.assertFalse(os.path.isdir(self.in_root("Footage")))
        self.assertNotIn("enabled", self.host.settings.project.values)

    def test_watching_status(self):
        self.make_module(enabled=True)
        try:
            sec = self.check_render()
            self.assertIsNone(sec["status"])  # the watcher state is not sent to the panel (3.0)
            self.assertEqual(self.module.render_host()["status"], {"text": "Watching · Sample", "tone": "ok"})
            self.assertIsNone(find(sec["items"], "files.watch"))  # a project setting of the host window now
        finally:
            self.module.on_setting_changed("watch", "project", False)
        self.assertEqual(self.module.render_host()["status"]["text"], "Paused")
        self.assertIsNone(self.module.section()["status"])

    def test_enabled_render_has_the_2_0_layout(self):
        self.make_module(enabled=True, watch=False)
        write_file(self.in_root("loose.mp4"))
        self.place(ProjectItem("l1", "loose.mp4", "clip", media_path=self.in_root("loose.mp4")),
                   ProjectItem("x1", "ext.mp4", "clip", media_path=self.ext("ext.mp4")),
                   ProjectItem("o1", "gone.mp4", "clip", media_path=self.in_root("Footage", "gone.mp4"),
                               is_offline=True))
        sec = self.check_render()
        items = sec["items"]
        header = items[0]
        self.assertEqual(header["kind"], "row")
        self.assertEqual(header["items"][0]["text"], "Sample")
        folder = header["items"][1]
        self.assertEqual((folder["id"], folder["variant"], folder["icon"], folder["label"]),
                         ("files.open_folder", "icon", "open-folder", "Open project folder"))
        self.assertEqual(items[1]["text"], self.root)
        chips = items[2]["items"]
        self.assertTrue(all(c["chip"] for c in chips))
        self.assertEqual([(c["text"], c["tone"]) for c in chips],
                         [("1 in the root", "neutral"), ("1 outside", "warn"), ("1 offline", "error")])
        group = items[3]
        self.assertEqual((group["kind"], group["id"], group["tiles"], group["columns"], group["title"]),
                         ("group", "files.actions", True, 2, None))
        tiles = {t["id"]: t for t in group["items"]}
        self.assertEqual(list(tiles), TILES + ["files.setup"])
        self.assertTrue(all(t["kind"] == "button" for t in tiles.values()))  # no switch tiles (3.0)
        self.assertEqual([(t["icon"], t["short"]) for t in tiles.values()],
                         [("sort", "SORT"), ("sync-all", "SYNC"), ("sync-used", "USED"), ("zip", "ZIP"),
                          ("folder", "SETUP")])
        self.assertTrue(all(t["hint"] for t in tiles.values()))
        self.assertEqual(tiles["files.sort"]["auto"],
                         {"id": "files.auto_sort", "label": "Auto sort", "value": self.module._proj("auto_sort"),
                          "hint": "Files new media of the project folder into the layout folders and bins."})
        self.assertEqual(tiles["files.sync_all"]["auto"],
                         {"id": "files.auto_copy", "label": "Auto copy", "value": self.module._proj("auto_copy"),
                          "hint": "Copies files imported from outside the project folder into Media and relinks them."})
        self.assertEqual([t for t in TILES[2:] + ["files.setup"] if "auto" in tiles[t]], [])
        self.assertIsNone(sec["status"])
        self.assertEqual(tiles["files.sort"]["meta"], "1 item to file")
        self.assertEqual(tiles["files.sort"]["confirm"],
                         "Move 1 file into the layout folders? Every move is journaled and can be undone.")
        self.assertEqual(tiles["files.sync_all"]["meta"], "1 outside the root")
        self.assertEqual(tiles["files.sync_all"]["confirm"], "Copy 1 file from outside the project folder into Media?")
        self.assertEqual(tiles["files.sync_used"]["meta"], "0 in sequences")
        self.assertEqual(tiles["files.sync_used"]["confirm"],
                         "Copy the files used in sequences from outside the project folder into Media?")
        self.assertEqual(tiles["files.zip"]["meta"], "Checks offline first")
        self.assertNotIn("confirm", tiles["files.zip"])
        bottom = items[4]["items"]
        self.assertEqual([b["id"] for b in bottom], ["files.undo_last"])  # no Journal tile in the panel (2.1)
        self.assertEqual((bottom[0]["variant"], bottom[0]["icon"]), ("icon", "undo"))
        self.assertTrue(bottom[0]["disabled"])  # setup itself is not undoable
        self.assertNotIn("files.journal", node_ids(items))
        offline = items[5]
        self.assertEqual((offline["kind"], offline["id"], offline["title"], offline["collapsible"]),
                         ("group", "files.offline", "Offline", True))
        rows = offline["items"][0]["rows"]
        self.assertEqual([a["id"] for a in rows[0]["actions"]], ["files.locate", "files.remove"])
        self.assertEqual(rows[0]["actions"][1]["confirm"],
                         "Remove gone.mp4 from the project? The file on disk is not touched.")
        for removed in ("files.collect", "files.pack", "files.watch", "files.recent", "files.cancel"):
            self.assertNotIn(removed, node_ids(items))

    def test_meta_without_estimate_and_used_count(self):
        self.make_module(enabled=True, watch=False)
        src = self.ext("used.mp4")
        self.pr.sequences_value = [SequenceInfo("seq1", "Main", 1920, 1080, 25.0, 10.0)]
        self.pr.track_items_value = {"seq1": [TrackItem("seq1", "video", 0, 0, "used.mp4", "u1", 0, 1, 0, 1)]}
        self.place(ProjectItem("u1", "used.mp4", "clip", media_path=src))
        self.module._refresh_used(self.module._state, force=True)
        tiles = {t["id"]: t for t in find(self.module.render(), "files.actions")["items"]}
        self.assertEqual(tiles["files.sort"]["meta"], "Nothing to file")
        self.assertIn("Sort the media", tiles["files.sort"]["confirm"])
        self.assertEqual(tiles["files.sync_used"]["meta"], "1 in sequences")

    def test_render_offline_layout_error_and_premiere_error(self):
        os.makedirs(os.path.join(self.root, ".premiere-deck"))
        with open(os.path.join(self.root, ".premiere-deck", "layout.json"), "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        gone = ProjectItem("g1", "gone.mp4", "clip", tree_path="Footage", media_path=self.in_root("Footage", "gone.mp4"),
                           is_offline=True)
        self.pr.set_items([gone])
        self.make_module(enabled=True, watch=False, layout="custom")
        sec = self.check_render()
        self.assertTrue(any("layout.json could not be read" in t for t in texts(sec["items"])))
        self.assertIsNotNone(self.block("layout_error"))
        self.pr.connected = False
        self.fire("project.changed")
        self.assertTrue(any("did not answer" in t for t in texts(self.module.render())))

    def test_busy_tile_disables_the_others_and_shows_progress(self):
        self.host.jobs.manual = True
        self.make_module(enabled=True, watch=False)
        self.host.jobs.step()
        write_file(self.in_root("loose.mp4"))
        self.place(ProjectItem("l1", "loose.mp4", "clip", media_path=self.in_root("loose.mp4")))
        self.module.on_action("files.sort")
        sec = self.check_render()
        tiles = {t["id"]: t for t in find(sec["items"], "files.actions")["items"]}
        self.assertTrue(tiles["files.sort"]["busy"])
        self.assertTrue(all(tiles[t]["disabled"] for t in TILES[1:]))
        self.assertIn("auto", tiles["files.sync_all"])  # the capsule stays usable on a disabled tile
        progress = next(n for n in walk(sec["items"]) if n["kind"] == "progress")
        self.assertEqual(progress["cancel"], "files.stop")
        self.assertEqual(sec["status"]["tone"], "accent")  # the job label, the only status still sent
        self.assertEqual(self.host.jobs.jobs[-1].priority, "high")
        job_blocks = [b for b in self.module.render_host()["blocks"] if b["id"].startswith("job_")]
        self.assertEqual(job_blocks[0]["cancel"], "files.stop")
        self.assertEqual(self.press("files.zip"), ("zip", "neutral", "Another action is running", None))
        self.host.jobs.step()
        self.assertEqual(self.host.ui.results[-1][:2], ("sort", "ok"))
        self.assertFalse(any(n["kind"] == "progress" for n in walk(self.module.section()["items"])))

    def test_settings_schema(self):
        self.make_module()
        fields = {f.key: f for f in self.module.settings_schema()}
        self.assertEqual({k for k, f in fields.items() if f.scope == "host"},
                         {"toast_filed", "copy_buffer_mb", "stable_seconds", "default_layout"})
        self.assertEqual({k for k, f in fields.items() if f.scope == "project"},
                         {"enforce_bins", "mirror_bins", "import_exports", "watch"})
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
        self.assertEqual(self.host.ui.results, [("setup", "ok", "Project set up · 13 folders", None)])  # no result
        name, data = self.host.events.emitted[-1]
        self.assertEqual((name, data["op"], data["text"]), ("journal.appended", "filed", "Filed A001.mp4 into Footage"))
        self.assertEqual(self.host.jobs.jobs[-1].priority, "high")
        # the module's own bin move does not trigger the mirror
        self.fire("project.changed")
        self.assertEqual(len(self.pr.calls_named("relink")), 1)

    def test_stock_and_generated_names_go_to_their_folders(self):
        stock = self.ext("pexels-city-night.mp4")
        generated = self.ext("kling_0012.mp4")
        for path in (stock, generated):
            self.host.tools.infos[path] = info(path, "video", has_audio=True, duration=8.0)
        a, b = self.user_import(stock), self.user_import(generated)
        self.assertEqual(self.item(a.node_id).tree_path, "Stock")
        self.assertEqual(self.item(b.node_id).tree_path, "Generated")

    def test_audio_class_decides_after_the_name_rules(self):
        self.module._classifier.classes.update({"talk.wav": (0.9, 0.1), "song.wav": (0.0, 1.4),
                                                "rain.wav": (0.05, 0.1), "sfx_speech.wav": (0.9, 0.0)})
        folders = {}
        for name in ("talk.wav", "song.wav", "rain.wav", "sfx_speech.wav"):
            path = self.ext(name)
            self.host.tools.infos[path] = info(path, "audio", duration=45.0)
            folders[name] = self.item(self.user_import(path).node_id).tree_path
        self.assertEqual(folders, {"talk.wav": "Audio/Voice", "song.wav": "Audio/Music", "rain.wav": "Audio/SFX",
                                   "sfx_speech.wav": "Audio/SFX"})

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
        masks = write_file(self.in_root("Sample Masks", "a.prmf"))
        temp = self.ext("download.mp4.crdownload")
        self.user_import(autosave)
        self.user_import(masks)
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

    def test_copy_failure_reason_is_in_the_toast_and_the_journal(self):
        src = self.ext("locked.mp4")
        with mock.patch("premiere_deck.modules.files.module.copy_file", side_effect=PermissionError(13, "denied")):
            item = self.user_import(src)
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertEqual(self.toasts(), ["Could not copy locked.mp4 · access denied"])
        self.assertEqual((self.journal()[-1]["status"], self.journal()[-1]["error"]), ("failed", "access denied"))
        self.assertFalse(os.path.exists(self.in_root("Footage", "locked.mp4")))

    def test_write_check_failure_stops_the_intercept_for_the_session(self):
        self.module._state.write_ok = None
        with mock.patch("premiere_deck.modules.files.module.write_check", return_value="access denied") as check:
            first = self.user_import(self.ext("one.mp4"))
            second = self.user_import(self.ext("two.mp4"))
        self.assertEqual(check.call_count, 1)
        self.assertEqual(self.toasts(), ["No write access to the project folder"])
        self.assertEqual(self.item(first.node_id).media_path, os.path.join(self.downloads, "one.mp4"))
        self.assertEqual(self.item(second.node_id).media_path, os.path.join(self.downloads, "two.mp4"))
        self.assertEqual(os.listdir(self.in_root("Footage")), [])


class BinMirror(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_user_file_moved_to_another_layout_bin_is_moved_on_disk(self):
        path = write_file(self.in_root("Footage", "grain.mov"), 5000)
        self.place(ProjectItem("b1", "grain.mov", "clip", tree_path="Footage", media_path=path))
        self.user_move_bin("b1", "Overlays")
        target = self.in_root("Overlays", "grain.mov")
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(path))  # a rename inside the root
        self.assertEqual(self.item("b1").media_path, target)
        entry = self.journal()[-1]
        self.assertEqual((entry["op"], entry["status"], entry["moved"], entry["copied"]),
                         ("moved_bin", "done", True, False))
        # the correction of a classification: Footage -> Stock files it under Stock
        self.user_move_bin("b1", "Stock")
        self.assertTrue(os.path.isfile(self.in_root("Stock", "grain.mov")))

    def test_module_copy_moved_to_another_bin_leaves_no_old_copy(self):
        src = self.ext("clip.mp4", 8000)
        item = self.user_import(src)
        first = self.in_root("Footage", "clip.mp4")
        self.assertTrue(os.path.isfile(first))
        self.user_move_bin(item.node_id, "Footage/Day1")
        moved = self.in_root("Footage", "Day1", "clip.mp4")
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.isfile(src))

    def test_file_shared_by_another_item_is_copied(self):
        src = self.ext("shared.mp4", 8000)
        first = self.user_import(src)
        second = self.user_import(src)  # same source again: reuses the copy
        copy = self.in_root("Footage", "shared.mp4")
        self.assertEqual(self.item(second.node_id).media_path, copy)
        self.user_move_bin(first.node_id, "Overlays")
        self.assertTrue(os.path.isfile(copy))
        self.assertTrue(os.path.isfile(self.in_root("Overlays", "shared.mp4")))
        self.assertTrue(self.journal()[-1]["copied"])

    def test_relink_failure_moves_the_file_back(self):
        path = write_file(self.in_root("Footage", "back.mov"), 5000)
        self.place(ProjectItem("b2", "back.mov", "clip", tree_path="Footage", media_path=path))
        self.pr.relink_result = False
        self.user_move_bin("b2", "Overlays")
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(self.in_root("Overlays", "back.mov")))
        self.assertEqual(self.journal()[-1]["status"], "failed")

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
        src = self.ext("ext.mp4")
        self.place(ProjectItem("e1", "ext.mp4", "clip", tree_path="", media_path=src))
        self.user_move_bin("e1", "Overlays")
        self.assertEqual(self.item("e1").media_path, src)


class Sort(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.host.ui.results.clear()

    def video(self, *parts, size=4000, seed=""):
        path = write_file(self.in_root(*parts), size, seed)
        self.host.tools.infos[path] = info(path, "video", has_audio=True, duration=12.0)
        return path

    def test_sort_moves_loose_media_and_misfiled_stock_and_can_be_undone(self):
        loose = self.video("C0042.MP4")
        synced = self.video("Media", "kling_0007.mp4")
        stock = self.video("Footage", "pexels-forest.mp4")
        right = self.video("Footage", "A001_C002.mov")
        self.place(ProjectItem("a", "C0042.MP4", "clip", media_path=loose),
                   ProjectItem("b", "kling_0007.mp4", "clip", tree_path="Media", media_path=synced),
                   ProjectItem("c", "pexels-forest.mp4", "clip", tree_path="Footage", media_path=stock),
                   ProjectItem("d", "A001_C002.mov", "clip", tree_path="Footage", media_path=right))
        self.assertEqual(self.module._state.sort_estimate, 2)  # loose and in Media, without probing
        result = self.press("files.sort")
        self.assertEqual(result, ("sort", "ok", "Sorted 3 files · 11.7 KB", None))
        self.assertEqual((self.item("a").media_path, self.item("a").tree_path), (self.in_root("Footage", "C0042.MP4"), "Footage"))
        self.assertEqual(self.item("b").media_path, self.in_root("Generated", "kling_0007.mp4"))
        self.assertEqual((self.item("c").media_path, self.item("c").tree_path),
                         (self.in_root("Stock", "pexels-forest.mp4"), "Stock"))
        self.assertEqual(self.item("d").media_path, right)
        self.assertFalse(os.path.exists(loose))  # moved, not copied
        sorted_entries = [e for e in self.journal() if e["op"] == "sorted"]
        self.assertEqual(len(sorted_entries), 3)
        self.assertTrue(all(e["status"] == "done" and e["moved"] and not e["copied"] for e in sorted_entries))
        self.assertEqual(self.pr.calls_named("relink")[0][0], ("a", self.in_root("Footage", "C0042.MP4")))
        # undo of the last sort move: file and bin go back together
        result = self.press("files.undo_last")
        self.assertEqual(result[:2], ("undo_last", "ok"))
        self.assertEqual((self.item("c").media_path, self.item("c").tree_path), (stock, "Footage"))
        self.assertTrue(os.path.isfile(stock))
        self.assertFalse(os.path.exists(self.in_root("Stock", "pexels-forest.mp4")))
        self.assertEqual(self.journal()[-1]["undo_of"], sorted_entries[-1]["id"])

    def test_nothing_to_sort(self):
        self.assertEqual(self.press("files.sort"), ("sort", "neutral", "Nothing to sort", None))

    def test_user_placed_files_overrides_and_same_top_folder_stay(self):
        placed = self.video("Footage", "pexels-placed.mp4")
        J.Journal(os.path.join(self.root, ".premiere-deck", "journal.jsonl")).append(
            "moved_bin", status="done", node_id="p", name="pexels-placed.mp4", to=placed, **{"from": "x"})
        named = self.video("Footage", "pexels-named.mp4")
        self.host.settings.project.values["overrides"] = [{"name": "*named*", "to": "Footage"}]
        audio = write_file(self.in_root("Audio", "SFX", "theme.wav"))
        self.host.tools.infos[audio] = info(audio, "audio", duration=200.0)
        self.place(ProjectItem("p", "pexels-placed.mp4", "clip", tree_path="Footage", media_path=placed),
                   ProjectItem("n", "pexels-named.mp4", "clip", tree_path="Footage", media_path=named),
                   ProjectItem("s", "theme.wav", "clip", tree_path="Audio/SFX", media_path=audio))
        self.assertEqual(self.press("files.sort")[2], "Nothing to sort")
        self.assertTrue(os.path.isfile(placed) and os.path.isfile(named) and os.path.isfile(audio))

    def test_file_under_its_folder_in_another_bin_only_changes_bin(self):
        path = self.video("Footage", "Day1", "C0001.MP4")
        self.place(ProjectItem("x", "C0001.MP4", "clip", tree_path="Selects", media_path=path))
        result = self.press("files.sort")
        self.assertEqual(result[:3], ("sort", "ok", "Sorted 1 file · 0 B"))
        self.assertEqual((self.item("x").media_path, self.item("x").tree_path), (path, "Footage/Day1"))
        self.assertEqual(self.journal()[-1]["op"], "binned")
        self.assertEqual(self.pr.calls_named("relink"), [])

    def test_shared_file_is_copied_so_every_item_keeps_a_file(self):
        loose = self.video("shared.mp4")
        self.place(ProjectItem("a", "shared.mp4", "clip", media_path=loose),
                   ProjectItem("b", "shared copy", "clip", media_path=loose))
        self.assertEqual(self.press("files.sort")[1], "ok")
        target = self.in_root("Footage", "shared.mp4")
        self.assertTrue(os.path.isfile(loose))
        self.assertEqual({self.item("a").media_path, self.item("b").media_path}, {target})
        entries = [e for e in self.journal() if e["op"] == "sorted"]
        self.assertEqual([(e["copied"], e["moved"]) for e in entries], [(True, False), (False, False)])
        self.press("files.undo_last")
        self.press("files.undo_last")
        self.assertEqual({self.item("a").media_path, self.item("b").media_path}, {loose})
        self.assertFalse(os.path.exists(target))  # the module's copy is gone again

    def test_move_failure_and_relink_failure(self):
        a = self.video("a.mp4")
        b = self.video("b.mp4")
        self.place(ProjectItem("a", "a.mp4", "clip", media_path=a), ProjectItem("b", "b.mp4", "clip", media_path=b))
        real = __import__("premiere_deck.modules.files.module", fromlist=["move_file"]).move_file

        def flaky(source, target):
            if source == a:
                raise OSError(13, "in use", source, 32)  # errno, text, file, winerror
            real(source, target)

        with mock.patch("premiere_deck.modules.files.module.move_file", side_effect=flaky):
            result = self.press("files.sort")
        self.assertEqual(result, ("sort", "warn", "Sorted 1 of 2 files · 1 failed", "a.mp4: file in use"))
        failed = next(e for e in self.journal() if e["op"] == "sorted" and e["status"] == "failed")
        self.assertEqual(failed["error"], "file in use")
        self.assertTrue(os.path.isfile(a))
        # a relink failure moves the file back
        self.pr.relink_result = False
        result = self.press("files.sort")
        self.assertEqual(result, ("sort", "warn", "Sorted 0 of 1 files · 1 failed", "a.mp4: relink failed"))
        self.assertTrue(os.path.isfile(a))
        self.assertFalse(os.path.exists(self.in_root("Footage", "a.mp4")))

    def test_write_check_and_stop(self):
        loose = self.video("loose.mp4")
        self.place(ProjectItem("l", "loose.mp4", "clip", media_path=loose))
        with mock.patch("premiere_deck.modules.files.module.write_check", return_value="disk full"):
            self.assertEqual(self.press("files.sort"),
                             ("sort", "error", "No write access to the project folder", "disk full"))
        self.host.jobs.manual = True
        self.module.on_action("files.sort")
        self.module.on_action("files.stop")
        self.host.jobs.step()
        self.assertEqual(self.host.ui.results[-1], ("sort", "neutral", "Sort stopped · 0 of 0 filed", None))
        self.assertTrue(os.path.isfile(loose))
        self.assertEqual(self.press("files.stop"), ("stop", "neutral", "Nothing to stop", None))


class Sync(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.host.ui.results.clear()

    def test_sync_all_copies_flat_into_media_and_undo(self):
        a = self.ext("a.mp4", 3000)
        b = write_file(os.path.join(self.downloads, "sub", "a.mp4"), 2000, "other")
        c = self.ext("pexels-1.mp4", 1000)
        self.place(ProjectItem("a", "a.mp4", "clip", tree_path="Inbox", media_path=a),
                   ProjectItem("b", "a.mp4 (other)", "clip", media_path=b),
                   ProjectItem("c", "pexels-1.mp4", "clip", media_path=c))
        self.assertEqual(self.press("files.sync_all"), ("sync_all", "ok", "Synced 3 files into Media · 5.9 KB", None))
        self.assertEqual(sorted(os.listdir(self.in_root("Media"))), ["a (2).mp4", "a.mp4", "pexels-1.mp4"])
        self.assertEqual({self.item(n).tree_path for n in "abc"}, {"Media"})
        self.assertEqual(self.item("c").media_path, self.in_root("Media", "pexels-1.mp4"))  # no classification
        self.assertTrue(os.path.isfile(a))
        entries = [e for e in self.journal() if e["op"] == "synced"]
        self.assertTrue(all(e["status"] == "done" and e["copied"] for e in entries))
        self.press("files.undo", row=entries[0]["id"])
        self.assertEqual((self.item("a").media_path, self.item("a").tree_path), (a, "Inbox"))
        self.assertFalse(os.path.exists(self.in_root("Media", "a.mp4")))
        # Sort files the synced media from Media into the layout
        self.host.tools.infos[self.in_root("Media", "pexels-1.mp4")] = info(self.in_root("Media", "pexels-1.mp4"),
                                                                          "video", duration=5.0, has_audio=True)
        self.assertEqual(self.press("files.sort")[1], "ok")
        self.assertEqual(self.item("c").media_path, self.in_root("Stock", "pexels-1.mp4"))

    def test_offline_items_are_counted_and_equal_files_reused(self):
        a = self.ext("a.mp4", 3000, "same")
        write_file(self.in_root("Media", "a.mp4"), 3000, "same")
        self.place(ProjectItem("a", "a.mp4", "clip", media_path=a),
                   ProjectItem("o", "lost.wav", "clip", media_path=os.path.join(self.downloads, "lost.wav"),
                               is_offline=True))
        result = self.press("files.sync_all")
        self.assertEqual(result, ("sync_all", "warn", "Synced 1 of 1 files · 1 offline", "Offline: lost.wav"))
        self.assertFalse(self.journal()[-1]["copied"])
        self.assertEqual(os.listdir(self.in_root("Media")), ["a.mp4"])

    def test_sync_used_takes_only_items_in_sequences(self):
        used, unused = self.ext("used.mp4"), self.ext("unused.mp4")
        self.place(ProjectItem("u", "used.mp4", "clip", media_path=used),
                   ProjectItem("n", "unused.mp4", "clip", media_path=unused))
        self.assertEqual(self.press("files.sync_used"),
                         ("sync_used", "neutral", "No used media outside the project folder", None))
        self.pr.sequences_value = [SequenceInfo("seq1", "Main", 1920, 1080, 25.0, 10.0)]
        self.pr.track_items_value = {"seq1": [TrackItem("seq1", "video", 0, 0, "used.mp4", "u", 0, 1, 0, 1)]}
        self.assertEqual(self.press("files.sync_used")[:2], ("sync_used", "ok"))
        self.assertEqual(self.item("u").media_path, self.in_root("Media", "used.mp4"))
        self.assertEqual(self.item("n").media_path, unused)

    def test_nothing_free_space_and_stop(self):
        self.assertEqual(self.press("files.sync_all"),
                         ("sync_all", "neutral", "Nothing outside the project folder", None))
        a = self.ext("big.mp4", 5000)
        self.place(ProjectItem("a", "big.mp4", "clip", media_path=a))
        with mock.patch("premiere_deck.modules.files.module.free_bytes", return_value=100):
            self.assertEqual(self.press("files.sync_all"),
                             ("sync_all", "error", "Not enough free space · needs 4.9 KB", None))
        self.assertFalse(os.path.exists(self.in_root("Media", "big.mp4")))
        self.host.jobs.manual = True
        self.module.on_action("files.sync_all")
        self.module.on_action("files.stop")
        self.host.jobs.step()
        self.assertEqual(self.host.ui.results[-1], ("sync_all", "neutral", "Sync stopped · 0 of 1 copied", None))

    def test_copy_failure_reason(self):
        a = self.ext("a.mp4")
        self.place(ProjectItem("a", "a.mp4", "clip", media_path=a))
        with mock.patch("premiere_deck.modules.files.module.copy_file", side_effect=OSError(28, "No space left")):
            result = self.press("files.sync_all")
        self.assertEqual(result, ("sync_all", "warn", "Synced 0 of 1 files · 1 failed", "a.mp4: disk full"))
        self.assertEqual(self.journal()[-1]["error"], "disk full")


class MakeZip(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.host.ui.results.clear()

    def test_zip_is_refused_with_media_outside_the_root(self):
        self.place(ProjectItem("x", "ext.mp4", "clip", media_path=self.ext("ext.mp4")))
        result = self.press("files.zip")
        self.assertEqual(result, ("zip", "warn", "Sync all media first · 1 file outside the folder", None))
        self.assertFalse(os.path.exists(self.in_root("Exports", "Sample.zip")))

    def test_zip_in_exports_with_report_and_offline(self):
        import zipfile

        inside = write_file(self.in_root("Footage", "a.mp4"), 2000)
        self.place(ProjectItem("a", "a.mp4", "clip", media_path=inside))
        result = self.press("files.zip")
        self.assertEqual(result[:2], ("zip", "ok"))
        self.assertTrue(result[2].startswith("ZIP ready · "), result)
        archive_path = self.in_root("Exports", "Sample.zip")
        self.assertEqual(result[3], archive_path)
        self.assertFalse(os.path.exists(self.root + ".zip"))  # nothing is written beside the root any more
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
        self.assertIn("Sample/Footage/a.mp4", names)
        self.assertFalse([n for n in names if n.startswith("Sample/Exports/")], names)  # the Exports folder stays out
        self.assertTrue(os.path.isfile(self.in_root("Exports", "Sample report.txt")))  # the report sits beside it
        entry = self.journal()[-1]
        self.assertEqual((entry["op"], entry["status"], entry["to"]), ("zipped", "done", archive_path))
        self.place(ProjectItem("o", "b.wav", "clip", media_path=self.in_root("Audio", "b.wav"), is_offline=True))
        result = self.press("files.zip")
        self.assertEqual(result[:3], ("zip", "warn", "ZIP ready · 1 offline: b.wav"))
        self.assertTrue(os.path.isfile(self.in_root("Exports", "Sample (2).zip")))
        self.assertTrue(os.path.isfile(self.in_root("Exports", "Sample (2) report.txt")))
        with zipfile.ZipFile(self.in_root("Exports", "Sample (2).zip")) as archive:
            self.assertFalse([n for n in archive.namelist() if "Sample.zip" in n or "report.txt" in n])  # no nesting
        self.assertEqual(self.press("files.undo_last")[1], "neutral")  # zipped entries are not undoable

    def test_write_failure_and_stop(self):
        write_file(self.in_root("Footage", "a.mp4"), 2000)
        with mock.patch("premiere_deck.modules.files.module.make_zip", side_effect=OSError(28, "No space")):
            result = self.press("files.zip")
        self.assertEqual(result, ("zip", "error", "Could not write the ZIP · disk full", None))
        self.assertEqual((self.journal()[-1]["status"], self.journal()[-1]["error"]), ("failed", "disk full"))
        self.host.jobs.manual = True
        self.module.on_action("files.zip")
        self.module.on_action("files.stop")
        self.host.jobs.step()
        self.assertEqual(self.host.ui.results[-1][:2], ("zip", "neutral"))
        self.assertTrue(self.host.ui.results[-1][2].startswith("ZIP stopped · "))
        self.assertFalse(any(name.endswith(".part") for name in os.listdir(self.in_root("Exports"))))


class Undo(Base):
    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)

    def test_nothing_to_undo(self):
        self.assertEqual(self.press("files.undo_last"), ("undo_last", "neutral", "Nothing to undo", None))
        self.assertTrue(find(self.module.render(), "files.undo_last")["disabled"])

    def test_undo_filed(self):
        src = self.ext("u.mp4", 5000)
        item = self.user_import(src, "Inbox")
        copy = self.in_root("Footage", "u.mp4")
        self.assertEqual(self.press("files.undo_last"), ("undo_last", "ok", "Undone · filed u.mp4", None))
        self.assertEqual(self.item(item.node_id).media_path, src)
        self.assertEqual(self.item(item.node_id).tree_path, "Inbox")
        self.assertFalse(os.path.exists(copy))
        self.assertTrue(os.path.isfile(src))
        last = self.journal()[-1]
        self.assertEqual((last["status"], last["undo_of"], last["deleted"]), ("undone", self.journal()[-2]["id"], True))
        self.assertEqual(self.press("files.undo_last")[2], "Nothing to undo")
        # the module's own changes during undo do not re-file the item
        self.fire("project.changed")
        self.assertEqual(self.item(item.node_id).media_path, src)

    def test_undo_filed_keeps_a_copy_the_user_changed(self):
        src = self.ext("v.mp4", 5000)
        self.user_import(src)
        write_file(self.in_root("Footage", "v.mp4"), 5000, "edited by the user")
        self.press("files.undo_last")
        self.assertTrue(os.path.isfile(self.in_root("Footage", "v.mp4")))
        self.assertFalse(self.journal()[-1]["deleted"])

    def test_undo_filed_when_original_is_gone(self):
        src = self.ext("w.mp4")
        self.user_import(src)
        os.remove(src)
        result = self.press("files.undo_last")
        self.assertEqual(result[1], "warn")
        self.assertTrue(result[2].startswith("Cannot undo · the original file is no longer at"))

    def test_undo_binned(self):
        path = write_file(self.in_root("Graphics", "g.png"))
        item = self.user_import(path, "Misc")
        self.press("files.undo_last")
        self.assertEqual(self.item(item.node_id).tree_path, "Misc")

    def test_undo_imported_removes_the_item_and_keeps_the_file(self):
        self.host.settings.project.values["watch"] = True
        path = write_file(self.in_root("Footage", "drop.mp4"))
        self.module._apply_disk_change(self.module._state, {"type": "created", "path": path})
        created = [i for i in self.pr.snapshot_value.items if i.media_path == path]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].tree_path, "Footage")
        self.press("files.undo_last")
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
        self.press("files.undo_last")
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
        self.press("files.undo_last")
        self.assertEqual((self.item("m1").tree_path, self.item("m1").media_path), ("Footage", old))

    def test_undo_moved_bin_moves_the_file_back(self):
        path = write_file(self.in_root("Footage", "mb.mp4"), 7000)
        self.place(ProjectItem("mb", "mb.mp4", "clip", tree_path="Footage", media_path=path))
        self.user_move_bin("mb", "Overlays")
        moved = self.in_root("Overlays", "mb.mp4")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.press("files.undo_last")[1], "ok")
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(moved))
        self.assertEqual((self.item("mb").media_path, self.item("mb").tree_path), (path, "Footage"))

    def test_undo_of_a_1x_moved_bin_copy_recreates_the_deleted_copy(self):
        src = self.ext("old.mp4", 7000)
        item = self.user_import(src)
        first = self.in_root("Footage", "old.mp4")
        moved = self.in_root("Overlays", "old.mp4")
        os.replace(first, moved)  # what a 1.x bin move left: a copy in Overlays, the first copy deleted
        journal = self.module._state.journal
        journal.append("moved_bin", status="done", node_id=item.node_id, name="old.mp4", **{"from": first}, to=moved,
                       bin_from="Footage", bin_to="Overlays", hash=self.journal()[-1]["hash"], copied=True,
                       deleted_from=True)
        self.pr._replace(item.node_id, media_path=moved, tree_path="Overlays")
        self.module._take_snapshot(self.module._state)
        self.assertEqual(self.press("files.undo_last")[1], "ok")
        self.assertTrue(os.path.isfile(first))
        self.assertFalse(os.path.exists(moved))
        self.assertTrue(self.journal()[-1]["recreated"])
        self.press("files.undo_last")  # and the filed entry below goes back to the original
        self.assertEqual(self.item(item.node_id).media_path, src)

    def test_undo_by_row_from_the_host_window_and_limit(self):
        paths = [write_file(self.in_root("Graphics", f"g{i}.png")) for i in range(3)]
        items = [self.user_import(p, "Misc") for p in paths]
        entries = [e for e in self.journal() if e["op"] == "binned"]
        self.assertEqual(self.press("files.undo", row=entries[0]["id"])[:2], ("undo", "ok"))
        self.assertEqual(self.item(items[0].node_id).tree_path, "Misc")
        self.assertEqual(self.item(items[1].node_id).tree_path, "Graphics")
        self.assertEqual(self.press("files.undo", row="nope")[1], "warn")
        self.module.on_action("files.host:journal_all")
        undo_rows = [r["id"] for r in self.block("journal")["rows"] if r["actions"]]
        self.assertEqual(set(undo_rows), {entries[1]["id"], entries[2]["id"]})
        self.module.UNDO_LIMIT = 1
        self.assertEqual(self.press("files.undo", row=entries[1]["id"])[1], "warn")

    def test_undo_when_the_item_was_deleted(self):
        src = self.ext("del.mp4")
        item = self.user_import(src)
        self.pr.set_items([i for i in self.pr.snapshot_value.items if i.node_id != item.node_id])
        self.assertEqual(self.press("files.undo_last")[2], "Cannot undo · the item is no longer in the project")


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
        jobs = len(self.host.jobs.jobs)
        self.module._on_disk_change(self.module._state, {"type": "created", "path": self.in_root("Footage", "copy (2).mp4")})
        self.assertEqual(len(self.host.jobs.jobs), jobs + 1)  # a different path is not suppressed

    def test_sort_moves_do_not_reach_the_watcher(self):
        loose = write_file(self.in_root("C0001.MP4"))
        self.place(ProjectItem("a", "C0001.MP4", "clip", media_path=loose))
        self.press("files.sort")
        before = len(self.host.jobs.jobs)
        self.module._on_disk_change(self.module._state, {"type": "moved", "src": loose,
                                                         "path": self.in_root("Footage", "C0001.MP4")})
        self.assertEqual(len(self.host.jobs.jobs), before)
        self.assertFalse(self.module._suppress.contains(("disk", norm(loose))))

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
        self.assertTrue(self.module._watch_ignored(state, self.in_root("Sample Masks", "x.prmf")))
        self.assertTrue(self.module._watch_ignored(state, self.in_root("Sample Masks")))
        self.assertFalse(self.module._watch_ignored(state, self.in_root("Footage", "a.mp4")))
        self.assertFalse(self.module._watch_accepts(state, self.in_root("Footage", "notes.txt")))
        self.assertTrue(self.module._watch_accepts(state, self.in_root("Footage", "a.MP4")))
        self.host.settings.project.values["import_exports"] = True
        self.assertFalse(self.module._watch_ignored(state, self.in_root("Exports", "final.mp4")))

    def test_ignored_files_get_no_journal_entry(self):
        self.host.settings.project.values["watch"] = True
        before = len(self.journal())
        for path in (write_file(self.in_root("Sample Masks", "m.prmf")), write_file(self.in_root("Footage", "a.prin")),
                     write_file(self.in_root("Footage", "readme.txt"))):
            self.module._apply_disk_change(self.module._state, {"type": "created", "path": path})
        self.assertEqual(len(self.journal()), before)
        self.assertEqual(self.pr.calls_named("import_files"), [])
        self.assertEqual(self.toasts(), [])


class SettingsAndRecovery(Base):
    def test_hand_edited_broken_project_settings_fall_back(self):
        self.make_module(enabled=False, enforce_bins="yes", mirror_bins=None, import_exports=3, watch="on",
                         pack_zip=[], overrides={"bad": 1}, layout="nonsense")
        self.host.settings.values.update(copy_buffer_mb=-4, stable_seconds="2", toast_filed="no",
                                         default_layout="missing")
        m = self.module
        self.assertEqual([m._proj(k) for k in ("enforce_bins", "mirror_bins", "import_exports", "watch")],
                         [True, True, False, True])
        self.assertEqual(m._proj("overrides"), [])
        self.assertEqual(m._proj("layout"), "default")
        self.assertEqual(m._host("copy_buffer_mb"), 16)
        self.assertEqual(m._host("stable_seconds"), 2)
        self.assertIs(m._host("toast_filed"), True)
        self.assertEqual(m._host("default_layout"), "default")
        self.host.settings.project.values["enabled"] = "true"
        self.assertFalse(m._enabled())
        self.assertEqual(self.host.settings.project.values["pack_zip"], [])  # a 1.x key is ignored and kept
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
        self.assertEqual(self.module.render_host()["status"]["text"], "Paused")

    def test_custom_layout_is_used(self):
        deck = os.path.join(self.root, ".premiere-deck")
        os.makedirs(deck)
        custom = {"id": "custom", "name": "Mine", "folders": ["Video", "Sound"], "project_dir": "Project",
                  "rules": [{"match": {"kind": "audio"}, "to": "Sound"}, {"match": {}, "to": "Video"}]}
        with open(os.path.join(deck, "layout.json"), "w", encoding="utf-8") as handle:
            json.dump(custom, handle)
        self.make_module(enabled=True, watch=False, layout="custom")
        self.assertIn("Media", self.module._state.layout.folders)  # a 1.x layout gets the Sync folder
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
        moved_to = write_file(self.in_root("Stock", "pexels-9.mp4"))
        moved_from = self.in_root("pexels-9.mp4")
        undone_to = write_file(self.in_root("Stock", "pexels-8.mp4"))
        undone_from = self.in_root("pexels-8.mp4")
        zip_part = write_file(self.root + ".zip.part", 10)
        self.pr.set_items([ProjectItem("h1", "half.mp4", "clip", media_path=src),
                           ProjectItem("s1", "stale.mp4", "clip", media_path=stale),
                           ProjectItem("r1", "relinked.mp4", "clip", tree_path="Footage", media_path=relinked),
                           ProjectItem("m1", "pexels-9.mp4", "clip", media_path=moved_from),
                           ProjectItem("m2", "pexels-8.mp4", "clip", media_path=undone_from)])
        e1 = journal.append("filed", node_id="h1", name="half.mp4", **{"from": src}, to=done_copy, bin_from="",
                            bin_to="Footage", copied=True)
        e2 = journal.append("filed", node_id="s1", name="stale.mp4", **{"from": stale}, to=self.in_root("Footage", "stale.mp4"),
                            bin_from="", bin_to="Footage", copied=True)
        e3 = journal.append("filed", node_id="r1", name="relinked.mp4", **{"from": relinked_src}, to=relinked,
                            bin_from="", bin_to="Footage", copied=True)
        e4 = journal.append("sorted", node_id="m1", name="pexels-9.mp4", **{"from": moved_from}, to=moved_to,
                            bin_from="", bin_to="Stock", copied=False, moved=True)
        write_file(undone_from)  # the move did not happen yet: both exist, the source still holds the file
        e5 = journal.append("sorted", node_id="m2", name="pexels-8.mp4", **{"from": undone_from}, to=undone_to,
                            bin_from="", bin_to="Stock", copied=False, moved=True)
        e6 = journal.append("zipped", name="Sample.zip", to=self.root + ".zip")
        self.module.project_opened(self.project)
        state = self.module._state.journal
        self.assertEqual(state.get(e1["id"])["status"], "done")
        self.assertTrue(state.get(e1["id"])["resumed"])
        self.assertEqual((self.item("h1").media_path, self.item("h1").tree_path), (done_copy, "Footage"))
        self.assertEqual(state.get(e2["id"])["status"], "failed")
        self.assertFalse(os.path.exists(self.in_root("Footage", "stale.mp4.part")))
        self.assertEqual(state.get(e3["id"])["status"], "done")
        self.assertTrue(state.get(e3["id"])["recovered"])
        self.assertEqual((state.get(e4["id"])["status"], self.item("m1").media_path), ("done", moved_to))
        self.assertEqual(state.get(e5["id"])["status"], "failed")
        self.assertTrue(os.path.isfile(undone_from) and os.path.isfile(undone_to))  # nothing overwritten
        self.assertEqual(state.get(e6["id"])["status"], "failed")
        self.assertFalse(os.path.exists(zip_part))

    def test_clean_up_drops_failed_entries_of_ignored_files_once(self):
        self.make_module(enabled=True, watch=False)
        self.module.project_closed()
        path = os.path.join(self.root, ".premiere-deck", "journal.jsonl")
        journal = J.Journal(path)
        mask = journal.append("imported", name="a.prmf", to=self.in_root("Sample Masks", "a.prmf"), bin_to="Sample Masks")
        journal.mark_failed(mask["id"], "nothing imported")
        other = journal.append("imported", name="x.prin", to=self.in_root("Footage", "x.prin"))
        journal.mark_failed(other["id"], "nothing imported")
        real = journal.append("imported", name="clip.mp4", to=self.in_root("Footage", "clip.mp4"))
        journal.mark_failed(real["id"], "nothing imported")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{torn line\n")
        self.module.project_opened(self.project)
        ids = [e["id"] for e in self.journal()]
        self.assertNotIn(mask["id"], ids)
        self.assertNotIn(other["id"], ids)
        self.assertIn(real["id"], ids)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("{torn line", content)
        self.assertNotIn("prmf", content)
        stamp = os.stat(path).st_mtime_ns
        self.module.project_closed()
        self.module.project_opened(self.project)
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)  # nothing matches: the file is not touched

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
        result = self.press("files.locate", row="o1")
        self.assertEqual(result[:2], ("locate", "ok"))
        self.assertEqual(self.host.host.revealed, [self.in_root("Footage")])
        self.assertEqual(self.press("files.locate", row="zz")[1], "warn")
        self.assertEqual(self.press("files.remove", row="o1"), ("remove", "ok", "Removed lost.mp4 from the project", None))
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
        self.assertEqual(self.press("files.remove", row="o2")[1], "error")

    def test_open_folder_and_journal(self):
        self.assertEqual(self.press("files.open_folder")[:2], ("open_folder", "ok"))
        self.assertEqual(self.host.host.opened, [self.root])
        self.assertEqual(self.press("files.journal")[:2], ("journal", "ok"))
        self.assertEqual(self.host.host.shown, ["files"])

    def test_watch_setting_starts_and_stops_the_watcher(self):
        self.module.on_setting_changed("watch", "project", True)
        self.assertIsNotNone(self.module._state.watcher)
        self.assertTrue(self.host.settings.project.values["watch"])
        self.module.on_setting_changed("watch", "project", False)
        self.assertIsNone(self.module._state.watcher)
        self.assertFalse(self.host.settings.project.values["watch"])

    def test_host_project_setting(self):
        self.assertIsNone(self.module.on_action("files.host:project_setting", {"key": "enforce_bins", "value": False}))
        self.assertFalse(self.host.settings.project.values["enforce_bins"])
        self.module.on_action("files.host:project_setting", {"mirror_bins": False})
        self.assertFalse(self.host.settings.project.values["mirror_bins"])
        self.assertEqual(self.module.on_action("files.host:project_setting", {"key": "pack_zip", "value": True}).toast[0],
                         "warn")
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
        bad = dict(layout, rules=[{"match": {}, "to": "Media"}])
        self.assertIn("Sync folder", self.module.on_action("files.host:layout_save", {"layout": bad}).toast[1])
        self.assertEqual(self.module.on_action("files.host:layout_reset").toast[0], "ok")
        self.assertEqual(self.module._state.layout.id, "default")
        self.assertIsNone(self.module.on_action("files.host:layout_select", {"value": "custom"}))
        self.assertIn("B-Roll", self.module._state.layout.folders)
        self.assertEqual(self.module.on_action("files.host:layout_select", "unknown").toast[0], "warn")

    def test_host_overrides_journal_filter_reveal(self):
        result = self.module.on_action("files.host:overrides_save",
                                       [{"source": self.downloads, "to": "Audio/SFX"}, {"to": "x"},
                                        {"name": "*", "to": "Media"}])
        self.assertEqual(result.toast, ("warn", "Overrides saved · 2 invalid skipped"))
        item = self.user_import(self.ext("long_track.wav"))
        self.assertEqual(self.item(item.node_id).tree_path, "Audio/SFX")
        self.assertEqual(self.module.on_action("files.host:overrides_save", "x").toast[0], "warn")
        self.module.on_action("files.host:journal_all")
        self.module.on_action("files.host:journal_filter", {"op": "filed", "query": "long"})
        journal = self.block("journal")
        self.assertEqual([r["name"] for r in journal["rows"]], ["long_track.wav"])
        self.module.on_action("host:journal_op", "")  # the registry passes window ids without the prefix
        self.module.on_action("host:journal_query", "")
        journal = self.block("journal")
        self.assertGreater(len(journal["rows"]), 1)
        self.assertEqual(journal["filters"][0]["id"], "files.host:journal_op")
        self.assertEqual(journal["search"]["id"], "files.host:journal_query")
        self.assertFalse(self.module.on_action("files.host:reveal").rerender)
        self.assertEqual(self.host.host.revealed[-1], self.root)

    def test_unknown_and_removed_actions(self):
        self.assertEqual(self.module.on_action("files.nothing").toast[0], "warn")
        self.assertEqual(self.module.on_action("files.host:nothing").toast[0], "warn")
        before = (len(self.host.ui.results), len(self.host.ui.toasts))
        for old in ("files.collect", "files.collect_run", "files.collect_discard", "files.pack", "files.cancel"):
            result = self.module.on_action(old)
            self.assertIsNone(result.toast)
            self.assertFalse(result.rerender)
        self.assertEqual((len(self.host.ui.results), len(self.host.ui.toasts)), before)

    def test_render_host_blocks(self):
        src = self.ext("a.mp4")
        for index in range(5):
            write_file(self.in_root("Graphics", f"g{index}.png"))
            self.user_import(self.in_root("Graphics", f"g{index}.png"), "Misc")
        self.place(ProjectItem("x", "a.mp4", "clip", media_path=src))
        page = self.module.render_host()
        self.assertEqual((page["title"], page["subtitle"]), ("Files", self.root))
        self.assertEqual(page["header_actions"][0]["variant"], "icon")
        ids = [b["id"] for b in page["blocks"]]
        self.assertEqual(ids[:3], ["status_chips", "main", "auto"])
        self.assertEqual(self.block("last_result")["rows"][0]["title"], "Project set up · 13 folders")
        for block_id in ("journal", "journal_actions", "layout", "layout_folders", "layout_rules", "overrides",
                         "override_add"):
            self.assertIn(block_id, ids)
        for gone in ("collect_plan", "pack_options", "cards", "status", "project_settings"):
            self.assertNotIn(gone, ids)
        main = self.block("main")
        self.assertEqual((main["tiles"], main["columns"], [b["id"] for b in main["items"]]), (True, 2, TILES))
        journal = self.block("journal")
        self.assertEqual(len(journal["rows"]), 3)
        self.assertNotIn("filters", journal)
        self.assertEqual(self.block("journal_actions")["items"][0]["label"], "Show all")
        self.module.on_action("files.host:journal_all")
        self.assertGreater(len(self.block("journal")["rows"]), 3)
        self.assertEqual(self.block("journal_actions")["items"][0]["label"], "Show fewer")
        self.press("files.sync_all")
        last = self.block("last_result")
        self.assertEqual(last["rows"][0]["tone"], "ok")
        self.assertTrue(last["rows"][0]["title"].startswith("Synced 1 file into Media"))
        self.assertEqual(last["rows"][0]["actions"][0]["id"], "files.undo_last")
        self.check_render()

    def test_override_add_and_remove(self):
        result = self.module.on_action("host:override_add", {"match": self.downloads, "to": "Audio/SFX"})
        self.assertEqual(result.toast[0], "ok")
        self.module.on_action("host:override_add", {"match": "*_vo*", "to": "Audio/Voice"})
        self.assertEqual(self.host.settings.project.values["overrides"],
                         [{"source": self.downloads, "to": "Audio/SFX"}, {"name": "*_vo*", "to": "Audio/Voice"}])
        self.assertEqual(self.module.on_action("host:override_add", {"match": "", "to": "x"}).toast[0], "warn")
        self.assertEqual(self.module.on_action("host:override_add", {"match": "*a*", "to": "../x"}).toast[0], "warn")
        self.assertEqual(self.module.on_action("host:override_add", {"match": "*a*", "to": "Media"}).toast[0], "warn")
        table = self.block("overrides")
        self.assertEqual([r["match"] for r in table["rows"]], [self.downloads, "*_vo*"])
        self.assertNotIn("Media", [o["value"] for o in self.block("override_add")["fields"][1]["options"]])
        self.assertEqual(self.module.on_action("host:override_remove", row="0").toast, ("ok", "Override removed"))
        self.assertEqual(self.host.settings.project.values["overrides"], [{"name": "*_vo*", "to": "Audio/Voice"}])
        self.assertEqual(self.module.on_action("host:override_remove", row="7").toast[0], "warn")


if __name__ == "__main__":
    unittest.main()
