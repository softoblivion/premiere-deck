import threading
import time
import unittest

from premiere_deck.core.jobs import JobCancelled, JobRunner, ModuleJobs
from tests.test_host_helpers import wait_for


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.posted = []
        self.runner = JobRunner(dispatch=lambda fn: (self.posted.append(fn), fn()))

    def tearDown(self):
        self.runner.shutdown(cancel=True, timeout=3)

    def test_done_progress_and_error_callbacks(self):
        events = []

        def work(job):
            job.progress(0.5, "half")
            return 42

        job = self.runner.submit(work, title="ok", on_progress=lambda j, v, l: events.append(("p", v, l)),
                                 on_done=lambda j, r: events.append(("done", r)))
        self.assertTrue(wait_for(lambda: job.state == "done"))
        self.assertTrue(wait_for(lambda: ("done", 42) in events))
        self.assertIn(("p", 0.5, "half"), events)
        self.assertTrue(self.posted, "callbacks go through dispatch")

        errors = []
        bad = self.runner.submit(lambda job: 1 / 0, title="bad", on_error=lambda j, e: errors.append(e))
        self.assertTrue(wait_for(lambda: bad.state == "failed" and errors))
        self.assertIsInstance(errors[0], ZeroDivisionError)

    def test_priorities_and_reserved_high_worker(self):
        gate = threading.Event()
        order = []

        def blocker(job):
            gate.wait(5)
            return None

        # Two long normal jobs take both general workers.
        long_jobs = [self.runner.submit(blocker, title=f"long{i}") for i in range(2)]
        self.assertTrue(wait_for(lambda: all(j.state == "running" for j in long_jobs)))
        low = self.runner.submit(lambda job: order.append("low"), title="low", priority="low")
        normal = self.runner.submit(lambda job: order.append("normal"), title="normal")
        high = self.runner.submit(lambda job: order.append("high"), title="high", priority="high")
        # The reserved worker runs the high job while the general workers are busy.
        self.assertTrue(wait_for(lambda: high.state == "done"))
        self.assertEqual(low.state, "queued")
        self.assertEqual(normal.state, "queued")
        gate.set()
        self.assertTrue(wait_for(lambda: low.state == "done" and normal.state == "done"))
        self.assertEqual(order[0], "high")
        self.assertLessEqual(normal.started_at, low.started_at)  # normal leaves the queue before low

    def test_cancel_queued_and_running(self):
        gate = threading.Event()
        blockers = [self.runner.submit(lambda job: gate.wait(5), title=f"b{i}") for i in range(2)]
        self.assertTrue(wait_for(lambda: all(j.state == "running" for j in blockers)))
        errors = []
        queued = self.runner.submit(lambda job: "never", title="queued", on_error=lambda j, e: errors.append(e))
        queued.cancel()
        self.assertEqual(queued.state, "cancelled")
        self.assertTrue(wait_for(lambda: errors))
        self.assertIsInstance(errors[0], JobCancelled)

        def cooperative(job):
            while not job.cancel_event.wait(0.01):
                pass
            raise JobCancelled()

        gate.set()
        running = self.runner.submit(cooperative, title="coop")
        self.assertTrue(wait_for(lambda: running.state == "running"))
        running.cancel()
        self.assertTrue(wait_for(lambda: running.state == "cancelled"))
        self.assertTrue(running.cancelled)

    def test_module_view_and_listener(self):
        snapshots = []
        self.runner.on_job(snapshots.append)
        files = ModuleJobs(self.runner, "files")
        gate = threading.Event()
        job = files.submit(lambda j: gate.wait(5), title="Collect")
        self.assertTrue(wait_for(lambda: job.state == "running"))
        self.assertEqual([j.id for j in files.running()], [job.id])
        self.assertEqual(ModuleJobs(self.runner, "search").running(), [])
        self.assertEqual(files.cancel_all(), 1)
        gate.set()
        self.assertTrue(wait_for(lambda: job.state in ("cancelled", "done")))
        self.assertTrue(any(s["module"] == "files" and s["title"] == "Collect" for s in snapshots))

    def test_high_priority_running_spans_modules(self):
        files, search = ModuleJobs(self.runner, "files"), ModuleJobs(self.runner, "search")
        self.assertFalse(search.high_priority_running())
        gate = threading.Event()
        low = search.submit(lambda j: gate.wait(5), title="Indexing", priority="low")
        self.assertTrue(wait_for(lambda: low.state == "running"))
        self.assertFalse(search.high_priority_running(), "a low job of the same module is not high priority")
        high = files.submit(lambda j: gate.wait(5), title="Collect", priority="high")
        self.assertTrue(wait_for(lambda: high.state == "running"))
        self.assertTrue(search.high_priority_running())
        self.assertTrue(self.runner.high_priority_running())
        gate.set()
        self.assertTrue(wait_for(lambda: high.state == "done" and low.state == "done"))
        self.assertFalse(search.high_priority_running())

    def test_unknown_priority(self):
        with self.assertRaises(ValueError):
            self.runner.submit(lambda job: None, title="x", priority="urgent")


if __name__ == "__main__":
    unittest.main()
