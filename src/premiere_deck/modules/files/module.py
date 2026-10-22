"""Files module (docs/FILES.md): media sorted the same way on disk and in the project panel, Collect, Pack, Undo.

Threads: hooks and job callbacks run on the host's module thread; copies, probes and the Collect plan run as jobs
(copies with priority `high`); the disk watcher settles changes on its own thread and hops onto the module thread
by submitting a tiny job whose `on_done` applies the change (the job runner dispatches callbacks onto the module
thread). Premiere is only called from the module thread. Shared state is guarded by one re-entrant lock.

Invariants (docs/FILES.md 1): originals are never moved, renamed or deleted; the module only deletes files it
created itself (a journaled copy whose content hash still matches) and only inside the project root.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from premiere_deck.core.media import MediaInfo, kind_from_extension
from premiere_deck.modules.base import ActionResult, Module, SettingField
from premiere_deck.premiere.model import KIND_CLIP, KIND_FILE, PremiereError, ProjectInfo, ProjectItem, Snapshot

from . import journal as J
from .classify import classify, parse_overrides
from .layout import (
    CUSTOM_LAYOUT_FILE, DECK_DIR, DEFAULT_LAYOUT_ID, Layout, LayoutError, Mapper, detect_root, is_excluded,
    is_inside, load_layout_file, norm, norm_bin, parse_layout, same_path, shipped_layouts,
)
from .sync import (
    CollectPlan, CopyCancelled, DiskWatcher, PlanEntry, PackReport, Suppression, build_pack_report,
    copy_file, file_size, fmt_size, free_zip_path, make_zip, resolve_target, safe_hash,
)

HOST_DEFAULTS = {"toast_filed": True, "copy_buffer_mb": 16, "stable_seconds": 2, "default_layout": DEFAULT_LAYOUT_ID}
INT_RANGES = {"copy_buffer_mb": (1, 256), "stable_seconds": (1, 60)}
PROJECT_DEFAULTS = {"enabled": False, "layout": DEFAULT_LAYOUT_ID, "overrides": [], "enforce_bins": True,
                    "mirror_bins": True, "import_exports": False, "watch": True, "pack_zip": False}
PROJECT_BOOLS = ("enforce_bins", "mirror_bins", "import_exports", "watch", "pack_zip")

OP_LABELS = {J.OP_FILED: "Filed", J.OP_COLLECTED: "Collected", J.OP_BINNED: "Moved to bin", J.OP_IMPORTED: "Imported",
             J.OP_RENAMED: "Renamed", J.OP_MOVED_DISK: "Moved on disk", J.OP_MOVED_BIN: "Moved with bin",
             J.OP_REMOVED: "Removed", "setup": "Set up"}


def ago(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return ""
    delta = max(0.0, (now or time.time()) - float(ts))
    if delta < 45:
        return "just now"
    if delta < 90 * 60:
        return f"{max(1, round(delta / 60))} min ago"
    if delta < 36 * 3600:
        return f"{round(delta / 3600)} h ago"
    return time.strftime("%d %b", time.localtime(float(ts)))


def _folder_name(path: Optional[str]) -> str:
    return os.path.basename(os.path.dirname(path)) if path else ""


@dataclass
class _Tracked:
    job: Any
    kind: str  # filing | moving | plan | collect | pack | sync
    label: str
    value: Optional[float] = None


@dataclass
class _Task:
    node_id: str
    name: str
    source: str
    bin_from: str
    folder: Optional[str] = None  # decided by the plan; None = classify in the job


@dataclass
class _Copied:
    task: _Task
    entry: Optional[dict] = None
    target: str = ""
    folder: str = ""
    reuse: bool = False
    error: str = ""
    cancelled: bool = False


@dataclass
class _State:
    """Per open project; replaced on every project_opened so late job callbacks of a closed project are ignored."""

    project: ProjectInfo
    root: str
    layout: Layout
    mapper: Mapper
    journal: J.Journal
    layout_error: str = ""
    items: dict = field(default_factory=dict)  # node_id -> ProjectItem (last snapshot)
    index: dict = field(default_factory=dict)  # norm media path -> set(node ids)
    baseline_ready: bool = False
    offline: list = field(default_factory=list)
    external: list = field(default_factory=list)  # media outside the root or loose in it
    plan: Optional[CollectPlan] = None
    pack_report: Optional[PackReport] = None
    watcher: Optional[DiskWatcher] = None
    watcher_error: str = ""
    premiere_error: str = ""
    sync_timer: Optional[threading.Timer] = None  # pending coalesced sync (on_event); None = nothing pending
    sync_token: Optional[object] = None  # identity of the pending sync; a callback of an older timer is ignored
    sync_first: float = 0.0  # monotonic time of the first event of the pending burst


class FilesModule(Module):
    id = "files"
    title = "Files"
    description = "Keeps project media sorted on disk and in bins."
    order = 10
    events = ["project.opened", "project.closed", "project.items_added", "project.changed"]
    requires = ["ffprobe"]
    default_enabled = True

    WATCH_DEBOUNCE = 1.5
    SYNC_DELAY = 0.3  # quiet window after the last Premiere event before the project is scanned once
    SYNC_MAX_WAIT = 2.0  # a burst that never goes quiet is scanned at most this long after its first event
    SUPPRESS_TTL = 15.0
    TOAST_MERGE_SECONDS = 2.0
    UNDO_LIMIT = 50
    RECENT_COUNT = 8
    HOST_JOURNAL_COUNT = 200

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._lock = threading.RLock()
        self._suppress = Suppression(self.SUPPRESS_TTL)
        self._state: Optional[_State] = None
        self._jobs: dict[str, _Tracked] = {}
        self._toast_at = 0.0
        self._toast_count = 0
        self._journal_filter = {"op": "", "query": ""}
        self._layouts: Optional[dict[str, Layout]] = None

    # ------------------------------------------------------------------------------------------------- settings

    def _host(self, key: str) -> Any:
        default = HOST_DEFAULTS[key]
        try:
            value = self.ctx.settings.get(key, default)
        except Exception:  # noqa: BLE001 - a broken settings store falls back to defaults
            return default
        return self._valid(key, value, default)

    def _proj(self, key: str) -> Any:
        default = PROJECT_DEFAULTS[key]
        try:
            value = self.ctx.settings.project.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        if key == "overrides":
            return value if isinstance(value, list) else []
        return self._valid(key, value, default)

    def _valid(self, key: str, value: Any, default: Any) -> Any:
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        if isinstance(default, int):
            low, high = INT_RANGES.get(key, (0, 1 << 30))
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                return default
            return value
        if key in ("default_layout", "layout"):
            allowed = set(self._shipped())
            if key == "layout":
                allowed.add("custom")
            return value if isinstance(value, str) and value in allowed else default
        return value if isinstance(value, type(default)) else default

    def _set_proj(self, key: str, value: Any) -> None:
        self.ctx.settings.project.set(key, value)

    def _shipped(self) -> dict[str, Layout]:
        if self._layouts is None:
            self._layouts = shipped_layouts()
        return self._layouts

    def settings_schema(self) -> list[SettingField]:
        options = [{"value": lid, "label": layout.name} for lid, layout in self._shipped().items()]
        return [
            SettingField("toast_filed", "Notify when filed", "bool", True,
                         hint="Show a short message in the panel for every filed import."),
            SettingField("copy_buffer_mb", "Copy buffer (MB)", "int", 16, hint="Read size of each copy step."),
            SettingField("stable_seconds", "Wait for copies (s)", "int", 2,
                         hint="A new file is imported once its size has not changed for this long."),
            SettingField("default_layout", "Default layout", "select", DEFAULT_LAYOUT_ID, options=options,
                         hint="Layout a project gets when it is set up."),
            SettingField("enforce_bins", "Keep bins in order", "bool", True, scope="project",
                         hint="Media imported from the project folder goes to the bin of its folder."),
            SettingField("mirror_bins", "Move files with bins", "bool", True, scope="project",
                         hint="Moving a clip to another bin moves its file to the matching folder."),
            SettingField("import_exports", "Import exports", "bool", False, scope="project",
                         hint="Files rendered into Exports are imported automatically."),
            SettingField("watch", "Watch folder", "bool", True, scope="project",
                         hint="Files added to the project folder on disk are imported."),
            SettingField("pack_zip", "Pack as zip", "bool", False, scope="project",
                         hint="Pack for sharing also writes a zip archive beside the project folder."),
        ]

    # ------------------------------------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self.project_closed()
        self._started = False

    def project_opened(self, project: ProjectInfo) -> None:
        with self._lock:
            if self._state is not None:
                self.project_closed()
            shipped = self._shipped()
            base = shipped.get(self._host("default_layout")) or shipped.get(DEFAULT_LAYOUT_ID)
            if base is None:
                raise LayoutError("the shipped default layout is missing")
            root = detect_root(project.path, base)
            if project.root and not same_path(project.root, root):
                self.log.info("Files root %s differs from the facade root %s", root, project.root)
            layout, error = self._choose_layout(root, base)
            if layout.project_dir != base.project_dir:
                root = detect_root(project.path, layout)
            state = _State(project=project, root=root, layout=layout, mapper=Mapper(root, layout),
                           journal=J.Journal(os.path.join(root, DECK_DIR, "journal.jsonl")), layout_error=error)
            self._state = state
            self._take_snapshot(state)
            if self._enabled():
                self._recover(state)
                self._start_watcher(state)
        self.ctx.ui.invalidate()

    def project_closed(self) -> None:
        with self._lock:
            state, self._state = self._state, None
            jobs = list(self._jobs.values())
            self._jobs.clear()
            if state is not None:
                self._cancel_sync(state)
        if state is not None and state.watcher is not None:
            state.watcher.stop()
        for tracked in jobs:
            try:
                tracked.job.cancel()
            except Exception:  # noqa: BLE001
                pass
        if state is not None:
            self.ctx.ui.invalidate()

    def on_event(self, name: str, data: Any) -> None:
        if name in ("project.items_added", "project.changed"):
            self._schedule_sync()

    def _schedule_sync(self) -> None:
        """Coalesce a burst of Premiere events into one snapshot (docs/FILES.md 3.1): every event re-arms a one-shot
        timer of SYNC_DELAY, capped at SYNC_MAX_WAIT after the first event of the burst. The timer thread only
        posts `_sync_due` to the module thread."""
        with self._lock:
            state = self._state
            if state is None:
                return
            now = time.monotonic()
            if state.sync_timer is None:
                state.sync_first = now
            else:
                state.sync_timer.cancel()
            wait = max(0.0, min(self.SYNC_DELAY, state.sync_first + self.SYNC_MAX_WAIT - now))
            token = object()
            timer = threading.Timer(wait, self._sync_timer_fired, args=(state, token))
            timer.daemon = True
            state.sync_timer, state.sync_token = timer, token
            timer.start()

    def _sync_timer_fired(self, state: _State, token: object) -> None:
        try:
            self.ctx.post(lambda: self._sync_due(state, token))
        except Exception:  # noqa: BLE001 - a timer thread must not die with a traceback
            self.log.exception("could not post the coalesced sync")

    def _sync_due(self, state: _State, token: object) -> None:
        with self._lock:
            if self._state is not state or state.sync_token is not token:
                return  # the project was closed or replaced, or a newer event re-armed the timer
            state.sync_timer = state.sync_token = None
            self._sync_from_premiere()

    @staticmethod
    def _cancel_sync(state: _State) -> None:
        timer, state.sync_timer, state.sync_token = state.sync_timer, None, None
        if timer is not None:
            timer.cancel()

    # ------------------------------------------------------------------------------------------------ layout

    def _choose_layout(self, root: str, base: Layout) -> tuple[Layout, str]:
        choice = self._proj("layout")
        if choice == "custom":
            path = os.path.join(root, DECK_DIR, CUSTOM_LAYOUT_FILE)
            try:
                return load_layout_file(path), ""
            except LayoutError as exc:
                self.log.warning("custom layout unusable: %s", exc)
                return base, "The project's layout.json could not be read; the default layout is used."
        return self._shipped().get(choice) or base, ""

    def _enabled(self) -> bool:
        return self._state is not None and self._proj("enabled") is True

    def _overrides(self):
        return parse_overrides(self._proj("overrides"))

    # ------------------------------------------------------------------------------------------------ snapshot

    def _take_snapshot(self, state: _State) -> Optional[Snapshot]:
        try:
            snap = self.ctx.premiere.snapshot()
        except PremiereError as exc:
            state.premiere_error = exc.message
            self.log.info("snapshot failed: %s", exc.message)
            return None
        state.premiere_error = ""
        self._set_items(state, snap.items)
        state.baseline_ready = True
        return snap

    def _set_items(self, state: _State, items) -> None:
        state.items = {item.node_id: item for item in items}
        index: dict[str, set] = {}
        for item in state.items.values():
            if item.media_path:
                index.setdefault(norm(item.media_path), set()).add(item.node_id)
        state.index = index
        self._refresh_derived(state)

    def _refresh_derived(self, state: _State) -> None:
        offline, external = [], []
        for item in state.items.values():
            if not self._is_media(item):
                continue
            exists = os.path.isfile(item.media_path)
            if item.is_offline or not exists:
                offline.append(item)
                continue
            if is_excluded(item.media_path, state.root, check_hidden=False):
                continue
            if not is_inside(item.media_path, state.root) or not state.mapper.in_layout(item.media_path):
                external.append(item)
        state.offline = sorted(offline, key=lambda i: i.name.casefold())
        state.external = external

    @staticmethod
    def _is_media(item: ProjectItem) -> bool:
        return item.kind in (KIND_CLIP, KIND_FILE) and bool(item.media_path) and not item.is_sequence

    def _index_set(self, state: _State, node_id: str, old: Optional[str], new: Optional[str]) -> None:
        if old:
            nodes = state.index.get(norm(old))
            if nodes is not None:
                nodes.discard(node_id)
                if not nodes:
                    del state.index[norm(old)]
        if new:
            state.index.setdefault(norm(new), set()).add(node_id)

    def _patch(self, state: _State, node_id: str, **changes) -> None:
        """Apply the known outcome of the module's own change to the cached snapshot, so the next diff does not
        see it as a user change and the derived lists are right at once."""
        item = state.items.get(node_id)
        if item is not None:
            state.items[node_id] = replace(item, **changes)

    def _referenced(self, state: _State, path: str, ignore: Optional[str] = None) -> bool:
        return bool(state.index.get(norm(path), set()) - ({ignore} if ignore else set()))

    # ------------------------------------------------------------------------------------------------ Premiere -> disk

    def _sync_from_premiere(self) -> None:
        with self._lock:
            state = self._state
            if state is None:
                return
            before = state.items
            was_ready = state.baseline_ready
            snap = self._take_snapshot(state)
            if snap is None or not was_ready or not self._enabled():
                self.ctx.ui.invalidate()
                return
            added: list[ProjectItem] = []
            for item in snap.items:
                old = before.get(item.node_id)
                if old is None:
                    if self._is_media(item):
                        if self._suppress.consume(("import", norm(item.media_path))):
                            continue
                        added.append(item)
                    continue
                if norm_bin(old.tree_path) != norm_bin(item.tree_path):
                    if self._suppress.consume(("bin", item.node_id, norm_bin(item.tree_path))):
                        continue
                    if self._is_media(item) and self._proj("mirror_bins"):
                        self._mirror_bin_move(state, old, item)
                elif old.media_path != item.media_path and item.media_path:
                    self._suppress.consume(("media", item.node_id, norm(item.media_path)))
            if added:
                self._intercept(state, added)
        self.ctx.ui.invalidate()

    def _intercept(self, state: _State, items: list[ProjectItem]) -> None:
        """docs/FILES.md 3.1 for new project items."""
        tasks: list[_Task] = []
        for item in items:
            path = item.media_path
            if not path or is_excluded(path, state.root, check_hidden=False):
                continue
            if not os.path.isfile(path):
                continue  # offline: listed, not fixed
            if is_inside(path, state.root) and state.mapper.in_layout(path):
                if self._proj("enforce_bins"):
                    self._enforce_bin(state, item)
                continue
            tasks.append(_Task(node_id=item.node_id, name=item.name, source=path, bin_from=item.tree_path))
        if tasks:
            title = f"Filing {tasks[0].name}" if len(tasks) == 1 else f"Filing {len(tasks)} files"
            self._submit_copies(state, tasks, J.OP_FILED, title, kind="filing")

    def _enforce_bin(self, state: _State, item: ProjectItem) -> None:
        folder = state.mapper.layout_folder_of(item.media_path)
        if folder is None:
            return
        target_bin = state.mapper.bin_for_folder(folder)
        if norm_bin(item.tree_path) == norm_bin(target_bin):
            return
        entry = state.journal.append(J.OP_BINNED, node_id=item.node_id, name=item.name, **{"from": item.media_path},
                                     to=item.media_path, bin_from=item.tree_path, bin_to=target_bin, copied=False)
        self._suppress.add(("bin", item.node_id, norm_bin(target_bin)))
        try:
            self.ctx.premiere.move_to_bin([item.node_id], target_bin)
        except PremiereError as exc:
            self._suppress.discard(("bin", item.node_id, norm_bin(target_bin)))
            state.journal.mark_failed(entry["id"], exc.message)
            return
        self._patch(state, item.node_id, tree_path=target_bin)
        state.journal.mark_done(entry["id"])
        self._emit(J.OP_BINNED, f"Moved {item.name} to the {target_bin} bin")

    # copies -------------------------------------------------------------------------------------------------------

    def _probe(self, path: str) -> MediaInfo:
        try:
            return self.ctx.tools.ffprobe(path)
        except Exception as exc:  # noqa: BLE001 - a probe failure classifies by extension (docs/FILES.md 4)
            self.log.info("probe failed for %s: %s", path, exc)
            return MediaInfo(path=path, kind=kind_from_extension(path), ext=Path(path).suffix.lower().lstrip("."),
                             size=file_size(path), probe_error=str(exc))

    def _copy_one(self, state: _State, job, task: _Task, op: str, on_bytes) -> _Copied:
        """Job thread: classify, resolve the target, journal `pending`, copy. Premiere is not touched here."""
        if not os.path.isfile(task.source):
            return _Copied(task, error="The source file is missing")
        folder = task.folder
        if folder is None:
            folder = classify(self._probe(task.source), state.layout, self._overrides(), task.source).folder
        target_dir = state.mapper.abs_folder(folder)
        target, reuse, source_hash = resolve_target(task.source, target_dir)
        if source_hash is None:
            source_hash = safe_hash(task.source)
        entry = state.journal.append(op, node_id=task.node_id, name=task.name, **{"from": task.source}, to=target,
                                     bin_from=task.bin_from, bin_to=state.mapper.bin_for_folder(folder),
                                     hash=source_hash, copied=not reuse, size=file_size(task.source))
        if not reuse:
            self._suppress.add(("disk", norm(target)))
            try:
                copy_file(task.source, target, buffer_bytes=self._host("copy_buffer_mb") << 20, on_bytes=on_bytes,
                          cancelled=lambda: bool(getattr(job, "cancelled", False)))
            except CopyCancelled:
                self._suppress.discard(("disk", norm(target)))
                state.journal.mark_failed(entry["id"], "cancelled")
                return _Copied(task, entry=None, cancelled=True)
            except OSError as exc:
                self._suppress.discard(("disk", norm(target)))
                state.journal.mark_failed(entry["id"], str(exc))
                self.log.warning("copy of %s failed: %s", task.source, exc)
                return _Copied(task, error=str(exc))
        return _Copied(task, entry=entry, target=target, folder=folder, reuse=reuse)

    def _submit_copies(self, state: _State, tasks: list[_Task], op: str, title: str, *, kind: str,
                       after: Optional[Callable[[list[_Copied], bool], None]] = None) -> None:
        results: list[_Copied] = []
        finished = {"done": False}

        def work(job):
            total = sum(file_size(t.source) for t in tasks) or 1
            done = [0]

            def on_bytes(count: int) -> None:
                done[0] += count
                job.progress(done[0] / total, f"Copying {current[0]}")

            current = [""]
            for index, task in enumerate(tasks):
                if getattr(job, "cancelled", False):
                    break
                current[0] = task.name if len(tasks) == 1 else f"{index + 1} of {len(tasks)}: {task.name}"
                result = self._copy_one(state, job, task, op, on_bytes)
                results.append(result)
                if result.cancelled:
                    break
            return results

        def finish(job, _value=None):
            finished["done"] = True
            self._untrack(job)
            cancelled = bool(getattr(job, "cancelled", False)) or any(r.cancelled for r in results)
            with self._lock:
                if self._state is not state:
                    self.log.info("%s finished after the project closed; the journal resumes it", title)
                    return
                filed = self._apply_copies(state, results, op)
            if op == J.OP_COLLECTED:
                state.plan = None
                count = len(filed)
                if cancelled:
                    self.ctx.ui.toast("warn", f"Collect stopped · {count} of {len(tasks)} filed")
                elif count:
                    self.ctx.ui.toast("ok", f"Collected {count} file{'s' if count != 1 else ''}")
            if after is not None:
                after(results, cancelled)
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title=title, priority="high", on_progress=self._on_progress,
                                   on_done=finish, on_error=lambda j, exc: finish(j, exc))
        if not finished["done"]:
            self._track(job, kind, title)

    def _apply_copies(self, state: _State, results: list[_Copied], op: str) -> list[tuple[str, str]]:
        """Module thread: relink, move to bin, journal done; roll back a copy whose relink fails."""
        filed: list[tuple[str, str]] = []
        errors: list[str] = []
        for result in results:
            entry = result.entry
            if entry is None:
                if result.error:
                    errors.append(result.task.name)
                continue
            node = result.task.node_id
            source = result.task.source
            self._suppress.add(("media", node, norm(result.target)))
            try:
                online = bool(self.ctx.premiere.relink(node, result.target))
            except PremiereError as exc:
                self.log.warning("relink of %s failed: %s", result.task.name, exc.message)
                online = False
            if not online:
                self._suppress.discard(("media", node, norm(result.target)))
                if entry.get("copied"):
                    self._delete_own_copy(state, result.target, entry_hash=entry.get("hash"), check_journal=False)
                state.journal.mark_failed(entry["id"], "relink failed")
                self.ctx.ui.toast("error", f"Could not relink {result.task.name}")
                continue
            self._index_set(state, node, source, result.target)
            self._patch(state, node, media_path=result.target, is_offline=False)
            bin_to = entry.get("bin_to") or ""
            if norm_bin(bin_to) != norm_bin(result.task.bin_from):
                self._suppress.add(("bin", node, norm_bin(bin_to)))
                try:
                    self.ctx.premiere.move_to_bin([node], bin_to)
                except PremiereError as exc:
                    self._suppress.discard(("bin", node, norm_bin(bin_to)))
                    self.log.warning("move to bin %s failed: %s", bin_to, exc.message)
                    bin_to = result.task.bin_from
                else:
                    self._patch(state, node, tree_path=bin_to)
            state.journal.mark_done(entry["id"], bin_to=bin_to)
            filed.append((result.task.name, result.folder))
        if filed:
            self._emit(op, f"{OP_LABELS[op]} {filed[0][0]} into {filed[0][1]}" if len(filed) == 1
                       else f"{OP_LABELS[op]} {len(filed)} files", count=len(filed))
        if op == J.OP_FILED:
            self._toast_filed(filed)
        if errors:
            text = f"Could not copy {errors[0]}" if len(errors) == 1 else f"Could not copy {len(errors)} files"
            self.ctx.ui.toast("error", text)
        self._refresh_derived(state)
        return filed

    def _emit(self, op: str, text: str, *, tone: str = "ok", **extra) -> None:
        """`journal.appended` for the host window; `text` and `tone` feed its activity list (BRIDGE_API.md 3.5)."""
        self.ctx.events.emit("journal.appended", {"module": self.id, "op": op, "text": text, "tone": tone, **extra})

    def _toast_filed(self, filed: list[tuple[str, str]]) -> None:
        if not filed or not self._host("toast_filed"):
            return
        now = time.monotonic()
        if self._toast_count and now - self._toast_at <= self.TOAST_MERGE_SECONDS:
            self._toast_count += len(filed)
        else:
            self._toast_count = len(filed)
        self._toast_at = now
        if self._toast_count == 1:
            name, folder = filed[0]
            self.ctx.ui.toast("ok", f"Filed {name} into {folder}")
        else:
            self.ctx.ui.toast("ok", f"Filed {self._toast_count} files")

    def _delete_own_copy(self, state: _State, path: str, *, entry_hash: Optional[str] = None,
                         ignore_node: Optional[str] = None, check_journal: bool = True) -> bool:
        """Delete a file the module created, only inside the root, only when no project item references it and its
        content still matches the hash journaled for the copy. Returns whether the file was deleted."""
        if not path or not is_inside(path, state.root) or not os.path.isfile(path):
            return False
        if self._referenced(state, path, ignore_node):
            return False
        expected = state.journal.module_copy_hash(path, norm) if check_journal else entry_hash
        if not expected or safe_hash(path) != expected:
            return False
        self._suppress.add(("disk", norm(path)))
        try:
            os.remove(path)
        except OSError as exc:
            self._suppress.discard(("disk", norm(path)))
            self.log.warning("could not delete the copy %s: %s", path, exc)
            return False
        state.index.pop(norm(path), None)
        return True

    # bin moves (docs/FILES.md 3.3) ------------------------------------------------------------------------------

    def _mirror_bin_move(self, state: _State, before: ProjectItem, after: ProjectItem) -> None:
        path = after.media_path
        if not path or not os.path.isfile(path) or not is_inside(path, state.root) or not state.mapper.in_layout(path):
            return
        folder = state.mapper.folder_for_bin(after.tree_path)
        if folder is None:
            return  # a bin without a folder ("Selects"): nothing on disk changes
        current = state.mapper.layout_folder_of(path)
        if current is not None and current.casefold() == folder.casefold():
            return
        node = after.node_id
        result: dict = {}
        finished = {"done": False}

        def work(job):
            target, reuse, file_hash = resolve_target(path, state.mapper.abs_folder(folder))
            file_hash = file_hash or safe_hash(path)
            entry = state.journal.append(J.OP_MOVED_BIN, node_id=node, name=after.name, **{"from": path}, to=target,
                                         bin_from=before.tree_path, bin_to=after.tree_path, hash=file_hash,
                                         copied=not reuse)
            result["entry"] = entry
            if not reuse:
                self._suppress.add(("disk", norm(target)))
                try:
                    copy_file(path, target, buffer_bytes=self._host("copy_buffer_mb") << 20,
                              cancelled=lambda: bool(getattr(job, "cancelled", False)))
                except (CopyCancelled, OSError) as exc:
                    self._suppress.discard(("disk", norm(target)))
                    state.journal.mark_failed(entry["id"], "cancelled" if isinstance(exc, CopyCancelled) else str(exc))
                    result["failed"] = True
            result["target"] = target
            return result

        def finish(job, _value=None):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is not state or "entry" not in result or result.get("failed"):
                    self.ctx.ui.invalidate()
                    return
                entry, target = result["entry"], result["target"]
                self._suppress.add(("media", node, norm(target)))
                try:
                    online = bool(self.ctx.premiere.relink(node, target))
                except PremiereError:
                    online = False
                if not online:
                    if entry.get("copied"):
                        self._delete_own_copy(state, target, entry_hash=entry.get("hash"), check_journal=False)
                    state.journal.mark_failed(entry["id"], "relink failed")
                    self.ctx.ui.toast("error", f"Could not relink {after.name}")
                    self.ctx.ui.invalidate()
                    return
                self._index_set(state, node, path, target)
                self._patch(state, node, media_path=target, is_offline=False)
                deleted = self._delete_own_copy(state, path, ignore_node=None)
                state.journal.mark_done(entry["id"], deleted_from=deleted)
                self._emit(J.OP_MOVED_BIN, f"Moved {after.name} to {folder} with its bin")
                self._refresh_derived(state)
            self.ctx.ui.invalidate()

        title = f"Moving {after.name}"
        job = self.ctx.jobs.submit(work, title=title, priority="high", on_progress=self._on_progress,
                                   on_done=finish, on_error=lambda j, exc: finish(j, exc))
        if not finished["done"]:
            self._track(job, "moving", title)

    # ------------------------------------------------------------------------------------------------ disk -> Premiere

    def _watch_ignored(self, state: _State, path: str) -> bool:
        if is_excluded(path, state.root):
            return True
        if state.mapper.is_exports(path) and not self._proj("import_exports"):
            return True
        return False

    def _start_watcher(self, state: _State) -> None:
        if state.watcher is not None or not self._proj("watch") or not os.path.isdir(state.root):
            return
        watcher = DiskWatcher(state.root, lambda change: self._on_disk_change(state, change),
                              ignored=lambda path: self._watch_ignored(state, path),
                              debounce=self.WATCH_DEBOUNCE, stable=float(self._host("stable_seconds")))
        try:
            watcher.start()
        except Exception as exc:  # noqa: BLE001 - shown in the panel, the module stays usable
            state.watcher_error = str(exc)
            self.log.warning("watcher failed to start: %s", exc)
            return
        state.watcher, state.watcher_error = watcher, ""

    def _stop_watcher(self, state: _State) -> None:
        watcher, state.watcher = state.watcher, None
        if watcher is not None:
            watcher.stop()

    def _on_disk_change(self, state: _State, change: dict) -> None:
        """Watcher thread: drop our own changes, then hop onto the module thread through a job callback."""
        path = change["path"]
        if change["type"] in ("created", "deleted", "moved") and self._suppress.consume(("disk", norm(path))):
            return
        if self._state is not state:
            return
        self.ctx.jobs.submit(lambda job: change, title=f"Syncing {os.path.basename(path)}", priority="normal",
                             on_done=lambda job, value: self._apply_disk_change(state, value))

    def _apply_disk_change(self, state: _State, change: dict) -> None:
        with self._lock:
            if self._state is not state or not self._enabled() or not self._proj("watch"):
                return
            kind = change["type"]
            try:
                if kind == "created":
                    self._disk_created(state, change["path"])
                elif kind == "moved":
                    self._disk_moved(state, change["src"], change["path"])
                elif kind == "deleted":
                    self._disk_deleted(state, [change["path"]])
                elif kind == "deleted_dir":
                    prefix = norm(change["path"]).rstrip("\\/") + os.sep
                    self._disk_deleted(state, [p for p in state.index if p.startswith(prefix)])
            except PremiereError as exc:
                self.log.warning("disk change %s not applied: %s", kind, exc.message)
        self.ctx.ui.invalidate()

    def _disk_created(self, state: _State, path: str) -> None:
        if state.index.get(norm(path)) or not os.path.isfile(path):
            return
        folder = state.mapper.layout_folder_of(path)
        if folder is None or self._watch_ignored(state, path):
            return
        bin_path = state.mapper.bin_for_folder(folder)
        name = os.path.basename(path)
        entry = state.journal.append(J.OP_IMPORTED, name=name, to=path, bin_to=bin_path, copied=False)
        self._suppress.add(("import", norm(path)))
        try:
            created = self.ctx.premiere.import_files([path], bin_path)
        except PremiereError as exc:
            self._suppress.discard(("import", norm(path)))
            state.journal.mark_failed(entry["id"], exc.message)
            self.ctx.ui.toast("error", f"Could not import {name}")
            return
        if not created:
            self._suppress.discard(("import", norm(path)))
            state.journal.mark_failed(entry["id"], "nothing imported")
            return
        for item in created:
            self._index_set(state, item.node_id, None, path)
            state.items[item.node_id] = item
        state.journal.mark_done(entry["id"], node_id=created[0].node_id, node_ids=[i.node_id for i in created])
        self._emit(J.OP_IMPORTED, f"Imported {name} into {bin_path or 'the root bin'}")

    def _disk_moved(self, state: _State, src: str, dest: str) -> None:
        nodes = sorted(state.index.get(norm(src), set()))
        if not nodes:
            self._disk_created(state, dest)
            return
        same_dir = norm(os.path.dirname(src)) == norm(os.path.dirname(dest))
        op = J.OP_RENAMED if same_dir else J.OP_MOVED_DISK
        new_bin: Optional[str] = None
        if not same_dir and not self._watch_ignored(state, dest):
            folder = state.mapper.layout_folder_of(dest)
            new_bin = state.mapper.bin_for_folder(folder) if folder is not None else None
        for node in nodes:
            item = state.items.get(node)
            name = item.name if item else os.path.basename(src)
            bin_from = item.tree_path if item else ""
            new_name = None
            if same_dir and item is not None:
                old_file = os.path.basename(src)
                if item.name == old_file:
                    new_name = os.path.basename(dest)
                elif item.name == os.path.splitext(old_file)[0]:
                    new_name = os.path.splitext(os.path.basename(dest))[0]
            bin_to = new_bin if new_bin is not None else bin_from
            entry = state.journal.append(op, node_id=node, name=new_name or name, **{"from": src}, to=dest,
                                         bin_from=bin_from, bin_to=bin_to, name_from=name, name_to=new_name,
                                         copied=False)
            self._suppress.add(("media", node, norm(dest)))
            try:
                online = bool(self.ctx.premiere.relink(node, dest))
            except PremiereError as exc:
                online = False
                self.log.warning("relink after a disk move failed: %s", exc.message)
            if not online:
                state.journal.mark_failed(entry["id"], "relink failed")
                self.ctx.ui.toast("error", f"Could not relink {name}")
                continue
            self._index_set(state, node, src, dest)
            self._patch(state, node, media_path=dest, is_offline=False)
            if new_name:
                self.ctx.premiere.rename_item(node, new_name)
                self._patch(state, node, name=new_name)
            if norm_bin(bin_to) != norm_bin(bin_from):
                self._suppress.add(("bin", node, norm_bin(bin_to)))
                self.ctx.premiere.move_to_bin([node], bin_to)
                self._patch(state, node, tree_path=bin_to)
            state.journal.mark_done(entry["id"])
            self._emit(op, f"Relinked {new_name or name} after a {'rename' if same_dir else 'move'} on disk")
        self._refresh_derived(state)

    def _disk_deleted(self, state: _State, paths: list[str]) -> None:
        """Nothing is done to the project; the items show under "Offline" (docs/FILES.md 3.2)."""
        changed = False
        for path in paths:
            for node in state.index.get(norm(path), set()):
                item = state.items.get(node)
                if item is not None and not item.is_offline:
                    state.items[node] = replace(item, is_offline=True)
                    changed = True
        if changed:
            self._refresh_derived(state)

    # ------------------------------------------------------------------------------------------------ recovery

    def _recover(self, state: _State) -> None:
        """docs/FILES.md 1.3: resume or roll back entries an interrupted run left `pending`."""
        for entry in state.journal.pending():
            try:
                self._recover_entry(state, entry)
            except PremiereError as exc:
                self.log.warning("recovery of %s postponed: %s", entry.get("id"), exc.message)
            except OSError as exc:
                state.journal.mark_failed(entry["id"], f"recovery failed: {exc}")

    def _recover_entry(self, state: _State, entry: dict) -> None:
        op, node, target = entry.get("op"), entry.get("node_id"), entry.get("to")
        if target and is_inside(target, state.root) and os.path.exists(target + ".part"):
            os.remove(target + ".part")
        item = state.items.get(node) if node else None
        if op in (J.OP_FILED, J.OP_COLLECTED, J.OP_MOVED_BIN):
            if item is not None and same_path(item.media_path, target):
                state.journal.mark_done(entry["id"], recovered=True, deleted_from=False)
                return
            if item is not None and target and os.path.isfile(target) and same_path(item.media_path, entry.get("from")):
                self._suppress.add(("media", node, norm(target)))
                if self.ctx.premiere.relink(node, target):
                    self._index_set(state, node, entry.get("from"), target)
                    self._patch(state, node, media_path=target, is_offline=False)
                    bin_to = entry.get("bin_to") or ""
                    if op != J.OP_MOVED_BIN and norm_bin(bin_to) != norm_bin(item.tree_path):
                        self._suppress.add(("bin", node, norm_bin(bin_to)))
                        self.ctx.premiere.move_to_bin([node], bin_to)
                        self._patch(state, node, tree_path=bin_to)
                    state.journal.mark_done(entry["id"], resumed=True, deleted_from=False)
                    return
            if entry.get("copied") and target:
                self._delete_own_copy(state, target, entry_hash=entry.get("hash"), check_journal=False)
            state.journal.mark_failed(entry["id"], "interrupted; rolled back")
            return
        if op == J.OP_IMPORTED:
            done = bool(target and state.index.get(norm(target)))
            (state.journal.mark_done if done else state.journal.mark_failed)(
                entry["id"], **({"recovered": True} if done else {"error": "interrupted"}))
            return
        if op == J.OP_BINNED:
            done = item is not None and norm_bin(item.tree_path) == norm_bin(entry.get("bin_to"))
        else:
            done = item is not None and same_path(item.media_path, target)
        if done:
            state.journal.mark_done(entry["id"], recovered=True)
        else:
            state.journal.mark_failed(entry["id"], "interrupted")

    # ------------------------------------------------------------------------------------------------ jobs

    def _track(self, job, kind: str, label: str) -> None:
        with self._lock:
            self._jobs[job.id] = _Tracked(job=job, kind=kind, label=label)
        self.ctx.ui.invalidate()

    def _untrack(self, job) -> None:
        with self._lock:
            self._jobs.pop(getattr(job, "id", ""), None)

    def _on_progress(self, job, value, label) -> None:
        with self._lock:
            tracked = self._jobs.get(job.id)
            if tracked is not None:
                tracked.value, tracked.label = value, label or tracked.label
        self.ctx.ui.invalidate()

    def _busy(self, *kinds: str) -> bool:
        return any(t.kind in kinds for t in self._jobs.values())

    # ------------------------------------------------------------------------------------------------ collect, pack

    def _start_plan(self, state: _State, after: Optional[Callable[[CollectPlan], None]] = None) -> None:
        items = list(state.external)
        missing = [{"node_id": i.node_id, "name": i.name, "path": i.media_path} for i in state.offline]
        overrides = self._overrides()
        finished = {"done": False}

        def work(job):
            plan = CollectPlan(missing=list(missing))
            reserved: dict = {}
            for index, item in enumerate(items):
                if getattr(job, "cancelled", False):
                    break
                job.progress(index / max(1, len(items)), f"Checking {item.name}")
                path = item.media_path
                if not os.path.isfile(path):
                    plan.missing.append({"node_id": item.node_id, "name": item.name, "path": path})
                    continue
                decision = classify(self._probe(path), state.layout, overrides, path)
                target, reuse, file_hash = resolve_target(path, state.mapper.abs_folder(decision.folder),
                                                          reserved=reserved)
                file_hash = file_hash or safe_hash(path)
                reserved[norm(target)] = file_hash
                plan.entries.append(PlanEntry(node_id=item.node_id, name=item.name, source=path,
                                              folder=decision.folder, target=target, size=file_size(path),
                                              reuse=reuse, bin_from=item.tree_path, hash=file_hash,
                                              reason=decision.reason))
            return plan

        def done(job, plan):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is not state:
                    return
                if getattr(job, "cancelled", False):
                    state.plan = None
                    self.ctx.ui.invalidate()
                    return
                state.plan = plan
            if after is not None:
                after(plan)
            self.ctx.ui.invalidate()

        def failed(job, exc):
            finished["done"] = True
            self._untrack(job)
            if not getattr(job, "cancelled", False):
                self.log.warning("collect plan failed: %s", exc)
                self.ctx.ui.toast("error", "Could not plan the collect")
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title="Planning collect", priority="normal", on_progress=self._on_progress,
                                   on_done=done, on_error=failed)
        if not finished["done"]:
            self._track(job, "plan", "Checking media")

    def _run_plan(self, state: _State, plan: CollectPlan,
                  after: Optional[Callable[[list[_Copied], bool], None]] = None) -> None:
        tasks = [_Task(node_id=e.node_id, name=e.name, source=e.source, bin_from=e.bin_from, folder=e.folder)
                 for e in plan.entries]
        if not tasks:
            state.plan = None
            if after is not None:
                after([], False)
            return
        self._submit_copies(state, tasks, J.OP_COLLECTED, f"Collecting {len(tasks)} files", kind="collect",
                            after=after)

    def _pack(self, state: _State) -> None:
        def verify(*_args) -> None:
            with self._lock:
                if self._state is not state:
                    return
                self._take_snapshot(state)
                self._pack_job(state)

        if state.external:
            self._start_plan(state, after=lambda plan: self._run_plan(
                state, plan, after=lambda results, cancelled: None if cancelled else verify()))
        else:
            verify()

    def _pack_job(self, state: _State) -> None:
        items = [i for i in state.items.values() if self._is_media(i)]
        want_zip = self._proj("pack_zip")
        project_name = os.path.basename(state.project.path)
        finished = {"done": False}

        def work(job):
            report = build_pack_report(project_name, state.root, items, lambda p: is_inside(p, state.root))
            report_path = os.path.join(state.root, DECK_DIR, "pack-report.txt")
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            if want_zip:
                report.archive = free_zip_path(state.root)
            _write_text(report_path, report.text())
            if want_zip:
                try:
                    make_zip(state.root, report.archive, buffer_bytes=self._host("copy_buffer_mb") << 20,
                             on_bytes=lambda done, total: job.progress(done / total, "Writing archive"),
                             cancelled=lambda: bool(getattr(job, "cancelled", False)))
                except (CopyCancelled, OSError) as exc:
                    report.archive = None
                    _write_text(report_path, report.text())
                    if isinstance(exc, CopyCancelled):
                        return report
                    raise
            return report

        def done(job, report):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is state:
                    state.pack_report = report
            self.ctx.ui.toast("ok" if report.ok else "warn", report.line())
            self._emit("pack", report.line(), tone="ok" if report.ok else "warn")
            self.ctx.ui.invalidate()

        def failed(job, exc):
            finished["done"] = True
            self._untrack(job)
            self.log.warning("pack failed: %s", exc)
            self.ctx.ui.toast("error", "Could not pack the project")
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title="Packing project", priority="normal", on_progress=self._on_progress,
                                   on_done=done, on_error=failed)
        if not finished["done"]:
            self._track(job, "pack", "Checking media")

    # ------------------------------------------------------------------------------------------------ undo

    def _undo(self, state: _State, entry_id: Optional[str]) -> ActionResult:
        candidates = state.journal.undoable(self.UNDO_LIMIT)
        if not candidates and not entry_id:
            return ActionResult(toast=("neutral", "Nothing to undo"))
        entry = candidates[0] if not entry_id else next((e for e in candidates if e["id"] == entry_id), None)
        if entry is None:
            return ActionResult(toast=("warn", "This change can no longer be undone"))
        try:
            message = self._reverse(state, entry)
        except PremiereError as exc:
            self.log.warning("undo failed: %s", exc.message)
            return ActionResult(toast=("error", "Undo failed in Premiere Pro"))
        if message:
            return ActionResult(toast=("warn", message))
        self._emit("undo", f"Undone: {self._describe(entry).lower()}")
        self._refresh_derived(state)
        return ActionResult(toast=("ok", f"Undone: {OP_LABELS.get(entry['op'], entry['op']).lower()} {entry.get('name', '')}"))

    def _reverse(self, state: _State, entry: dict) -> Optional[str]:
        """Reverses one entry (docs/FILES.md 3.6). Returns a sentence when it cannot be undone, else None."""
        op, node = entry["op"], entry.get("node_id")
        original, current = entry.get("from"), entry.get("to")
        bin_from, bin_to = entry.get("bin_from"), entry.get("bin_to")
        pr = self.ctx.premiere
        extra: dict = {}
        if op != J.OP_IMPORTED and (not node or pr.item(node) is None):
            return "The item is no longer in the project"

        def relink_to(path: str) -> bool:
            self._suppress.add(("media", node, norm(path)))
            if not pr.relink(node, path):
                return False
            self._index_set(state, node, current, path)
            self._patch(state, node, media_path=path, is_offline=False)
            return True

        def move_back() -> None:
            if bin_from is not None and norm_bin(bin_from) != norm_bin(bin_to):
                self._suppress.add(("bin", node, norm_bin(bin_from)))
                pr.move_to_bin([node], bin_from)
                self._patch(state, node, tree_path=bin_from)

        if op in (J.OP_FILED, J.OP_COLLECTED):
            if not original or not os.path.isfile(original):
                return f"The original file is no longer at {original}"
            if not relink_to(original):
                return f"Could not relink {entry.get('name', '')}"
            move_back()
            extra["deleted"] = bool(entry.get("copied")) and self._delete_own_copy(state, current)
        elif op == J.OP_BINNED:
            move_back()
        elif op == J.OP_IMPORTED:
            ids = entry.get("node_ids") or ([node] if node else [])
            if not ids:
                return "The imported item is not known"
            pr.remove_items(ids)
            for item_id in ids:
                self._index_set(state, item_id, current, None)
                state.items.pop(item_id, None)
        elif op == J.OP_RENAMED:
            relinked = bool(original and os.path.isfile(original) and relink_to(original))
            renamed = False
            if entry.get("name_to") and entry.get("name_from"):
                pr.rename_item(node, entry["name_from"])
                self._patch(state, node, name=entry["name_from"])
                renamed = True
            if not relinked and not renamed:
                return "The old file name no longer exists on disk"
        elif op == J.OP_MOVED_DISK:
            if original and os.path.isfile(original):
                if not relink_to(original):
                    return f"Could not relink {entry.get('name', '')}"
            move_back()
        elif op == J.OP_MOVED_BIN:
            recreated = False
            if not original or not os.path.isfile(original):
                if not entry.get("deleted_from") or not current or not os.path.isfile(current):
                    return f"The previous file is no longer at {original}"
                self._suppress.add(("disk", norm(original)))
                copy_file(current, original, buffer_bytes=self._host("copy_buffer_mb") << 20)
                recreated = True
            if not relink_to(original):
                return f"Could not relink {entry.get('name', '')}"
            move_back()
            extra["recreated"] = recreated
            extra["deleted"] = bool(entry.get("copied")) and self._delete_own_copy(state, current)
        else:
            return "This change cannot be undone"
        state.journal.append(op, status=J.UNDONE, undo_of=entry["id"], node_id=node, name=entry.get("name"),
                             **{"from": current}, to=original, bin_from=bin_to, bin_to=bin_from,
                             hash=entry.get("hash"), copied=False, **extra)
        return None

    # ------------------------------------------------------------------------------------------------ actions

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        local = self.strip_prefix(action_id)
        if local.startswith("host:"):
            local = local[len("host:"):]
            handler = getattr(self, f"_host_{local}", None)
            if handler is None:
                return super().on_action(action_id, value, row)
            with self._lock:
                if self._state is None:
                    return ActionResult(toast=("neutral", "Open a project in Premiere Pro first"), rerender=False)
                return handler(self._state, value, row)
        handler = getattr(self, f"_act_{local}", None)
        if handler is None:
            return super().on_action(action_id, value, row)
        with self._lock:
            if self._state is None:
                return ActionResult(toast=("neutral", "Open a project in Premiere Pro first"), rerender=False)
            state = self._state
            if local != "setup" and local not in ("locate", "remove") and not self._enabled():
                return ActionResult(toast=("neutral", "Set up this project first"))
            return handler(state, value, row)

    def _act_setup(self, state: _State, value, row) -> ActionResult:
        if self._enabled():
            return ActionResult(toast=("neutral", "This project is already set up"))
        created = 0
        for folder in state.layout.all_folders():
            path = state.mapper.abs_folder(folder)
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
                created += 1
        os.makedirs(os.path.join(state.root, DECK_DIR), exist_ok=True)
        if self._proj("layout") != "custom":
            self._set_proj("layout", state.layout.id)
        self._set_proj("enabled", True)
        self._mark_root(state)
        bins = 0
        try:
            for folder in state.layout.leaf_folders():
                if folder.split("/")[0].casefold() == state.layout.project_dir.split("/")[0].casefold():
                    continue
                self.ctx.premiere.create_bin_path(state.mapper.bin_for_folder(folder))
                bins += 1
        except PremiereError as exc:
            self.log.warning("bins not created: %s", exc.message)
        state.journal.append("setup", status=J.DONE, name=os.path.basename(state.root), to=state.root,
                             folders_created=created, bins=bins, layout=state.layout.id)
        self._start_watcher(state)
        self._refresh_derived(state)
        return ActionResult(toast=("ok", f"Project set up · {len(state.layout.folders)} folders"))

    def _mark_root(self, state: _State) -> None:
        """When the Files root is the parent of the project file's folder (project file in `Project/` beside layout
        folders) but the host still binds project settings to the project file's folder, create
        `<root>/.premiere-deck/project.json` (create-only) so the host's root rule (docs/PREMIERE_API.md 2) agrees
        from the next open on. An existing file is never touched."""
        if not state.project.root or same_path(state.project.root, state.root):
            return
        marker = os.path.join(state.root, DECK_DIR, "project.json")
        if os.path.exists(marker):
            return
        payload = {"modules": {self.id: {"enabled": True, "layout": self._proj("layout"), "watch": self._proj("watch")}}}
        try:
            with open(marker, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except OSError as exc:
            self.log.warning("root marker not written: %s", exc)

    def _act_collect(self, state: _State, value, row) -> ActionResult:
        if self._busy("plan", "collect", "pack"):
            return ActionResult(toast=("neutral", "Collect is already running"))
        if not state.external and not state.offline:
            return ActionResult(toast=("neutral", "Nothing to collect"))
        self._start_plan(state)
        return None

    def _act_collect_run(self, state: _State, value, row) -> ActionResult:
        if self._busy("collect", "pack"):
            return ActionResult(toast=("neutral", "Collect is already running"))
        if state.plan is None:
            self._start_plan(state, after=lambda plan: self._run_plan(state, plan))
            return None
        if not state.plan.entries:
            state.plan = None
            return ActionResult(toast=("neutral", "Nothing to collect"))
        self._run_plan(state, state.plan)
        return None

    def _act_collect_discard(self, state: _State, value, row) -> ActionResult:
        state.plan = None
        return None

    def _act_cancel(self, state: _State, value, row) -> ActionResult:
        jobs = list(self._jobs.values())
        for tracked in jobs:
            tracked.job.cancel()
        return ActionResult(toast=("neutral", "Stopping") if jobs else None)

    def _act_pack(self, state: _State, value, row) -> ActionResult:
        if isinstance(value, dict) and isinstance(value.get("pack_zip"), bool):  # the Pack form's submit
            self._set_proj("pack_zip", value["pack_zip"])
        if self._busy("plan", "collect", "pack"):
            return ActionResult(toast=("neutral", "Collect or pack is already running"))
        self._pack(state)
        return None

    def _act_undo_last(self, state: _State, value, row) -> ActionResult:
        return self._undo(state, None)

    def _act_undo(self, state: _State, value, row) -> ActionResult:
        return self._undo(state, row or (value if isinstance(value, str) else None))

    def _act_watch(self, state: _State, value, row) -> ActionResult:
        on = bool(value) if value is not None else not self._proj("watch")
        self._set_proj("watch", on)
        if on:
            self._start_watcher(state)
        else:
            self._stop_watcher(state)
        return ActionResult(toast=("neutral", "Watching the project folder" if on else "Watching paused"))

    def _act_locate(self, state: _State, value, row) -> ActionResult:
        item = state.items.get(row or "")
        if item is None or not item.media_path:
            return ActionResult(toast=("warn", "No path is known for this item"))
        path = item.media_path
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                path = ""
                break
            path = parent
        if not path:
            return ActionResult(toast=("warn", "The folder of this item no longer exists"))
        self.ctx.host.reveal(path)
        return ActionResult(rerender=False)

    def _act_remove(self, state: _State, value, row) -> ActionResult:
        item = state.items.get(row or "")
        if item is None:
            return ActionResult(toast=("warn", "The item is no longer in the project"))
        entry = state.journal.append(J.OP_REMOVED, node_id=item.node_id, name=item.name,
                                     **{"from": item.media_path}, bin_from=item.tree_path, copied=False)
        try:
            self.ctx.premiere.remove_items([item.node_id])
        except PremiereError as exc:
            state.journal.mark_failed(entry["id"], exc.message)
            return ActionResult(toast=("error", f"Could not remove {item.name}"))
        state.journal.mark_done(entry["id"])
        state.items.pop(item.node_id, None)
        self._index_set(state, item.node_id, item.media_path, None)
        self._refresh_derived(state)
        return ActionResult(toast=("ok", f"Removed {item.name} from the project"))

    # host window controls ("files.host:<name>") --------------------------------------------------------------

    def on_setting_changed(self, key: str, scope: str, value: Any) -> None:
        """The host window's Settings card stored a value (docs/MODULES.md 5): `watch` starts or stops the watcher,
        the other keys are read live."""
        if scope == "project" and key == "watch" and isinstance(value, bool):
            with self._lock:
                state = self._state
            if state is not None:
                self._act_watch(state, value, None)

    def _host_project_setting(self, state: _State, value, row) -> ActionResult:
        changes = _pairs(value)
        applied = []
        for key, new in changes.items():
            if key in PROJECT_BOOLS and isinstance(new, bool):
                if key == "watch":
                    self._act_watch(state, new, None)
                else:
                    self._set_proj(key, new)
                applied.append(key)
        if not applied:
            return ActionResult(toast=("warn", "Unknown project setting"), rerender=False)
        return None

    def _host_layout_select(self, state: _State, value, row) -> ActionResult:
        choice = value.get("value") if isinstance(value, dict) else value
        if choice != "custom" and choice not in self._shipped():
            return ActionResult(toast=("warn", "Unknown layout"), rerender=False)
        if choice == "custom" and not os.path.isfile(os.path.join(state.root, DECK_DIR, CUSTOM_LAYOUT_FILE)):
            self._write_custom_layout(state, state.layout.to_json())
        self._set_proj("layout", choice)
        self._reload_layout(state)
        return None

    def _host_layout_save(self, state: _State, value, row) -> ActionResult:
        data = value.get("layout") if isinstance(value, dict) and "layout" in value else value
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return ActionResult(toast=("error", "The layout is not valid JSON"), rerender=False)
        try:
            layout = parse_layout(data)
        except LayoutError as exc:
            return ActionResult(toast=("error", f"Layout not saved: {exc}"), rerender=False)
        payload = layout.to_json()
        payload["id"], payload["name"] = "custom", str(data.get("name") or "Custom") if isinstance(data, dict) else "Custom"
        self._write_custom_layout(state, payload)
        self._set_proj("layout", "custom")
        self._reload_layout(state)
        return ActionResult(toast=("ok", "Layout saved"))

    def _host_layout_reset(self, state: _State, value, row) -> ActionResult:
        self._set_proj("layout", self._host("default_layout"))
        self._reload_layout(state)
        return ActionResult(toast=("ok", "Default layout restored"))

    def _host_overrides_save(self, state: _State, value, row) -> ActionResult:
        raw = value.get("overrides") if isinstance(value, dict) else value
        if not isinstance(raw, list):
            return ActionResult(toast=("warn", "Overrides are a list"), rerender=False)
        clean = [{k: v for k, v in (("source", o.source), ("name", o.name), ("to", o.to)) if v}
                 for o in parse_overrides(raw)]
        self._set_proj("overrides", clean)
        dropped = len(raw) - len(clean)
        text = "Overrides saved" + (f" · {dropped} invalid skipped" if dropped else "")
        return ActionResult(toast=("ok" if not dropped else "warn", text))

    def _host_journal_op(self, state: _State, value, row) -> ActionResult:
        return self._host_journal_filter(state, {"op": value}, row)

    def _host_journal_query(self, state: _State, value, row) -> ActionResult:
        return self._host_journal_filter(state, {"query": value}, row)

    def _host_override_add(self, state: _State, value, row) -> ActionResult:
        pairs = _pairs(value)
        match = str(pairs.get("match") or "").strip()
        if not match:
            return ActionResult(toast=("warn", "Enter a source folder or a name pattern"), rerender=False)
        is_folder = any(ch in match for ch in "\\/:") and not any(ch in match for ch in "*?")
        entry = {"source" if is_folder else "name": match, "to": pairs.get("to")}
        if not parse_overrides([entry]):
            return ActionResult(toast=("warn", "That override is not valid"), rerender=False)
        current = [o for o in self._proj("overrides") if isinstance(o, dict)]
        return self._host_overrides_save(state, current + [entry], row)

    def _host_override_remove(self, state: _State, value, row) -> ActionResult:
        current = [{k: v for k, v in (("source", o.source), ("name", o.name), ("to", o.to)) if v}
                   for o in parse_overrides(self._proj("overrides"))]
        try:
            index = int(row if row is not None else value)
            current.pop(index)
        except (TypeError, ValueError, IndexError):
            return ActionResult(toast=("warn", "Unknown override"), rerender=False)
        self._set_proj("overrides", current)
        return ActionResult(toast=("ok", "Override removed"))

    def _host_journal_filter(self, state: _State, value, row) -> ActionResult:
        pairs = _pairs(value)
        op = pairs.get("op", self._journal_filter["op"])
        query = pairs.get("query", self._journal_filter["query"])
        self._journal_filter = {"op": op if isinstance(op, str) else "", "query": query if isinstance(query, str) else ""}
        return None

    def _host_reveal(self, state: _State, value, row) -> ActionResult:
        self.ctx.host.reveal(state.root)
        return ActionResult(rerender=False)

    def _write_custom_layout(self, state: _State, payload: dict) -> None:
        _write_text(os.path.join(state.root, DECK_DIR, CUSTOM_LAYOUT_FILE), json.dumps(payload, indent=2) + "\n")

    def _reload_layout(self, state: _State) -> None:
        shipped = self._shipped()
        base = shipped.get(self._host("default_layout")) or shipped[DEFAULT_LAYOUT_ID]
        layout, error = self._choose_layout(state.root, base)
        state.layout, state.layout_error = layout, error
        state.mapper = Mapper(state.root, layout)
        self._refresh_derived(state)

    # ------------------------------------------------------------------------------------------------ rendering

    def section_status(self) -> Optional[dict]:
        state = self._state
        if state is None:
            return self.status("No project", "neutral")
        if not self._enabled():
            return self.status("Set up this project", "warn")
        busy = next((t for t in self._jobs.values() if t.kind in ("filing", "collect", "moving", "pack")), None)
        if busy is not None:
            return self.status(busy.label, "accent")
        if state.watcher_error:
            return self.status("Watcher stopped", "error")
        if state.watcher is None:
            return self.status("Paused", "neutral")
        return self.status(f"Watching · {os.path.basename(state.root)}", "ok")

    def render(self) -> list:
        with self._lock:
            state = self._state
            if state is None:
                return [self.text("Open a project in Premiere Pro to sort its media.", small=True)]
            nodes: list = []
            if state.layout_error:
                nodes.append(self.text(state.layout_error, tone="warn", small=True))
            if state.premiere_error:
                nodes.append(self.text("Premiere Pro did not answer; the lists may be out of date.", tone="warn",
                                       small=True))
            if not self._enabled():
                nodes.append(self.text(f"Setup creates the {state.layout.name.lower()} folders and bins in "
                                       f"{os.path.basename(state.root)}. Nothing is moved.", small=True))
                nodes.append(self.button("setup", "Set up project", tone="accent",
                                         hint="Creates the layout folders and the matching bins."))
                if state.external:
                    nodes.append(self.text(f"{len(state.external)} items are outside the project folder.", small=True))
                nodes.extend(self._offline_nodes(state))
                return nodes
            nodes.extend(self._job_nodes())
            nodes.extend(self._collect_nodes(state))
            pack_busy = self._busy("pack")
            nodes.append(self.button("pack", "Pack for sharing", busy=pack_busy,
                                     disabled=self._busy("plan", "collect") and not pack_busy,
                                     confirm=self._pack_question(state),
                                     hint="Collects what is outside, checks every clip and writes a report."))
            if state.pack_report is not None:
                nodes.append(self.text(state.pack_report.line(), tone="ok" if state.pack_report.ok else "warn",
                                       small=True))
            undoable = state.journal.undoable(self.UNDO_LIMIT)
            last = undoable[0] if undoable else None
            nodes.append(self.button("undo_last", "Undo last", disabled=last is None,
                                     hint=self._describe(last) if last else "Nothing to undo."))
            nodes.append(self.toggle("watch", "Watch folder", self._proj("watch"),
                                     hint=state.watcher_error or "Imports files added to the project folder."))
            nodes.append(self.text("Recent", small=True))
            nodes.append(self.list_("recent", [self._recent_row(e) for e in state.journal.recent(self.RECENT_COUNT)],
                                    empty="No changes yet."))
            nodes.extend(self._offline_nodes(state))
            return nodes

    def _job_nodes(self) -> list:
        visible = [t for t in self._jobs.values() if t.kind != "sync"]
        if not visible:
            return []
        nodes = [self.progress(t.value, t.label) for t in visible[:3]]
        nodes.append(self.button("cancel", "Stop", tone="warn", hint="Finished files stay filed."))
        return nodes

    def _collect_nodes(self, state: _State) -> list:
        count = len(state.external)
        plan = state.plan
        if plan is not None and (plan.entries or plan.missing):
            nodes = [self.text(f"{plan.summary()} · Run", small=True)]
            if plan.missing:
                nodes.append(self.text(f"{len(plan.missing)} offline items are listed, not collected.", tone="warn",
                                       small=True))
            nodes.append(self.row([
                self.button("collect_run", "Run", tone="accent", disabled=not plan.entries or self._busy("collect"),
                            confirm=f"Copy {plan.summary()} into the project folder?"),
                self.button("collect_discard", "Discard"),
            ]))
            return nodes
        label = f"Collect project · {count} external" if count else "Collect project"
        return [self.button("collect", label, busy=self._busy("plan"), disabled=self._busy("collect"),
                            hint="Lists media outside the project folder before copying it.")]

    @staticmethod
    def _pack_question(state: _State) -> Optional[str]:
        count = len(state.external)
        if not count:
            return None
        return f"Copy {count} file{'s' if count != 1 else ''} from outside into the project folder, then pack?"

    def _offline_nodes(self, state: _State) -> list:
        if not state.offline:
            return []
        rows = []
        for item in state.offline[:50]:
            rows.append(self.list_row(item.node_id, item.name, subtitle=item.media_path or "No path", tone="warn",
                                      actions=[self.row_action("locate", "Locate"),
                                               self.row_action("remove", "Remove", tone="warn",
                                                               confirm=f"Remove {item.name} from the project? "
                                                                       "The file on disk is not touched.")]))
        return [self.text(f"Offline · {len(state.offline)}", tone="warn", small=True),
                self.list_("offline", rows, empty="")]

    def _describe(self, entry: dict) -> str:
        return f"{OP_LABELS.get(entry.get('op'), entry.get('op'))} {entry.get('name', '')}".strip()

    def _recent_row(self, entry: dict) -> dict:
        op = entry.get("op")
        status = entry.get("status")
        when = ago(entry.get("at"))
        state = self._state
        if status == J.UNDONE:
            subtitle = f"Undone · {OP_LABELS.get(op, op).lower()}"
            tone = "neutral"
        elif status == J.FAILED:
            subtitle = f"Failed · {entry.get('error', OP_LABELS.get(op, op))}"
            tone = "error"
        elif op in (J.OP_FILED, J.OP_COLLECTED, J.OP_MOVED_DISK, J.OP_MOVED_BIN):
            dest = entry.get("bin_to") or _folder_name(entry.get("to"))
            src = entry.get("bin_from") if op == J.OP_MOVED_BIN else _folder_name(entry.get("from"))
            subtitle = f"{src or 'Root'} → {dest or 'Root'}"
            tone = "neutral"
        elif op == J.OP_BINNED:
            subtitle = f"{entry.get('bin_from') or 'Root'} → {entry.get('bin_to') or 'Root'}"
            tone = "neutral"
        elif op == J.OP_IMPORTED:
            subtitle = f"Imported into {entry.get('bin_to') or 'Root'}"
            tone = "neutral"
        elif op == J.OP_RENAMED:
            subtitle = f"Renamed from {entry.get('name_from') or os.path.basename(entry.get('from') or '')}"
            tone = "neutral"
        elif op == "setup":
            subtitle = "Layout folders created"
            tone = "neutral"
        else:
            subtitle = OP_LABELS.get(op, str(op))
            tone = "neutral"
        if status == J.PENDING:
            subtitle = "In progress · " + subtitle
        title = entry.get("name") or (os.path.basename(state.root) if state else "")
        return self.list_row(entry["id"], str(title), subtitle=f"{subtitle} · {when}" if when else subtitle, tone=tone)

    # host window page (docs/FILES.md 7, HOST_UI.md 4.2; block shapes of docs/BRIDGE_API.md 5) ------------------

    def render_host(self) -> Optional[dict]:
        with self._lock:
            state = self._state
            if state is None:
                layout = self._shipped().get(self._host("default_layout")) or self._shipped()[DEFAULT_LAYOUT_ID]
                return {"title": self.title, "status": self.status("No project open", "neutral"), "blocks": [
                    {"type": "text", "id": "no_project", "text": "Open a project in Premiere Pro to file its media. "
                                                                  "The layout below is what a new project gets.",
                     "tone": "neutral", "small": True},
                    *self._layout_blocks(layout, state=None),
                ]}
            enabled = self._enabled()
            status = self.section_status() or self.status("", "neutral")
            day_start = time.time() - 24 * 3600
            entries = state.journal.entries()
            filed_today = sum(1 for e in entries if e.get("op") in (J.OP_FILED, J.OP_COLLECTED, J.OP_IMPORTED)
                              and e.get("status") == J.DONE and (e.get("at") or 0) >= day_start)
            blocks: list = [
                {"type": "cards", "id": "status", "items": [
                    {"id": "state", "title": "Status", "value": status["text"], "detail": state.root,
                     "tone": status["tone"], "progress": None, "actions": []},
                    {"id": "filed", "title": "Filed today", "value": str(filed_today), "detail": "Last 24 hours",
                     "tone": "neutral", "progress": None, "actions": []},
                    {"id": "external", "title": "Outside the folder", "value": str(len(state.external)),
                     "detail": "Collect copies them in", "tone": "warn" if state.external else "ok",
                     "progress": None, "actions": []},
                    {"id": "offline", "title": "Offline", "value": str(len(state.offline)),
                     "detail": "Media not found", "tone": "warn" if state.offline else "ok", "progress": None,
                     "actions": []},
                ]},
                {"type": "actions", "id": "main", "items": self._host_buttons(state, enabled)},
            ]
            blocks.extend({"type": "progress", "id": f"job_{t.job.id}", "value": t.value, "label": t.label}
                          for t in list(self._jobs.values())[:3])
            if state.layout_error:
                blocks.append({"type": "text", "id": "layout_error", "text": state.layout_error, "tone": "warn",
                               "small": True})
            if enabled:
                blocks.extend(self._journal_blocks(state, entries))
                blocks.extend(self._plan_blocks(state))
                pack_fields = [{"key": "pack_zip", "label": "Write a zip archive", "kind": "bool",
                                "value": self._proj("pack_zip"), "hint": "The archive is written beside the project "
                                                                          "folder.", "scope": "project"}]
                blocks.append({"type": "form", "id": "pack_options", "title": "Pack for sharing",
                               "hint": state.pack_report.line() if state.pack_report else None,
                               "action": self._id("host:project_setting"), "fields": pack_fields,
                               "submit": self.button("pack", "Pack now", busy=self._busy("pack"),
                                                     disabled=self._busy("plan", "collect"),
                                                     confirm=self._pack_question(state))})
                # The project settings are drawn once by the host window's Settings card (docs/MODULES.md 5).
            blocks.extend(self._layout_blocks(state.layout, state=state))
            return {"title": self.title, "status": status, "blocks": blocks}

    def _host_buttons(self, state: _State, enabled: bool) -> list:
        if not enabled:
            return [self.button("setup", "Set up project", tone="accent",
                                hint="Creates the layout folders and the matching bins."),
                    self.button("host:reveal", "Show folder")]
        undoable = state.journal.undoable(self.UNDO_LIMIT)
        return [
            self.button("collect", "Plan collect", busy=self._busy("plan"), disabled=self._busy("collect")),
            self.button("undo_last", "Undo last", disabled=not undoable,
                        hint=self._describe(undoable[0]) if undoable else "Nothing to undo."),
            self.button("watch", "Pause watching" if self._proj("watch") else "Resume watching"),
            self.button("cancel", "Stop", tone="warn", disabled=not self._jobs),
            self.button("host:reveal", "Show folder"),
        ]

    def _journal_blocks(self, state: _State, entries: list[dict]) -> list:
        undoable = {e["id"] for e in state.journal.undoable(self.UNDO_LIMIT)}
        flt = self._journal_filter
        shown = J.filter_entries(reversed(entries), flt["op"], flt["query"])[:self.HOST_JOURNAL_COUNT]
        rows = []
        for entry in shown:
            status = entry.get("status")
            tone = {"failed": "error", "pending": "accent", "undone": "neutral"}.get(status, "ok")
            rows.append({"id": entry["id"], "tone": tone,
                         "at": ago(entry.get("at")), "op": OP_LABELS.get(entry.get("op"), entry.get("op")),
                         "name": entry.get("name") or "",
                         "from": entry.get("from") or entry.get("bin_from") or "",
                         "to": entry.get("to") or entry.get("bin_to") or "",
                         "status": {"text": status, "tone": tone},
                         "actions": [self.button("undo", "Undo")] if entry["id"] in undoable else []})
        op_options = [{"value": "", "label": "All changes"}] + \
            [{"value": op, "label": label} for op, label in OP_LABELS.items()]
        return [{"type": "table", "id": "journal", "title": "Journal",
                 "columns": [{"key": "at", "label": "When"}, {"key": "op", "label": "Change"},
                             {"key": "name", "label": "Name"}, {"key": "from", "label": "From", "mono": True},
                             {"key": "to", "label": "To", "mono": True}, {"key": "status", "label": "Status"}],
                 "rows": rows, "empty": "No changes yet.",
                 "filters": [self.select("host:journal_op", "Change", flt["op"], op_options)],
                 "search": self.input("host:journal_query", "Search", flt["query"], placeholder="File name")}]

    def _plan_blocks(self, state: _State) -> list:
        plan = state.plan
        if plan is None:
            return [{"type": "list", "id": "collect_plan", "title": "Collect plan", "rows": [],
                     "empty": f"{len(state.external)} items are outside the project folder. "
                              "Plan collect lists them with their target folders."}]
        groups = [{"id": g["folder"], "folder": g["folder"], "count": str(g["count"]), "size": fmt_size(g["bytes"]),
                   "actions": []} for g in plan.groups()]
        rows = [{"id": e.node_id, "title": e.name, "subtitle": f"→ {e.folder}" + (" · already there" if e.reuse else ""),
                 "meta": fmt_size(e.size), "tone": "neutral", "thumb": None, "actions": []} for e in plan.entries]
        rows += [{"id": "missing:" + m["node_id"], "title": m["name"], "subtitle": f"Offline · {m['path'] or 'no path'}",
                  "meta": "", "tone": "warn", "thumb": None, "actions": []} for m in plan.missing]
        return [
            {"type": "table", "id": "collect_totals", "title": f"Collect plan · {plan.summary()}",
             "columns": [{"key": "folder", "label": "Folder"}, {"key": "count", "label": "Files", "align": "right"},
                         {"key": "size", "label": "Size", "align": "right"}],
             "rows": groups, "empty": "Nothing to collect."},
            {"type": "list", "id": "collect_plan", "title": "Files", "rows": rows, "empty": "Nothing to collect."},
            {"type": "actions", "id": "collect_actions", "items": [
                self.button("collect_run", "Run", tone="accent", disabled=not plan.entries, busy=self._busy("collect"),
                            confirm=f"Copy {plan.summary()} into the project folder?"),
                self.button("collect_discard", "Discard")]},
        ]

    def _layout_blocks(self, layout: Layout, *, state: Optional[_State]) -> list:
        options = [{"value": lid, "label": item.name} for lid, item in self._shipped().items()]
        options.append({"value": "custom", "label": "Custom (this project)"})
        editable = state is not None
        rules = [{"id": str(index), "order": str(index), "match": _describe_match(rule.match), "to": rule.to,
                  "actions": []} for index, rule in enumerate(layout.rules, start=1)]
        fields: list = [{"key": "layout", "label": "Layout", "kind": "select", "options": options, "scope": "project",
                         "value": self._proj("layout") if editable else layout.id}]
        if editable:
            fields.append(self.input("host:layout_save", "Layout JSON", json.dumps(layout.to_json()),
                                     placeholder="{\"folders\": [...], \"rules\": [...]}", submit_label="Save layout"))
        blocks = [
            {"type": "form", "id": "layout", "title": "Layout", "action": self._id("host:layout_select"),
             "hint": "Rules and folders apply to this project; saving makes the layout custom.",
             "fields": fields},
            {"type": "list", "id": "layout_folders", "title": "Folders",
             "rows": [{"id": f, "title": f, "subtitle": "Project file" if f == layout.project_dir else "",
                       "meta": "", "tone": "neutral", "thumb": None, "actions": []} for f in layout.folders],
             "empty": ""},
            {"type": "table", "id": "layout_rules", "title": "Rules", "hint": "First match wins.",
             "columns": [{"key": "order", "label": "#", "align": "right"}, {"key": "match", "label": "Match"},
                         {"key": "to", "label": "Folder"}],
             "rows": rules, "empty": ""},
        ]
        if editable:
            overrides = parse_overrides(self._proj("overrides"))
            folder_options = [{"value": f, "label": f} for f in layout.all_folders()]
            blocks.append({"type": "table", "id": "overrides", "title": "Overrides",
                           "hint": "Checked before the rules: a source folder or a file name pattern.",
                           "columns": [{"key": "match", "label": "Source folder or name", "mono": True},
                                       {"key": "to", "label": "Folder"}],
                           "rows": [{"id": str(i), "match": o.source or o.name, "to": o.to,
                                     "actions": [self.button("host:override_remove", "Remove")]}
                                    for i, o in enumerate(overrides)],
                           "empty": "No overrides."})
            blocks.append({"type": "form", "id": "override_add", "title": "Add an override",
                           "fields": [{"key": "match", "label": "Source folder or name pattern", "kind": "text",
                                       "value": "", "hint": "D:\\Downloads\\SFX or *_sfx*"},
                                      {"key": "to", "label": "Folder", "kind": "select",
                                       "value": folder_options[0]["value"] if folder_options else "",
                                       "options": folder_options}],
                           "submit": self.button("host:override_add", "Add")})
        return blocks


def _describe_match(match: dict) -> str:
    if not match:
        return "anything else"
    parts = []
    for key, value in match.items():
        if key == "ext":
            parts.append("ext " + ", ".join(value))
        elif key == "name":
            parts.append(f"name ~ {value}")
        elif key == "max_duration":
            parts.append(f"up to {value:g} s")
        elif key == "min_duration":
            parts.append(f"from {value:g} s")
        elif isinstance(value, bool):
            parts.append(key.replace("_", " ") if value else "no " + key.replace("_", " ").replace("has ", ""))
        else:
            parts.append(str(value))
    return " · ".join(parts)


def _pairs(value: Any) -> dict:
    """{"key": k, "value": v} or {k: v, ...} from a host window control."""
    if isinstance(value, dict):
        if "key" in value and "value" in value and isinstance(value["key"], str):
            return {value["key"]: value["value"]}
        return dict(value)
    return {}


def _write_text(path: str, text: str) -> None:
    """Atomic write of a file inside `.premiere-deck` (our own files only)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
