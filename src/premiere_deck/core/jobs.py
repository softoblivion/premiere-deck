"""Background jobs of the modules (docs/MODULES.md 2, 3): `JobRunner` and the per-module view `ModuleJobs`.

- Priorities `high`, `normal`, `low`; within a priority first in, first out.
- Two general workers take any job; one more worker is reserved for `high` jobs, so a quick high-priority job (a
  thumbnail the user waits for) never queues behind two long indexing jobs.
- `Job.cancel()` sets `cancelled` and the job's `cancel_event`; the function checks it and returns or raises. A
  job cancelled while still queued never runs.
- Callbacks (`on_progress`, `on_done`, `on_error`) run through `dispatch`, which the host sets to "post onto the
  module thread"; tests leave it synchronous. Rules: the function returned -> `on_done(job, result)`; it raised
  -> `on_error(job, exc)`; cancelled before it started -> `on_error(job, JobCancelled())`.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("premiere_deck.core.jobs")

PRIORITIES = {"high": 0, "normal": 1, "low": 2}
GENERAL_WORKERS = 2
HIGH_WORKERS = 1

Dispatch = Callable[[Callable[[], None]], None]


class JobCancelled(Exception):
    """The job was cancelled before it started (or its function raised this itself)."""

    def __init__(self, message: str = "Cancelled") -> None:
        super().__init__(message)


class Job:
    def __init__(self, runner: "JobRunner", job_id: str, title: str, priority: str, owner: str,
                 fn: Callable[["Job"], Any], on_progress, on_done, on_error) -> None:
        self._runner = runner
        self.id = job_id
        self.title = title
        self.priority = priority
        self.owner = owner
        self.cancelled = False
        self.cancel_event = threading.Event()
        self.state = "queued"  # queued | running | done | failed | cancelled
        self.value: Optional[float] = None
        self.label = ""
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._fn = fn
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_error = on_error

    def progress(self, value: Optional[float], label: str = "") -> None:
        """Called from inside the job function."""
        self.value = None if value is None else max(0.0, min(1.0, float(value)))
        self.label = str(label or "")
        if self._on_progress is not None:
            callback = self._on_progress
            self._runner._dispatch_cb(lambda: callback(self, self.value, self.label))
        self._runner._notify(self)

    def cancel(self) -> None:
        self._runner.cancel(self)

    def snapshot(self) -> dict:
        """Payload of the host window's `job` event (docs/BRIDGE_API.md 4)."""
        return {"id": self.id, "title": self.title, "module": self.owner, "priority": self.priority,
                "state": self.state, "progress": self.value, "label": self.label, "error": self.error,
                "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at}

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.title!r} {self.state}>"


