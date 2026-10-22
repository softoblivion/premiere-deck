"""Media probing: ffprobe JSON -> `MediaInfo` (core/media.py, docs/FILES.md 4) and the quick content hash.

Alpha is detected from what ffprobe reports:
- a pixel format with an alpha plane (`yuva420p`, `rgba`, `argb`, `bgra`, `abgr`, `ya8`, `gbrap`, `ayuv`, ...);
- ProRes 4444 (`profile` contains "4444") when ffprobe gives no pixel format, or one with alpha (ffmpeg's ProRes
  decoder reports `yuva444p*` exactly when the file carries an alpha channel);
- VP8 / VP9 / AV1 in WebM / Matroska with the stream tag `alpha_mode` = 1 (libvpx stores alpha as side data, so the
  pixel format alone says `yuv420p`).
A probe failure classifies by extension only and keeps the error text in `probe_error`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import PurePath
from typing import Any, Optional

from premiere_deck.core.media import KIND_AUDIO, KIND_IMAGE, KIND_VIDEO, MediaInfo, kind_from_extension

from . import procs

log = logging.getLogger("premiere_deck.core.probe")

PROBE_TIMEOUT = 30.0
HASH_BLOCK = 4 * 1024 * 1024
_ALPHA_PIX = re.compile(r"^(yuva|rgba|bgra|argb|abgr|ya\d|gbrap|ayuv|vuya|rgbaf|bgra64|rgba64)", re.I)
_IMAGE_CODECS = {"png", "mjpeg", "jpegls", "tiff", "bmp", "webp", "gif", "exr", "psd", "targa", "dpx", "jpeg2000",
                 "qoi", "pgm", "ppm", "pam", "sgi", "hdr"}
_IMAGE_FORMATS = ("image2", "_pipe", "gif", "webp_pipe")


def has_alpha_pix_fmt(pix_fmt: Optional[str]) -> bool:
    return bool(pix_fmt) and bool(_ALPHA_PIX.match(str(pix_fmt)))


def stream_alpha(stream: dict) -> bool:
    """Alpha of one video stream per the rules of the module docstring."""
    pix_fmt = stream.get("pix_fmt")
    if has_alpha_pix_fmt(pix_fmt):
        return True
    codec = str(stream.get("codec_name") or "").lower()
    profile = str(stream.get("profile") or "")
    if codec == "prores" and "4444" in profile and not pix_fmt:
        return True
    tags = {str(k).lower(): str(v) for k, v in (stream.get("tags") or {}).items()}
    if codec in ("vp8", "vp9", "av1") and tags.get("alpha_mode", "").strip() == "1":
        return True
    return False


def _rate(value: Any) -> Optional[float]:
    text = str(value or "")
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        return round(n / d, 3) if d and n else None
    try:
        rate = float(text)
    except ValueError:
        return None
    return rate or None


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_probe(path: str, data: dict, size: Optional[int] = None) -> MediaInfo:
    """`MediaInfo` from ffprobe's `-show_format -show_streams` JSON (pure; tested without ffprobe)."""
    ext = PurePath(path).suffix.lower().lstrip(".")
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    fmt = data.get("format") or {}
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    first_video = video[0] if video else None
    first_audio = audio[0] if audio else None
    duration = _float(fmt.get("duration"))
    if duration is None:
        for stream in video + audio:
            duration = _float(stream.get("duration"))
            if duration is not None:
                break

    by_ext = kind_from_extension(path)
    format_name = str(fmt.get("format_name") or "")
    is_image = False
    if first_video is not None:
        codec = str(first_video.get("codec_name") or "").lower()
        still_format = any(part in format_name for part in _IMAGE_FORMATS)
        if by_ext == KIND_IMAGE or (codec in _IMAGE_CODECS and still_format and not audio):
            is_image = True
    if is_image:
        kind = KIND_IMAGE
    elif first_video is not None:
        kind = KIND_VIDEO
    elif first_audio is not None:
        kind = KIND_AUDIO
    else:
        kind = by_ext

    extra: dict = {"format_name": format_name or None}
    if first_video is not None:
        extra.update(pix_fmt=first_video.get("pix_fmt"), profile=first_video.get("profile"))
    if first_audio is not None:
        extra.update(sample_rate=first_audio.get("sample_rate"), channels=first_audio.get("channels"))
    bit_rate = fmt.get("bit_rate")
    if bit_rate:
        extra["bit_rate"] = bit_rate

    return MediaInfo(
        path=path,
        kind=kind,
        ext=ext,
        size=int(size or 0),
        duration=None if kind == KIND_IMAGE else duration,
        width=int(first_video["width"]) if first_video and first_video.get("width") else None,
        height=int(first_video["height"]) if first_video and first_video.get("height") else None,
        fps=None if kind == KIND_IMAGE or first_video is None else
        (_rate(first_video.get("avg_frame_rate")) or _rate(first_video.get("r_frame_rate"))),
        has_video=first_video is not None,
        has_audio=first_audio is not None,
        alpha=any(stream_alpha(s) for s in video),
        codec_video=first_video.get("codec_name") if first_video else None,
        codec_audio=first_audio.get("codec_name") if first_audio else None,
        extra=extra,
    )


def _fallback(path: str, error: str) -> MediaInfo:
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return MediaInfo(path=path, kind=kind_from_extension(path), ext=PurePath(path).suffix.lower().lstrip("."),
                     size=size, probe_error=error)


def probe(path: str, ffprobe: Optional[str] = None, timeout: float = PROBE_TIMEOUT) -> MediaInfo:
    """Probe one file. Never raises for a bad file: the result carries `probe_error`."""
    path = str(path)
    if not os.path.isfile(path):
        return _fallback(path, "file not found")
    exe = ffprobe or shutil.which("ffprobe")
    if not exe:
        return _fallback(path, "ffprobe is not available")
    res = procs.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                    capture=True, timeout=timeout)
    if not res.ok:
        error = res.last_error_line() or ("timeout" if res.timed_out else f"ffprobe exit {res.returncode}")
        log.info("probe failed for %s: %s", path, error)
        return _fallback(path, error)
    try:
        data = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        return _fallback(path, f"ffprobe output is not JSON: {exc}")
    return parse_probe(path, data)


def quick_hash(path: str) -> str:
    """Hex BLAKE2b (16 bytes) of the file size (8 bytes, little endian), the first 4 MB and the last 4 MB.

    Fast identity of media files for duplicate and collision checks (docs/FILES.md); a file of 8 MB or less is
    hashed whole.
    """
    size = os.path.getsize(path)
    digest = hashlib.blake2b(digest_size=16)
    digest.update(int(size).to_bytes(8, "little"))
    with open(path, "rb") as handle:
        if size <= 2 * HASH_BLOCK:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(HASH_BLOCK))
            handle.seek(size - HASH_BLOCK)
            digest.update(handle.read(HASH_BLOCK))
    return digest.hexdigest()
