"""Install, update, verify and remove the CEP panel (docs/PANEL.md 4).

- Source: the bundled panel folder (`premiere_deck/panel`; the signed copy in a frozen build). A missing `VERSION`
  file reads as version "0".
- Staging: `<data>\\panel\\<version>\\` (one copy per host version; refreshed when the source content changed, which
  only happens in development).
- Target: `%APPDATA%\\Adobe\\CEP\\extensions\\com.softoblivion.premieredeck\\`. `PREMIERE_DECK_CEP_DIR` replaces the
  extensions folder (`...\\CEP\\extensions`) for tests and dev runs; the bundle folder name is appended to it.
- Install when the target is missing or its content differs from the staged copy (`VERSION` included; `deck.json`
  and `.debug` are ignored): copy to `<target>.new`, rename the target to `<target>.old`, rename `.new` into place,
  delete `.old` (a locked `.old` stays for the next run). A target that is a junction or symlink (development,
  `scripts/panel_dev.ps1`) is never touched.
- The manifest hash before and after decides what a connected panel must do: files only -> `reload`, manifest
  changed -> `restart_premiere` (`welcome.update`, docs/PROTOCOL.md 3). The decision of the last install is kept in
  `<data>\\panel\\install.json` so a panel that connects in a later run still gets the right answer.
- PlayerDebugMode fallback (4.3) writes `HKCU\\Software\\Adobe\\CSXS.<n>\\PlayerDebugMode = "1"` for n = 9..12, only
  after the user confirmed it in the window, and records the keys it changed in the setting `debug_mode_written`;
  uninstall (4.4) removes exactly those values. The registry is reached through `RegistryAccess`; tests pass a
  dictionary-backed fake and never touch the real registry.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from premiere_deck import EXTENSION_BUNDLE_ID
from premiere_deck.core.paths import (
    AppPaths, atomic_write_json, bundled_panel_dir, is_link_or_junction, read_json, remove_path,
)

log = logging.getLogger("premiere_deck.host.panel_install")

ENV_CEP_DIR = "PREMIERE_DECK_CEP_DIR"
VERSION_FILE = "VERSION"
MANIFEST = Path("CSXS") / "manifest.xml"
IGNORED_NAMES = {"deck.json", ".debug"}
CSXS_VERSIONS = (9, 10, 11, 12)
DEBUG_KEY = r"Software\Adobe\CSXS.{n}"
DEBUG_VALUE = "PlayerDebugMode"
STATE_FILE = "install.json"


def extensions_dir() -> Path:
    override = os.environ.get(ENV_CEP_DIR, "").strip()
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return Path(appdata) / "Adobe" / "CEP" / "extensions"


def default_target() -> Path:
    return extensions_dir() / EXTENSION_BUNDLE_ID


def read_version(folder: Path) -> str:
    """The `VERSION` file of a panel folder; "0" when missing or empty; None-safe."""
    try:
        text = (Path(folder) / VERSION_FILE).read_text("utf-8").strip()
    except OSError:
        return "0"
    return text or "0"


def file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def tree_hash(folder: Path) -> Optional[str]:
    """SHA-256 over relative paths and contents of every file (the ignored names left out); None when missing."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    digest = hashlib.sha256()
    files = []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for name in names:
            if name in IGNORED_NAMES:
                continue
            full = Path(root) / name
            files.append((full.relative_to(folder).as_posix(), full))
    for rel, full in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0")
        try:
            digest.update(full.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


# --- registry ------------------------------------------------------------------------------------------------------


class RegistryAccess(Protocol):
    def get(self, key: str, name: str) -> Optional[str]: ...
    def set(self, key: str, name: str, value: str) -> None: ...
    def delete(self, key: str, name: str) -> None: ...


class WinRegistry:
    """HKCU string values (the only hive the host writes)."""

    def get(self, key: str, name: str) -> Optional[str]:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                return str(value)
        except OSError:
            return None

    def set(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except OSError:
            pass


class MemoryRegistry:
    """Test double: {(key, name): value}."""

    def __init__(self, values: Optional[dict] = None) -> None:
        self.values: dict[tuple[str, str], str] = dict(values or {})

    def get(self, key: str, name: str) -> Optional[str]:
        return self.values.get((key, name))

    def set(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def delete(self, key: str, name: str) -> None:
        self.values.pop((key, name), None)


# --- installer -----------------------------------------------------------------------------------------------------


@dataclass
class InstallResult:
    action: str  # "none" | "installed" | "updated" | "skipped_junction" | "failed" | "no_source"
    version: str = "0"
    previous_version: Optional[str] = None
    manifest_changed: bool = False
    message: str = ""
    leftovers: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.action in ("installed", "updated")

    def as_dict(self) -> dict:
        return {"action": self.action, "version": self.version, "previous_version": self.previous_version,
                "manifest_changed": self.manifest_changed, "message": self.message}


class PanelInstaller:
    def __init__(self, paths: AppPaths, host_version: str, *, source: Optional[Path] = None,
                 target: Optional[Path] = None, settings: Any = None,
                 registry: Optional[RegistryAccess] = None) -> None:
        self.paths = paths
        self.host_version = host_version
        self.source = Path(source) if source else bundled_panel_dir()
        self.target = Path(target) if target else default_target()
        self.settings = settings
        self.registry: RegistryAccess = registry or WinRegistry()
        self.last_result: Optional[InstallResult] = None
        self._reload_sent: set[tuple[str, str]] = set()

    # -- facts
    @property
    def state_file(self) -> Path:
        return self.paths.panel_dir / STATE_FILE

    def source_version(self) -> str:
        return read_version(self.source)

    def installed_version(self) -> Optional[str]:
        return read_version(self.target) if self.target.is_dir() else None

    def is_junction(self) -> bool:
        return is_link_or_junction(self.target)

    def staged_dir(self) -> Path:
        return self.paths.staged_panel(self.source_version())

    def needs_install(self) -> bool:
        if self.is_junction():
            return False
        if not self.target.is_dir():
            return True
        staged = self.staged_dir()
        reference = staged if staged.is_dir() else self.source
        return tree_hash(self.target) != tree_hash(reference)

    def status(self) -> dict:
        """For the host window's Settings page (docs/BRIDGE_API.md 3.2)."""
        installed = self.installed_version()
        state = read_json(self.state_file, {}) or {}
        return {
            "target": str(self.target),
            "installed": installed is not None,
            "installed_version": installed,
            "bundled_version": self.source_version() if self.source.is_dir() else None,
            "host_version": self.host_version,
            "junction": self.is_junction(),
            "up_to_date": installed is not None and not self.needs_install(),
            "manifest_changed": bool(state.get("manifest_changed")),
            "debug_mode": self.debug_mode_state(),
            "last_result": self.last_result.as_dict() if self.last_result else None,
        }

    # -- staging and install
    def stage(self) -> Path:
        """Copy the bundled panel to `<data>\\panel\\<version>` unless an identical copy is there."""
        if not self.source.is_dir():
            raise FileNotFoundError(f"panel source folder missing: {self.source}")
        staged = self.staged_dir()
        if staged.is_dir() and tree_hash(staged) == tree_hash(self.source):
            return staged
        temp = staged.with_name(staged.name + ".part")
        remove_path(temp)
        shutil.copytree(self.source, temp, ignore=shutil.ignore_patterns(*IGNORED_NAMES, "__pycache__"))
        remove_path(staged)
        os.replace(temp, staged)
        log.info("staged panel %s in %s", self.source_version(), staged)
        return staged

    def ensure(self, force: bool = False) -> InstallResult:
        """Install or update when needed (or always with `force`, except over a junction). Never raises."""
        try:
            result = self._ensure(force)
        except Exception as exc:  # noqa: BLE001 - the host runs on; the window shows the reason
            log.exception("panel install failed")
            result = InstallResult("failed", self.source_version(), self.installed_version(),
                                   message=f"The panel could not be installed: {exc}")
        self.last_result = result
        return result

    def _ensure(self, force: bool) -> InstallResult:
        version = self.source_version()
        if not self.source.is_dir():
            return InstallResult("no_source", version, self.installed_version(),
                                 message="The panel files are missing from this build.")
        if self.is_junction():
            log.info("panel install skipped: %s is a junction to %s (development link, left untouched)",
                     self.target, os.path.realpath(self.target))
            return InstallResult("skipped_junction", version, self.installed_version(),
                                 message="The panel folder is linked to the source (development).")
        staged = self.stage()
        previous = self.installed_version()
        if not force and previous is not None and tree_hash(self.target) == tree_hash(staged):
            self._cleanup_leftovers()
            return InstallResult("none", version, previous)
        old_manifest = file_hash(self.target / MANIFEST) if previous is not None else None
        new_manifest = file_hash(staged / MANIFEST)
        leftovers = self._swap(staged)
        manifest_changed = previous is not None and old_manifest != new_manifest
        atomic_write_json(self.state_file, {
            "version": version, "previous_version": previous, "manifest_hash": new_manifest,
            "manifest_changed": manifest_changed, "installed_at": time.time(), "target": str(self.target)})
        action = "installed" if previous is None else "updated"
        log.info("panel %s %s in %s (manifest changed: %s)", version, action, self.target, manifest_changed)
        return InstallResult(action, version, previous, manifest_changed, leftovers=leftovers)

    def _swap(self, staged: Path) -> list:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        new = self.target.with_name(self.target.name + ".new")
        old = self.target.with_name(self.target.name + ".old")
        remove_path(new)
        shutil.copytree(staged, new)
        keep = self._carry_over()
        for name, data in keep.items():
            (new / name).write_bytes(data)
        if old.exists():
            remove_path(old)
            if old.exists():  # locked: move it aside, removed on a later run
                os.replace(old, old.with_name(f"{old.name}.{int(time.time())}"))
        moved = False
        if self.target.exists():
            os.replace(self.target, old)
            moved = True
        try:
            os.replace(new, self.target)
        except OSError:
            if moved:
                os.replace(old, self.target)  # undo
            raise
        leftovers = []
        if moved:
            remove_path(old)
            if old.exists():
                leftovers.append(str(old))
        return leftovers

    def _carry_over(self) -> dict:
        """`deck.json` of the running host survives a reinstall (the panel reads it on its next connect)."""
        keep = {}
        path = self.target / "deck.json"
        if path.is_file():
            try:
                keep["deck.json"] = path.read_bytes()
            except OSError:
                pass
        return keep

    def _cleanup_leftovers(self) -> None:
        parent = self.target.parent
        if not parent.is_dir():
            return
        for entry in parent.glob(self.target.name + ".old*"):
            remove_path(entry)

    # -- the welcome decision (docs/PROTOCOL.md 3)
    def update_for(self, panel_version: str, session_key: str = "") -> Optional[dict]:
        """`welcome.update` for a panel that reports `panel_version`: None when it runs the installed files."""
        if self.is_junction():
            return None
        installed = self.installed_version()
        if installed is None or str(panel_version) == installed:
            return None
        state = read_json(self.state_file, {}) or {}
        if state.get("manifest_changed"):
            return {"action": "restart_premiere"}
        key = (session_key, str(panel_version))
        if key in self._reload_sent:  # reloaded once and still old: only a restart helps
            return {"action": "restart_premiere"}
        self._reload_sent.add(key)
        return {"action": "reload"}

    # -- PlayerDebugMode fallback (4.3)
    def debug_mode_state(self) -> dict:
        written = list(self._written())
        enabled = [n for n in CSXS_VERSIONS if self.registry.get(DEBUG_KEY.format(n=n), DEBUG_VALUE) == "1"]
        return {"written_by_host": written, "enabled": [f"CSXS.{n}" for n in enabled]}

    def _written(self) -> list:
        if self.settings is None:
            return []
        value = self.settings.get("debug_mode_written", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    def enable_debug_mode(self) -> list:
        """Write PlayerDebugMode=1 where it is not set yet; returns the CSXS keys written. Call only after the
        user's confirmation."""
        written = self._written()
        for n in CSXS_VERSIONS:
            key = DEBUG_KEY.format(n=n)
            if self.registry.get(key, DEBUG_VALUE) == "1":
                continue
            self.registry.set(key, DEBUG_VALUE, "1")
            name = f"CSXS.{n}"
            if name not in written:
                written.append(name)
        if self.settings is not None:
            self.settings.set("debug_mode_written", written)
        log.info("PlayerDebugMode written for %s", written)
        return written

    def disable_debug_mode(self) -> list:
        """Remove exactly the values the host wrote; returns the CSXS keys removed."""
        removed = []
        for name in self._written():
            if not name.startswith("CSXS."):
                continue
            self.registry.delete("Software\\Adobe\\" + name, DEBUG_VALUE)
            removed.append(name)
        if self.settings is not None:
            self.settings.set("debug_mode_written", [])
        return removed

    # -- uninstall (4.4)
    def uninstall(self) -> dict:
        """Delete the target folder (only the link when it is a junction), its `.new` / `.old` leftovers and the
        registry values the host wrote."""
        target_removed = False
        if self.target.exists() or self.is_junction():
            remove_path(self.target)
            target_removed = not self.target.exists() and not self.is_junction()
        for suffix in (".new", ".old"):
            remove_path(self.target.with_name(self.target.name + suffix))
        self._cleanup_leftovers()
        removed = self.disable_debug_mode()
        remove_path(self.state_file)
        log.info("panel removed from %s (registry values removed: %s)", self.target, removed)
        return {"removed": target_removed, "registry_removed": removed}
