"""Premiere Pro discovery, launch and process watch (docs/HOST_UI.md 3.1, 3.5).

Discovery, newest first (the setting `premiere_exe` overrides when it names an existing file):
1. `HKLM\\SOFTWARE\\Adobe\\Premiere Pro\\<version>`: an `InstallPath` value when present, else the folder
   `...\\Adobe Premiere Pro <year>` found inside the path values of that key (Premiere 2026 writes
   `PluginInstallPath`, `SequencePresetsPath`, ... but no install path).
2. `C:\\Program Files\\Adobe\\Adobe Premiere Pro *\\` (also `%ProgramW6432%` / `%ProgramFiles%`).
Recent projects come from Premiere's preferences (`MRU.Document.<n>` in `Adobe Premiere Pro Prefs`).

Nothing here starts Premiere except `launch()`; tests call the discovery functions with fake roots only.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from premiere_deck.core import procs

log = logging.getLogger("premiere_deck.host.launcher")

EXE_NAME = "Adobe Premiere Pro.exe"
REG_KEY = r"SOFTWARE\Adobe\Premiere Pro"
_FOLDER_RE = re.compile(r"^(.*\\Adobe Premiere Pro[^\\]*)(\\|$)", re.I)
_YEAR_RE = re.compile(r"Adobe Premiere Pro (?:CC )?(\d{4})", re.I)
# Premiere 2026 writes `<BE.Prefs.MRU.Document.1>path</BE.Prefs.MRU.Document.1>`; the prefix is optional here.
_MRU_RE = re.compile(r"<((?:[\w.]+\.)?MRU\.Document\.(\d+))>([^<]+)</\1>")
_MRU_DATE_RE = re.compile(r"<((?:[\w.]+\.)?MRU\.Document\.Date\.(\d+))>([^<]+)</\1>")


@dataclass(frozen=True)
class PremiereInstall:
    exe: str
    version: str   # registry version ("26.0") or the folder year ("2026"); "" when unknown
    source: str    # "setting" | "registry" | "folder"

    def as_dict(self) -> dict:
        return {"exe": self.exe, "version": self.version, "source": self.source}


def _version_key(text: str) -> tuple:
    parts = re.findall(r"\d+", text or "")
    numbers = tuple(int(p) for p in parts)
    if numbers and numbers[0] >= 2000:  # a folder year (2026) ranks with its major (26)
        numbers = (numbers[0] - 2000,) + numbers[1:]
    return numbers


def install_folder_from_value(value: str) -> Optional[str]:
    """`C:\\...\\Adobe Premiere Pro 2026` from any path inside it, or None."""
    match = _FOLDER_RE.match(str(value or ""))
    return match.group(1) if match else None


def registry_installs(read_key: Optional[Callable[[], dict]] = None) -> list[PremiereInstall]:
    """Installs named by the registry. `read_key()` returns {version: {value name: value}} (tests pass one)."""
    entries = read_key() if read_key is not None else _read_registry()
    found: list[PremiereInstall] = []
    for version, values in entries.items():
        if not re.match(r"^\d+(\.\d+)*$", version):
            continue
        folders = []
        if values.get("InstallPath"):
            folders.append(str(values["InstallPath"]))
        for value in values.values():
            folder = install_folder_from_value(str(value)) if isinstance(value, str) else None
            if folder and folder not in folders:
                folders.append(folder)
        for folder in folders:
            exe = os.path.join(folder, EXE_NAME)
            if os.path.isfile(exe):
                found.append(PremiereInstall(exe, version, "registry"))
                break
    return found


def _read_registry() -> dict:
    if sys.platform != "win32":
        return {}
    import winreg

    result: dict[str, dict] = {}
    for view in (winreg.KEY_WOW64_64KEY, 0):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                values: dict[str, object] = {}
                try:
                    with winreg.OpenKey(root, name) as sub:
                        v = 0
                        while True:
                            try:
                                vname, vdata, _ = winreg.EnumValue(sub, v)
                            except OSError:
                                break
                            values[vname] = vdata
                            v += 1
                except OSError:
                    continue
                result.setdefault(name, values)
        if result:
            break
    return result


def folder_installs(roots: Optional[Iterable[str]] = None) -> list[PremiereInstall]:
    if roots is None:
        roots = []
        for env in ("ProgramW6432", "ProgramFiles"):
            value = os.environ.get(env)
            if value and value not in roots:
                roots.append(value)
        if r"C:\Program Files" not in roots:
            roots.append(r"C:\Program Files")
    found: list[PremiereInstall] = []
    for root in roots:
        for folder in glob.glob(os.path.join(root, "Adobe", "Adobe Premiere Pro*")):
            exe = os.path.join(folder, EXE_NAME)
            if os.path.isfile(exe):
                match = _YEAR_RE.search(folder)
                found.append(PremiereInstall(exe, match.group(1) if match else "", "folder"))
    return found


def find_premiere(override: Optional[str] = None, *, read_key: Optional[Callable[[], dict]] = None,
                  roots: Optional[Iterable[str]] = None) -> list[PremiereInstall]:
    """Every install found, the override first, then newest first; duplicates (same exe) removed."""
    installs: list[PremiereInstall] = []
    if override and override != "auto" and os.path.isfile(override):
        installs.append(PremiereInstall(os.path.normpath(override), "", "setting"))
    discovered = registry_installs(read_key) + folder_installs(roots)
    discovered.sort(key=lambda i: _version_key(i.version), reverse=True)
    seen = {os.path.normcase(i.exe) for i in installs}
    for install in discovered:
        key = os.path.normcase(os.path.normpath(install.exe))
        if key not in seen:
            seen.add(key)
            installs.append(install)
    return installs


def best_premiere(override: Optional[str] = None, **kw) -> Optional[PremiereInstall]:
    installs = find_premiere(override, **kw)
    return installs[0] if installs else None


# --- recent projects ----------------------------------------------------------------------------------------------

def prefs_files(documents: Optional[str] = None) -> list[Path]:
    """`Adobe Premiere Pro Prefs` files of every Premiere version, newest version first."""
    base = Path(documents or os.path.join(os.path.expanduser("~"), "Documents")) / "Adobe" / "Premiere Pro"
    files = sorted(base.glob("*/Profile-*/Adobe Premiere Pro Prefs"), key=lambda p: _version_key(p.parent.parent.name),
                   reverse=True)
    return files


def parse_mru(text: str) -> list[dict]:
    """`[{path, opened_at}]` from the prefs XML, in Premiere's MRU order; `\\\\?\\` prefixes removed."""
    dates = {int(n): d.strip() for _, n, d in _MRU_DATE_RE.findall(text)}
    entries = []
    for _, number, raw in sorted(_MRU_RE.findall(text), key=lambda e: int(e[1])):
        path = raw.strip()
        if path.startswith("\\\\?\\"):
            path = path[4:]
        if path.lower().endswith(".prproj"):
            entries.append({"path": path, "opened_at": dates.get(int(number))})
    return entries


