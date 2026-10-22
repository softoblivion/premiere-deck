"""Shared helpers of the host tests (no test cases here).

Every scratch folder lives under `PREMIERE_DECK_HOME` (set by the runner) or a temp folder; the CEP extensions
folder of a test is always a scratch folder (`PREMIERE_DECK_CEP_DIR`), never Adobe's.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path


def scratch_dir(name: str) -> Path:
    base = os.environ.get("PREMIERE_DECK_HOME") or tempfile.gettempdir()
    folder = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=_ensure(Path(base) / "tests")))
    return folder


def _ensure(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def remove_tree(path: Path) -> None:
    """rmtree with retries (Windows keeps files locked for a moment after handles close)."""
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(0.05)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def free_ports(count: int = 3) -> tuple[int, ...]:
    return tuple(free_port() for _ in range(count))


def wait_for(predicate, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def make_panel_source(folder: Path, version: str = "1.0.0", manifest: str = "<manifest v='1'/>",
                      extra: str = "one") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "CSXS").mkdir(exist_ok=True)
    (folder / "CSXS" / "manifest.xml").write_text(manifest, "utf-8")
    (folder / "index.html").write_text(f"<html>{extra}</html>", "utf-8")
    if version is not None:
        (folder / "VERSION").write_text(version, "utf-8")
    return folder


logging.getLogger("premiere_deck").setLevel(logging.CRITICAL)
logging.getLogger("module").setLevel(logging.CRITICAL)
logging.getLogger("websockets").setLevel(logging.CRITICAL)
