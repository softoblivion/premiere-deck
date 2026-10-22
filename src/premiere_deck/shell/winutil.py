"""Windows helpers used by the shell: the fixed-size frameless window (size lock, no maximize, DWM corners and
border colour), Explorer, single instance, message box.

Everything here is ctypes / stdlib only and degrades to a harmless no-op on non-Windows hosts, so the module can be
imported by the unit tests on any machine. The size choice (`pick_window_size`, `centered_rect`) is pure and tested
without a window.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

log = logging.getLogger("premiere_deck.shell.winutil")

IS_WINDOWS = sys.platform == "win32"

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_BORDER_COLOR = 34

SW_RESTORE = 9
SW_SHOW = 5

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WM_WINDOWPOSCHANGING = 0x0046
WM_SYSCOMMAND = 0x0112
WM_SIZE = 0x0005
WM_NCDESTROY = 0x0082
SIZE_MAXIMIZED = 2
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
MONITOR_DEFAULTTONEAREST = 2

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi")

    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    _user32.MessageBoxW.restype = ctypes.c_int

    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

    LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM]
    _user32.CallWindowProcW.restype = LRESULT
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HANDLE
    _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    _user32.GetDpiForWindow.restype = wintypes.UINT
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


# --- Explorer / shell --------------------------------------------------------------------------------------------

def reveal_in_explorer(path: str) -> bool:
    """Open Explorer with the file selected; a folder opens itself selected in its parent. False when missing."""
    if not path:
        return False
    path = os.path.normpath(path)
    if not os.path.exists(path):
        return False
    try:
        # One command-line string on purpose: explorer parses `/select,"<path>"` itself and rejects the argument
        # vector Python would build for a list. explorer.exe exits with 1 and is not waited for.
        subprocess.Popen(f'explorer /select,"{path}"', creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        log.exception("reveal_in_explorer failed for %s", path)
        return False


def open_path(path: str) -> bool:
    """Open a file or folder with the shell default handler."""
    if not path or not os.path.exists(path):
        return False
    try:
        os.startfile(path)  # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError):
        log.exception("open_path failed for %s", path)
        return False


def open_url(url: str) -> bool:
    """Open an https:// URL in the default browser. Anything else is refused."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return False
    try:
        os.startfile(parsed.geturl())  # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError):
        log.exception("open_url failed for %s", url)
        return False


def message_box(title: str, text: str, error: bool = False) -> None:
    """Blocking native message box, for failures before a window exists (a windowed exe has no console)."""
    if not IS_WINDOWS:
        print(f"{title}: {text}", file=sys.stderr)
        return
    _user32.MessageBoxW(None, text, title, MB_OK | (MB_ICONERROR if error else MB_ICONINFORMATION))


# --- single instance ---------------------------------------------------------------------------------------------

def acquire_single_instance(name: str):
    """Create the named mutex. Returns its handle (keep it alive), or None when another instance already owns it."""
    if not IS_WINDOWS:
        return object()
    handle = _kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not handle:
        return object()  # cannot decide; do not block the launch
    if err == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance(handle) -> None:
    if IS_WINDOWS and isinstance(handle, int) and handle:
        try:
            _kernel32.CloseHandle(handle)
        except OSError:
            pass


def find_window(title: str) -> int:
    if not IS_WINDOWS:
        return 0
    return _user32.FindWindowW(None, title) or 0


def bring_window_to_front(title: str, attempts: int = 20, delay: float = 0.1) -> bool:
    """Restore and focus the top-level window with this exact title (retried while the first instance starts)."""
    for _ in range(max(1, attempts)):
        hwnd = find_window(title)
        if hwnd:
            _user32.ShowWindow(hwnd, SW_RESTORE if _user32.IsIconic(hwnd) else SW_SHOW)
            return bool(_user32.SetForegroundWindow(hwnd))
        time.sleep(delay)
    return False


# --- DWM ---------------------------------------------------------------------------------------------------------

def set_rounded_corners(hwnd: int) -> bool:
    """Windows 11 rounded corners for a frameless window. Harmless (returns False) on older systems."""
    if not IS_WINDOWS or not hwnd:
        return False
    value = ctypes.c_int(DWMWCP_ROUND)
    hr = _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0


