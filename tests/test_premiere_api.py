"""Tests of the Premiere facade (premiere/api.py) against a fake agent transport (docs/PREMIERE_API.md 2-4)."""

from __future__ import annotations

import os
import queue
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Optional
from unittest import mock

from premiere_deck.host import protocol
from premiere_deck.premiere import api as api_module
from premiere_deck.premiere.api import PremiereApi, project_info_from_path, same_path
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable

JSX_DIR = Path(api_module.__file__).resolve().parent / "jsx"
PANEL_DIR = JSX_DIR.parent.parent / "panel"


class FakeTransport:
    """`AgentTransport` whose handler calls run on one "loop" thread, like the server's asyncio loop.

    `responder(message)` returns the agent's reply (a dict, a list of dicts, or None for no reply) to each sent
    message; `sent` records every message.
    """

    def __init__(self, responder: Optional[Callable[[dict], Any]] = None) -> None:
        self.connected = False
        self.sent: list[dict] = []
        self.responder = responder
        self.handler: Callable[[dict], None] = lambda message: None
        self.presence: Callable[[bool, Optional[protocol.Hello]], None] = lambda c, h: None
        self._tasks: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self.loop = threading.Thread(target=self._run, daemon=True)
        self.loop.start()

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            task()

    def call_soon(self, fn: Callable[[], None]) -> None:
        self._tasks.put(fn)

    def drain(self) -> None:
        done = threading.Event()
        self.call_soon(done.set)
        done.wait(2)

    def close(self) -> None:
        self._tasks.put(None)

    # AgentTransport
    def send(self, message: dict) -> None:
        self.sent.append(message)
        if self.responder is None:
            return
        reply = self.responder(message)
        if reply is None:
            return
        for item in reply if isinstance(reply, list) else [reply]:
            self.call_soon(lambda item=item: self.handler(item))

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        self.handler = handler

    def set_presence_handler(self, handler: Callable[[bool, Optional[protocol.Hello]], None]) -> None:
        self.presence = handler

    # test helpers
    def connect(self, project_path: Optional[str] = None) -> None:
        hello = protocol.Hello.parse({"type": "hello", "role": "agent", "panel_version": "0.1.0", "protocol": 1,
                                      "project_path": project_path})
        self.connected = True
        self.call_soon(lambda: self.presence(True, hello))
        self.drain()

    def disconnect(self) -> None:
        self.connected = False
        self.call_soon(lambda: self.presence(False, None))
        self.drain()

    def push(self, message: dict) -> None:
        self.call_soon(lambda: self.handler(message))
        self.drain()

    def scripts(self) -> list[dict]:
        return [m for m in self.sent if m["type"] == protocol.T_SCRIPT]


def ok(message: dict, result: Any) -> dict:
    return {"type": "script.result", "id": message["id"], "ok": True, "result": result, "ms": 1}


def by_name(results: dict) -> Callable[[dict], Any]:
    """Responder answering scripts by name from `results` (a value or a callable of the args)."""

    def respond(message: dict) -> Any:
        if message["type"] != protocol.T_SCRIPT:
            return None
        value = results[message["name"]]
        return ok(message, value(message["args"]) if callable(value) else value)

    return respond


def item_json(node_id: str, name: str, **extra: Any) -> dict:
    data = {"node_id": node_id, "name": name, "kind": "clip", "tree_path": "", "media_path": "C:\\m\\" + name,
            "is_offline": False, "is_sequence": False, "duration": 2.0, "has_video": True, "has_audio": True,
            "color_label": 1}
    data.update(extra)
    return data


SEQ = {"sequence_id": "s1", "name": "Main", "width": 1920, "height": 1080, "fps": 25.0, "duration": 10.0,
       "in_point": None, "out_point": 4.0}
TRACK_ITEM = {"sequence_id": "s1", "track_kind": "video", "track_index": 0, "index": 0, "name": "a.mp4",
              "node_id": "n1", "start": 0.0, "end": 2.0, "in_point": 0.0, "out_point": 2.0, "selected": True}


