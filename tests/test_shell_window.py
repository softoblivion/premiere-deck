"""The window's two locked sizes (docs/HOST_UI.md 2, docs/BRIDGE_API.md 4 `window` and 6): the size math, the
animator driven through fakes, the bridge's mode decision and `window` events, the simulated Premiere states of
`--fake` (`fake_premiere`), without a window."""

import json
import threading
import time
import unittest

from premiere_deck.core.paths import AppPaths
from premiere_deck.host.fake_agent import FakeAgent, canned_answers, make_fake_project, set_fake_premiere
from premiere_deck.host.hub import Hub
from premiere_deck.host.panel_install import MemoryRegistry, PanelInstaller
from premiere_deck.modules.base import Module
from premiere_deck.shell import bridge as bridge_mod
from premiere_deck.shell import winutil
from premiere_deck.shell.bridge import Bridge, BridgeError, EventPump
from premiere_deck.shell.main import parse_args, theme_background
from tests.test_host_helpers import free_ports, make_panel_source, remove_tree, scratch_dir, wait_for


class Plain(Module):
    id = "plain"
    title = "Plain"


class SizeMathTests(unittest.TestCase):
    def test_sizes_and_curve(self):
        self.assertEqual(winutil.WINDOW_SIZES, {"compact": (420, 560), "wide": (720, 560)})
        ease = winutil.cubic_bezier(*winutil.RESIZE_CURVE)
        self.assertEqual(ease(0), 0.0)
        self.assertEqual(ease(1), 1.0)
        values = [ease(i / 20) for i in range(21)]
        self.assertEqual(values, sorted(values), "monotonic")
        self.assertGreater(ease(0.5), 0.9, "ease-out: most of the way at half time")
        linear = winutil.cubic_bezier(0, 0, 1, 1)
        self.assertAlmostEqual(linear(0.3), 0.3, places=4)

    def test_rect_keeps_the_centre_inside_the_work_area(self):
        self.assertEqual(winutil.rect_toward((1070, 416, 420, 560), (720, 560), (0, 0, 2560, 1392)),
                         (920, 416, 720, 560))
        self.assertEqual(winutil.rect_toward((920, 416, 720, 560), (420, 560), (0, 0, 2560, 1392)),
                         (1070, 416, 420, 560))
        # Near the left edge the wide window is moved inside the work area.
        self.assertEqual(winutil.rect_toward((10, 100, 420, 560), (720, 560), (0, 0, 1920, 1040)),
                         (0, 100, 720, 560))
        # Unknown work area: the centre only.
        self.assertEqual(winutil.rect_toward((10, 100, 420, 560), (720, 560), None), (-140, 100, 720, 560))

    def test_a_rect_larger_than_the_work_area_keeps_its_top_left_inside(self):
        # 200 % on a 1080p work area: 840 x 1120 does not fit the height; the title bar stays on screen.
        self.assertEqual(winutil.rect_toward((750, 240, 420, 560), (840, 1120), (0, 0, 1920, 1040)),
                         (540, 0, 840, 1120))
        self.assertEqual(winutil.rect_toward((750, 240, 420, 560), (2000, 1120), (0, 0, 1920, 1040)),
                         (0, 0, 2000, 1120))

    def test_frames_end_exactly_on_the_target(self):
        frames = winutil.resize_frames((100, 100, 420, 560), (-50, 100, 720, 560), 300)
        self.assertEqual(len(frames), 18)
        self.assertEqual(frames[-1], (300.0, (-50, 100, 720, 560)))
        widths = [rect[2] for _, rect in frames]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(winutil.physical_size((420, 560), 1.5), (630, 840))

    def test_window_math_and_colour(self):
        self.assertEqual(winutil.centered_rect((420, 560), (0, 0, 1920, 1040)), (750, 240, 420, 560))
        self.assertEqual(winutil.colorref("#0d1014"), 0x14100D)


class FakeNative:
    """A window for the animator: a rect, the size hook's locked size and the UI-thread calls."""

    def __init__(self, rect):
        self.rect = rect
        self.locked = None
        self.calls = []
        self.lock = threading.Lock()
        self.ui_thread_ok = True

    def invoke(self, fn):
        with self.lock:  # one UI thread: steps never overlap
            fn()

    def apply(self, x, y, w, h):
        # The hook replaces every size proposal by its locked size: the animator must move it first.
        if (w, h) != self.locked:
            self.ui_thread_ok = False
        self.rect = (x, y, w, h)
        self.calls.append(self.rect)

    def set_locked(self, w, h):
        self.locked = (w, h)


