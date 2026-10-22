"""The disk watcher with a real `watchdog` observer on a scratch project folder (docs/FILES.md 3.2).

Timings are shortened (debounce 0.3 s, stable 1 s) and every wait is generous (up to 20 s).
"""

import os
import time
import unittest

from premiere_deck.modules.files.module import FilesModule
from premiere_deck.modules.files.sync import DiskWatcher
from premiere_deck.premiere.model import ProjectItem
from tests.test_modules_files_support import FilesCase, remove_tree, scratch, wait_for, write_file


class FastFiles(FilesModule):
    WATCH_DEBOUNCE = 0.3


class Watcher(FilesCase):
    def setUp(self):
        super().setUp()
        self.host.settings.values["stable_seconds"] = 1
        self.make_module(FastFiles, enabled=True)
        self.assertIsNotNone(self.module._state.watcher)
        time.sleep(0.3)  # let the observer settle on the new folders

    def imports(self):
        return [args for args, _ in self.pr.calls_named("import_files")]

    def items_at(self, path):
        return [i for i in self.pr.snapshot_value.items if i.media_path and os.path.normcase(i.media_path) == os.path.normcase(path)]

    def test_new_file_in_a_layout_folder_is_imported_into_its_bin(self):
        path = write_file(self.in_root("Audio", "SFX", "hit.wav"))
        self.assertTrue(wait_for(lambda: self.imports()))
        self.assertEqual(self.imports(), [((path,), "Audio/SFX")])
        self.assertEqual(self.journal()[-1]["op"], "imported")
        time.sleep(1.5)
        self.assertEqual(len(self.imports()), 1)  # one import per file

    def test_new_subfolder_becomes_a_bin(self):
        path = write_file(self.in_root("Footage", "Day1", "a.mp4"))
        self.assertTrue(wait_for(lambda: self.imports()))
        self.assertEqual(self.imports()[0][1], "Footage/Day1")
        self.assertEqual(len(self.items_at(path)), 1)

    def test_copy_in_progress_is_imported_once_complete(self):
        path = self.in_root("Footage", "slow.mp4")
        with open(path, "wb") as handle:
            for _ in range(6):
                handle.write(b"x" * 100_000)
                handle.flush()
                time.sleep(0.3)
        self.assertTrue(wait_for(lambda: self.imports()))
        self.assertEqual(len(self.imports()), 1)
        self.assertEqual(os.path.getsize(path), 600_000)

    def test_rename_and_move_on_disk_follow_the_item(self):
        path = write_file(self.in_root("Footage", "take1.mp4"))
        self.assertTrue(wait_for(lambda: self.items_at(path)))
        node = self.items_at(path)[0].node_id
        renamed = self.in_root("Footage", "take1_final.mp4")
        os.rename(path, renamed)
        self.assertTrue(wait_for(lambda: self.pr.snapshot_value.by_node_id()[node].media_path == renamed))
        item = self.pr.snapshot_value.by_node_id()[node]
        self.assertEqual(item.name, "take1_final.mp4")
        self.assertEqual(self.journal()[-1]["op"], "renamed")
        moved = self.in_root("Overlays", "take1_final.mp4")
        os.replace(renamed, moved)
        self.assertTrue(wait_for(lambda: self.pr.snapshot_value.by_node_id()[node].media_path == moved))
        self.assertTrue(wait_for(lambda: self.pr.snapshot_value.by_node_id()[node].tree_path == "Overlays"))
        self.assertEqual(self.journal()[-1]["op"], "moved_disk")
        self.assertEqual(len(self.imports()), 1)

    def test_deleted_file_lists_the_item_offline(self):
        path = write_file(self.in_root("Graphics", "logo.png"))
        self.pr.set_items([ProjectItem("d1", "logo.png", "clip", tree_path="Graphics", media_path=path)])
        self.fire("project.changed")
        os.remove(path)
        self.assertTrue(wait_for(lambda: [i.node_id for i in self.module._state.offline] == ["d1"]))
        self.assertIn("d1", self.pr.snapshot_value.by_node_id())  # nothing was done to the project

    def test_ignored_paths_and_own_copies_are_not_imported(self):
        write_file(self.in_root("Exports", "final.mp4"))
        write_file(self.in_root("Footage", "partial.mp4.part"))
        write_file(self.in_root("Adobe Premiere Pro Auto-Save", "Sample--3.prproj"))
        write_file(self.in_root(".premiere-deck", "notes.txt"))
        write_file(self.in_root("loose_at_root.mp4"))
        src = self.ext("external.mp4", 20_000)
        item = self.user_import(src)  # filed: the module's own copy lands in Footage
        copy = self.in_root("Footage", "external.mp4")
        self.assertEqual(self.pr.snapshot_value.by_node_id()[item.node_id].media_path, copy)
        time.sleep(4)
        self.assertEqual(self.imports(), [])
        self.assertEqual(len(self.items_at(copy)), 1)

    def test_import_exports_setting(self):
        self.host.settings.project.values["import_exports"] = True
        path = write_file(self.in_root("Exports", "cut_v2.mp4"))
        self.assertTrue(wait_for(lambda: self.imports()))
        self.assertEqual(self.imports()[0], ((path,), "Exports"))

    def test_paused_watch_does_nothing(self):
        self.module.on_action("files.watch", False)
        write_file(self.in_root("Footage", "while_paused.mp4"))
        time.sleep(3)
        self.assertEqual(self.imports(), [])


