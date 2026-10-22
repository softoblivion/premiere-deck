"""Classification of real media generated with ffmpeg and probed with the real ffprobe (core/probe.py)."""

import os
import unittest

from premiere_deck.core.probe import probe
from premiere_deck.modules.files.classify import classify
from premiere_deck.modules.files.layout import default_layout
from tests.test_modules_files_support import ffmpeg_available, make_media, remove_tree, scratch


@unittest.skipUnless(ffmpeg_available(), "ffmpeg and ffprobe are needed on PATH")
class RealMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = scratch("files-media-")
        cls.media = make_media(os.path.join(cls.dir, "src"))
        cls.layout = default_layout()

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.dir)

    def folder(self, key):
        info = probe(self.media[key])
        self.assertIsNone(info.probe_error, info.probe_error)
        return classify(info, self.layout).folder, info

    def test_vp9_webm_with_alpha_goes_to_overlays(self):
        folder, info = self.folder("alpha")
        self.assertEqual(info.kind, "video")
        self.assertTrue(info.alpha)
        self.assertEqual(folder, "Overlays")

    def test_plain_mp4_goes_to_footage(self):
        folder, info = self.folder("plain")
        self.assertFalse(info.alpha)
        self.assertTrue(info.has_audio)
        self.assertEqual(folder, "Footage")

    def test_half_second_wav_goes_to_sfx(self):
        folder, info = self.folder("short_wav")
        self.assertEqual(info.kind, "audio")
        self.assertLess(info.duration, 1.0)
        self.assertEqual(folder, "Audio/SFX")

    def test_forty_second_wav_goes_to_music(self):
        folder, info = self.folder("long_wav")
        self.assertGreater(info.duration, 30.0)
        self.assertEqual(folder, "Audio/Music")

    def test_png_goes_to_graphics(self):
        folder, info = self.folder("png")
        self.assertEqual(info.kind, "image")
        self.assertEqual(folder, "Graphics")


if __name__ == "__main__":
    unittest.main()