class SizeAnimatorTests(unittest.TestCase):
    def make(self, native, scale=1.0):
        return winutil.SizeAnimator(invoke=native.invoke, apply=native.apply, set_locked=native.set_locked,
                                    get_rect=lambda: native.rect, get_work=lambda: (0, 0, 2560, 1392),
                                    get_scale=lambda: scale)

    def test_animated_change_keeps_the_centre_and_the_hook_in_step(self):
        native = FakeNative((1070, 416, 420, 560))
        sizer = self.make(native)
        sizer.mode = "compact"
        self.assertFalse(sizer.set_mode("compact"), "already there")
        t0 = time.perf_counter()
        self.assertTrue(sizer.set_mode("wide", ms=120))
        self.assertTrue(sizer.wait(3))
        elapsed = (time.perf_counter() - t0) * 1000
        self.assertEqual(native.rect, (920, 416, 720, 560))
        self.assertGreater(len(native.calls), 3, "stepped, not jumped")
        self.assertTrue(native.ui_thread_ok)
        widths = [r[2] for r in native.calls]
        self.assertEqual(widths, sorted(widths))
        self.assertTrue(all(r[0] + r[2] // 2 in (1279, 1280) for r in native.calls), "centre kept")
        self.assertGreaterEqual(elapsed, 110)
        run = sizer.last_run
        self.assertEqual((run["from"], run["to"], run["actual"]), ("compact", "wide", (920, 416, 720, 560)))
        self.assertFalse(sizer.animating)

    def test_instant_change_and_scale(self):
        native = FakeNative((100, 100, 630, 840))
        sizer = self.make(native, scale=1.5)
        sizer.mode = "compact"
        self.assertTrue(sizer.set_mode("wide", animate=False))
        self.assertEqual(native.calls, [(0, 100, 1080, 840)], "moved inside the work area")
        self.assertEqual(native.locked, (1080, 840))

    def test_rescale_multiplies_the_locked_size_and_keeps_the_centre(self):
        native = FakeNative((1070, 416, 420, 560))
        sizer = self.make(native)
        sizer.mode = "compact"
        self.assertEqual(sizer.rescale(1.5), (965, 276, 630, 840))
        self.assertEqual((native.rect, native.locked), ((965, 276, 630, 840), (630, 840)))
        self.assertTrue(native.ui_thread_ok)
        self.assertTrue(sizer.set_mode("wide", animate=False))
        self.assertEqual(native.rect, (740, 276, 1080, 840), "the other locked size at the same factor")
        sizer.rescale(1.0)
        self.assertEqual(native.rect, (920, 416, 720, 560))
        self.assertEqual((sizer.mode, sizer.zoom, sizer.animating), ("wide", 1.0, False))
        sized = self.make(FakeNative((0, 0, 420, 560)), scale=1.25)
        sized.mode = "compact"
        self.assertEqual(sized.rescale(1.5)[2:], (788, 1050), "DPI scale times the Scale factor")

    def test_a_new_change_starts_from_the_current_rect(self):
        native = FakeNative((1070, 416, 420, 560))
        sizer = self.make(native)
        sizer.mode = "compact"
        sizer.set_mode("wide", ms=400)
        time.sleep(0.12)
        sizer.set_mode("compact", ms=100)
        self.assertTrue(sizer.wait(3))
        time.sleep(0.05)
        self.assertEqual(native.rect, (1070, 416, 420, 560))
        self.assertEqual(sizer.mode, "compact")
        self.assertEqual(sizer.last_run["to"], "compact")
        self.assertGreater(sizer.last_run["start"][2], 420, "reversed mid-way")
        with self.assertRaises(ValueError):
            sizer.set_mode("huge")


class FakeWindow:
    title = "Premiere Deck"

    def __init__(self):
        self.calls = []

    def run_js(self, script):
        pass

    def hide(self):
        self.calls.append("hide")

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def destroy(self):
        self.calls.append("destroy")


class BridgeWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("window")
        paths = AppPaths(self.root / "data")
        source = make_panel_source(self.root / "panel_src", "3.0.0")
        installer = PanelInstaller(paths, "3.0.0", source=source, target=self.root / "cep" / "bundle",
                                   registry=MemoryRegistry())
        self.hub = Hub(paths, fake=True, modules=[Plain], ports=free_ports(3), installer=installer, watch=False,
                       fake_display=False)
        self.pump = EventPump(threaded=False, start_delay=0)
        self.bridge = Bridge(self.hub, self.pump, "3.0.0")
        self.bridge._attach_window(FakeWindow())
        self.resizes = []
        self.bridge._attach_resizer(lambda mode, animate=True, ms=300: self.resizes.append((mode, animate, ms)) or True)
        self.hub.start()
        self.old_delay = bridge_mod.WIDEN_DELAY
        bridge_mod.WIDEN_DELAY = 0.05

    def tearDown(self):
        bridge_mod.WIDEN_DELAY = self.old_delay
        self.bridge._shutdown()
        remove_tree(self.root)

    def windows(self):
        return [payload for name, payload in self.pump.pending() if name == "window"]

    def state(self):
        return self.hub.premiere_state()["state"]

    def test_mode_follows_the_display_and_the_events_carry_the_duration(self):
        self.assertTrue(wait_for(lambda: self.state() == "waiting", 10))
        self.assertEqual(self.bridge.ready()["window"], {"mode": "compact", "focused": True})
        self.assertEqual(self.resizes, [])
        self.assertEqual(self.bridge.fake_premiere("connected")["displays"] in (0, 1), True)
        self.assertTrue(wait_for(lambda: self.resizes == [("wide", True, 300)], 10))
        self.assertEqual(self.windows()[-1], {"mode": "wide", "focused": True, "ms": 300})
        self.assertEqual(self.bridge.get_state()["window"]["mode"], "wide")
        self.bridge.fake_premiere("waiting")
        self.assertTrue(wait_for(lambda: self.resizes[-1] == ("compact", True, 300), 10))
        self.bridge.fake_premiere("not_running")
        self.assertTrue(wait_for(lambda: self.state() == "not_running", 10))
        self.assertEqual(len(self.resizes), 2, "idle and waiting share the compact size")
        with self.assertRaises(BridgeError):
            self.bridge.fake_premiere("crashed")

    def test_hidden_window_changes_at_once_and_show_applies_the_size(self):
        self.assertTrue(wait_for(lambda: self.state() == "waiting", 10))
        self.bridge._hidden = True
        self.bridge.fake_premiere("connected")
        self.assertTrue(wait_for(lambda: self.resizes == [("wide", False, 0)], 10))
        self.assertEqual(self.windows()[-1]["ms"], 0)
        self.bridge._show(None)
        self.assertEqual(len(self.resizes), 1)
        # A change while hidden that the show has to catch up with.
        self.bridge._hidden = True
        self.bridge._mode = "compact"
        self.bridge._show(None)
        self.assertEqual(self.resizes[-1], ("wide", False, 0))

    def test_a_short_connection_does_not_widen(self):
        bridge_mod.WIDEN_DELAY = 0.4
        self.assertTrue(wait_for(lambda: self.state() == "waiting", 10))
        self.bridge._on_premiere_state({"state": "connected"})
        self.bridge._on_premiere_state({"state": "waiting"})
        time.sleep(0.6)
        self.assertEqual(self.resizes, [])

    def test_focus_events_carry_the_mode(self):
        self.bridge._on_focus(False)
        self.assertEqual(self.windows()[-1], {"mode": "compact", "focused": False, "ms": 0})

    def test_theme_change_notifies_the_hub_and_the_shell(self):
        seen = []
        self.hub.notify_theme = lambda: seen.append("panel")
        self.bridge._on_theme = lambda theme: seen.append(theme)
        self.bridge.set_settings({"theme": "sky"})
        self.assertEqual(seen, ["sky", "panel"])
        self.bridge.set_settings({"keep_running_with_premiere": False})
        self.assertEqual(seen, ["sky", "panel"])

    def test_fake_premiere_only_in_the_fake_mode(self):
        self.hub.fake = False
        try:
            with self.assertRaises(BridgeError) as caught:
                self.bridge.fake_premiere("waiting")
            self.assertEqual(str(caught.exception), "Only available in the simulated mode.")
        finally:
            self.hub.fake = True


class FakeAgentTests(unittest.TestCase):
    def test_create_bin_path_answers_a_node_id(self):
        root = scratch_dir("fake-bins")
        try:
            answers = canned_answers(make_fake_project(root))
            self.assertEqual(answers["create_bin_path"]({"tree_path": "Audio\\Music"}), {"node_id": "bin:Audio/Music"})
            self.assertEqual(answers["create_bin_path"](None), {"node_id": "bin:"})
        finally:
            remove_tree(root)

    def test_display_toggles_and_states(self):
        root = scratch_dir("fake-states")
        paths = AppPaths(root / "data")
        source = make_panel_source(root / "panel_src", "3.0.0")
        installer = PanelInstaller(paths, "3.0.0", source=source, target=root / "cep" / "bundle",
                                   registry=MemoryRegistry())
        hub = Hub(paths, fake=True, modules=[Plain], ports=free_ports(3), installer=installer, watch=False,
                  fake_display=True)
        hub.start()
        try:
            state = lambda: hub.premiere_state()["state"]  # noqa: E731
            self.assertTrue(wait_for(lambda: state() == "connected", 10))
            hub.fake_agent.set_display(False)
            self.assertTrue(wait_for(lambda: state() == "waiting", 10))
            set_fake_premiere(hub, "not_running")
            self.assertTrue(wait_for(lambda: state() == "not_running", 10))
            self.assertTrue(hub.fake_agent.stopped)
            set_fake_premiere(hub, "connected")
            self.assertTrue(wait_for(lambda: state() == "connected", 10))
            self.assertIsInstance(hub.fake_agent, FakeAgent)
            with self.assertRaises(ValueError):
                set_fake_premiere(hub, "starting")
        finally:
            hub.stop()
            remove_tree(root)


class FakeForegroundApi:
    """Records the calls of `winutil.raise_window`; `refuse` is how many SetForegroundWindow calls fail first."""

    def __init__(self, foreground=500, iconic=False, refuse=0):
        self.calls = []
        self.foreground = foreground
        self.iconic = iconic
        self.refuse = refuse

    def is_iconic(self, hwnd):
        return self.iconic

    def show_window(self, hwnd, cmd):
        self.calls.append(("show", cmd))

    def foreground_window(self):
        return self.foreground

    def window_thread(self, hwnd):
        return 77

    def current_thread(self):
        return 11

    def attach_thread_input(self, source, target, attach):
        self.calls.append(("attach", source, target, attach))
        return True

    def key_event(self, vk, flags):
        self.calls.append(("key", vk, flags))

    def bring_to_top(self, hwnd):
        self.calls.append(("top", hwnd))

    def set_foreground(self, hwnd):
        self.calls.append(("foreground", hwnd))
        if self.refuse > 0:
            self.refuse -= 1
            return False
        self.foreground = hwnd
        return True


class RaiseWindowTests(unittest.TestCase):
    """The call sequence only: whether Windows lets a background process take the foreground cannot be unit-tested."""

    def test_attaches_to_the_foreground_thread_around_the_raise(self):
        api = FakeForegroundApi()
        self.assertTrue(winutil.raise_window(900, api))
        self.assertEqual(api.calls, [("show", winutil.SW_SHOW), ("attach", 11, 77, True), ("top", 900),
                                     ("foreground", 900), ("attach", 11, 77, False)])

    def test_minimised_window_is_restored_first(self):
        api = FakeForegroundApi(iconic=True)
        winutil.raise_window(900, api)
        self.assertEqual(api.calls[0], ("show", winutil.SW_RESTORE))

    def test_alt_tap_when_the_foreground_call_is_refused(self):
        api = FakeForegroundApi(refuse=1)
        self.assertTrue(winutil.raise_window(900, api))
        names = [c[0] for c in api.calls]
        self.assertEqual(names.count("foreground"), 2)
        self.assertIn(("key", winutil.VK_MENU, 0), api.calls)
        self.assertIn(("key", winutil.VK_MENU, winutil.KEYEVENTF_KEYUP), api.calls)
        self.assertLess(api.calls.index(("attach", 11, 77, False)), api.calls.index(("key", winutil.VK_MENU, 0)))

    def test_already_in_front_changes_nothing_else(self):
        api = FakeForegroundApi(foreground=900)
        self.assertTrue(winutil.raise_window(900, api))
        self.assertEqual(api.calls, [("show", winutil.SW_SHOW)])

    def test_no_window_or_a_failing_call_is_safe(self):
        self.assertFalse(winutil.raise_window(0, FakeForegroundApi()))

        class Broken(FakeForegroundApi):
            def show_window(self, hwnd, cmd):
                raise OSError("gone")

        self.assertFalse(winutil.raise_window(900, Broken()))
        self.assertFalse(winutil.bring_window_to_front("no such window title 7c0629", attempts=1, delay=0))

    def test_attachment_is_undone_when_the_raise_fails(self):
        class Raising(FakeForegroundApi):
            def set_foreground(self, hwnd):
                raise OSError("denied")

        api = Raising()
        self.assertFalse(winutil.raise_window(900, api))
        self.assertEqual(api.calls[-1], ("attach", 11, 77, False))


class ShellArgsTests(unittest.TestCase):
    def test_fake_state_and_background(self):
        self.assertEqual(parse_args(["--fake"]).fake_state, "connected")
        self.assertEqual(parse_args(["--fake", "--fake-state", "not_running"]).fake_state, "not_running")
        root = scratch_dir("themes")
        try:
            (root / "themes.json").write_text(json.dumps({"themes": [
                {"name": "black", "derived": {"--bg-mid": "#0d1014"}, "overrides": {}},
                {"name": "sky", "derived": {"--bg-mid": "#0b1520"}, "overrides": {"--bg-mid": "#dcebfb"}}]}), "utf-8")
            self.assertEqual(theme_background(root, "sky"), "#dcebfb")
            self.assertEqual(theme_background(root, "black"), "#0d1014")
            self.assertEqual(theme_background(root / "missing", "sky"), "#dcebfb")
        finally:
            remove_tree(root)


if __name__ == "__main__":
    unittest.main()
