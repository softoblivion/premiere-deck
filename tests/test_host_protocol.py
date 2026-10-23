import unittest

from premiere_deck.host import protocol as P


class TreeValidation(unittest.TestCase):
    def tree(self, items):
        return {"type": P.T_UI, "revision": 1, "title": "Deck", "status": P.status("ok", "ok"),
                "sections": [P.section("files", "Files", items)], "footer": None}

    def test_valid_tree_passes(self):
        items = [
            P.node_button("files.collect", "Collect project", hint="Copy external media", confirm="Copy 3 files?"),
            P.node_toggle("files.watch", "Watch", True),
            P.node_select("files.layout", "Layout", "default", [{"value": "default", "label": "Default"}]),
            P.node_input("files.name", "Name", "", placeholder="Nest 1", submit_label="Nest"),
            P.node_text("Watching", tone="ok", small=True),
            P.node_progress(0.4, "Copying"),
            P.node_list("files.recent", [P.list_row("r1", "clip.mp4", subtitle="Downloads -> Footage",
                                                      actions=[P.node_button("files.undo", "Undo")])], empty="Nothing yet"),
            P.node_row([P.node_divider()]),
        ]
        P.validate_tree(self.tree(items))

    def test_duplicate_ids_fail(self):
        items = [P.node_button("a.x", "A"), P.node_toggle("a.x", "B", False)]
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree(items))

    def test_unknown_kind_and_tone_fail(self):
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "image", "id": "a.i"}]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "text", "text": "x", "tone": "loud"}]))

    def test_group_node(self):
        group = P.node_group("Timeline", [P.node_button("a.one", "One"), P.node_button("a.two", "Two"),
                                          P.node_row([P.node_text("x")])], columns=2)
        P.validate_tree(self.tree([group, P.node_group(None, [])]))
        with self.assertRaises(P.ProtocolError):  # ids inside a group count
            P.validate_tree(self.tree([P.node_group("A", [P.node_button("a.one", "One")]), P.node_button("a.one", "Two")]))
        with self.assertRaises(P.ProtocolError):  # no nesting
            P.validate_tree(self.tree([P.node_group("A", [P.node_group("B", [])])]))
        with self.assertRaises(P.ProtocolError):  # columns 1 to 3
            P.validate_tree(self.tree([{"kind": "group", "title": "A", "items": [], "columns": 4}]))
        P.validate_tree(self.tree([{"kind": "group", "title": "A", "items": [], "columns": 3}]))
        with self.assertRaises(P.ProtocolError):  # title is a string or null
            P.validate_tree(self.tree([{"kind": "group", "title": 5, "items": []}]))

    def test_select_needs_options_and_rows_need_ids(self):
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "select", "id": "a.s", "label": "S", "value": "", "options": []}]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([P.node_list("a.l", [{"title": "no id"}])]))

    def test_progress_is_clamped(self):
        self.assertEqual(P.node_progress(1.7)["value"], 1.0)
        self.assertIsNone(P.node_progress(None)["value"])


    def test_list_row_bar(self):
        self.assertIsNone(P.list_row("r", "T")["bar"])
        self.assertEqual(P.list_row("r", "T", bar=0.4)["bar"], 0.4)
        self.assertEqual(P.list_row("r", "T", bar=1.7)["bar"], 1.0)
        self.assertEqual(P.list_row("r", "T", bar=-1)["bar"], 0.0)
        P.validate_tree(self.tree([P.node_list("a.l", [P.list_row("r1", "T", bar=0.5), P.list_row("r2", "T")])]))
        for bad in (1.5, -0.1, "high", True):
            row = dict(P.list_row("r1", "T"), bar=bad)
            with self.assertRaises(P.ProtocolError, msg=repr(bad)):
                P.validate_tree(self.tree([P.node_list("a.l", [row])]))


