"""pywebview `js_api` object (`Bridge`) and the Python -> JS event pump (`EventPump`).

Contract: docs/BRIDGE_API.md. pywebview exposes every public attribute of the js_api object, so `Bridge` has exactly
the methods of BRIDGE_API.md section 2 as public names (`CONTRACT_METHODS`); everything else starts with an
underscore. The hub (host/hub.py) does the work; the bridge validates arguments, turns failures into one sentence
and owns the window-side concerns (dialogs, hide / quit, updater).
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from premiere_deck.core.jobs import JobCancelled

from . import winutil

log = logging.getLogger("premiere_deck.shell.bridge")
ui_log = logging.getLogger("premiere_deck.ui")

LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
MAX_UI_LOG_CHARS = 2000
_DIALOG_OPEN = 10  # webview.FileDialog.OPEN
MSG_FAILED = "The operation failed; details are in the log."
PROJECT_TYPES = ("Premiere Pro projects (*.prproj)", "All files (*.*)")
EXE_TYPES = ("Programs (*.exe)", "All files (*.*)")


class BridgeError(Exception):
    """An error whose message is already a user-facing English sentence."""


def friendly_message(exc: BaseException) -> str:
    text = " ".join(str(getattr(exc, "message", None) or exc).split())
    if isinstance(exc, (BridgeError, ValueError)) and text:
        return text
    if isinstance(exc, PermissionError):
        return "No access to the file or folder."
    if isinstance(exc, FileNotFoundError):
        return text if text.endswith(".") and " " in text else "The file was not found."
    if isinstance(exc, TimeoutError):
        return "Premiere Pro did not answer in time."
    if isinstance(exc, OSError):
        return "File system error" + (f": {exc.strerror}." if exc.strerror else ".")
    return MSG_FAILED


def _api(fn: Callable) -> Callable:
    """Wrap a Bridge method: log failures and re-raise them as BridgeError with an English sentence."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except BridgeError as exc:
            log.info("bridge.%s refused: %s", fn.__name__, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - the page needs a sentence for any failure
            if isinstance(exc, ValueError):
                log.info("bridge.%s rejected: %s", fn.__name__, exc)
            else:
                log.exception("bridge.%s failed", fn.__name__)
            raise BridgeError(friendly_message(exc)) from None

    # pywebview reads parameter names with inspect.getfullargspec, which honours __signature__ but not __wrapped__.
    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


# --- event pump --------------------------------------------------------------------------------------------------

def js_literal(value: Any) -> str:
    """JSON that is also a safe JavaScript expression (U+2028 / U+2029 escaped, non-ASCII kept)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_event_script(events: list[tuple[str, Any]]) -> str:
    calls = "".join(
        f"try{{d({js_literal(name)},{js_literal(payload)})}}catch(e){{console.error(e)}};" for name, payload in events)
    return ("(function(){var h=window.premiereDeck;if(!h||typeof h.dispatch!=='function')return;"
            "var d=function(n,p){h.dispatch(n,p)};" + calls + "})();")


COALESCED = {"job": "id", "premiere": None, "modules": None, "panel": None, "settings": None, "update": None}


def _coalesce_key(name: str, payload: Any) -> Optional[str]:
    if name not in COALESCED:
        return None
    field = COALESCED[name]
    if field is None:
        return name
    if isinstance(payload, dict) and payload.get(field) is not None:
        return f"{name}:{payload[field]}"
    return None


class EventPump:
    """Thread-safe queue of events for the page.

    - Nothing is delivered before `set_ready()`; everything emitted earlier is kept.
    - State-like events (`premiere`, `modules`, `panel`, `settings`, `update`) and `job` per id are coalesced: a
      pending event keeps its place and takes the newest payload. Everything else is delivered in order.
    - Batches go out through `deliver(script)` (window.run_js) on one daemon thread, at most one per `min_interval`.
    """

    def __init__(self, deliver: Optional[Callable[[str], Any]] = None, start_delay: float = 0.1,
                 min_interval: float = 0.04, threaded: bool = True) -> None:
        self._deliver = deliver
        self._start_delay = start_delay
        self._min_interval = min_interval
        self._threaded = threaded
        self._cond = threading.Condition()
        self._queue: list[list] = []  # [name, payload, key]
        self._pending: dict[str, list] = {}
        self._ready_at: Optional[float] = None
        self._next_at = 0.0
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    def set_deliver(self, deliver: Callable[[str], Any]) -> None:
        with self._cond:
            self._deliver = deliver
            self._cond.notify_all()

    def emit(self, name: str, payload: Any = None) -> None:
        with self._cond:
            if self._stopped:
                return
            key = _coalesce_key(name, payload)
            if key is not None and key in self._pending:
                self._pending[key][1] = payload
            else:
                entry = [name, payload, key]
                self._queue.append(entry)
                if key is not None:
                    self._pending[key] = entry
            self._cond.notify_all()

    def set_ready(self) -> None:
        with self._cond:
            if self._ready_at is None:
                self._ready_at = time.monotonic() + self._start_delay
            self._cond.notify_all()
            start = self._threaded and self._thread is None and not self._stopped
            if start:
                self._thread = threading.Thread(target=self._run, name="event-pump", daemon=True)
        if start:
            self._thread.start()  # type: ignore[union-attr]

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    @property
    def ready(self) -> bool:
        return self._ready_at is not None

    def pending(self) -> list[tuple[str, Any]]:
        with self._cond:
            return [(name, payload) for name, payload, _ in self._queue]

    def take(self) -> list[tuple[str, Any]]:
        with self._cond:
            if self._ready_at is None or self._deliver is None or not self._queue:
                return []
            events = [(name, payload) for name, payload, _ in self._queue]
            self._queue = []
            self._pending = {}
            return events

    def flush(self) -> int:
        events = self.take()
        if not events:
            return 0
        try:
            self._deliver(build_event_script(events))  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - the window may be closing
            log.debug("event delivery failed", exc_info=True)
        return len(events)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    if self._ready_at is not None and self._deliver is not None and self._queue:
                        delay = max(self._ready_at, self._next_at) - time.monotonic()
                        if delay <= 0:
                            break
                        self._cond.wait(delay)
                    else:
                        self._cond.wait()
                if self._stopped:
                    return
            self.flush()
            self._next_at = time.monotonic() + self._min_interval


# --- bridge ------------------------------------------------------------------------------------------------------

def _default_update_status(version: str) -> dict:
    return {"stage": "idle", "current": version, "latest": None, "notes": None, "size": None, "published_at": None,
            "progress": None, "message": "Updates are not checked yet", "checked_at": None, "can_apply": False}


class Bridge:
    """The js_api object. Contract: docs/BRIDGE_API.md, sections 2 (methods) and 6 (shell rules)."""

    def __init__(self, hub: Any, pump: EventPump, version: str) -> None:
        self._hub = hub
        self._pump = pump
        self._version = version
        self._window: Any = None
        self._updater: Any = None
        self._scheduler: Any = None
        self._lock = threading.RLock()
        self._focused = True
        self._hidden = False
        self._quitting = False
        self._auto_download_for: Optional[str] = None
        self._shut_down = False
        self._on_hide: Callable[[], None] = lambda: None
        hub.emit = self._emit

    # -- wiring (underscore names are not exposed to JS)
    def _attach_window(self, window: Any) -> None:
        self._window = window
        self._pump.set_deliver(getattr(window, "run_js", None) or window.evaluate_js)

    def _attach_updater(self, updater: Any, scheduler: Any = None) -> None:
        self._updater = updater
        self._scheduler = scheduler

    def _emit(self, name: str, payload: Any = None) -> None:
        self._pump.emit(name, payload)

    def _toast(self, message: str, kind: str = "info") -> None:
        self._emit("toast", {"kind": kind, "message": message})

    def _on_focus(self, focused: bool) -> None:
        focused = bool(focused)
        with self._lock:
            if focused == self._focused:
                return
            self._focused = focused
        self._emit("window", {"focused": focused})

    def _update_status(self) -> dict:
        return self._updater.status() if self._updater is not None else _default_update_status(self._version)

    def _state(self) -> dict:
        state = self._hub.state()
        state["update"] = self._update_status()
        state["window"] = {"focused": self._focused}
        return state

    def _need_window(self) -> Any:
        if self._window is None:
            raise BridgeError("The window is not ready.")
        return self._window

    def _dialog_kind(self) -> Any:
        try:
            import webview

            return webview.FileDialog.OPEN
        except Exception:  # noqa: BLE001 - tests without pywebview
            return _DIALOG_OPEN

    def _open_dialog(self, file_types: tuple, directory: str = "") -> Optional[str]:
        window = self._need_window()
        result = window.create_file_dialog(self._dialog_kind(), directory=directory if os.path.isdir(directory) else "",
                                           allow_multiple=False, file_types=file_types)
        if not result:
            return None
        return str(result[0] if isinstance(result, (list, tuple)) else result)

    # -- window visibility (shell)
    def _should_hide(self) -> bool:
        if self._quitting:
            return False
        premiere = self._hub.premiere_state()
        return bool(premiere.get("running")) and bool(self._hub.settings.get("keep_running_with_premiere", True))

    def _hide(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.hide()
            self._hidden = True
            self._on_hide()
        except Exception:  # noqa: BLE001
            log.debug("hide failed", exc_info=True)

    def _show(self, page: Optional[str] = None) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.show()
            window.restore()
        except Exception:  # noqa: BLE001
            log.debug("show failed", exc_info=True)
        self._hidden = False
        winutil.bring_window_to_front(getattr(window, "title", ""), attempts=1)
        if page:
            self._emit("navigate", {"page": page})

    def _quit(self) -> None:
        self._quitting = True
        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                log.debug("window destroy failed", exc_info=True)

    def _later(self, fn: Callable[[], None], delay: float = 0.2) -> None:
        """Run `fn` right after the current call has returned its value to the page."""
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()

    # -- self-update wiring
    def _on_update_status(self, status: dict) -> None:
        self._emit("update", status)
        if status.get("stage") != "available" or not self._hub.settings.get("auto_download_updates", True):
            return
        version = str(status.get("latest") or "")
        with self._lock:
            if self._auto_download_for == version:
                return
            self._auto_download_for = version
        threading.Thread(target=self._auto_download, name="update-auto-download", daemon=True).start()

    def _auto_download(self) -> None:
        updater = self._updater
        if updater is None:
            return
        for _ in range(50):
            if updater.status().get("stage") != "available":
                return
            updater.download_background()
            if updater.status().get("stage") == "downloading":
                return
            time.sleep(0.1)

    def _on_update_toast(self, level: str, text: str) -> None:
        self._toast(text, "success" if level == "success" else ("error" if level == "error" else "info"))

    def _start_updates(self) -> None:
        updater = self._updater
        if updater is None:
            return
        try:
            updater.startup()
            updater.check_background()
        except Exception:  # noqa: BLE001
            log.warning("update check could not start", exc_info=True)
        if self._scheduler is not None:
            try:
                self._scheduler.start()
            except Exception:  # noqa: BLE001
                log.warning("periodic update checks could not start", exc_info=True)

    def _shutdown(self) -> None:
        """Idempotent: stop the scheduler, the hub (modules, jobs, server) and the pump."""
        with self._lock:
            if self._shut_down:
                return
            self._shut_down = True
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            except Exception:  # noqa: BLE001
                log.debug("scheduler stop failed", exc_info=True)
        try:
            self._hub.stop()
        except Exception:  # noqa: BLE001
            log.exception("hub stop failed")
        self._pump.stop()

    # ================================================================================================================
    # Contract methods (BRIDGE_API.md 2). Every public name below is exposed to the page.

    @_api
    def ready(self):
        state = self._state()
        self._pump.set_ready()
        return state

    @_api
    def get_state(self):
        return self._state()

    @_api
    def get_settings(self):
        return self._hub.settings.public()

    @_api
    def set_settings(self, patch):
        if not isinstance(patch, dict):
            raise BridgeError("Invalid settings.")
        before = self._hub.settings.public()
        try:
            after = self._hub.settings.patch(patch)
        except ValueError:
            self._emit("settings", self._hub.settings.public())
            raise
        self._emit("settings", after)
        if after.get("auto_download_updates") and not before.get("auto_download_updates") and self._updater:
            status = self._updater.status()
            if status.get("stage") == "available":
                self._on_update_status(status)
        if "premiere_exe" in patch:
            self._emit("premiere", self._hub.premiere_state())
        return after

    @_api
    def launch_premiere(self, project=None):
        if project is not None and (not isinstance(project, str) or not project.strip()):
            raise BridgeError("Invalid project path.")
        return self._hub.launch_premiere(project.strip() if project else None)

    @_api
    def pick_project(self):
        recent = self._hub.recent_projects(1)
        start = recent[0]["folder"] if recent else ""
        return self._open_dialog(PROJECT_TYPES, start)

    @_api
    def pick_premiere_exe(self):
        current = self._hub.premiere_state().get("exe") or ""
        path = self._open_dialog(EXE_TYPES, os.path.dirname(current) if current else "")
        if not path:
            return None
        return self.set_settings({"premiere_exe": path})

    @_api
    def recent_projects(self):
        return self._hub.recent_projects()

    @_api
    def modules(self):
        return self._hub.modules()

    @_api
    def set_module_enabled(self, id, enabled):  # noqa: A002 - parameter name is part of the JS contract
        _need_id(id)
        return self._hub.registry.set_enabled(id, bool(enabled))

    @_api
    def module_page(self, id):  # noqa: A002
        _need_id(id)
        return self._hub.registry.module_page(id)

    @_api
    def module_action(self, id, action, value=None, row=None):  # noqa: A002
        _need_id(id)
        if not isinstance(action, str) or not action:
            raise BridgeError("Unknown action.")
        if row is not None and not isinstance(row, str):
            row = str(row)
        result = self._hub.registry.handle_action(action, value, row, module_id=id, wait=True)
        return result or {"ok": False, "toast": None}

    @_api
    def module_settings(self, id):  # noqa: A002
        _need_id(id)
        return self._hub.registry.module_settings(id)

    @_api
    def set_module_setting(self, id, key, value, scope="host"):  # noqa: A002
        _need_id(id)
        if scope not in ("host", "project") or not isinstance(key, str):
            raise BridgeError("Unknown setting.")
        return self._hub.registry.set_module_setting(id, key, value, scope)

    @_api
    def panel_status(self):
        return self._hub.panel_status()

    @_api
    def panel_reinstall(self):
        status = self._hub.reinstall_panel()
        self._emit("panel", status)
        return status

    @_api
    def panel_remove(self):
        result = self._hub.remove_panel()
        self._emit("panel", self._hub.panel_status())
        return result

    @_api
    def panel_debug_mode(self, enable):
        result = self._hub.set_debug_mode(bool(enable))
        self._emit("panel", self._hub.panel_status())
        self._emit("settings", self._hub.settings.public())
        return result

    @_api
    def ffmpeg_status(self):
        return self._hub.tools.status()

    @_api
    def ffmpeg_download(self):
        tools = self._hub.tools
        if tools.has("ffmpeg") and tools.has("ffprobe"):
            return tools.status()
        done = threading.Event()
        errors: list = []

        def work(job):
            return tools.ensure("ffmpeg", lambda value, label: job.progress(value, label))

        job = self._hub.jobs.submit(work, title="Download ffmpeg", priority="high",
                                    on_done=lambda j, r: done.set(),
                                    on_error=lambda j, e: (errors.append(e), done.set()))
        done.wait(1800)
        if errors and not isinstance(errors[0], JobCancelled):
            raise BridgeError("ffmpeg could not be downloaded; check the connection and try again.")
        log.info("ffmpeg download job %s finished", job.id)
        return tools.status()

    @_api
    def answer_confirm(self, id, yes):  # noqa: A002
        return self._hub.answer_confirm(str(id), bool(yes))

    @_api
    def open_data_folder(self):
        return bool(winutil.open_path(str(self._hub.paths.home)))

    @_api
    def reveal(self, path):
        if not isinstance(path, str) or not path.strip():
            return False
        return bool(winutil.reveal_in_explorer(path))

    @_api
    def open_path(self, path):
        if not isinstance(path, str) or not path.strip():
            return False
        return bool(winutil.open_path(path))

    @_api
    def open_url(self, url):
        if not isinstance(url, str) or not url.strip():
            return False
        return bool(winutil.open_url(url))

    @_api
    def window_minimize(self):
        self._need_window().minimize()
        return None

    @_api
    def window_close(self):
        self._need_window()
        if self._should_hide():
            self._later(self._hide, 0.05)
        else:
            self._later(self._quit)
        return None

    @_api
    def quit(self):
        self._need_window()
        self._later(self._quit)
        return None

    @_api
    def update_status(self):
        return self._update_status()

    @_api
    def check_updates(self):
        updater = self._need_updater()
        updater.check_background()
        return updater.status()

    @_api
    def download_update(self):
        updater = self._need_updater()
        updater.download_background()
        return updater.status()

    @_api
    def apply_update(self):
        if not self._need_updater().apply():
            return False
        self._quitting = True
        self._later(self._quit, 0.3)  # the apply-mode exe waits for this process to exit
        return True

    @_api
    def log(self, level, message):
        ui_log.log(LOG_LEVELS.get(str(level).lower(), logging.INFO), "ui: %s", str(message)[:MAX_UI_LOG_CHARS])
        return None

    # ================================================================================================================

    def _need_updater(self) -> Any:
        if self._updater is None:
            raise BridgeError("Updates are not available.")
        return self._updater


def _need_id(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise BridgeError("Unknown module.")


# The public names of Bridge must be exactly the contract methods (checked by tests/test_shell_bridge.py).
CONTRACT_METHODS = (
    "ready", "get_state", "get_settings", "set_settings", "launch_premiere", "pick_project", "pick_premiere_exe",
    "recent_projects", "modules", "set_module_enabled", "module_page", "module_action", "module_settings",
    "set_module_setting", "panel_status", "panel_reinstall", "panel_remove", "panel_debug_mode", "ffmpeg_status",
    "ffmpeg_download", "answer_confirm", "open_data_folder", "reveal", "open_path", "open_url", "window_minimize",
    "window_close", "quit", "update_status", "check_updates", "download_update", "apply_update", "log",
)
