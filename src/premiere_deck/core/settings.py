"""Host settings (`settings.json`, docs/HOST_UI.md 6) and the module settings scopes (docs/MODULES.md 3, 5).

- `Settings`: the host file, thread-safe, saved atomically on every change. Unknown or broken values fall back to
  the defaults on load; `patch()` validates and raises `ValueError` with a user-facing sentence.
- `HostModuleSettings` / `ProjectModuleSettings`: what a module sees as `ctx.settings` and `ctx.settings.project`.
  Host scope lives under `modules.<id>` in `settings.json`; project scope under `modules.<id>` in
  `<project root>\\.premiere-deck\\project.json` (other keys of that file are kept as they are).
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import atomic_write_json, read_json

log = logging.getLogger("premiere_deck.core.settings")

# Project themes (docs/HOST_UI.md 6, src/premiere_deck/ui/themes.json). The 1.x kit names (dusk, nova, frost, bloom)
# and any other stored value fall back to the default on load.
THEMES = ("black", "sky")
DEFAULT_THEME = "black"
MAX_RECENT = 12
# The Scale setting (docs/HOST_UI.md 3.4, docs/BRIDGE_API.md 3.3 `ui_scale`): percent on top of Windows' scaling.
SCALE_STEPS = (100, 125, 150, 175, 200)
DEFAULT_SCALE = 100
PROJECT_FILE = "project.json"
# The pinned row of the panel (docs/BRIDGE_API.md 3.3 `pins`, docs/PROTOCOL.md 6.1): action ids `<module>.<action>`.
PINS_MAX = 5
PIN_ID = re.compile(r"^[a-z][a-z0-9_]*\.\S+$")

DEFAULTS: dict[str, Any] = {
    "premiere_exe": "auto",
    "keep_running_with_premiere": True,
    "theme": DEFAULT_THEME,
    "auto_download_updates": True,
    "show_activity": False,  # the activity strip of the main screen (docs/HOST_UI.md 3.3)
    "ui_scale": DEFAULT_SCALE,  # window and text size, one of SCALE_STEPS
    "debug_mode_written": [],
    "recent_projects": [],
    "recent_opened": {},  # {path: ISO 8601 UTC of the last open through Deck}
    "pins": [],  # action ids of the panel's pinned row, in order (edited from the panel only)
    "modules": {},
    "modules_enabled": {},
}

# Keys the window may change through set_settings (the rest is written by the host itself).
PATCHABLE = ("premiere_exe", "keep_running_with_premiere", "theme", "auto_download_updates", "show_activity",
             "ui_scale")


def normalize_scale(value: Any) -> int:
    """The nearest step of SCALE_STEPS for a number or a numeric string (the larger step on a tie); ValueError
    with a sentence for anything else."""
    if isinstance(value, bool):
        raise ValueError("The scale needs a percentage.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("The scale needs a percentage.") from None
    if number != number or number <= 0:
        raise ValueError("The scale needs a percentage.")
    return min(SCALE_STEPS, key=lambda step: (abs(step - number), -step))


def normalize_pins(value: Any) -> list[str]:
    """The stored pinned row: the action ids of a list in order, without repeats, entries that are not
    `<module>.<action>` strings and anything past PINS_MAX; ValueError when the value is not a list."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("The setting needs a list.")
    pins: list[str] = []
    for item in value:
        if isinstance(item, str) and PIN_ID.match(item) and item not in pins:
            pins.append(item)
    return pins[:PINS_MAX]


