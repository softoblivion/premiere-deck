"""Module registry (docs/MODULES.md 2, 8): discovery, lifecycle on one module thread, routing, UI tree.

- `MODULES` lists the module packages; each is imported lazily as `premiere_deck.modules.<pkg>.module` and must
  define exactly one `Module` subclass. A package that is missing or fails to import is recorded (state
  `missing` / `import_failed`) and shown in the host window; it is never fatal.
- Every hook runs on `ModuleThread` ("module-thread"), in order. Exceptions are caught per module, logged under
  `module.<id>` and shown as the toast "<Title>: <sentence>"; a module that raises in `start()` is `failed`.
- Actions are routed by the id prefix (`files.collect` -> module `files`, hook argument `collect`); window-only ids
  arrive as `host:<local id>` and are passed on unchanged. Events go to the modules whose `events` name them.
- `invalidate()` coalesces re-renders: one tree is built at most every `RENDER_DELAY` seconds and handed to
  `on_tree` (the hub sends it to every display); `on_change` tells the window that module pages may have changed.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from premiere_deck import APP_NAME
from premiere_deck.core.settings import coerce_setting
from premiere_deck.modules.base import ActionResult, Module, ModuleContext, SettingField
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable, ProjectInfo

from . import protocol as P

log = logging.getLogger("premiere_deck.host.registry")

MODULES = ["files", "quick_actions"]
RENDER_DELAY = 0.05
CALL_TIMEOUT = 60.0

# Module states shown in the host window (docs/BRIDGE_API.md 3.4).
MISSING, IMPORT_FAILED, DISABLED, RUNNING, FAILED, STOPPED = (
    "missing", "import_failed", "disabled", "running", "failed", "stopped")


class ModuleThread:
    """One thread that runs posted callables in order. `call()` waits for the result (inline on the thread)."""

    def __init__(self, name: str = "module-thread") -> None:
        self._queue: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._stopped = False
        self._thread.start()

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def on_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def post(self, fn: Callable[[], None]) -> None:
        if not self._stopped:
            self._queue.put(fn)

    def call(self, fn: Callable[[], Any], timeout: Optional[float] = CALL_TIMEOUT) -> Any:
        if self.on_thread():
            return fn()
        if self._stopped:
            raise RuntimeError("the module thread is stopped")
        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001
                future.set_exception(exc)

        self._queue.put(run)
        return future.result(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._queue.put(None)
        if not self.on_thread():
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            fn = self._queue.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:  # noqa: BLE001 - posted work must never stop the thread
                log.exception("module thread task failed")


@dataclass
class ModuleEntry:
    package: str
    id: str
    title: str
    description: str = ""
    order: int = 100
    state: str = MISSING
    error: Optional[str] = None
    instance: Optional[Module] = None
    cls: Optional[type] = None
    enabled: bool = True

    def as_dict(self) -> dict:
        module = self.instance
        status = None
        if module is not None and self.state == RUNNING:
            try:
                status = module.section_status()
            except Exception:  # noqa: BLE001
                status = None
        return {
            "id": self.id, "package": self.package, "title": self.title, "description": self.description,
            "order": self.order, "state": self.state, "enabled": self.enabled, "error": self.error,
            "status": status, "requires": list(getattr(self.cls, "requires", ()) or ()),
            "has_page": bool(module is not None and type(module).render_host is not Module.render_host),
            "has_settings": bool(module is not None and type(module).settings_schema is not Module.settings_schema),
        }


def friendly(exc: BaseException) -> str:
    """One sentence for a toast; never a raw ExtendScript error (docs/PROTOCOL.md 9)."""
    if isinstance(exc, PremiereUnavailable):
        return "Premiere Pro is not connected."
    if isinstance(exc, PremiereError):
        if (exc.message or "").strip().lower() == "timeout":
            return "Premiere is waiting for a dialog."
        return "Premiere Pro could not complete the operation."
    text = " ".join(str(exc).split())
    if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError)) and text:
        return text if text.endswith(".") else text + "."
    if isinstance(exc, PermissionError):
        return "No access to the file or folder."
    return "The operation failed; details are in the log."


def find_module_class(module_obj: Any) -> type:
    """The single `Module` subclass defined in (or imported into) a `module.py`."""
    classes = [obj for _, obj in inspect.getmembers(module_obj, inspect.isclass)
               if issubclass(obj, Module) and obj is not Module and obj.__module__ == module_obj.__name__]
    if not classes:
        classes = [obj for _, obj in inspect.getmembers(module_obj, inspect.isclass)
                   if issubclass(obj, Module) and obj is not Module]
    if len(classes) != 1:
        raise ImportError(f"{module_obj.__name__} defines {len(classes)} Module subclasses; exactly one is needed")
    return classes[0]


ContextFactory = Callable[[str, "Registry"], ModuleContext]


class Registry:
    def __init__(self, context_factory: ContextFactory, *, modules: Sequence[Any] = tuple(MODULES),
                 thread: Optional[ModuleThread] = None, is_enabled: Optional[Callable[[str, bool], bool]] = None,
                 set_enabled: Optional[Callable[[str, bool], None]] = None,
                 on_tree: Optional[Callable[[dict], None]] = None, on_change: Optional[Callable[[], None]] = None,
                 toast: Optional[Callable[[str, str], None]] = None,
                 host_status: Optional[Callable[[], Optional[dict]]] = None, version: str = "0.0.0",
                 pins: Optional[Callable[[list], Optional[dict]]] = None) -> None:
        self._factory = context_factory
        self._pins = pins  # the tree's `pins` from its sections (docs/PROTOCOL.md 6.1); no key when None
        self._specs = list(modules)
        self.thread = thread or ModuleThread()
        self._is_enabled = is_enabled or (lambda module_id, default: default)
        self._store_enabled = set_enabled
        self.on_tree = on_tree
        self.on_change = on_change
        self._toast = toast
        self._host_status = host_status
        self.version = version
        self.entries: list[ModuleEntry] = []
        self.project: Optional[ProjectInfo] = None
        self.revision = 0
        self.last_tree: Optional[dict] = None
        self._render_lock = threading.Lock()
        self._render_pending = False
        self._started = False

    # -- discovery (module thread)
    def load(self) -> list[ModuleEntry]:
        return self.thread.call(self._load)

    def _load(self) -> list[ModuleEntry]:
        entries = []
        for spec in self._specs:
            entries.append(self._load_one(spec))
        self.entries = sorted(entries, key=lambda e: (e.order, e.id))
        return self.entries

    def _load_one(self, spec: Any) -> ModuleEntry:
        if isinstance(spec, type):
            package, cls, error = spec.__module__, spec, None
        else:
            package = str(spec)
            cls, error = None, None
            try:
                module_obj = importlib.import_module(f"premiere_deck.modules.{package}.module")
                cls = find_module_class(module_obj)
            except ModuleNotFoundError as exc:
                missing = exc.name or ""
                if missing.startswith(f"premiere_deck.modules.{package}"):
                    log.info("module package %s is not installed", package)
                    return ModuleEntry(package, package, package.replace("_", " ").capitalize(), state=MISSING,
                                       error="Not installed in this build.", enabled=False)
                error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - a broken module never stops the host
                error = f"{type(exc).__name__}: {exc}"
            if cls is None:
                log.error("module package %s failed to import: %s", package, error)
                return ModuleEntry(package, package, package.replace("_", " ").capitalize(), state=IMPORT_FAILED,
                                   error=error, enabled=False)
        entry = ModuleEntry(package, cls.id or package, cls.title or package, cls.description, int(cls.order),
                            cls=cls)
        entry.enabled = bool(self._is_enabled(entry.id, bool(cls.default_enabled)))
        try:
            entry.instance = cls(self._factory(entry.id, self))
            entry.state = DISABLED if not entry.enabled else STOPPED
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(f"module.{entry.id}").exception("module could not be created")
            entry.state, entry.error = FAILED, friendly(exc)
        return entry

    def entry(self, module_id: str) -> Optional[ModuleEntry]:
        for entry in self.entries:
            if entry.id == module_id:
                return entry
        return None

    def running(self) -> list[ModuleEntry]:
        return [e for e in self.entries if e.state == RUNNING and e.instance is not None]

    def modules(self) -> list[dict]:
        return self.thread.call(lambda: [e.as_dict() for e in self.entries])

    # -- guarded hook calls (module thread)
    def _guard(self, entry: ModuleEntry, hook: str, fn: Callable[[], Any], *, toast: bool = True) -> tuple[bool, Any]:
        try:
            return True, fn()
        except Exception as exc:  # noqa: BLE001 - isolation per module (docs/MODULES.md 2)
            logging.getLogger(f"module.{entry.id}").exception("%s failed", hook)
            if toast:
                self.toast("error", f"{entry.title}: {friendly(exc)}")
            return False, exc

    # -- lifecycle
    def start(self) -> None:
        self.thread.call(self._start_all)

    def _start_all(self) -> None:
        self._started = True
        for entry in self.entries:
            if entry.enabled and entry.state == STOPPED:
                self._start_one(entry)
        self.invalidate()

    def _start_one(self, entry: ModuleEntry) -> None:
        module = entry.instance
        if module is None:
            return
        ok, result = self._guard(entry, "start", module.start, toast=False)
        if not ok:
            entry.state, entry.error = FAILED, friendly(result)
            self.toast("error", f"{entry.title}: {entry.error}")
            return
        entry.state, entry.error = RUNNING, None
        module._started = True
        if self.project is not None:
            module.ctx.project = self.project
            self._guard(entry, "project_opened", lambda: module.project_opened(self.project))

    def _stop_one(self, entry: ModuleEntry, state: str) -> None:
        module = entry.instance
        if module is not None and entry.state == RUNNING:
            self._guard(entry, "stop", module.stop, toast=False)
            module._started = False
            cancel_all = getattr(module.ctx.jobs, "cancel_all", None)
            if callable(cancel_all):
                cancel_all()
        entry.state = state

    def stop(self) -> None:
        try:
            self.thread.call(self._stop_all, timeout=15)
        except Exception:  # noqa: BLE001
            log.exception("registry stop failed")
        self.thread.stop()

    def _stop_all(self) -> None:
        for entry in reversed(self.entries):
            if entry.state == RUNNING:
                self._stop_one(entry, STOPPED)
        self._started = False

    def set_enabled(self, module_id: str, enabled: bool) -> dict:
        def run() -> dict:
            entry = self.entry(module_id)
            if entry is None:
                raise ValueError("Unknown module.")
            if entry.state in (MISSING, IMPORT_FAILED):
                raise ValueError("This module is not available in this build.")
            entry.enabled = bool(enabled)
            if self._store_enabled is not None:
                self._store_enabled(entry.id, entry.enabled)
            if enabled and entry.state in (DISABLED, STOPPED, FAILED):
                entry.state = STOPPED
                if self._started:
                    self._start_one(entry)
            elif not enabled:
                self._stop_one(entry, DISABLED)
                entry.state = DISABLED
            self.invalidate()
            return entry.as_dict()

        return self.thread.call(run)

    # -- project
    def project_opened(self, project: ProjectInfo) -> None:
        self.thread.post(lambda: self._project_opened(project))

    def _project_opened(self, project: ProjectInfo) -> None:
        if self.project is not None and self.project.path == project.path:
            return
        if self.project is not None:
            self._project_closed()
        self.project = project
        for entry in self.entries:
            if entry.instance is not None:
                entry.instance.ctx.project = project
        for entry in self.running():
            module = entry.instance
            self._guard(entry, "project_opened", lambda m=module: m.project_opened(project))
        self.invalidate()

    def project_closed(self) -> None:
        self.thread.post(self._project_closed)

    def _project_closed(self) -> None:
        if self.project is None:
            return
        self.project = None
        for entry in self.running():
            module = entry.instance
            self._guard(entry, "project_closed", module.project_closed)
            cancel_all = getattr(module.ctx.jobs, "cancel_all", None)
            if callable(cancel_all):
                cancel_all()
        for entry in self.entries:
            if entry.instance is not None:
                entry.instance.ctx.project = None
        self.invalidate()

    # -- events and actions
    def dispatch_event(self, name: str, data: Any = None) -> None:
        self.thread.post(lambda: self._dispatch_event(name, data))

    def _dispatch_event(self, name: str, data: Any) -> None:
        for entry in self.running():
            module = entry.instance
            if name in (module.events or ()):
                self._guard(entry, f"on_event({name})", lambda m=module: m.on_event(name, data))
        self.invalidate()

    def events(self) -> list[str]:
        """The union of the running modules' events that the agent can deliver (for `subscribe`)."""
        names: set[str] = set()
        for entry in self.entries:
            if entry.state == RUNNING and entry.instance is not None:
                names.update(n for n in (entry.instance.events or ()) if n in P.EVENTS)
        return sorted(names)

    @staticmethod
    def module_id_of(action_id: str) -> str:
        return str(action_id).split(".", 1)[0]

    def handle_action(self, action_id: str, value: Any = None, row: Optional[str] = None,
                      module_id: Optional[str] = None, wait: bool = False) -> Optional[dict]:
        """Route an action. From the panel (`wait=False`) it is posted; the window waits for the result dict
        `{ok, toast}` (docs/BRIDGE_API.md 2)."""
        if wait:
            return self.thread.call(lambda: self._handle_action(action_id, value, row, module_id))
        self.thread.post(lambda: self._handle_action(action_id, value, row, module_id))
        return None

    def _handle_action(self, action_id: str, value: Any, row: Optional[str], module_id: Optional[str]) -> dict:
        target = module_id or self.module_id_of(action_id)
        entry = self.entry(target)
        if entry is None or entry.instance is None:
            log.info("action %s: no module %r", action_id, target)
            return {"ok": False, "toast": None}
        if entry.state != RUNNING:
            self.toast("warn", f"{entry.title} is turned off.")
            return {"ok": False, "toast": ["warn", f"{entry.title} is turned off."]}
        module = entry.instance
        local = module.strip_prefix(str(action_id))
        ok, result = self._guard(entry, f"on_action({local})", lambda: module.on_action(local, value, row))
        toast = None
        if ok and isinstance(result, ActionResult):
            if result.toast:
                tone, text = result.toast
                toast = [tone if tone in P.TONES else "neutral", str(text)]
                self.toast(*toast)
            if result.rerender:
                self.invalidate()
        else:
            self.invalidate()
        return {"ok": ok, "toast": toast}

    # -- host window pages and settings
    def module_page(self, module_id: str) -> dict:
        def run() -> dict:
            entry = self.entry(module_id)
            if entry is None:
                raise ValueError("Unknown module.")
            page = None
            if entry.state == RUNNING and entry.instance is not None:
                ok, result = self._guard(entry, "render_host", entry.instance.render_host, toast=False)
                page = result if ok and isinstance(result, dict) else None
                if not ok:
                    entry.error = friendly(result)
            return {"module": entry.as_dict(), "page": page}

        return self.thread.call(run)

    def module_settings(self, module_id: str) -> dict:
        def run() -> dict:
            entry = self.entry(module_id)
            if entry is None:
                raise ValueError("Unknown module.")
            fields = []
            module = entry.instance
            if module is not None:
                ok, schema = self._guard(entry, "settings_schema", module.settings_schema, toast=False)
                for f in (schema if ok and isinstance(schema, list) else []):
                    if not isinstance(f, SettingField):
                        continue
                    store = module.ctx.settings.project if f.scope == "project" else module.ctx.settings
                    available = f.scope != "project" or self.project is not None
                    try:
                        value = store.get(f.key, f.default) if available else f.default
                    except Exception:  # noqa: BLE001
                        value = f.default
                    if f.kind == "text" and isinstance(value, (list, tuple)):
                        value = ", ".join(str(v) for v in value)
                    fields.append({"key": f.key, "label": f.label, "kind": f.kind, "default": f.default,
                                   "options": f.options, "hint": f.hint, "scope": f.scope, "value": value,
                                   "available": available})
            return {"module": entry.as_dict(), "fields": fields, "project": self.project.name if self.project else None}

        return self.thread.call(run)

    def set_module_setting(self, module_id: str, key: str, value: Any, scope: str = "host") -> Any:
        def run() -> Any:
            entry = self.entry(module_id)
            if entry is None or entry.instance is None:
                raise ValueError("Unknown module.")
            module = entry.instance
            fields = {f.key: f for f in (module.settings_schema() or []) if isinstance(f, SettingField)}
            field = fields.get(key)
            if field is None or field.scope != scope:
                raise ValueError("Unknown setting.")
            stored = coerce_setting(field.kind, value, field.options)
            if scope == "project":
                if self.project is None:
                    raise ValueError("Open a project in Premiere Pro first.")
                module.ctx.settings.project.set(key, stored)
            else:
                module.ctx.settings.set(key, stored)
            self._guard(entry, f"on_setting_changed({key})", lambda: module.on_setting_changed(key, scope, stored))
            self.invalidate()
            return stored

        return self.thread.call(run)

    # -- UI tree
    def toast(self, tone: str, text: str) -> None:
        if self._toast is not None:
            try:
                self._toast(tone if tone in P.TONES else "neutral", text)
            except Exception:  # noqa: BLE001
                log.exception("toast failed")

    def invalidate(self) -> None:
        """Re-render soon; repeated calls within RENDER_DELAY produce one tree."""
        with self._render_lock:
            if self._render_pending:
                return
            self._render_pending = True
        timer = threading.Timer(RENDER_DELAY, lambda: self.thread.post(self._render))
        timer.daemon = True
        timer.start()

    def render_now(self) -> dict:
        return self.thread.call(self._render)

    def _render(self) -> dict:
        with self._render_lock:
            self._render_pending = False
        tree = self.build_tree()
        self.last_tree = tree
        if self.on_tree is not None:
            try:
                self.on_tree(tree)
            except Exception:  # noqa: BLE001
                log.exception("sending the tree failed")
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("change listener failed")
        return tree

    def build_tree(self) -> dict:
        sections = []
        for entry in sorted(self.entries, key=lambda e: (e.order, e.id)):
            if entry.state == RUNNING and entry.instance is not None:
                ok, section = self._guard(entry, "render", entry.instance.section, toast=False)
                if ok:
                    sections.append(section)
                    continue
                sections.append(P.section(entry.id, entry.title, [P.node_text(friendly(section), tone="error", small=True)],
                                          status=P.status("Error", "error")))
            elif entry.state == FAILED:
                sections.append(P.section(entry.id, entry.title,
                                          [P.node_text(entry.error or "Not available.", tone="error", small=True)],
                                          status=P.status("Not available", "error"), collapsed=True))
        status = None
        if self._host_status is not None:
            try:
                status = self._host_status()
            except Exception:  # noqa: BLE001
                log.exception("host status failed")
        footer = {"text": f"{APP_NAME} {self.version}", "link": {"label": "Open host", "action": "host.open"}}
        self.revision += 1
        try:
            return P.ui(self.revision, APP_NAME, status, sections, footer, self._tree_pins(sections))
        except P.ProtocolError:
            log.exception("invalid tree; sending sections one by one")
            valid = []
            for sec in sections:
                try:
                    P.ui(self.revision, APP_NAME, None, valid + [sec], None)
                    valid.append(sec)
                except P.ProtocolError:
                    log.error("section %s dropped from the tree", sec.get("id"))
            return P.ui(self.revision, APP_NAME, status, valid, footer, self._tree_pins(valid))

    def _tree_pins(self, sections: list) -> Optional[dict]:
        if self._pins is None:
            return None
        try:
            return self._pins(sections)
        except Exception:  # noqa: BLE001 - a broken pin list never costs the tree
            log.exception("pins failed")
            return None

    def wait_rendered(self, timeout: float = 2.0) -> Optional[dict]:
        """Tests: wait until a pending render ran; returns the last tree."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._render_lock:
                pending = self._render_pending
            if not pending:
                self.thread.call(lambda: None)
                with self._render_lock:
                    if not self._render_pending:
                        return self.last_tree
            time.sleep(0.01)
        return self.last_tree
