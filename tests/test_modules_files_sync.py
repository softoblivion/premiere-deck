import errno
import json
import os
import unittest
import zipfile

from premiere_deck.modules.files import journal as J
from premiere_deck.modules.files.layout import norm
from unittest import mock

from premiere_deck.modules.files import sync as S
from premiere_deck.modules.files.sync import (
    CopyCancelled, CopyError, Suppression, build_zip_report, copy_file, failure_reason, fmt_size, free_zip_path,
    make_zip, move_file, pack_files, resolve_target, write_check,
)
from premiere_deck.premiere.model import ProjectItem
from tests.test_modules_files_support import remove_tree, scratch, write_file


class Copies(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("files-sync-")
        self.src = write_file(os.path.join(self.dir, "src", "clip.mp4"), 300_000, "a")
        self.dest = os.path.join(self.dir, "dest")

    def tearDown(self):
        remove_tree(self.dir)

    def test_copy_keeps_size_and_time_and_leaves_no_part(self):
        os.utime(self.src, (1_600_000_000, 1_600_000_000))
        target = os.path.join(self.dest, "clip.mp4")
        seen = []
        copied = copy_file(self.src, target, buffer_bytes=65536, on_bytes=seen.append)
        self.assertEqual(copied, 300_000)
        self.assertEqual(sum(seen), 300_000)
        self.assertEqual(os.path.getsize(target), 300_000)
        self.assertEqual(int(os.path.getmtime(target)), 1_600_000_000)
        self.assertFalse(os.path.exists(target + ".part"))
        self.assertTrue(os.path.exists(self.src))  # the original stays

    def test_copy_never_overwrites(self):
        target = write_file(os.path.join(self.dest, "clip.mp4"), 10, "other")
        with self.assertRaises(CopyError):
            copy_file(self.src, target)
        self.assertEqual(os.path.getsize(target), 10)

    def test_cancelled_copy_removes_the_part_file(self):
        target = os.path.join(self.dest, "clip.mp4")
        calls = []
        with self.assertRaises(CopyCancelled):
            copy_file(self.src, target, buffer_bytes=65536, on_bytes=calls.append, cancelled=lambda: bool(calls))
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_collision_equal_is_reused(self):
        write_file(os.path.join(self.dest, "clip.mp4"), 300_000, "a")
        target, reuse, digest = resolve_target(self.src, self.dest)
        self.assertEqual(target, os.path.join(self.dest, "clip.mp4"))
        self.assertTrue(reuse)
        self.assertTrue(digest)

    def test_collision_different_gets_a_suffix(self):
        write_file(os.path.join(self.dest, "clip.mp4"), 300_000, "b")
        write_file(os.path.join(self.dest, "clip (2).mp4"), 12, "c")
        target, reuse, _ = resolve_target(self.src, self.dest)
        self.assertEqual(target, os.path.join(self.dest, "clip (3).mp4"))
        self.assertFalse(reuse)
        # a suffixed file with equal content is reused
        write_file(os.path.join(self.dest, "clip (3).mp4"), 300_000, "a")
        target, reuse, _ = resolve_target(self.src, self.dest)
        self.assertEqual(target, os.path.join(self.dest, "clip (3).mp4"))
        self.assertTrue(reuse)

    def test_reserved_targets_of_a_plan(self):
        other = write_file(os.path.join(self.dir, "src2", "clip.mp4"), 1000, "z")
        reserved = {}
        first, reuse1, h1 = resolve_target(self.src, self.dest, reserved=reserved)
        reserved[norm(first)] = h1 or "h"
        second, reuse2, _ = resolve_target(other, self.dest, reserved=reserved)
        self.assertFalse(reuse1)
        self.assertFalse(reuse2)
        self.assertNotEqual(norm(first), norm(second))
        self.assertTrue(second.endswith("clip (2).mp4"))

    def test_move_never_overwrites(self):
        target = os.path.join(self.dest, "moved.mp4")
        move_file(self.src, target)
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(os.path.getsize(target), 300_000)
        other = write_file(os.path.join(self.dir, "src", "other.mp4"), 10, "o")
        with self.assertRaises(CopyError):
            move_file(other, target)
        self.assertTrue(os.path.isfile(other))

    def test_failure_reasons(self):
        self.assertEqual(failure_reason(PermissionError(13, "denied")), "access denied")
        self.assertEqual(failure_reason(OSError(13, "x", "f", 5)), "access denied")
        self.assertEqual(failure_reason(OSError(1, "x", "f", 1314)), "access denied")
        self.assertEqual(failure_reason(OSError(13, "x", "f", 32)), "file in use")
        self.assertEqual(failure_reason(OSError(13, "x", "f", 33)), "file in use")
        self.assertEqual(failure_reason(OSError(28, "No space left")), "disk full")
        self.assertEqual(failure_reason(OSError(22, "x", "f", 112)), "disk full")
        self.assertEqual(failure_reason(OSError(22, "x", "f", 39)), "disk full")
        self.assertEqual(failure_reason(OSError(22, "x", "f", 206)), "path too long")
        self.assertEqual(failure_reason(OSError(errno.ENAMETOOLONG, "File name too long")), "path too long")
        self.assertEqual(failure_reason(FileNotFoundError(2, "gone")), "source not found")
        self.assertEqual(failure_reason(OSError(5, "io")), "copy error")
        self.assertEqual(failure_reason(CopyCancelled()), "cancelled")
        long_target = "C:\\" + "x" * 300 + ".mp4"
        with mock.patch.object(S, "long_paths_enabled", return_value=False):
            self.assertEqual(failure_reason(FileNotFoundError(2, "gone"), target=long_target), "path too long")
        with mock.patch.object(S, "long_paths_enabled", return_value=True):
            self.assertEqual(failure_reason(FileNotFoundError(2, "gone"), target=long_target), "source not found")

    def test_write_check(self):
        root = os.path.join(self.dir, "root")
        os.makedirs(root)
        self.assertIsNone(write_check(root))
        self.assertEqual(os.listdir(os.path.join(root, ".premiere-deck")), [])  # the temporary file is gone
        with self.assertLogs("module.files.sync", "WARNING"):
            with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
                self.assertEqual(write_check(root), "access denied")
            blocker = write_file(os.path.join(self.dir, "file_root"), 10)
            self.assertIsNotNone(write_check(blocker))  # .premiere-deck cannot be created under a file

    def test_sizes(self):
        self.assertEqual(fmt_size(0), "0 B")
        self.assertEqual(fmt_size(1536), "1.5 KB")
        self.assertEqual(fmt_size(3.2 * 1024 ** 3), "3.2 GB")


class SuppressionSet(unittest.TestCase):
    def test_consumed_once_and_expires(self):
        now = [100.0]
        s = Suppression(ttl=15.0, clock=lambda: now[0])
        s.add(("disk", "a"))
        s.add(("disk", "b"))
        self.assertTrue(s.consume(("disk", "a")))
        self.assertFalse(s.consume(("disk", "a")))
        now[0] += 16
        self.assertFalse(s.consume(("disk", "b")))
        self.assertEqual(len(s), 0)


class Journal(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("files-journal-")
        self.path = os.path.join(self.dir, ".premiere-deck", "journal.jsonl")

    def tearDown(self):
        remove_tree(self.dir)

    def test_append_update_fold_and_reload(self):
        j = J.Journal(self.path)
        entry = j.append(J.OP_FILED, node_id="n1", name="a.mp4", **{"from": "C:/a.mp4"}, to="D:/r/a.mp4", copied=True,
                         hash="h1")
        self.assertEqual(entry["status"], J.PENDING)
        self.assertEqual([e["id"] for e in j.pending()], [entry["id"]])
        j.mark_done(entry["id"], bin_to="Footage")
        again = J.Journal(self.path)
        folded = again.get(entry["id"])
        self.assertEqual(folded["status"], J.DONE)
        self.assertEqual(folded["bin_to"], "Footage")
        self.assertEqual(folded["from"], "C:/a.mp4")
        with open(self.path, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle]
        self.assertEqual(len(lines), 2)
        self.assertEqual(set(lines[1]), {"id", "status", "bin_to"})

    def test_torn_and_hand_edited_lines_are_skipped(self):
        j = J.Journal(self.path)
        first = j.append(J.OP_IMPORTED, status=J.DONE, name="a.wav", to="x")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"id": "broken", "op": \n')
            handle.write("[1, 2]\n\n")
        second = j.append(J.OP_IMPORTED, status=J.DONE, name="b.wav", to="y")
        reloaded = J.Journal(self.path)
        self.assertEqual([e["id"] for e in reloaded.entries()], [first["id"], second["id"]])
        self.assertEqual(reloaded.skipped_lines, 2)

    def test_undoable_window_and_undone_links(self):
        j = J.Journal(self.path)
        ids = [j.append(J.OP_BINNED, status=J.DONE, name=f"{i}", node_id=f"n{i}")["id"] for i in range(60)]
        undoable = j.undoable(50)
        self.assertEqual(len(undoable), 50)
        self.assertEqual(undoable[0]["id"], ids[-1])
        j.append(J.OP_BINNED, status=J.UNDONE, undo_of=ids[-1])
        self.assertEqual(j.undoable(50)[0]["id"], ids[-2])
        j.append(J.OP_REMOVED, status=J.DONE, name="gone")
        self.assertEqual(j.undoable(50)[0]["id"], ids[-2])

    def test_module_copy_hash(self):
        j = J.Journal(self.path)
        e = j.append(J.OP_FILED, to=os.path.join(self.dir, "Footage", "a.mp4"), copied=True, hash="abc")
        self.assertIsNone(j.module_copy_hash(os.path.join(self.dir, "footage", "A.MP4"), norm))
        j.mark_done(e["id"])
        self.assertEqual(j.module_copy_hash(os.path.join(self.dir, "footage", "A.MP4"), norm), "abc")
        reused = j.append(J.OP_FILED, status=J.DONE, to=os.path.join(self.dir, "Footage", "b.mp4"), copied=False,
                          hash="def")
        self.assertIsNone(j.module_copy_hash(reused["to"], norm))

    def test_drop_rewrites_once_and_keeps_other_lines(self):
        j = J.Journal(self.path)
        keep = j.append(J.OP_IMPORTED, name="a.mp4", to="a.mp4")
        drop = j.append(J.OP_IMPORTED, name="m.prmf", to="m.prmf")
        j.mark_failed(drop["id"], "nothing imported")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("{torn\n")
        self.assertEqual(j.drop([]), 0)
        self.assertEqual(j.drop(["unknown"]), 0)
        self.assertEqual(j.drop([drop["id"]]), 1)
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "{torn")
        self.assertEqual([e["id"] for e in J.Journal(self.path).entries()], [keep["id"]])
        later = j.append(J.OP_IMPORTED, name="b.mp4", to="b.mp4")  # appending still works after the rewrite
        self.assertEqual([e["id"] for e in j.entries()], [keep["id"], later["id"]])

    def test_filter(self):
        j = J.Journal(self.path)
        j.append(J.OP_FILED, status=J.DONE, name="Whoosh.wav", to="x")
        j.append(J.OP_IMPORTED, status=J.DONE, name="clip.mp4", to="y")
        self.assertEqual(len(J.filter_entries(j.entries(), J.OP_FILED)), 1)
        self.assertEqual(J.filter_entries(j.entries(), "", "whoosh")[0]["name"], "Whoosh.wav")


class Zip(unittest.TestCase):
    def setUp(self):
        self.base = scratch("files-pack-")
        self.root = os.path.join(self.base, "Spring")
        write_file(os.path.join(self.root, "Spring.prproj"), 100)
        write_file(os.path.join(self.root, "Footage", "a.mp4"), 2000)
        write_file(os.path.join(self.root, ".premiere-deck", "project.json"), 50)
        write_file(os.path.join(self.root, "Adobe Premiere Pro Video Previews", "p.mpeg"), 500)
        write_file(os.path.join(self.root, "Media Cache Files", "m.ims"), 500)
        write_file(os.path.join(self.root, "Footage", "b.mp4.part"), 500)
        write_file(os.path.join(self.root, "Adobe Premiere Pro Auto-Save", "Spring--1.prproj"), 100)

    def tearDown(self):
        remove_tree(self.base)

    def test_pack_files_skip_caches_and_previews(self):
        names = sorted(arc for _, arc, _ in pack_files(self.root))
        self.assertEqual(names, ["Spring/.premiere-deck/project.json", "Spring/Adobe Premiere Pro Auto-Save/Spring--1.prproj",
                                 "Spring/Footage/a.mp4", "Spring/Spring.prproj"])

    def test_zip_is_written_beside_the_root_with_a_free_name(self):
        seen = []
        first = make_zip(self.root, on_file=lambda k, n, name: seen.append((k, n, name)))
        self.assertEqual(first, self.root + ".zip")
        self.assertEqual(seen[0][:2], (1, 4))
        self.assertEqual(seen[-1][0], 4)
        with zipfile.ZipFile(first) as archive:
            self.assertIn("Spring/Footage/a.mp4", archive.namelist())
            self.assertEqual(len(archive.read("Spring/Footage/a.mp4")), 2000)
        self.assertEqual(free_zip_path(self.root), self.root + " (2).zip")
        with self.assertRaises(CopyCancelled):
            make_zip(self.root, cancelled=lambda: True)
        self.assertFalse(os.path.exists(self.root + " (2).zip.part"))
        with self.assertRaises(CopyError):
            make_zip(self.root, first)  # never overwrites

    def test_pack_files_can_leave_one_folder_out(self):
        exports = os.path.join(self.root, "Exports")
        os.makedirs(os.path.join(exports, "Frames"))
        for name in ("old.zip", os.path.join("Frames", "f.png")):
            with open(os.path.join(exports, name), "wb") as handle:
                handle.write(b"x")
        self.assertEqual(sum(arc.startswith("Spring/Exports/") for _, arc, _ in pack_files(self.root)), 2)
        names = [arc for _, arc, _ in pack_files(self.root, exclude_dir=exports)]
        self.assertFalse([n for n in names if n.startswith("Spring/Exports")], names)
        self.assertIn("Spring/Footage/a.mp4", names)

    def test_zip_name_in_a_folder_gets_collision_suffixes(self):
        exports = os.path.join(self.root, "Exports")
        os.makedirs(exports)
        first = free_zip_path(self.root, exports, "Spring")
        self.assertEqual(first, os.path.join(exports, "Spring.zip"))
        make_zip(self.root, first, files=pack_files(self.root, exclude_dir=exports))
        self.assertEqual(free_zip_path(self.root, exports, "Spring"), os.path.join(exports, "Spring (2).zip"))
        with open(os.path.join(exports, "Spring (2).zip.part"), "wb"):
            pass  # a partial archive of that name also counts as taken
        self.assertEqual(free_zip_path(self.root, exports, "Spring"), os.path.join(exports, "Spring (3).zip"))

    def test_report_lists_offline(self):
        inside = os.path.join(self.root, "Footage", "a.mp4")
        items = [ProjectItem("n1", "a.mp4", "clip", media_path=inside),
                 ProjectItem("n1b", "a again", "clip", media_path=inside),
                 ProjectItem("n2", "gone.mp4", "clip", media_path=os.path.join(self.root, "Footage", "gone.mp4"), is_offline=True),
                 ProjectItem("n4", "Seq", "sequence")]
        report = build_zip_report("Spring.prproj", self.root, items, lambda p: p.startswith(self.root))
        self.assertEqual((report.media_items, report.inside, report.total_bytes), (3, 2, 2000))
        self.assertFalse(report.ok)
        self.assertEqual(report.line(), "ZIP ready · 1 offline: gone.mp4")
        self.assertIn("Offline items:", report.text())
        ok = build_zip_report("Spring.prproj", self.root, items[:1], lambda p: True)
        ok.files, ok.archive_bytes = 120, int(3.2 * 1024 ** 3)
        self.assertEqual(ok.line(), "ZIP ready · 120 files · 3.2 GB")


if __name__ == "__main__":
    unittest.main()
