"""Auto copy and Auto sort (docs/FILES.md 3.1, 5.1, 6): the load-time mapping of the 2.0 setting, the two switches in the
panel tree and on the host page, and the import intercept with each combination of the switches, on media generated
by ffmpeg and probed with the real ffprobe."""

import os
import shutil
import unittest

from premiere_deck.core.probe import probe
from premiere_deck.host import protocol as P
from tests.test_modules_files import find, node_ids, walk
from tests.test_modules_files_support import FilesCase, ffmpeg_available, make_media, remove_tree, scratch, write_file


class Switches(FilesCase):
    """Settings, mapping, tree and host page: no media needed."""

    def store(self):
        return self.host.settings.project.values

    def test_a_set_up_2_0_project_maps_onto_both_switches_once(self):
        self.store().update(enabled=True)  # 2.0 stored `enabled` (the project was set up and the intercept ran)
        self.make_module()
        self.assertEqual((self.store().get("auto_copy"), self.store().get("auto_sort")), (True, True))
        self.module.on_action("files.auto_sort", False)  # the user switches one off; a reload keeps that
        self.module.project_opened(self.project)
        self.assertEqual((self.store()["auto_copy"], self.store()["auto_sort"]), (True, False))

    def test_the_mapping_leaves_other_projects_alone(self):
        self.make_module()  # not set up: nothing to map
        self.assertNotIn("auto_copy", self.store())
        self.assertNotIn("auto_sort", self.store())
        self.module.stop()
        self.store().update(enabled=True, auto_copy=False)  # a key the project already stores is never overwritten
        self.make_module()
        self.assertEqual(self.store()["auto_copy"], False)
        self.assertNotIn("auto_sort", self.store())

    def test_setup_stores_both_switches_off_and_does_not_touch_them(self):
        self.make_module(enabled=True, auto=False)
        self.assertEqual((self.store()["auto_copy"], self.store()["auto_sort"]), (False, False))
        self.module.project_opened(self.project)  # a reload of a project set up in 2.1 is not mapped
        self.assertEqual((self.store()["auto_copy"], self.store()["auto_sort"]), (False, False))
        self.module.stop()
        self.store().update(auto_copy=True, enabled=False)  # a value set before Setup survives it
        self.make_module()
        self.module.on_action("files.setup")
        self.assertEqual((self.store()["auto_copy"], self.store()["auto_sort"]), (True, False))

    def test_stored_values_are_validated(self):
        self.store().update(enabled=True, auto_copy="yes", auto_sort=1)
        self.make_module()
        self.assertEqual((self.module._proj("auto_copy"), self.module._proj("auto_sort")), (False, False))

    def switch_nodes(self):
        sec = self.module.section()  # validates the tree against the protocol
        P.validate_tree({"type": "ui", "revision": 1, "title": "", "status": None, "sections": [sec], "footer": None})
        return {n["id"]: n for n in walk(sec["items"]) if n.get("kind") == "switch"}

    def test_switch_tiles_beside_setup_before_the_project_is_set_up(self):
        self.make_module()
        group = find(self.module.render(), "files.actions")
        self.assertEqual((group["tiles"], [n["id"] for n in group["items"]]),
                         (True, ["files.setup", "files.auto_copy", "files.auto_sort"]))
        setup, copy, sort = group["items"]
        self.assertEqual((setup["kind"], setup["short"]), ("button", "SETUP"))
        self.assertEqual([(n["kind"], n["label"], n["short"], n["value"]) for n in (copy, sort)],
                         [("switch", "Auto copy", "AUTO COPY", False), ("switch", "Auto sort", "AUTO SORT", False)])
        self.assertTrue(all(n["hint"] for n in (copy, sort)))
        # a flip before Setup is refused with a result, and the tile keeps the old value
        before = len(self.host.ui.results)
        self.module.on_action("files.auto_copy", True)
        self.assertEqual(self.host.ui.results[before:], [("auto_copy", "warn", "Set up this project first", None)])
        self.assertNotIn("auto_copy", self.store())
        self.assertFalse(self.switch_nodes()["files.auto_copy"]["value"])

    def test_switch_tiles_after_setup_and_the_flip(self):
        self.make_module(enabled=True, auto=False, watch=False)
        nodes = self.switch_nodes()
        self.assertEqual(list(nodes), ["files.auto_copy", "files.auto_sort"])
        group = find(self.module.render(), "files.actions")
        self.assertEqual([n["id"] for n in group["items"]][-2:], ["files.auto_copy", "files.auto_sort"])
        self.assertNotIn("files.journal", node_ids(self.module.render()))
        before = len(self.host.ui.results)
        result = self.module.on_action("files.auto_copy", True)
        self.assertTrue(result is None or result.rerender)
        self.assertEqual(len(self.host.ui.results), before)  # a successful flip shows no result line
        self.assertEqual(self.store()["auto_copy"], True)
        nodes = self.switch_nodes()
        self.assertEqual((nodes["files.auto_copy"]["value"], nodes["files.auto_sort"]["value"]), (True, False))
        self.module.on_action("files.auto_copy", False)
        self.assertFalse(self.switch_nodes()["files.auto_copy"]["value"])
        self.module.on_action("files.auto_sort", "not a bool")  # anything but a bool flips the stored value
        self.assertTrue(self.store()["auto_sort"])

    def test_host_page_has_the_switch_rows_next_to_setup(self):
        self.make_module()
        blocks = {b["id"]: b for b in self.module.render_host()["blocks"]}
        self.assertEqual(list(blocks)[:2], ["setup_actions", "auto"])
        rows = blocks["auto"]["rows"]
        self.assertEqual([(r["id"], r["title"], r["toggle"]["id"], r["toggle"]["value"]) for r in rows],
                         [("auto_copy", "Auto copy", "files.auto_copy", False),
                          ("auto_sort", "Auto sort", "files.auto_sort", False)])
        self.module.on_action("files.setup")
        self.module.on_action("files.auto_sort", True, "auto_sort")  # what the row toggle sends
        blocks = {b["id"]: b for b in self.module.render_host()["blocks"]}
        self.assertEqual(list(blocks)[:3], ["status_chips", "main", "auto"])
        self.assertEqual([r["toggle"]["value"] for r in blocks["auto"]["rows"]], [False, True])
        self.assertEqual(len(blocks["main"]["items"]), 4)  # the host page keeps its four action tiles
        self.assertEqual([b["short"] for b in blocks["main"]["items"]],
                         ["Sort project", "Sync all media", "Sync used media", "Make ZIP"])
        self.assertNotIn("files.journal", [i["id"] for i in blocks["main"]["items"]])

    def test_host_project_setting_accepts_the_switches(self):
        self.make_module(enabled=True, auto=False, watch=False)
        self.module.on_action("files.host:project_setting", {"key": "auto_copy", "value": True})
        self.assertTrue(self.store()["auto_copy"])


