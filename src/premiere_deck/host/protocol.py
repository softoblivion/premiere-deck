"""Host <-> panel protocol types and validation (docs/PROTOCOL.md).

The server (host/server.py) and the facade (premiere/api.py) build and parse messages with
these helpers; modules build UI nodes through modules/base.py, which calls `validate_tree` before a tree is sent.
Nothing here touches sockets.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol

from premiere_deck import PROTOCOL_VERSION

# Roles of a connected panel instance.
ROLE_AGENT = "agent"
ROLE_DISPLAY = "display"
ROLES = (ROLE_AGENT, ROLE_DISPLAY)

# WebSocket close codes.
CLOSE_UNAUTHORIZED = 4001
CLOSE_PROTOCOL = 4002

# Port range the host tries, in order (docs/PROTOCOL.md 2).
PORT_RANGE = tuple(range(47800, 47810))

# Message type names.
T_HELLO = "hello"
T_WELCOME = "welcome"
T_BYE = "bye"
T_SCRIPT = "script"
T_SCRIPT_RESULT = "script.result"
T_SUBSCRIBE = "subscribe"
T_OPEN_DISPLAY = "open_display"
T_RELOAD = "reload"
T_EVENT = "event"
T_HEARTBEAT = "heartbeat"
T_LOG = "log"
T_UI = "ui"
T_ACTION = "action"
T_DISPLAY_READY = "display.ready"
T_TOAST = "toast"

TONES = ("neutral", "ok", "warn", "error", "accent")

# Premiere events a module may subscribe to (docs/PROTOCOL.md 8).
EVENTS = (
    "project.opened",
    "project.closed",
    "project.items_added",
    "project.changed",
    "sequence.activated",
    "sequence.structure_changed",
    "sequence.selection_changed",
    "panel.opened",
    "panel.closed",
)

MAX_FRAME_BYTES = 8 * 1024 * 1024
SCRIPT_PAGE_BYTES = 6 * 1024 * 1024
DEFAULT_SCRIPT_TIMEOUT_MS = 30000


class ProtocolError(ValueError):
    """A message or a UI tree violates docs/PROTOCOL.md."""


def new_token() -> str:
    return secrets.token_hex(16)


def dumps(message: dict) -> str:
    """JSON text of a message; U+2028 / U+2029 escaped so the text is safe inside any JS context."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def loads(text: str) -> dict:
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("a message is an object with a string 'type'")
    return message


# --- handshake -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hello:
    role: str
    panel_version: str
    protocol: int
    premiere_version: str = ""
    cep_version: str = ""
    extension_id: str = ""
    project_path: Optional[str] = None

    @classmethod
    def parse(cls, message: dict) -> "Hello":
        if message.get("type") != T_HELLO:
            raise ProtocolError("expected hello")
        role = message.get("role")
        if role not in ROLES:
            raise ProtocolError(f"unknown role {role!r}")
        try:
            protocol = int(message.get("protocol", 0))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("protocol is an integer") from exc
        return cls(
            role=role,
            panel_version=str(message.get("panel_version", "")),
            protocol=protocol,
            premiere_version=str(message.get("premiere_version", "")),
            cep_version=str(message.get("cep_version", "")),
            extension_id=str(message.get("extension_id", "")),
            project_path=message.get("project_path") or None,
        )


def welcome(host_version: str, session_id: str, update: Optional[dict] = None) -> dict:
    return {
        "type": T_WELCOME,
        "host_version": host_version,
        "protocol": PROTOCOL_VERSION,
        "session_id": session_id,
        "update": update,
    }


def bye(reason: str) -> dict:
    return {"type": T_BYE, "reason": reason}


# --- host -> agent ---------------------------------------------------------------------------------------------


def script(script_id: str, name: str, code: str, args: Any = None, timeout_ms: int = DEFAULT_SCRIPT_TIMEOUT_MS) -> dict:
    return {"type": T_SCRIPT, "id": script_id, "name": name, "code": code, "args": args, "timeout_ms": int(timeout_ms)}


def subscribe(events: Iterable[str]) -> dict:
    names = sorted(set(events))
    unknown = [name for name in names if name not in EVENTS]
    if unknown:
        raise ProtocolError(f"unknown events: {unknown}")
    return {"type": T_SUBSCRIBE, "events": names}


def open_display() -> dict:
    return {"type": T_OPEN_DISPLAY}


