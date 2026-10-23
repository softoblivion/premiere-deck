"""The hub end to end: server + fake agent over a real socket + registry with in-test modules + a display client.
Runs once with the real facade (when it is in the tree) and once with the stand-in."""

import contextlib
import json
import sys
import threading
import unittest
from unittest import mock

from websockets.sync.client import connect

from premiere_deck import PROTOCOL_VERSION
from premiere_deck.core.paths import AppPaths
from premiere_deck.host.hub import Hub, StubPremiere, project_info_for
from premiere_deck.host.panel_install import MemoryRegistry, PanelInstaller
from premiere_deck.modules.base import ActionResult, Module
from tests.test_host_helpers import free_ports, make_panel_source, remove_tree, scratch_dir, wait_for


class Echo(Module):
    id = "echo"
    title = "Echo"
    order = 10
    events = ["project.items_added", "panel.opened", "panel.closed"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.log_lines = []
        self.answer = None

    def project_opened(self, project):
        self.log_lines.append(("project_opened", project.name))

    def project_closed(self):
        self.log_lines.append(("project_closed",))

    def on_event(self, name, data):
        self.log_lines.append(("event", name))

    def on_action(self, action_id, value=None, row=None):
        self.log_lines.append(("action", action_id, value))
        if action_id == "ask":
            def work(job):
                return self.ctx.ui.confirm("Copy 3 files?")

            self.ctx.jobs.submit(work, title="Ask", on_done=lambda job, result: setattr(self, "answer", result))
            return None
        if action_id == "note":
            self.ctx.events.emit("journal.appended", {"text": "Filed clip.mp4", "tone": "ok"})
        if action_id == "result":
            self.ctx.ui.result("go", value or "ok", "Echoed go", "A reason." if value == "error" else None)
            return None
        if action_id == "job_result":
            self.ctx.jobs.submit(lambda job: "done", title="Result job",
                                 on_done=lambda job, r: self.ctx.ui.result("echo.go", "neutral", "Job done"))
            return None
        if action_id == "show":
            self.ctx.host.show("echo")
            self.ctx.host.show()
            return None
        return ActionResult(toast=("ok", f"Echo {action_id}"))

    def render(self):
        project = self.ctx.project.name if self.ctx.project else "none"
        return [self.button("go", "Go"), self.text(f"Project {project}")]


class HubTestBase(unittest.TestCase):
    use_stub = False
    fake_display = False
    modules = [Echo]

    def setUp(self):
        self.root = scratch_dir("hub")
        self.paths = AppPaths(self.root / "data")
        source = make_panel_source(self.root / "panel_src", "2.0.0")
        self.target = self.root / "cep" / "com.softoblivion.premieredeck"
        installer = PanelInstaller(self.paths, "2.0.0", source=source, target=self.target, registry=MemoryRegistry())
        self.stack = contextlib.ExitStack()
        if self.use_stub:
            self.stack.enter_context(mock.patch.dict(sys.modules, {"premiere_deck.premiere.api": None}))
        self.installer = installer
        self.hub = Hub(self.paths, fake=True, modules=list(self.modules), ports=free_ports(3), installer=installer,
                       watch=False, fake_display=self.fake_display)
        self.events = []
        self.hub.emit = lambda name, payload: self.events.append((name, payload))
        self.opened = []
        self.hub.on_open_window = self.opened.append
        self.hub.start()
        self.echo = self.hub.registry.entry("echo").instance
        self.assertTrue(wait_for(lambda: self.hub.fake_agent.connected.is_set(), 10))

    def tearDown(self):
        self.stack.close()
        self.hub.stop()
        remove_tree(self.root)

    def display(self):
        url = f"ws://127.0.0.1:{self.hub.server.port}/ws?role=display&token={self.hub.server.token}"
        ws = self.stack.enter_context(connect(url, open_timeout=5))
        ws.send(json.dumps({"type": "hello", "role": "display", "panel_version": "2.0.0",
                            "protocol": PROTOCOL_VERSION}))
        self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "welcome")
        return ws

    def next_of(self, ws, kind, predicate=lambda m: True, timeout=5):
        while True:
            message = json.loads(ws.recv(timeout=timeout))
            if message["type"] == kind and predicate(message):
                return message