class Base(unittest.TestCase):
    def make(self, responder: Optional[Callable[[dict], Any]] = None, *, connect: bool = True,
             project: Optional[str] = None) -> tuple[PremiereApi, FakeTransport]:
        transport = FakeTransport(responder)
        self.addCleanup(transport.close)
        api = PremiereApi(transport, JSX_DIR)
        if connect:
            transport.connect(project)
        return api, transport


class QueueTests(Base):
    def test_one_script_in_flight(self) -> None:
        pending: list[dict] = []
        api, transport = self.make(lambda m: pending.append(m) if m["type"] == "script" else None)
        results: dict[str, Any] = {}

        def call(name: str) -> None:
            results[name] = api.eval(name, "return 1;")

        threads = [threading.Thread(target=call, args=(f"c{i}",)) for i in range(3)]
        for thread in threads:
            thread.start()
        for expected in range(1, 4):
            deadline = time.monotonic() + 2
            while len(pending) < expected and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            # Never more than one unanswered script at the agent.
            self.assertEqual(len(pending), expected)
            message = pending[expected - 1]
            transport.push(ok(message, message["name"]))
        for thread in threads:
            thread.join(2)
        self.assertEqual(sorted(results), ["c0", "c1", "c2"])
        self.assertEqual({k: v for k, v in results.items()}, {name: name for name in results})

    def test_watchdog_timeout_releases_the_slot(self) -> None:
        answered: list[dict] = []

        def respond(message: dict) -> Any:
            if message["type"] != "script":
                return None
            if message["name"] == "stuck":
                return None
            answered.append(message)
            return ok(message, 7)

        api, _ = self.make(respond)
        with mock.patch.object(api_module, "TIMEOUT_GRACE_S", 0.05), self.assertLogs("premiere.api", "WARNING"):
            with self.assertRaises(PremiereError) as caught:
                api.eval("stuck", "return 1;", timeout_ms=50)
        self.assertEqual(caught.exception.message, "timeout")
        self.assertEqual(api.eval("next", "return 7;"), 7)

    def test_agent_timeout_is_an_error(self) -> None:
        def respond(message: dict) -> Any:
            if message["type"] == "script":
                return {"type": "script.result", "id": message["id"], "ok": False,
                        "error": {"message": "timeout", "line": None, "source": "x"}, "ms": 30000}
            return None

        api, _ = self.make(respond)
        with self.assertRaises(PremiereError) as caught, self.assertLogs("premiere.api", "WARNING"):
            api.eval("slow", "return 1;")
        self.assertEqual(caught.exception.message, "timeout")

    def test_late_result_is_ignored(self) -> None:
        api, transport = self.make(lambda m: None)
        transport.push({"type": "script.result", "id": "q999", "ok": True, "result": 1})
        self.assertFalse(api.is_connected() is False)

    def test_not_connected(self) -> None:
        api, _ = self.make(connect=False)
        with self.assertRaises(PremiereUnavailable):
            api.eval("x", "return 1;")

    def test_disconnect_fails_pending_calls(self) -> None:
        api, transport = self.make(lambda m: None)
        errors: list[BaseException] = []

        def call() -> None:
            try:
                api.eval("x", "return 1;")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(0.1)
        transport.disconnect()
        thread.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PremiereUnavailable)

    def test_close_fails_pending_and_later_calls(self) -> None:
        api, _ = self.make(lambda m: None)
        errors: list[BaseException] = []
        thread = threading.Thread(target=lambda: errors.append(self._call(api)))
        thread.start()
        time.sleep(0.1)
        api.close()
        thread.join(2)
        self.assertIsInstance(errors[0], PremiereUnavailable)
        with self.assertRaises(PremiereUnavailable):
            api.eval("x", "return 1;")

    @staticmethod
    def _call(api: PremiereApi) -> Optional[BaseException]:
        try:
            api.eval("x", "return 1;")
        except BaseException as exc:
            return exc
        return None

    def test_paged_result(self) -> None:
        def respond(message: dict) -> Any:
            if message["type"] != "script":
                return None
            text = '{"items": [1, 2, 3], "name": "é"}'
            parts = [text[:10], text[10:20], text[20:]]
            return [{"type": "script.result", "id": message["id"], "ok": True, "page": i + 1, "pages": 3,
                     "chunk": part, "ms": 5} for i, part in enumerate(parts)]

        api, _ = self.make(respond)
        self.assertEqual(api.eval("big", "return 1;"), {"items": [1, 2, 3], "name": "é"})


