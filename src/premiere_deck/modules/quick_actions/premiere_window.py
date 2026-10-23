"""Premiere's own menu commands that ExtendScript cannot reach (docs/PREMIERE_API.md 7.18): the main window's native
menu bar is read with the Win32 menu API and the commands are posted to the window as `WM_COMMAND`, which runs them
like menu picks. ExtendScript 26.5.2 has no `app.executeCommand`, and a synthetic Ctrl+M is swallowed while the Deck
panel has the keyboard focus.

Commands that act on the focused panel (Edit > Label) need the Timeline focused first: Window > Timelines >
<sequence name> does that (a posted command, so it runs before the next one); `Project.openSequence` does not move
the focus."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

WINDOW_TITLE = "Adobe Premiere"
WM_COMMAND = 0x0111
MF_BYPOSITION = 0x400
MF_BYCOMMAND = 0x0
MISSING = (-1, 0xFFFFFFFF)

# Command ids read from the menu bar of Premiere Pro 26.5.2; the label path is the fallback when an id moves.
EXPORT_MEDIA_ID = 11240
EXPORT_MEDIA_PATH = ("File", "Export", "Media...")
LABEL_FIRST_ID = 13124  # Edit > Label > Violet; the 16 colours follow in label order
LABEL_MENU = ("Edit", "Label")
TIMELINES_MENU = ("Window", "Timelines")


@dataclass(frozen=True)
class Command:
    """One menu pick: the command id when the menu bar holds it, else the item at `path`; an optional command that
    is not found is skipped, a required one makes the whole run fail before anything is posted."""

    command: Optional[int]
    path: tuple[str, ...]
    required: bool = True


def menu_label(raw: str) -> str:
    """A menu label without mnemonic ampersands and the accelerator ("&Media...\\tCtrl+M" -> "Media...")."""
    return raw.split("\t", 1)[0].replace("&", "").strip()


def export_commands(sequence_name: str) -> list[Command]:
    """Timeline of the sequence to the front (optional), then File > Export > Media..."""
    return [Command(None, TIMELINES_MENU + (sequence_name,), required=False),
            Command(EXPORT_MEDIA_ID, EXPORT_MEDIA_PATH)]


def label_commands(sequence_name: str, label_index: int, label_name: str) -> list[Command]:
    """Timeline of the sequence focused, then Edit > Label > <colour> on its selected clips."""
    return [Command(None, TIMELINES_MENU + (sequence_name,)),
            Command(LABEL_FIRST_ID + label_index, LABEL_MENU + (label_name,))]


def run(commands: list[Command]) -> bool:
    """Posts the commands to Premiere's main window in order. False when no Premiere window is found or a required
    command is missing from its menu bar."""
    if sys.platform != "win32":
        return False
    return _post(commands)  # pragma: no cover - Win32 only


def open_export_mode(sequence_name: str) -> bool:
    return run(export_commands(sequence_name))


def label_timeline_selection(sequence_name: str, label_index: int, label_name: str) -> bool:
    return run(label_commands(sequence_name, label_index, label_name))


# --- Win32 ---------------------------------------------------------------------------------------------------------


def _post(commands: list[Command]) -> bool:  # pragma: no cover - Win32 only
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetMenuState.restype = wt.UINT
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    windows: list[int] = []

    def visit(hwnd, _param):
        if user32.IsWindowVisible(hwnd) and user32.GetMenu(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.startswith(WINDOW_TITLE):
                windows.append(hwnd)
        return True

    user32.EnumWindows(proc(visit), 0)

    def find(menu, path: tuple[str, ...]) -> Optional[int]:
        wanted = path[0].replace("&", "")
        for pos in range(max(0, user32.GetMenuItemCount(menu))):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetMenuStringW(menu, pos, buffer, 512, MF_BYPOSITION)
            if menu_label(buffer.value) != wanted:
                continue
            if len(path) == 1:
                command = user32.GetMenuItemID(menu, pos)
                return None if command in MISSING else int(command)
            sub = user32.GetSubMenu(menu, pos)
            return find(sub, path[1:]) if sub else None
        return None

    for hwnd in windows:
        menu = user32.GetMenu(hwnd)
        targets: list[int] = []
        for step in commands:
            target = None
            if step.command is not None and user32.GetMenuState(menu, step.command, MF_BYCOMMAND) not in MISSING:
                target = step.command
            if target is None:
                target = find(menu, step.path)
            # Menu states are refreshed only when a menu opens, so a greyed flag here may be stale: not checked.
            if target is None:
                if step.required:
                    targets = []
                    break
                continue
            targets.append(target)
        if targets and all(user32.PostMessageW(hwnd, WM_COMMAND, target, 0) for target in targets):
            return True
    return False
