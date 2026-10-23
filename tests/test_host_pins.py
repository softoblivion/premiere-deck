"""The pinned row end to end (docs/PROTOCOL.md 6.1 `pins`, docs/BRIDGE_API.md 3.3): a display sends `panel.pin` /
`panel.unpin`, the hub stores the list in the host settings and sends the tree again."""

import json

from premiere_deck.core.settings import Settings
from premiere_deck.host import protocol as P
from premiere_deck.host.hub import Hub
from premiere_deck.modules.base import Module
from tests.test_host_helpers import free_ports, wait_for
from tests.test_host_hub import Echo, HubTestBase


class Board(Module):
    """Seven tiles, a switch tile, an icon button and a button outside any tile group; `hidden` tiles are left out."""

    id = "board"
    title = "Board"
    order = 5

    def __init__(self, ctx):
        super().__init__(ctx)
        self.hidden = set()

    def on_action(self, action_id, value=None, row=None):
        return None

    def render(self):
        tiles = [self.button(f"t{i}", f"Tile {i}", short=f"T{i}") for i in range(1, 8)
                 if f"t{i}" not in self.hidden]
        tiles.append(self.switch("solo", "Solo", False, short="SOLO"))
        return [self.group("Tiles", tiles, id="tiles", tiles=True, short="TILE"),
                self.row([self.button("undo", "Undo", icon="undo", variant="icon"),
                          self.button("plain", "Plain")])]


class PinsTests(HubTestBase):
    modules = [Board, Echo]

    def setUp(self):
        super().setUp()
        self.board = self.hub.registry.entry("board").instance

    def make_hub(self, **kw):
        return Hub(self.paths, fake=True, modules=[Board, Echo], ports=free_ports(3), installer=self.installer,
                   watch=False, fake_display=self.fake_display, **kw)

    def ws(self):
        ws = self.display()
        tree = self.next_of(ws, "ui")
        self.assertIn("pins", tree)
        return ws, tree

    def send(self, ws, action, value):
        ws.send(json.dumps({"type": "action", "id": action, "value": value, "revision": 1}))

    def pins_tree(self, ws, items):
        return self.next_of(ws, "ui", lambda m: m["pins"]["items"] == items)

    def test_every_tree_carries_pins(self):
        ws, tree = self.ws()
        self.assertEqual(tree["pins"], {"items": [], "max": 5})
        self.hub.registry.invalidate()
        self.assertEqual(self.next_of(ws, "ui")["pins"], {"items": [], "max": 5})
        self.assertEqual(self.hub.registry.render_now()["pins"], {"items": [], "max": 5})

    def test_pin_unpin_dedupe_and_persistence(self):
        ws, _ = self.ws()
        self.send(ws, P.PANEL_PIN, "board.t3")
        self.pins_tree(ws, ["board.t3"])
        self.send(ws, P.PANEL_PIN, "board.solo")  # a switch tile is pinnable
        self.pins_tree(ws, ["board.t3", "board.solo"])
        self.assertEqual(Settings(self.paths.settings_file).pins(), ["board.t3", "board.solo"])
        self.send(ws, P.PANEL_PIN, "board.t3")  # pinned already: nothing changes, nothing answers
        self.send(ws, P.PANEL_UNPIN, "board.t9")  # not pinned: the same
        self.send(ws, P.PANEL_UNPIN, "board.t3")
        seen = []
        while not (seen and seen[-1]["type"] == "ui" and seen[-1]["pins"]["items"] == ["board.solo"]):
            seen.append(json.loads(ws.recv(timeout=5)))
        self.assertEqual([m for m in seen if m["type"] == "action_result"], [])
        self.assertTrue(all(m["pins"]["items"] in (["board.t3", "board.solo"], ["board.solo"])
                            for m in seen if m["type"] == "ui"))
        self.assertEqual(Settings(self.paths.settings_file).pins(), ["board.solo"])
        self.assertFalse(any(name == "result" for name, _ in self.events), "the window hears nothing of pins")
        self.assertNotIn("pins", self.hub.state()["settings"])

    def test_a_sixth_pin_is_refused(self):
        ws, _ = self.ws()
        for i in range(1, 6):
            self.send(ws, P.PANEL_PIN, f"board.t{i}")
        five = [f"board.t{i}" for i in range(1, 6)]
        self.pins_tree(ws, five)
        self.send(ws, P.PANEL_PIN, "board.t6")
        result = self.next_of(ws, "action_result")
        self.assertEqual((result["id"], result["outcome"], result["text"], result["detail"]),
                         ("panel.pins", "warn", "Five pins at most", "Unpin a tile to make room."))
        self.assertEqual(self.hub.settings.pins(), five)

    def test_only_tiles_of_the_current_tree_can_be_pinned(self):
        ws, _ = self.ws()
        for bad in ("board.undo", "board.plain", "board.t99", "echo.go", "host.open", "", None, 5):
            self.send(ws, P.PANEL_PIN, bad)
            result = self.next_of(ws, "action_result")
            self.assertEqual((result["id"], result["outcome"], result["text"]),
                             ("panel.pins", "warn", "This control cannot be pinned"), bad)
        self.assertEqual(self.hub.settings.pins(), [])
        self.send(ws, P.PANEL_UNPIN, None)  # a bad unpin is ignored
        self.send(ws, P.PANEL_PIN, "board.t1")
        self.pins_tree(ws, ["board.t1"])

    def test_a_pin_whose_tile_is_not_rendered_stays_stored(self):
        ws, _ = self.ws()
        for i in range(1, 6):
            self.send(ws, P.PANEL_PIN, f"board.t{i}")
        self.pins_tree(ws, [f"board.t{i}" for i in range(1, 6)])
        self.hub.registry.thread.call(lambda: self.board.hidden.update({"t1", "t2"}))
        self.hub.registry.invalidate()
        self.pins_tree(ws, ["board.t3", "board.t4", "board.t5"])
        self.assertEqual(len(self.hub.settings.pins()), 5, "hidden pins are kept")
        self.send(ws, P.PANEL_PIN, "board.t6")  # room is made from the oldest pin that is not shown
        self.pins_tree(ws, ["board.t3", "board.t4", "board.t5", "board.t6"])
        self.assertEqual(self.hub.settings.pins(), ["board.t2", "board.t3", "board.t4", "board.t5", "board.t6"])
        self.hub.registry.thread.call(lambda: self.board.hidden.clear())
        self.hub.registry.invalidate()
        self.pins_tree(ws, ["board.t2", "board.t3", "board.t4", "board.t5", "board.t6"])

    def test_pins_of_unknown_modules_are_dropped_at_start(self):
        self.hub.settings.set_pins(["gone.tile", "board.t1", "echo.go"])
        self.hub.stop()
        self.hub = self.make_hub()
        self.hub.start()
        self.assertTrue(wait_for(lambda: self.hub.fake_agent.connected.is_set(), 10))
        self.assertEqual(self.hub.settings.pins(), ["board.t1", "echo.go"])
        self.assertEqual(Settings(self.paths.settings_file).pins(), ["board.t1", "echo.go"])
        tree = self.hub.registry.render_now()
        self.assertEqual(tree["pins"]["items"], ["board.t1"], "echo.go is no tile, so it is not sent")