class ErrorTests(Base):
    def test_error_mapping_and_lib_line_offset(self) -> None:
        lib_lines = (JSX_DIR / "lib.jsx").read_text(encoding="utf-8").rstrip("\n").count("\n") + 1

        def respond(message: dict) -> Any:
            if message["type"] != "script":
                return None
            return {"type": "script.result", "id": message["id"], "ok": False,
                    "error": {"message": "boom", "line": lib_lines + 3, "source": "var x"}, "ms": 2}

        api, _ = self.make(respond)
        with self.assertRaises(PremiereError) as caught:
            api.eval("e", "// @uses lib\nvar a = 1;\nthrow new Error('boom');")
        self.assertEqual(caught.exception.message, "boom")
        self.assertEqual(caught.exception.line, 3)
        self.assertEqual(caught.exception.source, "var x")

    def test_lib_prepended_and_marker_neutralised(self) -> None:
        api, transport = self.make(by_name({"a": 1, "b": 2}))
        api.eval("a", "// @uses lib\nreturn 1;")
        api.eval("b", "return 2;")
        first, second = transport.scripts()
        self.assertIn("function binByPath(", first["code"])
        self.assertNotIn("// @uses lib", first["code"])
        self.assertIn(api_module.USES_LIB_SENT, first["code"])
        self.assertEqual(second["code"], "return 2;")

    def test_eval_reads_a_jsx_path_once(self) -> None:
        api, transport = self.make(by_name({"v": "26.5.2"}))
        path = str(JSX_DIR / "version.jsx")
        self.assertEqual(api.eval("v", path), "26.5.2")
        self.assertIn("app.version", transport.scripts()[0]["code"])
        self.assertIs(api.script(path), api.script(path))


