"""Frame extraction for the index (docs/AI_SEARCH.md 1-2) and single-frame thumbnails (section 3).

One ffmpeg process per clip decodes the video, picks a frame every `step` seconds plus every scene cut
(`select` on `scene` > 0.3), scales the shorter side to 224 px, centre-crops 224 x 224 and writes raw RGB24 to
stdout; `showinfo` on stderr gives each picked frame's time. Timestamps start at 0 at the first frame (ffmpeg
shifts the input start time away), which is the clip time Premiere uses for source in points.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

import numpy as np

from premiere_deck.core import procs

SIZE = 224
SCENE_THRESHOLD = 0.3
_PTS = re.compile(r"\bn:\s*\d+\b.*?\bpts_time:\s*(-?[0-9.]+(?:e[-+]?\d+)?)")


class FrameError(Exception):
    """ffmpeg could not read the file; the message is ffmpeg's last error line."""


@dataclass(frozen=True)
class FramePlan:
    step: float
    max_frames: int
    image: bool = False


def plan_for(duration: Optional[float], step: float, max_frames: int, image: bool = False) -> FramePlan:
    """Widen the step for long clips so `max_frames` covers the whole clip instead of only its start."""
    step = max(0.1, float(step))
    max_frames = max(1, int(max_frames))
    if image:
        return FramePlan(step, 1, True)
    if duration and duration / step > max_frames * 0.8:
        step = duration / (max_frames * 0.8)  # leave a fifth of the cap for scene cuts
    return FramePlan(step, max_frames, False)


def ffmpeg_args(ffmpeg: str, path: str, plan: FramePlan) -> list[str]:
    return [ffmpeg, *frame_arguments(path, plan)]


def frame_arguments(path: str, plan: FramePlan) -> list[str]:
    """The ffmpeg arguments without the executable (what `Tools.ffmpeg_pipe` takes)."""
    crop = f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=increase:flags=bicubic,crop={SIZE}:{SIZE}"
    if plan.image:
        vf = f"{crop},showinfo"
    else:
        # Scale first: the scene score on 224 px frames is as useful and far cheaper than on 4K frames.
        pick = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{plan.step:.3f})"
                f"+gt(scene\\,{SCENE_THRESHOLD})'")
        vf = f"{crop},{pick},showinfo"
    return ["-hide_banner", "-nostats", "-nostdin", "-loglevel", "info", "-i", path, "-map", "0:v:0",
            "-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "passthrough", "-frames:v", str(plan.max_frames),
            "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]


class _Times:
    """Collects `pts_time` values from the stderr pump; readers wait for the n-th value."""

    def __init__(self) -> None:
        self.values: list[float] = []
        self.tail: list[str] = []
        self.closed = False
        self.cond = threading.Condition()

    def on_line(self, line: str) -> None:
        match = _PTS.search(line) if "Parsed_showinfo" in line else None
        with self.cond:
            if match:
                self.values.append(max(0.0, float(match.group(1))))
                self.cond.notify_all()
            elif not line.startswith("[Parsed_showinfo"):
                self.tail = (self.tail + [line])[-20:]

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def get(self, index: int, fallback: float) -> float:
        with self.cond:
            while len(self.values) <= index and not self.closed:
                self.cond.wait(0.5)
            return self.values[index] if index < len(self.values) else fallback


def iter_frames(ffmpeg: str, path: str, plan: FramePlan, *, cancelled: Optional[Callable[[], bool]] = None,
                pipe: Optional[Callable[[Sequence[str]], subprocess.Popen]] = None) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (seconds, uint8 [224, 224, 3]) in time order. Raises FrameError when ffmpeg fails before the first
    frame; a cancel stops the process and ends the iteration. `pipe` is `Tools.ffmpeg_pipe` (the host starts
    ffmpeg and owns its Job Object); without it the process is started here with `ffmpeg` as the executable."""
    frame_bytes = SIZE * SIZE * 3
    times = _Times()
    job = None if pipe is not None else procs.JobObject()
    try:
        if pipe is not None:
            proc = pipe(frame_arguments(path, plan))
        else:
            proc = procs.spawn(ffmpeg_args(ffmpeg, path, plan), job=job)
    except OSError as exc:
        if job is not None:
            job.close()
        raise FrameError(f"ffmpeg cannot start: {exc}") from exc

    def pump() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                times.on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            times.close()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    count = 0
    try:
        while True:
            if cancelled is not None and cancelled():
                break
            data = _read_exact(proc.stdout, frame_bytes)
            if len(data) < frame_bytes:
                break
            frame = np.frombuffer(data, dtype=np.uint8).reshape(SIZE, SIZE, 3)
            yield times.get(count, count * plan.step), frame
            count += 1
    finally:
        if proc.poll() is None:
            if job is not None:
                job.kill()
            else:
                proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=5)
        if job is not None:
            job.close()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
    if count == 0 and not (cancelled is not None and cancelled()) and proc.returncode not in (0, None):
        raise FrameError(times.tail[-1] if times.tail else f"ffmpeg exited with code {proc.returncode}")


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def thumbnail_jpeg(ffmpeg: str, path: str, seconds: float, width: int = 160, *, image: bool = False,
                   timeout: float = 20.0) -> bytes:
    """One JPEG frame at `seconds` (input seek, fast), `width` px wide. Raises FrameError."""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if not image and seconds > 0:
        args += ["-ss", f"{seconds:.3f}"]
    args += ["-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={width}:-2:flags=bicubic",
             "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    with procs.JobObject() as job:
        try:
            proc = procs.spawn(args, job=job)
        except OSError as exc:
            raise FrameError(f"ffmpeg cannot start: {exc}") from exc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            job.kill()
            proc.communicate()
            raise FrameError("ffmpeg timed out") from exc
    if proc.returncode != 0 or not out:
        lines = err.decode("utf-8", errors="replace").strip().splitlines()
        raise FrameError(lines[-1] if lines else "no frame at that time")
    return out
