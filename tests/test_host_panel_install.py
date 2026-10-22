"""Panel installer on scratch folders (docs/PANEL.md 4); the registry is a dictionary fake."""

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from premiere_deck.core.paths import AppPaths
from premiere_deck.core.settings import Settings
from premiere_deck.host import panel_install as PI
from premiere_deck.host.panel_install import MemoryRegistry, PanelInstaller
from tests.test_host_helpers import make_panel_source, remove_tree, scratch_dir


class PanelInstallTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("panel")
        self.paths = AppPaths(self.root / "data")
        self.paths.ensure()
        self.source = make_panel_source(self.root / "src_panel", "1.0.0")
        self.extensions = self.root / "cep" / "extensions"
        self.env = mock.patch.dict(os.environ, {PI.ENV_CEP_DIR: str(self.extensions)})
        self.env.start()
        self.settings = Settings(self.paths.settings_file)
        self.registry = MemoryRegistry({("Software\\Adobe\\CSXS.12", "PlayerDebugMode"): "1"})
        self.installer = PanelInstaller(self.paths, "1.0.0", source=self.source, settings=self.settings,
                                        registry=self.registry)

    def tearDown(self):
        self.env.stop()
        remove_tree(self.root)

    def test_target_follows_the_environment(self):
        self.assertEqual(self.installer.target, self.extensions / "com.softoblivion.premieredeck")
        self.assertEqual(PI.default_target(), self.extensions / "com.softoblivion.premieredeck")

    def test_fresh_install_then_nothing_to_do(self):
        self.assertTrue(self.installer.needs_install())
        result = self.installer.ensure()
        self.assertEqual(result.action, "installed")
        self.assertEqual(self.installer.installed_version(), "1.0.0")
        self.assertTrue((self.paths.panel_dir / "1.0.0" / "index.html").is_file(), "staged copy")
        self.assertTrue((self.installer.target / "CSXS" / "manifest.xml").is_file())
        self.assertFalse(self.installer.needs_install())
        self.assertEqual(self.installer.ensure().action, "none")
        status = self.installer.status()
        self.assertTrue(status["installed"] and status["up_to_date"])
        self.assertFalse(status["junction"])

    def test_missing_version_reads_zero(self):
        os.remove(self.source / "VERSION")
        self.assertEqual(self.installer.source_version(), "0")
        self.assertEqual(self.installer.ensure().action, "installed")
        self.assertEqual(self.installer.installed_version(), "0")

    def test_update_files_only_means_reload(self):
        self.installer.ensure()
        (self.installer.target / "deck.json").write_text('{"port": 1}', "utf-8")
        make_panel_source(self.source, "1.0.1", extra="two")
        result = self.installer.ensure()
        self.assertEqual((result.action, result.previous_version, result.manifest_changed), ("updated", "1.0.0", False))
        self.assertEqual((self.installer.target / "index.html").read_text("utf-8"), "<html>two</html>")
        self.assertEqual(json.loads((self.installer.target / "deck.json").read_text("utf-8")), {"port": 1},
                         "deck.json survives the swap")
        siblings = [p.name for p in self.installer.target.parent.iterdir()]
        self.assertEqual(siblings, ["com.softoblivion.premieredeck"], "no .new / .old leftovers")
        self.assertEqual(self.installer.update_for("1.0.0", "s1"), {"action": "reload"})
        self.assertEqual(self.installer.update_for("1.0.0", "s1"), {"action": "restart_premiere"},
                         "a panel that is still old after a reload needs a restart")
        self.assertIsNone(self.installer.update_for("1.0.1", "s1"))

    def test_manifest_change_means_restart(self):
        self.installer.ensure()
        make_panel_source(self.source, "1.1.0", manifest="<manifest v='2'/>")
        result = self.installer.ensure()
        self.assertTrue(result.manifest_changed)
        self.assertEqual(self.installer.update_for("1.0.0"), {"action": "restart_premiere"})
        # A later host run (new installer object) still knows it from install.json.
        later = PanelInstaller(self.paths, "1.1.0", source=self.source, settings=self.settings, registry=self.registry)
        self.assertEqual(later.update_for("1.0.0"), {"action": "restart_premiere"})

    def test_force_reinstall_and_leftover_old_folder(self):
        self.installer.ensure()
        old = self.installer.target.with_name(self.installer.target.name + ".old")
        old.mkdir()
        (old / "stale.txt").write_text("x", "utf-8")
        result = self.installer.ensure(force=True)
        self.assertEqual(result.action, "updated")
        self.assertFalse(old.exists())

    def test_junction_is_never_touched(self):
        dev = make_panel_source(self.root / "dev_panel", "0.0.1-dev")
        self.extensions.mkdir(parents=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.installer.target), str(dev)], check=True,
                       capture_output=True)
        self.assertTrue(self.installer.is_junction())
        self.assertFalse(self.installer.needs_install())
        result = self.installer.ensure(force=True)
        self.assertEqual(result.action, "skipped_junction")
        self.assertEqual((dev / "VERSION").read_text("utf-8"), "0.0.1-dev")
        self.assertIsNone(self.installer.update_for("0.0.0"))
        self.installer.uninstall()
        self.assertFalse(self.installer.target.exists())
        self.assertTrue((dev / "index.html").is_file(), "removing the link keeps the source")

    def test_debug_mode_writes_only_missing_values_and_uninstall_removes_them(self):
        written = self.installer.enable_debug_mode()
        self.assertEqual(written, ["CSXS.9", "CSXS.10", "CSXS.11"])
        self.assertEqual(self.settings.get("debug_mode_written"), ["CSXS.9", "CSXS.10", "CSXS.11"])
        for n in (9, 10, 11, 12):
            self.assertEqual(self.registry.get(f"Software\\Adobe\\CSXS.{n}", "PlayerDebugMode"), "1")
        self.installer.ensure()
        result = self.installer.uninstall()
        self.assertTrue(result["removed"])
        self.assertEqual(result["registry_removed"], ["CSXS.9", "CSXS.10", "CSXS.11"])
        self.assertEqual(self.registry.values, {("Software\\Adobe\\CSXS.12", "PlayerDebugMode"): "1"},
                         "the value the user had before stays")
        self.assertEqual(self.settings.get("debug_mode_written"), [])
        self.assertIsNone(self.installer.installed_version())

    def test_missing_source(self):
        installer = PanelInstaller(self.paths, "1.0.0", source=self.root / "nowhere", settings=self.settings,
                                   registry=self.registry)
        result = installer.ensure()
        self.assertEqual(result.action, "no_source")
        self.assertFalse(installer.target.exists())


if __name__ == "__main__":
    unittest.main()