class HubTests(HubTestBase):
    def test_start_removes_retired_data(self):
        retired = self.paths.home / "models"
        retired.mkdir()
        (retired / "weights.bin").write_bytes(b"x")
        self.hub.stop()
        self.hub = Hub(self.paths, fake=True, modules=[Echo], ports=free_ports(3), installer=self.hub.installer,
                       watch=False)
        self.hub.start()
        self.assertFalse(retired.exists())

    def test_install_discovery_and_project_from_hello(self):
        self.assertEqual(self.hub.install_result["action"], "installed")
        deck = json.loads((self.target / "deck.json").read_text("utf-8"))
        self.assertEqual(deck["port"], self.hub.server.port)
        self.assertTrue(wait_for(lambda: ("project_opened", "Sample Project") in self.echo.log_lines, 10))
        state = self.hub.state()
        self.assertEqual(state["premiere"]["state"], "waiting", "an agent without a display is not connected")
        self.assertTrue(state["premiere"]["agent_connected"])
        self.assertFalse(state["premiere"]["fallback_offer"])
        self.assertEqual(state["premiere"]["project"]["name"], "Sample Project")
        self.assertEqual(state["modules"][0]["id"], "echo")
        self.assertTrue(wait_for(lambda: "project.items_added" in self.hub.fake_agent.subscribed))
        self.assertFalse(any(m.get("type") == "open_display" for m in self.hub.fake_agent.received),
                         "the host never asks the agent to open the panel")

    def test_state_follows_the_display(self):
        self.assertEqual(self.hub.premiere_state()["state"], "waiting")
        ws = self.display()
        self.assertTrue(wait_for(lambda: self.hub.premiere_state()["state"] == "connected"))
        self.assertTrue(wait_for(lambda: any(name == "premiere" and p["state"] == "connected" and p["displays"] == 1
                                             for name, p in self.events)), "a display connect fires `premiere`")
        ws.close()
        self.assertTrue(wait_for(lambda: self.hub.premiere_state()["state"] == "waiting"))
        self.assertEqual(self.events[-1][0], "premiere")
        self.assertEqual((self.events[-1][1]["state"], self.events[-1][1]["displays"]), ("waiting", 0))
        self.assertFalse(any(m.get("type") == "open_display" for m in self.hub.fake_agent.received))

    def test_display_gets_tree_and_routes_actions(self):
        self.assertTrue(wait_for(lambda: self.hub.registry.project is not None, 10))
        ws = self.display()
        tree = self.next_of(ws, "ui", lambda m: any(
            n.get("text") == "Project Sample Project" for s in m["sections"] for n in s["items"]))
        self.assertEqual(tree["status"], {"text": "Connected · Sample Project", "tone": "ok"})
        self.assertTrue(wait_for(lambda: ("event", "panel.opened") in self.echo.log_lines))
        ws.send(json.dumps({"type": "action", "id": "echo.go", "value": 1, "revision": tree["revision"]}))
        toast = self.next_of(ws, "toast")
        self.assertEqual((toast["tone"], toast["text"]), ("ok", "Echo go"))
        self.assertIn(("action", "go", 1), self.echo.log_lines)
        ws.send(json.dumps({"type": "action", "id": "host.settings", "revision": tree["revision"]}))
        self.assertTrue(wait_for(lambda: self.opened == ["settings"]))
        self.assertIn(("toast", {"kind": "success", "message": "Echo go"}), self.events)

    def test_display_gets_the_theme_right_after_welcome_and_on_change(self):
        url = f"ws://127.0.0.1:{self.hub.server.port}/ws?role=display&token={self.hub.server.token}"
        ws = self.stack.enter_context(connect(url, open_timeout=5))
        ws.send(json.dumps({"type": "hello", "role": "display", "panel_version": "2.0.0",
                            "protocol": PROTOCOL_VERSION}))
        self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "welcome")
        self.assertEqual(json.loads(ws.recv(timeout=5)), {"type": "theme", "name": "black"})
        self.hub.settings.set("theme", "sky")
        self.assertEqual(self.hub.theme_name(), "sky")
        self.hub.notify_theme()
        self.assertEqual(self.next_of(ws, "theme"), {"type": "theme", "name": "sky"})
        second = self.display()  # welcome is consumed by the helper
        self.assertEqual(json.loads(second.recv(timeout=5)), {"type": "theme", "name": "sky"})
        with mock.patch.object(self.hub.settings, "get", return_value="nova"):  # unknown to the panel: falls back
            self.assertEqual(self.hub.theme_name(), "black")
            self.hub.notify_theme()
        self.assertEqual(self.next_of(second, "theme")["name"], "black")

    def test_ctx_ui_result_reaches_displays_the_window_and_the_activity_feed(self):
        ws = self.display()
        self.hub.registry.handle_action("echo.result", "error", wait=True)
        message = self.next_of(ws, "action_result")
        self.assertEqual((message["id"], message["outcome"], message["text"], message["detail"]),
                         ("echo.go", "error", "Echoed go", "A reason."))
        self.assertGreater(message["at"], 1_700_000_000_000)
        event = next(payload for name, payload in self.events if name == "result")
        self.assertEqual(event, {"module": "echo", "id": "echo.go", "outcome": "error", "text": "Echoed go",
                                 "detail": "A reason.", "at": message["at"]})
        line = self.hub.activity()[0]
        self.assertEqual((line["module"], line["text"], line["tone"]), ("echo", "Echoed go", "error"))
        self.assertIn(("activity", line), self.events)
        self.hub.registry.handle_action("echo.result", "ok", wait=True)  # the full id is accepted as is
        self.assertEqual(self.next_of(ws, "action_result")["outcome"], "ok")

    def test_ctx_ui_result_from_a_job_and_a_bad_outcome(self):
        ws = self.display()
        self.hub.registry.handle_action("echo.job_result", wait=True)
        self.assertEqual(self.next_of(ws, "action_result")["text"], "Job done")
        before = len(self.hub.activity())
        self.hub.action_result("echo", "echo.go", "accent", "Nope")  # not an outcome: logged and dropped
        self.assertEqual(len(self.hub.activity()), before)

    def test_ctx_host_show_reaches_the_window(self):
        self.hub.registry.handle_action("echo.show", wait=True)
        self.assertEqual(self.opened, ["echo", None])

    def test_panel_file_update_reloads_the_displays_and_the_agent(self):
        ws = self.display()
        (self.root / "panel_src" / "index.html").write_text("<html>changed</html>", "utf-8")
        result = self.hub.check_panel_update()
        self.assertTrue(result["updated"])
        self.assertEqual(self.next_of(ws, "reload"), {"type": "reload"})
        self.assertTrue(wait_for(lambda: any(m.get("type") == "reload" for m in self.hub.fake_agent.received)))

    def test_panel_manifest_update_does_not_reload(self):
        ws = self.display()
        (self.root / "panel_src" / "CSXS" / "manifest.xml").write_text("<manifest v='2'/>", "utf-8")
        self.assertTrue(self.hub.check_panel_update()["updated"])
        self.hub.toast("ok", "marker")
        message = self.next_of(ws, "toast")
        self.assertEqual(message["text"], "marker", "no reload came before the marker toast")
        self.assertFalse(any(m.get("type") == "reload" for m in self.hub.fake_agent.received))

    def test_agent_events_reach_modules(self):
        self.hub.fake_agent.fire("project.items_added", {"node_ids": ["n9"]})
        self.assertTrue(wait_for(lambda: ("event", "project.items_added") in self.echo.log_lines))

    def test_project_switch_and_close(self):
        self.assertTrue(wait_for(lambda: self.hub.registry.project is not None, 10))
        other = self.root / "Other" / "Other.prproj"
        other.parent.mkdir()
        other.write_bytes(b"")
        self.hub.fake_agent.fire("project.opened", {"path": str(other)})
        self.assertTrue(wait_for(lambda: ("project_opened", "Other") in self.echo.log_lines, 10))
        self.assertIn(("project_closed",), self.echo.log_lines)
        self.assertIn(str(other), self.hub.settings.get("recent_projects"))
        self.hub.fake_agent.fire("project.closed", {"path": str(other)})
        self.assertTrue(wait_for(lambda: self.hub.registry.project is None, 10))

    def test_confirm_from_a_job_and_activity(self):
        self.hub.registry.handle_action("echo.ask", wait=True)
        self.assertTrue(wait_for(lambda: any(name == "confirm" for name, _ in self.events)))
        question = next(p for name, p in self.events if name == "confirm")
        self.assertEqual(question["question"], "Copy 3 files?")
        self.assertTrue(self.hub.answer_confirm(question["id"], True))
        self.assertTrue(wait_for(lambda: self.echo.answer is True))
        self.assertTrue(wait_for(lambda: any(line["text"] == "Ask finished" for line in self.hub.activity())))
        self.hub.registry.handle_action("echo.note", wait=True)
        self.assertEqual(self.hub.activity()[0]["text"], "Filed clip.mp4")
        self.assertFalse(self.hub.confirm.__self__.registry.thread.on_thread())

    def test_confirm_on_module_thread_answers_no(self):
        self.assertFalse(self.hub.registry.thread.call(lambda: self.hub.confirm("Delete?")))

    def test_agent_loss_closes_the_project(self):
        self.assertTrue(wait_for(lambda: self.hub.registry.project is not None, 10))
        self.hub.fake_agent.stop()
        self.assertTrue(wait_for(lambda: self.hub.registry.project is None, 10))
        self.assertTrue(wait_for(lambda: self.hub.premiere_state()["state"] == "not_running"))

    def test_fake_launch_and_stop_removes_discovery(self):
        state = self.hub.launch_premiere(None)
        self.assertTrue(state["fake"])
        self.hub.stop()
        self.assertFalse((self.target / "deck.json").exists())


