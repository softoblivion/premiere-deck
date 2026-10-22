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
        return ActionResult(toast=("ok", f"Echo {action_id}"))

    def render(self):
        project = self.ctx.project.name if self.ctx.project else "none"
        return [self.button("go", "Go"), self.text(f"Project {project}")]


class HubTestBase(unittest.TestCase):
    use_stub = False

    def setUp(self):
        self.root = scratch_dir("hub")
        self.paths = AppPaths(self.root / "data")
        source = make_panel_source(self.root / "panel_src", "2.0.0")
        self.target = self.root / "cep" / "com.softoblivion.premieredeck"
        installer = PanelInstaller(self.paths, "2.0.0", source=source, target=self.target, registry=MemoryRegistry())
        self.stack = contextlib.ExitStack()
        if self.use_stub:
            self.stack.enter_context(mock.patch.dict(sys.modules, {"premiere_deck.premiere.api": None}))
        self.hub = Hub(self.paths, fake=True, modules=[Echo], ports=free_ports(3), installer=installer, watch=False)
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
    def test_install_discovery_and_project_from_hello(self):
        self.assertEqual(self.hub.install_result["action"], "installed")
        deck = json.loads((self.target / "deck.json").read_text("utf-8"))
        self.assertEqual(deck["port"], self.hub.server.port)
        self.assertTrue(wait_for(lambda: ("project_opened", "Sample Project") in self.echo.log_lines, 10))
        state = self.hub.state()
        self.assertEqual(state["premiere"]["state"], "connected")
        self.assertEqual(state["premiere"]["project"]["name"], "Sample Project")
        self.assertEqual(state["modules"][0]["id"], "echo")
        self.assertTrue(wait_for(lambda: "project.items_added" in self.hub.fake_agent.subscribed))
        self.assertTrue(any(m.get("type") == "open_display" for m in self.hub.fake_agent.received),
                        "the agent is asked to open the panel when no display is connected")

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
