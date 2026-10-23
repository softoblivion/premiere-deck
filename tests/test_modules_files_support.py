"""Shared helpers of the Files module tests (no test cases here).

Every test works in a scratch folder made with `tempfile.mkdtemp` under `PREMIERE_DECK_HOME`; `PREMIERE_DECK_HOME`
is pointed at that folder for the duration of the test and restored afterwards.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Optional

from premiere_deck.core.media import MediaInfo
from premiere_deck.modules.base import FakeHost
from premiere_deck.premiere.model import ProjectItem


def scratch(prefix: str) -> str:
    base = os.environ.get("PREMIERE_DECK_HOME") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def remove_tree(path: str, attempts: int = 20) -> None:
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1)


def write_file(path: str | Path, size: int = 4096, seed: str = "") -> str:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = (seed or os.path.basename(path)).encode("utf-8") * 64
    data = (block * (size // max(1, len(block)) + 1))[:size]
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def wait_for(condition: Callable[[], bool], timeout: float = 20.0, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run_ffmpeg(args: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags, timeout=120)


# No "Lavf" / "Lavc" encoder tags (they mark a file as stock, docs/FILES.md 3.8): bit-exact output, encoder tags empty.
BITEXACT = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-metadata", "encoder=",
            "-metadata:s:v", "encoder=", "-metadata:s:a", "encoder="]


def make_media(folder: str) -> dict[str, str]:
    """The media of docs/FILES.md 10: alpha video (VP9 WebM yuva420p), plain mp4 (written bit-exact, so it carries
    no encoder tag and its source stays unknown), 0.5 s and 40 s wav, a PNG."""
    os.makedirs(folder, exist_ok=True)
    out = {
        "alpha": os.path.join(folder, "logo_anim.webm"),
        "plain": os.path.join(folder, "interview_cam_a.mp4"),
        "short_wav": os.path.join(folder, "blip.wav"),
        "long_wav": os.path.join(folder, "background_track.wav"),
        "png": os.path.join(folder, "lower_third.png"),
    }
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=red@0.5:s=64x64:d=1,format=yuva420p", "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", out["alpha"]])
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=64x64:d=1", "-f", "lavfi", "-i", "sine=d=1", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", *BITEXACT, out["plain"]])
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:d=0.5", out["short_wav"]])
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=220:d=40", "-ar", "8000", out["long_wav"]])
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1", out["png"]])
    return out


def make_classifier_media(folder: str) -> dict[str, str]:
    """The classifier media of docs/FILES.md 10: tagged mp4 files (camera make / model, a Lavf encoder), a 5 s
    1280x720 24 fps mp4 without audio, a 20 s sine tone, a 20 s chord with tremolo (music-like), 20 s of noise and
    20 s of silence."""
    os.makedirs(folder, exist_ok=True)
    out = {
        "camera_tag": os.path.join(folder, "holiday.mp4"),
        "lavf": os.path.join(folder, "skyline.mp4"),
        "generator": os.path.join(folder, "sunset_take.mp4"),
        "generator_23976": os.path.join(folder, "sunset_ntsc.mp4"),
        "tone": os.path.join(folder, "tone.wav"),
        "chord": os.path.join(folder, "chord.wav"),
        "noise": os.path.join(folder, "noise.wav"),
        "silence": os.path.join(folder, "silence.wav"),
    }
    video = ["-f", "lavfi", "-i", "testsrc=s=64x64:d=1:r=25", "-f", "lavfi", "-i", "sine=d=1", "-shortest",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    run_ffmpeg([*video, *BITEXACT, "-movflags", "use_metadata_tags", "-metadata", "make=Sony",
                "-metadata", "model=ILCE-7SM3", out["camera_tag"]])
    run_ffmpeg([*video, out["lavf"]])  # ffmpeg writes encoder=Lavf<version>
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=1280x720:d=5:r=24", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", *BITEXACT, out["generator"]])
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=1280x720:d=5:r=24000/1001", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", *BITEXACT, out["generator_23976"]])
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:d=20", out["tone"]])
    run_ffmpeg(["-f", "lavfi", "-i",
                "aevalsrc=0.2*(sin(2*PI*261.6*t)+sin(2*PI*329.6*t)+sin(2*PI*392*t))*(0.5+0.5*sin(2*PI*2*t)):"
                "s=44100:d=20", out["chord"]])
    run_ffmpeg(["-f", "lavfi", "-i", "anoisesrc=d=20:c=white:a=0.3", out["noise"]])
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "20", out["silence"]])
    return out


def make_speech(path: str) -> bool:
    """A spoken sentence through the Windows speech synthesizer (System.Speech), or False when it is not
    available. ffmpeg alone cannot make a speech sample."""
    if os.name != "nt" or not shutil.which("powershell"):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = ("Welcome back to the channel. Today we look at how the project folder is organised, "
            "where the footage goes and how the voice over is recorded for the final cut.")
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{text}'); $s.Dispose()")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.isfile(path) and os.path.getsize(path) > 10_000


def fake_classifier(classes: Optional[dict] = None):
    """A `MediaClassifier` whose decode yields one second of silence and whose scorer answers from `classes`
    (file name -> (speech, music)); a file not listed fails like a decode error (audio_class None)."""
    import numpy as np

    from premiere_deck.modules.files.media_class import ClassifierError, MediaClassifier

    classes = dict(classes or {})
    current = {"name": ""}

    class Pipe:
        returncode = 0

        def communicate(self, timeout=None):
            return np.zeros(16000, dtype="<f4").tobytes(), b""

    def pipe(args):
        current["name"] = os.path.basename(args[args.index("-i") + 1])
        if current["name"] not in classes:
            raise FileNotFoundError("ffmpeg is not available")
        return Pipe()

    def scorer(waveform):
        if current["name"] not in classes:
            raise ClassifierError("not scored")
        return classes[current["name"]]

    classifier = MediaClassifier(pipe, scorer=scorer)
    classifier.classes = classes
    return classifier


def info(path: str, kind: str, **kw) -> MediaInfo:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return MediaInfo(path=path, kind=kind, ext=ext, size=size, **kw)


class FilesCase(unittest.TestCase):
    """A FakeHost with a project at `<scratch>/projects/Sample` and a downloads folder outside it."""

    manual_jobs = False
    SYNC_DELAY = 0.01

    def setUp(self) -> None:
        self._old_home = os.environ.get("PREMIERE_DECK_HOME")
        self.scratch = scratch("files-")
        os.environ["PREMIERE_DECK_HOME"] = self.scratch
        self.host = FakeHost(manual_jobs=self.manual_jobs, module_id="files")
        self.host.log.setLevel(logging.ERROR)  # expected warnings of failure-path tests stay out of the output
        self.project = self.host.make_project(name="Sample")
        self.root = self.project.root
        self.downloads = os.path.join(self.scratch, "Downloads")
        os.makedirs(self.downloads, exist_ok=True)
        self.pr = self.host.premiere
        self.module = None
        self._node = 0

    def tearDown(self) -> None:
        if self.module is not None:
            self.module.stop()
        if self._old_home is None:
            os.environ.pop("PREMIERE_DECK_HOME", None)
        else:
            os.environ["PREMIERE_DECK_HOME"] = self._old_home
        remove_tree(self.scratch)

    # helpers
    def make_module(self, cls=None, *, enabled: bool = False, **project_settings):
        from premiere_deck.modules.files.module import FilesModule

        cls = cls or FilesModule
        self.host.settings.project.values.update(project_settings)
        self.module = cls(self.host.context())
        self.module.SYNC_DELAY = self.SYNC_DELAY  # short quiet window; the coalescing itself is tested in test_modules_files_events
        self.module._classifier = fake_classifier()  # no ffmpeg decode, no model: audio_class stays None
        self.module.start()
        self.module.project_opened(self.project)
        if enabled:
            self.module.on_action("files.setup")
            self.assertEqual(self.host.ui.results[-1][:2], ("setup", "ok"), self.host.ui.results[-1])
        return self.module

    def press(self, action: str, value=None, row: Optional[str] = None) -> tuple:
        """Press a control and return its one result (button, outcome, text, detail); no toast for a press."""
        before_results, before_toasts = len(self.host.ui.results), len(self.host.ui.toasts)
        self.module.on_action(action, value, row)
        results = self.host.ui.results[before_results:]
        self.assertEqual(len(results), 1, f"{action}: {results}")
        self.assertEqual(self.host.ui.toasts[before_toasts:], [], f"{action} toasted")
        return results[0]

    def fire(self, name: str = "project.changed") -> None:
        """Deliver a Premiere event and wait for the coalesced sync it arms to finish."""
        self.module.on_event(name, {})
        self.settle()

    def settle(self, timeout: float = 5.0) -> None:
        """Wait until no coalesced sync is pending; the sync runs under the module lock, so taking it waits for a
        sync that has started."""
        def idle() -> bool:
            with self.module._lock:
                state = self.module._state
                return state is None or state.sync_timer is None

        self.assertTrue(wait_for(idle, timeout, 0.005), "the coalesced sync did not run")

    def user_import(self, path: str, tree_path: str = "", *, offline: bool = False,
                    name: Optional[str] = None, fire: bool = True) -> ProjectItem:
        self._node += 1
        item = ProjectItem(node_id=f"u{self._node}", name=name or os.path.basename(path), kind="clip",
                           tree_path=tree_path, media_path=path, is_offline=offline)
        self.pr.set_items(list(self.pr.snapshot_value.items) + [item])
        if fire:
            self.fire("project.items_added")
        return item

    def user_move_bin(self, node_id: str, tree_path: str) -> None:
        self.pr._replace(node_id, tree_path=tree_path)
        self.fire("project.changed")

    def item(self, node_id: str) -> ProjectItem:
        return self.pr.snapshot_value.by_node_id()[node_id]

    def ext(self, name: str, size: int = 4096, seed: str = "") -> str:
        return write_file(os.path.join(self.downloads, name), size, seed)

    def in_root(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def journal(self):
        return self.module._state.journal.entries()

    def toasts(self) -> list[str]:
        return [text for _, text in self.host.ui.toasts]