class Fields20(unittest.TestCase):
    """The 2.0 node fields (docs/PROTOCOL.md 6.1)."""

    def tree(self, items, **section):
        return {"type": P.T_UI, "revision": 1, "title": "Deck", "status": None,
                "sections": [P.section("actions", "Actions", items, **section)], "footer": None}

    def test_button_fields(self):
        menu = P.menu("a.social.format", "9x16", [{"value": "9x16", "label": "9:16"}, {"value": "1x1", "label": "1:1"}],
                      "9:16")
        node = P.node_button("a.social", "Social copy", hint="Copies", icon="social-copy", short="Social copy",
                             meta="Checks offline first", menu=menu, variant="icon")
        self.assertEqual((node["icon"], node["short"], node["meta"], node["variant"], node["menu"]),
                         ("social-copy", "Social copy", "Checks offline first", "icon", menu))
        P.validate_tree(self.tree([node]))
        plain = P.node_button("a.plain", "Plain")
        self.assertFalse({"icon", "short", "meta", "menu", "variant"} & set(plain), "absent fields are not sent")

    def test_short_label_is_at_most_16_characters(self):
        P.node_button("a.x", "X", short="1234567890123456")
        with self.assertRaises(P.ProtocolError):
            P.node_button("a.x", "X", short="12345678901234567")
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "button", "id": "a.x", "label": "X", "short": "x" * 17}]))
        with self.assertRaises(P.ProtocolError):
            P.node_button("a.x", "X", variant="round")

    def test_button_fields_are_validated(self):
        bad_nodes = [
            {"kind": "button", "id": "a.x", "label": "X", "icon": 3},
            {"kind": "button", "id": "a.x", "label": "X", "meta": 3},
            {"kind": "button", "id": "a.x", "label": "X", "variant": "wide"},
            {"kind": "button", "id": "a.x", "label": "X", "menu": {"id": "a.m", "options": [], "badge": "x"}},
            {"kind": "button", "id": "a.x", "label": "X", "menu": {"options": [{"value": "a", "label": "A"}]}},
            {"kind": "button", "id": "a.x", "label": "X", "menu": {"id": "a.m", "badge": "x",
                                                                   "options": [{"value": "a"}]}},
        ]
        for node in bad_nodes:
            with self.subTest(node=node), self.assertRaises(P.ProtocolError):
                P.validate_tree(self.tree([node]))
        with self.assertRaises(P.ProtocolError):  # a menu id shares the id space of the tree
            P.validate_tree(self.tree([P.node_button("a.x", "X"), P.node_button(
                "a.y", "Y", menu=P.menu("a.x", "a", [{"value": "a", "label": "A"}], "A"))]))

    def test_group_fields(self):
        tiles = P.node_group("Timeline", [P.node_button("a.one", "One")], id="a.timeline", collapsible=True, tiles=True)
        self.assertEqual((tiles["id"], tiles["collapsible"], tiles["tiles"], tiles["columns"]),
                         ("a.timeline", True, True, 3), "tiles default to three columns")
        plain = P.node_group("T", [])
        self.assertEqual((plain["columns"], "id" in plain, "tiles" in plain, "collapsible" in plain), (1, False, False, False))
        self.assertEqual(P.node_group("T", [], tiles=True, columns=2)["columns"], 2)
        P.validate_tree(self.tree([tiles]))
        with self.assertRaises(P.ProtocolError):  # group ids are unique, also against node ids
            P.validate_tree(self.tree([tiles, P.node_group("B", [], id="a.timeline")]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([tiles, P.node_button("a.timeline", "Same")]))
        with self.assertRaises(P.ProtocolError):  # a collapsible group needs an id
            P.validate_tree(self.tree([P.node_group("A", [], collapsible=True)]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "group", "title": "A", "items": [], "id": "", "tiles": True}]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "group", "title": "A", "items": [], "tiles": "yes"}]))

    def test_progress_chip_and_section_icon(self):
        progress = P.node_progress(0.4, "Sorting project 38 / 142", detail="C0042.MP4 > Footage", cancel="files.stop")
        self.assertEqual((progress["detail"], progress["cancel"]), ("C0042.MP4 > Footage", "files.stop"))
        self.assertFalse({"detail", "cancel"} & set(P.node_progress(0.4, "x")))
        chip = P.node_text("142 in the root", tone="ok", small=True, chip=True)
        self.assertTrue(chip["chip"])
        self.assertNotIn("chip", P.node_text("x"))
        P.validate_tree(self.tree([progress, chip], icon="actions"))
        self.assertEqual(P.section("a", "A", [], icon="folder")["icon"], "folder")
        self.assertNotIn("icon", P.section("a", "A", []))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "progress", "value": 0.1, "label": "x", "cancel": 5}]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([{"kind": "text", "text": "x", "chip": "yes"}]))
        with self.assertRaises(P.ProtocolError):
            P.validate_tree({"type": P.T_UI, "revision": 1, "title": "", "status": None, "footer": None,
                             "sections": [dict(P.section("a", "A", []), icon=7)]})

    def test_list_row_buttons_may_carry_2_0_fields(self):
        row = P.list_row("r1", "T", actions=[P.node_button("a.go", "Go", icon="open", variant="icon")])
        P.validate_tree(self.tree([P.node_list("a.l", [row])]))
        bad = P.list_row("r1", "T", actions=[{"kind": "button", "id": "a.go", "label": "Go", "short": "x" * 20}])
        with self.assertRaises(P.ProtocolError):
            P.validate_tree(self.tree([P.node_list("a.l", [bad])]))


