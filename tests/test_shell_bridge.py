"""Bridge contract (docs/BRIDGE_API.md 2, 6), the event pump and the shell helpers, without a window."""

import inspect
import json
import time
import unittest

from premiere_deck.core.paths import AppPaths
from premiere_deck.host.hub import Hub
from premiere_deck.host.panel_install import MemoryRegistry, PanelInstaller
from premiere_deck.modules.base import Module
from premiere_deck.shell import winutil
from premiere_deck.shell.bridge import (
    CONTRACT_METHODS, Bridge, BridgeError, EventPump, build_event_script, friendly_message, js_literal,
)
from premiere_deck.shell.main import parse_args
from tests.test_host_helpers import free_ports, make_panel_source, remove_tree, scratch_dir, wait_for


class Plain(Module):
    id = "plain"
    title = "Plain"

    def on_action(self, action_id, value=None, row=None):
        if action_id == "fail":
            raise ValueError("The plan is empty")
        return None


class FakeWindow:
    title = "Premiere Deck"

    def __init__(self):
        self.calls = []
        self.scripts = []

    def run_js(self, script):
        self.scripts.append(script)

    def hide(self):
        self.calls.append("hide")

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def minimize(self):
        self.calls.append("minimize")

    def destroy(self):
        self.calls.append("destroy")

    def create_file_dialog(self, *args, **kw):
        self.calls.append(("dialog", kw.get("file_types")))
        return ["D:\\Edits\\A.prproj"]


