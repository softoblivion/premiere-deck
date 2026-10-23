"""Files module (docs/FILES.md): media sorted the same way on disk and in the project panel; Sort project, Sync all
media, Sync used media, Make ZIP, Undo.

Threads: hooks and job callbacks run on the host's module thread; probes, the classifier, copies, moves, the Sort
plan, Sync and ZIP run as jobs (priority `high`); a job reaches the module thread for each finished file through
`ctx.post` (relink and bin move), and its `on_done` reports the result after those posts (the module thread is one
FIFO queue). The disk watcher settles changes on its own thread and hops onto the module thread by submitting a tiny
job whose `on_done` applies the change. Premiere is only called from the module thread. Shared state is guarded by
one re-entrant lock.

Invariants (docs/FILES.md 1): files outside the root are never moved, renamed or deleted; inside the root files are
moved between layout folders only on Sort or a bin move, journaled and undoable; the module only deletes files it
created itself (a journaled copy whose content hash still matches) and only inside the root.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from premiere_deck.core.media import MediaInfo, is_media_extension, kind_from_extension
from premiere_deck.modules.base import ActionResult, Module, SettingField
from premiere_deck.premiere.model import KIND_CLIP, KIND_FILE, PremiereError, ProjectInfo, ProjectItem, Snapshot

from . import journal as J
from .classify import Decision, classify, parse_overrides
from .layout import (
    CUSTOM_LAYOUT_FILE, DECK_DIR, DEFAULT_LAYOUT_ID, Layout, LayoutError, Mapper, detect_root, is_inside, is_under,
    load_layout_file, masks_folder, norm, norm_bin, parse_layout, same_path, shipped_layouts,
)
from .media_class import MediaClassifier
from .sync import (
    CopyCancelled, DiskWatcher, PlanEntry, Suppression, ZipReport, build_zip_report, copy_file, failure_reason,
    file_size, fmt_size, free_bytes, free_zip_path, make_zip, move_file, pack_files, plural, resolve_target,
    safe_hash, write_check,
)

HOST_DEFAULTS = {"toast_filed": True, "copy_buffer_mb": 16, "stable_seconds": 2, "default_layout": DEFAULT_LAYOUT_ID}
INT_RANGES = {"copy_buffer_mb": (1, 256), "stable_seconds": (1, 60)}
PROJECT_DEFAULTS = {"enabled": False, "layout": DEFAULT_LAYOUT_ID, "overrides": [], "enforce_bins": True,
                    "mirror_bins": True, "import_exports": False, "watch": True}
PROJECT_BOOLS = ("enforce_bins", "mirror_bins", "import_exports", "watch")

LONG_KINDS = ("sort", "sync_all", "sync_used", "zip")  # at most one of these runs at a time (docs/FILES.md 3)
STOPPED_WORD = {"sort": "Sort", "sync_all": "Sync", "sync_used": "Sync", "zip": "ZIP"}
REMOVED_ACTIONS = {"collect", "collect_run", "collect_discard", "pack", "cancel"}  # 1.x ids, answered with nothing
NO_SETUP_NEEDED = {"setup", "locate", "remove", "open_folder"}
NO_WRITE_ACCESS = "No write access to the project folder"

OP_LABELS = {J.OP_FILED: "Filed", J.OP_SORTED: "Sorted", J.OP_SYNCED: "Synced", J.OP_ZIPPED: "Made ZIP",
             J.OP_COLLECTED: "Collected", J.OP_BINNED: "Moved to bin", J.OP_IMPORTED: "Imported",
             J.OP_RENAMED: "Renamed", J.OP_MOVED_DISK: "Moved on disk", J.OP_MOVED_BIN: "Moved with bin",
             J.OP_REMOVED: "Removed", J.OP_SETUP: "Set up", "packed": "Packed"}

HINTS = {
    "sort": "Moves the media inside the project folder into the layout folders and bins.",
    "sync_all": "Copies every file from outside the project folder into Media.",
    "sync_used": "Copies the files used in sequences from outside the project folder into Media.",
    "zip": "Writes the project folder as a ZIP archive beside it.",
}


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


def _top(folder: Optional[str]) -> str:
    return (folder or "").split("/")[0].casefold()


@dataclass
class _Tracked:
    job: Any
    kind: str  # filing | moving | sort | sync_all | sync_used | zip
    label: str
    value: Optional[float] = None
    detail: Optional[str] = None


@dataclass
class _Task:
    node_id: str
    name: str
    source: str
    bin_from: str
    folder: Optional[str] = None  # decided already; None = classify in the job


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
class _Run:
    """Counters of one Sort or Sync job, updated on the module thread by the per-file posts."""

    planned: int = 0
    done: int = 0
    bytes: int = 0
    failures: list = field(default_factory=list)  # (name, reason)
    offline: list = field(default_factory=list)  # names
    nothing_text: str = ""
    error: Optional[tuple[str, Optional[str]]] = None  # (text, detail) of a refusal inside the job


@dataclass
class _State:
    """Per open project; replaced on every project_opened so late job callbacks of a closed project are ignored."""

    project: ProjectInfo
    root: str
    layout: Layout
    mapper: Mapper
    journal: J.Journal
    masks: Optional[str] = None
    layout_error: str = ""
    items: dict = field(default_factory=dict)  # node_id -> ProjectItem (last snapshot)
    index: dict = field(default_factory=dict)  # norm media path -> set(node ids)
    baseline_ready: bool = False
    offline: list = field(default_factory=list)
    external: list = field(default_factory=list)  # media outside the root or loose in it (the intercept's scope)
    outside: list = field(default_factory=list)  # media outside the root (Sync)
    inside_count: int = 0
    sort_estimate: int = 0
    used_count: Optional[int] = None  # used external items, None before the first count
    used_at: float = 0.0
    used_timer: Optional[threading.Timer] = None
    zip_report: Optional[ZipReport] = None
    last_result: Optional[dict] = None  # {"button", "outcome", "text", "detail", "at"}
    results: dict = field(default_factory=dict)  # button -> last result
    write_ok: Optional[bool] = None  # the intercept's write check of this project session
    watcher: Optional[DiskWatcher] = None
    watcher_error: str = ""
    premiere_error: str = ""
    sync_timer: Optional[threading.Timer] = None  # pending coalesced sync (on_event); None = nothing pending
    sync_token: Optional[object] = None  # identity of the pending sync; a callback of an older timer is ignored
    sync_first: float = 0.0  # monotonic time of the first event of the pending burst


class FilesModule(Module):
    id = "files"
    title = "Files"
    icon = "folder"
    description = "Keeps project media sorted on disk and in bins."
    order = 10
    events = ["project.opened", "project.closed", "project.items_added", "project.changed"]
    requires = ["ffprobe"]
    default_enabled = True

    WATCH_DEBOUNCE = 1.5
    SYNC_DELAY = 0.3  # quiet window after the last Premiere event before the project is scanned once
    SYNC_MAX_WAIT = 2.0  # a burst that never goes quiet is scanned at most this long after its first event
    USED_INTERVAL = 10.0  # the used-in-sequences count is recomputed at most this often
    SUPPRESS_TTL = 15.0
    TOAST_MERGE_SECONDS = 2.0
    UNDO_LIMIT = 50
    JOURNAL_PREVIEW = 3
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
        self._journal_all = False
        self._layouts: Optional[dict[str, Layout]] = None
        self._classifier = MediaClassifier(lambda args: self.ctx.tools.ffmpeg_pipe(args))

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
            masks = masks_folder(project.path)
            state = _State(project=project, root=root, layout=layout, mapper=Mapper(root, layout, masks),
                           journal=J.Journal(os.path.join(root, DECK_DIR, "journal.jsonl")), masks=masks,
                           layout_error=error)
            self._state = state
            snap = self._take_snapshot(state)
            if self._enabled():
                self._cleanup_journal(state)
                self._recover(state)
                self._start_watcher(state)
                if snap is not None:
                    self._refresh_used(state, force=True)
        self.ctx.ui.invalidate()

    def project_closed(self) -> None:
        with self._lock:
            state, self._state = self._state, None
            jobs = list(self._jobs.values())
            self._jobs.clear()
            if state is not None:
                self._cancel_sync(state)
                timer, state.used_timer = state.used_timer, None
                if timer is not None:
                    timer.cancel()
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

    def _overrides(self, state: Optional[_State] = None):
        state = state or self._state
        return parse_overrides(self._proj("overrides"), state.layout.media_dir if state else None)

    def _excluded(self, state: _State, path: str, *, check_hidden: bool = False) -> bool:
        return state.mapper.excluded(path, check_hidden=check_hidden)

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
        offline, external, outside = [], [], []
        inside = estimate = 0
        for item in state.items.values():
            if not self._is_media(item):
                continue
            exists = os.path.isfile(item.media_path)
            if item.is_offline or not exists:
                offline.append(item)
                continue
            if self._excluded(state, item.media_path):
                continue
            if not is_inside(item.media_path, state.root):
                external.append(item)
                outside.append(item)
                continue
            inside += 1
            if not state.mapper.in_layout(item.media_path):
                external.append(item)
            folder = state.mapper.deepest_layout_folder(item.media_path)
            if folder is None or state.layout.in_media_dir(folder):
                estimate += 1
        state.offline = sorted(offline, key=lambda i: i.name.casefold())
        state.external = external
        state.outside = outside
        state.inside_count = inside
        state.sort_estimate = estimate

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

    # used in sequences (docs/FILES.md 3.5) -----------------------------------------------------------------------

    def _used_ids(self) -> set[str]:
        """Node ids of project items used in at least one sequence (module thread; Premiere calls)."""
        used: set[str] = set()
        for sequence in self.ctx.premiere.sequences():
            for track_item in self.ctx.premiere.track_items(sequence.sequence_id):
                if track_item.node_id:
                    used.add(track_item.node_id)
        return used

    def _refresh_used(self, state: _State, *, force: bool = False) -> None:
        """Recount the used external items, at most once per USED_INTERVAL; a skipped recount is scheduled."""
        now = time.monotonic()
        wait = state.used_at + self.USED_INTERVAL - now
        if not force and state.used_at and wait > 0:
            if state.used_timer is None:
                def fire() -> None:
                    try:
                        self.ctx.post(lambda: self._used_due(state))
                    except Exception:  # noqa: BLE001
                        self.log.exception("could not post the used count")

                timer = threading.Timer(wait, fire)
                timer.daemon = True
                state.used_timer = timer
                timer.start()
            return
        state.used_at = now
        try:
            used = self._used_ids()
        except PremiereError as exc:
            self.log.info("used count skipped: %s", exc.message)
            return
        state.used_count = sum(1 for item in state.outside if item.node_id in used)

    def _used_due(self, state: _State) -> None:
        with self._lock:
            if self._state is not state:
                return
            state.used_timer = None
            self._refresh_used(state, force=True)
        self.ctx.ui.invalidate()

    # ------------------------------------------------------------------------------------------------ results

    def _result(self, button: str, outcome: str, text: str, detail: Optional[str] = None) -> None:
        """One result per pressed control (docs/MODULES.md 4) through `ctx.ui.result`; remembered for the host
        page's last-result row. A host without `ui.result` gets a toast instead."""
        record = {"button": button, "outcome": outcome, "text": text, "detail": detail, "at": time.time()}
        state = self._state
        if state is not None:
            state.last_result = record
            state.results[button] = record
        sender = getattr(self.ctx.ui, "result", None)
        if callable(sender):
            sender(button, outcome, text, detail)
        else:
            self.ctx.ui.toast(outcome, text if not detail else f"{text} · {detail}")

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
            changed = len(snap.items) != len(before)
            for item in snap.items:
                old = before.get(item.node_id)
                if old is None:
                    changed = True
                    if self._is_media(item):
                        if self._suppress.consume(("import", norm(item.media_path))):
                            continue
                        added.append(item)
                    continue
                if norm_bin(old.tree_path) != norm_bin(item.tree_path):
                    changed = True
                    if self._suppress.consume(("bin", item.node_id, norm_bin(item.tree_path))):
                        continue
                    if self._is_media(item) and self._proj("mirror_bins"):
                        self._mirror_bin_move(state, old, item)
                elif old.media_path != item.media_path and item.media_path:
                    changed = True
                    self._suppress.consume(("media", item.node_id, norm(item.media_path)))
            if added:
                self._intercept(state, added)
            if changed:
                self._refresh_used(state)
        self.ctx.ui.invalidate()

    def _intercept(self, state: _State, items: list[ProjectItem]) -> None:
        """docs/FILES.md 3.1 for new project items."""
        tasks: list[_Task] = []
        for item in items:
            path = item.media_path
            if not path or self._excluded(state, path):
                continue
            if not os.path.isfile(path):
                continue  # offline: listed, not fixed
            if is_inside(path, state.root) and state.mapper.in_layout(path):
                if self._proj("enforce_bins"):
                    self._enforce_bin(state, item)
                continue
            tasks.append(_Task(node_id=item.node_id, name=item.name, source=path, bin_from=item.tree_path))
        if not tasks:
            return
        if state.write_ok is None:
            reason = write_check(state.root)
            state.write_ok = reason is None
            if reason is not None:
                self.log.warning("the intercept stops for this session: %s", reason)
                self.ctx.ui.toast("error", NO_WRITE_ACCESS)
        if not state.write_ok:
            return
        title = f"Filing {tasks[0].name}" if len(tasks) == 1 else f"Filing {len(tasks)} files"
        self._submit_copies(state, tasks, J.OP_FILED, title, kind="filing")

    def _enforce_bin(self, state: _State, item: ProjectItem) -> None:
        folder = state.mapper.layout_folder_of(item.media_path)
        if folder is None:
            return
        target_bin = state.mapper.bin_for_folder(folder)
        if norm_bin(item.tree_path) == norm_bin(target_bin):
            return
        self._bin_move(state, item.node_id, item.name, item.media_path, item.tree_path, target_bin)

    def _bin_move(self, state: _State, node: str, name: str, path: str, bin_from: str, target_bin: str) -> bool:
        """Journal `binned` and move one item to a bin without any disk change (module thread)."""
        entry = state.journal.append(J.OP_BINNED, node_id=node, name=name, **{"from": path}, to=path,
                                     bin_from=bin_from, bin_to=target_bin, copied=False)
        self._suppress.add(("bin", node, norm_bin(target_bin)))
        try:
            self.ctx.premiere.move_to_bin([node], target_bin)
        except PremiereError as exc:
            self._suppress.discard(("bin", node, norm_bin(target_bin)))
            state.journal.mark_failed(entry["id"], exc.message)
            return False
        self._patch(state, node, tree_path=target_bin)
        state.journal.mark_done(entry["id"])
        self._emit(J.OP_BINNED, f"Moved {name} to the {target_bin} bin")
        return True

    # classification -----------------------------------------------------------------------------------------------

    def _probe(self, path: str) -> MediaInfo:
        try:
            return self.ctx.tools.ffprobe(path)
        except Exception as exc:  # noqa: BLE001 - a probe failure classifies by extension (docs/FILES.md 4)
            self.log.info("probe failed for %s: %s", path, exc)
            return MediaInfo(path=path, kind=kind_from_extension(path), ext=Path(path).suffix.lower().lstrip("."),
                             size=file_size(path), probe_error=str(exc))

    def _decide(self, state: _State, path: str, overrides=None) -> Decision:
        """Probe, overrides, rules; the classifier of docs/FILES.md 3.8 runs when a rule needs it (job thread)."""
        overrides = self._overrides(state) if overrides is None else overrides
        return classify(self._probe(path), state.layout, overrides, path, enrich=self._classifier.enrich)

    # copies (intercept) -------------------------------------------------------------------------------------------

    def _copy_one(self, state: _State, job, task: _Task, op: str, on_bytes) -> _Copied:
        """Job thread: classify, resolve the target, journal `pending`, copy. Premiere is not touched here."""
        if not os.path.isfile(task.source):
            return _Copied(task, error="source not found")
        folder = task.folder
        if folder is None:
            folder = self._decide(state, task.source).folder
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
                reason = failure_reason(exc, target=target, source=task.source)
                state.journal.mark_failed(entry["id"], reason)
                self.log.warning("copy of %s failed (%s): %s", task.source, reason, exc)
                return _Copied(task, error=reason)
        return _Copied(task, entry=entry, target=target, folder=folder, reuse=reuse)

    def _submit_copies(self, state: _State, tasks: list[_Task], op: str, title: str, *, kind: str) -> None:
        results: list[_Copied] = []
        finished = {"done": False}

        def work(job):
            total = sum(file_size(t.source) for t in tasks) or 1
            done = [0]
            current = [""]

            def on_bytes(count: int) -> None:
                done[0] += count
                job.progress(done[0] / total, f"Copying {current[0]}")

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
            with self._lock:
                if self._state is not state:
                    self.log.info("%s finished after the project closed; the journal resumes it", title)
                    return
                self._apply_copies(state, results, op)
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title=title, priority="high", on_progress=self._on_progress,
                                   on_done=finish, on_error=lambda j, exc: finish(j, exc))
        if not finished["done"]:
            self._track(job, kind, title)

    def _apply_copies(self, state: _State, results: list[_Copied], op: str) -> list[tuple[str, str]]:
        """Module thread: relink, move to bin, journal done; roll back a copy whose relink fails."""
        filed: list[tuple[str, str]] = []
        errors: list[tuple[str, str]] = []
        for result in results:
            entry = result.entry
            if entry is None:
                if result.error:
                    errors.append((result.task.name, result.error))
                continue
            node = result.task.node_id
            source = result.task.source
            if not self._relink(state, node, result.target, old=source):
                if entry.get("copied"):
                    self._delete_own_copy(state, result.target, entry_hash=entry.get("hash"), check_journal=False)
                state.journal.mark_failed(entry["id"], "relink failed")
                self.ctx.ui.toast("error", f"Could not relink {result.task.name}")
                continue
            bin_to = self._to_bin(state, node, entry.get("bin_to") or "", result.task.bin_from)
            state.journal.mark_done(entry["id"], bin_to=bin_to)
            filed.append((result.task.name, result.folder))
        if filed:
            self._emit(op, f"{OP_LABELS[op]} {filed[0][0]} into {filed[0][1]}" if len(filed) == 1
                       else f"{OP_LABELS[op]} {len(filed)} files", count=len(filed))
        if op == J.OP_FILED:
            self._toast_filed(filed)
        if errors:
            name, reason = errors[0]
            text = f"Could not copy {name} · {reason}" if len(errors) == 1 else \
                f"Could not copy {len(errors)} files · {reason}"
            self.ctx.ui.toast("error", text)
        self._refresh_derived(state)
        return filed

    def _relink(self, state: _State, node: str, path: str, *, old: Optional[str] = None) -> bool:
        """Relink one item (module thread) with suppression; patches the snapshot and the index on success."""
        self._suppress.add(("media", node, norm(path)))
        try:
            online = bool(self.ctx.premiere.relink(node, path))
        except PremiereError as exc:
            self.log.warning("relink of %s failed: %s", node, exc.message)
            online = False
        if not online:
            self._suppress.discard(("media", node, norm(path)))
            return False
        self._index_set(state, node, old, path)
        self._patch(state, node, media_path=path, is_offline=False)
        return True

    def _to_bin(self, state: _State, node: str, bin_to: str, bin_from: str) -> str:
        """Move an item to `bin_to` unless it is there; returns the bin it ends in."""
        if norm_bin(bin_to) == norm_bin(bin_from):
            return bin_to
        self._suppress.add(("bin", node, norm_bin(bin_to)))
        try:
            self.ctx.premiere.move_to_bin([node], bin_to)
        except PremiereError as exc:
            self._suppress.discard(("bin", node, norm_bin(bin_to)))
            self.log.warning("move to bin %s failed: %s", bin_to, exc.message)
            return bin_from
        self._patch(state, node, tree_path=bin_to)
        return bin_to

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

    def _move_back(self, target: str, original: str) -> bool:
        """Rename a file the module moved back to where it was (relink failure, undo, recovery)."""
        if not os.path.isfile(target) or os.path.exists(original):
            return False
        self._suppress.add(("disk", norm(original)))
        self._suppress.add(("disk", norm(target)))
        try:
            move_file(target, original)
        except OSError as exc:
            self.log.warning("could not move %s back: %s", target, exc)
            return False
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
        shared = self._referenced(state, path, ignore=node)  # another item keeps the file: copy instead of move
        result: dict = {}
        finished = {"done": False}

        def work(job):
            target, reuse, file_hash = resolve_target(path, state.mapper.abs_folder(folder))
            file_hash = file_hash or safe_hash(path)
            copied, moved = shared and not reuse, not shared and not reuse
            entry = state.journal.append(J.OP_MOVED_BIN, node_id=node, name=after.name, **{"from": path}, to=target,
                                         bin_from=before.tree_path, bin_to=after.tree_path, hash=file_hash,
                                         copied=copied, moved=moved, size=file_size(path))
            result.update(entry=entry, target=target)
            try:
                if copied:
                    self._suppress.add(("disk", norm(target)))
                    copy_file(path, target, buffer_bytes=self._host("copy_buffer_mb") << 20,
                              cancelled=lambda: bool(getattr(job, "cancelled", False)))
                elif moved:
                    self._suppress.add(("disk", norm(target)))
                    self._suppress.add(("disk", norm(path)))
                    move_file(path, target)
            except (CopyCancelled, OSError) as exc:
                self._suppress.discard(("disk", norm(target)))
                reason = failure_reason(exc, target=target, source=path)
                state.journal.mark_failed(entry["id"], reason)
                result["failed"] = reason
            return result

        def finish(job, _value=None):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is not state or "entry" not in result:
                    self.ctx.ui.invalidate()
                    return
                if result.get("failed"):
                    self.ctx.ui.toast("error", f"Could not move {after.name} · {result['failed']}")
                    self.ctx.ui.invalidate()
                    return
                entry, target = result["entry"], result["target"]
                if not self._relink(state, node, target, old=path):
                    if entry.get("moved"):
                        self._move_back(target, path)
                    elif entry.get("copied"):
                        self._delete_own_copy(state, target, entry_hash=entry.get("hash"), check_journal=False)
                    state.journal.mark_failed(entry["id"], "relink failed")
                    self.ctx.ui.toast("error", f"Could not relink {after.name}")
                    self.ctx.ui.invalidate()
                    return
                deleted = False
                if not entry.get("moved") and not entry.get("copied"):
                    deleted = self._delete_own_copy(state, path)  # an equal file was reused: drop our old copy
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
        """docs/FILES.md 3.2: exclusions of 1.5 (with the `<stem> Masks` folder) and the exports folder."""
        if state.mapper.excluded(path):
            return True
        if state.mapper.is_exports(path) and not self._proj("import_exports"):
            return True
        return False

    def _watch_accepts(self, state: _State, path: str) -> bool:
        """Only files with an extension of the media tables are imported (docs/FILES.md 3.2)."""
        return is_media_extension(path)

    def _start_watcher(self, state: _State) -> None:
        if state.watcher is not None or not self._proj("watch") or not os.path.isdir(state.root):
            return
        watcher = DiskWatcher(state.root, lambda change: self._on_disk_change(state, change),
                              ignored=lambda path: self._watch_ignored(state, path),
                              accept_file=lambda path: self._watch_accepts(state, path),
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

    def _set_watch(self, state: _State, on: bool) -> None:
        self._set_proj("watch", on)
        if on:
            self._start_watcher(state)
        else:
            self._stop_watcher(state)

    def _on_disk_change(self, state: _State, change: dict) -> None:
        """Watcher thread: drop our own changes, then hop onto the module thread through a job callback."""
        path = change["path"]
        if change["type"] in ("created", "deleted", "moved") and self._suppress.consume(("disk", norm(path))):
            if change["type"] == "moved" and change.get("src"):
                self._suppress.consume(("disk", norm(change["src"])))
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
        if self._watch_ignored(state, path) or not self._watch_accepts(state, path):
            return  # never imported, never journaled (docs/FILES.md 3.2)
        folder = state.mapper.layout_folder_of(path)
        if folder is None:
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
        self._refresh_derived(state)

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
            if not self._relink(state, node, dest, old=src):
                state.journal.mark_failed(entry["id"], "relink failed")
                self.ctx.ui.toast("error", f"Could not relink {name}")
                continue
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

    def _cleanup_journal(self, state: _State) -> None:
        """docs/FILES.md 3.2: drop the `failed` entries of files the watcher must never have touched (excluded by
        1.5 or not a media extension: the mask files a 1.x watcher tried to import); one rewrite, or none."""
        drop = []
        for entry in state.journal.entries():
            if entry.get("status") != J.FAILED or entry.get("op") in (J.OP_ZIPPED, J.OP_SETUP):
                continue
            path = entry.get("to") or entry.get("from")
            if not path or not isinstance(path, str):
                continue
            if not is_media_extension(path) or (is_inside(path, state.root) and self._excluded(state, path)):
                drop.append(entry["id"])
        if drop:
            try:
                count = state.journal.drop(drop)
            except OSError as exc:
                self.log.warning("journal clean-up skipped: %s", exc)
                return
            self.log.info("dropped %d failed journal entries of ignored files", count)

    def _recover(self, state: _State) -> None:
        """docs/FILES.md 1.3, 5.2: resume or roll back entries an interrupted run left `pending`."""
        for entry in state.journal.pending():
            try:
                self._recover_entry(state, entry)
            except PremiereError as exc:
                self.log.warning("recovery of %s postponed: %s", entry.get("id"), exc.message)
            except OSError as exc:
                state.journal.mark_failed(entry["id"], f"recovery failed: {exc}")

    def _recover_entry(self, state: _State, entry: dict) -> None:
        op, node, target, source = entry.get("op"), entry.get("node_id"), entry.get("to"), entry.get("from")
        if op == J.OP_ZIPPED:
            if target and os.path.exists(target + ".part"):
                os.remove(target + ".part")  # our own partial archive beside the root
            state.journal.mark_failed(entry["id"], "interrupted")
            return
        if target and is_inside(target, state.root) and os.path.exists(target + ".part"):
            os.remove(target + ".part")
        item = state.items.get(node) if node else None
        if op in J.FILE_OPS:
            if item is not None and same_path(item.media_path, target):
                state.journal.mark_done(entry["id"], recovered=True, deleted_from=False)
                return
            moved = bool(entry.get("moved"))
            complete = target and os.path.isfile(target) and (not moved or not os.path.exists(source or ""))
            if item is not None and complete and same_path(item.media_path, source):
                self._suppress.add(("media", node, norm(target)))
                if self.ctx.premiere.relink(node, target):
                    self._index_set(state, node, source, target)
                    self._patch(state, node, media_path=target, is_offline=False)
                    bin_to = entry.get("bin_to") or ""
                    if op != J.OP_MOVED_BIN and norm_bin(bin_to) != norm_bin(item.tree_path):
                        self._suppress.add(("bin", node, norm_bin(bin_to)))
                        self.ctx.premiere.move_to_bin([node], bin_to)
                        self._patch(state, node, tree_path=bin_to)
                    state.journal.mark_done(entry["id"], resumed=True, deleted_from=False)
                    return
            if moved and target and source:
                self._move_back(target, source)
            elif entry.get("copied") and target:
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

    def _track(self, job, kind: str, label: str, detail: Optional[str] = None) -> None:
        with self._lock:
            self._jobs[job.id] = _Tracked(job=job, kind=kind, label=label, detail=detail)
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

    def _progress(self, job, value: Optional[float], label: str, detail: Optional[str]) -> None:
        """Job thread: the progress of a long action with its current file (docs/FILES.md 3.4 to 3.6)."""
        with self._lock:
            tracked = self._jobs.get(getattr(job, "id", ""))
            if tracked is not None:
                tracked.detail = detail
        job.progress(value, label)

    def _busy(self, *kinds: str) -> bool:
        return any(t.kind in kinds for t in self._jobs.values())

    def _long_running(self) -> Optional[str]:
        return next((t.kind for t in self._jobs.values() if t.kind in LONG_KINDS), None)

    def _run_long(self, state: _State, kind: str, title: str, work: Callable, finish: Callable) -> None:
        """Submit one of Sort / Sync / ZIP; `finish(job, value, error)` runs on the module thread."""
        finished = {"done": False}

        def done(job, value):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is not state:
                    return
                finish(job, value, None)
            self.ctx.ui.invalidate()

        def failed(job, exc):
            finished["done"] = True
            self._untrack(job)
            with self._lock:
                if self._state is not state:
                    return
                self.log.warning("%s failed: %s", title, exc)
                finish(job, None, exc)
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title=title, priority="high", on_progress=self._on_progress,
                                   on_done=done, on_error=failed)
        if not finished["done"]:
            self._track(job, kind, title)

    def _post(self, state: _State, fn: Callable[[], None]) -> None:
        """Job thread -> module thread for one finished file; dropped when the project was closed meanwhile (the
        journal entry stays pending and is recovered on the next open)."""
        def run() -> None:
            with self._lock:
                if self._state is state:
                    fn()
        self.ctx.post(run)

    # ------------------------------------------------------------------------------------------------ Sort (3.4)

    def _user_placed(self, state: _State) -> set[str]:
        """Files the user put where they are: `to` of a done, not undone `moved_bin` / `moved_disk` entry."""
        entries = state.journal.entries()
        undone = {e["undo_of"] for e in entries if e.get("undo_of")}
        return {norm(e["to"]) for e in entries
                if e.get("op") in (J.OP_MOVED_BIN, J.OP_MOVED_DISK) and e.get("status") == J.DONE
                and not e.get("undo_of") and e["id"] not in undone and e.get("to")}

    def _act_sort(self, state: _State, value, row) -> None:
        if self._long_running():
            return self._result("sort", "neutral", "Another action is running")
        if self._take_snapshot(state) is None:
            return self._result("sort", "error", "Premiere Pro did not answer", state.premiere_error or None)
        reason = write_check(state.root)
        if reason:
            return self._result("sort", "error", NO_WRITE_ACCESS, reason)
        mapper, layout = state.mapper, state.layout
        candidates = [item for item in state.items.values() if self._is_media(item) and not item.is_offline
                      and os.path.isfile(item.media_path) and is_inside(item.media_path, state.root)
                      and not self._excluded(state, item.media_path)]
        refs = {path: len(nodes) for path, nodes in state.index.items()}
        placed = self._user_placed(state)
        overrides = self._overrides(state)
        run = _Run()

        def work(job):
            planned: list[PlanEntry] = []
            for index, item in enumerate(candidates, start=1):
                if getattr(job, "cancelled", False):
                    return planned
                path = item.media_path
                self._progress(job, None, f"Checking media {index} / {len(candidates)}", item.name)
                key = norm(path)
                if key in placed:
                    continue
                target = self._decide(state, path, overrides).folder
                here = mapper.rel_folder(path) or ""
                if is_under(here, target):
                    folder = mapper.layout_folder_of(path) or target
                    if norm_bin(item.tree_path) != norm_bin(mapper.bin_for_folder(folder)):
                        planned.append(PlanEntry(node_id=item.node_id, name=item.name, source=path, folder=folder,
                                                 target=path, size=0, reuse=True, bin_from=item.tree_path,
                                                 bin_only=True))
                    continue
                deepest = mapper.deepest_layout_folder(path)
                if deepest is not None and not layout.in_media_dir(deepest) and _top(deepest) == _top(target):
                    continue  # already in the target's top-level folder (Audio/SFX vs Audio/Music)
                planned.append(PlanEntry(node_id=item.node_id, name=item.name, source=path, folder=target,
                                         target="", size=file_size(path), reuse=False, bin_from=item.tree_path,
                                         copy=refs.get(key, 0) > 1))
            run.planned = len(planned)
            for index, entry in enumerate(planned, start=1):
                if getattr(job, "cancelled", False):
                    break
                self._progress(job, (index - 1) / max(1, len(planned)), f"Sorting project {index} / {len(planned)}",
                               f"{entry.name} > {entry.folder}")
                if entry.bin_only:
                    self._post(state, lambda e=entry: self._apply_bin_only(state, e, run))
                    continue
                self._sort_one(state, job, entry, run)
            return planned

        def finish(job, planned, error):
            cancelled = bool(getattr(job, "cancelled", False))
            if error is not None:
                return self._result("sort", "error", "Sort failed", str(error))
            n, k = run.planned, run.done
            if cancelled:
                return self._result("sort", "neutral", f"Sort stopped · {k} of {n} filed")
            if not n:
                return self._result("sort", "neutral", "Nothing to sort")
            if run.failures:
                name, why = run.failures[0]
                return self._result("sort", "warn", f"Sorted {k} of {n} files · {len(run.failures)} failed",
                                    f"{name}: {why}")
            self._emit(J.OP_SORTED, f"Sorted {plural(k, 'file')}", count=k)
            self._result("sort", "ok", f"Sorted {plural(k, 'file')} · {fmt_size(run.bytes)}")

        self._run_long(state, "sort", "Sorting project", work, finish)

    def _sort_one(self, state: _State, job, entry: PlanEntry, run: _Run) -> None:
        """Job thread: journal `sorted` pending, move (or copy a shared file), then post the relink."""
        source = entry.source
        if not os.path.isfile(source):
            self._post(state, lambda: run.failures.append((entry.name, "source not found")))
            return
        target, reuse, file_hash = resolve_target(source, state.mapper.abs_folder(entry.folder))
        file_hash = file_hash or safe_hash(source)
        copied, moved = entry.copy and not reuse, not entry.copy and not reuse
        record = state.journal.append(J.OP_SORTED, node_id=entry.node_id, name=entry.name, **{"from": source},
                                      to=target, bin_from=entry.bin_from,
                                      bin_to=state.mapper.bin_for_folder(entry.folder), hash=file_hash,
                                      size=entry.size, copied=copied, moved=moved)
        try:
            if copied:
                self._suppress.add(("disk", norm(target)))
                copy_file(source, target, buffer_bytes=self._host("copy_buffer_mb") << 20,
                          cancelled=lambda: bool(getattr(job, "cancelled", False)))
            elif moved:
                self._suppress.add(("disk", norm(target)))
                self._suppress.add(("disk", norm(source)))
                move_file(source, target)
        except (CopyCancelled, OSError) as exc:
            self._suppress.discard(("disk", norm(target)))
            reason = failure_reason(exc, target=target, source=source)
            state.journal.mark_failed(record["id"], reason)
            if not isinstance(exc, CopyCancelled):
                self.log.warning("sort of %s failed (%s): %s", source, reason, exc)
                self._post(state, lambda: run.failures.append((entry.name, reason)))
            return
        done_entry = replace(entry, size=0 if reuse else entry.size)
        self._post(state, lambda: self._apply_moved(state, record, done_entry, target, run))

    def _apply_moved(self, state: _State, record: dict, entry: PlanEntry, target: str, run: _Run) -> None:
        """Module thread: relink to the moved / copied / reused file, bin move, journal done; a failed relink moves
        the file back (or deletes the copy) and journals `failed`."""
        node, source = entry.node_id, entry.source
        if not self._relink(state, node, target, old=source):
            if record.get("moved"):
                self._move_back(target, source)
            elif record.get("copied"):
                self._delete_own_copy(state, target, entry_hash=record.get("hash"), check_journal=False)
            state.journal.mark_failed(record["id"], "relink failed")
            run.failures.append((entry.name, "relink failed"))
            return
        bin_to = self._to_bin(state, node, record.get("bin_to") or "", entry.bin_from)
        state.journal.mark_done(record["id"], bin_to=bin_to)
        run.done += 1
        run.bytes += entry.size
        self._refresh_derived(state)

    def _apply_bin_only(self, state: _State, entry: PlanEntry, run: _Run) -> None:
        bin_to = state.mapper.bin_for_folder(entry.folder)
        if self._bin_move(state, entry.node_id, entry.name, entry.source, entry.bin_from, bin_to):
            run.done += 1
        else:
            run.failures.append((entry.name, "bin move failed"))

    # ------------------------------------------------------------------------------------------------ Sync (3.5)

    def _act_sync_all(self, state: _State, value, row) -> None:
        self._start_sync(state, used_only=False)

    def _act_sync_used(self, state: _State, value, row) -> None:
        self._start_sync(state, used_only=True)

    def _start_sync(self, state: _State, *, used_only: bool) -> None:
        button = "sync_used" if used_only else "sync_all"
        if self._long_running():
            return self._result(button, "neutral", "Another action is running")
        if self._take_snapshot(state) is None:
            return self._result(button, "error", "Premiere Pro did not answer", state.premiere_error or None)
        items = list(state.outside)
        offline = [item for item in state.offline if not is_inside(item.media_path or "", state.root)]
        if used_only:
            try:
                used = self._used_ids()
            except PremiereError as exc:
                return self._result(button, "error", "Premiere Pro did not answer", exc.message)
            items = [item for item in items if item.node_id in used]
            offline = [item for item in offline if item.node_id in used]
            state.used_count, state.used_at = len(items), time.monotonic()
        if not items and not offline:
            return self._result(button, "neutral", "No used media outside the project folder" if used_only
                                else "Nothing outside the project folder")
        reason = write_check(state.root)
        if reason:
            return self._result(button, "error", NO_WRITE_ACCESS, reason)
        media_dir = state.mapper.abs_folder(state.layout.media_dir)
        media_bin = state.mapper.bin_for_folder(state.layout.media_dir)
        run = _Run(offline=[item.name for item in offline])

        def work(job):
            planned: list[PlanEntry] = []
            reserved: dict = {}
            for item in items:
                target, reuse, file_hash = resolve_target(item.media_path, media_dir, reserved=reserved)
                file_hash = file_hash or safe_hash(item.media_path)
                reserved[norm(target)] = file_hash
                planned.append(PlanEntry(node_id=item.node_id, name=item.name, source=item.media_path,
                                         folder=state.layout.media_dir, target=target,
                                         size=file_size(item.media_path), reuse=reuse, bin_from=item.tree_path,
                                         hash=file_hash))
            needed = sum(entry.size for entry in planned if not entry.reuse)
            free = free_bytes(state.root)
            if free is not None and needed > free:
                run.error = ("Not enough free space · needs " + fmt_size(needed), None)
                return planned
            run.planned = len(planned)
            for index, entry in enumerate(planned, start=1):
                if getattr(job, "cancelled", False):
                    break
                self._progress(job, (index - 1) / max(1, len(planned)), f"Syncing media {index} / {len(planned)}",
                               entry.name)
                self._sync_one(state, job, entry, media_dir, media_bin, run)
            return planned

        def finish(job, planned, error):
            if error is not None:
                return self._result(button, "error", "Sync failed", str(error))
            if run.error is not None:
                return self._result(button, "error", *run.error)
            n, k = run.planned, run.done
            if getattr(job, "cancelled", False):
                return self._result(button, "neutral", f"Sync stopped · {k} of {n} copied")
            if run.failures or run.offline:
                parts = [f"Synced {k} of {n} files"] if n else ["Nothing to copy"]
                if run.failures:
                    parts.append(f"{len(run.failures)} failed")
                if run.offline:
                    parts.append(f"{len(run.offline)} offline")
                if run.failures:
                    name, why = run.failures[0]
                    detail = f"{name}: {why}"
                else:
                    detail = "Offline: " + ", ".join(run.offline[:5]) + (" and more" if len(run.offline) > 5 else "")
                return self._result(button, "warn", " · ".join(parts), detail)
            self._emit(J.OP_SYNCED, f"Synced {plural(k, 'file')} into {state.layout.media_dir}", count=k)
            self._result(button, "ok", f"Synced {plural(k, 'file')} into {state.layout.media_dir} · "
                                       f"{fmt_size(run.bytes)}")

        self._run_long(state, button, "Syncing media", work, finish)

    def _sync_one(self, state: _State, job, entry: PlanEntry, media_dir: str, media_bin: str, run: _Run) -> None:
        source = entry.source
        if not os.path.isfile(source):
            self._post(state, lambda: run.failures.append((entry.name, "source not found")))
            return
        target, reuse, file_hash = resolve_target(source, media_dir)
        file_hash = file_hash or entry.hash or safe_hash(source)
        record = state.journal.append(J.OP_SYNCED, node_id=entry.node_id, name=entry.name, **{"from": source},
                                      to=target, bin_from=entry.bin_from, bin_to=media_bin, hash=file_hash,
                                      size=entry.size, copied=not reuse)
        if not reuse:
            self._suppress.add(("disk", norm(target)))
            try:
                copy_file(source, target, buffer_bytes=self._host("copy_buffer_mb") << 20,
                          cancelled=lambda: bool(getattr(job, "cancelled", False)))
            except (CopyCancelled, OSError) as exc:
                self._suppress.discard(("disk", norm(target)))
                reason = failure_reason(exc, target=target, source=source)
                state.journal.mark_failed(record["id"], reason)
                if not isinstance(exc, CopyCancelled):
                    self.log.warning("sync of %s failed (%s): %s", source, reason, exc)
                    self._post(state, lambda: run.failures.append((entry.name, reason)))
                return
        moved_entry = replace(entry, size=0 if reuse else entry.size)
        self._post(state, lambda: self._apply_moved(state, record, moved_entry, target, run))

    # ------------------------------------------------------------------------------------------------ ZIP (3.6)

    def _act_zip(self, state: _State, value, row) -> None:
        if self._long_running():
            return self._result("zip", "neutral", "Another action is running")
        if self._take_snapshot(state) is None:
            return self._result("zip", "error", "Premiere Pro did not answer", state.premiere_error or None)
        if state.outside:
            return self._result("zip", "warn", f"Sync all media first · {plural(len(state.outside), 'file')} "
                                               "outside the folder")
        reason = write_check(state.root)
        if reason:
            return self._result("zip", "error", NO_WRITE_ACCESS, reason)
        items = [item for item in state.items.values() if self._is_media(item)]
        project_name = os.path.basename(state.project.path)
        progress = {"k": 0, "n": 0}
        outcome: dict = {}

        def work(job):
            report = build_zip_report(project_name, state.root, items, lambda p: is_inside(p, state.root))
            report_path = os.path.join(state.root, DECK_DIR, "pack-report.txt")
            report.archive = free_zip_path(state.root)
            entry = state.journal.append(J.OP_ZIPPED, name=os.path.basename(report.archive), to=report.archive,
                                         offline=[o["name"] for o in report.offline], copied=False)
            outcome["entry"] = entry
            _write_text(report_path, report.text())
            files = pack_files(state.root)

            def on_file(k: int, n: int, name: str) -> None:
                progress.update(k=k, n=n)
                self._progress(job, (k - 1) / max(1, n), f"Making ZIP {k} / {n}", name)

            try:
                make_zip(state.root, report.archive, files=files, on_file=on_file,
                         buffer_bytes=self._host("copy_buffer_mb") << 20,
                         cancelled=lambda: bool(getattr(job, "cancelled", False)))
            except (CopyCancelled, OSError) as exc:
                archive, report.archive = report.archive, None
                _write_text(report_path, report.text())
                why = failure_reason(exc, target=archive)
                state.journal.mark_failed(entry["id"], why)
                outcome["error"] = why
                return report
            report.files = len(files)
            report.archive_bytes = file_size(report.archive)
            state.journal.mark_done(entry["id"], size=report.archive_bytes)
            return report

        def finish(job, report, error):
            if error is not None:
                return self._result("zip", "error", "Could not write the ZIP", str(error))
            if outcome.get("error") == "cancelled" or (getattr(job, "cancelled", False) and "error" in outcome):
                return self._result("zip", "neutral", f"ZIP stopped · {progress['k']} of {progress['n']} files")
            if outcome.get("error"):
                return self._result("zip", "error", f"Could not write the ZIP · {outcome['error']}")
            state.zip_report = report
            self._emit(J.OP_ZIPPED, report.line(), tone="ok" if report.ok else "warn")
            self._result("zip", "ok" if report.ok else "warn", report.line(), report.archive)

        self._run_long(state, "zip", "Making ZIP", work, finish)

    def _act_stop(self, state: _State, value, row) -> None:
        jobs = list(self._jobs.values())
        if not jobs:
            return self._result("stop", "neutral", "Nothing to stop")
        for tracked in jobs:
            tracked.job.cancel()
        return None  # each stopped job reports "<Sort | Sync | ZIP> stopped · ..." itself

    # ------------------------------------------------------------------------------------------------ undo (3.7)

    def _undo(self, state: _State, entry_id: Optional[str], button: str) -> None:
        candidates = state.journal.undoable(self.UNDO_LIMIT)
        if not candidates and not entry_id:
            return self._result(button, "neutral", "Nothing to undo")
        entry = candidates[0] if not entry_id else next((e for e in candidates if e["id"] == entry_id), None)
        if entry is None:
            return self._result(button, "warn", "Cannot undo · this change can no longer be undone")
        try:
            message = self._reverse(state, entry)
        except PremiereError as exc:
            self.log.warning("undo failed: %s", exc.message)
            return self._result(button, "error", "Undo failed in Premiere Pro", exc.message)
        except OSError as exc:
            self.log.warning("undo failed: %s", exc)
            return self._result(button, "error", "Undo failed", failure_reason(exc))
        if message:
            return self._result(button, "warn", f"Cannot undo · {message}")
        self._emit("undo", f"Undone: {self._describe(entry).lower()}")
        self._refresh_derived(state)
        self._result(button, "ok", f"Undone · {OP_LABELS.get(entry['op'], entry['op']).lower()} "
                                   f"{entry.get('name', '')}".rstrip())

    def _reverse(self, state: _State, entry: dict) -> Optional[str]:
        """Reverses one entry (docs/FILES.md 3.7). Returns a reason when it cannot be undone, else None."""
        op, node = entry["op"], entry.get("node_id")
        original, current = entry.get("from"), entry.get("to")
        bin_from, bin_to = entry.get("bin_from"), entry.get("bin_to")
        pr = self.ctx.premiere
        extra: dict = {}
        if op != J.OP_IMPORTED and (not node or pr.item(node) is None):
            return "the item is no longer in the project"

        def relink_to(path: str) -> bool:
            return self._relink(state, node, path, old=current)

        def move_back() -> None:
            if bin_from is not None and norm_bin(bin_from) != norm_bin(bin_to):
                self._suppress.add(("bin", node, norm_bin(bin_from)))
                pr.move_to_bin([node], bin_from)
                self._patch(state, node, tree_path=bin_from)

        def undo_copy() -> Optional[str]:
            if not original or not os.path.isfile(original):
                return f"the original file is no longer at {original}"
            if not relink_to(original):
                return f"could not relink {entry.get('name', '')}"
            move_back()
            extra["deleted"] = bool(entry.get("copied")) and self._delete_own_copy(state, current)
            return None

        def undo_move() -> Optional[str]:
            if not current or not os.path.isfile(current):
                return f"the file is no longer at {current}"
            if not original or os.path.exists(original):
                return f"{original} is taken by another file"
            if not self._move_back(current, original):
                return f"could not move {entry.get('name', '')} back"
            if not relink_to(original):
                self._suppress.add(("disk", norm(current)))
                move_file(original, current)  # keep the item online at the moved file
                return f"could not relink {entry.get('name', '')}"
            move_back()
            extra["moved_back"] = True
            return None

        if op in (J.OP_FILED, J.OP_COLLECTED, J.OP_SYNCED):
            message = undo_copy()
        elif op == J.OP_SORTED:
            message = undo_move() if entry.get("moved") else undo_copy()
        elif op == J.OP_BINNED:
            move_back()
            message = None
        elif op == J.OP_IMPORTED:
            ids = entry.get("node_ids") or ([node] if node else [])
            if not ids:
                return "the imported item is not known"
            pr.remove_items(ids)
            for item_id in ids:
                self._index_set(state, item_id, current, None)
                state.items.pop(item_id, None)
            message = None
        elif op == J.OP_RENAMED:
            relinked = bool(original and os.path.isfile(original) and relink_to(original))
            renamed = False
            if entry.get("name_to") and entry.get("name_from"):
                pr.rename_item(node, entry["name_from"])
                self._patch(state, node, name=entry["name_from"])
                renamed = True
            message = None if (relinked or renamed) else "the old file name no longer exists on disk"
        elif op == J.OP_MOVED_DISK:
            message = None
            if original and os.path.isfile(original) and not relink_to(original):
                message = f"could not relink {entry.get('name', '')}"
            if message is None:
                move_back()
        elif op == J.OP_MOVED_BIN:
            if entry.get("moved"):
                message = undo_move()
            else:
                message = self._undo_moved_bin_copy(state, entry, relink_to, move_back, extra)
        else:
            return "this change cannot be undone"
        if message:
            return message
        state.journal.append(op, status=J.UNDONE, undo_of=entry["id"], node_id=node, name=entry.get("name"),
                             **{"from": current}, to=original, bin_from=bin_to, bin_to=bin_from,
                             hash=entry.get("hash"), copied=False, **extra)
        return None

    def _undo_moved_bin_copy(self, state: _State, entry: dict, relink_to, move_back, extra: dict) -> Optional[str]:
        """A `moved_bin` entry that copied (a shared file, or any 1.x entry): relink to the previous file,
        recreating a copy the 1.x module deleted, then drop the copy when nothing else uses it."""
        original, current = entry.get("from"), entry.get("to")
        recreated = False
        if not original or not os.path.isfile(original):
            if not entry.get("deleted_from") or not current or not os.path.isfile(current):
                return f"the previous file is no longer at {original}"
            self._suppress.add(("disk", norm(original)))
            copy_file(current, original, buffer_bytes=self._host("copy_buffer_mb") << 20)
            recreated = True
        if not relink_to(original):
            return f"could not relink {entry.get('name', '')}"
        move_back()
        extra["recreated"] = recreated
        extra["deleted"] = bool(entry.get("copied")) and self._delete_own_copy(state, current)
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
        if local in REMOVED_ACTIONS:
            self.log.info("ignored the removed action %s", action_id)
            return ActionResult(rerender=False)
        handler = getattr(self, f"_act_{local}", None)
        if handler is None:
            return super().on_action(action_id, value, row)
        with self._lock:
            if self._state is None:
                self._result(local, "neutral", "Open a project in Premiere Pro first")
                return ActionResult(rerender=False)
            state = self._state
            if local not in NO_SETUP_NEEDED and not self._enabled():
                self._result(local, "warn", "Set up this project first")
                return None
            return handler(state, value, row)

    def _act_setup(self, state: _State, value, row) -> None:
        if self._enabled():
            return self._result("setup", "neutral", "This project is already set up")
        reason = write_check(state.root)
        if reason:
            return self._result("setup", "error", NO_WRITE_ACCESS, reason)
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
        except Exception:  # noqa: BLE001 - a malformed answer; bins are also created when a file first needs one
            self.log.exception("bins not created")
        state.journal.append(J.OP_SETUP, status=J.DONE, name=os.path.basename(state.root), to=state.root,
                             folders_created=created, bins=bins, layout=state.layout.id)
        state.write_ok = True
        self._start_watcher(state)
        self._refresh_derived(state)
        self._refresh_used(state, force=True)
        self._result("setup", "ok", f"Project set up · {len(state.layout.folders)} folders")

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

    def _act_open_folder(self, state: _State, value, row) -> None:
        self.ctx.host.open_path(state.root)
        self._result("open_folder", "ok", f"Opened {os.path.basename(state.root)}")

    def _act_journal(self, state: _State, value, row) -> None:
        show = getattr(self.ctx.host, "show", None)
        if callable(show):
            show(self.id)
        else:
            self.log.info("the host has no window to show the journal in")
        self._result("journal", "ok", "Journal opened in the host window")

    def _act_undo_last(self, state: _State, value, row) -> None:
        self._undo(state, None, "undo_last")

    def _act_undo(self, state: _State, value, row) -> None:
        self._undo(state, row or (value if isinstance(value, str) else None), "undo")

    def _act_locate(self, state: _State, value, row) -> Optional[ActionResult]:
        item = state.items.get(row or "")
        if item is None or not item.media_path:
            return self._result("locate", "warn", "No path is known for this item")
        path = item.media_path
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                path = ""
                break
            path = parent
        if not path:
            return self._result("locate", "warn", "The folder of this item no longer exists")
        self.ctx.host.reveal(path)
        self._result("locate", "ok", f"Showing {os.path.basename(path.rstrip(os.sep)) or path}")
        return ActionResult(rerender=False)

    def _act_remove(self, state: _State, value, row) -> None:
        item = state.items.get(row or "")
        if item is None:
            return self._result("remove", "warn", "The item is no longer in the project")
        entry = state.journal.append(J.OP_REMOVED, node_id=item.node_id, name=item.name,
                                     **{"from": item.media_path}, bin_from=item.tree_path, copied=False)
        try:
            self.ctx.premiere.remove_items([item.node_id])
        except PremiereError as exc:
            state.journal.mark_failed(entry["id"], exc.message)
            return self._result("remove", "error", f"Could not remove {item.name}", exc.message)
        state.journal.mark_done(entry["id"])
        state.items.pop(item.node_id, None)
        self._index_set(state, item.node_id, item.media_path, None)
        self._refresh_derived(state)
        self._result("remove", "ok", f"Removed {item.name} from the project")

    # host window controls ("files.host:<name>") --------------------------------------------------------------

    def on_setting_changed(self, key: str, scope: str, value: Any) -> None:
        """The host window's Settings card stored a value (docs/MODULES.md 5): `watch` starts or stops the watcher,
        the other keys are read live."""
        if scope == "project" and key == "watch" and isinstance(value, bool):
            with self._lock:
                state = self._state
                if state is not None:
                    self._set_watch(state, value)

    def _host_project_setting(self, state: _State, value, row) -> Optional[ActionResult]:
        changes = _pairs(value)
        applied = []
        for key, new in changes.items():
            if key in PROJECT_BOOLS and isinstance(new, bool):
                if key == "watch":
                    self._set_watch(state, new)
                else:
                    self._set_proj(key, new)
                applied.append(key)
        if not applied:
            return ActionResult(toast=("warn", "Unknown project setting"), rerender=False)
        return None

    def _host_layout_select(self, state: _State, value, row) -> Optional[ActionResult]:
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
                 for o in parse_overrides(raw, state.layout.media_dir)]
        self._set_proj("overrides", clean)
        dropped = len(raw) - len(clean)
        text = "Overrides saved" + (f" · {dropped} invalid skipped" if dropped else "")
        return ActionResult(toast=("ok" if not dropped else "warn", text))

    def _host_journal_op(self, state: _State, value, row) -> Optional[ActionResult]:
        return self._host_journal_filter(state, {"op": value}, row)

    def _host_journal_query(self, state: _State, value, row) -> Optional[ActionResult]:
        return self._host_journal_filter(state, {"query": value}, row)

    def _host_journal_all(self, state: _State, value, row) -> None:
        self._journal_all = bool(value) if isinstance(value, bool) else not self._journal_all
        return None

    def _host_override_add(self, state: _State, value, row) -> ActionResult:
        pairs = _pairs(value)
        match = str(pairs.get("match") or "").strip()
        if not match:
            return ActionResult(toast=("warn", "Enter a source folder or a name pattern"), rerender=False)
        is_folder = any(ch in match for ch in "\\/:") and not any(ch in match for ch in "*?")
        entry = {"source" if is_folder else "name": match, "to": pairs.get("to")}
        if not parse_overrides([entry], state.layout.media_dir):
            return ActionResult(toast=("warn", "That override is not valid"), rerender=False)
        current = [o for o in self._proj("overrides") if isinstance(o, dict)]
        return self._host_overrides_save(state, current + [entry], row)

    def _host_override_remove(self, state: _State, value, row) -> ActionResult:
        current = [{k: v for k, v in (("source", o.source), ("name", o.name), ("to", o.to)) if v}
                   for o in parse_overrides(self._proj("overrides"), state.layout.media_dir)]
        try:
            index = int(row if row is not None else value)
            current.pop(index)
        except (TypeError, ValueError, IndexError):
            return ActionResult(toast=("warn", "Unknown override"), rerender=False)
        self._set_proj("overrides", current)
        return ActionResult(toast=("ok", "Override removed"))

    def _host_journal_filter(self, state: _State, value, row) -> None:
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
        state.mapper = Mapper(state.root, layout, state.masks)
        self._refresh_derived(state)

    # ------------------------------------------------------------------------------------------------ rendering

    def _node(self, node: dict, **extra) -> dict:
        """A node with the 2.0 fields of docs/PROTOCOL.md 6.1 (only the given, non-None ones)."""
        short = extra.get("short")
        if short is not None and len(short) > 16:
            raise ValueError(f"short label {short!r} is longer than 16 characters")
        node.update({key: value for key, value in extra.items() if value is not None})
        return node

    def _group(self, local_id: str, title: Optional[str], items: list, *, columns: int = 1, tiles: bool = False,
               collapsible: bool = False) -> dict:
        return self._node(self.group(title, items, columns=columns), id=self._id(local_id), tiles=tiles,
                          collapsible=collapsible)

    def _chip(self, text: str, tone: str) -> dict:
        return self._node(self.text(text, tone=tone, small=True), chip=True)

    def _chip_values(self, state: _State) -> list[tuple[str, str, str]]:
        outside, offline = len(state.outside), len(state.offline)
        return [("inside", f"{state.inside_count} in the root", "neutral"),
                ("outside", f"{outside} outside", "warn" if outside else "neutral"),
                ("offline", f"{offline} offline", "error" if offline else "neutral")]

    def _tiles(self, state: _State) -> list:
        running = self._long_running()
        estimate = state.sort_estimate
        outside = len(state.outside)
        sort_meta = f"{plural(estimate, 'item')} to file" if estimate else "Nothing to file"
        sort_confirm = (f"Move {plural(estimate, 'file')} into the layout folders? Every move is journaled and can "
                        "be undone." if estimate else
                        "Sort the media of the project folder into the layout folders? Every move is journaled and "
                        "can be undone.")
        used_meta = "Used in sequences" if state.used_count is None else f"{state.used_count} in sequences"

        def tile(local: str, label: str, short: str, icon: str, meta: str, confirm: Optional[str]) -> dict:
            return self._node(self.button(local, label, busy=running == local,
                                          disabled=running is not None and running != local,
                                          confirm=confirm, hint=HINTS[local]),
                              icon=icon, short=short, meta=meta)

        return [
            tile("sort", "Sort project", "Sort project", "sort", sort_meta, sort_confirm),
            tile("sync_all", "Sync all media", "Sync all media", "sync-all", f"{outside} outside the root",
                 f"Copy {plural(outside, 'file')} from outside the project folder into Media?" if outside else None),
            tile("sync_used", "Sync used media", "Sync used media", "sync-used", used_meta,
                 "Copy the files used in sequences from outside the project folder into Media?"),
            tile("zip", "Make ZIP", "Make ZIP", "zip", "Checks offline first", None),
        ]

    def _open_folder_button(self) -> dict:
        return self._node(self.button("open_folder", "Open project folder", hint="Opens the project folder in Explorer."),
                          variant="icon", icon="open-folder")

    def section_status(self) -> Optional[dict]:
        state = self._state
        if state is None:
            return self.status("No project", "neutral")
        if not self._enabled():
            return self.status("Set up this project", "warn")
        busy = next(iter(self._jobs.values()), None)
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
                    nodes.append(self.text(f"{plural(len(state.external), 'item')} outside the project folder.",
                                           small=True))
                nodes.extend(self._offline_nodes(state))
                return nodes
            nodes.append(self.row([self.text(state.project.name or os.path.basename(state.project.path)),
                                   self._open_folder_button()]))
            nodes.append(self.text(state.root, small=True))
            nodes.append(self.row([self._chip(text, tone) for _, text, tone in self._chip_values(state)]))
            nodes.append(self._group("actions", None, self._tiles(state), columns=2, tiles=True))
            nodes.extend(self._job_nodes())
            entries = state.journal.entries()
            undoable = state.journal.undoable(self.UNDO_LIMIT)
            last = undoable[0] if undoable else None
            nodes.append(self.row([
                self._node(self.button("journal", f"Journal · {_entries(len(entries))}",
                                       hint="Opens the journal in the host window."), icon="journal"),
                self._node(self.button("undo_last", "Undo last", disabled=last is None,
                                       hint=self._describe(last) if last else "Nothing to undo."),
                           variant="icon", icon="undo"),
            ]))
            nodes.extend(self._offline_nodes(state))
            return nodes

    def _job_nodes(self) -> list:
        return [self._node(self.progress(t.value, t.label), detail=t.detail, cancel=self._id("stop"))
                for t in list(self._jobs.values())[:3]]

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
        return [self._group("offline", "Offline", [self.list_("offline_list", rows, empty="")], collapsible=True)]

    def _describe(self, entry: dict) -> str:
        return f"{OP_LABELS.get(entry.get('op'), entry.get('op'))} {entry.get('name', '')}".strip()

    # host window page (docs/FILES.md 7; block shapes of docs/BRIDGE_API.md 5) ------------------------------------

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
            chips = {"type": "chips", "id": "status_chips",
                     "items": [{"id": cid, "text": text, "tone": tone, "icon": None}
                               for cid, text, tone in self._chip_values(state)]}
            page = {"title": self.title, "status": status, "subtitle": state.root,
                    "header_actions": [self._open_folder_button()]}
            if not enabled:
                blocks: list = [
                    {"type": "actions", "id": "setup_actions", "items": [
                        self.button("setup", "Set up project", tone="accent",
                                    hint="Creates the layout folders and the matching bins.")]},
                    chips,
                ]
                if state.layout_error:
                    blocks.append({"type": "text", "id": "layout_error", "text": state.layout_error, "tone": "warn",
                                   "small": True})
                blocks.extend(self._layout_blocks(state.layout, state=state))
                page["blocks"] = blocks
                return page
            blocks = [chips, {"type": "actions", "id": "main", "tiles": True, "columns": 2,
                              "items": self._tiles(state)}]
            blocks.extend(self._node({"type": "progress", "id": f"job_{t.job.id}", "value": t.value,
                                      "label": t.label}, detail=t.detail, cancel=self._id("stop"))
                          for t in list(self._jobs.values())[:3])
            undoable = state.journal.undoable(self.UNDO_LIMIT)
            if state.last_result is not None:
                last = state.last_result
                blocks.append({"type": "list", "id": "last_result", "rows": [{
                    "id": "last", "title": last["text"], "subtitle": last.get("detail") or "",
                    "meta": ago(last["at"]), "tone": last["outcome"], "thumb": None,
                    "actions": [self.button("undo_last", "Undo last",
                                            hint=self._describe(undoable[0]))] if undoable else []}],
                    "empty": ""})
            blocks.extend(self._journal_blocks(state, state.journal.entries(), undoable))
            if state.layout_error:
                blocks.append({"type": "text", "id": "layout_error", "text": state.layout_error, "tone": "warn",
                               "small": True})
            blocks.extend(self._layout_blocks(state.layout, state=state))
            page["blocks"] = blocks
            return page

    def _journal_blocks(self, state: _State, entries: list[dict], undoable: list[dict]) -> list:
        undo_ids = {e["id"] for e in undoable}
        flt = self._journal_filter
        if self._journal_all:
            shown = J.filter_entries(reversed(entries), flt["op"], flt["query"])[:self.HOST_JOURNAL_COUNT]
        else:
            shown = list(reversed(entries))[:self.JOURNAL_PREVIEW]
        rows = []
        for entry in shown:
            status = entry.get("status")
            tone = {"failed": "error", "pending": "accent", "undone": "neutral"}.get(status, "ok")
            text = status if status != J.FAILED or not entry.get("error") else f"failed · {entry['error']}"
            rows.append({"id": entry["id"], "tone": tone,
                         "at": ago(entry.get("at")), "op": OP_LABELS.get(entry.get("op"), entry.get("op")),
                         "name": entry.get("name") or "",
                         "from": entry.get("from") or entry.get("bin_from") or "",
                         "to": entry.get("to") or entry.get("bin_to") or "",
                         "status": {"text": text, "tone": tone},
                         "actions": [self.button("undo", "Undo")] if entry["id"] in undo_ids else []})
        table = {"type": "table", "id": "journal", "title": "Journal",
                 "columns": [{"key": "at", "label": "When"}, {"key": "op", "label": "Change"},
                             {"key": "name", "label": "Name"}, {"key": "from", "label": "From", "mono": True},
                             {"key": "to", "label": "To", "mono": True}, {"key": "status", "label": "Status"}],
                 "rows": rows, "empty": "No changes yet."}
        if self._journal_all:
            op_options = [{"value": "", "label": "All changes"}] + \
                [{"value": op, "label": label} for op, label in OP_LABELS.items()]
            table["filters"] = [self.select("host:journal_op", "Change", flt["op"], op_options)]
            table["search"] = self.input("host:journal_query", "Search", flt["query"], placeholder="File name")
        toggle = {"type": "actions", "id": "journal_actions", "items": [
            self.button("host:journal_all", "Show fewer" if self._journal_all else "Show all",
                        hint=f"{_entries(len(entries))} in the journal.")]}
        return [table, toggle]

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
        roles = {layout.project_dir.casefold(): "Project file", layout.media_dir.casefold(): "Sync target",
                 layout.exports_dir.casefold(): "Renders"}
        blocks = [
            {"type": "form", "id": "layout", "title": "Layout", "action": self._id("host:layout_select"),
             "hint": "Rules and folders apply to this project; saving makes the layout custom.",
             "fields": fields},
            {"type": "list", "id": "layout_folders", "title": "Folders",
             "rows": [{"id": f, "title": f, "subtitle": roles.get(f.casefold(), ""),
                       "meta": "", "tone": "neutral", "thumb": None, "actions": []} for f in layout.folders],
             "empty": ""},
            {"type": "table", "id": "layout_rules", "title": "Rules", "hint": "First match wins.",
             "columns": [{"key": "order", "label": "#", "align": "right"}, {"key": "match", "label": "Match"},
                         {"key": "to", "label": "Folder"}],
             "rows": rules, "empty": ""},
        ]
        if editable:
            overrides = parse_overrides(self._proj("overrides"), layout.media_dir)
            folder_options = [{"value": f, "label": f} for f in layout.all_folders()
                              if not is_under(f, layout.media_dir)]
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
        elif key in ("source", "audio_class"):
            values = value if isinstance(value, list) else [value]
            parts.append(("source " if key == "source" else "sounds like ") + " or ".join(values))
        elif isinstance(value, bool):
            parts.append(key.replace("_", " ") if value else "no " + key.replace("_", " ").replace("has ", ""))
        else:
            parts.append(str(value))
    return " · ".join(parts)


def _entries(count: int) -> str:
    return f"{count} {'entry' if count == 1 else 'entries'}"


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