class Switch21(unittest.TestCase):
    """The 2.1 `switch` node (docs/PROTOCOL.md 6.1)."""

    def tree(self, items):
        return {"type": P.T_UI, "revision": 1, "title": "Deck", "status": None,
                "sections": [P.section("actions", "Actions", items)], "footer": None}

    def test_switch_fields(self):
        node = P.node_switch("a.solo", "Solo track", True, short="SOLO", icon="solo", hint="Mutes the others",
                             busy=True)
        self.assertEqual(node, {"kind": "switch", "id": "a.solo", "label": "Solo track", "value": True,
                                "short": "SOLO", "icon": "solo", "hint": "Mutes the others", "disabled": False,
                                "busy": True})
        plain = P.node_switch("a.lock", "Lock others", 0)
        self.assertIs(plain["value"], False)
        self.assertFalse({"short", "icon", "hint"} & set(plain), "absent fields are not sent")
        P.validate_tree(self.tree([P.node_group("Tracks", [node, plain, P.node_button("a.add", "Add")],
                                                tiles=True)]))

    def test_switch_is_validated(self):
        with self.assertRaises(P.ProtocolError):
            P.node_switch("a.x", "X", True, short="x" * 17)
        bad_nodes = [
            {"kind": "switch", "label": "X", "value": True},
            {"kind": "switch", "id": "a.x", "label": "X"},
            {"kind": "switch", "id": "a.x", "label": "X", "value": "on"},
            {"kind": "switch", "id": "a.x", "label": "X", "value": True, "short": "x" * 17},
            {"kind": "switch", "id": "a.x", "label": "X", "value": True, "icon": 3},
            {"kind": "switch", "id": "a.x", "label": "X", "value": True, "busy": "yes"},
        ]
        for node in bad_nodes:
            with self.subTest(node=node), self.assertRaises(P.ProtocolError):
                P.validate_tree(self.tree([node]))
        with self.assertRaises(P.ProtocolError):  # switch ids share the id space
            P.validate_tree(self.tree([P.node_button("a.x", "X"), P.node_switch("a.x", "Y", False)]))


class MenuSwatch21(unittest.TestCase):
    """2.1: a menu option may carry a `swatch` colour (docs/PROTOCOL.md 6.1)."""

    def tree(self, menu):
        return {"type": P.T_UI, "revision": 1, "title": "Deck", "status": None, "footer": None,
                "sections": [P.section("actions", "Actions", [P.node_button("a.colour", "Colour", menu=menu)])]}

    def test_swatch_is_kept_and_validated(self):
        menu = P.menu("a.colour.label", "iris", [{"value": "iris", "label": "Iris", "swatch": "#1aa3e0"},
                                                 {"value": "none", "label": "None"}, {"value": "x", "label": "X", "swatch": "#abc"}],
                      "Iris")
        self.assertEqual(menu["options"][0], {"value": "iris", "label": "Iris", "swatch": "#1aa3e0"})
        self.assertNotIn("swatch", menu["options"][1])
        P.validate_tree(self.tree(menu))
        for bad in ("blue", "#12345", "1aa3e0", 7, "url(x)"):
            with self.subTest(bad=bad), self.assertRaises(P.ProtocolError):
                P.menu("a.m", "a", [{"value": "a", "label": "A", "swatch": bad}], "A")
            with self.subTest(tree=bad), self.assertRaises(P.ProtocolError):
                P.validate_tree(self.tree({"id": "a.m", "value": "a", "badge": "A",
                                           "options": [{"value": "a", "label": "A", "swatch": bad}]}))


