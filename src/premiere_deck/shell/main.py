"""Application entry point: single instance, logging, hub, bridge, tray and the fixed-size frameless window.

    python -m premiere_deck [--project PATH] [--from-premiere] [--fake [--fake-state STATE]] [--debug] [--ui-dir PATH]
    PremiereDeck.exe --apply-update ...   (the updater's own mode, handled before anything else)

A second start hands its arguments to the running instance (WM_COPYDATA to the window) and exits: the first
instance shows its window and, with `--project`, launches Premiere with that project (docs/HOST_UI.md 1).

The window has two locked sizes, compact and wide (docs/HOST_UI.md 2): the bridge decides the mode from
`PremiereState.state` and sends the `window` event; `_Shell` moves the native size (winutil.SizeAnimator) on the
WinForms UI thread.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from premiere_deck import APP_NAME, __version__
from premiere_deck.core.selfupdate import APPLY_FLAG, build_scheduler, build_updater, run_apply

WINDOW_TITLE = APP_NAME
MUTEX_NAME = "Local\\PremiereDeck.SingleInstance"
BACKGROUND = {"black": "#0d1014", "sky": "#dcebfb"}  # --bg-mid of each theme (ui/themes.json); the page paints over it
FAKE_STATES = ("not_running", "waiting", "connected")
HTTP_PORT_FIRST = 42301  # fixed range keeps the page origin (and its localStorage) stable between launches
HTTP_PORT_TRIES = 40

log = logging.getLogger("premiere_deck.shell.main")
_keep_alive: list[Any] = []  # ctypes callbacks, hooks and the tray that must outlive the window


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="PremiereDeck", description=APP_NAME)
    parser.add_argument("--project", help="launch Premiere Pro with this project")
    parser.add_argument("--from-premiere", action="store_true", help="started by the panel: start in the tray")
    parser.add_argument("--fake", action="store_true", help="simulate Premiere Pro with an in-process agent")
    parser.add_argument("--fake-state", choices=FAKE_STATES, default="connected",
                        help="with --fake: the simulated state at start (default connected)")
    parser.add_argument("--debug", action="store_true", help="WebView2 dev tools and verbose logging")
    parser.add_argument("--ui-dir", help="serve the window page from this folder")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        logging.getLogger("premiere_deck.shell.main").info("ignored arguments: %s", unknown)
    return args


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def resolve_ui_dir(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1] / "ui"  # same relative place inside the extraction folder


def resolve_icon() -> Optional[str]:
    candidate = bundle_root() / "icon.ico" if getattr(sys, "frozen", False) else project_root() / "build" / "icon.ico"
    return str(candidate) if candidate.is_file() else None


def pick_http_port() -> int:
    for port in range(HTTP_PORT_FIRST, HTTP_PORT_FIRST + HTTP_PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return 0


def theme_background(ui_dir: Path, theme: str) -> str:
    """The page background (`--bg-mid`) of a theme from ui/themes.json, for the window and its DWM border."""
    try:
        data = json.loads((ui_dir / "themes.json").read_text("utf-8"))
        for item in data.get("themes", []):
            if item.get("name") == theme:
                value = (item.get("overrides") or {}).get("--bg-mid") or (item.get("derived") or {}).get("--bg-mid")
                if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
                    return value
    except (OSError, ValueError, AttributeError):
        log.debug("themes.json not readable", exc_info=True)
    return BACKGROUND.get(theme, BACKGROUND["black"])


def hand_over(args: argparse.Namespace) -> bool:
    """Second instance: pass the arguments to the first one. True when it answered."""
    from . import winutil

    hwnd = winutil.find_window_retry(WINDOW_TITLE, attempts=50)
    if not hwnd:
        return False
    message = {"show": not args.from_premiere, "project": os.path.abspath(args.project) if args.project else None}
    return winutil.send_copydata(hwnd, json.dumps(message))


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == APPLY_FLAG:
        return int(run_apply(raw[1:]) or 0)
    args = parse_args(raw)

    from . import winutil

    mutex = winutil.acquire_single_instance(MUTEX_NAME + (".fake" if args.fake else ""))
    if mutex is None:
        hand_over(args)
        return 0

    from premiere_deck.core.log import setup_logging
    from premiere_deck.core.paths import AppPaths

    try:
        paths = AppPaths()
        paths.ensure()
    except Exception as exc:  # noqa: BLE001 - a windowed exe has no console; show the reason
        winutil.message_box(WINDOW_TITLE, f"Could not create the data folder:\n{exc}", error=True)
        return 3
    setup_logging(paths.logs_dir, args.debug)
    ui_dir = resolve_ui_dir(args.ui_dir)
    index = ui_dir / "index.html"
    log.info("%s %s starting (fake=%s, from_premiere=%s, ui=%s, data=%s)", APP_NAME, __version__, args.fake,
             args.from_premiere, ui_dir, paths.home)
    if not index.is_file():
        winutil.message_box(WINDOW_TITLE, f"Window page not found:\n{index}", error=True)
        return 2

    from premiere_deck.host.hub import Hub

    from .bridge import Bridge, EventPump

    pump = EventPump()
    try:
        hub = Hub(paths, fake=args.fake, fake_display=args.fake_state == "connected")
        bridge = Bridge(hub, pump, __version__)
        hub.start()
        if args.fake and args.fake_state == "not_running":
            from premiere_deck.host.fake_agent import set_fake_premiere

            set_fake_premiere(hub, "not_running")
    except Exception as exc:  # noqa: BLE001
        log.exception("the host could not start")
        winutil.message_box(WINDOW_TITLE, f"Premiere Deck could not start:\n{exc}", error=True)
        return 4
    try:
        updater = build_updater(paths, __version__, bridge._on_update_status, bridge._on_update_toast)
        bridge._attach_updater(updater, build_scheduler(updater))
    except Exception:  # noqa: BLE001 - the app works without updates
        log.exception("the updater could not be created")

    import webview  # late: pulls in pythonnet and WinForms

    background = theme_background(ui_dir, str(hub.settings.get("theme") or "black"))
    size = winutil.WINDOW_SIZES[bridge._mode]
    window = webview.create_window(
        WINDOW_TITLE, url=str(index), js_api=bridge, width=size[0], height=size[1],
        frameless=True, easy_drag=False, resizable=False, shadow=True, text_select=True,
        background_color=background, hidden=bool(args.from_premiere))
    bridge._attach_window(window)

    def on_closing():
        # Alt+F4 / taskbar close follow the close-button rule; a quit (`_quitting`) is never cancelled.
        if bridge._should_hide():
            bridge._later(bridge._hide, 0.05)
            return False
        return None

    window.events.closing += on_closing
    shell = _Shell(window, bridge, hub, ui_dir)
    hub.on_open_window = shell.open_window
    if args.from_premiere:
        bridge._hidden = True
    if args.project:
        threading.Thread(target=shell.launch_project, args=(os.path.abspath(args.project),), daemon=True,
                         name="launch-project").start()

    try:
        webview.start(shell.after_start, gui="edgechromium", debug=args.debug, private_mode=False,
                      storage_path=str(paths.webview_dir), http_port=pick_http_port(), icon=resolve_icon())
    except Exception:  # noqa: BLE001
        log.exception("the window could not be started")
        winutil.message_box(WINDOW_TITLE, "The window could not be started. Check that the Microsoft Edge WebView2 "
                            "Runtime is installed. Details are in the log.", error=True)
    finally:
        shell.dispose_tray()
        bridge._shutdown()
        log.info("%s stopped", APP_NAME)
        logging.shutdown()
        winutil.release_single_instance(mutex)
    # Non-daemon .NET / WebView2 helper threads can keep the process alive after the last window is gone.
    os._exit(0)


class _Shell:
    """Window-side glue: native setup on the UI thread, tray, second-instance messages, quit with Premiere."""

    def __init__(self, window: Any, bridge: Any, hub: Any, ui_dir: Optional[Path] = None) -> None:
        self.window = window
        self.bridge = bridge
        self.hub = hub
        self.ui_dir = ui_dir or resolve_ui_dir()
        self.tray: Any = None
        self.hook: Any = None
        self.sizer: Any = None
        self.hwnd = 0
        self._native_done = False
        self._balloon_shown = False  # the tray balloon is shown once per host run
        bridge._on_hide = self._on_hidden
        bridge._on_theme = self._on_theme
        self._prev_process_cb = None
        if hub.watch is not None:
            previous = hub.watch._on_change

            def on_change(running: bool) -> None:
                previous(running)
                if not running and self.bridge._hidden:
                    log.info("Premiere Pro exited while the window is in the tray; quitting")
                    self.bridge._quit()

            hub.watch._on_change = on_change

    def after_start(self) -> None:
        """pywebview's start function (own thread): wait for the native form, then set it up on the UI thread."""
        for _ in range(200):
            if getattr(self.window, "native", None) is not None:
                break
            time.sleep(0.05)
        native = getattr(self.window, "native", None)
        if native is None:
            log.warning("native window not available; no tray, no size lock")
            return
        _on_ui_thread(self.window, self._setup_native)
        threading.Thread(target=self.bridge._start_updates, name="update-startup", daemon=True).start()

    def _setup_native(self) -> None:
        from . import winutil
        from .tray import Tray

        if self._native_done:
            return
        self._native_done = True
        native = self.window.native
        try:
            hwnd = self.hwnd = int(native.Handle.ToInt64())
            theme = str(self.hub.settings.get("theme") or "black")
            winutil.set_rounded_corners(hwnd)
            winutil.set_border_color(hwnd, theme_background(self.ui_dir, theme))
            hook = winutil.FixedSizeHook(hwnd, on_copydata=self._on_copydata)
            if hook.install():
                _keep_alive.append(hook)
                self.hook = hook
                mode = self.bridge._mode
                size = winutil.WINDOW_SIZES[mode]
                rect = hook.lock(*size, apply=native.SetBounds)
                log.info("window %s %sx%s logical, %sx%s physical, scale %.2f, rect %s", mode, size[0], size[1],
                         rect[2], rect[3], winutil.window_scale(hwnd), winutil.window_rect(hwnd))
                self.sizer = winutil.SizeAnimator(
                    invoke=lambda fn: _on_ui_thread(self.window, fn),
                    apply=native.SetBounds,
                    set_locked=hook.set_size,
                    get_rect=lambda: winutil.window_rect(hwnd),
                    get_work=lambda: winutil.monitor_work_area(hwnd),
                    get_scale=lambda: winutil.window_scale(hwnd),
                    logger=log)
                self.sizer.mode = mode
                self.bridge._attach_resizer(self._resize, winutil.animations_enabled)
                if self.bridge._mode != mode:  # the state changed while the window was being set up
                    self.sizer.set_mode(self.bridge._mode, animate=False)
            else:
                log.warning("fixed-size hook was not installed")
        except Exception:  # noqa: BLE001
            log.exception("window setup failed")
        try:
            native.Activated += lambda *_: self.bridge._on_focus(True)
            native.Deactivate += lambda *_: self.bridge._on_focus(False)
        except Exception:  # noqa: BLE001
            log.debug("focus tracking unavailable", exc_info=True)
        tray = Tray(WINDOW_TITLE, resolve_icon(), on_open=lambda: self.open_window(None),
                    on_launch=self._tray_launch, on_quit=lambda: threading.Thread(target=self.bridge._quit,
                                                                                  daemon=True).start())
        if tray.create():
            self.tray = tray
            _keep_alive.append(tray)

    def _on_copydata(self, text: str) -> None:
        """UI thread: arguments of a second start."""
        try:
            message = json.loads(text)
        except ValueError:
            return
        if message.get("show", True):
            threading.Thread(target=self.open_window, args=(None,), daemon=True).start()
        project = message.get("project")
        if isinstance(project, str) and project:
            threading.Thread(target=self.launch_project, args=(project,), daemon=True, name="launch-project").start()

    def open_window(self, page: Optional[str]) -> None:
        self.bridge._show(page)

    def _resize(self, mode: str, *, animate: bool = True, ms: int = 300) -> bool:
        """Bridge -> native: move to the locked size of `mode`; a minimized or hidden window changes at once."""
        from . import winutil

        sizer = self.sizer
        if sizer is None:
            return False
        if winutil.is_iconic(self.hwnd) or not winutil.is_visible(self.hwnd):
            animate = False
        return sizer.set_mode(mode, animate=animate, ms=ms)

    def _on_theme(self, theme: str) -> None:
        from . import winutil

        if self.hwnd:
            color = theme_background(self.ui_dir, theme)
            _on_ui_thread(self.window, lambda: winutil.set_border_color(self.hwnd, color))

    def _on_hidden(self) -> None:
        tray = self.tray
        if tray is None or self._balloon_shown:
            return
        self._balloon_shown = True
        log.info("window hidden to the tray; balloon shown (once per run)")
        _on_ui_thread(self.window, lambda: tray.notify("Premiere Deck keeps running while Premiere Pro is open."))

    def _tray_launch(self) -> None:
        threading.Thread(target=self.launch_project, args=(None,), daemon=True, name="launch-premiere").start()

    def launch_project(self, project: Optional[str]) -> None:
        for _ in range(100):  # the hub may still be starting
            if self.hub.registry is not None:
                break
            time.sleep(0.05)
        try:
            self.hub.launch_premiere(project)
        except Exception as exc:  # noqa: BLE001
            from .bridge import friendly_message

            log.info("launch failed: %s", exc)
            self.hub.toast("error", friendly_message(exc))

    def dispose_tray(self) -> None:
        tray = self.tray
        self.tray = None
        if tray is not None:
            try:
                _on_ui_thread(self.window, tray.dispose)
            except Exception:  # noqa: BLE001
                tray.dispose()


def _on_ui_thread(window: Any, fn) -> None:
    """Run `fn` on the WinForms UI thread (pywebview calls handlers on worker threads)."""
    native = getattr(window, "native", None)
    try:
        if native is not None and native.InvokeRequired:
            from System import Action  # pythonnet

            native.Invoke(Action(fn))
            return
    except Exception:  # noqa: BLE001
        log.debug("UI-thread invoke failed; running directly", exc_info=True)
    fn()


if __name__ == "__main__":
    sys.exit(main())
