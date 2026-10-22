import json
import os
import unittest

from premiere_deck.core.media import MediaInfo
from premiere_deck.modules.files import layout as L
from premiere_deck.modules.files.classify import Override, classify, name_matches, parse_overrides
from tests.test_modules_files_support import remove_tree, scratch, write_file


def mi(name, kind, **kw):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return MediaInfo(path=os.path.join("C:\\src", name), kind=kind, ext=ext, size=100, **kw)


class DefaultLayoutRules(unittest.TestCase):
    """Every rule of layouts/default.json, top to bottom."""

    def setUp(self):
        self.layout = L.default_layout()

    def folder(self, info, overrides=()):
        return classify(info, self.layout, overrides).folder

    def test_shipped_default_parses(self):
        self.assertEqual(self.layout.id, "default")
        self.assertEqual(self.layout.project_dir, "Project")
        self.assertIn("Audio/SFX", self.layout.folders)
        self.assertTrue(self.layout.mirror_subfolders)
        self.assertEqual(len(self.layout.rules), 11)
        self.assertIn("default", L.shipped_layouts())

    def test_rule_1_video_with_alpha_is_overlay(self):
        self.assertEqual(self.folder(mi("logo.mov", "video", alpha=True)), "Overlays")

    def test_rule_2_video_named_like_an_overlay(self):
        for name in ("Film_Grain_4K.mp4", "light leak 03.mov", "VHS-noise.mp4", "smoke_loop.mov", "Dust.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.folder(mi(name, "video")), "Overlays")

    def test_rule_3_other_video_is_footage(self):
        self.assertEqual(self.folder(mi("A001_C002.mp4", "video", duration=12.0)), "Footage")

    def test_rule_4_sfx_names(self):
        for name in ("whoosh_01.wav", "SFX hit.mp3", "Riser-long.wav", "impact.flac"):
            with self.subTest(name=name):
                self.assertEqual(self.folder(mi(name, "audio", duration=60.0)), "Audio/SFX")

    def test_rule_5_voice_names(self):
        for name in ("VO_take2.wav", "client voice.mp3", "Interview_raw.wav", "narration.m4a"):
            with self.subTest(name=name):
                self.assertEqual(self.folder(mi(name, "audio", duration=300.0)), "Audio/Voice")

    def test_rule_6_short_audio_is_sfx(self):
        self.assertEqual(self.folder(mi("blip.wav", "audio", duration=0.5)), "Audio/SFX")
        self.assertEqual(self.folder(mi("sting.wav", "audio", duration=20.0)), "Audio/SFX")

    def test_rule_7_other_audio_is_music(self):
        self.assertEqual(self.folder(mi("track.mp3", "audio", duration=40.0)), "Audio/Music")
        self.assertEqual(self.folder(mi("unknown_length.mp3", "audio")), "Audio/Music")

    def test_rule_8_images_are_graphics(self):
        self.assertEqual(self.folder(mi("lower_third.png", "image")), "Graphics")

    def test_rule_9_motion_files(self):
        for name in ("title.mogrt", "intro.aep", "card.aegraphic"):
            with self.subTest(name=name):
                self.assertEqual(self.folder(mi(name, "other")), "Motion")

    def test_rule_10_design_files_without_probe_kind(self):
        self.assertEqual(self.folder(mi("layout.psd", "other")), "Graphics")
        self.assertEqual(self.folder(mi("icon.svg", "other")), "Graphics")

    def test_rule_11_anything_else_is_footage(self):
        self.assertEqual(self.folder(mi("notes.xyz", "other")), "Footage")

    def test_names_match_at_word_starts_only(self):
        self.assertTrue(name_matches("vo|voice", "VO_take2"))
        self.assertTrue(name_matches("vo|voice", "client vo"))
        self.assertFalse(name_matches("vo|voice", "devoted"))
        self.assertFalse(name_matches("pop", "lollipop"))
        self.assertTrue(name_matches("pop", "pop_01"))
        self.assertTrue(name_matches("(?i)grain", "FILM grain"))
        # music named "devoted" with 3 minutes stays music, not voice
        self.assertEqual(self.folder(mi("devoted.wav", "audio", duration=180.0)), "Audio/Music")

    def test_overrides_come_before_rules(self):
        overrides = parse_overrides([
            {"source": "C:\\src\\pack", "to": "Audio/SFX"},
            {"name": "*_music*", "to": "Audio/Music"},
            {"to": "Footage"},  # neither source nor name: skipped
            {"source": "C:\\x", "to": "../outside"},  # unsafe target: skipped
            "garbage",
        ])
        self.assertEqual(len(overrides), 2)
        pack = MediaInfo(path="C:\\src\\pack\\long.wav", kind="audio", ext="wav", size=1, duration=100.0)
        self.assertEqual(classify(pack, self.layout, overrides).folder, "Audio/SFX")
        self.assertTrue(classify(pack, self.layout, overrides).reason.startswith("override"))
        named = mi("whoosh_music_bed.wav", "audio", duration=1.0)
        self.assertEqual(classify(named, self.layout, overrides).folder, "Audio/Music")
        self.assertEqual(parse_overrides("not a list"), [])


class LayoutFiles(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("files-layout-")

    def tearDown(self):
        remove_tree(self.dir)

    def test_broken_layouts_raise_layout_error(self):
        bad = os.path.join(self.dir, "layout.json")
        cases = ["{not json", json.dumps([1, 2]), json.dumps({"folders": [], "rules": [{"to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"kind": "sound"}, "to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"name": "(["}, "to": "A"}]}),
                 json.dumps({"folders": ["../escape"], "rules": [{"to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"colour": 1}, "to": "A"}]})]
        for text in cases:
            with self.subTest(text=text):
                with open(bad, "w", encoding="utf-8") as handle:
                    handle.write(text)
                with self.assertRaises(L.LayoutError):
                    L.load_layout_file(bad)
        with self.assertRaises(L.LayoutError):
            L.load_layout_file(os.path.join(self.dir, "missing.json"))

    def test_clean_folder(self):
        self.assertEqual(L.clean_folder("Audio\\SFX/"), "Audio/SFX")
        for bad in ("", "..", "C:/x", ".premiere-deck", "Adobe Premiere Pro Auto-Save", "a/../b", 5, "Media Cache Files"):
            with self.subTest(bad=bad):
                self.assertIsNone(L.clean_folder(bad))

    def test_root_detection(self):
        layout = L.default_layout()
        flat = os.path.join(self.dir, "Flat")
        write_file(os.path.join(flat, "Flat.prproj"))
        self.assertEqual(L.detect_root(os.path.join(flat, "Flat.prproj"), layout), flat)
        # project file inside Project/, parent holds a layout folder -> parent is the root
        nested = os.path.join(self.dir, "Nested")
        os.makedirs(os.path.join(nested, "Footage"))
        write_file(os.path.join(nested, "Project", "Nested.prproj"))
        self.assertEqual(L.detect_root(os.path.join(nested, "Project", "Nested.prproj"), layout), nested)
        # parent holds .premiere-deck
        deck = os.path.join(self.dir, "Deck")
        os.makedirs(os.path.join(deck, ".premiere-deck"))
        write_file(os.path.join(deck, "project", "Deck.prproj"))
        self.assertEqual(L.detect_root(os.path.join(deck, "project", "Deck.prproj"), layout), deck)
        # a folder named Project with nothing beside it stays the root
        alone = os.path.join(self.dir, "Alone")
        write_file(os.path.join(alone, "Project", "Alone.prproj"))
        self.assertEqual(L.detect_root(os.path.join(alone, "Project", "Alone.prproj"), layout),
                         os.path.join(alone, "Project"))

    def test_exclusions_of_section_1_5(self):
        root = self.dir
        excluded = [
            "Sample.prproj", "Adobe Premiere Pro Auto-Save/Sample--12.prproj",
            "Adobe Premiere Pro Video Previews/x.mpeg", "Adobe Premiere Pro Audio Previews/y.cfa",
            "Adobe Premiere Pro Captured Video/c.avi", "Adobe Premiere Pro Captured Audio/c.wav",
            "Media Cache Files/a.ims", "Footage/Media Cache/b.cfa", ".premiere-deck/journal.jsonl",
            "Footage/~lock.mp4", "Footage/clip.mp4.part", "Footage/a.tmp", "Footage/b.crdownload",
            "Graphics/Thumbs.db", "Graphics/desktop.ini", "Footage/.hidden.mp4",
        ]
        for rel in excluded:
            with self.subTest(rel=rel):
                self.assertTrue(L.is_excluded(os.path.join(root, *rel.split("/")), root))
        self.assertFalse(L.is_excluded(os.path.join(root, "Footage", "clip.mp4"), root))
        hidden = write_file(os.path.join(root, "Footage", "secret.mp4"))
        os.system(f'attrib +h "{hidden}"')
        self.assertTrue(L.is_excluded(hidden, root))

    def test_mapper_folders_and_bins(self):
        root = self.dir
        m = L.Mapper(root, L.default_layout())
        j = lambda *p: os.path.join(root, *p)  # noqa: E731
        self.assertEqual(m.layout_folder_of(j("Footage", "a.mp4")), "Footage")
        self.assertEqual(m.layout_folder_of(j("footage", "Day1", "a.mp4")), "Footage/Day1")
        self.assertEqual(m.layout_folder_of(j("audio", "sfx", "a.wav")), "Audio/SFX")
        self.assertIsNone(m.layout_folder_of(j("a.mp4")))  # loose at the root
        self.assertIsNone(m.layout_folder_of(j("Project", "a.mp4")))  # next to the project file
        self.assertIsNone(m.layout_folder_of(os.path.join(os.path.dirname(root), "x.mp4")))
        self.assertEqual(m.layout_folder_of(j("Footage2", "a.mp4")), "Footage2")  # mirrored user folder
        self.assertTrue(m.is_exports(j("Exports", "Frames", "f.png")))
        self.assertFalse(m.is_exports(j("Footage", "f.png")))
        self.assertEqual(m.folder_for_bin("Overlays"), "Overlays")
        self.assertEqual(m.folder_for_bin("audio/music"), "Audio/Music")
        self.assertEqual(m.folder_for_bin("Footage/Day2"), "Footage/Day2")
        self.assertIsNone(m.folder_for_bin("Selects"))
        self.assertIsNone(m.folder_for_bin(""))
        os.makedirs(j("Footage2"))
        self.assertEqual(m.folder_for_bin("Footage2"), "Footage2")
        flat = L.Mapper(root, L.parse_layout({**L.default_layout().to_json(), "mirror_subfolders": False}))
        self.assertEqual(flat.layout_folder_of(j("Footage", "Day1", "a.mp4")), "Footage")
        self.assertIsNone(flat.layout_folder_of(j("Footage2", "a.mp4")))
        self.assertIsNone(flat.folder_for_bin("Footage/Day2"))


if __name__ == "__main__":
    unittest.main()
