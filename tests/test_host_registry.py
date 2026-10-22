"""Module registry with two tiny in-test modules (docs/MODULES.md 2): render order, action and event routing,
exception isolation, enable / disable, project hooks, settings scopes."""

import threading
import unittest

from premiere_deck.modules.base import ActionResult, FakeHost, Module, SettingField
from premiere_deck.host.registry import FAILED, MISSING, RUNNING, ModuleThread, Registry, find_module_class
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable, ProjectInfo
from tests.test_host_helpers import remove_tree, scratch_dir, wait_for


class Alpha(Module):
    id = "alpha"
    title = "Alpha"
    order = 20
    events = ["project.items_added", "panel.opened"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.calls = []
        self.thread_names = set()

    def _note(self, *call):
        self.calls.append(call)
        self.thread_names.add(threading.current_thread().name)

    def start(self):
        self._note("start")

    def stop(self):
        self._note("stop")

    def project_opened(self, project):
        self._note("project_opened", project.name)

    def project_closed(self):
        self._note("project_closed")

    def on_event(self, name, data):
        self._note("event", name, data)
        if data == "boom":
            raise RuntimeError("event handling broke")

    def on_action(self, action_id, value=None, row=None):
        self._note("action", action_id, value, row)
        if action_id == "explode":
            raise PremiereUnavailable()
        if action_id == "timeout":
            raise PremiereError("timeout")
        return ActionResult(toast=("ok", "Done"))

    def render(self):
        return [self.button("go", "Go"), self.text(f"{len(self.calls)} calls", small=True)]

    def render_host(self):
        return {"blocks": [{"type": "actions", "items": [self.button("go", "Go")]}]}

    def settings_schema(self):
        return [SettingField("count", "Count", "int", 3),
                SettingField("mode", "Mode", "select", "a", options=[{"value": "a", "label": "A"},
                                                                     {"value": "b", "label": "B"}]),
                SettingField("watch", "Watch", "bool", True, scope="project"),
                SettingField("bins", "Bins", "text", "A, B")]

    def on_setting_changed(self, key, scope, value):
        self._note("setting", key, scope, value)
        if key == "mode":
            raise RuntimeError("side effect broke")


class Beta(Module):
    id = "beta"
    title = "Beta"
    order = 10
    events = ["project.items_added"]
    default_enabled = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self.events_seen = []
        self.broken_render = False

    def on_event(self, name, data):
        self.events_seen.append(name)

    def render(self):
        if self.broken_render:
            raise ValueError("render broke")
        return [self.toggle("on", "On", True)]


class Broken(Module):
    id = "broken"
    title = "Broken"
    order = 5

    def start(self):
        raise RuntimeError("cannot start")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("registry")
        self.trees = []
        self.toasts = []
        self.enabled = {}
        self.hosts = {}

        def factory(module_id, registry):
            host = FakeHost(module_id=module_id)
            self.hosts[module_id] = host
            return host.context()

        self.registry = Registry(factory, modules=[Alpha, Beta, Broken, "not_a_module_package"],
                                 is_enabled=lambda mid, default: self.enabled.get(mid, default),
                                 set_enabled=lambda mid, value: self.enabled.__setitem__(mid, value),
                                 on_tree=self.trees.append, toast=lambda tone, text: self.toasts.append((tone, text)),
                                 host_status=lambda: {"text": "Connected", "tone": "ok"}, version="9.9.9")
        self.registry.load()
        self.registry.start()
        self.alpha = self.registry.entry("alpha").instance
        self.beta = self.registry.entry("beta").instance

    def tearDown(self):
        self.registry.stop()
        remove_tree(self.root)

    def tree(self):
        self.registry.invalidate()
        return self.registry.wait_rendered()

    def test_states_and_isolation_of_a_failing_start(self):
        states = {e.id: e.state for e in self.registry.entries}
        self.assertEqual(states["alpha"], RUNNING)
        self.assertEqual(states["beta"], RUNNING)
        self.assertEqual(states["broken"], FAILED)
        self.assertEqual(states["not_a_module_package"], MISSING)
        self.assertIn(("error", "Broken: cannot start."), self.toasts)
        info = {m["id"]: m for m in self.registry.modules()}
        self.assertEqual(info["broken"]["error"], "cannot start.")
        self.assertTrue(info["alpha"]["has_page"] and info["alpha"]["has_settings"])
        self.assertFalse(info["beta"]["has_page"])

    def test_tree_order_status_and_footer(self):
        tree = self.tree()
        self.assertEqual([s["id"] for s in tree["sections"]], ["broken", "beta", "alpha"])
        self.assertEqual(tree["status"], {"text": "Connected", "tone": "ok"})
        self.assertEqual(tree["footer"]["text"], "Premiere Deck 9.9.9")
        self.assertEqual(tree["footer"]["link"]["action"], "host.open")
        ids = [n.get("id") for n in tree["sections"][2]["items"]]
        self.assertIn("alpha.go", ids)
        revision = tree["revision"]
        self.assertGreater(self.tree()["revision"], revision)

    def test_render_error_is_isolated(self):
        self.beta.broken_render = True
        tree = self.tree()
        beta = next(s for s in tree["sections"] if s["id"] == "beta")
        self.assertEqual(beta["status"]["tone"], "error")
        self.assertTrue(any(s["id"] == "alpha" for s in tree["sections"]))

    def test_coalesced_rerender(self):
        before = len(self.trees)
        for _ in range(20):
            self.registry.invalidate()
        self.registry.wait_rendered()
        self.assertLessEqual(len(self.trees) - before, 2)

    def test_action_routing_by_prefix(self):
        result = self.registry.handle_action("alpha.go", 5, "row1", wait=True)
        self.assertEqual(result, {"ok": True, "toast": ["ok", "Done"]})
        self.assertIn(("action", "go", 5, "row1"), self.alpha.calls)
        self.assertIn(("ok", "Done"), self.toasts)
        self.registry.handle_action("host:journal.undo", None, None, module_id="alpha", wait=True)
        self.assertIn(("action", "host:journal.undo", None, None), self.alpha.calls)
        self.assertEqual(self.registry.handle_action("nobody.x", wait=True), {"ok": False, "toast": None})
        self.assertEqual(self.alpha.thread_names, {"module-thread"})

    def test_action_exceptions_become_toasts(self):
        result = self.registry.handle_action("alpha.explode", wait=True)
        self.assertFalse(result["ok"])
        self.assertIn(("error", "Alpha: Premiere Pro is not connected."), self.toasts)
        self.registry.handle_action("alpha.timeout", wait=True)
        self.assertIn(("error", "Alpha: Premiere is waiting for a dialog."), self.toasts)
        self.assertEqual(self.registry.entry("alpha").state, RUNNING, "the module stays alive")

    def test_event_routing_by_subscription(self):
        self.registry.dispatch_event("project.items_added", {"node_ids": ["n1"]})
        self.registry.dispatch_event("panel.opened", {})
        self.registry.dispatch_event("sequence.activated", {})
        self.registry.dispatch_event("project.items_added", "boom")
        self.registry.thread.call(lambda: None)
        alpha_events = [c[1] for c in self.alpha.calls if c[0] == "event"]
        self.assertEqual(alpha_events, ["project.items_added", "panel.opened", "project.items_added"])
        self.assertEqual(self.beta.events_seen, ["project.items_added", "project.items_added"])
        self.assertIn(("error", "Alpha: event handling broke."), self.toasts)
        self.assertEqual(self.registry.events(), ["panel.opened", "project.items_added"])

    def test_project_hooks_and_context(self):
        project = ProjectInfo("C:\\p\\Spring.prproj", "C:\\p", "Spring", "C:\\p\\.premiere-deck")
        self.registry.project_opened(project)
        self.registry.project_opened(project)  # same path: no second call
        self.registry.thread.call(lambda: None)
        self.assertEqual([c for c in self.alpha.calls if c[0] == "project_opened"], [("project_opened", "Spring")])
        self.assertEqual(self.alpha.ctx.project, project)
        self.registry.project_closed()
        self.registry.thread.call(lambda: None)
        self.assertIn(("project_closed",), self.alpha.calls)
        self.assertIsNone(self.alpha.ctx.project)

    def test_enable_disable(self):
        info = self.registry.set_enabled("alpha", False)
        self.assertEqual(info["state"], "disabled")
        self.assertIn(("stop",), self.alpha.calls)
        self.assertEqual(self.enabled["alpha"], False)
        self.assertNotIn("alpha", [s["id"] for s in self.tree()["sections"]])
        self.assertFalse(self.registry.handle_action("alpha.go", wait=True)["ok"])
        self.registry.set_enabled("alpha", True)
        self.assertEqual(self.alpha.calls.count(("start",)), 2)
        with self.assertRaises(ValueError):
            self.registry.set_enabled("not_a_module_package", True)

    def test_disabled_by_setting_is_created_but_not_started(self):
        registry = Registry(lambda mid, reg: FakeHost(module_id=mid).context(), modules=[Alpha],
                            is_enabled=lambda mid, default: False)
        try:
            registry.load()
            registry.start()
            entry = registry.entry("alpha")
            self.assertEqual(entry.state, "disabled")
            self.assertEqual(entry.instance.calls, [])
        finally:
            registry.stop()

    def test_pages_and_settings(self):
        page = self.registry.module_page("alpha")
        self.assertEqual(page["page"]["blocks"][0]["type"], "actions")
        self.assertIsNone(self.registry.module_page("beta")["page"])
        settings = self.registry.module_settings("alpha")
        fields = {f["key"]: f for f in settings["fields"]}
        self.assertEqual(fields["count"]["value"], 3)
        self.assertFalse(fields["watch"]["available"])
        self.assertEqual(self.registry.set_module_setting("alpha", "count", "7"), 7)
        self.assertEqual(self.hosts["alpha"].settings.get("count"), 7)
        self.assertIn(("setting", "count", "host", 7), self.alpha.calls)  # the hook runs after the store
        self.assertEqual(self.registry.set_module_setting("alpha", "mode", "b"), "b")  # a raising hook is guarded
        self.assertEqual(self.hosts["alpha"].settings.get("mode"), "b")
        self.hosts["alpha"].settings.set("bins", ["A", "B", "C"])  # a list behind a text field is shown joined
        fields = {f["key"]: f for f in self.registry.module_settings("alpha")["fields"]}
        self.assertEqual(fields["bins"]["value"], "A, B, C")
        with self.assertRaises(ValueError):
            self.registry.set_module_setting("alpha", "mode", "z")
        with self.assertRaises(ValueError):
            self.registry.set_module_setting("alpha", "watch", True, "project")
        with self.assertRaises(ValueError):
            self.registry.set_module_setting("alpha", "count", 1, "project")
        self.registry.project_opened(ProjectInfo("C:\\p\\S.prproj", "C:\\p", "S", "C:\\p\\.premiere-deck"))
        self.assertIs(self.registry.set_module_setting("alpha", "watch", False, "project"), False)
        self.assertEqual(self.hosts["alpha"].settings.project.get("watch"), False)


class HelpersTests(unittest.TestCase):
    def test_module_thread_call_and_inline(self):
        thread = ModuleThread("test-thread")
        try:
            self.assertEqual(thread.call(lambda: threading.current_thread().name), "test-thread")
            self.assertEqual(thread.call(lambda: thread.call(lambda: 5)), 5, "nested call runs inline")
            with self.assertRaises(ZeroDivisionError):
                thread.call(lambda: 1 / 0)
        finally:
            thread.stop()

    def test_find_module_class(self):
        import types

        mod = types.ModuleType("fake_mod")
        with self.assertRaises(ImportError):
            find_module_class(mod)
        Alpha.__module__  # noqa: B018
        mod.Alpha = Alpha
        self.assertIs(find_module_class(mod), Alpha)


if __name__ == "__main__":
    unittest.main()
