"""Host <-> panel protocol types and validation (docs/PROTOCOL.md).

The server (host/server.py) and the facade (premiere/api.py) build and parse messages with
these helpers; modules build UI nodes through modules/base.py, which calls `validate_tree` before a tree is sent.
Nothing here touches sockets.
"""

from __future__ import annotations

import json
import re
import secrets
import time
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
T_RELOAD = "reload"
T_EVENT = "event"
T_HEARTBEAT = "heartbeat"
T_LOG = "log"
T_UI = "ui"
T_ACTION = "action"
T_DISPLAY_READY = "display.ready"
T_TOAST = "toast"
T_THEME = "theme"
T_ACTION_RESULT = "action_result"

TONES = ("neutral", "ok", "warn", "error", "accent")
OUTCOMES = ("ok", "warn", "error", "neutral")  # action_result outcomes (docs/PROTOCOL.md 6.6)
THEMES = ("black", "sky")  # docs/PROTOCOL.md 6.5
DEFAULT_THEME = "black"
SHORT_MAX = 16  # longest tile label (button `short`)
GROUP_SHORT_MAX = 6  # longest group code (group `short`, 3.0)
PINS_MAX = 5  # size of the pinned row (tree `pins`, 3.0)
PANEL_PIN = "panel.pin"  # display actions that edit the pinned row (docs/PROTOCOL.md 6.1, 6.2)
PANEL_UNPIN = "panel.unpin"
PANEL_PINS = "panel.pins"  # `action_result` id of the pinned row
SWATCH = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")  # menu option `swatch` (docs/PROTOCOL.md 6.1)