class OperationTests(Base):
    def test_project_root_rule(self) -> None:
        home = os.environ.get("PREMIERE_DECK_HOME") or tempfile.mkdtemp()
        root = Path(tempfile.mkdtemp(dir=home))
        (root / "Project").mkdir()
        flat = project_info_from_path(str(root / "Project" / "Edit.prproj"))
        self.assertEqual(flat.root, str(root / "Project"))
        (root / ".premiere-deck").mkdir()
        layout = project_info_from_path(str(root / "Project" / "Edit.prproj"))
        self.assertEqual(layout.root, str(root))
        self.assertEqual(layout.name, "Edit")
        self.assertEqual(layout.deck_dir, str(root / ".premiere-deck"))
        # Same rule as hub.project_info_for: the project's own .premiere-deck folder wins.
        (root / "Project" / ".premiere-deck").mkdir()
        own = project_info_from_path(str(root / "Project" / "Edit.prproj"))
        self.assertEqual(own.root, str(root / "Project"))
        from premiere_deck.host.hub import project_info_for
        self.assertEqual(project_info_for(str(root / "Project" / "Edit.prproj")).root, own.root)

    def test_project_none(self) -> None:
        api, _ = self.make(by_name({"project_info": None}))
        self.assertIsNone(api.project())

    def test_snapshot_pages(self) -> None:
        items = [item_json(f"n{i}", f"c{i}.mp4") for i in range(5)]

        def page(args: dict) -> dict:
            start = args["start"]
            end = min(len(items), start + args["limit"])
            if start and args["token"] != "t1":
                return {"stale": True}
            return {"token": "t1", "project_path": "C:\\p\\a.prproj", "total": len(items), "items": items[start:end],
                    "next": end if end < len(items) else None}

        api, transport = self.make(by_name({"snapshot": page}))
        with mock.patch.object(api_module, "SNAPSHOT_PAGE_ITEMS", 2):
            snapshot = api.snapshot()
        self.assertEqual([item.node_id for item in snapshot.items], [f"n{i}" for i in range(5)])
        self.assertEqual(snapshot.project_path, "C:\\p\\a.prproj")
        self.assertEqual(len(transport.scripts()), 3)
        self.assertEqual(transport.scripts()[0]["timeout_ms"], api_module.SNAPSHOT_TIMEOUT_MS)

    def test_snapshot_restarts_when_stale(self) -> None:
        calls: list[dict] = []

        def page(args: dict) -> dict:
            calls.append(dict(args))
            if len(calls) == 2:
                return {"stale": True}
            if args["start"] == 0:
                return {"token": "t", "project_path": "p", "items": [item_json("a", "a")], "next": 1}
            return {"token": "t", "project_path": "p", "items": [item_json("b", "b")], "next": None}

        api, _ = self.make(by_name({"snapshot": page}))
        snapshot = api.snapshot()
        self.assertEqual([item.node_id for item in snapshot.items], ["a", "b"])
        self.assertEqual([c["start"] for c in calls], [0, 1, 0, 1])

    def test_decoding(self) -> None:
        api, _ = self.make(by_name({
            "item": lambda args: item_json(args["node_id"], "x.mp4", tree_path="Footage/Day1", duration=None),
            "sequences": [SEQ],
            "active_sequence": SEQ,
            "selection": {"sequence_id": "s1", "items": [TRACK_ITEM]},
            "track_items": [TRACK_ITEM, dict(TRACK_ITEM, index=1, node_id=None, selected=False)],
            "playhead": {"seconds": 3.5},
            "create_bin_path": {"node_id": "b1"},
            "version": "26.5.2",
            "workspace": {"workspaces": ["Editing", "Color"]},
            "import_files": {"items": [item_json("n9", "new.mp4", tree_path="Audio")], "missing": [], "failed": []},
        }))
        item = api.item("n1")
        self.assertEqual((item.node_id, item.tree_path, item.full_path, item.duration), ("n1", "Footage/Day1", "Footage/Day1/x.mp4", None))
        self.assertEqual(api.sequences()[0].out_point, 4.0)
        self.assertIsNone(api.active_sequence().in_point)
        selection = api.selection()
        self.assertTrue(selection)
        self.assertEqual(selection.items[0].node_id, "n1")
        self.assertIsNone(api.track_items("s1")[1].node_id)
        self.assertEqual(api.playhead(), 3.5)
        self.assertEqual(api.create_bin_path("A/B"), "b1")
        self.assertEqual(api.premiere_version(), "26.5.2")
        self.assertEqual(api.workspaces(), ["Editing", "Color"])
        self.assertEqual([i.node_id for i in api.import_files(["C:\\new.mp4"], "Audio")], ["n9"])

    def test_empty_selection_without_sequence(self) -> None:
        api, _ = self.make(by_name({"selection": {"sequence_id": "", "items": []}, "active_sequence": None}))
        self.assertFalse(api.selection())
        self.assertIsNone(api.active_sequence())

    def test_missing_items_raise(self) -> None:
        api, _ = self.make(by_name({"move_to_bin": {"moved": 1, "missing": ["n2"]},
                                    "set_color_label": {"missing": ["n3"]}}))
        with self.assertRaises(PremiereError):
            api.move_to_bin(["n1", "n2"], "A")
        with self.assertRaises(PremiereError):
            api.set_color_label(["n3"], 2)
        with self.assertRaises(ValueError):
            api.set_color_label(["n3"], 16)

    def test_remove_items_returns_the_count_and_logs_missing(self) -> None:
        api, transport = self.make(by_name({"remove_items": {"removed": 2, "missing": ["n3"]}}))
        with self.assertLogs("premiere.api", "WARNING") as logs:
            self.assertEqual(api.remove_items(["n1", "n2", "n3"]), 2)
        self.assertIn("n3", "\n".join(logs.output))
        self.assertEqual(transport.scripts()[0]["args"], {"node_ids": ["n1", "n2", "n3"]})

    def test_import_failures_are_logged(self) -> None:
        api, _ = self.make(by_name({"import_files": {"items": [], "missing": ["C:\\gone.mp4"], "failed": ["C:\\bad.wav"]}}))
        with self.assertLogs("premiere.api", "WARNING") as logs:
            self.assertEqual(api.import_files(["C:\\gone.mp4", "C:\\bad.wav"], ""), [])
        self.assertIn("C:\\bad.wav", "\n".join(logs.output))

    def test_open_in_source_marker_fallback(self) -> None:
        api, transport = self.make(by_name({"open_in_source": {"positioned": False, "position": 0}, "add_marker": {}}))
        api.open_in_source("n1", seconds=2.0)
        names = [m["name"] for m in transport.scripts()]
        self.assertEqual(names, ["open_in_source", "add_marker"])
        self.assertEqual(transport.scripts()[1]["args"]["name"], api_module.MARKER_FALLBACK_NAME)
        api.open_in_source("n1")
        self.assertEqual(len(transport.scripts()), 3)

    def test_positioned_source_needs_no_marker(self) -> None:
        api, transport = self.make(by_name({"open_in_source": {"positioned": True, "position": 2.0}}))
        api.open_in_source("n1", seconds=2.0)
        self.assertEqual([m["name"] for m in transport.scripts()], ["open_in_source"])

    def test_simple_operations_send_their_args(self) -> None:
        api, transport = self.make(by_name({name: {"ok": True} for name in (
            "relink", "rename_item", "insert_clip", "add_marker", "save", "open_project", "playhead", "workspace")}))
        api.relink("n1", "C:\\a.mp4")
        api.rename_item("n1", "b")
        api.insert_clip("n1", start=1.0, end=2.0)
        api.add_marker("s1", 1.5, "M", "c")
        api.save_project()
        api.save_project_as("C:\\p\\v2.prproj")
        api.open_project("C:\\p\\a.prproj")
        api.set_playhead(4)
        api.set_workspace("Editing")
        args = [(m["name"], m["args"]) for m in transport.scripts()]
        self.assertEqual(args[0], ("relink", {"node_id": "n1", "media_path": "C:\\a.mp4"}))
        self.assertEqual(args[2][1], {"node_id": "n1", "start": 1.0, "end": 2.0, "at": None, "video_track": 0,
                                      "audio_track": 0})
        self.assertEqual(args[4], ("save", {"path": None}))
        self.assertEqual(args[5], ("save", {"path": "C:\\p\\v2.prproj"}))
        self.assertEqual(args[7], ("playhead", {"set": 4.0}))
        with self.assertRaises(ValueError):
            api.insert_clip("n1", start=2.0, end=1.0)

    def test_relink_result(self) -> None:
        api, _ = self.make(by_name({"relink": {"raw": 0, "online": False}}))
        self.assertFalse(api.relink("n1", "C:\\x.mp4"))