class WatcherUnit(unittest.TestCase):
    """DiskWatcher alone: debounce, pairing of delete + create, folder moves."""

    def setUp(self):
        self.dir = scratch("files-watcher-")
        self.changes = []
        self.watcher = DiskWatcher(self.dir, self.changes.append, ignored=lambda p: p.endswith(".part"),
                                   debounce=0.3, stable=0.6, hold=1.0)
        self.watcher.start()
        time.sleep(0.3)

    def tearDown(self):
        self.watcher.stop()
        self.assertFalse(self.watcher.running)
        remove_tree(self.dir)

    def test_temporary_file_is_dropped(self):
        path = os.path.join(self.dir, "tmp.mp4")
        write_file(path)
        os.remove(path)
        time.sleep(2.5)
        self.assertEqual([c for c in self.changes if c["type"] != "deleted"], [])

    def test_part_rename_is_a_created_file(self):
        part = write_file(os.path.join(self.dir, "a.mp4.part"))
        os.rename(part, os.path.join(self.dir, "a.mp4"))
        self.assertTrue(wait_for(lambda: self.changes))
        self.assertEqual(self.changes[0], {"type": "created", "path": os.path.join(self.dir, "a.mp4")})

    def test_move_between_folders_is_one_moved_change(self):
        os.makedirs(os.path.join(self.dir, "A"))
        os.makedirs(os.path.join(self.dir, "B"))
        src = write_file(os.path.join(self.dir, "A", "clip.mp4"))
        self.assertTrue(wait_for(lambda: self.changes))
        self.changes.clear()
        dest = os.path.join(self.dir, "B", "clip.mp4")
        os.replace(src, dest)
        self.assertTrue(wait_for(lambda: self.changes))
        time.sleep(2.0)
        self.assertEqual(self.changes, [{"type": "moved", "path": dest, "src": src}])

    def test_folder_moved_in_from_outside_lists_its_files(self):
        outside = scratch("files-outside-")
        try:
            write_file(os.path.join(outside, "Pack", "one.wav"))
            write_file(os.path.join(outside, "Pack", "two.wav"))
            os.replace(os.path.join(outside, "Pack"), os.path.join(self.dir, "Pack"))
            self.assertTrue(wait_for(lambda: len(self.changes) >= 2))
            self.assertEqual(sorted(os.path.basename(c["path"]) for c in self.changes), ["one.wav", "two.wav"])
            self.assertTrue(all(c["type"] == "created" for c in self.changes))
        finally:
            remove_tree(outside)


if __name__ == "__main__":
    unittest.main()
