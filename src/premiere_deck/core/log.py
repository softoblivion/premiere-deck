"""Logging setup: `logs/host.log` (rotating 1 MB x 5) for the host, the modules and the panel loggers.

Logger names: `premiere_deck.*` (host code), `module.<id>` (modules, docs/MODULES.md 2), `panel.agent` and
`panel.display` (the `log` messages of the panel, docs/PROTOCOL.md 5), `pywebview`.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

LOG_NAME = "host.log"
ROOT_LOGGERS = ("premiere_deck", "module", "panel", "pywebview", "websockets")
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"

_handlers: list[logging.Handler] = []


def setup_logging(logs_dir: Path, debug: bool = False, console: Optional[bool] = None) -> Optional[Path]:
    """Attach the rotating file handler (and stderr with `debug`). Returns the log file path or None. Idempotent."""
    shutdown_logging()
    fmt = logging.Formatter(FORMAT)
    path: Optional[Path] = None
    try:
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
        path = Path(logs_dir) / LOG_NAME
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8",
                                                       delay=True)
        _handlers.append(handler)
    except OSError:
        path = None
    if (debug if console is None else console) and sys.stderr is not None:
        _handlers.append(logging.StreamHandler(sys.stderr))
    for handler in _handlers:
        handler.setFormatter(fmt)
    for name in ROOT_LOGGERS:
        logger = logging.getLogger(name)
        if name in ("pywebview", "websockets"):
            logger.setLevel(logging.WARNING)
        else:
            logger.setLevel(logging.DEBUG if debug else logging.INFO)
        logger.propagate = False
        for handler in _handlers:
            logger.addHandler(handler)
    _install_hooks()
    return path


def shutdown_logging() -> None:
    """Detach and close the handlers (tests remove their scratch folder afterwards)."""
    for handler in list(_handlers):
        for name in ROOT_LOGGERS:
            logging.getLogger(name).removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    _handlers.clear()


def _install_hooks() -> None:
    crash = logging.getLogger("premiere_deck.crash")

    def excepthook(exc_type, exc, tb):
        crash.critical("unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        crash.critical("unhandled exception in thread %s", getattr(args.thread, "name", "?"),
                       exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