class EventTests(Base):
    def collect(self, api: PremiereApi) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        api.on_event(lambda name, data: events.append((name, data)))
        return events

    def test_subscribe_on_set_and_on_connect(self) -> None:
        api, transport = self.make(connect=False)
        api.set_events(["sequence.activated", "project.items_added", "sequence.activated"])
        self.assertEqual([m for m in transport.sent if m["type"] == "subscribe"], [])
        transport.connect()
        subscribe = [m for m in transport.sent if m["type"] == "subscribe"]
        self.assertEqual(subscribe[-1]["events"], ["project.items_added", "sequence.activated"])
        api.set_events(["sequence.selection_changed"])
        self.assertEqual(transport.sent[-1], {"type": "subscribe", "events": ["sequence.selection_changed"]})
        with self.assertRaises(protocol.ProtocolError):
            api.set_events(["no.such.event"])

    def test_events_are_delivered(self) -> None:
        api, transport = self.make()
        events = self.collect(api)
        transport.push({"type": "event", "name": "sequence.activated", "data": {"sequence_id": "s2", "name": "B"}, "at": 1})
        transport.push({"type": "event", "name": "project.items_added", "data": {}, "at": 2})
        self.assertEqual(events, [("sequence.activated", {"sequence_id": "s2", "name": "B"}), ("project.items_added", {})])
        self.assertEqual(api.active_sequence_id, "s2")

    def test_project_change_from_heartbeats(self) -> None:
        api, transport = self.make(project="C:\\p\\A.prproj")
        events = self.collect(api)
        self.assertEqual(api.project_path, "C:\\p\\A.prproj")
        transport.push({"type": "heartbeat", "project_path": "c:/P/a.PRPROJ", "active_sequence_id": "s1", "at": 1})
        self.assertEqual(events, [])
        transport.push({"type": "heartbeat", "project_path": "C:\\p\\B.prproj", "active_sequence_id": None, "at": 2})
        self.assertEqual(events, [("project.closed", {"path": "C:\\p\\A.prproj"}), ("project.opened", {"path": "C:\\p\\B.prproj"})])
        transport.push({"type": "heartbeat", "project_path": None, "active_sequence_id": None, "at": 3})
        self.assertEqual(events[-1], ("project.closed", {"path": "C:\\p\\B.prproj"}))

    def test_agent_project_events_are_deduplicated(self) -> None:
        api, transport = self.make()
        events = self.collect(api)
        transport.push({"type": "event", "name": "project.opened", "data": {"path": "C:\\p\\A.prproj"}, "at": 1})
        transport.push({"type": "heartbeat", "project_path": "C:\\p\\A.prproj", "active_sequence_id": None, "at": 2})
        transport.push({"type": "event", "name": "project.closed", "data": {"path": "C:\\p\\A.prproj"}, "at": 3})
        self.assertEqual([name for name, _ in events], ["project.opened", "project.closed"])

    def test_connect_and_disconnect_report_the_project(self) -> None:
        api, transport = self.make(connect=False)
        events = self.collect(api)
        transport.connect("C:\\p\\A.prproj")
        transport.disconnect()
        self.assertEqual([name for name, _ in events], ["project.opened", "project.closed"])
        self.assertFalse(api.is_connected())

    def test_agent_log_goes_to_panel_logger(self) -> None:
        _, transport = self.make()
        with self.assertLogs("panel.agent", "WARNING"):
            transport.push({"type": "log", "level": "warning", "message": "slow"})

    def test_callback_errors_do_not_break_delivery(self) -> None:
        api, transport = self.make()
        api.on_event(lambda name, data: 1 / 0)
        events = self.collect(api)
        with self.assertLogs("premiere.api", "ERROR"):
            transport.push({"type": "event", "name": "project.changed", "data": {}, "at": 1})
        self.assertEqual(events, [("project.changed", {})])