class JobRunner:
    def __init__(self, dispatch: Optional[Dispatch] = None, general_workers: int = GENERAL_WORKERS,
                 high_workers: int = HIGH_WORKERS) -> None:
        self._dispatch = dispatch
        self._cond = threading.Condition()
        self._heap: list[tuple[int, int, Job]] = []
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._jobs: dict[str, Job] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._stopped = False
        self._threads: list[threading.Thread] = []
        for index in range(general_workers):
            self._threads.append(self._start_worker(f"job-worker-{index + 1}", high_only=False))
        for index in range(high_workers):
            self._threads.append(self._start_worker(f"job-high-{index + 1}", high_only=True))

    def set_dispatch(self, dispatch: Optional[Dispatch]) -> None:
        self._dispatch = dispatch

    def on_job(self, fn: Callable[[dict], None]) -> None:
        """Listener for every job state / progress change (the bridge's `job` event)."""
        self._listeners.append(fn)

    # -- submission
    def submit(self, fn: Callable[[Job], Any], *, title: str, priority: str = "normal",
               on_progress=None, on_done=None, on_error=None, owner: str = "host") -> Job:
        if priority not in PRIORITIES:
            raise ValueError(f"unknown priority {priority!r}")
        with self._cond:
            if self._stopped:
                raise RuntimeError("the job runner is stopped")
            job = Job(self, f"job-{next(self._ids)}", str(title), priority, owner, fn, on_progress, on_done, on_error)
            self._jobs[job.id] = job
            heapq.heappush(self._heap, (PRIORITIES[priority], next(self._seq), job))
            self._cond.notify_all()
        self._notify(job)
        return job

    def cancel(self, job: Job) -> None:
        with self._cond:
            if job.state in ("done", "failed", "cancelled"):
                return
            job.cancelled = True
            job.cancel_event.set()
            queued = job.state == "queued"
            if queued:
                job.state = "cancelled"
                job.finished_at = time.time()
                self._heap = [entry for entry in self._heap if entry[2] is not job]
                heapq.heapify(self._heap)
        if queued:
            self._finish_callbacks(job, error=JobCancelled())
        self._notify(job)

    def cancel_owner(self, owner: str) -> int:
        jobs = [job for job in self.jobs() if job.owner == owner and job.state in ("queued", "running")]
        for job in jobs:
            self.cancel(job)
        return len(jobs)

    def jobs(self) -> list[Job]:
        with self._cond:
            return list(self._jobs.values())

    def running(self, owner: Optional[str] = None) -> list[Job]:
        return [job for job in self.jobs() if job.state in ("queued", "running") and (owner is None or job.owner == owner)]

    def high_priority_running(self) -> bool:
        """True while any job of priority `high` is queued or running, whoever owns it."""
        return any(job.priority == "high" for job in self.running())

    def active(self) -> list[dict]:
        return [job.snapshot() for job in self.running()]

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return True
            time.sleep(0.01)
        return not self.running()

    def shutdown(self, cancel: bool = True, timeout: float = 5.0) -> None:
        if cancel:
            for job in self.running():
                self.cancel(job)
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    # -- workers
    def _start_worker(self, name: str, high_only: bool) -> threading.Thread:
        thread = threading.Thread(target=self._work, args=(high_only,), name=name, daemon=True)
        thread.start()
        return thread

    def _take(self, high_only: bool) -> Optional[Job]:
        with self._cond:
            while True:
                if self._stopped:
                    return None
                if self._heap and (not high_only or self._heap[0][0] == PRIORITIES["high"]):
                    job = heapq.heappop(self._heap)[2]
                    job.state = "running"
                    job.started_at = time.time()
                    return job
                self._cond.wait()

    def _work(self, high_only: bool) -> None:
        while True:
            job = self._take(high_only)
            if job is None:
                return
            self._notify(job)
            try:
                result = job._fn(job)
            except BaseException as exc:  # noqa: BLE001 - every failure goes to on_error
                with self._cond:
                    job.state = "cancelled" if (job.cancelled or isinstance(exc, JobCancelled)) else "failed"
                    job.error = str(exc) or type(exc).__name__
                    job.finished_at = time.time()
                if job.state == "failed":
                    log.warning("job %s (%s) failed: %s", job.id, job.title, job.error,
                                exc_info=not isinstance(exc, JobCancelled))
                self._finish_callbacks(job, error=exc)
            else:
                with self._cond:
                    job.state = "cancelled" if job.cancelled else "done"
                    job.finished_at = time.time()
                self._finish_callbacks(job, result=result)
            self._notify(job)
            self._forget_old()

    def _finish_callbacks(self, job: Job, result: Any = None, error: Optional[BaseException] = None) -> None:
        if error is not None:
            if job._on_error is not None:
                callback = job._on_error
                self._dispatch_cb(lambda: callback(job, error))
        elif job._on_done is not None:
            callback = job._on_done
            self._dispatch_cb(lambda: callback(job, result))

    def _dispatch_cb(self, fn: Callable[[], None]) -> None:
        def guarded() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - a module callback must not kill a worker
                log.exception("job callback failed")

        dispatch = self._dispatch
        if dispatch is None:
            guarded()
        else:
            dispatch(guarded)

    def _notify(self, job: Job) -> None:
        if not self._listeners:
            return
        snap = job.snapshot()
        for fn in list(self._listeners):
            try:
                fn(snap)
            except Exception:  # noqa: BLE001
                log.debug("job listener failed", exc_info=True)

    def _forget_old(self, keep: int = 50) -> None:
        with self._cond:
            finished = [j for j in self._jobs.values() if j.state in ("done", "failed", "cancelled")]
            if len(finished) <= keep:
                return
            finished.sort(key=lambda j: j.finished_at or 0)
            for job in finished[:-keep]:
                self._jobs.pop(job.id, None)


class ModuleJobs:
    """`ctx.jobs` of one module: submits with the module as owner; `running()` lists the module's own jobs."""

    def __init__(self, runner: JobRunner, owner: str) -> None:
        self._runner = runner
        self.owner = owner

    def submit(self, fn, *, title, priority="normal", on_progress=None, on_done=None, on_error=None) -> Job:
        return self._runner.submit(fn, title=title, priority=priority, on_progress=on_progress, on_done=on_done,
                                   on_error=on_error, owner=self.owner)

    def running(self) -> list[Job]:
        return self._runner.running(self.owner)

    def high_priority_running(self) -> bool:
        """True while any module's `high` job is queued or running (a Files copy); low-priority work pauses on it."""
        return self._runner.high_priority_running()

    def cancel_all(self) -> int:
        return self._runner.cancel_owner(self.owner)
