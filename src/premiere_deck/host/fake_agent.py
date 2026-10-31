"""In-process stand-in for the panel's agent (`--fake`, docs/HOST_UI.md 1): a real WebSocket client that connects
to the host's server like the CEP agent does and answers `script` messages with canned data, so the window, the
registry and the modules run without Premiere Pro.

Answers are chosen by the script `name` (the facade names scripts after their jsx file); unknown names return
`null`. The fake project folder lives in the data folder (`<data>/fake/Sample Project`). A fake display (the visible
panel) can join and leave (`set_display`); `set_fake_premiere` puts the whole simulation into `not_running`,
`waiting` or `connected` (the bridge's `fake_premiere`, `--fake-state`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from premiere_deck import EXTENSION_AGENT_ID, EXTENSION_PANEL_ID, PROTOCOL_VERSION

from . import protocol as P

log = logging.getLogger("premiere_deck.host.fake_agent")

PREMIERE_VERSION = "26.0.0"
HEARTBEAT_SECONDS = 10.0


def make_fake_project(home: Path) -> dict:
    root = Path(home) / "fake" / "Sample Project"
    (root / "Footage").mkdir(parents=True, exist_ok=True)
    project = root / "Sample Project.prproj"
    if not project.exists():
        project.write_bytes(b"")
    return {"path": str(project), "root": str(root), "name": "Sample Project", "deck_dir": str(root / ".premiere-deck")}


def canned_answers(project: dict) -> dict[str, Any]:
    root = project["root"]
    items = [
        {"node_id": "b1", "name": "Footage", "kind": "bin", "tree_path": ""},
        {"node_id": "c1", "name": "interview_take3.mp4", "kind": "clip", "tree_path": "Footage",
         "media_path": root + "\\Footage\\interview_take3.mp4", "duration": 42.0, "has_video": True, "has_audio": True},
        {"node_id": "c2", "name": "drone_coast.mov", "kind": "clip", "tree_path": "Footage",
         "media_path": root + "\\Footage\\drone_coast.mov", "duration": 18.5, "has_video": True, "has_audio": False},
        {"node_id": "c3", "name": "music_bed.wav", "kind": "clip", "tree_path": "",
         "media_path": "C:\\Users\\Public\\Music\\music_bed.wav", "duration": 95.0, "has_video": False,
         "has_audio": True},
        {"node_id": "s1", "name": "Main edit", "kind": "sequence", "tree_path": "", "is_sequence": True},
    ]
    sequence = {"sequence_id": "s1", "name": "Main edit", "width": 1920, "height": 1080, "fps": 25.0,
                "duration": 120.0, "in_point": None, "out_point": None}

    def create_bin_path(args: Any) -> dict:
        # The jsx returns the leaf bin's node id (premiere/jsx/create_bin_path.jsx); a stable id per path.
        tree_path = str((args or {}).get("tree_path") or "")
        return {"node_id": "bin:" + tree_path.replace("\\", "/").strip("/")}

    return {
        "create_bin_path": create_bin_path,
        "project_info": project, "project": project,
        "snapshot": {"project_path": project["path"], "items": items},
        "item": items[1],
        "sequences": [sequence], "active_sequence": sequence,
        "selection": {"sequence_id": "s1", "items": []},
        "track_items": [],
        "playhead": 12.5, "set_playhead": None,
        "workspaces": ["Editing", "Assembly", "Premiere Deck"],
        "version": PREMIERE_VERSION, "premiere_version": PREMIERE_VERSION,
        # 3.2: the label palette (no live preferences: every value falls back to the defaults), the Labels target
        # and the Audio tab's tracks.
        "label_palette": {"available": False, "labels": []},
        "actions.colour": {"target": "", "clips": 0, "items": 0, "bins": 0, "sequence": "Main edit"},
        "audio_tracks": {"found": True, "tracks": [
            {"index": 0, "name": "Audio 1", "muted": False, "clip_count": 6},
            {"index": 1, "name": "Dialogue", "muted": False, "clip_count": 4},
            {"index": 2, "name": "Music", "muted": True, "clip_count": 1},
            {"index": 3, "name": "Audio 4", "muted": False, "clip_count": 0}]},
    }


class FakeAgent:
    """Connects to `ws://127.0.0.1:<port>/ws?role=agent&token=...` and serves scripts until `stop()`."""

    def __init__(self, port: int, token: str, *, home: Path, panel_version: str = "0",
                 answers: Optional[dict[str, Any]] = None, delay: float = 0.02, with_display: bool = False) -> None:
        self.port = port
        self.token = token
        self.panel_version = panel_version
        # `--fake` stands in for a user who keeps the panel open: a display client joins after the agent, so the
        # window reaches the `connected` state (docs/BRIDGE_API.md 3.2) without Premiere.
        self.with_display = with_display
        self.display_connected = threading.Event()
        self.display_received: list[dict] = []
        self._display_thread: Optional[threading.Thread] = None
        self._display_stop = threading.Event()
        self._display_ws: Any = None
        self.project = make_fake_project(home)
        self.answers = canned_answers(self.project)
        if answers:
            self.answers.update(answers)
        self.delay = delay
        self.scripts: list[dict] = []
        self.subscribed: list[str] = []
        self.received: list[dict] = []
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._send_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fake-agent", daemon=True)
        self._thread.start()
        if self.with_display:
            self.set_display(True)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def set_display(self, on: bool) -> None:
        """Open (True) or close (False) the fake visible panel; the window follows to `connected` and back."""
        thread = self._display_thread
        if on:
            if thread is not None and thread.is_alive() and not self._display_stop.is_set():
                return
            self._display_stop = threading.Event()
            self._display_thread = threading.Thread(target=self._run_display, args=(self._display_stop,),
                                                    name="fake-display", daemon=True)
            self._display_thread.start()
            return
        self._display_stop.set()
        ws = self._display_ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)

    def stop(self) -> None:
        self._stop.set()
        self._display_stop.set()
        for ws in (self._ws, self._display_ws):
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        for thread in (self._thread, self._display_thread):
            if thread is not None:
                thread.join(3)

    def send(self, message: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._send_lock:
            ws.send(P.dumps(message))

    def fire(self, name: str, data: Any = None) -> None:
        """Send an `event` as the real agent would."""
        self.send({"type": P.T_EVENT, "name": name, "data": data or {}, "at": int(time.time() * 1000)})

    def _run(self) -> None:
        from websockets.sync.client import connect

        url = f"ws://127.0.0.1:{self.port}/ws?role=agent&token={self.token}"
        while not self._stop.is_set():
            try:
                with connect(url, open_timeout=5, max_size=P.MAX_FRAME_BYTES) as ws:
                    self._ws = ws
                    self.send({"type": P.T_HELLO, "role": P.ROLE_AGENT, "panel_version": self.panel_version,
                               "protocol": PROTOCOL_VERSION, "premiere_version": PREMIERE_VERSION + " (fake)",
                               "cep_version": "12.0", "extension_id": EXTENSION_AGENT_ID,
                               "project_path": self.project["path"]})
                    welcome = json.loads(ws.recv(timeout=5))
                    if welcome.get("type") != P.T_WELCOME:
                        log.warning("fake agent: no welcome (%s)", welcome)
                        return
                    self.connected.set()
                    log.info("fake agent connected (session %s)", welcome.get("session_id"))
                    heartbeat = threading.Thread(target=self._heartbeat, name="fake-agent-heartbeat", daemon=True)
                    heartbeat.start()
                    for raw in ws:
                        if self._stop.is_set():
                            break
                        self._on_message(json.loads(raw))
            except Exception as exc:  # noqa: BLE001 - reconnect like the real agent
                if self._stop.is_set():
                    break
                log.info("fake agent connection ended: %s", exc)
            finally:
                self._ws = None
                self.connected.clear()
            self._stop.wait(3.0)

    def _run_display(self, stop: threading.Event) -> None:
        """The fake visible panel: handshakes as `display` once the agent is connected and keeps the latest
        messages (`ui`, `theme`, `toast`); reconnects like the real display until `stop` is set."""
        from websockets.sync.client import connect

        url = f"ws://127.0.0.1:{self.port}/ws?role=display&token={self.token}"
        while not self._stop.is_set() and not stop.is_set():
            if not self.connected.wait(0.5):
                continue
            try:
                with connect(url, open_timeout=5, max_size=P.MAX_FRAME_BYTES) as ws:
                    self._display_ws = ws
                    ws.send(P.dumps({"type": P.T_HELLO, "role": P.ROLE_DISPLAY, "panel_version": self.panel_version,
                                     "protocol": PROTOCOL_VERSION, "premiere_version": PREMIERE_VERSION + " (fake)",
                                     "cep_version": "12.0", "extension_id": EXTENSION_PANEL_ID,
                                     "project_path": None}))
                    welcome = json.loads(ws.recv(timeout=5))
                    if welcome.get("type") != P.T_WELCOME:
                        log.warning("fake display: no welcome (%s)", welcome)
                        return
                    self.display_connected.set()
                    log.info("fake display connected")
                    for raw in ws:
                        if self._stop.is_set() or stop.is_set():
                            break
                        self.display_received.append(json.loads(raw))
                        del self.display_received[:-20]
            except Exception as exc:  # noqa: BLE001 - reconnect like the real display
                if self._stop.is_set() or stop.is_set():
                    break
                log.info("fake display connection ended: %s", exc)
            finally:
                self._display_ws = None
                self.display_connected.clear()
            stop.wait(3.0)

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS) and self._ws is not None:
            try:
                self.send({"type": P.T_HEARTBEAT, "project_path": self.project["path"], "active_sequence_id": "s1",
                           "at": int(time.time() * 1000)})
            except Exception:  # noqa: BLE001
                return

    def _on_message(self, message: dict) -> None:
        self.received.append(message)
        kind = message.get("type")
        if kind == P.T_SCRIPT:
            self.scripts.append({k: v for k, v in message.items() if k != "code"})
            name = str(message.get("name") or "")
            answer = self.answers.get(name)
            if callable(answer):
                try:
                    answer = answer(message.get("args"))
                except Exception as exc:  # noqa: BLE001
                    self._reply_error(message, str(exc))
                    return
            if self.delay:
                time.sleep(self.delay)
            self.send({"type": P.T_SCRIPT_RESULT, "id": message.get("id"), "ok": True, "result": answer,
                       "ms": int(self.delay * 1000)})
        elif kind == P.T_SUBSCRIBE:
            self.subscribed = list(message.get("events") or [])
        else:
            log.debug("fake agent: %s", kind)

    def _reply_error(self, message: dict, text: str) -> None:
        self.send({"type": P.T_SCRIPT_RESULT, "id": message.get("id"), "ok": False,
                   "error": {"message": text, "line": None, "source": str(message.get("code", ""))[:200]}, "ms": 0})


FAKE_STATES = ("not_running", "waiting", "connected")


def set_fake_premiere(hub: Any, state: str) -> None:
    """`--fake` only: simulate Premiere's side for the window (docs/BRIDGE_API.md 2 `fake_premiere`).
    `not_running` stops the fake agent and its display; `waiting` runs the agent without a display; `connected`
    adds the display (the visible panel). The hub's state events follow from the real connections."""
    if state not in FAKE_STATES:
        raise ValueError("Unknown state.")
    agent = hub.fake_agent
    if state == "not_running":
        if agent is not None and not agent.stopped:
            agent.stop()
        return
    if agent is None or agent.stopped:
        agent = FakeAgent(hub.server.port, hub.server.token, home=hub.paths.home,
                          panel_version=hub.installer.installed_version() or "0", with_display=False)
        hub.fake_agent = agent
        agent.start()
    agent.set_display(state == "connected")


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
