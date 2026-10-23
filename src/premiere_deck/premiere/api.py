"""The Premiere facade (docs/PREMIERE_API.md): typed operations over the agent connection.

Every operation is one jsx file in `jsx/`, sent as a `script` message (docs/PROTOCOL.md 4, 7) through an
`AgentTransport`; results are decoded into the dataclasses of `premiere.model`. One script is in flight per agent;
callers block on a future. The facade also owns the `subscribe` list, the project state derived from heartbeats
and agent events, and the event callbacks the registry listens to.

Threads: the transport calls `_on_message` / `_on_presence` on its own thread (the server's asyncio loop). Public
operations block, so they must not run on that thread; once `bind_module_thread` was called they run on the module
thread only (docs/MODULES.md 2) and raise `RuntimeError` anywhere else.
"""

from __future__ import annotations

import itertools
import logging
import ntpath
import os
import threading
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from premiere_deck.host import protocol
from premiere_deck.premiere.model import (
    PremiereError, PremiereUnavailable, ProjectInfo, ProjectItem, Selection, SequenceInfo, Snapshot,
    TrackItem, item_from_json,
)

log = logging.getLogger("premiere.api")
panel_log = logging.getLogger("panel.agent")

JSX_DIR = Path(__file__).resolve().parent / "jsx"
DECK_DIR_NAME = ".premiere-deck"
USES_LIB = "// @uses lib"
USES_LIB_SENT = "// (uses lib)"
SNAPSHOT_TIMEOUT_MS = 120000
SNAPSHOT_PAGE_ITEMS = 2000
# The agent reports its own timeout; the facade waits this much longer before it gives up on the agent.
TIMEOUT_GRACE_S = 5.0
# Upper bound for a caller waiting in the queue behind other scripts.
MAX_WAIT_S = 600.0
MARKER_FALLBACK_NAME = "Deck"

EventCallback = Callable[[str, Any], None]


class _Call:
    __slots__ = ("id", "name", "message", "future", "timeout_ms", "lib_lines", "sent_at", "timer")

    def __init__(self, call_id: str, name: str, message: dict, timeout_ms: int, lib_lines: int) -> None:
        self.id = call_id
        self.name = name
        self.message = message
        self.future: Future = Future()
        self.timeout_ms = timeout_ms
        self.lib_lines = lib_lines
        self.sent_at = 0.0
        self.timer: Optional[threading.Timer] = None


def same_path(a: Optional[str], b: Optional[str]) -> bool:
    """Windows paths compare case-insensitively and without regard to slash direction (PREMIERE_API 5)."""
    if not a or not b:
        return not a and not b
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def project_info_from_path(path: str) -> ProjectInfo:
    """`root` = the folder of the .prproj, or its parent when the parent holds a .premiere-deck folder and the
    project's own folder does not (same rule as host.hub.project_info_for, docs/PREMIERE_API.md 2)."""
    folder = ntpath.dirname(path)
    parent = ntpath.dirname(folder)
    root = folder
    if (parent and parent != folder and os.path.isdir(ntpath.join(parent, DECK_DIR_NAME))
            and not os.path.isdir(ntpath.join(folder, DECK_DIR_NAME))):
        root = parent
    name = ntpath.splitext(ntpath.basename(path))[0]
    return ProjectInfo(path=path, root=root, name=name, deck_dir=ntpath.join(root, DECK_DIR_NAME))


def sequence_from_json(data: dict) -> SequenceInfo:
    return SequenceInfo(
        sequence_id=str(data["sequence_id"]),
        name=str(data.get("name", "")),
        width=int(data.get("width") or 0),
        height=int(data.get("height") or 0),
        fps=float(data.get("fps") or 0.0),
        duration=float(data.get("duration") or 0.0),
        in_point=None if data.get("in_point") is None else float(data["in_point"]),
        out_point=None if data.get("out_point") is None else float(data["out_point"]),
    )


