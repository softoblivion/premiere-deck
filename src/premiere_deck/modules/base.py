"""Module contract (docs/MODULES.md): the base class every module subclasses, the services a module receives, and
the test doubles (`FakeHost`) module tests build their context from.

The host implements the service protocols in host/hub.py and the real `PremiereApi`; modules
only ever see the protocols below.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from premiere_deck.core.media import MediaInfo, kind_from_extension
from premiere_deck.host import protocol as P
from premiere_deck.premiere.model import (
    PremiereLike, PremiereUnavailable, ProjectInfo, ProjectItem, Selection, SequenceInfo, Snapshot, TrackItem,
)

Tone = str  # one of protocol.TONES


# --- results and settings ----------------------------------------------------------------------------------------


@dataclass
class ActionResult:
    toast: Optional[tuple[Tone, str]] = None
    rerender: bool = True


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    kind: str  # bool | int | text | select | path
    default: Any
    options: Optional[list] = None  # [{"value", "label"}] for select
    hint: Optional[str] = None
    scope: str = "host"  # host | project


# --- service protocols --------------------------------------------------------------------------------------------


class Job(Protocol):
    id: str
    title: str
    cancelled: bool

    def progress(self, value: Optional[float], label: str = "") -> None: ...
    def cancel(self) -> None: ...


class JobRunner(Protocol):
    def submit(self, fn: Callable[[Job], Any], *, title: str, priority: str = "normal",
               on_progress: Optional[Callable[[Job, Optional[float], str], None]] = None,
               on_done: Optional[Callable[[Job, Any], None]] = None,
               on_error: Optional[Callable[[Job, BaseException], None]] = None) -> Job: ...
    def running(self) -> list[Job]: ...
    def high_priority_running(self) -> bool: ...


class ScopedSettings(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def all(self) -> dict: ...


class ModuleSettings(ScopedSettings, Protocol):
    project: ScopedSettings


class Tools(Protocol):
    def ffprobe(self, path: str) -> MediaInfo: ...
    def ffmpeg(self, args: Sequence[str], *, timeout: Optional[float] = None, on_line: Optional[Callable[[str], None]] = None) -> int: ...
    def ffmpeg_pipe(self, args: Sequence[str]) -> subprocess.Popen: ...
    def has(self, tool: str) -> bool: ...
    def ensure(self, tool: str, on_progress: Optional[Callable[[float, str], None]] = None) -> str: ...
    def path_of(self, tool: str) -> Optional[str]: ...


class UiPort(Protocol):
    def invalidate(self) -> None: ...
    def toast(self, tone: Tone, text: str) -> None: ...
    def result(self, button_id: str, outcome: str, text: str, detail: Optional[str] = None) -> None: ...
    def confirm(self, question: str) -> bool: ...


class HostInfo(Protocol):
    version: str
    data_dir: str

    def open_path(self, path: str) -> None: ...
    def reveal(self, path: str) -> None: ...
    def open_url(self, url: str) -> None: ...
    def show(self, page: Optional[str] = None) -> None: ...


class EventBus(Protocol):
    def emit(self, name: str, data: Any = None) -> None: ...


class PathsLike(Protocol):
    root: Path
    logs_dir: Path

    def cache_dir(self, module_id: str) -> Path: ...


@dataclass
class ModuleContext:
    premiere: PremiereLike
    jobs: JobRunner
    settings: ModuleSettings
    paths: PathsLike
    tools: Tools
    ui: UiPort
    host: HostInfo
    events: EventBus
    log: logging.Logger
    project: Optional[ProjectInfo] = None
    # Runs `fn` on the module thread (from a job or watcher thread); the default runs it inline (tests).
    post: Callable[[Callable[[], None]], None] = lambda fn: fn()


# --- the base class ----------------------------------------------------------------------------------------------


class Module:
    """Subclass per module (docs/MODULES.md 1-2). Hooks have safe defaults; override what the module needs."""

    id: str = ""
    title: str = ""
    icon: Optional[str] = None  # symbol name of the panel sprite for the tab (docs/PANEL.md 1)
    description: str = ""
    order: int = 100
    events: Sequence[str] = ()
    requires: Sequence[str] = ()
    default_enabled: bool = True

    def __init__(self, ctx: ModuleContext) -> None:
        if not self.id or not self.title:
            raise ValueError(f"{type(self).__name__} needs id and title")
        self.ctx = ctx
        self.log = ctx.log
        self._started = False

    # lifecycle
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def project_opened(self, project: ProjectInfo) -> None: ...
    def project_closed(self) -> None: ...
    def on_event(self, name: str, data: Any) -> None: ...

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        return ActionResult(toast=("warn", f"{self.title}: unknown action {action_id}"), rerender=False)

    # rendering
    def render(self) -> list:
        return []

    def render_host(self) -> Optional[dict]:
        return None

    def settings_schema(self) -> list[SettingField]:
        return []

    def on_setting_changed(self, key: str, scope: str, value: Any) -> None:
        """After the host window stored a `settings_schema()` value (docs/MODULES.md 5): side effects only."""

    def section_status(self) -> Optional[dict]:
        return None

    # helpers that add the module prefix (docs/MODULES.md 4)
    def _id(self, local_id: str) -> str:
        return f"{self.id}.{local_id}"

    def strip_prefix(self, action_id: str) -> str:
        prefix = self.id + "."
        return action_id[len(prefix):] if action_id.startswith(prefix) else action_id

    def _pid(self, node_id: str) -> str:
        """The id with this module's prefix; an id that already carries it is kept (a module may pass either)."""
        return node_id if node_id.startswith(self.id + ".") else self._id(node_id)

    def button(self, local_id: str, label: str, **kw: Any) -> dict:
        menu = kw.get("menu")
        if isinstance(menu, dict) and isinstance(menu.get("id"), str):
            kw["menu"] = dict(menu, id=self._pid(menu["id"]))
        return P.node_button(self._pid(local_id), label, **kw)

    def menu(self, local_id: str, value: Any, options: list, badge: str) -> dict:
        """`button(..., menu=...)` value: the choice a tile carries (docs/PROTOCOL.md 6.1)."""
        return P.menu(self._pid(local_id), value, options, badge)

    def toggle(self, local_id: str, label: str, value: bool, **kw: Any) -> dict:
        return P.node_toggle(self._pid(local_id), label, value, **kw)

    def select(self, local_id: str, label: str, value: str, options: list, **kw: Any) -> dict:
        return P.node_select(self._pid(local_id), label, value, options, **kw)

    def input(self, local_id: str, label: str, value: str = "", **kw: Any) -> dict:
        return P.node_input(self._pid(local_id), label, value, **kw)

    def list_(self, local_id: str, rows: list, **kw: Any) -> dict:
        return P.node_list(self._pid(local_id), rows, **kw)

    def row_action(self, local_id: str, label: str, **kw: Any) -> dict:
        return self.button(local_id, label, **kw)

    def group(self, title: Optional[str], items: list, *, id: Optional[str] = None, **kw: Any) -> dict:
        """`id`, `columns`, `collapsible` and `tiles` as in `protocol.node_group`; the id gets the module prefix."""
        return P.node_group(title, items, id=None if id is None else self._pid(id), **kw)

    def progress(self, value: Optional[float], label: str = "", *, detail: Optional[str] = None,
                 cancel: Optional[str] = None) -> dict:
        """`cancel` is a local button id (the Stop pill sends it, prefixed)."""
        return P.node_progress(value, label, detail=detail, cancel=None if cancel is None else self._pid(cancel))

    text = staticmethod(P.node_text)
    row = staticmethod(P.node_row)
    divider = staticmethod(P.node_divider)
    list_row = staticmethod(P.list_row)
    status = staticmethod(P.status)

    def section(self) -> dict:
        """The module's panel section, validated against the protocol."""
        sec = P.section(self.id, self.title, self.render(), status=self.section_status(), icon=self.icon)
        P.validate_tree({"type": P.T_UI, "revision": 0, "title": "", "status": None, "sections": [sec], "footer": None})
        return sec


