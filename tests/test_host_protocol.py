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
        with self.assertRaises(P.ProtocolError):  # columns 1 or 2
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


class Messages(unittest.TestCase):
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