class ThreadTests(Base):
    def test_call_from_transport_thread_raises(self) -> None:
        api, transport = self.make(by_name({"x": 1}))
        transport.push({"type": "heartbeat", "project_path": None, "at": 1})
        errors: list[BaseException] = []

        def inside() -> None:
            try:
                api.eval("x", "return 1;")
            except BaseException as exc:
                errors.append(exc)

        transport.call_soon(inside)
        transport.drain()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_module_thread_binding(self) -> None:
        api, _ = self.make(by_name({"x": 1}))
        api.bind_module_thread()
        self.assertEqual(api.eval("x", "return 1;"), 1)
        errors: list[BaseException] = []

        def job() -> None:
            try:
                api.eval("x", "return 1;")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=job)
        thread.start()
        thread.join(2)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_run_on_module_thread(self) -> None:
        api, _ = self.make(by_name({"x": 5}))
        work: "queue.Queue[Callable[[], None]]" = queue.Queue()
        module_thread = threading.Thread(target=lambda: [work.get()() for _ in range(1)], daemon=True)
        api.bind_module_thread(module_thread, submit=work.put)
        module_thread.start()
        future = api.run_on_module_thread(api.eval, "x", "return 5;")
        self.assertEqual(future.result(2), 5)

    def test_host_alias_hooks(self) -> None:
        api, transport = self.make(by_name({"x": 3}))
        events: list[tuple[str, Any]] = []
        api.set_event_handler(lambda name, data: events.append((name, data)))
        api.set_subscriptions(["project.changed"])
        self.assertEqual(transport.sent[-1], {"type": "subscribe", "events": ["project.changed"]})
        transport.push({"type": "event", "name": "project.changed", "data": {}, "at": 1})
        self.assertEqual(events, [("project.changed", {})])
        work: "queue.Queue[Callable[[], None]]" = queue.Queue()
        module_thread = threading.Thread(target=lambda: work.get()(), daemon=True)
        api.bind_module_thread(module_thread)
        api.set_module_executor(work.put)
        module_thread.start()
        self.assertEqual(api.run_on_module_thread(api.eval, "x", "return 3;").result(2), 3)

    def test_run_on_module_thread_needs_an_executor(self) -> None:
        api, _ = self.make()
        other = threading.Thread(target=lambda: None)
        other.start()
        other.join()
        api.bind_module_thread(other)
        with self.assertRaises(RuntimeError):
            api.run_on_module_thread(lambda: 1)