# --- test doubles (docs/MODULES.md 7) ----------------------------------------------------------------------------


class FakeJob:
    def __init__(self, job_id: str, title: str) -> None:
        self.id = job_id
        self.title = title
        self.cancelled = False
        self.progress_log: list[tuple[Optional[float], str]] = []
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.done = False

    def progress(self, value: Optional[float], label: str = "") -> None:
        self.progress_log.append((value, label))

    def cancel(self) -> None:
        self.cancelled = True


class FakeJobs:
    """Runs jobs inline (default) or on `step()` when `manual=True`. `high_priority` is what
    `high_priority_running()` answers; a test sets it to simulate a Files copy."""

    def __init__(self, manual: bool = False) -> None:
        self.manual = manual
        self.high_priority = False
        self.jobs: list[FakeJob] = []
        self._pending: list[tuple[FakeJob, Callable, Optional[Callable], Optional[Callable], Optional[Callable]]] = []

    def submit(self, fn, *, title, priority="normal", on_progress=None, on_done=None, on_error=None):
        job = FakeJob(f"j{len(self.jobs) + 1}", title)
        job.priority = priority
        self.jobs.append(job)
        self._pending.append((job, fn, on_progress, on_done, on_error))
        if not self.manual:
            self.step()
        return job

    def step(self) -> int:
        count = 0
        while self._pending:
            job, fn, on_progress, on_done, on_error = self._pending.pop(0)
            if on_progress is not None:
                original = job.progress

                def progress(value, label="", _job=job, _orig=original, _cb=on_progress):
                    _orig(value, label)
                    _cb(_job, value, label)

                job.progress = progress  # type: ignore[method-assign]
            try:
                job.result = fn(job)
                job.done = True
                if on_done is not None:
                    on_done(job, job.result)
            except BaseException as exc:  # noqa: BLE001 - tests inspect every error
                job.error = exc
                job.done = True
                if on_error is not None:
                    on_error(job, exc)
                else:
                    raise
            count += 1
        return count

    def running(self) -> list:
        return [job for job, *_ in self._pending]

    def high_priority_running(self) -> bool:
        return self.high_priority


