import os
import tempfile
import unittest

from premiere_deck.host import protocol as P
from premiere_deck.modules.base import ActionResult, FakeHost, Module
from premiere_deck.premiere.model import PremiereUnavailable, ProjectItem


class Demo(Module):
    id = "demo"
    title = "Demo"
    events = ["project.opened"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.count = 0

    def render(self):
        return [self.button("count", f"Count {self.count}"), self.toggle("on", "On", True),
                self.list_("rows", [self.list_row("r1", "Row", actions=[self.row_action("undo", "Undo")])])]

    def on_action(self, action_id, value=None, row=None):
        if self.strip_prefix(action_id) == "count":
            self.count += 1
            return ActionResult(toast=("ok", "Counted"))
        return super().on_action(action_id, value, row)


class ModuleBase(unittest.TestCase):
    def setUp(self):
        os.environ["PREMIERE_DECK_HOME"] = tempfile.mkdtemp(prefix="deck-base-")
        self.host = FakeHost()
        self.module = Demo(self.host.context())

    def test_section_has_prefixed_ids_and_validates(self):
        sec = self.module.section()
        ids = [node["id"] for node in sec["items"]]
        self.assertEqual(ids, ["demo.count", "demo.on", "demo.rows"])
        self.assertEqual(sec["items"][2]["rows"][0]["actions"][0]["id"], "demo.undo")
        P.validate_tree({"type": "ui", "revision": 0, "title": "", "status": None, "sections": [sec], "footer": None})

    def test_actions_strip_prefix_and_unknown_warns(self):
        result = self.module.on_action("demo.count")
        self.assertEqual(result.toast, ("ok", "Counted"))
        self.assertEqual(self.module.count, 1)
        unknown = self.module.on_action("demo.nothing")
        self.assertEqual(unknown.toast[0], "warn")
        self.assertFalse(unknown.rerender)

    def test_module_needs_id_and_title(self):
        class Broken(Module):
            pass

        with self.assertRaises(ValueError):
            Broken(self.host.context())


class Fakes(unittest.TestCase):
    def setUp(self):
        os.environ["PREMIERE_DECK_HOME"] = tempfile.mkdtemp(prefix="deck-fakes-")
        self.host = FakeHost()

    def test_fake_premiere_tracks_items(self):
        project = self.host.make_project()
        pr = self.host.premiere
        created = pr.import_files([r"D:\Downloads\clip.mp4"], "Footage")
        self.assertEqual(created[0].tree_path, "Footage")
        self.assertTrue(created[0].has_video)
        pr.relink(created[0].node_id, os.path.join(project.root, "Footage", "clip.mp4"))
        pr.move_to_bin([created[0].node_id], "Overlays")
        item = pr.item(created[0].node_id)
        self.assertEqual(item.tree_path, "Overlays")
        self.assertTrue(item.media_path.endswith("clip.mp4"))
        self.assertEqual([n for n, *_ in pr.calls][:4], ["import_files", "relink", "move_to_bin", "item"])
        pr.connected = False
        with self.assertRaises(PremiereUnavailable):
            pr.snapshot()

    def test_fake_premiere_remove_items(self):
        pr = self.host.premiere
        created = pr.import_files([r"D:\Downloads\a.mp4", r"D:\Downloads\b.mp4"], "Footage")
        removed = pr.remove_items([created[0].node_id, "unknown"])
        self.assertEqual(removed, 1)
        self.assertIsNone(pr.item(created[0].node_id))
        self.assertIsNotNone(pr.item(created[1].node_id))
        self.assertEqual(pr.calls_named("remove_items")[0][0], ((created[0].node_id, "unknown"),))

    def test_fake_jobs_inline_and_manual(self):
        results = []
        job = self.host.jobs.submit(lambda j: (j.progress(0.5, "half"), 42)[1], title="t", on_done=lambda j, r: results.append(r))
        self.assertEqual(results, [42])
        self.assertEqual(job.progress_log, [(0.5, "half")])
        manual = FakeHost(manual_jobs=True)
        errors = []
        manual.jobs.submit(lambda j: 1 / 0, title="boom", on_error=lambda j, e: errors.append(type(e).__name__))
        self.assertEqual(errors, [])
        manual.jobs.step()
        self.assertEqual(errors, ["ZeroDivisionError"])

    def test_fake_jobs_high_priority_flag(self):
        self.assertFalse(self.host.jobs.high_priority_running())
        self.host.jobs.high_priority = True
        self.assertTrue(self.host.jobs.high_priority_running())

    def test_fake_tools_ffmpeg_pipe(self):
        proc = self.host.tools.ffmpeg_pipe(["-i", "a", "pipe:1"])
        try:
            self.assertEqual(proc.stdout.read(), b"")
            proc.wait(timeout=30)
        finally:
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.host.tools.pipe_calls, [["-i", "a", "pipe:1"]])

    def test_fake_tools_probe_by_extension(self):
        info = self.host.tools.ffprobe(r"D:\x\sound.wav")
        self.assertEqual((info.kind, info.ext), ("audio", "wav"))
        self.assertEqual(self.host.tools.ffmpeg(["-i", "a"]), 0)
        self.assertEqual(self.host.tools.ffmpeg_calls, [["-i", "a"]])

    def test_fake_premiere_eval_results_and_errors(self):
        pr = self.host.premiere
        pr.eval_results["sum"] = lambda args: args["a"] + args["b"]
        self.assertEqual(pr.eval("sum", "", {"a": 1, "b": 2}), 3)
        pr.eval_errors["bad"] = RuntimeError("no")
        with self.assertRaises(RuntimeError):
            pr.eval("bad", "")

    def test_snapshot_helpers(self):
        items = (ProjectItem("1", "Footage", "bin"), ProjectItem("2", "a.mp4", "clip", "Footage", r"D:\a.mp4"))
        self.host.premiere.set_items(items)
        snap = self.host.premiere.snapshot()
        self.assertEqual(len(snap.bins()), 1)
        self.assertEqual(snap.clips()[0].full_path, "Footage/a.mp4")


if __name__ == "__main__":
    unittest.main()