class FakeDisplayTests(HubTestBase):
    fake_display = True

    def test_fake_run_reaches_connected(self):
        self.assertTrue(wait_for(lambda: self.hub.fake_agent.display_connected.is_set(), 10))
        self.assertTrue(wait_for(lambda: self.hub.premiere_state()["state"] == "connected"))
        self.assertTrue(wait_for(lambda: any(m.get("type") == "ui" for m in self.hub.fake_agent.display_received), 10))


class StateTests(unittest.TestCase):
    """`PremiereState.state` without an agent (docs/BRIDGE_API.md 3.2)."""

    def setUp(self):
        self.root = scratch_dir("state")
        paths = AppPaths(self.root / "data")
        source = make_panel_source(self.root / "panel_src", "2.0.0")
        installer = PanelInstaller(paths, "2.0.0", source=source, target=self.root / "cep" / "bundle",
                                   registry=MemoryRegistry())
        self.launched = []
        self.project = self.root / "A.prproj"
        self.project.write_bytes(b"")
        self.hub = Hub(paths, fake=False, modules=[Echo], ports=free_ports(3), installer=installer, watch=False,
                       launch_fn=lambda exe, project: self.launched.append(project) or 1234)
        self.hub.start()

    def tearDown(self):
        self.hub.stop()
        remove_tree(self.root)

    def test_not_running_starting_waiting(self):
        state = self.hub.premiere_state()
        self.assertEqual((state["state"], state["running"], state["agent_connected"]), ("not_running", False, False))
        self.hub._on_premiere_process(True)
        state = self.hub.premiere_state()
        self.assertEqual((state["state"], state["fallback_offer"]), ("starting", False))
        self.hub._running_since -= 21  # 20 s without an agent
        state = self.hub.premiere_state()
        self.assertEqual((state["state"], state["fallback_offer"]), ("waiting", True))
        self.hub._agent_seen = True  # the agent was there and left (Premiere exiting): no offer
        self.assertFalse(self.hub.premiere_state()["fallback_offer"])
        self.hub._on_premiere_process(True)  # a new Premiere session
        self.hub._running_since -= 21
        self.assertTrue(self.hub.premiere_state()["fallback_offer"])
        self.hub.settings.set("debug_mode_written", ["CSXS.12"])
        self.assertFalse(self.hub.premiere_state()["fallback_offer"])
        self.hub._on_premiere_process(False)
        self.assertEqual(self.hub.premiere_state()["state"], "not_running")

    def test_launch_is_starting(self):
        install = mock.Mock(exe=r"C:\Adobe Premiere Pro.exe")
        with mock.patch("premiere_deck.host.launcher.is_premiere_running", return_value=False), \
                mock.patch("premiere_deck.host.launcher.best_premiere", return_value=install):
            state = self.hub.launch_premiere(None)
            self.assertEqual(state["state"], "starting")
            with mock.patch("premiere_deck.host.launcher.is_premiere_running", return_value=True):
                again = self.hub.launch_premiere(str(self.project))  # a second click while Premiere starts
        self.assertEqual(again["state"], "starting")
        self.assertEqual(self.launched, [None], "Premiere is started once")
        self.hub._launched_at -= 21
        self.assertEqual(self.hub.premiere_state()["state"], "not_running", "no process seen, no agent")