@unittest.skipUnless(ffmpeg_available(), "ffmpeg and ffprobe are needed on PATH")
class InterceptSwitches(FilesCase):
    @classmethod
    def setUpClass(cls):
        cls.src_dir = scratch("files-auto-src-")
        cls.media = make_media(cls.src_dir)

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.src_dir)

    def setUp(self):
        super().setUp()

    def start(self, **switches):
        self.make_module(enabled=True, auto=False, watch=False, **switches)
        for key, value in switches.items():
            self.assertEqual(self.host.settings.project.values[key], value)
        self.host.ui.toasts.clear()

    def generated(self, key: str, folder: str, name: str = "") -> str:
        """A copy of a generated file at `folder`, probed with the real ffprobe for the module's fake tools."""
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name or os.path.basename(self.media[key]))
        shutil.copyfile(self.media[key], path)
        info = probe(path)
        self.assertIsNone(info.probe_error, info.probe_error)
        self.host.tools.infos[path] = info
        return path

    def flip(self, key: str, value: bool) -> None:
        self.module.on_action(f"files.{key}", value)
        self.assertEqual(self.host.settings.project.values[key], value)

    # -- both off ---------------------------------------------------------------------------------------------

    def test_both_off_the_module_only_observes(self):
        self.start(auto_copy=False, auto_sort=False)
        outside = self.generated("plain", self.downloads)
        loose = self.generated("alpha", self.root)
        misplaced = self.generated("png", self.in_root("Footage"))
        a = self.user_import(outside)
        b = self.user_import(loose)
        c = self.user_import(misplaced, "Elsewhere")
        self.assertEqual(self.pr.calls_named("relink"), [])
        self.assertEqual(self.pr.calls_named("move_to_bin"), [])
        self.assertEqual((self.item(a.node_id).media_path, self.item(b.node_id).media_path), (outside, loose))
        self.assertEqual(self.item(c.node_id).tree_path, "Elsewhere")
        self.assertEqual(self.toasts(), [])
        self.assertEqual([e["op"] for e in self.journal()], ["setup"])
        self.assertEqual(len(self.module._state.external), 2)  # still counted for the panel

    # -- Auto copy only ---------------------------------------------------------------------------------------

    def test_auto_copy_copies_external_imports_into_media_without_classifying(self):
        self.start(auto_copy=True, auto_sort=False)
        alpha = self.generated("alpha", self.downloads)  # Auto sort would file this under Overlays
        wav = self.generated("short_wav", self.downloads)
        loose = self.generated("plain", self.root)
        a, b, c = self.user_import(alpha), self.user_import(wav), self.user_import(loose)
        for item, source in ((a, alpha), (b, wav)):
            target = self.in_root("Media", os.path.basename(source))
            self.assertTrue(os.path.isfile(target) and os.path.isfile(source))
            self.assertEqual(self.item(item.node_id).media_path, target)
            self.assertEqual(self.item(item.node_id).tree_path, "Media")
        self.assertFalse(os.path.exists(self.in_root("Overlays", os.path.basename(alpha))))
        entries = [e for e in self.journal() if e["op"] != "setup"]
        self.assertEqual([(e["op"], e["status"], e["copied"]) for e in entries], [("synced", "done", True)] * 2)
        self.assertEqual(self.toasts(), ["Copied logo_anim.webm into Media", "Copied 2 files"])
        self.assertEqual(self.item(c.node_id).media_path, loose)  # loose files of the root belong to Auto sort
        self.assertEqual(len(self.pr.calls_named("relink")), 2)
        self.assertTrue(self.press_undo_ok())

    def press_undo_ok(self) -> bool:
        """Auto copy entries are undone like Sync entries: relink to the source, drop the copy."""
        self.module.on_action("files.undo_last")
        return self.host.ui.results[-1][:2] == ("undo_last", "ok")

    def test_auto_copy_does_not_enforce_bins(self):
        self.start(auto_copy=True, auto_sort=False)
        inside = self.generated("png", self.in_root("Graphics"))
        item = self.user_import(inside, "Elsewhere")
        self.assertEqual(self.item(item.node_id).tree_path, "Elsewhere")

    # -- Auto sort only ---------------------------------------------------------------------------------------

    def test_auto_sort_files_the_root_and_leaves_external_imports_alone(self):
        self.start(auto_copy=False, auto_sort=True)
        outside = self.generated("plain", self.downloads)
        loose = self.generated("alpha", self.root)
        wrong_bin = self.generated("png", self.in_root("Graphics"))
        a = self.user_import(outside)
        b = self.user_import(loose)
        c = self.user_import(wrong_bin, "Elsewhere")
        self.assertEqual(self.item(a.node_id).media_path, outside)
        filed = self.in_root("Overlays", os.path.basename(loose))  # a classified copy, the original stays
        self.assertTrue(os.path.isfile(filed) and os.path.isfile(loose))
        self.assertEqual((self.item(b.node_id).media_path, self.item(b.node_id).tree_path), (filed, "Overlays"))
        self.assertEqual(self.item(c.node_id).tree_path, "Graphics")  # enforce_bins: the bin of its folder
        self.assertEqual(self.toasts(), ["Filed logo_anim.webm into Overlays"])
        ops = [(e["op"], e["status"]) for e in self.journal() if e["op"] != "setup"]
        self.assertEqual(ops, [("filed", "done"), ("binned", "done")])

    def test_auto_sort_moves_a_file_dropped_into_media_to_its_folder(self):
        self.start(auto_copy=False, auto_sort=True)
        dropped = self.generated("short_wav", self.in_root("Media"))
        item = self.user_import(dropped, "Media")
        target = self.in_root("Audio", "SFX", "blip.wav")
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(dropped))  # a move inside the root, journaled and undoable
        self.assertEqual((self.item(item.node_id).media_path, self.item(item.node_id).tree_path),
                         (target, "Audio/SFX"))
        entry = self.journal()[-1]
        self.assertEqual((entry["op"], entry["status"], entry["moved"]), ("sorted", "done", True))
        self.assertEqual(self.toasts(), ["Filed blip.wav into Audio/SFX"])
        self.module.on_action("files.undo_last")
        self.assertEqual(self.host.ui.results[-1][:2], ("undo_last", "ok"))
        self.assertTrue(os.path.isfile(dropped))
        self.assertEqual(self.item(item.node_id).media_path, dropped)

    def test_auto_sort_leaves_media_in_media_when_auto_sort_is_off(self):
        self.start(auto_copy=True, auto_sort=False)
        dropped = self.generated("short_wav", self.in_root("Media"))
        item = self.user_import(dropped, "Media")
        self.assertEqual(self.item(item.node_id).media_path, dropped)
        self.assertEqual(self.pr.calls_named("relink"), [])

    # -- both on ----------------------------------------------------------------------------------------------

    def test_both_on_files_an_external_import_straight_into_its_folder(self):
        self.start(auto_copy=True, auto_sort=True)
        alpha = self.generated("alpha", self.downloads)
        long_wav = self.generated("long_wav", self.downloads)
        a, b = self.user_import(alpha), self.user_import(long_wav)
        self.assertEqual(self.item(a.node_id).media_path, self.in_root("Overlays", "logo_anim.webm"))
        self.assertEqual(self.item(b.node_id).tree_path, "Audio/Music")
        self.assertFalse(os.path.exists(self.in_root("Media", "logo_anim.webm")))
        entries = [e for e in self.journal() if e["op"] != "setup"]
        self.assertEqual([(e["op"], e["status"], e["copied"]) for e in entries], [("filed", "done", True)] * 2)
        self.assertTrue(all(os.path.isfile(s) for s in (alpha, long_wav)))  # the sources are never touched
        self.assertEqual(self.toasts(), ["Filed logo_anim.webm into Overlays", "Filed 2 files"])

    # -- switching at run time --------------------------------------------------------------------------------

    def test_a_flip_takes_effect_for_the_next_import_only(self):
        self.start(auto_copy=False, auto_sort=False)
        first = self.generated("plain", self.downloads, "first.mp4")
        a = self.user_import(first)
        self.assertEqual(self.item(a.node_id).media_path, first)
        self.flip("auto_copy", True)
        second = self.generated("plain", self.downloads, "second.mp4")
        b = self.user_import(second)
        self.assertEqual(self.item(b.node_id).media_path, self.in_root("Media", "second.mp4"))
        self.assertEqual(self.item(a.node_id).media_path, first)  # nothing is retro-fitted
        self.flip("auto_copy", False)
        third = self.generated("plain", self.downloads, "third.mp4")
        c = self.user_import(third)
        self.assertEqual(self.item(c.node_id).media_path, third)


if __name__ == "__main__":
    unittest.main()