def reload() -> dict:
    return {"type": T_RELOAD}


# --- agent -> host ---------------------------------------------------------------------------------------------


@dataclass
class ScriptResultPages:
    """Collects paged `script.result` chunks until complete (docs/PROTOCOL.md 6.4)."""

    pages: int
    chunks: dict = field(default_factory=dict)

    def add(self, message: dict) -> Optional[dict]:
        page = int(message["page"])
        self.chunks[page] = str(message.get("chunk", ""))
        if len(self.chunks) < self.pages:
            return None
        text = "".join(self.chunks[i] for i in range(1, self.pages + 1))
        return {"type": T_SCRIPT_RESULT, "id": message["id"], "ok": True, "result": json.loads(text), "ms": message.get("ms")}


def is_paged(message: dict) -> bool:
    return message.get("type") == T_SCRIPT_RESULT and "page" in message and "pages" in message


# --- display ---------------------------------------------------------------------------------------------------


def toast(tone: str, text: str, ms: int = 3500) -> dict:
    _check_tone(tone)
    return {"type": T_TOAST, "tone": tone, "text": str(text), "ms": int(ms)}


def ui(revision: int, title: str, status: Optional[dict], sections: list, footer: Optional[dict] = None) -> dict:
    message = {"type": T_UI, "revision": int(revision), "title": title, "status": status, "sections": sections, "footer": footer}
    validate_tree(message)
    return message


# --- UI node builders (used by modules/base.py, which adds the module prefix to ids) ------------------------------


def node_button(id: str, label: str, *, hint: Optional[str] = None, tone: str = "neutral", busy: bool = False,
                disabled: bool = False, confirm: Optional[str] = None) -> dict:
    return _strip({"kind": "button", "id": id, "label": label, "hint": hint, "tone": tone, "busy": busy,
                   "disabled": disabled, "confirm": confirm})


def node_toggle(id: str, label: str, value: bool, *, hint: Optional[str] = None, disabled: bool = False) -> dict:
    return _strip({"kind": "toggle", "id": id, "label": label, "value": bool(value), "hint": hint, "disabled": disabled})


def node_select(id: str, label: str, value: str, options: list, *, disabled: bool = False) -> dict:
    return _strip({"kind": "select", "id": id, "label": label, "value": value,
                   "options": [{"value": str(o["value"]), "label": str(o["label"])} for o in options],
                   "disabled": disabled})


def node_input(id: str, label: str, value: str = "", *, placeholder: str = "", submit_label: Optional[str] = None,
               disabled: bool = False) -> dict:
    return _strip({"kind": "input", "id": id, "label": label, "value": value, "placeholder": placeholder,
                   "submit_label": submit_label, "disabled": disabled})


def node_text(text: str, *, tone: str = "neutral", small: bool = False) -> dict:
    return {"kind": "text", "text": str(text), "tone": tone, "small": bool(small)}


def node_progress(value: Optional[float], label: str = "") -> dict:
    return {"kind": "progress", "value": None if value is None else max(0.0, min(1.0, float(value))), "label": label}


def node_list(id: str, rows: list, *, empty: str = "") -> dict:
    return {"kind": "list", "id": id, "rows": rows, "empty": empty}


def list_row(id: str, title: str, *, subtitle: str = "", tone: str = "neutral", thumb: Optional[str] = None,
             actions: Optional[list] = None, bar: Optional[float] = None) -> dict:
    """`bar` (0..1 or None) is drawn by the display as a thin bar under the subtitle."""
    return {"id": id, "title": title, "subtitle": subtitle, "tone": tone, "thumb": thumb, "actions": actions or [],
            "bar": None if bar is None else max(0.0, min(1.0, float(bar)))}


def node_row(items: list) -> dict:
    return {"kind": "row", "items": items}


def node_divider() -> dict:
    return {"kind": "divider"}


def node_group(title: Optional[str], items: list, *, columns: int = 1) -> dict:
    """A titled block inside a section (docs/PROTOCOL.md 6.1); `columns` 2 lays consecutive buttons out in pairs."""
    return {"kind": "group", "title": title, "items": items, "columns": columns}


def section(id: str, title: str, items: list, *, status: Optional[dict] = None, collapsed: bool = False) -> dict:
    return {"id": id, "title": title, "collapsed": bool(collapsed), "status": status, "items": items}


def status(text: str, tone: str = "neutral") -> dict:
    _check_tone(tone)
    return {"text": str(text), "tone": tone}


