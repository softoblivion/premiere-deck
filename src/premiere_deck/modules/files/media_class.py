"""The Files classifier (docs/FILES.md 3.8): `MediaInfo.source` for video, `MediaInfo.audio_class` for audio.

Video source: no pixel model, container / stream tags, names and stream facts only (`video_source`, pure).
Audio class: YAMNet (AudioSet, Apache-2.0) as an ONNX model on the CPU through `onnxruntime`, run on the first 20 s
of the file decoded by ffmpeg to 16 kHz mono float32 (`AudioModel`, `decode_audio`). The classifier only reports;
the layout decides what a class means (docs/FILES.md 2).

`MediaClassifier.enrich(info)` returns `dataclasses.replace(info, source=..., audio_class=...)` and caches the
result per (path, size, mtime) for the host run. Any audio failure leaves `audio_class` None and is logged once per
file.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import threading
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Callable, Optional, Sequence

from premiere_deck.core.media import KIND_AUDIO, KIND_VIDEO, MediaInfo

log = logging.getLogger("module.files.media_class")

MODELS_DIR = Path(__file__).with_name("models")
MODEL_FILE = MODELS_DIR / "yamnet.onnx"
CLASS_MAP_FILE = MODELS_DIR / "yamnet_class_map.csv"

SAMPLE_RATE = 16000
DECODE_SECONDS = 20
DECODE_TIMEOUT = 60.0
GROUP_MIN = 0.2  # the winning group must reach this summed mean score (docs/FILES.md 3.8)

# AudioSet ontology groups by MID, as contiguous rows of the class map: "Speech" and its children (Child speech,
# Conversation, Narration, Babbling, Speech synthesizer); "Music" with its instruments, genres, concepts, roles and
# moods (rows "Music" .. "Scary music").
SPEECH_RANGE = ("/m/09x0r", "/m/0brhx")
MUSIC_RANGE = ("/m/04rlf", "/t/dd00037")

# --- video source --------------------------------------------------------------------------------------------------

CAMERA_TAG_KEYS = ("make", "model", "com.apple.quicktime.make", "com.apple.quicktime.model",
                   "com.android.manufacturer")
CAMERA_VENDORS = ("sony", "canon", "panasonic", "nikon", "fujifilm", "blackmagic", "red", "arri", "dji", "gopro",
                  "apple", "samsung", "google")
# `major_brand` values that only cameras write: Sony XAVC (XAVC S / HS / XAVC Intra in MP4) and Canon MP4 (CAEP).
CAMERA_BRANDS = ("xavc", "caep")
GENERATOR_TOKENS = ("runway", "kling", "veo", "sora", "pika", "luma", "hailuo", "minimax", "wan",
                    "midjourney", "generated")
STOCK_TOKENS = ("pexels", "pixabay", "shutterstock", "istock", "adobestock", "artgrid", "envato", "storyblocks",
                "videvo", "videezy", "mixkit", "coverr")
CAMERA_EXTENSIONS = ("mxf", "braw", "r3d", "mts", "m2ts")
GENERATOR_SIZES = {(1280, 720), (1024, 576), (1344, 768), (848, 480), (1920, 1080)}
GENERATOR_SIZES |= {(h, w) for w, h in GENERATOR_SIZES}  # vertical outputs of the same generators
GENERATOR_MIN_S, GENERATOR_MAX_S = 4.0, 15.0


def _token_regex(tokens: Sequence[str]) -> re.Pattern:
    body = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(?:" + body + r")(?![a-z])", re.IGNORECASE)


_GENERATOR = re.compile(_token_regex(GENERATOR_TOKENS).pattern + r"|(?<![a-z])gen[-_ ]?[34](?![a-z0-9])",
                        re.IGNORECASE)
_STOCK = _token_regex(STOCK_TOKENS)
_VENDOR = _token_regex(CAMERA_VENDORS)
# Camera file names, optionally after a date prefix an import tool adds ("20260910_C1195"): Sony C0001, DSC_, IMG_,
# MVI_ / MVI, DJI_, GoPro GX01 / GH01, the cinema-camera reel pattern A001_C001.
_DATE_PREFIX = r"(?:(?:19|20)\d{2}-?\d{2}-?\d{2}[_ -])?"
_CAMERA_NAME = re.compile(
    r"^" + _DATE_PREFIX + r"(?:C\d{4}(?!\d)|DSC_|IMG_|MVI_?\d|DJI_|GX01|GH01|[A-Z]\d{3}_?[A-Z]\d{3})", re.IGNORECASE)
_NUMERIC_ID = re.compile(r"^(\d{5,})(?:[-_].*)?$")
_DATE = re.compile(r"^(?:19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")


def camera_tag(tags: dict) -> bool:
    """A camera make / model tag, a camera brand, or an encoder / handler tag naming a camera vendor."""
    for key in CAMERA_TAG_KEYS:
        if str(tags.get(key) or "").strip():
            return True
    if str(tags.get("major_brand") or "").strip().casefold() in CAMERA_BRANDS:
        return True
    for key in ("encoder", "handler_name"):
        if _VENDOR.search(str(tags.get(key) or "")):
            return True
    return False


def _numeric_id(stem: str) -> bool:
    match = _NUMERIC_ID.match(stem)
    if not match:
        return False
    return not _DATE.match(match.group(1))  # "20240101_123456" is a phone's date name, not a stock id


def _is_24(fps: Optional[float]) -> bool:
    return fps is not None and abs(float(fps) - 24.0) < 0.0005


def video_source(info: MediaInfo) -> str:
    """`camera`, `stock`, `generated` or `unknown` for a video file (docs/FILES.md 3.8, first match wins)."""
    stem = PurePath(info.path).stem
    tags = {str(k).lower(): str(v) for k, v in (info.tags or {}).items()}
    cam_tag = camera_tag(tags)
    if _GENERATOR.search(stem):
        return "generated"
    if _STOCK.search(stem):
        return "stock"
    if cam_tag:
        return "camera"
    ext = (info.ext or PurePath(info.path).suffix.lstrip(".")).lower()
    if _CAMERA_NAME.match(stem) or ext in CAMERA_EXTENSIONS:
        return "camera"
    if (info.has_audio is False and info.nb_streams in (None, 1)
            and info.duration is not None and GENERATOR_MIN_S <= info.duration <= GENERATOR_MAX_S
            and _is_24(info.fps) and (info.width, info.height) in GENERATOR_SIZES):
        return "generated"
    encoder = tags.get("encoder", "")
    # Lavf = the ffmpeg muxer's container tag; the first video stream's "Lavc<version> <codec>" tag (ffmpeg's
    # encoder) wins over it in the merged tags, so both name ffmpeg as the last writer.
    if _numeric_id(stem) or encoder.startswith(("Lavf", "Lavc")) or "handbrake" in encoder.casefold():
        return "stock"
    return "unknown"


# --- audio class ---------------------------------------------------------------------------------------------------


class ClassifierError(RuntimeError):
    """The audio class could not be computed (no ffmpeg, a decode error, a model error)."""


def decode_args(path: str) -> list[str]:
    """ffmpeg arguments (without the executable) that write the first 20 s of the first audio stream as 16 kHz
    mono float32 little-endian samples to stdout."""
    return ["-nostdin", "-v", "error", "-t", str(DECODE_SECONDS), "-i", str(path), "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"]


def decode_audio(path: str, pipe: Callable[[Sequence[str]], subprocess.Popen], timeout: float = DECODE_TIMEOUT):
    """The decoded samples as a float32 numpy array (through `ctx.tools.ffmpeg_pipe`)."""
    import numpy as np

    try:
        proc = pipe(decode_args(path))
    except FileNotFoundError as exc:
        raise ClassifierError("ffmpeg is not available") from exc
    except OSError as exc:
        raise ClassifierError(f"ffmpeg could not start: {exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise ClassifierError("decode timed out") from exc
    if proc.returncode != 0:
        line = (err or b"").decode("utf-8", errors="replace").strip().splitlines()
        raise ClassifierError(line[-1] if line else f"ffmpeg exit {proc.returncode}")
    usable = len(out or b"") // 4 * 4
    if usable == 0:
        raise ClassifierError("no audio samples")
    return np.frombuffer(out[:usable], dtype="<f4").astype(np.float32)


def decide(speech: float, music: float) -> str:
    """`speech` when the speech group is the larger one and at least 0.2, `music` when the music group is, else
    `other`."""
    if speech >= music and speech >= GROUP_MIN:
        return "speech"
    if music > speech and music >= GROUP_MIN:
        return "music"
    return "other"


def load_class_map(path: Path = CLASS_MAP_FILE) -> list[tuple[str, str]]:
    """(mid, display name) per class index."""
    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    return [(row[1], row[2]) for row in rows[1:] if len(row) >= 3]


def _range(classes: list[tuple[str, str]], first: str, last: str) -> list[int]:
    mids = [mid for mid, _ in classes]
    try:
        a, b = mids.index(first), mids.index(last)
    except ValueError as exc:
        raise ClassifierError(f"class map lacks {exc}") from exc
    return list(range(a, b + 1))


class AudioModel:
    """YAMNet in `onnxruntime` on the CPU execution provider; the session is created on first use and kept."""

    def __init__(self, model: Path = MODEL_FILE, class_map: Path = CLASS_MAP_FILE, threads: int = 2) -> None:
        self.model_path = Path(model)
        self.class_map_path = Path(class_map)
        self.threads = threads
        self._session = None
        self._lock = threading.Lock()
        self.speech_idx: list[int] = []
        self.music_idx: list[int] = []

    def available(self) -> bool:
        return self.model_path.is_file() and self.class_map_path.is_file()

    def _load(self):
        if self._session is not None:
            return self._session
        if not self.available():
            raise ClassifierError("the audio model is missing")
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise ClassifierError("onnxruntime is not installed") from exc
        classes = load_class_map(self.class_map_path)
        self.speech_idx = _range(classes, *SPEECH_RANGE)
        self.music_idx = _range(classes, *MUSIC_RANGE)
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.intra_op_num_threads = max(1, int(self.threads))
        options.inter_op_num_threads = 1
        try:
            self._session = ort.InferenceSession(str(self.model_path), options, providers=["CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own exception types
            raise ClassifierError(f"the audio model could not be loaded: {exc}") from exc
        return self._session

    def scores(self, waveform) -> tuple[float, float]:
        """(speech group, music group): the per-frame scores averaged over the frames, summed per group."""
        with self._lock:
            session = self._load()
            name = session.get_inputs()[0].name
            try:
                frames = session.run(["output_0"], {name: waveform})[0]
            except Exception as exc:  # noqa: BLE001
                raise ClassifierError(f"the audio model failed: {exc}") from exc
        mean = frames.mean(axis=0)
        return float(mean[self.speech_idx].sum()), float(mean[self.music_idx].sum())


Scorer = Callable[[object], tuple[float, float]]
_shared_model: Optional[AudioModel] = None
_shared_lock = threading.Lock()


def shared_model() -> AudioModel:
    """One model session per host run."""
    global _shared_model
    with _shared_lock:
        if _shared_model is None:
            _shared_model = AudioModel()
        return _shared_model


class MediaClassifier:
    """Per-module classifier with the (path, size, mtime) cache of docs/FILES.md 3.8.

    `pipe` is `ctx.tools.ffmpeg_pipe`; `scorer` maps a waveform to (speech, music) group scores (the bundled model
    unless a test passes a fake).
    """

    def __init__(self, pipe: Callable[[Sequence[str]], subprocess.Popen], scorer: Optional[Scorer] = None) -> None:
        self.pipe = pipe
        self._scorer = scorer
        self._cache: dict[tuple, tuple[Optional[str], Optional[str]]] = {}
        self._logged: set[tuple] = set()
        self._lock = threading.Lock()

    @property
    def scorer(self) -> Scorer:
        return self._scorer or shared_model().scores

    @staticmethod
    def _key(path: str) -> tuple:
        try:
            st = os.stat(path)
            return (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
        except OSError:
            return (os.path.normcase(os.path.abspath(path)), None, None)

    def audio_class(self, path: str) -> Optional[str]:
        try:
            speech, music = self.scorer(decode_audio(path, self.pipe))
        except Exception as exc:  # noqa: BLE001 - any failure leaves the class unknown (docs/FILES.md 3.8)
            key = self._key(path)
            if key not in self._logged:
                self._logged.add(key)
                log.info("audio class of %s unknown: %s", path, exc)
            return None
        return decide(speech, music)

    def enrich(self, info: MediaInfo) -> MediaInfo:
        """`info` with `source` (video) or `audio_class` (audio) filled; other kinds are returned unchanged."""
        if info.kind not in (KIND_VIDEO, KIND_AUDIO):
            return info
        key = self._key(info.path)
        with self._lock:
            cached = self._cache.get(key)
        if info.probe_error:
            # a probe failure classifies by extension only: source unknown, no audio class (docs/FILES.md 2)
            return replace(info, source="unknown") if info.kind == KIND_VIDEO else replace(info, audio_class=None)
        if cached is None:
            if info.kind == KIND_VIDEO:
                cached = (video_source(info), None)
            else:
                cached = (None, self.audio_class(info.path))
            with self._lock:
                self._cache[key] = cached
        source, audio_class = cached
        if info.kind == KIND_VIDEO:
            return replace(info, source=source)
        return replace(info, audio_class=audio_class)


__all__ = [name for name in globals() if not name.startswith("_")]
