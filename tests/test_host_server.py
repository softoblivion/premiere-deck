"""Server handshake and routing against a real `websockets` client on free ports (docs/PROTOCOL.md 1-3, 6)."""

import contextlib
import json
import unittest

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from premiere_deck import PROTOCOL_VERSION
from premiere_deck.host import protocol as P
from premiere_deck.host.server import DeckServer, is_loopback, parse_query
from tests.test_host_helpers import free_ports, remove_tree, scratch_dir, wait_for


def hello(role, version="1.0.0", project=None, protocol=PROTOCOL_VERSION):
    return json.dumps({"type": "hello", "role": role, "panel_version": version, "protocol": protocol,
                       "premiere_version": "26.0", "cep_version": "12.0", "project_path": project})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("server")
        self.panel = self.root / "panel"
        self.panel.mkdir()
        self.decisions = []
        self.server = DeckServer("1.0.0", ports=free_ports(3), discovery_dir=lambda: self.panel,
                                 host_exe="C:\\Apps\\PremiereDeck.exe", update_for=self._update_for)
        self.agent_messages = []
        self.presence = []
        self.display_messages = []
        self.display_counts = []
        self.server.agent.set_handler(self.agent_messages.append)
        self.server.agent.set_presence_handler(lambda present, h: self.presence.append((present, h)))
        self.server.displays.set_handler(self.display_messages.append)
        self.server.displays.set_presence_handler(self.display_counts.append)
        self.port = self.server.start()
        self.stack = contextlib.ExitStack()
        self.clients = []

    def tearDown(self):
        self.stack.close()
        self.server.stop()
        remove_tree(self.root)

    def _update_for(self, h):
        self.decisions.append(h.panel_version)
        return {"action": "reload"} if h.panel_version == "0.9.0" else None

    def ws_connect(self, url):
        return self.stack.enter_context(connect(url, open_timeout=5))

    def url(self, role="agent", token=None):
        return f"ws://127.0.0.1:{self.port}/ws?role={role}&token={token or self.server.token}"

    def open(self, role="agent", token=None, version="1.0.0", project=None):
        ws = self.ws_connect(self.url(role, token))
        ws.send(hello(role, version, project))
        return ws

    def recv(self, ws, timeout=5):
        return json.loads(ws.recv(timeout=timeout))

    def test_helpers(self):
        self.assertTrue(is_loopback(("127.0.0.1", 5)))
        self.assertTrue(is_loopback(("::1", 5, 0, 0)))
        self.assertFalse(is_loopback(("192.168.1.2", 5)))
        self.assertEqual(parse_query("/ws?role=agent&token=abc"), {"role": "agent", "token": "abc"})

    def test_discovery_file(self):
        data = json.loads((self.panel / "deck.json").read_text("utf-8"))
        self.assertEqual(data["port"], self.port)
        self.assertEqual(data["token"], self.server.token)
        self.assertEqual(data["protocol"], PROTOCOL_VERSION)
        self.assertEqual(data["host_version"], "1.0.0")
        self.assertTrue(data["autostart_host"])
        self.assertEqual(len(data["token"]), 32)

    def test_handshake_welcome_and_agent_routing(self):
        ws = self.open("agent", project="D:\\Edits\\A.prproj")
        welcome = self.recv(ws)
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(welcome["session_id"], self.server.session_id)
        self.assertIsNone(welcome["update"])
        self.assertTrue(wait_for(lambda: self.presence and self.presence[-1][0]))
        self.assertEqual(self.presence[-1][1].project_path, "D:\\Edits\\A.prproj")
        self.assertTrue(self.server.agent.connected)
        self.server.agent.send(P.script("s1", "version", "return app.version;"))
        script = self.recv(ws)
        self.assertEqual((script["type"], script["id"]), ("script", "s1"))
        ws.send(json.dumps({"type": "script.result", "id": "s1", "ok": True, "result": "26.0", "ms": 3}))
        ws.send(json.dumps({"type": "event", "name": "sequence.activated", "data": {"sequence_id": "q"}, "at": 1}))
        ws.send(json.dumps({"type": "heartbeat", "project_path": None, "active_sequence_id": None, "at": 2}))
        ws.send(json.dumps({"type": "log", "level": "info", "message": "agent log line"}))
        ws.send(json.dumps({"type": "mystery"}))
        self.assertTrue(wait_for(lambda: len(self.agent_messages) >= 3))
        self.assertEqual([m["type"] for m in self.agent_messages], ["script.result", "event", "heartbeat"])
        ws.close()
        self.assertTrue(wait_for(lambda: self.presence[-1][0] is False))
        self.assertFalse(self.server.agent.connected)
        with self.assertRaises(ConnectionError):
            self.server.agent.send(P.reload())

    def test_bad_token_and_unknown_role_are_closed_4001(self):
        for role, token in (("agent", "0" * 32), ("viewer", None)):
            ws = self.ws_connect(self.url(role, token))
            with self.assertRaises(ConnectionClosed) as caught:
                ws.recv(timeout=5)
            self.assertEqual(caught.exception.rcvd.code, P.CLOSE_UNAUTHORIZED)
        self.assertEqual(self.presence, [])

    def test_protocol_mismatch_gets_bye_4002(self):
        ws = self.ws_connect(self.url("agent"))
        ws.send(hello("agent", protocol=PROTOCOL_VERSION + 1))
        self.assertEqual(self.recv(ws), {"type": "bye", "reason": "protocol"})
        with self.assertRaises(ConnectionClosed) as caught:
            ws.recv(timeout=5)
        self.assertEqual(caught.exception.rcvd.code, P.CLOSE_PROTOCOL)

    def test_second_agent_replaces_the_first(self):
        first = self.open("agent")
        self.recv(first)
        second = self.open("agent", version="0.9.0")
        welcome = self.recv(second)
        self.assertEqual(welcome["update"], {"action": "reload"})
        with self.assertRaises(ConnectionClosed):
            first.recv(timeout=5)
        self.assertTrue(wait_for(lambda: len(self.presence) == 2))
        self.assertTrue(all(present for present, _ in self.presence), "closing the old agent is not a disconnect")
        self.assertEqual(self.server.agent.hello.panel_version, "0.9.0")
        self.server.agent.send(P.reload())
        self.assertEqual(self.recv(second)["type"], "reload")

    def test_paged_script_result_is_joined(self):
        ws = self.open("agent")
        self.recv(ws)
        text = json.dumps({"items": list(range(50)), "name": "big"})
        chunks = [text[i:i + 40] for i in range(0, len(text), 40)]
        for index, chunk in reversed(list(enumerate(chunks, start=1))):
            ws.send(json.dumps({"type": "script.result", "id": "p1", "ok": True, "page": index,
                                "pages": len(chunks), "chunk": chunk}))
        self.assertTrue(wait_for(lambda: self.agent_messages))
        result = self.agent_messages[0]
        self.assertEqual(result["id"], "p1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {"items": list(range(50)), "name": "big"})
        self.assertNotIn("page", result)

    def test_displays_get_broadcasts_and_send_actions(self):
        tree = P.ui(1, "Premiere Deck", None, [P.section("files", "Files", [P.node_button("files.collect", "Collect")])])
        self.server.displays.send_ui(tree)  # cached for displays that connect later
        one = self.open("display")
        self.assertEqual(self.recv(one)["type"], "welcome")
        self.assertEqual(self.recv(one)["revision"], 1)
        two = self.open("display")
        self.recv(two)
        self.recv(two)
        self.assertTrue(wait_for(lambda: self.display_counts[-1:] == [2]))
        tree2 = dict(tree, revision=2)
        self.server.displays.send_ui(tree2)
        self.server.displays.toast("ok", "Filed clip.mp4")
        for ws in (one, two):
            self.assertEqual(self.recv(ws)["revision"], 2)
            toast = self.recv(ws)
            self.assertEqual((toast["type"], toast["tone"], toast["text"]), ("toast", "ok", "Filed clip.mp4"))
        one.send(json.dumps({"type": "action", "id": "files.collect", "revision": 2}))
        two.send(json.dumps({"type": "display.ready", "size": {"w": 360, "h": 640}}))
        self.assertTrue(wait_for(lambda: len(self.display_messages) == 2))
        self.assertEqual({m["type"] for m in self.display_messages}, {"action", "display.ready"})
        self.assertFalse(self.server.agent.connected, "displays are not agents")
        one.close()
        self.assertTrue(wait_for(lambda: self.display_counts[-1:] == [1]))

    def test_role_mismatch_and_garbage_hello(self):
        ws = self.ws_connect(self.url("display"))
        ws.send(hello("agent"))
        with self.assertRaises(ConnectionClosed) as caught:
            ws.recv(timeout=5)
        self.assertEqual(caught.exception.rcvd.code, P.CLOSE_UNAUTHORIZED)
        ws2 = self.ws_connect(self.url("agent"))
        ws2.send("not json")
        with self.assertRaises(ConnectionClosed):
            ws2.recv(timeout=5)

    def test_stop_removes_discovery_file(self):
        self.server.stop()
        self.assertFalse((self.panel / "deck.json").exists())


class PortRangeTests(unittest.TestCase):
    def test_second_server_takes_the_next_port(self):
        ports = free_ports(2)
        first = DeckServer("1.0.0", ports=ports)
        second = DeckServer("1.0.0", ports=ports)
        try:
            self.assertEqual(first.start(), ports[0])
            self.assertEqual(second.start(), ports[1])
            third = DeckServer("1.0.0", ports=ports)
            with self.assertRaises(RuntimeError):
                third.start()
        finally:
            first.stop()
            second.stop()


if __name__ == "__main__":
    unittest.main()