class HubStubTests(HubTestBase):
    use_stub = True

    def test_stub_facade_keeps_the_host_running(self):
        self.assertIsInstance(self.hub.premiere, StubPremiere)
        self.assertTrue(wait_for(lambda: ("project_opened", "Sample Project") in self.echo.log_lines, 10))
        self.assertTrue(wait_for(lambda: "project.items_added" in self.hub.fake_agent.subscribed),
                        "without the facade the hub sends subscribe itself")
        self.hub.fake_agent.fire("project.items_added", {})
        self.assertTrue(wait_for(lambda: ("event", "project.items_added") in self.echo.log_lines))
        with self.assertRaises(Exception):
            self.hub.premiere.snapshot()


class ProjectRuleTests(unittest.TestCase):
    def test_root_is_the_parent_when_it_holds_the_deck_folder(self):
        root = scratch_dir("rule")
        try:
            (root / ".premiere-deck").mkdir()
            (root / ".premiere-deck" / "project.json").write_text("{}", "utf-8")
            (root / "Project").mkdir()
            info = project_info_for(str(root / "Project" / "Spring.prproj"))
            self.assertEqual(info.root, str(root))
            self.assertEqual(info.name, "Spring")
            plain = project_info_for(str(root / "Edits" / "Other" / "A.prproj"))
            self.assertEqual(plain.root, str(root / "Edits" / "Other"))
            self.assertEqual(plain.deck_dir, str(root / "Edits" / "Other" / ".premiere-deck"))
        finally:
            remove_tree(root)


if __name__ == "__main__":
    unittest.main()
