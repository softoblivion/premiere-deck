"""Files module: Premiere events are coalesced into one project snapshot per burst (docs/FILES.md 3.1)."""

import time
import unittest

from tests.test_modules_files_support import FilesCase, wait_for


class Coalescing(FilesCase):
    SYNC_DELAY = 0.05

    def setUp(self):
        super().setUp()
        self.make_module(enabled=True, watch=False)
        self.module.SYNC_MAX_WAIT = 2.0
        self.base = len(self.snapshots())

    def snapshots(self):
        return self.pr.calls_named("snapshot")

    def taken(self) -> int:
        return len(self.snapshots()) - self.base

    def test_burst_of_events_takes_one_snapshot(self):
        for _ in range(5):
            self.module.on_event("project.changed", {})
        self.assertEqual(self.taken(), 0)  # nothing is scanned inside the quiet window
        self.assertTrue(wait_for(lambda: self.taken() >= 1, 1.0, 0.005))
        self.settle()
        time.sleep(self.module.SYNC_DELAY * 3)  # a stray second sync would show up here
        self.assertEqual(self.taken(), 1)

    def test_mixed_event_names_share_one_sync(self):
        self.module.on_event("project.items_added", {})
        self.module.on_event("project.changed", {})
        self.module.on_event("project.items_added", {})
        self.settle()
        time.sleep(self.module.SYNC_DELAY * 3)
        self.assertEqual(self.taken(), 1)

    def test_event_after_the_sync_starts_a_new_one(self):
        for _ in range(3):
            self.module.on_event("project.changed", {})
        self.settle()
        self.assertEqual(self.taken(), 1)
        self.module.on_event("project.changed", {})
        self.settle()
        time.sleep(self.module.SYNC_DELAY * 3)
        self.assertEqual(self.taken(), 2)

    def test_endless_stream_still_syncs_by_the_cap(self):
        self.module.SYNC_DELAY = 0.1
        self.module.SYNC_MAX_WAIT = 0.35
        started = time.monotonic()
        while self.taken() == 0 and time.monotonic() - started < 3.0:
            self.module.on_event("project.changed", {})  # spacing is far below SYNC_DELAY
            time.sleep(0.02)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(self.taken(), 1)
        self.assertLess(elapsed, 1.0)  # without the cap the stream would starve the sync
        self.assertGreaterEqual(elapsed, 0.3)  # and the cap is not shorter than configured

    def test_project_closed_drops_the_pending_sync(self):
        self.module.on_event("project.changed", {})
        self.module.on_event("project.changed", {})
        self.module.project_closed()
        time.sleep(self.module.SYNC_DELAY * 4)
        self.assertEqual(self.taken(), 0)

    def test_callback_of_a_replaced_project_state_is_ignored(self):
        self.module.on_event("project.changed", {})
        old_state = self.module._state
        old_token = old_state.sync_token
        self.module.project_opened(self.project)  # replaces the state; it takes its own direct snapshot
        after_open = len(self.snapshots())
        self.module._sync_due(old_state, old_token)
        self.assertEqual(len(self.snapshots()), after_open)
        time.sleep(self.module.SYNC_DELAY * 4)
        self.assertEqual(len(self.snapshots()), after_open)

    def test_stale_token_is_ignored(self):
        self.module.on_event("project.changed", {})
        state = self.module._state
        stale = state.sync_token
        self.module.on_event("project.changed", {})  # re-arms: the first token is stale
        self.module._sync_due(state, stale)
        self.assertEqual(self.taken(), 0)
        self.settle()
        self.assertEqual(self.taken(), 1)

    def test_event_without_a_project_does_nothing(self):
        self.module.project_closed()
        self.module.on_event("project.changed", {})
        time.sleep(self.module.SYNC_DELAY * 3)
        self.assertEqual(self.taken(), 0)
        self.assertIsNone(self.module._state)


if __name__ == "__main__":
    unittest.main()
