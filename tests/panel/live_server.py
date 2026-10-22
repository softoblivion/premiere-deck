"""Development host for live tests of the panel and the facade (docs/PANEL.md 4.5). Not shipped.

A small `websockets` server implementing docs/PROTOCOL.md 1-3 for one agent and any number of displays, with a
`PremiereApi` on top. It writes `deck.json` into the installed panel folder (the dev junction) and removes it on
exit. The display gets a sample tree with every node kind; its actions change the tree, so the round trip is
visible in the panel.

Control, for scripted checks: a loopback HTTP endpoint (default port 47890)
    POST /exec   body = Python source, run with `api`, `server`, `state` in scope; `result = ...` is returned as JSON
    GET  /state  connection, recorded events, actions and agent messages
Run:
    set PYTHONPATH=src
    .venv\\Scripts\\python tests\\panel\\live_server.py [--panel-dir DIR] [--port N] [--control-port N] [--repl]
"""

from __future__ import annotations

import argparse
import asyncio
import code as code_module
import contextlib
import io
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve

from premiere_deck import PROTOCOL_VERSION, __version__
from premiere_deck.host import protocol
from premiere_deck.premiere.api import PremiereApi

DEFAULT_PANEL_DIR = Path(os.environ.get("APPDATA", "")) / "Adobe" / "CEP" / "extensions" / "com.softoblivion.premieredeck"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.actions: list[dict] = []
        self.agent_messages: list[dict] = []
        self.display_messages: list[dict] = []
        self.hellos: list[dict] = []

    def record(self, bucket: str, item: dict) -> None:
        with self.lock:
            target = getattr(self, bucket)
            target.append(item)
            del target[:-500]

    def dump(self) -> dict:
        with self.lock:
            return {"events": list(self.events), "actions": list(self.actions),
                    "agent_messages": list(self.agent_messages), "display_messages": list(self.display_messages),
                    "hellos": list(self.hellos)}


def sample_ui(model: dict, revision: int) -> dict:
    """A tree with every node kind of PROTOCOL 6.1."""
    rows = [protocol.list_row(row_id, title, subtitle=sub, tone=tone,
                              actions=[protocol.node_button("demo.list.open", "Open"),
                                       protocol.node_button("demo.list.remove", "Remove", confirm="Remove this row?")])
            for row_id, title, sub, tone in model["rows"]]
    controls = [
        protocol.node_button("demo.button", "Plain button", hint="Sends an action, the host answers with a toast"),
        protocol.node_button("demo.busy", "Busy for 2 s", busy=model["busy"], tone="accent"),
        protocol.node_button("demo.confirm", "Confirmed action", confirm="Run the confirmed action?", tone="warn"),
        protocol.node_button("demo.disabled", "Disabled button", disabled=True),
        protocol.node_toggle("demo.toggle", "A toggle", model["toggle"], hint="Sends its value"),
        protocol.node_select("demo.select", "A select", model["select"],
                             [{"value": "a", "label": "Option A"}, {"value": "b", "label": "Option B"},
                              {"value": "c", "label": "Option C"}]),
        protocol.node_input("demo.input", "An input", model["input"], placeholder="Type and press Enter",
                            submit_label="Send"),
        protocol.node_text(f"Last input: {model['last_input'] or '-'}"),
        protocol.node_text("Small dim text", small=True),
        protocol.node_text("Warning text", tone="warn"),
        protocol.node_progress(model["progress"], "Progress"),
        protocol.node_progress(None, "Indeterminate"),
        protocol.node_divider(),
        protocol.node_row([protocol.node_button("demo.row.a", "Row A"), protocol.node_button("demo.row.b", "Row B")]),
        protocol.node_list("demo.list", rows, empty="No rows"),
    ]
    sections = [
        protocol.section("demo", "Node kinds", controls, status=protocol.status(f"revision {revision}", "ok")),
        protocol.section("second", "Second section", [protocol.node_text("Collapsed by default")], collapsed=True),
    ]
    return protocol.ui(revision, "Premiere Deck", protocol.status("Connected · dev server", "ok"), sections,
                       {"text": f"Premiere Deck {__version__} (dev)", "link": {"label": "Open host", "action": "host.open"}})