class FakeScopedSettings:
    def __init__(self, values: Optional[dict] = None) -> None:
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def all(self):
        return dict(self.values)


class FakeSettings(FakeScopedSettings):
    def __init__(self, values=None, project_values=None) -> None:
        super().__init__(values)
        self.project = FakeScopedSettings(project_values)


class FakeTools:
    """`ffprobe` answers from `infos` (path -> MediaInfo) or by extension; `ffmpeg` records calls and returns 0;
    `ffmpeg_pipe` records the arguments and returns a Popen of `python -c "pass"` (empty stdout and stderr pipes)
    unless a test sets `pipe_factory` (a callable of the arguments returning a Popen)."""

    def __init__(self, infos: Optional[dict] = None, available: Sequence[str] = ("ffmpeg", "ffprobe")) -> None:
        self.infos = dict(infos or {})
        self.available = set(available)
        self.ffmpeg_calls: list[list[str]] = []
        self.ffmpeg_handler: Optional[Callable[[Sequence[str]], int]] = None
        self.pipe_calls: list[list[str]] = []
        self.pipe_factory: Optional[Callable[[Sequence[str]], subprocess.Popen]] = None

    def ffprobe(self, path: str) -> MediaInfo:
        if path in self.infos:
            return self.infos[path]
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return MediaInfo(path=path, kind=kind_from_extension(path), ext=Path(path).suffix.lower().lstrip("."), size=size)

    def ffmpeg(self, args, *, timeout=None, on_line=None) -> int:
        self.ffmpeg_calls.append(list(args))
        return self.ffmpeg_handler(args) if self.ffmpeg_handler else 0

    def ffmpeg_pipe(self, args) -> subprocess.Popen:
        self.pipe_calls.append(list(args))
        if self.pipe_factory is not None:
            return self.pipe_factory(args)
        return subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def has(self, tool: str) -> bool:
        return tool in self.available

    def ensure(self, tool: str, on_progress=None) -> str:
        self.available.add(tool)
        return tool + ".exe"

    def path_of(self, tool: str) -> Optional[str]:
        return tool + ".exe" if tool in self.available else None


class FakeUi:
    """Records toasts, results (`results`: (button id as given, outcome, text, detail)) and confirmations."""

    def __init__(self, confirm_answer: bool = True) -> None:
        self.toasts: list[tuple[str, str]] = []
        self.results: list[tuple[str, str, str, Optional[str]]] = []
        self.invalidations = 0
        self.confirm_answer = confirm_answer
        self.confirmations: list[str] = []

    def invalidate(self) -> None:
        self.invalidations += 1

    def toast(self, tone: str, text: str) -> None:
        self.toasts.append((tone, text))

    def result(self, button_id: str, outcome: str, text: str, detail: Optional[str] = None) -> None:
        if outcome not in ("ok", "warn", "error", "neutral"):
            raise ValueError(f"unknown outcome {outcome!r}")
        self.results.append((button_id, outcome, text, detail))

    def confirm(self, question: str) -> bool:
        self.confirmations.append(question)
        return self.confirm_answer