def premiere_recent_projects(documents: Optional[str] = None, limit: int = 10) -> list[dict]:
    for prefs in prefs_files(documents):
        try:
            text = prefs.read_text("utf-8", errors="replace")
        except OSError:
            continue
        entries = parse_mru(text)
        if entries:
            return entries[:limit]
    return []


# --- processes ----------------------------------------------------------------------------------------------------

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


def process_ids(image_name: str = EXE_NAME) -> list[int]:
    """PIDs of running processes with this image name (Toolhelp snapshot; no child process)."""
    if sys.platform != "win32":
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        target = image_name.lower()
        while ok:
            if entry.szExeFile.lower() == target:
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


def is_premiere_running() -> bool:
    return bool(process_ids())


def launch(exe: str, project: Optional[str] = None) -> int:
    """Start Premiere Pro (optionally with a project); returns the PID. Raises OSError / FileNotFoundError."""
    if not exe or not os.path.isfile(exe):
        raise FileNotFoundError("Premiere Pro was not found.")
    args = [exe]
    if project:
        if not os.path.isfile(project):
            raise FileNotFoundError("The project file was not found.")
        # Premiere 26.5 ignores a project argument with forward slashes (verified live 2026-10-22): the file
        # picker yields backslashes, but recent-project entries and the command line may not.
        args.append(os.path.normpath(project))
    proc = procs.launch_detached(args, cwd=os.path.dirname(exe))
    log.info("launched Premiere Pro (pid %s)%s", proc.pid, f" with {project}" if project else "")
    return proc.pid


class ProcessWatch:
    """Polls the process list every `interval` seconds; `on_change(running)` on every change (watch thread)."""

    def __init__(self, on_change: Callable[[bool], None], interval: float = 2.0,
                 probe: Callable[[], bool] = is_premiere_running) -> None:
        self._on_change = on_change
        self._interval = interval
        self._probe = probe
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running: Optional[bool] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="premiere-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)

    def poll(self) -> bool:
        try:
            running = bool(self._probe())
        except Exception:  # noqa: BLE001
            log.debug("process probe failed", exc_info=True)
            running = bool(self.running)
        if running != self.running:
            self.running = running
            try:
                self._on_change(running)
            except Exception:  # noqa: BLE001
                log.exception("process watch callback failed")
        return running

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self._interval)