def track_item_from_json(data: dict) -> TrackItem:
    return TrackItem(
        sequence_id=str(data["sequence_id"]),
        track_kind=str(data["track_kind"]),
        track_index=int(data["track_index"]),
        index=int(data["index"]),
        name=str(data.get("name", "")),
        node_id=None if data.get("node_id") in (None, "") else str(data["node_id"]),
        start=float(data["start"]),
        end=float(data["end"]),
        in_point=float(data["in_point"]),
        out_point=float(data["out_point"]),
        selected=bool(data.get("selected", False)),
    )


class PremiereApi:
    """`PremiereLike` over an `AgentTransport` (docs/PREMIERE_API.md 2-4)."""

    def __init__(self, transport: protocol.AgentTransport, jsx_dir: Optional[os.PathLike | str] = None) -> None:
        self._transport = transport
        self._jsx_dir = Path(jsx_dir) if jsx_dir else JSX_DIR
        self._lock = threading.RLock()
        self._queue: deque[_Call] = deque()
        self._inflight: Optional[_Call] = None
        self._pages: dict[str, protocol.ScriptResultPages] = {}
        self._ids = itertools.count(1)
        self._scripts: dict[str, str] = {}
        self._connected = False
        self._hello: Optional[protocol.Hello] = None
        self._events: tuple[str, ...] = ()
        self._callbacks: list[EventCallback] = []
        self._project_path: Optional[str] = None
        self._active_sequence_id: Optional[str] = None
        self._last_heartbeat: Optional[float] = None
        self._transport_thread: Optional[int] = None
        self._module_thread: Optional[int] = None
        self._module_submit: Optional[Callable[[Callable[[], None]], None]] = None
        transport.set_handler(self._on_message)
        transport.set_presence_handler(self._on_presence)

    # --- wiring used by the registry / hub ---------------------------------------------------------------------

    def bind_module_thread(self, thread: Optional[threading.Thread] = None,
                           submit: Optional[Callable[[Callable[[], None]], None]] = None) -> None:
        """From now on operations run on `thread` only (default: the calling thread). `submit(fn)` schedules `fn`
        on that thread; it backs `run_on_module_thread`."""
        self._module_thread = (thread or threading.current_thread()).ident
        self._module_submit = submit

    def run_on_module_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Runs `fn(*args, **kwargs)` on the module thread (at once when already there); returns a Future."""
        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # handed to the caller through the future
                future.set_exception(exc)

        if self._module_thread is None or threading.get_ident() == self._module_thread:
            run()
            return future
        if self._module_submit is None:
            raise RuntimeError("no module thread executor was bound")
        self._module_submit(run)
        return future

    def on_event(self, callback: EventCallback) -> None:
        """`callback(name, data)` for every Premiere event (PROTOCOL 8) and the derived project.opened / closed.
        Called on the transport thread; the receiver marshals onto its own thread."""
        with self._lock:
            self._callbacks.append(callback)

    def set_events(self, names: Sequence[str]) -> None:
        """The union of the modules' events; sent as `subscribe` now and after every reconnect."""
        message = protocol.subscribe(names)
        with self._lock:
            self._events = tuple(message["events"])
            connected = self._connected
        if connected:
            self._send(message)

    # Aliases of the hooks above under their alternative names (same behaviour).
    def set_event_handler(self, callback: EventCallback) -> None:
        self.on_event(callback)

    def set_subscriptions(self, names: Sequence[str]) -> None:
        self.set_events(names)

    def set_module_executor(self, submit: Callable[[Callable[[], None]], None]) -> None:
        """`submit(fn)` runs `fn` on the module thread; backs `run_on_module_thread`."""
        self._module_submit = submit

    def close(self) -> None:
        """Host shutdown: every queued or running call fails with PremiereUnavailable; later calls fail at once."""
        with self._lock:
            self._connected = False
        self._fail_all(PremiereUnavailable("Premiere Deck is closing"))

    @property
    def events(self) -> tuple[str, ...]:
        return self._events

    @property
    def project_path(self) -> Optional[str]:
        """The project path last reported by the agent (hello, heartbeat or event); no round trip."""
        return self._project_path

    @property
    def active_sequence_id(self) -> Optional[str]:
        return self._active_sequence_id

    @property
    def hello(self) -> Optional[protocol.Hello]:
        return self._hello

    # --- transport callbacks (transport thread) ----------------------------------------------------------------

    def _on_presence(self, connected: bool, hello: Optional[protocol.Hello]) -> None:
        if connected:
            with self._lock:
                replaced = self._connected
                self._connected = True
                self._hello = hello
                events = self._events
            if replaced:
                # A new agent replaced the old one: the script in flight on the old socket is lost; queued ones
                # go to the new agent.
                self._fail_inflight(PremiereUnavailable("Premiere Pro reconnected"))
            self._send(protocol.subscribe(events))
            self._set_project(hello.project_path if hello else None)
            self._pump()
        else:
            with self._lock:
                self._connected = False
                self._hello = None
            self._fail_all(PremiereUnavailable())
            self._set_project(None)

    def _on_message(self, message: dict) -> None:
        self._transport_thread = threading.get_ident()
        kind = message.get("type")
        if kind == protocol.T_SCRIPT_RESULT:
            self._on_result(message)
        elif kind == protocol.T_HEARTBEAT:
            self._last_heartbeat = time.monotonic()
            self._active_sequence_id = message.get("active_sequence_id") or None
            self._set_project(message.get("project_path") or None)
        elif kind == protocol.T_EVENT:
            self._on_agent_event(str(message.get("name", "")), message.get("data") or {})
        elif kind == protocol.T_LOG:
            level = getattr(logging, str(message.get("level", "info")).upper(), logging.INFO)
            panel_log.log(level, "%s", message.get("message", ""))
        else:
            log.debug("ignored agent message %s", kind)

    def _on_agent_event(self, name: str, data: Any) -> None:
        if name == "project.opened":
            self._set_project((data or {}).get("path") or None)
        elif name == "project.closed":
            closed = (data or {}).get("path")
            if closed is None or same_path(closed, self._project_path):
                self._set_project(None)
        else:
            if name == "sequence.activated" and isinstance(data, dict) and data.get("sequence_id"):
                self._active_sequence_id = str(data["sequence_id"])
            self._emit(name, data)

    def _set_project(self, path: Optional[str]) -> None:
        with self._lock:
            old = self._project_path
            if same_path(old, path):
                return
            self._project_path = path
        if old:
            self._emit("project.closed", {"path": old})
        if path:
            self._emit("project.opened", {"path": path})

    def _emit(self, name: str, data: Any) -> None:
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(name, data)
            except Exception:
                log.exception("event callback failed for %s", name)

    # --- queue --------------------------------------------------------------------------------------------------

    def _send(self, message: dict) -> None:
        try:
            self._transport.send(message)
        except Exception:
            log.exception("send %s failed", message.get("type"))

    def _pump(self) -> None:
        with self._lock:
            if self._inflight is not None or not self._queue or not self._connected:
                return
            call = self._queue.popleft()
            self._inflight = call
            call.sent_at = time.monotonic()
            call.timer = threading.Timer(call.timeout_ms / 1000.0 + TIMEOUT_GRACE_S, self._watchdog, (call,))
            call.timer.daemon = True
            call.timer.start()
        try:
            self._transport.send(call.message)
        except Exception as exc:
            self._finish(call, error=PremiereUnavailable(f"send failed: {exc}"))

    def _watchdog(self, call: _Call) -> None:
        with self._lock:
            if self._inflight is not call:
                return
        log.warning("%s: no result from the agent within %d ms", call.name, call.timeout_ms)
        self._finish(call, error=PremiereError("timeout", None, call.message.get("code", "")[:200]))

    def _finish(self, call: _Call, *, result: Any = None, error: Optional[BaseException] = None) -> None:
        with self._lock:
            if self._inflight is call:
                self._inflight = None
            if call.timer is not None:
                call.timer.cancel()
            self._pages.pop(call.id, None)
        ms = int((time.monotonic() - call.sent_at) * 1000) if call.sent_at else 0
        if error is None:
            log.debug("%s %d ok", call.name, ms)
            if not call.future.done():
                call.future.set_result(result)
        else:
            log.warning("%s %d failed: %s", call.name, ms, getattr(error, "message", error))
            if not call.future.done():
                call.future.set_exception(error)
        self._pump()

    def _fail_inflight(self, error: PremiereError) -> None:
        with self._lock:
            call = self._inflight
            self._inflight = None
            if call is not None:
                if call.timer is not None:
                    call.timer.cancel()
                self._pages.pop(call.id, None)
        if call is not None and not call.future.done():
            call.future.set_exception(error)

    def _fail_all(self, error: PremiereError) -> None:
        with self._lock:
            calls = list(self._queue)
            self._queue.clear()
            inflight = self._inflight
        if inflight is not None:
            calls.insert(0, inflight)
        for call in calls:
            with self._lock:
                if self._inflight is call:
                    self._inflight = None
                if call.timer is not None:
                    call.timer.cancel()
                self._pages.pop(call.id, None)
            if not call.future.done():
                call.future.set_exception(error)

    def _on_result(self, message: dict) -> None:
        call_id = str(message.get("id", ""))
        if protocol.is_paged(message):
            with self._lock:
                pages = self._pages.get(call_id)
                if pages is None:
                    pages = self._pages[call_id] = protocol.ScriptResultPages(int(message["pages"]))
            try:
                complete = pages.add(message)
            except ValueError as exc:
                complete = {"type": protocol.T_SCRIPT_RESULT, "id": call_id, "ok": False,
                            "error": {"message": f"paged result is not JSON: {exc}"}}
            if complete is None:
                return
            message = complete
        with self._lock:
            call = self._inflight if self._inflight is not None and self._inflight.id == call_id else None
        if call is None:
            log.debug("late or unknown script.result %s", call_id)
            return
        if message.get("ok"):
            self._finish(call, result=message.get("result"))
            return
        error = message.get("error") or {}
        line = error.get("line")
        try:
            line = None if line is None else int(line)
        except (TypeError, ValueError):
            line = None
        if line is not None and call.lib_lines and line > call.lib_lines:
            line -= call.lib_lines
        self._finish(call, error=PremiereError(str(error.get("message") or "script failed"), line,
                                               str(error.get("source") or "")))

    # --- scripts ------------------------------------------------------------------------------------------------

    def script(self, path: str | os.PathLike) -> str:
        """The cached text of a jsx file."""
        key = str(Path(path).resolve())
        with self._lock:
            text = self._scripts.get(key)
        if text is None:
            text = Path(key).read_text(encoding="utf-8")
            with self._lock:
                self._scripts[key] = text
        return text

    def _program(self, code: str) -> tuple[str, int]:
        """`code` with lib.jsx prepended when its first three lines declare `// @uses lib`.

        The marker line itself is neutralised: ExtendScript reads `// @name` comments as preprocessor directives and
        rejects unknown ones with "Syntax error" (PREMIERE_API 7.6)."""
        lines = code.lstrip("﻿").split("\n")
        marker = next((i for i, line in enumerate(lines[:3]) if line.strip() == USES_LIB), None)
        if marker is None:
            return code, 0
        lines[marker] = USES_LIB_SENT
        lib = self.script(self._jsx_dir / "lib.jsx").rstrip("\n") + "\n"
        return lib + "\n".join(lines), lib.count("\n")

    def _check_thread(self) -> None:
        ident = threading.get_ident()
        if self._transport_thread is not None and ident == self._transport_thread:
            raise RuntimeError("PremiereApi blocks; it cannot be called on the transport thread")
        if self._module_thread is not None and ident != self._module_thread:
            raise RuntimeError("PremiereApi is called from the module thread only; use run_on_module_thread")

    def is_connected(self) -> bool:
        return self._connected

    def eval(self, name: str, code: str, args: Any = None, *, timeout_ms: int = protocol.DEFAULT_SCRIPT_TIMEOUT_MS) -> Any:
        """Runs a raw script (PROTOCOL 7). `code` may be the path of a .jsx file (read once and cached)."""
        self._check_thread()
        text = code
        if "\n" not in code and code.lower().endswith(".jsx") and os.path.isfile(code):
            text = self.script(code)
        program, lib_lines = self._program(text)
        with self._lock:
            if not self._connected:
                raise PremiereUnavailable()
            call_id = f"q{next(self._ids)}"
            call = _Call(call_id, name, protocol.script(call_id, name, program, args, timeout_ms), int(timeout_ms), lib_lines)
            self._queue.append(call)
        self._pump()
        try:
            return call.future.result(timeout=MAX_WAIT_S + timeout_ms / 1000.0)
        except TimeoutError:
            with self._lock:
                if call in self._queue:
                    self._queue.remove(call)
            raise PremiereError("timeout", None, program[:200]) from None

    def _run(self, operation: str, args: Any = None, *, timeout_ms: int = protocol.DEFAULT_SCRIPT_TIMEOUT_MS) -> Any:
        return self.eval(operation, self.script(self._jsx_dir / f"{operation}.jsx"), args, timeout_ms=timeout_ms)

    # --- operations (PREMIERE_API 2) ----------------------------------------------------------------------------

    def project(self) -> Optional[ProjectInfo]:
        data = self._run("project_info")
        if not data or not data.get("path"):
            return None
        return project_info_from_path(str(data["path"]))

    def snapshot(self) -> Snapshot:
        taken_at = time.time()
        items: list[ProjectItem] = []
        start = 0
        token = None
        project_path = ""
        while True:
            page = self._run("snapshot", {"start": start, "limit": SNAPSHOT_PAGE_ITEMS, "token": token},
                             timeout_ms=SNAPSHOT_TIMEOUT_MS)
            if page is None:
                return Snapshot(project_path="", taken_at=taken_at, items=())
            if page.get("stale"):
                # The project changed between pages (another project opened): start over once.
                if token is None:
                    raise PremiereError("snapshot token rejected on the first page")
                items, start, token = [], 0, None
                continue
            token = page.get("token")
            project_path = str(page.get("project_path") or "")
            items.extend(item_from_json(item) for item in page.get("items", []))
            following = page.get("next")
            if following is None:
                break
            start = int(following)
        return Snapshot(project_path=project_path, taken_at=taken_at, items=tuple(items))

    def item(self, node_id: str) -> Optional[ProjectItem]:
        data = self._run("item", {"node_id": node_id})
        return item_from_json(data) if data else None

    def create_bin_path(self, tree_path: str) -> str:
        return str(self._run("create_bin_path", {"tree_path": tree_path})["node_id"])

    def move_to_bin(self, node_ids: Sequence[str], tree_path: str) -> None:
        result = self._run("move_to_bin", {"node_ids": list(node_ids), "tree_path": tree_path})
        missing = result.get("missing") or []
        if missing:
            raise PremiereError(f"project items not found: {', '.join(missing)}")

    def relink(self, node_id: str, media_path: str) -> bool:
        result = self._run("relink", {"node_id": node_id, "media_path": media_path})
        return bool(result and result.get("online"))

    def import_files(self, paths: Sequence[str], tree_path: str, *, suppress_dialogs: bool = True) -> list[ProjectItem]:
        result = self._run("import_files", {"paths": list(paths), "tree_path": tree_path,
                                            "suppress_dialogs": bool(suppress_dialogs)}, timeout_ms=120000)
        missing = result.get("missing") or []
        failed = result.get("failed") or []
        if missing or failed:
            # The caller compares the returned items with its paths; Premiere gives no reason (PREMIERE_API 7.2).
            log.warning("import_files: %d not found, %d not imported: %s", len(missing), len(failed),
                        ", ".join(list(missing) + list(failed))[:500])
        return [item_from_json(item) for item in result.get("items", [])]

    def rename_item(self, node_id: str, name: str) -> None:
        self._run("rename_item", {"node_id": node_id, "name": name})

    def remove_items(self, node_ids: Sequence[str]) -> int:
        result = self._run("remove_items", {"node_ids": [str(node_id) for node_id in node_ids]}) or {}
        missing = result.get("missing") or []
        if missing:
            # An item that is already gone is the wanted end state (PREMIERE_API 2); keep a trace only.
            log.warning("remove_items: %d not found: %s", len(missing), ", ".join(missing)[:500])
        return int(result.get("removed", 0))

    def set_color_label(self, node_ids: Sequence[str], label: int) -> None:
        if not 0 <= int(label) <= 15:
            raise ValueError("label is 0..15")
        result = self._run("set_color_label", {"node_ids": list(node_ids), "label": int(label)})
        missing = result.get("missing") or []
        if missing:
            raise PremiereError(f"project items not found: {', '.join(missing)}")

    def active_sequence(self) -> Optional[SequenceInfo]:
        data = self._run("active_sequence")
        return sequence_from_json(data) if data else None

    def sequences(self) -> list[SequenceInfo]:
        return [sequence_from_json(item) for item in self._run("sequences") or []]

    def selection(self) -> Selection:
        data = self._run("selection")
        if not data:
            return Selection(sequence_id="")
        return Selection(sequence_id=str(data.get("sequence_id") or ""),
                         items=tuple(track_item_from_json(item) for item in data.get("items", [])))

    def track_items(self, sequence_id: str) -> list[TrackItem]:
        return [track_item_from_json(item) for item in self._run("track_items", {"sequence_id": sequence_id}) or []]

    def playhead(self) -> float:
        return float(self._run("playhead", {"set": None})["seconds"])

    def set_playhead(self, seconds: float) -> None:
        self._run("playhead", {"set": float(seconds)})

    def open_in_source(self, node_id: str, *, seconds: Optional[float] = None) -> None:
        result = self._run("open_in_source", {"node_id": node_id, "seconds": seconds})
        if seconds is not None and not (result or {}).get("positioned"):
            # PREMIERE_API 7.4: the Source Monitor could not be positioned; leave a findable marker instead.
            self.add_marker(node_id, seconds, MARKER_FALLBACK_NAME)

    def insert_clip(self, node_id: str, *, start: float, end: float, at: Optional[float] = None,
                    video_track: int = 0, audio_track: int = 0) -> None:
        if end <= start:
            raise ValueError("end must be after start")
        self._run("insert_clip", {"node_id": node_id, "start": float(start), "end": float(end),
                                  "at": None if at is None else float(at),
                                  "video_track": int(video_track), "audio_track": int(audio_track)})

    def add_marker(self, target_id: str, seconds: float, name: str, comment: str = "") -> None:
        self._run("add_marker", {"target_id": target_id, "seconds": float(seconds), "name": name, "comment": comment})

    def save_project(self) -> None:
        self._run("save", {"path": None}, timeout_ms=120000)

    def save_project_as(self, path: str) -> None:
        self._run("save", {"path": path}, timeout_ms=120000)

    def open_project(self, path: str) -> None:
        self._run("open_project", {"path": path}, timeout_ms=120000)

    def workspaces(self) -> list[str]:
        return [str(name) for name in self._run("workspace", {"set": None}).get("workspaces", [])]

    def set_workspace(self, name: str) -> None:
        result = self._run("workspace", {"set": name})
        if not result.get("ok"):
            raise PremiereError(f"no workspace named {name!r}")

    def premiere_version(self) -> str:
        return str(self._run("version"))


__all__ = ["PremiereApi", "PremiereError", "PremiereUnavailable", "project_info_from_path", "same_path",
           "sequence_from_json", "track_item_from_json"]
