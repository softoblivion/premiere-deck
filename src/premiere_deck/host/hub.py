"""The host facade (`Hub`): glue between the server, the Premiere facade, the registry, the panel installer, the
launcher and the window bridge; `HostServices` adapters that become each module's `ModuleContext`.

Threads: the server thread delivers agent and display messages; the hub only posts work to the registry's module
thread from there. The window bridge calls the hub from pywebview threads; methods that touch modules go through
`registry.thread.call(...)`. Window events leave through `emit(name, payload)` (the bridge's event pump).

The Premiere facade (`premiere.api.PremiereApi`) is imported lazily. While it is not
part of the build, `StubPremiere` stands in: modules see `PremiereUnavailable` for every operation and the host
still starts. Facade hooks the hub uses when present: `bind_module_thread(thread, submit)`, `on_event(callback)`
(then the facade derives `project.*` from hello / heartbeat / events), `set_events(names)` (then the facade owns
`subscribe`), `close()`.
"""

from __future__ import annotations

import collections
import itertools
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from premiere_deck import APP_NAME, __version__
from premiere_deck.core.jobs import JobRunner, ModuleJobs
from premiere_deck.core.paths import AppPaths, package_dir, read_json
from premiere_deck.core.settings import HostModuleSettings, Settings
from premiere_deck.core.tools import HostTools
from premiere_deck.modules.base import ModuleContext
from premiere_deck.premiere.model import PremiereLike, PremiereUnavailable, ProjectInfo

from . import launcher as L
from . import protocol as P
from .panel_install import PanelInstaller, extensions_dir
from .registry import MODULES, Registry
from .server import DeckServer

log = logging.getLogger("premiere_deck.host.hub")

FALLBACK_AFTER = 20.0       # seconds without an agent after a host-launched Premiere start (docs/PANEL.md 4.3)
CONFIRM_TIMEOUT = 600.0
ACTIVITY_SIZE = 20
TONE_TO_LEVEL = {"ok": "success", "warn": "warning", "error": "error", "neutral": "info", "accent": "info"}
DECK_DIR = ".premiere-deck"


def project_info_for(path: str) -> ProjectInfo:
    """The rule of docs/PREMIERE_API.md 2 (`project()`): root = the folder of the `.prproj`, or its parent when the
    parent holds a `.premiere-deck` folder and the folder itself does not (Files layout with the project file in
    `Project/`; the Files module creates that folder at Setup)."""
    project = Path(path)
    folder = project.parent
    root = folder
    if (folder.parent / DECK_DIR).is_dir() and not (folder / DECK_DIR).is_dir():
        root = folder.parent
    return ProjectInfo(path=str(project), root=str(root), name=project.stem, deck_dir=str(root / DECK_DIR))


class StubPremiere:
    """Stands in for `PremiereApi` while the facade is not in the build: every operation is unavailable."""

    stub = True

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def is_connected(self) -> bool:
        return False

    def script(self, path: str) -> str:
        return Path(path).read_text("utf-8") if os.path.isfile(path) else ""

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_") or not hasattr(PremiereLike, name):  # no facade hooks (set_events, on_event, ...)
            raise AttributeError(name)

        def unavailable(*_args: Any, **_kw: Any) -> Any:
            raise PremiereUnavailable("The Premiere connection is not part of this build.")

        return unavailable


# --- module services ---------------------------------------------------------------------------------------------


class HostInfoService:
    def __init__(self, hub: "Hub") -> None:
        self._hub = hub
        self.version = __version__
        self.data_dir = str(hub.paths.home)

    def open_path(self, path: str) -> None:
        self._hub.open_path(path)

    def reveal(self, path: str) -> None:
        self._hub.reveal(path)

    def open_url(self, url: str) -> None:
        self._hub.open_url(url)

    def show(self, page: Optional[str] = None) -> None:
        self._hub.show(page)