class DevServer:
    """Implements `AgentTransport` for `PremiereApi`."""

    def __init__(self, panel_dir: Path, port: Optional[int], state: State) -> None:
        self.panel_dir = panel_dir
        self.port = port
        self.state = state
        self.token = protocol.new_token()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.agent: Optional[ServerConnection] = None
        self.displays: set[ServerConnection] = set()
        self.connected = False
        self.handler: Callable[[dict], None] = lambda message: None
        self.presence: Callable[[bool, Optional[protocol.Hello]], None] = lambda connected, hello: None
        self.revision = 0
        self.model = {"busy": False, "toggle": True, "select": "b", "input": "", "last_input": "", "progress": 0.3,
                      "rows": [("r1", "First row", "subtitle one", "ok"), ("r2", "Second row", "subtitle two", "warn"),
                               ("r3", "Third row", "", "neutral")]}
        self.custom_ui: Optional[dict] = None
        self.autostart = False
        self.host_exe: Optional[str] = None
        # Test switches: welcome.update for the next handshakes, or a protocol refusal (PROTOCOL 3).
        self.welcome_update: Optional[dict] = None
        self.refuse_protocol = False

    # AgentTransport
    def send(self, message: dict) -> None:
        agent = self.agent
        if agent is None or self.loop is None:
            raise ConnectionError("no agent")
        text = protocol.dumps(message)
        asyncio.run_coroutine_threadsafe(agent.send(text), self.loop)

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        self.handler = handler

    def set_presence_handler(self, handler: Callable[[bool, Optional[protocol.Hello]], None]) -> None:
        self.presence = handler

    # display side
    def broadcast(self, message: dict) -> None:
        if self.loop is None:
            return
        text = protocol.dumps(message)
        for display in list(self.displays):
            asyncio.run_coroutine_threadsafe(display.send(text), self.loop)

    def push_ui(self) -> None:
        self.revision += 1
        tree = self.custom_ui or sample_ui(self.model, self.revision)
        tree = dict(tree, revision=self.revision)
        self.broadcast(tree)

    def toast(self, text: str, tone: str = "ok", ms: int = 3500) -> None:
        self.broadcast(protocol.toast(tone, text, ms))

    def send_agent(self, message: dict) -> None:
        self.send(message)

    def write_deck(self) -> None:
        deck = {"port": self.port, "token": self.token, "host_version": __version__, "protocol": PROTOCOL_VERSION,
                "host_exe": self.host_exe, "autostart_host": self.autostart, "written_at": int(time.time())}
        (self.panel_dir / "deck.json").write_text(json.dumps(deck, indent=2), encoding="utf-8")

    def remove_deck(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.panel_dir / "deck.json").unlink()

    def on_action(self, message: dict) -> None:
        action = message.get("id")
        model = self.model
        if action == "demo.busy":
            model["busy"] = True

            def clear() -> None:
                model["busy"] = False
                self.push_ui()

            threading.Timer(2.0, clear).start()
        elif action == "demo.toggle":
            model["toggle"] = bool(message.get("value"))
        elif action == "demo.select":
            model["select"] = str(message.get("value"))
        elif action == "demo.input":
            model["last_input"] = str(message.get("value"))
            model["input"] = ""
        elif action == "demo.list.remove":
            model["rows"] = [row for row in model["rows"] if row[0] != message.get("row")]
        model["progress"] = round((model["progress"] + 0.1) % 1.0, 2)
        self.toast(f"{action} value={message.get('value')!r} row={message.get('row')!r}", "ok")
        self.push_ui()

    async def connection(self, ws: ServerConnection) -> None:
        query = parse_qs(urlparse(ws.request.path).query)
        role = (query.get("role") or [""])[0]
        token = (query.get("token") or [""])[0]
        if token != self.token or role not in protocol.ROLES:
            await ws.close(protocol.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        try:
            hello_text = await asyncio.wait_for(ws.recv(), 10)
            hello_message = protocol.loads(hello_text)
            hello = protocol.Hello.parse(hello_message)
        except Exception:
            await ws.close(protocol.CLOSE_PROTOCOL, "bad hello")
            return
        self.state.record("hellos", dict(hello_message, at=time.time()))
        if self.refuse_protocol:
            await ws.send(protocol.dumps(protocol.bye("protocol")))
            await ws.close(protocol.CLOSE_PROTOCOL, "protocol")
            return
        await ws.send(protocol.dumps(protocol.welcome(__version__, f"s-{int(time.time()) % 100000:05d}",
                                                      self.welcome_update)))
        if role == protocol.ROLE_AGENT:
            await self.serve_agent(ws, hello)
        else:
            await self.serve_display(ws)

    async def serve_agent(self, ws: ServerConnection, hello: protocol.Hello) -> None:
        self.agent = ws
        self.connected = True
        await asyncio.to_thread(self.presence, True, hello)
        try:
            async for text in ws:
                message = protocol.loads(text)
                if message.get("type") != protocol.T_SCRIPT_RESULT:
                    self.state.record("agent_messages", dict(message, received=time.time()))
                if message.get("type") == protocol.T_LOG:
                    print(f"[agent {message.get('level')}] {message.get('message')}", flush=True)
                self.handler(message)
        finally:
            if self.agent is ws:
                self.agent = None
                self.connected = False
                await asyncio.to_thread(self.presence, False, None)

    async def serve_display(self, ws: ServerConnection) -> None:
        self.displays.add(ws)
        self.state.record("events", {"name": "panel.opened", "data": {}, "at": time.time()})
        try:
            self.push_ui()
            async for text in ws:
                message = protocol.loads(text)
                self.state.record("display_messages", dict(message, received=time.time()))
                if message.get("type") == protocol.T_ACTION:
                    self.state.record("actions", dict(message, received=time.time()))
                    await asyncio.to_thread(self.on_action, message)
                elif message.get("type") == protocol.T_LOG:
                    print(f"[display {message.get('level')}] {message.get('message')}", flush=True)
        finally:
            self.displays.discard(ws)
            self.state.record("events", {"name": "panel.closed", "data": {}, "at": time.time()})

    async def run(self, ready: threading.Event) -> None:
        self.loop = asyncio.get_running_loop()
        ports = [self.port] if self.port else list(protocol.PORT_RANGE)
        last_error: Optional[Exception] = None
        for port in ports:
            try:
                server = await serve(self.connection, "127.0.0.1", port, max_size=protocol.MAX_FRAME_BYTES,
                                     ping_interval=15, ping_timeout=30)
            except OSError as exc:
                last_error = exc
                continue
            self.port = port
            self.write_deck()
            print(f"dev server on ws://127.0.0.1:{port}/ws, deck.json in {self.panel_dir}", flush=True)
            ready.set()
            async with server:
                await server.serve_forever()
            return
        raise RuntimeError(f"no free port: {last_error}")


def control_server(port: int, scope: dict) -> ThreadingHTTPServer:
    state: State = scope["state"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def reply(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, default=repr, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/state"):
                server: DevServer = scope["server"]
                self.reply(200, dict(state.dump(), connected=server.connected, displays=len(server.displays),
                                     project_path=scope["api"].project_path, port=server.port))
            else:
                self.reply(404, {"error": "unknown"})

        def do_POST(self) -> None:
            if not self.path.startswith("/exec"):
                self.reply(404, {"error": "unknown"})
                return
            source = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
            local = dict(scope)
            out = io.StringIO()
            started = time.monotonic()
            try:
                with contextlib.redirect_stdout(out):
                    exec(compile(source, "<exec>", "exec"), local)
                self.reply(200, {"ok": True, "result": local.get("result"), "stdout": out.getvalue(),
                                 "ms": int((time.monotonic() - started) * 1000)})
            except BaseException as exc:
                self.reply(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(),
                                 "stdout": out.getvalue(), "ms": int((time.monotonic() - started) * 1000)})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel-dir", type=Path, default=DEFAULT_PANEL_DIR)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--control-port", type=int, default=47890)
    parser.add_argument("--host-exe", default=None, help="host_exe written into deck.json")
    parser.add_argument("--autostart", action="store_true", help="autostart_host in deck.json")
    parser.add_argument("--repl", action="store_true")
    args = parser.parse_args()

    if not args.panel_dir.is_dir():
        print(f"panel folder not found: {args.panel_dir}", file=sys.stderr)
        return 2
    state = State()
    server = DevServer(args.panel_dir, args.port, state)
    server.host_exe = args.host_exe
    server.autostart = args.autostart
    api = PremiereApi(server)
    api.on_event(lambda name, data: state.record("events", {"name": name, "data": data, "at": time.time()}))
    scope = {"api": api, "server": server, "state": state, "protocol": protocol, "time": time, "json": json}

    ready = threading.Event()
    failure: list[BaseException] = []

    def run_loop() -> None:
        try:
            asyncio.run(server.run(ready))
        except BaseException as exc:
            failure.append(exc)
            ready.set()

    threading.Thread(target=run_loop, daemon=True).start()
    ready.wait()
    if failure:
        print(f"server failed: {failure[0]}", file=sys.stderr)
        return 1
    httpd = control_server(args.control_port, scope)
    print(f"control on http://127.0.0.1:{args.control_port}/ (POST /exec, GET /state)", flush=True)
    try:
        if args.repl:
            code_module.interact(local=scope, banner="api, server, state are in scope")
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        server.remove_deck()
    return 0


if __name__ == "__main__":
    sys.exit(main())
