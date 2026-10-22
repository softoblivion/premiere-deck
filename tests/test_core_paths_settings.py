import json
import os
import unittest
from pathlib import Path
from unittest import mock

from premiere_deck.core import paths as paths_mod
from premiere_deck.core.paths import AppPaths, atomic_write_json, read_json
from premiere_deck.core.settings import (
    DEFAULT_THEME, HostModuleSettings, ProjectModuleSettings, Settings, coerce_setting,
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
        for folder in (paths.bin_dir, paths.models_dir, paths.logs_dir, paths.webview_dir, paths.updates_dir):
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
        settings.patch({"theme": "Bloom", "keep_running_with_premiere": False})
        again = Settings(self.file)
        self.assertEqual(again.get("theme"), "bloom")
        self.assertFalse(again.get("keep_running_with_premiere"))

    def test_bad_values_fall_back_and_patch_refuses(self):
        self.file.write_text(json.dumps({"theme": "neon", "keep_running_with_premiere": "yes", "future_key": 3}), "utf-8")
        settings = Settings(self.file)
        self.assertEqual(settings.get("theme"), DEFAULT_THEME)
        self.assertTrue(settings.get("keep_running_with_premiere"))
        self.assertEqual(settings.get("future_key"), 3)
        with self.assertRaises(ValueError):
            settings.patch({"theme": "neon"})
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
        (deck / "project.json").write_text(json.dumps({"layout_id": "x", "modules": {"search": {"a": 1}}}), "utf-8")
        scoped.set("layout", "custom")
        data = json.loads((deck / "project.json").read_text("utf-8"))
        self.assertEqual(data["layout_id"], "x")
        self.assertEqual(data["modules"]["search"], {"a": 1})
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