class UiService:
    def __init__(self, hub: "Hub", module_id: str) -> None:
        self._hub = hub
        self._module_id = module_id

    def invalidate(self) -> None:
        if self._hub.registry is not None:
            self._hub.registry.invalidate()

    def toast(self, tone: str, text: str) -> None:
        self._hub.toast(tone, text)

    def result(self, button_id: str, outcome: str, text: str, detail: Optional[str] = None) -> None:
        prefix = self._module_id + "."
        full_id = button_id if button_id.startswith(prefix) else prefix + button_id
        self._hub.action_result(self._module_id, full_id, outcome, text, detail)

    def confirm(self, question: str) -> bool:
        return self._hub.confirm(question, module_id=self._module_id)


class EventService:
    def __init__(self, hub: "Hub", module_id: str) -> None:
        self._hub = hub
        self._module_id = module_id

    def emit(self, name: str, data: Any = None) -> None:
        self._hub.module_event(self._module_id, name, data)


# --- the hub -----------------------------------------------------------------------------------------------------


class Hub:
    def __init__(self, paths: Optional[AppPaths] = None, settings: Optional[Settings] = None, *, fake: bool = False,
                 modules: Optional[list] = None, ports: Optional[tuple] = None,
                 installer: Optional[PanelInstaller] = None, launch_fn: Callable[..., int] = L.launch,
                 watch: bool = True, fake_display: bool = True) -> None:
        self.paths = paths or AppPaths()
        self.paths.ensure()
        self.settings = settings or Settings(self.paths.settings_file)
        self.fake = fake
        self.version = __version__
        self._modules = list(modules) if modules is not None else list(MODULES)
        self._ports = ports
        self._launch_fn = launch_fn
        self._use_watch = watch
        self._fake_display = fake_display  # --fake: a fake visible panel joins too (host/fake_agent.py)
        self.emit: Callable[[str, Any], None] = lambda name, payload: None
        self.on_open_window: Callable[[Optional[str]], None] = lambda page: None
        if installer is None:
            target = None
            if fake and not os.environ.get("PREMIERE_DECK_CEP_DIR"):
                target = self.paths.home / "fake-cep" / "extensions"  # --fake never writes Adobe's folder
                target = target / "com.softoblivion.premieredeck"
            installer = PanelInstaller(self.paths, self.version, target=target, settings=self.settings)
        if installer.settings is None:
            installer.settings = self.settings  # debug_mode_written lives in the host settings
        self.installer = installer
        self.tools = HostTools(self.paths)
        self.registry: Optional[Registry] = None
        self.jobs: Optional[JobRunner] = None
        self.server: Optional[DeckServer] = None
        self.premiere: Any = None
        self.fake_agent: Any = None
        self.watch: Optional[L.ProcessWatch] = None
        self.install_result: Optional[dict] = None
        self.project_path: Optional[str] = None
        self.premiere_running = False
        self._running_since: Optional[float] = None
        self._launched_at: Optional[float] = None
        self._displays = 0
        self._agent_seen = False  # the agent connected during this Premiere session (no debug-mode offer then)
        self._facade_events = False
        self._activity: collections.deque = collections.deque(maxlen=ACTIVITY_SIZE)
        self._confirms: dict[str, tuple[threading.Event, list]] = {}
        self._confirm_ids = itertools.count(1)
        self._lock = threading.RLock()
        self._started = False
        self._stopped = False

    # -- lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for folder in self.paths.remove_retired_data():
            log.info("removed data of a retired feature: %s", folder)
        self.install_result = self.installer.ensure().as_dict()
        self.server = DeckServer(self.version, ports=self._ports or P.PORT_RANGE, discovery_dir=self._discovery_dir,
                                 host_exe=self.paths.exe_path, update_for=self._update_for, theme=self.theme_name)
        self.server.agent.add_presence_tap(self._on_agent_presence)
        self.server.agent.add_tap(self._on_agent_message)
        self.server.displays.set_handler(self._on_display_message)
        self.server.displays.set_presence_handler(self._on_display_count)
        self.registry = Registry(self._context_for, modules=self._modules, is_enabled=self.settings.module_enabled,
                                 set_enabled=self.settings.set_module_enabled, on_tree=self._send_tree,
                                 on_change=self._on_registry_change, toast=self.toast,
                                 host_status=self.panel_status_line, version=self.version, pins=self.tree_pins)
        self.jobs = JobRunner(dispatch=self.registry.thread.post)
        self.jobs.on_job(self._on_job)
        self.premiere = self._build_facade()
        self.server.start()
        self.registry.load()
        self._drop_unknown_pins()
        self.registry.start()
        self.registry.thread.call(self._send_subscriptions)
        if self._use_watch and not self.fake:
            self.watch = L.ProcessWatch(self._on_premiere_process)
            self.watch.start()
        if self.fake:
            self._start_fake_agent()
        log.info("%s %s host started (port %s, fake=%s)", APP_NAME, self.version, self.server.port, self.fake)

    def stop(self) -> None:
        with self._lock:
            if self._stopped or not self._started:
                return
            self._stopped = True
        for event, answer in list(self._confirms.values()):
            answer.append(False)
            event.set()
        if self.fake_agent is not None:
            self.fake_agent.stop()
        if self.watch is not None:
            self.watch.stop()
        if self.registry is not None:
            self.registry.stop()
        if self.jobs is not None:
            self.jobs.shutdown(cancel=True, timeout=3)
        close = getattr(self.premiere, "close", None)
        if callable(close) and not getattr(self.premiere, "stub", False):
            try:
                close()
            except Exception:  # noqa: BLE001
                log.debug("facade close failed", exc_info=True)
        if self.server is not None:
            self.server.stop()
        self.settings.save()
        log.info("host stopped")

    def _discovery_dir(self) -> Optional[Path]:
        target = self.installer.target
        return target if target.is_dir() else None

    def _update_for(self, hello: P.Hello) -> Optional[dict]:
        if hello.role != P.ROLE_AGENT and hello.role != P.ROLE_DISPLAY:
            return None
        session = self.server.session_id if self.server else ""
        return self.installer.update_for(hello.panel_version, f"{session}:{hello.role}")

    def _start_fake_agent(self) -> None:
        from .fake_agent import FakeAgent

        panel_version = self.installer.installed_version() or "0"
        self.fake_agent = FakeAgent(self.server.port, self.server.token, home=self.paths.home,
                                    panel_version=panel_version, with_display=self._fake_display)
        self.fake_agent.start()

    # -- facade
    def _build_facade(self) -> Any:
        transport = self.server.agent
        try:
            from premiere_deck.premiere.api import PremiereApi
        except ImportError as exc:
            log.warning("Premiere facade not available (%s); using the stand-in", exc)
            return StubPremiere(transport)
        jsx_dir = package_dir() / "premiere" / "jsx"
        try:
            api = PremiereApi(transport, jsx_dir)
        except Exception:  # noqa: BLE001
            log.exception("PremiereApi could not be created; using the stand-in")
            return StubPremiere(transport)
        bind = getattr(api, "bind_module_thread", None)
        if callable(bind):
            bind(self.registry.thread.thread, self.registry.thread.post)
        on_event = getattr(api, "on_event", None)
        if callable(on_event):
            on_event(self._on_premiere_event)  # the facade derives project.* from hello / heartbeat / events
            self._facade_events = True
        return api

    def _send_subscriptions(self, on_connect: bool = False) -> None:
        """The union of the running modules' events. A facade with `set_events` owns `subscribe` (it re-sends on
        every reconnect itself); otherwise the hub sends it on each agent connect."""
        events = self.registry.events() if self.registry else []
        setter = getattr(self.premiere, "set_events", None)
        if callable(setter):
            if not on_connect:
                setter(events)
            return
        if self.server and self.server.agent.connected:
            try:
                self.server.agent.send(P.subscribe(events))
            except (ConnectionError, P.ProtocolError):
                log.debug("subscribe not sent", exc_info=True)

    # -- module contexts
    def _context_for(self, module_id: str, registry: Registry) -> ModuleContext:
        return ModuleContext(
            premiere=self.premiere, jobs=ModuleJobs(self.jobs, module_id),
            settings=HostModuleSettings(self.settings, module_id, self._deck_dir), paths=self.paths, tools=self.tools,
            ui=UiService(self, module_id), host=HostInfoService(self), events=EventService(self, module_id),
            log=logging.getLogger(f"module.{module_id}"), project=registry.project, post=registry.thread.post)

    def _deck_dir(self) -> Optional[str]:
        project = self.registry.project if self.registry else None
        return project.deck_dir if project else None

    # -- agent and display traffic (server thread)
    def _on_agent_presence(self, present: bool, hello: Optional[P.Hello]) -> None:
        if present and hello is not None:
            self._launched_at = None
            self._agent_seen = True
            self.registry.thread.post(lambda: self._send_subscriptions(on_connect=True))
            # The host never opens the visible panel (docs/PROTOCOL.md 3): the user opens it once from
            # Window > Extensions and Premiere keeps it in the workspace.
            self._set_project_path(hello.project_path)
        else:
            self._set_project_path(None)
        self.registry.invalidate()
        self._emit_state()

    def _on_agent_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == P.T_EVENT and not self._facade_events:
            self._on_premiere_event(str(message.get("name") or ""), message.get("data"))
        elif kind == P.T_HEARTBEAT and not self._facade_events:
            self._set_project_path(message.get("project_path") or None)

    def _on_premiere_event(self, name: str, data: Any) -> None:
        if name == "project.opened":
            self._set_project_path((data or {}).get("path") if isinstance(data, dict) else None)
        elif name == "project.closed":
            self._set_project_path(None)
        if name in P.EVENTS:
            self.registry.dispatch_event(name, data)

    def _set_project_path(self, path: Optional[str]) -> None:
        path = str(path) if path else None
        with self._lock:
            if path == self.project_path:
                return
            self.project_path = path
        if path is None:
            self.registry.project_closed()
        else:
            self.registry.thread.post(lambda: self._open_project(path))
        self._emit_state()

    def _open_project(self, path: str) -> None:
        info = None
        if self.premiere is not None and not getattr(self.premiere, "stub", False):
            try:
                candidate = self.premiere.project()
                if candidate is not None and os.path.normcase(candidate.path) == os.path.normcase(path):
                    info = candidate
            except Exception:  # noqa: BLE001 - the local rule is the fallback
                log.debug("facade project() failed; using the local rule", exc_info=True)
        if info is None:
            info = project_info_for(path)
        if self.project_path != path:
            return  # switched again meanwhile
        self.registry._project_opened(info)
        self.settings.add_recent_project(path)
        self._emit_state()

    def _on_display_message(self, message: dict) -> None:
        if message.get("type") != P.T_ACTION:
            return
        action_id = str(message.get("id") or "")
        if action_id in (P.PANEL_PIN, P.PANEL_UNPIN):
            value = message.get("value")
            self.registry.thread.post(lambda: self.pin_action(action_id, value))
            return
        if action_id.startswith("host."):
            self._host_action(action_id)
            return
        self.registry.handle_action(action_id, message.get("value"), message.get("row"))

    def _on_display_count(self, count: int) -> None:
        before, self._displays = self._displays, count
        if before == 0 and count > 0:
            self.registry.dispatch_event("panel.opened", {})
        elif before > 0 and count == 0:
            self.registry.dispatch_event("panel.closed", {})
        self._emit_state()

    def _host_action(self, action_id: str) -> None:
        page = "settings" if action_id == "host.settings" else "deck"  # BRIDGE_API.md 4 `navigate`
        try:
            self.on_open_window(page)
        except Exception:  # noqa: BLE001
            log.exception("opening the window failed")

    # -- pinned row (docs/PROTOCOL.md 6.1 `pins`, docs/BRIDGE_API.md 3.3)
    def tree_pins(self, sections: list) -> dict:
        """The tree's `pins`: the stored ids that are tiles of this tree; a stored pin whose tile is not rendered
        now (a module turned off, a project not set up) stays stored and is left out."""
        tiles = set(P.tile_ids(sections))
        return P.pins([pin for pin in self.settings.pins() if pin in tiles])

    def _drop_unknown_pins(self) -> None:
        """At start: pins of modules this host does not know are dropped from the setting."""
        known = {entry.id for entry in self.registry.entries} if self.registry else set()
        pins = self.settings.pins()
        kept = [pin for pin in pins if pin.split(".", 1)[0] in known]
        if kept != pins:
            log.info("dropped unknown pins: %s", [pin for pin in pins if pin not in kept])
            self.settings.set_pins(kept)

    def pin_action(self, action_id: str, value: Any) -> None:
        """`panel.pin` / `panel.unpin` from a display (module thread): store the change and send the tree again.
        A pin whose id is no tile of the current tree, or that would make a sixth visible pin, is refused with an
        `action_result` for `panel.pins`; a pin to make room for drops the oldest stored pins that are not shown."""
        target = value if isinstance(value, str) else ""
        pins = self.settings.pins()
        if action_id == P.PANEL_UNPIN:
            if target in pins:
                self.settings.set_pins([pin for pin in pins if pin != target])
                self.registry.invalidate()
            return
        if target in pins:
            return
        tree = self.registry.last_tree or self.registry._render()
        tiles = set(P.tile_ids(tree.get("sections", [])))
        if target not in tiles:
            self._pins_result("warn", "This control cannot be pinned")
            return
        pins.append(target)
        hidden = [pin for pin in pins if pin not in tiles]
        while len(pins) > P.PINS_MAX and hidden:
            pins.remove(hidden.pop(0))
        if len(pins) > P.PINS_MAX:
            self._pins_result("warn", "Five pins at most", "Unpin a tile to make room.")
            return
        self.settings.set_pins(pins)
        self.registry.invalidate()

    def _pins_result(self, outcome: str, text: str, detail: Optional[str] = None) -> None:
        """The pinned row's result line: to the displays only (the row exists in the panel only)."""
        if self.server is not None:
            try:
                self.server.displays.send(P.action_result(P.PANEL_PINS, outcome, text, detail))
            except Exception:  # noqa: BLE001
                log.debug("pins result failed", exc_info=True)

    def _send_tree(self, tree: dict) -> None:
        if self.server is not None:
            self.server.displays.send_ui(tree)

    def _on_registry_change(self) -> None:
        """Module thread, after every re-render: refresh the subscription when the set of events changed."""
        events = tuple(self.registry.events())
        if events != getattr(self, "_last_events", None):
            self._last_events = events
            self._send_subscriptions()
        self.emit("modules", [e.as_dict() for e in self.registry.entries])

    # -- Premiere process
    def _on_premiere_process(self, running: bool) -> None:
        self.premiere_running = running
        self._running_since = time.time() if running else None
        if not running:
            self._launched_at = None
        # A new Premiere session (or none); an agent that is already there counts as seen.
        self._agent_seen = bool(running and self.server is not None and self.server.agent.connected)
        self._emit_state()
        if running:
            timer = threading.Timer(FALLBACK_AFTER + 0.5, self._emit_state)
            timer.daemon = True
            timer.start()

    def premiere_state(self) -> dict:
        """`PremiereState` (docs/BRIDGE_API.md 3.2): `connected` only while a display (the visible panel) is
        connected; `starting` in the first 20 s of a start without an agent; `waiting` while Premiere runs without a
        display (`agent_connected` tells whether the headless agent is there)."""
        agent = self.server.agent.hello if self.server else None
        agent_connected = agent is not None
        displays = self._displays
        now = time.time()
        running = self.premiere_running or agent_connected or displays > 0
        launching = self._launched_at is not None and now - self._launched_at < FALLBACK_AFTER
        if displays > 0:
            state = "connected"
        elif not agent_connected and launching:
            state = "starting"
        elif (not agent_connected and running and self._running_since is not None
              and now - self._running_since < FALLBACK_AFTER):
            state = "starting"
        elif running:
            state = "waiting"
        else:
            state = "not_running"
        project = self.registry.project if self.registry else None
        installs = self._installs()
        return {
            "state": state,
            "running": running,
            "agent_connected": agent_connected,
            "premiere_version": agent.premiere_version if agent else None,
            "panel_version": agent.panel_version if agent else None,
            "project": None if project is None else {"path": project.path, "name": project.name,
                                                     "root": project.root},
            "project_path": self.project_path,
            "exe": installs[0].exe if installs else None,
            "installs": [i.as_dict() for i in installs],
            # The debug-mode offer is for a panel that never loaded in this Premiere session, not for an agent that
            # went away (Premiere exiting, a page reload).
            "fallback_offer": (state == "waiting" and not agent_connected and not self._agent_seen
                               and not self.fake and not self.settings.get("debug_mode_written")),
            "displays": displays,
            "fake": self.fake,
        }

    def _installs(self) -> list:
        """Premiere installs, cached for 30 s (registry and folder scan, read only) and per `premiere_exe` value.
        Also in fake mode, where only the launch is simulated."""
        key = self.settings.get("premiere_exe")
        cached = getattr(self, "_installs_cache", None)
        if cached and cached[0] == key and time.monotonic() - cached[1] < 30:
            return cached[2]
        installs = L.find_premiere(key)
        self._installs_cache = (key, time.monotonic(), installs)
        return installs

    def panel_status_line(self) -> dict:
        """The header line of the panel's tree (docs/PROTOCOL.md 6.1)."""
        project = self.registry.project if self.registry else None
        if project is None:
            return P.status("Connected · No project open", "neutral")
        return P.status(f"Connected · {project.name}", "ok")

    def launch_premiere(self, project: Optional[str] = None) -> dict:
        if project and not os.path.isfile(project):
            raise ValueError("The project file was not found.")
        if self.fake:
            self.toast("neutral", "Premiere Pro is simulated in this mode.")
            if project and self.fake_agent is not None:
                self._set_project_path(project)
            return self.premiere_state()
        connected = self.server is not None and self.server.agent.connected
        launched = self._launched_at
        if launched is not None and time.time() - launched < FALLBACK_AFTER and not connected:
            # A second request while the first start is under way (a double click, the tray and the window):
            # Premiere is not started twice and the request is not an error.
            log.info("launch request ignored: Premiere Pro is starting")
            return self.premiere_state()
        if L.is_premiere_running() or connected:
            if not project:
                raise ValueError("Premiere Pro is already running.")
            if not connected:
                raise ValueError("Premiere Pro is running without the panel; open the project there.")
            self.registry.thread.call(lambda: self.premiere.open_project(project), timeout=120)
            self.settings.add_recent_project(project)
            return self.premiere_state()
        install = L.best_premiere(self.settings.get("premiere_exe"))
        if install is None:
            raise ValueError("Premiere Pro was not found. Choose its program file in Settings.")
        self._launch_fn(install.exe, project)
        self._launched_at = time.time()
        self._agent_seen = False
        if project:
            self.settings.add_recent_project(project)
        self._emit_state()
        timer = threading.Timer(FALLBACK_AFTER + 0.5, self._emit_state)
        timer.daemon = True
        timer.start()
        return self.premiere_state()

    def recent_projects(self, limit: int = 10) -> list[dict]:
        """Deck's own list first, then Premiere's MRU; missing files are left out."""
        seen: set[str] = set()
        rows: list[dict] = []
        opened = self.settings.get("recent_opened", {}) or {}
        own = [{"path": p, "opened_at": opened.get(p), "source": "deck"}
               for p in self.settings.get("recent_projects", [])]
        mru = [] if self.fake else [dict(e, source="premiere") for e in L.premiere_recent_projects()]
        if self.fake and self.fake_agent is not None:
            own.insert(0, {"path": self.fake_agent.project["path"], "opened_at": None, "source": "deck"})
        for entry in own + mru:
            path = entry.get("path")
            if not isinstance(path, str) or path.lower() in seen:
                continue
            seen.add(path.lower())
            if not os.path.isfile(path):
                continue
            rows.append({"path": path, "name": Path(path).stem, "folder": str(Path(path).parent),
                         "opened_at": entry.get("opened_at"), "source": entry.get("source")})
            if len(rows) >= limit:
                break
        return rows

    # -- panel install (window Settings)
    def panel_status(self) -> dict:
        status = self.installer.status()
        status["extensions_dir"] = str(extensions_dir())
        return status

    def reinstall_panel(self) -> dict:
        result = self.installer.ensure(force=True)
        self._after_install(result)
        if result.action == "skipped_junction":
            raise ValueError("The panel folder is linked to the source folder; nothing was replaced.")
        return self.panel_status()

    def check_panel_update(self) -> dict:
        """`panel_check_update()` (docs/BRIDGE_API.md 2): the start-up check of docs/PANEL.md 4.2 on demand. The
        installed panel is compared with the bundled one and replaced when they differ (`ensure()`, the same call
        `start()` makes). `installed` is the version found before the call."""
        installed = self.installer.installed_version()
        bundled = self.installer.source_version()
        result = self.installer.ensure()
        self._after_install(result)
        log.info("panel update check: installed %s, bundled %s, %s", installed, bundled, result.action)
        return {"updated": result.changed, "installed": installed, "bundled": bundled}

    def _after_install(self, result: Any) -> None:
        """After `ensure()`: rewrite `deck.json`, ask the connected agent and every connected display to load the
        new files (`reload` when only files changed; a manifest change waits for a Premiere restart,
        `welcome.update`), refresh the state; a failed install raises its sentence."""
        if self.server is not None:
            self.server.write_discovery()
            if result.changed and not result.manifest_changed:
                if self.server.agent.connected:
                    try:
                        self.server.agent.send(P.reload())
                    except ConnectionError:
                        pass
                self.server.displays.reload()  # the visible panel loads the new files as well (docs/PROTOCOL.md 4)
        self._emit_state()
        if result.action == "failed":
            raise ValueError(result.message)

    def remove_panel(self) -> dict:
        result = self.installer.uninstall()
        self._emit_state()
        return result

    def set_debug_mode(self, enabled: bool) -> dict:
        if enabled:
            self.installer.enable_debug_mode()
        else:
            self.installer.disable_debug_mode()
        self._emit_state()
        return self.installer.debug_mode_state()

    # -- modules (window)
    def modules(self) -> list[dict]:
        return self.registry.modules() if self.registry else []

    def module_event(self, module_id: str, name: str, data: Any) -> None:
        text = None
        tone = "neutral"
        if isinstance(data, dict):
            text = data.get("text") or data.get("message")
            tone = data.get("tone") or tone
        if text:
            self._add_activity(module_id, str(text), tone)
        self.emit("module_event", {"module": module_id, "name": name, "data": data})

    def _on_job(self, job: dict) -> None:
        self.emit("job", job)
        state = job.get("state")
        if state == "done":
            self._add_activity(job.get("module"), f"{job.get('title')} finished", "ok")
        elif state == "failed":
            self._add_activity(job.get("module"), f"{job.get('title')} failed", "error")

    def _add_activity(self, module_id: Optional[str], text: str, tone: str = "neutral") -> None:
        line = {"at": time.time(), "module": module_id, "text": text, "tone": tone}
        self._activity.appendleft(line)
        self.emit("activity", line)

    def activity(self) -> list[dict]:
        return list(self._activity)

    # -- theme (docs/PROTOCOL.md 6.5)
    def theme_name(self) -> str:
        """The `theme` setting as the panel gets it: `black` or `sky`."""
        name = self.settings.get("theme")
        return name if name in P.THEMES else P.DEFAULT_THEME

    def notify_theme(self) -> None:
        """Sends the current theme to every connected display. The window calls it after a theme change; displays
        that connect later get it right after `welcome`."""
        if self.server is not None:
            try:
                self.server.displays.theme(self.theme_name())
            except Exception:  # noqa: BLE001
                log.debug("panel theme failed", exc_info=True)

    # -- action results (docs/PROTOCOL.md 6.6)
    def action_result(self, module_id: str, button_id: str, outcome: str, text: str,
                      detail: Optional[str] = None) -> None:
        """`ctx.ui.result`: `action_result` to every display, the `result` event to the window, a line in the
        activity feed (`tone` = the outcome). Callable from any thread."""
        if outcome not in P.OUTCOMES:
            log.warning("result of %s with unknown outcome %r ignored", button_id, outcome)
            return
        message = P.action_result(button_id, outcome, text, detail)
        if self.server is not None:
            try:
                self.server.displays.send(message)
            except Exception:  # noqa: BLE001
                log.debug("panel result failed", exc_info=True)
        self.emit("result", {"module": module_id, "id": message["id"], "outcome": outcome, "text": message["text"],
                             "detail": message["detail"], "at": message["at"]})
        self._add_activity(module_id, message["text"], outcome)

    # -- toasts and confirmations
    def toast(self, tone: str, text: str) -> None:
        tone = tone if tone in P.TONES else "neutral"
        if self.server is not None:
            try:
                self.server.displays.toast(tone, text)
            except Exception:  # noqa: BLE001
                log.debug("panel toast failed", exc_info=True)
        self.emit("toast", {"kind": TONE_TO_LEVEL.get(tone, "info"), "message": text})

    def confirm(self, question: str, module_id: Optional[str] = None) -> bool:
        """Blocking yes / no in the host window; job threads only (docs/MODULES.md 3)."""
        if self.registry is not None and self.registry.thread.on_thread():
            log.warning("confirm() called on the module thread by %s; answered no", module_id)
            return False
        confirm_id = f"q{next(self._confirm_ids)}"
        event = threading.Event()
        answer: list = []
        self._confirms[confirm_id] = (event, answer)
        try:
            self.emit("confirm", {"id": confirm_id, "question": str(question), "module": module_id})
            try:
                self.on_open_window(None)
            except Exception:  # noqa: BLE001
                pass
            event.wait(CONFIRM_TIMEOUT)
            return bool(answer and answer[0])
        finally:
            self._confirms.pop(confirm_id, None)

    def answer_confirm(self, confirm_id: str, yes: bool) -> bool:
        entry = self._confirms.get(str(confirm_id))
        if entry is None:
            return False
        entry[1].append(bool(yes))
        entry[0].set()
        return True

    # -- shell helpers
    def show(self, page: Optional[str] = None) -> None:
        """`ctx.host.show(page)`: the host window to the front, on `page` (a module id) when given."""
        try:
            self.on_open_window(page)
        except Exception:  # noqa: BLE001
            log.exception("opening the window failed")

    def open_path(self, path: str) -> bool:
        from premiere_deck.shell import winutil

        return winutil.open_path(path)

    def reveal(self, path: str) -> bool:
        from premiere_deck.shell import winutil

        return winutil.reveal_in_explorer(path)

    def open_url(self, url: str) -> bool:
        from premiere_deck.shell import winutil

        return winutil.open_url(url)

    # -- window state
    def state(self) -> dict:
        return {
            "version": self.version,
            "fake": self.fake,
            "data_dir": str(self.paths.home),
            "premiere": self.premiere_state(),
            "panel": self.panel_status(),
            "install": self.install_result,
            "modules": self.modules(),
            "recent": self.recent_projects(),
            "activity": self.activity(),
            "jobs": self.jobs.active() if self.jobs else [],
            "ffmpeg": self.tools.status(),
            "settings": self.settings.public(),
            "server": self.server.state() if self.server else None,
        }

    def _emit_state(self) -> None:
        try:
            state = self.premiere_state()
            key = (state["state"], state["agent_connected"], state["displays"], state["fallback_offer"])
            if key != getattr(self, "_logged_state", None):
                self._logged_state = key
                log.info("premiere state %s (agent %s, displays %s%s)", state["state"],
                         "connected" if state["agent_connected"] else "absent", state["displays"],
                         ", debug-mode offer" if state["fallback_offer"] else "")
            self.emit("premiere", state)
        except Exception:  # noqa: BLE001
            log.debug("state event failed", exc_info=True)

    def host_settings_file(self) -> dict:
        return read_json(self.paths.settings_file, {}) or {}
