"""Layouts, rules (with the classifier fields of docs/FILES.md 2), overrides, exclusions, root detection, mapping."""

import json
import os
import unittest
from dataclasses import replace

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
        self.assertEqual(len(self.layout.rules), 16)
        self.assertEqual(self.layout.media_dir, "Media")
        self.assertEqual(list(self.layout.folders),
                         ["Footage", "Stock", "Generated", "Overlays", "Audio/Music", "Audio/SFX", "Audio/Voice",
                          "Graphics", "Motion", "Media", "Exports", "Exports/Frames", "Project"])
        self.assertIn("default", L.shipped_layouts())
        self.assertEqual(self.layout.to_json()["media_dir"], "Media")

    def test_rule_1_video_with_alpha_is_overlay(self):
        self.assertEqual(self.folder(mi("logo.mov", "video", alpha=True)), "Overlays")

    def test_rule_2_video_named_like_an_overlay(self):
        for name in ("Film_Grain_4K.mp4", "light leak 03.mov", "VHS-noise.mp4", "smoke_loop.mov", "Dust.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.folder(mi(name, "video")), "Overlays")

    def test_rule_3_other_video_is_footage(self):
        self.assertEqual(self.folder(mi("A001_C002.mp4", "video", duration=12.0)), "Footage")
        self.assertEqual(self.folder(mi("x.mp4", "video", source="camera")), "Footage")
        self.assertEqual(self.folder(mi("x.mp4", "video", source="unknown")), "Footage")

    def test_classifier_rules_generated_and_stock_video(self):
        self.assertEqual(self.folder(mi("x.mp4", "video", source="generated")), "Generated")
        self.assertEqual(self.folder(mi("x.mp4", "video", source="stock")), "Stock")
        # alpha and overlay names come first
        self.assertEqual(self.folder(mi("x.mov", "video", alpha=True, source="stock")), "Overlays")
        self.assertEqual(self.folder(mi("film grain.mp4", "video", source="generated")), "Overlays")

    def test_classifier_rules_audio_class(self):
        self.assertEqual(self.folder(mi("a.wav", "audio", duration=200.0, audio_class="speech")), "Audio/Voice")
        self.assertEqual(self.folder(mi("a.wav", "audio", duration=5.0, audio_class="music")), "Audio/Music")
        self.assertEqual(self.folder(mi("a.wav", "audio", duration=200.0, audio_class="other")), "Audio/SFX")
        # a name rule wins over the class; an unknown class falls to the duration rules
        self.assertEqual(self.folder(mi("whoosh.wav", "audio", duration=200.0, audio_class="speech")), "Audio/SFX")
        self.assertEqual(self.folder(mi("vo_01.wav", "audio", duration=200.0, audio_class="music")), "Audio/Voice")
        self.assertEqual(self.folder(mi("a.wav", "audio", duration=5.0)), "Audio/SFX")

    def test_classifier_runs_only_when_a_classifier_rule_is_reached(self):
        calls = []

        def enrich(info):
            calls.append(info.path)
            return replace(info, audio_class="speech")

        named = mi("whoosh_long.wav", "audio", duration=200.0)
        self.assertEqual(classify(named, self.layout, enrich=enrich).folder, "Audio/SFX")
        self.assertEqual(calls, [])
        decision = classify(mi("talk.wav", "audio", duration=200.0), self.layout, enrich=enrich)
        self.assertEqual((decision.folder, decision.info.audio_class), ("Audio/Voice", "speech"))
        self.assertEqual(len(calls), 1)
        overrides = parse_overrides([{"name": "talk*", "to": "Audio/Music"}])
        self.assertEqual(classify(mi("talk.wav", "audio"), self.layout, overrides, enrich=enrich).folder,
                         "Audio/Music")
        self.assertEqual(len(calls), 1)

        def broken(info):
            raise RuntimeError("model failed")

        self.assertEqual(classify(mi("talk.wav", "audio", duration=200.0), self.layout, enrich=broken).folder,
                         "Audio/Music")

    def test_list_values_of_classifier_fields(self):
        layout = L.parse_layout({"folders": ["A", "B"], "rules": [
            {"match": {"kind": "video", "source": ["stock", "generated"]}, "to": "A"}, {"match": {}, "to": "B"}]})
        self.assertEqual(classify(mi("x.mp4", "video", source="generated"), layout).folder, "A")
        self.assertEqual(classify(mi("x.mp4", "video", source="camera"), layout).folder, "B")
        self.assertEqual(classify(mi("x.mp4", "video"), layout).folder, "B")

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
            {"name": "*", "to": "Media/Sub"},  # into the Sync folder: skipped
        ], media_dir="Media")
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
                 json.dumps({"folders": ["A"], "rules": [{"match": {"colour": 1}, "to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"source": "drone"}, "to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"audio_class": ["speech", "noise"]}, "to": "A"}]}),
                 json.dumps({"folders": ["A"], "rules": [{"match": {"source": []}, "to": "A"}]}),
                 json.dumps({"folders": ["A", "Media"], "rules": [{"match": {}, "to": "Media"}]}),
                 json.dumps({"folders": ["A"], "media_dir": "Sync", "rules": [{"match": {}, "to": "Sync/x"}]}),
                 json.dumps({"folders": ["A"], "media_dir": "../x", "rules": [{"match": {}, "to": "A"}]})]
        for text in cases:
            with self.subTest(text=text):
                with open(bad, "w", encoding="utf-8") as handle:
                    handle.write(text)
                with self.assertRaises(L.LayoutError):
                    L.load_layout_file(bad)
        with self.assertRaises(L.LayoutError):
            L.load_layout_file(os.path.join(self.dir, "missing.json"))

    def test_1x_layout_without_media_dir_gets_one(self):
        layout = L.parse_layout({"folders": ["Video"], "rules": [{"match": {}, "to": "Video"}]})
        self.assertEqual((layout.media_dir, list(layout.folders)), ("Media", ["Video", "Media"]))
        custom = L.parse_layout({"folders": ["Video", "Inbox"], "media_dir": "inbox",
                                 "rules": [{"match": {}, "to": "Video"}]})
        self.assertEqual(list(custom.folders), ["Video", "Inbox"])
        self.assertTrue(custom.in_media_dir("Inbox/Day1"))

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
        masks = L.masks_folder(os.path.join(root, "Sample.prproj"))
        self.assertEqual(masks, "sample masks")
        mask_file = os.path.join(root, "Sample Masks", "47844aff.prmf")
        self.assertTrue(L.is_excluded(mask_file, root, masks=masks))
        self.assertTrue(L.is_excluded(os.path.join(root, "sample masks", "x.mp4"), root, masks=masks))
        self.assertTrue(L.is_excluded(os.path.join(root, "Sample Masks"), root, masks=masks))
        self.assertTrue(L.is_excluded(os.path.join(root, "Other", "a.prmf"), root))  # .prmf anywhere
        self.assertFalse(L.is_excluded(os.path.join(root, "Other Masks", "x.mp4"), root, masks=masks))
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
        self.assertEqual(m.deepest_layout_folder(j("Footage", "Day1", "a.mp4")), "Footage")
        self.assertEqual(m.deepest_layout_folder(j("audio", "sfx", "x", "a.wav")), "Audio/SFX")
        self.assertIsNone(m.deepest_layout_folder(j("Footage2", "a.mp4")))
        self.assertIsNone(m.deepest_layout_folder(j("Project", "a.mp4")))
        self.assertEqual(m.rel_folder(j("Footage", "Day1", "a.mp4")), "Footage/Day1")
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
