"""Location of the runtime data folder and atomic file writes (docs/ARCHITECTURE.md, section 6).

Nothing outside this module builds a path inside the data folder. The environment is read when `AppPaths` is
created, so tests set `PREMIERE_DECK_HOME` before they build one.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

APP_DIR_NAME = "premiere-deck_data"
ENV_HOME = "PREMIERE_DECK_HOME"
EXE_NAME = "PremiereDeck.exe"


def app_dir() -> Path:
    """Folder of the exe when frozen, else the project root (the parent of `src`); never the extraction folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def package_dir() -> Path:
    """`premiere_deck/` itself: the source folder, or the same layout inside PyInstaller's extraction folder."""
    return Path(__file__).resolve().parent.parent


def bundled_panel_dir() -> Path:
    """The panel folder shipped with the host (`premiere_deck/panel`, signed copy in a frozen build)."""
    return package_dir() / "panel"


def running_exe() -> Optional[Path]:
    """The packaged exe, or None when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


class AppPaths:
    """Resolved paths of the data folder. `root` and `home` are the same folder (base.PathsLike uses `root`)."""

    def __init__(self, home: Path | str | None = None) -> None:
        if home is None:
            env = os.environ.get(ENV_HOME, "").strip()
            home = Path(env) if env else app_dir() / APP_DIR_NAME
        self.home: Path = Path(home).resolve()
        self.root: Path = self.home
        self.settings_file: Path = self.home / "settings.json"
        self.bin_dir: Path = self.home / "bin"
        self.versions_file: Path = self.bin_dir / "versions.json"
        self.panel_dir: Path = self.home / "panel"
        self.logs_dir: Path = self.home / "logs"
        self.log_dir: Path = self.logs_dir  # alias used by the shell
        self.host_log: Path = self.logs_dir / "host.log"
        self.job_logs_dir: Path = self.logs_dir / "jobs"
        self.webview_dir: Path = self.home / "webview"
        self.updates_dir: Path = self.home / "updates"
        self.cache_root: Path = self.home / "cache"
        exe = running_exe()
        self.exe_path: Optional[str] = str(exe) if exe else None

    def ensure(self) -> None:
        """Create the data folder and its sub-folders."""
        for folder in (self.home, self.bin_dir, self.logs_dir, self.job_logs_dir, self.webview_dir, self.updates_dir,
                       self.cache_root):
            folder.mkdir(parents=True, exist_ok=True)

    def cache_dir(self, module_id: str) -> Path:
        """`cache/<module_id>`, created on demand. Module ids are folder names (base.Module.id)."""
        if not module_id or any(ch in module_id for ch in '\\/:*?"<>|') or module_id in (".", ".."):
            raise ValueError(f"not a module id: {module_id!r}")
        folder = self.cache_root / module_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def binary(self, name: str) -> Path:
        return self.bin_dir / name

    def remove_retired_data(self) -> list[Path]:
        """Delete data folders of removed features (`models/`, `cache/search/` of versions before 2.0); returns what
        existed. Errors are ignored. A project's own `.premiere-deck/` folders are never touched."""
        removed: list[Path] = []
        for folder in (self.home / "models", self.cache_root / "search"):
            if folder.exists():
                remove_path(folder)
                removed.append(folder)
        return removed

    def job_log(self, job_id: str) -> Path:
        self.job_logs_dir.mkdir(parents=True, exist_ok=True)
        return self.job_logs_dir / f"{job_id}.log"

    def staged_panel(self, version: str) -> Path:
        """`panel/<version>`: one staged copy of the bundled panel per host version (docs/PANEL.md 4.1)."""
        return self.panel_dir / version


def remove_path(path: Path) -> None:
    """Delete a file, a folder tree or a junction; missing paths and locked files are ignored."""
    path = Path(path)
    try:
        if is_link_or_junction(path):
            os.rmdir(path) if path.is_dir() else path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_link_or_junction(path: Path) -> bool:
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return False


def replace_file(source: Path, target: Path) -> None:
    """os.replace with a short retry: Windows refuses while another process (scanner, indexer) holds the target."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `<name>.part` next to `path`, then replace `path` with it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with open(part, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(part, path)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """UTF-8 without BOM; line endings kept as given."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    """Parsed JSON, or `default` when the file is missing or broken (a hand-edited file never crashes the host)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default