class PumpTests(unittest.TestCase):
    def test_buffer_until_ready_and_coalesce(self):
        delivered = []
        pump = EventPump(delivered.append, threaded=False, start_delay=0)
        pump.emit("job", {"id": "j1", "progress": 0.1})
        pump.emit("toast", {"kind": "info", "message": "a"})
        pump.emit("job", {"id": "j1", "progress": 0.5})
        pump.emit("job", {"id": "j2", "progress": 0.2})
        pump.emit("premiere", {"state": "starting"})
        pump.emit("premiere", {"state": "connected"})
        self.assertEqual(pump.flush(), 0, "nothing before ready()")
        self.assertEqual(pump.pending(), [("job", {"id": "j1", "progress": 0.5}), ("toast", {"kind": "info", "message": "a"}),
                                          ("job", {"id": "j2", "progress": 0.2}), ("premiere", {"state": "connected"})])
        pump.set_ready()
        time.sleep(0.01)
        self.assertEqual(pump.flush(), 4)
        self.assertIn("window.premiereDeck", delivered[0])
        self.assertEqual(pump.flush(), 0)

    def test_threaded_delivery(self):
        delivered = []
        pump = EventPump(delivered.append, start_delay=0, min_interval=0)
        pump.set_ready()
        pump.emit("activity", {"text": "x"})
        self.assertTrue(wait_for(lambda: delivered))
        pump.stop()

    def test_js_literal_is_safe(self):
        self.assertEqual(js_literal("a\u2028b"), '"a\\u2028b"')
        script = build_event_script([("toast", {"message": "Ünïcode </script>"})])
        self.assertIn("Ünïcode", script)
        self.assertTrue(script.startswith("(function(){"))

    def test_friendly_messages(self):
        self.assertEqual(friendly_message(ValueError("Choose a file.")), "Choose a file.")
        self.assertEqual(friendly_message(PermissionError()), "No access to the file or folder.")
        self.assertEqual(friendly_message(KeyError("x")), "The operation failed; details are in the log.")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("bridge")
        paths = AppPaths(self.root / "data")
        self.source = source = make_panel_source(self.root / "panel_src", "3.0.0")
        installer = PanelInstaller(paths, "3.0.0", source=source, target=self.root / "cep" / "bundle",
                                   registry=MemoryRegistry())
        self.hub = Hub(paths, fake=True, modules=[Plain], ports=free_ports(3), installer=installer, watch=False)
        self.pump = EventPump(threaded=False, start_delay=0)
        self.bridge = Bridge(self.hub, self.pump, "3.0.0")
        self.window = FakeWindow()
        self.bridge._attach_window(self.window)
        self.hub.start()

    def tearDown(self):
        self.bridge._shutdown()
        remove_tree(self.root)

    def test_public_names_are_exactly_the_contract(self):
        public = sorted(name for name, _ in inspect.getmembers(self.bridge) if not name.startswith("_"))
        self.assertEqual(public, sorted(CONTRACT_METHODS))
        self.assertEqual(len(set(CONTRACT_METHODS)), len(CONTRACT_METHODS))
        # pywebview reads the parameter names through the wrapper.
        self.assertEqual(list(inspect.signature(self.bridge.module_action).parameters), ["id", "action", "value", "row"])

    def test_ready_state_and_events(self):
        state = self.bridge.ready()
        for key in ("version", "premiere", "panel", "modules", "recent", "activity", "jobs", "ffmpeg", "settings",
                    "update", "server"):
            self.assertIn(key, state)
        self.assertEqual(state["update"]["stage"], "idle")
        self.assertEqual(state["modules"][0]["id"], "plain")
        self.hub.toast("warn", "Careful")
        time.sleep(0.01)
        self.pump.flush()
        self.assertTrue(any('"Careful"' in s for s in self.window.scripts))

    def test_settings_patch_and_refusal(self):
        after = self.bridge.set_settings({"theme": "sky"})
        self.assertEqual(after["theme"], "sky")
        with self.assertRaises(BridgeError) as caught:
            self.bridge.set_settings({"theme": "neon"})
        self.assertEqual(str(caught.exception), "Unknown theme.")
        with self.assertRaises(BridgeError):
            self.bridge.set_settings("theme")

    def test_show_activity_setting_through_the_bridge(self):
        self.assertIs(self.bridge.get_settings()["show_activity"], False)
        self.assertIs(self.bridge.ready()["settings"]["show_activity"], False)
        after = self.bridge.set_settings({"show_activity": True})
        self.assertIs(after["show_activity"], True)
        events = [payload for name, payload in self.pump.pending() if name == "settings"]
        self.assertIs(events[-1]["show_activity"], True)
        with self.assertRaises(BridgeError):
            self.bridge.set_settings({"show_activity": "on"})
        self.assertIs(self.bridge.get_settings()["show_activity"], True)

    def test_module_calls(self):
        self.assertEqual(self.bridge.modules()[0]["state"], "running")
        self.assertEqual(self.bridge.module_action("plain", "plain.go"), {"ok": True, "toast": None})
        self.assertEqual(self.bridge.module_action("plain", "fail"), {"ok": False, "toast": None})
        self.assertEqual(self.bridge.module_page("plain")["page"], None)
        self.assertEqual(self.bridge.set_module_enabled("plain", False)["state"], "disabled")
        with self.assertRaises(BridgeError):
            self.bridge.module_page("")
        with self.assertRaises(BridgeError) as caught:
            self.bridge.module_settings("nobody")
        self.assertEqual(str(caught.exception), "Unknown module.")

    def test_close_hides_while_premiere_runs_else_quits(self):
        self.assertTrue(wait_for(lambda: self.hub.premiere_state()["running"], 10))  # the fake agent counts as running
        self.bridge.window_close()
        self.assertTrue(wait_for(lambda: "hide" in self.window.calls))
        self.assertTrue(self.bridge._hidden)
        self.bridge._show("settings")
        self.assertIn("show", self.window.calls)
        self.hub.settings.patch({"keep_running_with_premiere": False})
        self.bridge.window_close()
        self.assertTrue(wait_for(lambda: "destroy" in self.window.calls))

    def test_dialogs_and_panel_calls(self):
        self.assertEqual(self.bridge.pick_project(), "D:\\Edits\\A.prproj")
        status = self.bridge.panel_status()
        self.assertTrue(status["installed"])
        self.assertEqual(self.bridge.panel_reinstall()["installed_version"], "3.0.0")
        self.assertEqual(self.bridge.panel_debug_mode(True)["written_by_host"], ["CSXS.9", "CSXS.10", "CSXS.11", "CSXS.12"])
        self.assertEqual(self.bridge.panel_debug_mode(False)["written_by_host"], [])
        self.assertTrue(self.bridge.panel_remove()["removed"])
        self.assertFalse(self.bridge.open_url("http://example.com"))
        with self.assertRaises(BridgeError):
            self.bridge.check_updates()


    def test_panel_check_update(self):
        self.assertEqual(self.bridge.panel_check_update(), {"updated": False, "installed": "3.0.0", "bundled": "3.0.0"})
        self.assertFalse(any(name == "panel" for name, _ in self.pump.pending()))
        make_panel_source(self.source, "3.0.1", extra="two")  # a newer bundled panel (after a self-update)
        self.assertEqual(self.bridge.panel_check_update(), {"updated": True, "installed": "3.0.0", "bundled": "3.0.1"})
        self.assertEqual(self.hub.installer.installed_version(), "3.0.1")
        panel = [payload for name, payload in self.pump.pending() if name == "panel"]
        self.assertEqual(panel[-1]["installed_version"], "3.0.1")
        self.assertTrue((self.root / "cep" / "bundle" / "deck.json").is_file(), "deck.json is rewritten")


class ShellHelpersTests(unittest.TestCase):
    def test_args(self):
        args = parse_args(["--project", "D:\\A.prproj", "--from-premiere", "--fake", "--unknown"])
        self.assertEqual(args.project, "D:\\A.prproj")
        self.assertTrue(args.from_premiere and args.fake)

    def test_window_math(self):
        self.assertEqual(winutil.pick_window_size([(1040, 680)], (1920, 1040)), (1040, 680))
        self.assertEqual(winutil.centered_rect((1040, 680), (0, 0, 1920, 1040)), (440, 180, 1040, 680))
        self.assertEqual(winutil.colorref("#0b0716"), 0x16070B)

    def test_copydata_to_nowhere(self):
        self.assertFalse(winutil.send_copydata(0, json.dumps({"show": True})))


if __name__ == "__main__":
    unittest.main()
