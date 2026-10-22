"""Premiere discovery without starting Premiere: fake registry entries and fake Program Files folders."""

import os
import unittest
from pathlib import Path
from unittest import mock

from premiere_deck.host import launcher as L
from tests.test_host_helpers import remove_tree, scratch_dir


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("launcher")
        self.programs = self.root / "Program Files"
        self.exe_2025 = self._install("Adobe Premiere Pro 2025")
        self.exe_2026 = self._install("Adobe Premiere Pro 2026")

    def tearDown(self):
        remove_tree(self.root)

    def _install(self, folder_name):
        folder = self.programs / "Adobe" / folder_name
        folder.mkdir(parents=True)
        exe = folder / L.EXE_NAME
        exe.write_bytes(b"MZ")
        return exe

    def registry(self):
        base = str(self.programs / "Adobe")
        return {
            "25.0": {"PluginInstallPath": base + "\\Adobe Premiere Pro 2025\\Plug-ins\\Common"},
            "26.0": {"SequencePresetsPath": base + "\\Adobe Premiere Pro 2026\\Settings\\SequencePresets",
                     "CommonPluginInstallPath": "C:\\Program Files\\Adobe\\Common\\Plug-ins\\7.0\\MediaCore"},
            "24.0": {"InstallPath": str(self.root / "gone")},
            "CurrentVersion": {"": "26.0"},
        }

    def test_install_folder_from_value(self):
        self.assertEqual(L.install_folder_from_value("C:\\Program Files\\Adobe\\Adobe Premiere Pro 2026\\Plug-ins\\Common"),
                         "C:\\Program Files\\Adobe\\Adobe Premiere Pro 2026")
        self.assertIsNone(L.install_folder_from_value("C:\\Program Files\\Adobe\\Common\\Plug-ins"))

    def test_registry_newest_first_and_dedup(self):
        installs = L.find_premiere(read_key=self.registry, roots=[str(self.programs)])
        self.assertEqual([i.exe for i in installs], [str(self.exe_2026), str(self.exe_2025)])
        self.assertEqual(installs[0].version, "26.0")
        self.assertEqual(installs[0].source, "registry")

    def test_folders_only(self):
        installs = L.find_premiere(read_key=lambda: {}, roots=[str(self.programs)])
        self.assertEqual([(i.version, i.source) for i in installs], [("2026", "folder"), ("2025", "folder")])

    def test_setting_overrides(self):
        custom = self.root / "Portable" / L.EXE_NAME
        custom.parent.mkdir()
        custom.write_bytes(b"MZ")
        installs = L.find_premiere(str(custom), read_key=self.registry, roots=[str(self.programs)])
        self.assertEqual(installs[0].source, "setting")
        self.assertEqual(Path(installs[0].exe), custom)
        missing = L.find_premiere(str(self.root / "nope.exe"), read_key=self.registry, roots=[str(self.programs)])
        self.assertEqual(missing[0].source, "registry", "an override that does not exist is ignored")
        self.assertIsNone(L.best_premiere(read_key=lambda: {}, roots=[str(self.root / "empty")]))

    def test_recent_projects_from_prefs(self):
        docs = self.root / "Documents"
        prefs = docs / "Adobe" / "Premiere Pro" / "26.0" / "Profile-user" / "Adobe Premiere Pro Prefs"
        prefs.parent.mkdir(parents=True)
        prefs.write_text(
            "<?xml version='1.0'?><PremiereData><Properties Version=\"1\">"
            "<BE.Prefs.MRU.Document.2>D:\\Edits\\Old\\Old.prproj</BE.Prefs.MRU.Document.2>"
            "<BE.Prefs.MRU.Document.1>\\\\?\\D:\\Edits\\New\\New.prproj</BE.Prefs.MRU.Document.1>"
            "<BE.Prefs.MRU.Document.Date.1>2026-10-21T16:12:47Z</BE.Prefs.MRU.Document.Date.1>"
            "<BE.Prefs.MRU.SequencePreset>C:\\x.sqpreset</BE.Prefs.MRU.SequencePreset>"
            "</Properties></PremiereData>", "utf-8")
        older = docs / "Adobe" / "Premiere Pro" / "25.0" / "Profile-user" / "Adobe Premiere Pro Prefs"
        older.parent.mkdir(parents=True)
        older.write_text("<MRU.Document.1>D:\\x.prproj</MRU.Document.1>", "utf-8")
        recent = L.premiere_recent_projects(str(docs))
        self.assertEqual(recent, [{"path": "D:\\Edits\\New\\New.prproj", "opened_at": "2026-10-21T16:12:47Z"},
                                  {"path": "D:\\Edits\\Old\\Old.prproj", "opened_at": None}])

    def test_launch_refuses_missing_files_and_watch_reports_changes(self):
        with self.assertRaises(FileNotFoundError):
            L.launch(str(self.root / "missing.exe"))
        with self.assertRaises(FileNotFoundError):
            L.launch(str(self.exe_2026), project=str(self.root / "missing.prproj"))
        project = self.root / "Edit.prproj"
        project.write_text("", "utf-8")
        launched = []

        class _Proc:
            pid = 4242

        with mock.patch.object(L.procs, "launch_detached", side_effect=lambda args, cwd: launched.append(args) or _Proc()):
            self.assertEqual(L.launch(str(self.exe_2026), project=str(project).replace("\\", "/")), 4242)
        self.assertEqual(launched, [[str(self.exe_2026), os.path.normpath(str(project))]])
        changes = []
        state = {"running": False}
        watch = L.ProcessWatch(changes.append, probe=lambda: state["running"])
        watch.poll()
        state["running"] = True
        watch.poll()
        watch.poll()
        state["running"] = False
        watch.poll()
        self.assertEqual(changes, [False, True, False])

    def test_process_ids_does_not_crash(self):
        self.assertIsInstance(L.process_ids("surely-not-running-deck.exe"), list)
        self.assertEqual(L.process_ids("surely-not-running-deck.exe"), [])


if __name__ == "__main__":
    unittest.main()