def colorref(hex_color: str) -> int:
    """'#rrggbb' -> COLORREF 0x00BBGGRR."""
    text = hex_color.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #rrggbb colour: {hex_color!r}")
    r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return (b << 16) | (g << 8) | r


def set_border_color(hwnd: int, hex_color: str) -> bool:
    """Windows 11 window border colour (DWMWA_BORDER_COLOR); the page background hides the light frame line.
    Harmless (returns False) before Windows 11 build 22000."""
    if not IS_WINDOWS or not hwnd:
        return False
    value = wintypes.DWORD(colorref(hex_color))
    hr = _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0


# --- window size (pure) ------------------------------------------------------------------------------------------

def pick_window_size(ladder: Sequence[tuple[int, int]], work: Optional[tuple[int, int]]) -> tuple[int, int]:
    """The largest size of `ladder` (largest first, logical px) that fits the work area `work` (logical px); the
    smallest step when none fits, the first one when the work area is unknown."""
    if not ladder:
        raise ValueError("empty size ladder")
    if work is None:
        return ladder[0]
    for width, height in ladder:
        if width <= work[0] and height <= work[1]:
            return width, height
    return ladder[-1]


def centered_rect(size: tuple[int, int], work: Optional[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """(x, y, w, h) of a window of `size` centred on the work area (left, top, right, bottom), kept inside it when
    it fits; (0, 0, w, h) when the work area is unknown. Physical px."""
    w, h = size
    if work is None:
        return 0, 0, w, h
    x = work[0] + ((work[2] - work[0]) - w) // 2
    y = work[1] + ((work[3] - work[1]) - h) // 2
    return max(work[0], x), max(work[1], y), w, h


# --- fixed-size frameless window ---------------------------------------------------------------------------------
#
# A frameless, non-resizable pywebview form (WinForms FormBorderStyle.None) has no sizing border, but a script or the
# shell can still resize or maximize it: the style keeps the WS_MAXIMIZEBOX bit, WM_SYSCOMMAND SC_MAXIMIZE and
# ShowWindow(SW_MAXIMIZE) both fill the work area, and SetWindowPos accepts any size (measured with pywebview 6.2.1).
# `FixedSizeHook` subclasses the window procedure: every size proposal of the restored or maximized window is
# replaced by the fixed size, SC_MAXIMIZE is swallowed, a maximized state that still arrives (ShowWindow(SW_MAXIMIZE)
# from outside) is undone with SC_RESTORE, WS_MAXIMIZEBOX / WS_THICKFRAME are cleared and WS_MINIMIZEBOX is set
# (taskbar click and Win+Down minimize). Minimizing and restoring keep the size without help.

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


WM_COPYDATA = 0x004A
COPYDATA_MAGIC = 0x50444B31  # "PDK1": marks Premiere Deck's own second-instance messages
SMTO_ABORTIFHUNG = 0x0002


class _COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


def send_copydata(hwnd: int, text: str, timeout_ms: int = 3000) -> bool:
    """Send UTF-8 `text` to the first instance's window (WM_COPYDATA); True when it answered."""
    if not IS_WINDOWS or not hwnd:
        return False
    raw = text.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    data = _COPYDATASTRUCT(COPYDATA_MAGIC, len(raw), ctypes.cast(buffer, ctypes.c_void_p))
    result = ctypes.c_size_t(0)
    send = _user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                     ctypes.POINTER(ctypes.c_size_t)]
    send.restype = ctypes.c_size_t
    ok = send(hwnd, WM_COPYDATA, 0, ctypes.addressof(data), SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    return bool(ok) and result.value == 1


def find_window_retry(title: str, attempts: int = 30, delay: float = 0.1) -> int:
    for _ in range(max(1, attempts)):
        hwnd = find_window(title)
        if hwnd:
            return hwnd
        time.sleep(delay)
    return 0


class _WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", wintypes.HWND), ("x", ctypes.c_int),
                ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


def monitor_work_area(hwnd) -> Optional[tuple[int, int, int, int]]:
    """(left, top, right, bottom) of the work area of the monitor the window is on (physical px), or None."""
    if not IS_WINDOWS or not hwnd:
        return None
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    w = info.rcWork
    return w.left, w.top, w.right, w.bottom


def window_scale(hwnd) -> float:
    """DPI scale of the window (1.0 at 96 dpi)."""
    if not IS_WINDOWS or not hwnd:
        return 1.0
    try:
        return (_user32.GetDpiForWindow(hwnd) or 96) / 96.0
    except (AttributeError, OSError):
        return 1.0


class FixedSizeHook:
    """Window-procedure subclass that holds the window at one physical size; keep the instance alive as long as
    the window exists."""

    def __init__(self, hwnd: int, on_copydata: Optional[Callable[[str], None]] = None) -> None:
        self.hwnd = hwnd
        self.on_copydata = on_copydata  # second-instance messages (send_copydata), UTF-8 text
        self.size: Optional[tuple[int, int]] = None  # physical px; None = not locked yet
        self._prev_proc = None
        self._proc = None
        self.installed = False

    def install(self) -> bool:
        if not IS_WINDOWS or not self.hwnd or self.installed:
            return self.installed
        style = _user32.GetWindowLongPtrW(self.hwnd, GWL_STYLE) or 0
        style = (style | WS_MINIMIZEBOX) & ~(WS_MAXIMIZEBOX | WS_THICKFRAME)
        _user32.SetWindowLongPtrW(self.hwnd, GWL_STYLE, style)
        self._proc = _WNDPROC(self._wnd_proc)
        self._prev_proc = _user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC,
                                                    ctypes.cast(self._proc, ctypes.c_void_p).value)
        _user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                             SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        self.installed = bool(self._prev_proc)
        return self.installed

    def fit_size(self, ladder: Sequence[tuple[int, int]]) -> tuple[int, int]:
        """Logical size from `ladder` for the work area of the window's monitor at the window's DPI."""
        scale = window_scale(self.hwnd)
        work = monitor_work_area(self.hwnd)
        work_logical = None
        if work is not None:
            work_logical = (int((work[2] - work[0]) / scale), int((work[3] - work[1]) / scale))
        return pick_window_size(ladder, work_logical)

    def lock(self, width: int, height: int,
             apply: Optional[Callable[[int, int, int, int], None]] = None) -> tuple[int, int, int, int]:
        """Fix the window at `width` x `height` logical px, centred on the work area. `apply(x, y, w, h)` performs
        the move (pass the form's SetBounds so WinForms keeps its own bounds in sync); the default is SetWindowPos.
        Returns the physical rect (x, y, w, h)."""
        scale = window_scale(self.hwnd)
        self.size = (int(round(width * scale)), int(round(height * scale)))
        x, y, w, h = centered_rect(self.size, monitor_work_area(self.hwnd))
        if apply is not None:
            apply(x, y, w, h)
        elif IS_WINDOWS and self.hwnd:
            _user32.SetWindowPos(self.hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        return x, y, w, h

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WINDOWPOSCHANGING and self.size is not None:
                pos = ctypes.cast(lparam, ctypes.POINTER(_WINDOWPOS)).contents
                if not (pos.flags & SWP_NOSIZE) and pos.x > -32000 and not _user32.IsIconic(hwnd):
                    pos.cx, pos.cy = self.size
            elif msg == WM_SYSCOMMAND and (int(wparam) & 0xFFF0) == SC_MAXIMIZE:
                return 0  # system menu, keyboard
            elif msg == WM_COPYDATA and self.on_copydata is not None:
                data = ctypes.cast(lparam, ctypes.POINTER(_COPYDATASTRUCT)).contents
                if data.dwData == COPYDATA_MAGIC and data.lpData and 0 < data.cbData <= 65536:
                    text = ctypes.string_at(data.lpData, data.cbData).decode("utf-8", errors="replace")
                    self.on_copydata(text)
                    return 1
            elif msg == WM_SIZE and int(wparam) == SIZE_MAXIMIZED:
                _user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
            elif msg == WM_NCDESTROY:
                result = _user32.CallWindowProcW(self._prev_proc, hwnd, msg, wparam, lparam)
                self.installed = False
                return result
        except Exception:  # noqa: BLE001 - never let a callback exception escape into the message loop
            log.debug("window procedure hook failed for message 0x%04x", msg, exc_info=True)
        return _user32.CallWindowProcW(self._prev_proc, hwnd, msg, wparam, lparam)