class FakeHostInfo:
    def __init__(self, version: str = "0.0.0", data_dir: str = "") -> None:
        self.version = version
        self.data_dir = data_dir
        self.opened: list[str] = []
        self.revealed: list[str] = []
        self.urls: list[str] = []
        self.shown: list[Optional[str]] = []

    def open_path(self, path: str) -> None:
        self.opened.append(path)

    def show(self, page: Optional[str] = None) -> None:
        self.shown.append(page)

    def reveal(self, path: str) -> None:
        self.revealed.append(path)

    def open_url(self, url: str) -> None:
        self.urls.append(url)


class FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, Any]] = []

    def emit(self, name: str, data: Any = None) -> None:
        self.emitted.append((name, data))


class FakePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.logs_dir = root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def cache_dir(self, module_id: str) -> Path:
        folder = self.root / "cache" / module_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder


class FakePremiere:
    """Records every call; answers from the attributes a test sets (`snapshot_value`, `selection_value`, ...).

    `eval` returns `eval_results[name]` (a value or a callable of args) or raises `eval_errors[name]`.
    Project items created by `import_files` get node ids `n<N>` and are appended to the snapshot.
    """

    def __init__(self, project: Optional[ProjectInfo] = None, snapshot: Optional[Snapshot] = None) -> None:
        self.connected = True
        self.project_value = project
        self.snapshot_value = snapshot or Snapshot(project_path=project.path if project else "", taken_at=time.time())
        self.selection_value = Selection(sequence_id="seq1")
        self.active_sequence_value: Optional[SequenceInfo] = None
        self.sequences_value: list[SequenceInfo] = []
        self.track_items_value: dict[str, list[TrackItem]] = {}
        self.playhead_value = 0.0
        self.workspaces_value = ["Editing", "Premiere Deck"]
        self.version_value = "26.3.0"
        self.eval_results: dict[str, Any] = {}
        self.eval_errors: dict[str, BaseException] = {}
        self.relink_result = True
        self.calls: list[tuple[str, tuple, dict]] = []
        self._next_node = 1000
        self.bins: set[str] = set()

    # bookkeeping
    def _record(self, name: str, *args: Any, **kw: Any) -> None:
        if not self.connected:
            raise PremiereUnavailable()
        self.calls.append((name, args, kw))

    def calls_named(self, name: str) -> list[tuple[tuple, dict]]:
        return [(args, kw) for n, args, kw in self.calls if n == name]

    def set_items(self, items: Sequence[ProjectItem]) -> None:
        self.snapshot_value = Snapshot(self.snapshot_value.project_path, time.time(), tuple(items))

    def _replace(self, node_id: str, **changes: Any) -> None:
        items = []
        for item in self.snapshot_value.items:
            if item.node_id == node_id:
                values = {**item.__dict__, **changes}
                item = ProjectItem(**values)
            items.append(item)
        self.set_items(items)

    # PremiereLike
    def is_connected(self) -> bool:
        return self.connected

    def project(self):
        self._record("project")
        return self.project_value

    def snapshot(self) -> Snapshot:
        self._record("snapshot")
        return self.snapshot_value

    def item(self, node_id: str):
        self._record("item", node_id)
        return self.snapshot_value.by_node_id().get(node_id)

    def create_bin_path(self, tree_path: str) -> str:
        self._record("create_bin_path", tree_path)
        self.bins.add(tree_path)
        return "bin:" + tree_path

    def move_to_bin(self, node_ids, tree_path: str) -> None:
        self._record("move_to_bin", tuple(node_ids), tree_path)
        self.bins.add(tree_path)
        for node_id in node_ids:
            self._replace(node_id, tree_path=tree_path)

    def relink(self, node_id: str, media_path: str) -> bool:
        self._record("relink", node_id, media_path)
        if self.relink_result:
            self._replace(node_id, media_path=media_path, is_offline=False)
        return self.relink_result

    def import_files(self, paths, tree_path: str, *, suppress_dialogs: bool = True):
        self._record("import_files", tuple(paths), tree_path, suppress_dialogs=suppress_dialogs)
        created = []
        for path in paths:
            self._next_node += 1
            item = ProjectItem(node_id=f"n{self._next_node}", name=Path(path).name, kind="clip", tree_path=tree_path,
                               media_path=path, has_video=kind_from_extension(path) == "video",
                               has_audio=kind_from_extension(path) in ("video", "audio"))
            created.append(item)
        self.set_items(list(self.snapshot_value.items) + created)
        return created

    def rename_item(self, node_id: str, name: str) -> None:
        self._record("rename_item", node_id, name)
        self._replace(node_id, name=name)

    def remove_items(self, node_ids) -> int:
        self._record("remove_items", tuple(node_ids))
        known = self.snapshot_value.by_node_id()
        gone = {node_id for node_id in node_ids if node_id in known}
        self.set_items([item for item in self.snapshot_value.items if item.node_id not in gone])
        return len(gone)

    def set_color_label(self, node_ids, label: int) -> None:
        self._record("set_color_label", tuple(node_ids), label)
        for node_id in node_ids:
            self._replace(node_id, color_label=label)

    def active_sequence(self):
        self._record("active_sequence")
        return self.active_sequence_value

    def sequences(self):
        self._record("sequences")
        return list(self.sequences_value)

    def selection(self) -> Selection:
        self._record("selection")
        return self.selection_value

    def track_items(self, sequence_id: str):
        self._record("track_items", sequence_id)
        return list(self.track_items_value.get(sequence_id, []))

    def playhead(self) -> float:
        self._record("playhead")
        return self.playhead_value

    def set_playhead(self, seconds: float) -> None:
        self._record("set_playhead", seconds)
        self.playhead_value = seconds

    def open_in_source(self, node_id: str, *, seconds=None) -> None:
        self._record("open_in_source", node_id, seconds=seconds)

    def insert_clip(self, node_id: str, *, start, end, at=None, video_track=0, audio_track=0) -> None:
        self._record("insert_clip", node_id, start=start, end=end, at=at, video_track=video_track, audio_track=audio_track)

    def add_marker(self, target_id: str, seconds: float, name: str, comment: str = "") -> None:
        self._record("add_marker", target_id, seconds, name, comment)

    def save_project(self) -> None:
        self._record("save_project")

    def save_project_as(self, path: str) -> None:
        self._record("save_project_as", path)
        if self.project_value:
            self.project_value = ProjectInfo(path=path, root=self.project_value.root, name=Path(path).stem,
                                             deck_dir=self.project_value.deck_dir)

    def open_project(self, path: str) -> None:
        self._record("open_project", path)

    def workspaces(self):
        self._record("workspaces")
        return list(self.workspaces_value)

    def set_workspace(self, name: str) -> None:
        self._record("set_workspace", name)

    def premiere_version(self) -> str:
        self._record("premiere_version")
        return self.version_value

    def eval(self, name: str, code: str, args: Any = None, *, timeout_ms: int = 30000) -> Any:
        self._record("eval", name, args=args, timeout_ms=timeout_ms)
        if name in self.eval_errors:
            raise self.eval_errors[name]
        result = self.eval_results.get(name)
        return result(args) if callable(result) else result

    def script(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""


class FakeHost:
    """Builds a `ModuleContext` on a scratch data folder (`PREMIERE_DECK_HOME` or a temp dir)."""

    def __init__(self, *, project: Optional[ProjectInfo] = None, snapshot: Optional[Snapshot] = None,
                 settings: Optional[dict] = None, project_settings: Optional[dict] = None,
                 infos: Optional[dict] = None, manual_jobs: bool = False, module_id: str = "test") -> None:
        home = os.environ.get("PREMIERE_DECK_HOME") or tempfile.mkdtemp(prefix="premiere-deck-test-")
        self.root = Path(home)
        self.premiere = FakePremiere(project, snapshot)
        self.jobs = FakeJobs(manual=manual_jobs)
        self.settings = FakeSettings(settings, project_settings)
        self.paths = FakePaths(self.root)
        self.tools = FakeTools(infos)
        self.ui = FakeUi()
        self.host = FakeHostInfo(data_dir=str(self.root))
        self.events = FakeEvents()
        self.log = logging.getLogger(f"module.{module_id}.test")
        self.project = project

    def context(self) -> ModuleContext:
        return ModuleContext(premiere=self.premiere, jobs=self.jobs, settings=self.settings, paths=self.paths,
                             tools=self.tools, ui=self.ui, host=self.host, events=self.events, log=self.log,
                             project=self.project, post=lambda fn: fn())

    def make_project(self, root: Optional[Path] = None, name: str = "Sample") -> ProjectInfo:
        root = root or (self.root / "projects" / name)
        root.mkdir(parents=True, exist_ok=True)
        info = ProjectInfo(path=str(root / f"{name}.prproj"), root=str(root), name=name, deck_dir=str(root / ".premiere-deck"))
        (root / f"{name}.prproj").write_bytes(b"")
        self.project = info
        self.premiere.project_value = info
        self.premiere.snapshot_value = Snapshot(project_path=info.path, taken_at=time.time(), items=self.premiere.snapshot_value.items)
        return info


__all__ = [name for name in globals() if not name.startswith("_")] + ["threading", "field"]