def _normalise(key: str, value: Any) -> Any:
    """The value as stored, or raise ValueError with a sentence for the user."""
    default = DEFAULTS[key]
    if key == "pins":
        return normalize_pins(value)
    if key == "theme":
        name = str(value or "").strip().lower()
        if name not in THEMES:
            raise ValueError("Unknown theme.")
        return name
    if key == "ui_scale":
        return normalize_scale(value)
    if key == "premiere_exe":
        text = str(value or "").strip()
        if not text or text.lower() == "auto":
            return "auto"
        if not text.lower().endswith(".exe"):
            raise ValueError("Choose the Premiere Pro program file (Adobe Premiere Pro.exe).")
        return text
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError("The setting needs on or off.")
        return value
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError("The setting needs a list.")
        return list(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError("The setting needs a group of values.")
        return dict(value)
    return value


class Settings:
    """`settings.json` of the host. Every change is written at once (the file is small)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list[Callable[[dict], None]] = []
        self.load()

    # -- persistence
    def load(self) -> None:
        raw = read_json(self.path, {})
        data = copy.deepcopy(DEFAULTS)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key not in DEFAULTS:
                    data[key] = value  # keep keys of newer versions
                    continue
                try:
                    data[key] = _normalise(key, value)
                except ValueError:
                    log.warning("settings.json: bad value for %s, using the default", key)
        with self._lock:
            self._data = data

    def save(self) -> None:
        with self._lock:
            snapshot = copy.deepcopy(self._data)
        try:
            atomic_write_json(self.path, snapshot)
        except OSError:
            log.exception("could not save %s", self.path)

    def on_change(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        self.save()
        public = self.public()
        for fn in list(self._listeners):
            try:
                fn(public)
            except Exception:  # noqa: BLE001
                log.exception("settings listener failed")

    # -- host keys
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        stored = _normalise(key, value) if key in DEFAULTS else value
        with self._lock:
            self._data[key] = stored
        self._changed()

    def public(self) -> dict:
        """The keys the window shows (docs/BRIDGE_API.md 3.3)."""
        with self._lock:
            return {key: copy.deepcopy(self._data[key]) for key in PATCHABLE + ("recent_projects", "debug_mode_written")}

    def patch(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("Invalid settings.")
        cleaned = {}
        for key, value in patch.items():
            if key not in PATCHABLE:
                raise ValueError("The setting could not be changed.")
            cleaned[key] = _normalise(key, value)
        with self._lock:
            self._data.update(cleaned)
        self._changed()
        return self.public()

    def add_recent_project(self, path: str) -> None:
        if not path:
            return
        with self._lock:
            recent = [p for p in self._data.get("recent_projects", []) if isinstance(p, str)]
            recent = [p for p in recent if p.lower() != path.lower()]
            recent.insert(0, path)
            recent = recent[:MAX_RECENT]
            self._data["recent_projects"] = recent
            from datetime import datetime, timezone

            opened = self._data.get("recent_opened")
            opened = dict(opened) if isinstance(opened, dict) else {}
            opened = {k: v for k, v in opened.items() if isinstance(k, str) and isinstance(v, str)
                      and k.lower() != path.lower() and any(k.lower() == p.lower() for p in recent)}
            opened[path] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._data["recent_opened"] = opened
        self._changed()

    # -- pinned row (docs/BRIDGE_API.md 3.3 `pins`)
    def pins(self) -> list[str]:
        with self._lock:
            return list(self._data.get("pins") or [])

    def set_pins(self, pins: Any) -> list[str]:
        """Stores the pinned row (normalised: no repeats, at most PINS_MAX); saves only when it changed."""
        stored = normalize_pins(pins)
        with self._lock:
            if stored == self._data.get("pins"):
                return list(stored)
            self._data["pins"] = stored
        self._changed()
        return list(stored)

    # -- modules
    def module_enabled(self, module_id: str, default: bool) -> bool:
        with self._lock:
            value = self._data.get("modules_enabled", {}).get(module_id)
        return default if not isinstance(value, bool) else value

    def set_module_enabled(self, module_id: str, enabled: bool) -> None:
        with self._lock:
            self._data.setdefault("modules_enabled", {})[module_id] = bool(enabled)
        self._changed()

    def module_values(self, module_id: str) -> dict:
        with self._lock:
            values = self._data.get("modules", {}).get(module_id)
            return copy.deepcopy(values) if isinstance(values, dict) else {}

    def module_set(self, module_id: str, key: str, value: Any) -> None:
        with self._lock:
            modules = self._data.setdefault("modules", {})
            if not isinstance(modules.get(module_id), dict):
                modules[module_id] = {}
            modules[module_id][key] = copy.deepcopy(value)
        self._changed()


class ProjectModuleSettings:
    """`ctx.settings.project`: `modules.<id>` of the open project's `project.json`. Re-read on every access because
    the Files module creates the file and the user may edit it by hand."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, module_id: str, deck_dir: Callable[[], Optional[str]]) -> None:
        self.module_id = module_id
        self._deck_dir = deck_dir

    def _file(self) -> Optional[Path]:
        folder = self._deck_dir()
        return Path(folder) / PROJECT_FILE if folder else None

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        key = str(path).lower()
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    def all(self) -> dict:
        path = self._file()
        if path is None:
            return {}
        data = read_json(path, {})
        values = data.get("modules", {}).get(self.module_id) if isinstance(data, dict) and isinstance(data.get("modules"), dict) else None
        return copy.deepcopy(values) if isinstance(values, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.all().get(key, default)

    def set(self, key: str, value: Any) -> None:
        path = self._file()
        if path is None:
            raise RuntimeError("No project is open.")
        with self._lock_for(path):
            data = read_json(path, {})
            if not isinstance(data, dict):
                data = {}
            modules = data.get("modules")
            if not isinstance(modules, dict):
                modules = data["modules"] = {}
            if not isinstance(modules.get(self.module_id), dict):
                modules[self.module_id] = {}
            modules[self.module_id][key] = copy.deepcopy(value)
            atomic_write_json(path, data)

    @property
    def available(self) -> bool:
        return self._file() is not None


class HostModuleSettings:
    """`ctx.settings`: host scope with `.project` for the project scope."""

    def __init__(self, settings: Settings, module_id: str, deck_dir: Callable[[], Optional[str]]) -> None:
        self._settings = settings
        self.module_id = module_id
        self.project = ProjectModuleSettings(module_id, deck_dir)

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.module_values(self.module_id).get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._settings.module_set(self.module_id, key, value)

    def all(self) -> dict:
        return self._settings.module_values(self.module_id)


def coerce_setting(kind: str, value: Any, options: Optional[list] = None) -> Any:
    """A value from the window converted to a `SettingField` kind; ValueError with a sentence when it cannot be."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("The setting needs on or off.")
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError("The setting needs a whole number.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("The setting needs a whole number.") from None
        if number != int(number):
            raise ValueError("The setting needs a whole number.")
        return int(number)
    if kind == "select":
        allowed = [str(o.get("value")) for o in (options or []) if isinstance(o, dict)]
        text = str(value)
        if allowed and text not in allowed:
            raise ValueError("Choose one of the listed values.")
        return text
    if kind in ("text", "path"):
        return "" if value is None else str(value)
    raise ValueError("The setting could not be changed.")
