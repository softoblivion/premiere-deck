"""Windows helpers used by the shell: the fixed-size frameless window (size lock, no maximize, DWM corners and
border colour), the animated change between its two locked sizes, Explorer, single instance, message box.

Everything here is ctypes / stdlib only and degrades to a harmless no-op on non-Windows hosts, so the module can be
imported by the unit tests on any machine. The size math (`pick_window_size`, `centered_rect`, `rect_toward`,
`resize_frames`, `cubic_bezier`) and the `SizeAnimator` (driven through callables) are tested without a window.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
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
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

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
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _user32.keybd_event.restype = None
    _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    _user32.MessageBoxW.restype = ctypes.c_int

    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

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
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL


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


class _ForegroundApi:
    """The user32 / kernel32 calls `raise_window` makes, as one object so the call sequence can be tested."""

    def is_iconic(self, hwnd: int) -> bool:
        return bool(_user32.IsIconic(hwnd))

    def show_window(self, hwnd: int, cmd: int) -> None:
        _user32.ShowWindow(hwnd, cmd)

    def foreground_window(self) -> int:
        return _user32.GetForegroundWindow() or 0

    def window_thread(self, hwnd: int) -> int:
        return int(_user32.GetWindowThreadProcessId(hwnd, None) or 0)

    def current_thread(self) -> int:
        return int(_kernel32.GetCurrentThreadId())

    def attach_thread_input(self, source: int, target: int, attach: bool) -> bool:
        return bool(_user32.AttachThreadInput(source, target, attach))

    def key_event(self, vk: int, flags: int) -> None:
        _user32.keybd_event(vk, 0, flags, 0)

    def bring_to_top(self, hwnd: int) -> None:
        _user32.BringWindowToTop(hwnd)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(_user32.SetForegroundWindow(hwnd))


def raise_window(hwnd: int, api) -> bool:
    """Put `hwnd` in front of the foreground application of another process (Premiere Pro).

    A background process cannot take the foreground with a plain `SetForegroundWindow` (Windows' foreground lock
    refuses it and only flashes the taskbar button). The accepted workaround: restore a minimised window, attach
    this thread's input queue to the foreground window's thread for the call, `BringWindowToTop`, then
    `SetForegroundWindow`; when that is still refused, tap the Alt key (a synthetic key press makes the calling
    process eligible) and try once more. The input attachment is always undone. A window that is hidden is shown
    first; nothing here raises. Returns whether the window ended up in front.
    """
    if not hwnd:
        return False
    try:
        api.show_window(hwnd, SW_RESTORE if api.is_iconic(hwnd) else SW_SHOW)
        foreground = api.foreground_window()
        if foreground == hwnd:
            return True
        mine = api.current_thread()
        theirs = api.window_thread(foreground) if foreground else 0
        attached = bool(theirs) and theirs != mine and api.attach_thread_input(mine, theirs, True)
        try:
            api.bring_to_top(hwnd)
            done = api.set_foreground(hwnd)
        finally:
            if attached:
                api.attach_thread_input(mine, theirs, False)
        if not done and api.foreground_window() != hwnd:
            api.key_event(VK_MENU, 0)
            api.key_event(VK_MENU, KEYEVENTF_KEYUP)
            api.bring_to_top(hwnd)
            done = api.set_foreground(hwnd)
        return bool(done) or api.foreground_window() == hwnd
    except Exception:  # noqa: BLE001 - a refused raise must never break "open the window"
        log.debug("raise_window failed", exc_info=True)
        return False


def bring_window_to_front(title: str, attempts: int = 20, delay: float = 0.1) -> bool:
    """Restore and focus the top-level window with this exact title (retried while the first instance starts),
    in front of Premiere Pro as well (`raise_window`)."""
    api = _ForegroundApi() if IS_WINDOWS else None
    for _ in range(max(1, attempts)):
        hwnd = find_window(title)
        if hwnd and api is not None:
            return raise_window(hwnd, api)
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


# --- the two locked sizes and the animated change between them (pure) -------------------------------------------
#
# The window has two locked logical sizes (docs/HOST_UI.md 2, docs/BRIDGE_API.md 6): compact in the idle and waiting
# states, wide in main. The change keeps the window's centre (clamped into the work area) and runs over RESIZE_MS on
# the same curve as the page (`cubic-bezier(0.2, 0.8, 0.2, 1)`, an ease-out), so the page's layout animation and the
# native frame end together.

WINDOW_SIZES = {"compact": (420, 560), "wide": (720, 560)}
RESIZE_MS = 300
RESIZE_CURVE = (0.2, 0.8, 0.2, 1.0)
FRAME_MS = 1000.0 / 60.0

Rect = tuple[int, int, int, int]


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """The CSS `cubic-bezier(x1, y1, x2, y2)` timing function: progress (0..1) -> eased value."""

    def coord(t: float, a: float, b: float) -> float:
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t ** 2 + t ** 3

    def ease(p: float) -> float:
        if p <= 0:
            return 0.0
        if p >= 1:
            return 1.0
        lo, hi, t = 0.0, 1.0, p
        for _ in range(40):  # bisection on x(t) = p; x is monotonic for 0 <= x1, x2 <= 1
            x = coord(t, x1, x2)
            if abs(x - p) < 1e-6:
                break
            if x < p:
                lo = t
            else:
                hi = t
            t = (lo + hi) / 2
        return coord(t, y1, y2)

    return ease


def rect_toward(rect: Rect, size: tuple[int, int], work: Optional[tuple[int, int, int, int]]) -> Rect:
    """The rect of `size` with the centre of `rect`, moved inside the work area (left, top, right, bottom) when it
    fits there. Physical px."""
    x, y, w, h = rect
    nw, nh = size
    nx = x + (w - nw) // 2
    ny = y + (h - nh) // 2
    if work is not None:
        left, top, right, bottom = work
        if nw <= right - left:
            nx = min(max(nx, left), right - nw)
        if nh <= bottom - top:
            ny = min(max(ny, top), bottom - nh)
    return nx, ny, nw, nh


def lerp_rect(start: Rect, end: Rect, k: float) -> Rect:
    return tuple(int(round(a + (b - a) * k)) for a, b in zip(start, end))  # type: ignore[return-value]


def resize_frames(start: Rect, end: Rect, ms: float, frame_ms: float = FRAME_MS,
                  curve: tuple[float, float, float, float] = RESIZE_CURVE) -> list[tuple[float, Rect]]:
    """The planned steps of a size change: (time in ms, rect) per frame, the last one exactly `end` at `ms`."""
    ease = cubic_bezier(*curve)
    count = max(1, int(round(ms / frame_ms)))
    return [(ms * i / count, lerp_rect(start, end, ease(i / count))) for i in range(1, count + 1)]


def physical_size(logical: tuple[int, int], scale: float) -> tuple[int, int]:
    return int(round(logical[0] * scale)), int(round(logical[1] * scale))


class SizeAnimator:
    """Moves the window between the locked sizes of WINDOW_SIZES.

    Everything native is passed in, so the class runs without a window in the tests:
    - `invoke(fn)` runs `fn` on the UI thread and returns when it ran (WinForms `Invoke`);
    - `apply(x, y, w, h)` moves the window (the form's `SetBounds`), physical px, UI thread;
    - `set_locked(w, h)` updates the size the `FixedSizeHook` holds, UI thread, called before every `apply` so the
      hook (which replaces every size proposal by its locked size) never fights the animation;
    - `get_rect()` the window's current physical rect, `get_work()` the work area of its monitor, `get_scale()` its
      DPI scale.
    A change during an animation starts from the current rect; `set_mode` returns at once (a daemon thread steps).
    """

    def __init__(self, *, invoke: Callable[[Callable[[], None]], None], apply: Callable[[int, int, int, int], None],
                 set_locked: Callable[[int, int], None], get_rect: Callable[[], Optional[Rect]],
                 get_work: Callable[[], Optional[tuple[int, int, int, int]]] = lambda: None,
                 get_scale: Callable[[], float] = lambda: 1.0, frame_ms: float = FRAME_MS,
                 clock: Callable[[], float] = time.perf_counter, logger: Optional[logging.Logger] = None) -> None:
        self._invoke = invoke
        self._apply = apply
        self._set_locked = set_locked
        self._get_rect = get_rect
        self._get_work = get_work
        self._get_scale = get_scale
        self._frame_ms = frame_ms
        self._clock = clock
        self._log = logger or log
        self._lock = threading.Lock()
        self._gen = 0
        self.mode: Optional[str] = None
        self.animating = False
        self.last_run: Optional[dict] = None  # measurements of the last change (tests, log)
        self._done = threading.Event()
        self._done.set()

    def target_rect(self, mode: str, start: Optional[Rect] = None) -> Rect:
        size = physical_size(WINDOW_SIZES[mode], self._get_scale())
        if start is None:
            start = self._get_rect()
        if start is None:
            return centered_rect(size, self._get_work())
        return rect_toward(start, size, self._get_work())

    def set_mode(self, mode: str, *, animate: bool = True, ms: int = RESIZE_MS) -> bool:
        """Change to `mode` (`compact` | `wide`). False when the window is already there and nothing is running."""
        if mode not in WINDOW_SIZES:
            raise ValueError(f"unknown window mode: {mode!r}")
        with self._lock:
            if mode == self.mode and not self.animating:
                return False
            self._gen += 1
            gen = self._gen
            previous = self.mode
            self.mode = mode
            self.animating = bool(animate and ms > 0)
            self._done.clear()
        start = self._get_rect()
        end = self.target_rect(mode, start)
        if not animate or ms <= 0 or start is None:
            self._step(end)
            self._finish(gen, previous, mode, start, end, 0.0, 1)
            return True
        threading.Thread(target=self._run, args=(gen, previous, mode, start, end, ms), daemon=True,
                         name="window-resize").start()
        return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def _step(self, rect: Rect) -> None:
        def ui() -> None:
            self._set_locked(rect[2], rect[3])
            self._apply(*rect)

        self._invoke(ui)

    def _run(self, gen: int, previous: Optional[str], mode: str, start: Rect, end: Rect, ms: int) -> None:
        ease = cubic_bezier(*RESIZE_CURVE)
        t0 = self._clock()
        self._log.info("window resize %s -> %s: start %s, %d ms", previous, mode, start, ms)
        steps = 0
        try:
            while True:
                if gen != self._gen:
                    return  # a newer change took over from the current rect
                elapsed = (self._clock() - t0) * 1000.0
                k = min(1.0, elapsed / ms)
                rect = end if k >= 1.0 else lerp_rect(start, end, ease(k))
                self._step(rect)
                steps += 1
                if k >= 1.0:
                    break
                # Sleep to the next frame boundary measured from the start, so slow steps do not stretch the run.
                next_at = (int(elapsed // self._frame_ms) + 1) * self._frame_ms
                time.sleep(max(0.0, (next_at - (self._clock() - t0) * 1000.0) / 1000.0))
        except Exception:  # noqa: BLE001 - the window may be closing
            self._log.debug("window resize step failed", exc_info=True)
        self._finish(gen, previous, mode, start, end, (self._clock() - t0) * 1000.0, steps)

    def _finish(self, gen: int, previous: Optional[str], mode: str, start: Optional[Rect], end: Rect,
                elapsed: float, steps: int) -> None:
        actual = None
        try:
            actual = self._get_rect()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            if gen != self._gen:
                return
            self.animating = False
            self.last_run = {"from": previous, "to": mode, "start": start, "end": end, "actual": actual,
                             "ms": round(elapsed, 1), "steps": steps}
            self._done.set()
        self._log.info("window resize %s -> %s: end %s (planned %s) after %.0f ms, %d steps", previous, mode,
                       actual, end, elapsed, steps)


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


def window_rect(hwnd) -> Optional[Rect]:
    """(x, y, w, h) of the window in physical px, or None."""
    if not IS_WINDOWS or not hwnd:
        return None
    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def is_iconic(hwnd) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsIconic(hwnd))


def is_visible(hwnd) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsWindowVisible(hwnd))


SPI_GETCLIENTAREAANIMATION = 0x1042


def animations_enabled() -> bool:
    """Windows "Animation effects" (Settings > Accessibility > Visual effects); Chromium maps it to
    `prefers-reduced-motion`, so the native size change follows the same switch as the page."""
    if not IS_WINDOWS:
        return True
    value = wintypes.BOOL(1)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except (AttributeError, OSError):
        pass
    return True


def window_scale(hwnd) -> float:
    """DPI scale of the window (1.0 at 96 dpi)."""
    if not IS_WINDOWS or not hwnd:
        return 1.0
    try:
        return (_user32.GetDpiForWindow(hwnd) or 96) / 96.0
    except (AttributeError, OSError):
        return 1.0


class FixedSizeHook:
    """Window-procedure subclass that holds the window at one physical size (moved by `set_size` while the shell
    animates between the two locked sizes); keep the instance alive as long as the window exists."""

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

    def set_size(self, width: int, height: int) -> None:
        """Change the held size (physical px) without moving the window; the size animation calls it before each
        step (UI thread)."""
        self.size = (int(width), int(height))

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