class PathTests(unittest.TestCase):
    def test_same_path(self) -> None:
        self.assertTrue(same_path("C:\\A\\b.prproj", "c:/a/B.PRPROJ"))
        self.assertTrue(same_path(None, ""))
        self.assertFalse(same_path("C:\\a", None))


class JsxTests(unittest.TestCase):
    """The jsx files follow docs/PREMIERE_API.md 2 and 5."""

    OPERATIONS = ("project_info", "snapshot", "item", "create_bin_path", "move_to_bin", "relink", "import_files",
                  "rename_item", "remove_items", "set_color_label", "active_sequence", "sequences", "selection", "track_items",
                  "playhead", "open_in_source", "insert_clip", "add_marker", "save", "open_project", "workspace",
                  "version")
    LIB_FUNCTIONS = ("walk", "findByNodeId", "binByPath", "itemInfo", "ticksToSeconds", "secondsToTicks", "pathOf",
                     "safeMediaPath", "requireItem", "requireSequence", "findSequence", "sequenceInfo",
                     "trackItemInfo", "eachTrackItem", "columnsOf", "splitTreePath", "timeFromSeconds")

    def test_every_operation_has_a_file(self) -> None:
        for name in self.OPERATIONS:
            self.assertTrue((JSX_DIR / f"{name}.jsx").is_file(), name)

    def test_lib_provides_the_helpers(self) -> None:
        lib = (JSX_DIR / "lib.jsx").read_text(encoding="utf-8")
        for name in self.LIB_FUNCTIONS:
            self.assertRegex(lib, rf"function {name}\(", name)

    def test_scripts_declare_lib_when_they_use_it(self) -> None:
        pattern = re.compile(r"\b(" + "|".join(self.LIB_FUNCTIONS) + r")\(")
        for path in JSX_DIR.glob("*.jsx"):
            if path.name == "lib.jsx":
                continue
            text = path.read_text(encoding="utf-8")
            head = text.splitlines()[:3]
            if pattern.search(text):
                self.assertIn(api_module.USES_LIB, [line.strip() for line in head], path.name)

    def test_no_dialogs_or_console(self) -> None:
        for path in JSX_DIR.glob("*.jsx"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\b(alert|confirm|prompt)\(|\$\.writeln", path.name)

    def test_host_jsx_contains_json2_unmodified(self) -> None:
        json2 = (JSX_DIR / "json2.js").read_text(encoding="utf-8")
        host = (PANEL_DIR / "host.jsx").read_text(encoding="utf-8")
        self.assertIn(json2, host)


if __name__ == "__main__":
    unittest.main()
