import json
import os
import unittest
from pathlib import Path
from unittest import mock

from premiere_deck.core import paths as paths_mod
from premiere_deck.core.paths import AppPaths, atomic_write_json, read_json
from premiere_deck.core.settings import (
    DEFAULT_THEME, HostModuleSettings, ProjectModuleSettings, Settings, coerce_setting, normalize_pins,
)
from tests.test_host_helpers import remove_tree, scratch_dir


class PathsTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("paths")

    def tearDown(self):
        remove_tree(self.root)

    def test_home_override_from_environment(self):
        with mock.patch.dict(os.environ, {paths_mod.ENV_HOME: str(self.root / "data")}):
            paths = AppPaths()
        self.assertEqual(paths.home, (self.root / "data").resolve())
        self.assertEqual(paths.root, paths.home)
        paths.ensure()
        for folder in (paths.bin_dir, paths.logs_dir, paths.webview_dir, paths.updates_dir):
            self.assertTrue(folder.is_dir(), folder)
        self.assertEqual(paths.settings_file.name, "settings.json")
        self.assertEqual(paths.host_log, paths.logs_dir / "host.log")

    def test_default_home_is_beside_the_app(self):
        with mock.patch.dict(os.environ, {paths_mod.ENV_HOME: ""}):
            paths = AppPaths()
        self.assertEqual(paths.home.name, "premiere-deck_data")
        self.assertEqual(paths.home.parent, paths_mod.app_dir().resolve())

    def test_cache_dir_per_module(self):
        paths = AppPaths(self.root)
        folder = paths.cache_dir("files")
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder, paths.cache_root / "files")
        for bad in ("", "..", "a/b", "c:d"):
            with self.assertRaises(ValueError):
                paths.cache_dir(bad)

    def test_remove_retired_data(self):
        paths = AppPaths(self.root)
        paths.ensure()
        self.assertEqual(paths.remove_retired_data(), [], "nothing to remove on a clean data folder")
        (paths.home / "models").mkdir()
        (paths.home / "models" / "weights.bin").write_bytes(b"x")
        retired = paths.cache_root / "search"
        retired.mkdir(parents=True)
        (retired / "thumb.jpg").write_bytes(b"x")
        kept = paths.cache_dir("files")
        (kept / "keep.txt").write_text("x", "utf-8")
        removed = paths.remove_retired_data()
        self.assertEqual(sorted(p.name for p in removed), ["models", "search"])
        self.assertFalse((paths.home / "models").exists())
        self.assertFalse(retired.exists())
        self.assertTrue((kept / "keep.txt").is_file(), "other cache folders stay")

    def test_atomic_write_and_broken_json(self):
        target = self.root / "x" / "data.json"
        atomic_write_json(target, {"a": 1, "text": "Ünïcode"})
        self.assertEqual(read_json(target), {"a": 1, "text": "Ünïcode"})
        self.assertFalse(target.with_name("data.json.part").exists())
        target.write_text("{broken", "utf-8")
        self.assertEqual(read_json(target, {"default": True}), {"default": True})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("settings")
        self.file = self.root / "settings.json"

    def tearDown(self):
        remove_tree(self.root)

    def test_defaults_and_persistence(self):
        settings = Settings(self.file)
        self.assertEqual(settings.get("theme"), DEFAULT_THEME)
        self.assertTrue(settings.get("keep_running_with_premiere"))
        self.assertEqual(DEFAULT_THEME, "black")
        settings.patch({"theme": "Sky", "keep_running_with_premiere": False})
        again = Settings(self.file)
        self.assertEqual(again.get("theme"), "sky")
        self.assertFalse(again.get("keep_running_with_premiere"))

    def test_pins_are_normalised_stored_and_not_public(self):
        settings = Settings(self.file)
        self.assertEqual(settings.pins(), [])
        self.assertEqual(settings.set_pins(["actions.colour", "files.sort", "actions.colour"]),
                         ["actions.colour", "files.sort"])
        self.assertEqual(Settings(self.file).pins(), ["actions.colour", "files.sort"])
        self.assertNotIn("pins", settings.public())  # edited from the panel only
        with self.assertRaises(ValueError):
            settings.patch({"pins": []})
        with self.assertRaises(ValueError):
            settings.set_pins("files.sort")
        self.assertEqual(normalize_pins([f"a.t{i}" for i in range(8)]), [f"a.t{i}" for i in range(5)])
        self.assertEqual(normalize_pins(["files.sort", "", "nodot", 5, None, "Files.sort", "files.", "a.b c"]),
                         ["files.sort"])
        self.file.write_text(json.dumps({"pins": ["files.sort", 3, "files.sort", "bad", "actions.dup"]}), "utf-8")
        self.assertEqual(Settings(self.file).pins(), ["files.sort", "actions.dup"])
        self.file.write_text(json.dumps({"pins": "files.sort"}), "utf-8")
        self.assertEqual(Settings(self.file).pins(), [])

    def test_show_activity_defaults_off_and_is_patchable(self):
        settings = Settings(self.file)
        self.assertIs(settings.get("show_activity"), False)
        self.assertIs(settings.public()["show_activity"], False)
        self.assertIs(settings.patch({"show_activity": True})["show_activity"], True)
        self.assertIs(Settings(self.file).get("show_activity"), True)
        for bad in ("yes", 1, None, "true"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.patch({"show_activity": bad})
        self.assertIs(settings.get("show_activity"), True)
        self.file.write_text(json.dumps({"show_activity": "on"}), "utf-8")
        self.assertIs(Settings(self.file).get("show_activity"), False)  # a bad stored value falls back to the default

    def test_ui_scale_snaps_to_a_step_and_is_patchable(self):
        settings = Settings(self.file)
        self.assertEqual(settings.get("ui_scale"), 100)
        self.assertEqual(settings.public()["ui_scale"], 100)
        self.assertEqual(settings.patch({"ui_scale": 150})["ui_scale"], 150)
        self.assertEqual(Settings(self.file).get("ui_scale"), 150)
        for value, step in ((112.5, 125), (130, 125), ("175", 175), (1000, 200), (1, 100)):
            self.assertEqual(settings.patch({"ui_scale": value})["ui_scale"], step, repr(value))
        for bad in ("big", None, True, 0, -50, float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.patch({"ui_scale": bad})
        self.assertEqual(settings.get("ui_scale"), 100)
        self.file.write_text(json.dumps({"ui_scale": "huge"}), "utf-8")
        self.assertEqual(Settings(self.file).get("ui_scale"), 100)  # a bad stored value falls back to the default
        self.file.write_text(json.dumps({"ui_scale": 160}), "utf-8")
        self.assertEqual(Settings(self.file).get("ui_scale"), 150)  # a hand-edited number snaps to its step

    def test_theme_names_of_1x_fall_back_to_black(self):
        for old in ("dusk", "nova", "frost", "bloom", "", None, 7):
            self.file.write_text(json.dumps({"theme": old}), "utf-8")
            self.assertEqual(Settings(self.file).get("theme"), "black", old)
        for name in ("black", "sky", " SKY "):
            self.file.write_text(json.dumps({"theme": name}), "utf-8")
            self.assertEqual(Settings(self.file).get("theme"), name.strip().lower())

    def test_bad_values_fall_back_and_patch_refuses(self):
        self.file.write_text(json.dumps({"theme": "neon", "keep_running_with_premiere": "yes", "future_key": 3}), "utf-8")
        settings = Settings(self.file)
        self.assertEqual(settings.get("theme"), DEFAULT_THEME)
        self.assertTrue(settings.get("keep_running_with_premiere"))
        self.assertEqual(settings.get("future_key"), 3)
        with self.assertRaises(ValueError):
            settings.patch({"theme": "neon"})
        with self.assertRaises(ValueError):
            settings.patch({"theme": "nova"})
        with self.assertRaises(ValueError):
            settings.patch({"recent_projects": []})
        with self.assertRaises(ValueError):
            settings.patch({"premiere_exe": "C:\\notes.txt"})
        self.assertEqual(settings.patch({"premiere_exe": ""})["premiere_exe"], "auto")

    def test_recent_projects_and_modules(self):
        settings = Settings(self.file)
        for name in ("a", "b", "A"):
            settings.add_recent_project(f"C:\\p\\{name}.prproj")
        self.assertEqual(settings.get("recent_projects"), ["C:\\p\\A.prproj", "C:\\p\\b.prproj"])
        self.assertTrue(settings.module_enabled("files", True))
        settings.set_module_enabled("files", False)
        self.assertFalse(Settings(self.file).module_enabled("files", True))
        host = HostModuleSettings(settings, "files", lambda: None)
        host.set("toast_filed", False)
        self.assertEqual(Settings(self.file).module_values("files"), {"toast_filed": False})
        self.assertEqual(host.get("missing", 7), 7)

    def test_project_scope_reads_and_writes_project_json(self):
        deck = self.root / "Project A" / ".premiere-deck"
        current = {"deck": None}
        scoped = ProjectModuleSettings("files", lambda: current["deck"])
        self.assertEqual(scoped.get("layout", "default"), "default")
        with self.assertRaises(RuntimeError):
            scoped.set("layout", "custom")
        current["deck"] = str(deck)
        deck.mkdir(parents=True)
        (deck / "project.json").write_text(json.dumps({"layout_id": "x", "modules": {"extra": {"a": 1}}}), "utf-8")
        scoped.set("layout", "custom")
        data = json.loads((deck / "project.json").read_text("utf-8"))
        self.assertEqual(data["layout_id"], "x")
        self.assertEqual(data["modules"]["extra"], {"a": 1})
        self.assertEqual(data["modules"]["files"], {"layout": "custom"})
        (deck / "project.json").write_text("not json", "utf-8")
        self.assertEqual(scoped.get("layout", "default"), "default")
        scoped.set("watch", True)
        self.assertEqual(scoped.all(), {"watch": True})

    def test_coerce_setting(self):
        self.assertIs(coerce_setting("bool", True), True)
        with self.assertRaises(ValueError):
            coerce_setting("bool", "true")
        self.assertEqual(coerce_setting("int", "16"), 16)
        self.assertEqual(coerce_setting("int", 2.0), 2)
        with self.assertRaises(ValueError):
            coerce_setting("int", "2.5")
        options = [{"value": "default", "label": "Default"}]
        self.assertEqual(coerce_setting("select", "default", options), "default")
        with self.assertRaises(ValueError):
            coerce_setting("select", "other", options)
        self.assertEqual(coerce_setting("path", None), "")


if __name__ == "__main__":
    unittest.main()
