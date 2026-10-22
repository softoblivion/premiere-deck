"""Tray icon of the host (docs/HOST_UI.md 1): WinForms `NotifyIcon` through pythonnet (already loaded by
pywebview's WinForms backend), so no extra dependency. Create and dispose it on the WinForms UI thread.

Menu: Open, Launch Premiere Pro, Quit; a double click opens the window.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger("premiere_deck.shell.tray")


class Tray:
    def __init__(self, title: str, icon_path: Optional[str], on_open: Callable[[], None],
                 on_launch: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self.title = title
        self.icon_path = icon_path
        self._on_open = on_open
        self._on_launch = on_launch
        self._on_quit = on_quit
        self._icon = None
        self._handlers: list = []  # delegates must stay referenced

    def create(self) -> bool:
        """UI thread only. Returns False when WinForms is not available (tests, non-Windows)."""
        try:
            import clr  # noqa: F401  (pythonnet)
            from System import EventHandler
            from System.Drawing import Icon, SystemIcons
            from System.Windows.Forms import ContextMenuStrip, MouseEventHandler, NotifyIcon, ToolStripMenuItem
        except Exception:  # noqa: BLE001
            log.warning("tray icon not available", exc_info=True)
            return False
        icon = NotifyIcon()
        try:
            icon.Icon = Icon(self.icon_path) if self.icon_path else SystemIcons.Application
        except Exception:  # noqa: BLE001
            icon.Icon = SystemIcons.Application
        icon.Text = self.title[:63]
        menu = ContextMenuStrip()
        for label, fn in (("Open", self._on_open), ("Launch Premiere Pro", self._on_launch), ("Quit", self._on_quit)):
            item = ToolStripMenuItem(label)
            handler = EventHandler(lambda _s, _e, f=fn: self._safe(f))
            self._handlers.append(handler)
            item.Click += handler
            menu.Items.Add(item)
        icon.ContextMenuStrip = menu
        double = MouseEventHandler(lambda _s, _e: self._safe(self._on_open))
        self._handlers.append(double)
        icon.MouseDoubleClick += double
        icon.Visible = True
        self._icon = icon
        return True

    def notify(self, text: str, timeout_ms: int = 3000) -> None:
        """A balloon line (UI thread)."""
        if self._icon is None:
            return
        try:
            self._icon.BalloonTipTitle = self.title
            self._icon.BalloonTipText = text
            self._icon.ShowBalloonTip(timeout_ms)
        except Exception:  # noqa: BLE001
            log.debug("balloon failed", exc_info=True)

    def dispose(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.Visible = False
            self._icon.Dispose()
        except Exception:  # noqa: BLE001
            log.debug("tray dispose failed", exc_info=True)
        self._icon = None

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a menu click must not crash the message loop
            log.exception("tray action failed")
