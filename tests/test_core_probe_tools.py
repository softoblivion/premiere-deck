import os
import shutil
import unittest
import zipfile
from pathlib import Path

from premiere_deck.core.media import KIND_AUDIO, KIND_IMAGE, KIND_VIDEO
from premiere_deck.core.paths import AppPaths
from premiere_deck.core.probe import has_alpha_pix_fmt, parse_probe, probe, quick_hash, stream_alpha
from premiere_deck.core.tools import HostTools, extract_ffmpeg, find_ffmpeg, parse_ffmpeg_version, parse_sums
from tests.test_host_helpers import remove_tree, scratch_dir

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class ProbeRulesTests(unittest.TestCase):
    def test_alpha_pixel_formats(self):
        for fmt in ("yuva420p", "yuva444p10le", "rgba", "argb", "bgra", "abgr", "ya8", "gbrap", "rgba64le"):
            self.assertTrue(has_alpha_pix_fmt(fmt), fmt)
        for fmt in ("yuv420p", "gray", "bayer_rggb8", "nv12", "yuvj422p", "pal8", None, ""):
            self.assertFalse(has_alpha_pix_fmt(fmt), fmt)

    def test_stream_alpha_rules(self):
        self.assertTrue(stream_alpha({"codec_name": "vp9", "pix_fmt": "yuv420p", "tags": {"ALPHA_MODE": "1"}}))
        self.assertTrue(stream_alpha({"codec_name": "av1", "pix_fmt": "yuv420p", "tags": {"alpha_mode": "1"}}))
        self.assertFalse(stream_alpha({"codec_name": "vp9", "pix_fmt": "yuv420p", "tags": {"alpha_mode": "0"}}))
        self.assertTrue(stream_alpha({"codec_name": "prores", "profile": "4444"}))
        self.assertFalse(stream_alpha({"codec_name": "prores", "profile": "4444", "pix_fmt": "yuv444p12le"}))
        self.assertFalse(stream_alpha({"codec_name": "prores", "profile": "HQ", "pix_fmt": "yuv422p10le"}))

    def test_parse_probe_kinds(self):
        data = {"format": {"duration": "2.5", "format_name": "mov,mp4"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                             "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p"},
                            {"codec_type": "audio", "codec_name": "aac"}]}
        info = parse_probe("C:\\a\\clip.MP4", data, size=10)
        self.assertEqual((info.kind, info.ext, info.width, info.fps, info.duration), (KIND_VIDEO, "mp4", 1920, 29.97, 2.5))
        self.assertTrue(info.has_audio and info.has_video)
        self.assertFalse(info.alpha)
        audio_only = parse_probe("x.mp4", {"format": {"duration": "3"}, "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}]}, size=1)
        self.assertEqual(audio_only.kind, KIND_AUDIO)
        self.assertFalse(audio_only.has_video)
        image = parse_probe("x.png", {"format": {"format_name": "png_pipe"}, "streams": [
            {"codec_type": "video", "codec_name": "png", "pix_fmt": "rgba", "width": 4, "height": 4}]}, size=1)
        self.assertEqual(image.kind, KIND_IMAGE)
        self.assertTrue(image.alpha)
        self.assertIsNone(image.duration)

    def test_missing_file(self):
        info = probe("Z:\\does\\not\\exist.mov")
        self.assertEqual(info.kind, KIND_VIDEO)
        self.assertEqual(info.probe_error, "file not found")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe on PATH are needed")
class ProbeMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = scratch_dir("probe")
        cls.paths = AppPaths(cls.root / "data")
        cls.paths.ensure()
        cls.tools = HostTools(cls.paths)
        lavfi = ["-hide_banner", "-loglevel", "error", "-y"]
        cls.plain = str(cls.root / "plain.mp4")
        cls.webm = str(cls.root / "alpha.webm")
        cls.prores = str(cls.root / "alpha_4444.mov")
        cls.png = str(cls.root / "still.png")
        cls.wav = str(cls.root / "tone.wav")
        jobs = [
            lavfi + ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                     "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.plain],
            lavfi + ["-f", "lavfi", "-i", "color=c=red@0.5:size=160x120:rate=25,format=yuva420p", "-t", "1",
                     "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", cls.webm],
            lavfi + ["-f", "lavfi", "-i", "color=c=blue@0.5:size=160x120:rate=25,format=yuva444p10le", "-t", "1",
                     "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", cls.prores],
            lavfi + ["-f", "lavfi", "-i", "color=c=green@0.5:size=32x32,format=rgba", "-frames:v", "1", cls.png],
            lavfi + ["-f", "lavfi", "-i", "sine=frequency=220", "-t", "0.5", cls.wav],
        ]
        for args in jobs:
            code = cls.tools.ffmpeg(args, timeout=60)
            if code != 0:
                raise RuntimeError(f"ffmpeg failed ({code}) for {args[-1]}")

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.root)

    def test_plain_mp4(self):
        info = self.tools.ffprobe(self.plain)
        self.assertEqual(info.kind, KIND_VIDEO)
        self.assertFalse(info.alpha)
        self.assertTrue(info.has_audio)
        self.assertEqual((info.width, info.height, info.fps), (160, 120, 25.0))
        self.assertAlmostEqual(info.duration, 1.0, delta=0.15)
        self.assertEqual(info.codec_video, "h264")
        self.assertIsNone(info.probe_error)

    def test_alpha_webm_vp9(self):
        info = self.tools.ffprobe(self.webm)
        self.assertEqual(info.kind, KIND_VIDEO)
        self.assertEqual(info.codec_video, "vp9")
        self.assertTrue(info.alpha)

    def test_alpha_prores_4444(self):
        info = self.tools.ffprobe(self.prores)
        self.assertEqual(info.codec_video, "prores")
        self.assertTrue(info.alpha)

    def test_png_and_wav(self):
        png = self.tools.ffprobe(self.png)
        self.assertEqual(png.kind, KIND_IMAGE)
        self.assertTrue(png.alpha)
        wav = self.tools.ffprobe(self.wav)
        self.assertEqual(wav.kind, KIND_AUDIO)
        self.assertFalse(wav.has_video)

    def test_broken_file_falls_back_to_extension(self):
        broken = self.root / "broken.mov"
        broken.write_bytes(b"not a movie")
        info = self.tools.ffprobe(str(broken))
        self.assertEqual(info.kind, KIND_VIDEO)
        self.assertTrue(info.probe_error)

    def test_ffmpeg_reports_missing_and_lines(self):
        lines = []
        code = self.tools.ffmpeg(["-hide_banner", "-version"], on_line=lines.append)
        self.assertEqual(code, 0)
        self.assertTrue(any("ffmpeg version" in line for line in lines))
        status = self.tools.status()
        self.assertEqual(status["state"], "ready")

    def test_ffmpeg_pipe_streams_stdout_and_stays_in_a_job(self):
        proc = self.tools.ffmpeg_pipe(["-hide_banner", "-nostdin", "-i", self.plain, "-frames:v", "1",
                                       "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        try:
            data = proc.stdout.read()
            proc.stderr.read()
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(data), 160 * 120 * 3)
        self.assertIn(proc, self.tools._pipe_job._procs)

    def test_ffmpeg_pipe_without_ffmpeg_raises(self):
        empty = HostTools(AppPaths(self.root / "empty-data"), use_path=False)
        with self.assertRaises(FileNotFoundError):
            empty.ffmpeg_pipe(["-version"])


class QuickHashTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("hash")

    def tearDown(self):
        remove_tree(self.root)

    def test_head_tail_and_size(self):
        big = self.root / "big.bin"
        data = bytearray(os.urandom(1024)) * (10 * 1024)  # 10 MB
        big.write_bytes(bytes(data))
        first = quick_hash(str(big))
        self.assertEqual(len(first), 32)
        data[5 * 1024 * 1024] ^= 0xFF  # the middle is not hashed by design
        big.write_bytes(bytes(data))
        self.assertEqual(quick_hash(str(big)), first)
        data[-1] ^= 0xFF
        big.write_bytes(bytes(data))
        self.assertNotEqual(quick_hash(str(big)), first)
        small = self.root / "small.bin"
        small.write_bytes(b"abc")
        other = self.root / "small2.bin"
        other.write_bytes(b"abd")
        self.assertNotEqual(quick_hash(str(small)), quick_hash(str(other)))


class ToolsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("tools")
        self.paths = AppPaths(self.root)
        self.paths.ensure()

    def tearDown(self):
        remove_tree(self.root)

    def test_own_bin_wins_over_path(self):
        self.assertIsNone(find_ffmpeg(self.paths, use_path=False))
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (self.paths.bin_dir / name).write_bytes(b"MZ")
        found = find_ffmpeg(self.paths)
        self.assertEqual(found.source, "bin")
        tools = HostTools(self.paths)
        self.assertEqual(tools.path_of("ffprobe"), str(self.paths.bin_dir / "ffprobe.exe"))
        self.assertIsNone(tools.path_of("other"))

    def test_sums_versions_and_extract(self):
        sums = parse_sums("a" * 64 + "  ffmpeg.zip\n" + "B" * 64 + " *other.zip\nnoise\n")
        self.assertEqual(sums, {"ffmpeg.zip": "a" * 64, "other.zip": "b" * 64})
        self.assertEqual(parse_ffmpeg_version("ffmpeg version 9.0.2-full_build-www.gyan.dev"), "9.0.2")
        self.assertEqual(parse_ffmpeg_version("ffmpeg version N-12345-gabc"), "N-12345")
        archive = self.root / "ff.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ffmpeg-master/bin/ffmpeg.exe", b"one")
            zf.writestr("ffmpeg-master/bin/ffprobe.exe", b"two")
            zf.writestr("ffmpeg-master/doc/readme.txt", b"x")
        out = extract_ffmpeg(archive, self.paths.bin_dir)
        self.assertEqual(Path(out["ffprobe.exe"]).read_bytes(), b"two")
        self.assertFalse(any(p.name.endswith(".part") for p in self.paths.bin_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