class Messages(unittest.TestCase):
    def test_theme_message(self):
        self.assertEqual(P.theme("sky"), {"type": "theme", "name": "sky"})
        self.assertEqual(P.theme("black")["name"], "black")
        for unknown in ("dusk", "", None, 3):
            self.assertEqual(P.theme(unknown), {"type": "theme", "name": "black"}, repr(unknown))

    def test_action_result_message(self):
        message = P.action_result("actions.adjustment_layer", "ok", "Adjustment layer added · V3", None, at=1761000000000)
        self.assertEqual(message, {"type": "action_result", "id": "actions.adjustment_layer", "outcome": "ok",
                                   "text": "Adjustment layer added · V3", "detail": None, "at": 1761000000000})
        for outcome in ("ok", "warn", "error", "neutral"):
            self.assertEqual(P.action_result("a.b", outcome, "t", "why")["detail"], "why")
        self.assertGreater(P.action_result("a.b", "ok", "t")["at"], 1_700_000_000_000, "epoch milliseconds")
        with self.assertRaises(P.ProtocolError):
            P.action_result("a.b", "accent", "t")
        with self.assertRaises(P.ProtocolError):
            P.action_result("", "ok", "t")

    def test_hello_parses_and_rejects(self):
        hello = P.Hello.parse({"type": "hello", "role": "agent", "panel_version": "1.0.0", "protocol": 1,
                               "project_path": None})
        self.assertEqual(hello.role, "agent")
        self.assertIsNone(hello.project_path)
        with self.assertRaises(P.ProtocolError):
            P.Hello.parse({"type": "hello", "role": "viewer", "protocol": 1})
        with self.assertRaises(P.ProtocolError):
            P.Hello.parse({"type": "welcome"})

    def test_subscribe_rejects_unknown_events(self):
        self.assertEqual(P.subscribe(["project.changed", "project.opened"])["events"], ["project.changed", "project.opened"])
        with self.assertRaises(P.ProtocolError):
            P.subscribe(["timeline.exploded"])

    def test_dumps_escapes_line_separators_and_loads_checks_shape(self):
        text = P.dumps({"type": "toast", "text": "a b"})
        self.assertNotIn(" ", text)
        self.assertEqual(P.loads(text)["text"], "a b")
        with self.assertRaises(P.ProtocolError):
            P.loads("[1, 2]")
        with self.assertRaises(P.ProtocolError):
            P.loads("not json")

    def test_paged_results_are_joined_in_order(self):
        pages = P.ScriptResultPages(pages=2)
        self.assertTrue(P.is_paged({"type": "script.result", "id": "s1", "page": 2, "pages": 2, "chunk": "2]}"}))
        self.assertIsNone(pages.add({"id": "s1", "page": 2, "pages": 2, "chunk": " 2]}"}))
        done = pages.add({"id": "s1", "page": 1, "pages": 2, "chunk": '{"items": [1,'})
        self.assertEqual(done["result"], {"items": [1, 2]})
        self.assertTrue(done["ok"])

    def test_script_and_toast_shapes(self):
        msg = P.script("s1", "snapshot", "return 1;", {"a": 1}, timeout_ms=5000)
        self.assertEqual((msg["type"], msg["id"], msg["timeout_ms"]), ("script", "s1", 5000))
        self.assertEqual(P.toast("ok", "Filed", 1000)["ms"], 1000)
        with self.assertRaises(P.ProtocolError):
            P.toast("shout", "x")


if __name__ == "__main__":
    unittest.main()
