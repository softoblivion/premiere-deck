"""WebSocket server of the host (docs/PROTOCOL.md 1-3, 6): transport, handshake, roles, discovery file.

`DeckServer` runs `websockets` (asyncio) in one background thread ("deck-server"). It owns sockets, the token,
handshakes, the agent replacement rule, paging of `script.result` and the discovery file `deck.json`. It knows no
script and no module:

- `server.agent` is the `AgentTransport` (host/protocol.py) the facade `PremiereApi` is built on. Its handler gets
  every agent message after the handshake (paged results already joined); `add_tap()` lets the hub watch the same
  stream (events, heartbeat) without taking the facade's place.
- `server.displays` is the `DisplayPort`: broadcasts `ui` / `toast` to every display (a display that connects later
  gets the last `ui` at once) and delivers `action` and `display.ready` to one handler.

Every callback runs on the server thread; receivers must return fast (the hub posts to the module thread).
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from premiere_deck import PROTOCOL_VERSION
from premiere_deck.core.paths import atomic_write_json

from . import protocol as P

log = logging.getLogger("premiere_deck.host.server")
agent_log = logging.getLogger("panel.agent")
display_log = logging.getLogger("panel.display")

HELLO_TIMEOUT = 10.0
PING_INTERVAL = 15.0
PING_TIMEOUT = 30.0  # two missed pongs
CLOSE_REPLACED = 4003
DISCOVERY_NAME = "deck.json"
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}

UpdateDecider = Callable[[P.Hello], Optional[dict]]


def is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, (tuple, list)) and address else address
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return str(host) == "localhost"


def parse_query(path: str) -> dict[str, str]:
    query = parse_qs(urlsplit(path or "").query)
    return {key: values[0] for key, values in query.items() if values}


def _log_message(direction: str, role: str, message: dict) -> None:
    if not log.isEnabledFor(logging.DEBUG):
        return
    shown = {k: v for k, v in message.items() if k not in ("code", "chunk")}
    text = json.dumps(shown, ensure_ascii=False)
    log.debug("%s %s %s", direction, role, text[:500])


class _Client:
    _ids = itertools.count(1)

    def __init__(self, ws: Any, hello: P.Hello) -> None:
        self.id = f"c{next(self._ids)}"
        self.ws = ws
        self.hello = hello
        self.role = hello.role
        self.connected_at = time.time()
        self.pages: dict[str, P.ScriptResultPages] = {}


class AgentPort:
    """`AgentTransport` for `PremiereApi` (host/protocol.py). One agent at a time."""

    def __init__(self, server: "DeckServer") -> None:
        self._server = server
        self._handler: Optional[Callable[[dict], None]] = None
        self._presence: Optional[Callable[[bool, Optional[P.Hello]], None]] = None
        self._taps: list[Callable[[dict], None]] = []
        self._presence_taps: list[Callable[[bool, Optional[P.Hello]], None]] = []

    @property
    def connected(self) -> bool:
        return self._server._agent is not None

    @property
    def hello(self) -> Optional[P.Hello]:
        agent = self._server._agent
        return agent.hello if agent else None

    def send(self, message: dict) -> None:
        """Thread-safe; raises ConnectionError when no agent is connected."""
        self._server._send_to_agent(message)

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        self._handler = handler

    def set_presence_handler(self, handler: Callable[[bool, Optional[P.Hello]], None]) -> None:
        self._presence = handler

    def add_tap(self, fn: Callable[[dict], None]) -> None:
        """A second listener of agent messages (the hub: events, heartbeat)."""
        self._taps.append(fn)

    def add_presence_tap(self, fn: Callable[[bool, Optional[P.Hello]], None]) -> None:
        self._presence_taps.append(fn)

    def _deliver(self, message: dict) -> None:
        for fn in ([self._handler] if self._handler else []) + list(self._taps):
            try:
                fn(message)
            except Exception:  # noqa: BLE001
                log.exception("agent message handler failed")

    def _presence_changed(self, present: bool, hello: Optional[P.Hello]) -> None:
        for fn in ([self._presence] if self._presence else []) + list(self._presence_taps):
            try:
                fn(present, hello)
            except Exception:  # noqa: BLE001
                log.exception("agent presence handler failed")


class DisplayPort:
    """Every connected display gets the same `ui` tree and toasts (docs/PROTOCOL.md 6)."""

    def __init__(self, server: "DeckServer") -> None:
        self._server = server
        self._handler: Optional[Callable[[dict], None]] = None
        self._presence: Optional[Callable[[int], None]] = None
        self.last_ui: Optional[dict] = None

    @property
    def count(self) -> int:
        return len(self._server._displays)

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        """Receives `action` and `display.ready` messages."""
        self._handler = handler

    def set_presence_handler(self, handler: Callable[[int], None]) -> None:
        """Receives the number of connected displays after every connect / disconnect."""
        self._presence = handler

    def send_ui(self, message: dict) -> None:
        if message.get("type") != P.T_UI:
            raise P.ProtocolError("send_ui needs a ui message")
        self.last_ui = message
        self._server._broadcast(message)

    def toast(self, tone: str, text: str, ms: int = 3500) -> None:
        self._server._broadcast(P.toast(tone, text, ms))

    def _deliver(self, message: dict) -> None:
        if self._handler is not None:
            try:
                self._handler(message)
            except Exception:  # noqa: BLE001
                log.exception("display message handler failed")

    def _count_changed(self) -> None:
        if self._presence is not None:
            try:
                self._presence(self.count)
            except Exception:  # noqa: BLE001
                log.exception("display presence handler failed")


class DeckServer:
    def __init__(self, host_version: str, *, ports: tuple[int, ...] = P.PORT_RANGE, host: str = "127.0.0.1",
                 discovery_dir: Optional[Callable[[], Optional[Path]]] = None, host_exe: Optional[str] = None,
                 autostart_host: bool = True, update_for: Optional[UpdateDecider] = None,
                 token: Optional[str] = None) -> None:
        self.host_version = host_version
        self.ports = tuple(ports)
        self.host = host
        self.token = token or P.new_token()
        self.session_id = "s-" + secrets.token_hex(2)
        self.port: Optional[int] = None
        self.host_exe = host_exe
        self.autostart_host = autostart_host
        self._discovery_dir = discovery_dir
        self._update_for = update_for
        self.agent = AgentPort(self)
        self.displays = DisplayPort(self)
        self._agent: Optional[_Client] = None
        self._displays: dict[str, _Client] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._stop_event: Optional[asyncio.Event] = None
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._discovery_written: Optional[Path] = None

    # -- lifecycle (any thread)
    def start(self, timeout: float = 10.0) -> int:
        """Start the server thread, bind the first free port of `ports`, write the discovery file. Returns the port."""
        if self._thread is not None:
            return self.port or 0
        self._thread = threading.Thread(target=self._run, name="deck-server", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("the WebSocket server did not start")
        if self._start_error is not None:
            raise self._start_error
        self.write_discovery()
        return self.port or 0

    def stop(self, timeout: float = 5.0) -> None:
        self.remove_discovery()
        loop = self._loop
        if loop is not None and self._stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.port is not None

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the server thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(fn)

    # -- discovery file (docs/PROTOCOL.md 2)
    def discovery(self) -> dict:
        return {
            "port": self.port,
            "token": self.token,
            "host_version": self.host_version,
            "protocol": PROTOCOL_VERSION,
            "host_exe": self.host_exe,
            "autostart_host": bool(self.autostart_host and self.host_exe),
            "written_at": int(time.time()),
        }

    def write_discovery(self) -> Optional[Path]:
        folder = self._discovery_dir() if self._discovery_dir else None
        if folder is None or self.port is None:
            return None
        folder = Path(folder)
        if not folder.is_dir():
            log.info("discovery folder %s is missing; deck.json not written", folder)
            return None
        path = folder / DISCOVERY_NAME
        try:
            atomic_write_json(path, self.discovery())
        except OSError:
            log.exception("could not write %s", path)
            return None
        self._discovery_written = path
        log.info("discovery file %s (port %s)", path, self.port)
        return path

    def remove_discovery(self) -> None:
        path = self._discovery_written
        self._discovery_written = None
        if path is None:
            return
        try:
            data = json.loads(path.read_text("utf-8"))
            if data.get("token") != self.token:
                return  # another host run owns the file now
            os.remove(path)
        except (OSError, ValueError):
            pass

    def state(self) -> dict:
        agent = self._agent
        return {
            "port": self.port,
            "agent": None if agent is None else {
                "panel_version": agent.hello.panel_version, "premiere_version": agent.hello.premiere_version,
                "cep_version": agent.hello.cep_version, "project_path": agent.hello.project_path,
                "connected_at": agent.connected_at},
            "displays": len(self._displays),
        }

    # -- server thread
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001
            if not self._started.is_set():
                self._start_error = exc
                self._started.set()
            else:
                log.exception("server loop failed")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self._loop = None

    async def _main(self) -> None:
        from websockets.asyncio.server import serve

        self._stop_event = asyncio.Event()
        last_error: Optional[BaseException] = None
        for port in self.ports:
            try:
                self._server = await serve(self._handle, self.host, port, max_size=P.MAX_FRAME_BYTES,
                                           ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                                           compression=None)
            except OSError as exc:
                last_error = exc
                continue
            self.port = port
            break
        if self.port is None:
            raise RuntimeError(f"no free port in {self.ports[0]}..{self.ports[-1]}: {last_error}")
        log.info("WebSocket server on %s:%s (session %s)", self.host, self.port, self.session_id)
        self._started.set()
        await self._stop_event.wait()
        clients = ([self._agent] if self._agent else []) + list(self._displays.values())
        for client in clients:
            try:
                await client.ws.close(1001, "host exit")
            except Exception:  # noqa: BLE001
                pass
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), 3)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    async def _handle(self, ws: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        if not is_loopback(ws.remote_address):
            await ws.close(P.CLOSE_UNAUTHORIZED, "loopback only")
            return
        query = parse_query(getattr(ws.request, "path", ""))
        role = query.get("role")
        if query.get("token") != self.token or role not in P.ROLES:
            log.info("refused a connection (role %r, token %s)", role, "ok" if query.get("token") == self.token else "bad")
            await ws.close(P.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT)
            hello = P.Hello.parse(P.loads(raw if isinstance(raw, str) else raw.decode("utf-8")))
        except (asyncio.TimeoutError, P.ProtocolError, ConnectionClosed, UnicodeDecodeError) as exc:
            log.info("handshake failed (%s): %s", role, exc)
            await ws.close(P.CLOSE_UNAUTHORIZED, "handshake")
            return
        if hello.role != role:
            await ws.close(P.CLOSE_UNAUTHORIZED, "role mismatch")
            return
        if hello.protocol != PROTOCOL_VERSION:
            await self._send(ws, P.bye("protocol"), role)
            await ws.close(P.CLOSE_PROTOCOL, "protocol")
            return

        update = None
        if self._update_for is not None:
            try:
                update = self._update_for(hello)
            except Exception:  # noqa: BLE001
                log.exception("update decision failed")
        client = _Client(ws, hello)
        await self._send(ws, P.welcome(self.host_version, self.session_id, update), role)
        log.info("%s connected: panel %s, Premiere %s, CEP %s", role, hello.panel_version, hello.premiere_version,
                 hello.cep_version)

        if role == P.ROLE_AGENT:
            old = self._agent
            self._agent = client
            if old is not None:
                log.info("agent %s replaced by %s", old.id, client.id)
                asyncio.ensure_future(self._close_quietly(old.ws, CLOSE_REPLACED, "replaced"))
            self.agent._presence_changed(True, hello)
        else:
            self._displays[client.id] = client
            if self.displays.last_ui is not None:
                await self._send(ws, self.displays.last_ui, role)
            self.displays._count_changed()

        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue  # no binary frames in this protocol
                try:
                    message = P.loads(raw)
                except P.ProtocolError as exc:
                    log.debug("bad frame from %s: %s", role, exc)
                    continue
                _log_message("<-", role, message)
                if role == P.ROLE_AGENT:
                    self._on_agent_message(client, message)
                else:
                    self._on_display_message(message)
        except ConnectionClosed:
            pass
        finally:
            if role == P.ROLE_AGENT:
                if self._agent is client:
                    self._agent = None
                    log.info("agent disconnected")
                    self.agent._presence_changed(False, None)
            else:
                if self._displays.pop(client.id, None) is not None:
                    self.displays._count_changed()

    @staticmethod
    async def _close_quietly(ws: Any, code: int, reason: str) -> None:
        try:
            await ws.close(code, reason)
        except Exception:  # noqa: BLE001
            pass

    def _on_agent_message(self, client: _Client, message: dict) -> None:
        kind = message.get("type")
        if kind == P.T_LOG:
            agent_log.log(LOG_LEVELS.get(str(message.get("level")), logging.INFO), "%s", str(message.get("message"))[:2000])
            return
        if P.is_paged(message):
            script_id = str(message.get("id"))
            try:
                pages = client.pages.setdefault(script_id, P.ScriptResultPages(int(message["pages"])))
                complete = pages.add(message)
            except (ValueError, KeyError, TypeError) as exc:
                client.pages.pop(script_id, None)
                complete = {"type": P.T_SCRIPT_RESULT, "id": script_id, "ok": False,
                            "error": {"message": f"bad paged result: {exc}", "line": None, "source": ""},
                            "ms": message.get("ms")}
            if complete is None:
                return
            client.pages.pop(script_id, None)
            message = complete
        if kind in (P.T_SCRIPT_RESULT, P.T_EVENT, P.T_HEARTBEAT):
            self.agent._deliver(message)
        else:
            log.debug("ignored agent message type %r", kind)

    def _on_display_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == P.T_LOG:
            display_log.log(LOG_LEVELS.get(str(message.get("level")), logging.INFO), "%s", str(message.get("message"))[:2000])
        elif kind in (P.T_ACTION, P.T_DISPLAY_READY):
            self.displays._deliver(message)
        else:
            log.debug("ignored display message type %r", kind)

    async def _send(self, ws: Any, message: dict, role: str) -> bool:
        _log_message("->", role, message)
        try:
            await ws.send(P.dumps(message))
            return True
        except Exception:  # noqa: BLE001 - the socket is closing; the client reconnects
            log.debug("send to %s failed", role, exc_info=True)
            return False

    # -- sending from any thread
    def _send_to_agent(self, message: dict) -> None:
        agent = self._agent
        loop = self._loop
        if agent is None or loop is None:
            raise ConnectionError("no agent connected")
        asyncio.run_coroutine_threadsafe(self._send(agent.ws, message, P.ROLE_AGENT), loop)

    def _broadcast(self, message: dict) -> None:
        loop = self._loop
        if loop is None:
            return

        async def send_all() -> None:
            for client in list(self._displays.values()):
                await self._send(client.ws, message, P.ROLE_DISPLAY)

        asyncio.run_coroutine_threadsafe(send_all(), loop)