# --- validation ------------------------------------------------------------------------------------------------

_NODE_KINDS = {"button", "toggle", "select", "input", "text", "progress", "list", "row", "group", "divider"}
_INTERACTIVE = {"button", "toggle", "select", "input", "list"}


def validate_tree(message: dict) -> None:
    """Raises ProtocolError when a `ui` message violates docs/PROTOCOL.md 6.1 (duplicate ids, bad kinds, tones)."""
    if message.get("type") != T_UI:
        raise ProtocolError("not a ui message")
    if message.get("status") is not None:
        _check_status(message["status"])
    seen: set[str] = set()
    sections = message.get("sections")
    if not isinstance(sections, list):
        raise ProtocolError("sections is a list")
    for sec in sections:
        if not isinstance(sec, dict) or not isinstance(sec.get("id"), str) or not sec["id"]:
            raise ProtocolError("a section has a string id")
        _unique(seen, "section:" + sec["id"])
        if sec.get("status") is not None:
            _check_status(sec["status"])
        _validate_nodes(sec.get("items", []), seen)
    footer = message.get("footer")
    if footer is not None and not isinstance(footer, dict):
        raise ProtocolError("footer is an object")


def _validate_nodes(nodes: Any, seen: set) -> None:
    if not isinstance(nodes, list):
        raise ProtocolError("items is a list")
    for node in nodes:
        if not isinstance(node, dict):
            raise ProtocolError("a node is an object")
        kind = node.get("kind")
        if kind not in _NODE_KINDS:
            raise ProtocolError(f"unknown node kind {kind!r}")
        if kind in _INTERACTIVE:
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                raise ProtocolError(f"{kind} needs a string id")
            _unique(seen, node_id)
        if "tone" in node:
            _check_tone(node["tone"])
        if kind == "select":
            if not isinstance(node.get("options"), list) or not node["options"]:
                raise ProtocolError("select needs options")
        if kind == "list":
            for row in node.get("rows", []):
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise ProtocolError("a list row has a string id")
                _unique(seen, node["id"] + "/" + row["id"])
                if "tone" in row:
                    _check_tone(row["tone"])
                bar = row.get("bar")
                if bar is not None and (isinstance(bar, bool) or not isinstance(bar, (int, float)) or not 0 <= bar <= 1):
                    raise ProtocolError("a list row bar is a number from 0 to 1 or null")
                for action in row.get("actions", []):
                    if action.get("kind") != "button":
                        raise ProtocolError("row actions are buttons")
                    if not isinstance(action.get("id"), str) or not action["id"]:
                        raise ProtocolError("a row button needs an id")
                    if "tone" in action:
                        _check_tone(action["tone"])
        if kind == "row":
            _validate_nodes(node.get("items", []), seen)
        if kind == "group":
            title = node.get("title")
            if title is not None and not isinstance(title, str):
                raise ProtocolError("a group title is a string or null")
            if node.get("columns", 1) not in (1, 2):
                raise ProtocolError("group columns is 1 or 2")
            items = node.get("items", [])
            if any(isinstance(item, dict) and item.get("kind") == "group" for item in items if isinstance(items, list)):
                raise ProtocolError("groups do not nest")
            _validate_nodes(items, seen)


def _unique(seen: set, key: str) -> None:
    if key in seen:
        raise ProtocolError(f"duplicate id {key!r}")
    seen.add(key)


def _check_tone(tone: Any) -> None:
    if tone not in TONES:
        raise ProtocolError(f"unknown tone {tone!r}")


def _check_status(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ProtocolError("status is {text, tone}")
    _check_tone(value.get("tone", "neutral"))


def _strip(node: dict) -> dict:
    return {key: value for key, value in node.items() if value is not None}


# --- transport boundary between the server (host/server.py) and the facade (premiere/api.py) ----------------------


class AgentTransport(Protocol):
    """What `PremiereApi` needs from the server: send to the current agent, learn about its messages and presence.

    The server owns sockets, handshakes and reconnects; the facade owns scripts, queueing and events. One agent at a
    time: a new agent connection replaces the old one and fires `on_connect` again.
    """

    connected: bool

    def send(self, message: dict) -> None: ...
    def set_handler(self, handler: Callable[[dict], None]) -> None: ...
    def set_presence_handler(self, handler: Callable[[bool, Optional["Hello"]], None]) -> None: ...