# Premiere events a module may subscribe to (docs/PROTOCOL.md 8).
EVENTS = (
    "project.opened",
    "project.closed",
    "project.items_added",
    "project.changed",
    "project.selection_changed",
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


def theme(name: Any) -> dict:
    """`theme` message (docs/PROTOCOL.md 6.5); an unknown name is sent as the default theme."""
    return {"type": T_THEME, "name": name if name in THEMES else DEFAULT_THEME}


def action_result(button_id: str, outcome: str, text: str, detail: Optional[str] = None,
                  at: Optional[int] = None) -> dict:
    """`action_result` message (docs/PROTOCOL.md 6.6); `at` is epoch ms (now when omitted)."""
    if outcome not in OUTCOMES:
        raise ProtocolError(f"unknown outcome {outcome!r}")
    if not isinstance(button_id, str) or not button_id:
        raise ProtocolError("an action_result needs the button id")
    return {"type": T_ACTION_RESULT, "id": button_id, "outcome": outcome, "text": str(text),
            "detail": None if detail is None else str(detail),
            "at": int(time.time() * 1000) if at is None else int(at)}


def ui(revision: int, title: str, status: Optional[dict], sections: list, footer: Optional[dict] = None,
       pins: Optional[dict] = None) -> dict:
    """`pins` (3.0): the pinned row built by `pins()`; the key is absent when None."""
    message = {"type": T_UI, "revision": int(revision), "title": title, "status": status, "sections": sections, "footer": footer}
    if pins is not None:
        message["pins"] = pins
    validate_tree(message)
    return message


def pins(items: Iterable[str], max_items: int = PINS_MAX) -> dict:
    """The tree's top-level `pins` (docs/PROTOCOL.md 6.1, 3.0): the action ids of the pinned row, in order."""
    return {"items": [str(item) for item in items], "max": int(max_items)}


def tile_ids(sections: list) -> list[str]:
    """Ids of the nodes a pin may reference (docs/PROTOCOL.md 6.1 `pins`): the `button` and `switch` tiles of the
    `tiles` groups of `sections`, in tree order (icon-only buttons and list row buttons are no tiles)."""
    ids: list[str] = []

    def walk(nodes: Any, in_tiles: bool) -> None:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            kind = node.get("kind")
            if kind == "group":
                walk(node.get("items"), bool(node.get("tiles")))
            elif kind == "row":
                walk(node.get("items"), in_tiles)
            elif (in_tiles and kind in ("button", "switch") and node.get("variant") is None
                  and isinstance(node.get("id"), str)):
                ids.append(node["id"])

    for sec in sections if isinstance(sections, list) else []:
        if isinstance(sec, dict):
            walk(sec.get("items"), False)
    return ids


# --- UI node builders (used by modules/base.py, which adds the module prefix to ids) ------------------------------


def menu(id: str, value: Any, options: list, badge: str) -> dict:
    """The choice a tile carries (docs/PROTOCOL.md 6.1 `button.menu`): `badge` is the current choice in short form;
    a pick sends `action` with this `id` and the option's `value`. An option may carry `swatch` (2.1, a CSS hex
    colour such as "#1aa3e0"), drawn as a dot before its label."""
    def option(o: dict) -> dict:
        out = {"value": o["value"], "label": str(o["label"])}
        if o.get("swatch") is not None:
            if not isinstance(o["swatch"], str) or not SWATCH.match(o["swatch"]):
                raise ProtocolError(f"menu option swatch {o['swatch']!r} is not a #rgb or #rrggbb colour")
            out["swatch"] = o["swatch"]
        return out

    return {"id": id, "value": value, "badge": str(badge), "options": [option(o) for o in options]}


def auto(id: str, label: str, value: bool, *, hint: Optional[str] = None) -> dict:
    """3.0 `button.auto` (docs/PROTOCOL.md 6.1): an automation capsule attached to a tile; a press on the capsule
    sends `action` with this `id` and `value` = the opposite of `value`."""
    node = _strip({"id": id, "label": label, "hint": hint, "value": value})
    _check_auto(node)
    return node


def prompt(id: str, label: str, value: str = "", *, placeholder: str = "") -> dict:
    """3.0 `button.prompt` (docs/PROTOCOL.md 6.1): the stored value a tile carries, edited in a popover with one
    text field opened from the tile's corner mark; a submit sends `action` with this `id` and the text."""
    node = {"id": id, "label": label, "value": value, "placeholder": placeholder}
    _check_prompt(node)
    return node


def node_button(id: str, label: str, *, hint: Optional[str] = None, tone: str = "neutral", busy: bool = False,
                disabled: bool = False, confirm: Optional[str] = None, icon: Optional[str] = None,
                short: Optional[str] = None, meta: Optional[str] = None, menu: Optional[dict] = None,
                variant: Optional[str] = None, auto: Optional[dict] = None, prompt: Optional[dict] = None) -> dict:
    """2.0 fields: `icon` (sprite symbol), `short` (tile label, at most 16 characters), `meta` (line under the tile
    label), `menu` (see `menu()`), `variant` (`"icon"`: a small round button with the icon only). 3.0: `auto`
    (see `auto()`) and `prompt` (see `prompt()`); a button carries at most one of `menu` and `prompt`."""
    if short is not None and len(short) > SHORT_MAX:
        raise ProtocolError(f"short label {short!r} is longer than {SHORT_MAX} characters")
    if variant not in (None, "icon"):
        raise ProtocolError(f"unknown button variant {variant!r}")
    if menu is not None and prompt is not None:
        raise ProtocolError("a button carries a menu or a prompt, not both")
    return _strip({"kind": "button", "id": id, "label": label, "hint": hint, "tone": tone, "busy": busy,
                   "disabled": disabled, "confirm": confirm, "icon": icon, "short": short, "meta": meta,
                   "menu": menu, "variant": variant, "auto": auto, "prompt": prompt})


def node_toggle(id: str, label: str, value: bool, *, hint: Optional[str] = None, disabled: bool = False) -> dict:
    return _strip({"kind": "toggle", "id": id, "label": label, "value": bool(value), "hint": hint, "disabled": disabled})


def node_switch(id: str, label: str, value: bool, *, short: Optional[str] = None, icon: Optional[str] = None,
                hint: Optional[str] = None, disabled: bool = False, busy: bool = False) -> dict:
    """2.1 `switch` (docs/PROTOCOL.md 6.1): a tile with a capsule switch; a click sends `action` with `value` =
    the opposite of `value`. `short` is the tile code (at most 16 characters), `busy` greys it while a job runs."""
    if short is not None and len(short) > SHORT_MAX:
        raise ProtocolError(f"short label {short!r} is longer than {SHORT_MAX} characters")
    return _strip({"kind": "switch", "id": id, "label": label, "value": bool(value), "short": short, "icon": icon,
                   "hint": hint, "disabled": disabled, "busy": busy})


def node_select(id: str, label: str, value: str, options: list, *, disabled: bool = False) -> dict:
    return _strip({"kind": "select", "id": id, "label": label, "value": value,
                   "options": [{"value": str(o["value"]), "label": str(o["label"])} for o in options],
                   "disabled": disabled})


def node_input(id: str, label: str, value: str = "", *, placeholder: str = "", submit_label: Optional[str] = None,
               disabled: bool = False) -> dict:
    return _strip({"kind": "input", "id": id, "label": label, "value": value, "placeholder": placeholder,
                   "submit_label": submit_label, "disabled": disabled})


def node_text(text: str, *, tone: str = "neutral", small: bool = False, chip: bool = False) -> dict:
    """`chip`: a small status chip, a dot in the tone's colour and the text (docs/PROTOCOL.md 6.1)."""
    node = {"kind": "text", "text": str(text), "tone": tone, "small": bool(small)}
    if chip:
        node["chip"] = True
    return node


def node_progress(value: Optional[float], label: str = "", *, detail: Optional[str] = None,
                  cancel: Optional[str] = None) -> dict:
    """`detail`: one line under the bar (the current item); `cancel`: a button node id the "Stop" pill sends."""
    node = {"kind": "progress", "value": None if value is None else max(0.0, min(1.0, float(value))), "label": label}
    if detail is not None:
        node["detail"] = str(detail)
    if cancel is not None:
        node["cancel"] = str(cancel)
    return node


SWATCH_COLUMNS_MAX = 16  # most cells of one `swatches` row (docs/PROTOCOL.md 6.1)


def node_swatches(id: str, label: str, value: Any, options: list, *, hint: Optional[str] = None, columns: int = 8,
                  disabled: bool = False) -> dict:
    """3.2 `swatches` (docs/PROTOCOL.md 6.1): a row of colour cells, one per option `{value, label, swatch}` (swatch
    required, a CSS hex colour); a click sends `action` with this `id` and the option's `value`, like a `select`.
    `value` is the current option (drawn with a ring) or None; `columns` 1 to 16 cells per row."""
    node = _strip({"kind": "swatches", "id": id, "label": label, "hint": hint, "value": value,
                   "options": [_swatch_option(o) for o in options], "columns": columns, "disabled": disabled})
    if value is None:
        node["value"] = None
    _validate_swatches(node)
    return node


def _swatch_option(o: dict) -> dict:
    if not isinstance(o, dict) or "value" not in o:
        raise ProtocolError("a swatches option is {value, label, swatch}")
    return {"value": o["value"], "label": str(o.get("label", "")), "swatch": o.get("swatch")}


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


def node_group(title: Optional[str], items: list, *, columns: Optional[int] = None, id: Optional[str] = None,
               collapsible: bool = False, tiles: bool = False, short: Optional[str] = None,
               icon: Optional[str] = None) -> dict:
    """A titled block inside a section (docs/PROTOCOL.md 6.1). `columns` 1 to 3 (default 1, or 3 with `tiles`) lays
    consecutive buttons out in that many columns; `tiles` draws the buttons as icon tiles; `collapsible` makes the
    caption the collapse control, remembered by the display per `id` (unique in the tree). 3.0: `short` is the
    group code at the row's edge (1 to 6 characters; `title` is its tooltip), `icon` a sprite symbol."""
    node = {"kind": "group", "title": title, "items": items,
            "columns": columns if columns is not None else (3 if tiles else 1)}
    if id is not None:
        node["id"] = id
    if collapsible:
        node["collapsible"] = True
    if tiles:
        node["tiles"] = True
    if short is not None:
        _check_group_short(short)
        node["short"] = short
    if icon is not None:
        node["icon"] = icon
    return node


def section(id: str, title: str, items: list, *, status: Optional[dict] = None, collapsed: bool = False,
            icon: Optional[str] = None) -> dict:
    """A panel tab; `icon` is a symbol name of the panel sprite (docs/PANEL.md 1). `collapsed` is ignored by 2.x."""
    sec = {"id": id, "title": title, "collapsed": bool(collapsed), "status": status, "items": items}
    if icon is not None:
        sec["icon"] = icon
    return sec


def status(text: str, tone: str = "neutral") -> dict:
    _check_tone(tone)
    return {"text": str(text), "tone": tone}


# --- validation ------------------------------------------------------------------------------------------------

_NODE_KINDS = {"button", "switch", "toggle", "select", "input", "text", "progress", "list", "row", "group", "divider",
               "swatches"}
_INTERACTIVE = {"button", "switch", "toggle", "select", "input", "list", "swatches"}


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
        if sec.get("icon") is not None and not isinstance(sec["icon"], str):
            raise ProtocolError("a section icon is a string or null")
        if sec.get("status") is not None:
            _check_status(sec["status"])
        _validate_nodes(sec.get("items", []), seen)
    footer = message.get("footer")
    if footer is not None and not isinstance(footer, dict):
        raise ProtocolError("footer is an object")
    if message.get("pins") is not None:
        _check_pins(message["pins"])


def _check_pins(value: Any) -> None:
    """`pins` (3.0): `{items: [unique non-empty strings], max: 1..PINS_MAX}` with at most `max` items. An item needs
    no node in the tree (the display skips it)."""
    if not isinstance(value, dict):
        raise ProtocolError("pins is {items, max}")
    limit = value.get("max")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PINS_MAX:
        raise ProtocolError(f"pins max is a whole number from 1 to {PINS_MAX}")
    items = value.get("items")
    if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
        raise ProtocolError("pins items is a list of action ids")
    if len(set(items)) != len(items):
        raise ProtocolError("pins items repeat an id")
    if len(items) > limit:
        raise ProtocolError(f"pins hold at most {limit} items")


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
        if kind == "button":
            _validate_button(node, seen)
        if kind == "switch":
            _validate_switch(node)
        if kind == "swatches":
            _validate_swatches(node)
        if kind == "text" and not isinstance(node.get("chip", False), bool):
            raise ProtocolError("text chip is a bool")
        if kind == "progress":
            for field_name in ("detail", "cancel"):
                if node.get(field_name) is not None and not isinstance(node[field_name], str):
                    raise ProtocolError(f"progress {field_name} is a string or null")
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
                    _validate_button(action, None)
        if kind == "row":
            _validate_nodes(node.get("items", []), seen)
        if kind == "group":
            title = node.get("title")
            if title is not None and not isinstance(title, str):
                raise ProtocolError("a group title is a string or null")
            columns = node.get("columns", 1)
            if isinstance(columns, bool) or columns not in (1, 2, 3):
                raise ProtocolError("group columns is 1, 2 or 3")
            for flag in ("collapsible", "tiles"):
                if not isinstance(node.get(flag, False), bool):
                    raise ProtocolError(f"group {flag} is a bool")
            if node.get("short") is not None:
                _check_group_short(node["short"])
            if node.get("icon") is not None and not isinstance(node["icon"], str):
                raise ProtocolError("a group icon is a string or null")
            group_id = node.get("id")
            if group_id is not None:
                if not isinstance(group_id, str) or not group_id:
                    raise ProtocolError("a group id is a non-empty string")
                _unique(seen, group_id)
            elif node.get("collapsible"):
                raise ProtocolError("a collapsible group needs an id")
            items = node.get("items", [])
            if any(isinstance(item, dict) and item.get("kind") == "group" for item in items if isinstance(items, list)):
                raise ProtocolError("groups do not nest")
            _validate_nodes(items, seen)


def _validate_button(node: dict, seen: Optional[set]) -> None:
    """2.0 button fields (docs/PROTOCOL.md 6.1); `seen` is None for list row buttons (their ids repeat per row)."""
    for field_name in ("icon", "short", "meta"):
        if node.get(field_name) is not None and not isinstance(node[field_name], str):
            raise ProtocolError(f"button {field_name} is a string or null")
    short = node.get("short")
    if isinstance(short, str) and len(short) > SHORT_MAX:
        raise ProtocolError(f"short label {short!r} is longer than {SHORT_MAX} characters")
    if node.get("variant") not in (None, "icon"):
        raise ProtocolError(f"unknown button variant {node.get('variant')!r}")
    if node.get("auto") is not None:
        _check_auto(node["auto"])
        if seen is not None:
            _unique(seen, node["auto"]["id"])
    if node.get("prompt") is not None:
        if node.get("menu") is not None:
            raise ProtocolError("a button carries a menu or a prompt, not both")
        _check_prompt(node["prompt"])
        if seen is not None:
            _unique(seen, node["prompt"]["id"])
    menu_value = node.get("menu")
    if menu_value is None:
        return
    if not isinstance(menu_value, dict) or not isinstance(menu_value.get("id"), str) or not menu_value["id"]:
        raise ProtocolError("a button menu needs a string id")
    options = menu_value.get("options")
    if not isinstance(options, list) or not options or not all(
            isinstance(o, dict) and "value" in o and isinstance(o.get("label"), str) for o in options):
        raise ProtocolError("a button menu needs options of {value, label}")
    for o in options:
        swatch = o.get("swatch")
        if swatch is not None and (not isinstance(swatch, str) or not SWATCH.match(swatch)):
            raise ProtocolError(f"menu option swatch {swatch!r} is not a #rgb or #rrggbb colour")
    if not isinstance(menu_value.get("badge", ""), str):
        raise ProtocolError("a button menu badge is a string")
    if seen is not None:
        _unique(seen, menu_value["id"])


def _validate_switch(node: dict) -> None:
    """2.1 switch fields (docs/PROTOCOL.md 6.1): `value` is a bool; `short` at most 16 characters."""
    if not isinstance(node.get("value"), bool):
        raise ProtocolError("switch value is a bool")
    for field_name in ("label", "short", "icon", "hint"):
        if node.get(field_name) is not None and not isinstance(node[field_name], str):
            raise ProtocolError(f"switch {field_name} is a string or null")
    short = node.get("short")
    if isinstance(short, str) and len(short) > SHORT_MAX:
        raise ProtocolError(f"short label {short!r} is longer than {SHORT_MAX} characters")
    for flag in ("disabled", "busy"):
        if not isinstance(node.get(flag, False), bool):
            raise ProtocolError(f"switch {flag} is a bool")


def _validate_swatches(node: dict) -> None:
    """3.2 `swatches` (docs/PROTOCOL.md 6.1): options of `{value, label, swatch}` with unique values and a #rgb or
    #rrggbb swatch each, `columns` a whole number from 1 to 16, `value` null or one of the option values."""
    options = node.get("options")
    if not isinstance(options, list) or not options:
        raise ProtocolError("swatches need options")
    values = []
    for o in options:
        if not isinstance(o, dict) or "value" not in o or not isinstance(o.get("label"), str):
            raise ProtocolError("a swatches option is {value, label, swatch}")
        swatch = o.get("swatch")
        if not isinstance(swatch, str) or not SWATCH.match(swatch):
            raise ProtocolError(f"swatches option swatch {swatch!r} is not a #rgb or #rrggbb colour")
        values.append(o["value"])
    if len(set(map(str, values))) != len(values):
        raise ProtocolError("swatches option values repeat")
    columns = node.get("columns", 8)
    if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= SWATCH_COLUMNS_MAX:
        raise ProtocolError(f"swatches columns is a whole number from 1 to {SWATCH_COLUMNS_MAX}")
    value = node.get("value")
    if value is not None and value not in values:
        raise ProtocolError(f"swatches value {value!r} is not an option")
    for field_name in ("label", "hint"):
        if node.get(field_name) is not None and not isinstance(node[field_name], str):
            raise ProtocolError(f"swatches {field_name} is a string or null")
    if not isinstance(node.get("disabled", False), bool):
        raise ProtocolError("swatches disabled is a bool")


def _check_auto(value: Any) -> None:
    """`button.auto` (3.0): `{id, label, hint, value}`; `value` a bool, `hint` a string or null."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        raise ProtocolError("a button auto needs a string id")
    if not isinstance(value.get("label"), str):
        raise ProtocolError("a button auto label is a string")
    if not isinstance(value.get("value"), bool):
        raise ProtocolError("a button auto value is a bool")
    if value.get("hint") is not None and not isinstance(value["hint"], str):
        raise ProtocolError("a button auto hint is a string or null")


def _check_prompt(value: Any) -> None:
    """`button.prompt` (3.0): `{id, label, value, placeholder}`, all strings."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        raise ProtocolError("a button prompt needs a string id")
    for field_name in ("label", "value", "placeholder"):
        if not isinstance(value.get(field_name), str):
            raise ProtocolError(f"a button prompt {field_name} is a string")


def _check_group_short(value: Any) -> None:
    if not isinstance(value, str) or not value or len(value) > GROUP_SHORT_MAX:
        raise ProtocolError(f"a group code is a string of 1 to {GROUP_SHORT_MAX} characters")


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
