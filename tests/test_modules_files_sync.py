import json
import os
import unittest
import zipfile

from premiere_deck.modules.files import journal as J
from premiere_deck.modules.files.layout import norm
from premiere_deck.modules.files.sync import (
    CollectPlan, CopyCancelled, CopyError, PlanEntry, Suppression, build_pack_report, copy_file, fmt_size,
    free_zip_path, make_zip, pack_files, resolve_target,
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

    def test_filter(self):
        j = J.Journal(self.path)
        j.append(J.OP_FILED, status=J.DONE, name="Whoosh.wav", to="x")
        j.append(J.OP_IMPORTED, status=J.DONE, name="clip.mp4", to="y")
        self.assertEqual(len(J.filter_entries(j.entries(), J.OP_FILED)), 1)
        self.assertEqual(J.filter_entries(j.entries(), "", "whoosh")[0]["name"], "Whoosh.wav")


class Pack(unittest.TestCase):
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
        first = make_zip(self.root)
        self.assertEqual(first, self.root + ".zip")
        with zipfile.ZipFile(first) as archive:
            self.assertIn("Spring/Footage/a.mp4", archive.namelist())
            self.assertEqual(len(archive.read("Spring/Footage/a.mp4")), 2000)
        self.assertEqual(free_zip_path(self.root), self.root + " (2).zip")
        with self.assertRaises(CopyCancelled):
            make_zip(self.root, cancelled=lambda: True)
        self.assertFalse(os.path.exists(self.root + " (2).zip.part"))

    def test_report_lists_offline_and_outside(self):
        inside = os.path.join(self.root, "Footage", "a.mp4")
        outside = write_file(os.path.join(self.base, "elsewhere.wav"), 10)
        items = [ProjectItem("n1", "a.mp4", "clip", media_path=inside),
                 ProjectItem("n2", "gone.mp4", "clip", media_path=os.path.join(self.root, "Footage", "gone.mp4"), is_offline=True),
                 ProjectItem("n3", "elsewhere.wav", "clip", media_path=outside),
                 ProjectItem("n4", "Seq", "sequence")]
        report = build_pack_report("Spring.prproj", self.root, items, lambda p: p.startswith(self.root))
        self.assertEqual(report.media_items, 3)
        self.assertEqual(report.inside, 1)
        self.assertFalse(report.ok)
        self.assertIn("gone.mp4", report.line())
        self.assertIn("1 offline", report.line())
        text = report.text()
        self.assertIn("Offline items:", text)
        self.assertIn("elsewhere.wav", text)

    def test_plan_groups(self):
        plan = CollectPlan(entries=[
            PlanEntry("n1", "a.mp4", "s", "Footage", "t", 1024, False, ""),
            PlanEntry("n2", "b.mp4", "s", "Footage", "t", 1024, True, ""),
            PlanEntry("n3", "c.wav", "s", "Audio/SFX", "t", 2048, False, "")])
        self.assertEqual(plan.total_bytes, 3072)
        self.assertEqual(plan.summary(), "3 files · 3.0 KB")
        self.assertEqual([(g["folder"], g["count"]) for g in plan.groups()], [("Audio/SFX", 1), ("Footage", 2)])


if __name__ == "__main__":
    unittest.main()
