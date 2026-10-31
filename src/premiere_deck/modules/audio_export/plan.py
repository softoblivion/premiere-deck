"""Pure rules of the audio export (docs AUDIO_EXPORT.md 2-4): formats, range, output folder, file names, passes,
timeouts, the silence check of a written file and the user sentence of a script error. No Premiere, no I/O except
reading the layout file and listing the output folder."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from premiere_deck.premiere.model import AudioTrack, SequenceInfo

MODE_SEPARATE = "separate"
MODE_MIX = "mix"
MODES = ((MODE_SEPARATE, "Separate files"), (MODE_MIX, "One file"))
MODE_BADGES = ((MODE_SEPARATE, "SEP"), (MODE_MIX, "MIX"))

RANGE_INOUT = "inout"
RANGE_ENTIRE = "entire"
RANGES = ((RANGE_INOUT, "In to Out"), (RANGE_ENTIRE, "Entire sequence"))
RANGE_BADGES = ((RANGE_INOUT, "I/O"), (RANGE_ENTIRE, "ALL"))
# Sequence.exportAsMediaDirect workAreaType (app.encoder.ENCODE_ENTIRE / ENCODE_IN_TO_OUT).
WORK_AREA_ENTIRE = 0
WORK_AREA_IN_TO_OUT = 1

DEFAULT_EXPORTS_DIR = "Exports"
AUDIO_DIR = "Audio"
TEMP_PREFIX = "deckaudio"
STALE_TEMP_SECONDS = 600.0  # an own temporary output older than this is a leftover of an earlier export
SILENT_DB = -60.0  # a file whose loudest sample is at or below this is silent

USER_ERROR_PREFIX = "deck: "
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEFAULT_TRACK_NAME = re.compile(r"^audio\s*\d+$", re.I)
_MAX_VOLUME = re.compile(r"max_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", re.I)


@dataclass(frozen=True)
class AudioFormat:
    """An audio-only system preset of Premiere (`MediaIO\\systempresets\\<folder>\\<file>`)."""

    id: str
    label: str
    badge: str
    folder: str
    file: str
    ext: str


FORMATS = (
    AudioFormat("wav", "WAV 48 kHz 16-bit", "WAV", "3F3F3F3F_57415645", "Waveform Audio 48kHz 16-bit.epr", "wav"),
    AudioFormat("mp3", "MP3 256 kbps", "MP3", "3F3F3F3F_4D503320", "MP3 256kbps High Quality.epr", "mp3"),
    AudioFormat("aiff", "AIFF 48 kHz", "AIFF", "3F3F3F3F_41494646", "AIFF 48kHz.epr", "aif"),
)
FORMAT_BY_ID = {fmt.id: fmt for fmt in FORMATS}


@dataclass(frozen=True)
class Pass:
    """One output file: the tracks heard in it (indexes) and its final file name."""

    tracks: tuple[int, ...]
    name: str
    label: str  # "A2" or "Mix" in progress lines and results


def safe_stem(name: str, fallback: str = "Sequence") -> str:
    """A Windows file name stem: invalid characters become `_`, no trailing dots or spaces."""
    stem = _INVALID_NAME.sub("_", name).strip().rstrip(". ")
    return stem or fallback


def track_label(track: AudioTrack) -> str:
    """"A1" for a track with Premiere's default name ("Audio 1"), else "A1 Dialogue"."""
    name = track.name.strip()
    if not name or _DEFAULT_TRACK_NAME.match(name) or name.upper() == track.code:
        return track.code
    return f"{track.code} {name}"


def default_choice(tracks: Sequence[AudioTrack]) -> set[int]:
    """The tracks chosen before the user picks: those with clips that are not muted in Premiere."""
    return {track.index for track in tracks if track.clip_count and not track.muted}


def work_area(sequence: SequenceInfo, range_id: str) -> int:
    """In to Out when asked and the sequence has both marks, else the entire sequence."""
    if range_id == RANGE_INOUT and has_in_out(sequence):
        return WORK_AREA_IN_TO_OUT
    return WORK_AREA_ENTIRE


def has_in_out(sequence: SequenceInfo) -> bool:
    return (sequence.in_point is not None and sequence.out_point is not None
            and sequence.out_point > sequence.in_point)


def range_seconds(sequence: SequenceInfo, area: int) -> float:
    if area == WORK_AREA_IN_TO_OUT and has_in_out(sequence):
        return float(sequence.out_point - sequence.in_point)  # type: ignore[operator]
    return max(0.0, float(sequence.duration))


def timeout_ms(seconds: float) -> int:
    """Per file: 2 minutes plus 1.5 x the range (audio renders faster than real time), at most 4 hours."""
    return int(min(4 * 3600, 120 + 1.5 * max(0.0, seconds)) * 1000)


def passes(sequence: SequenceInfo, tracks: Sequence[AudioTrack], chosen: Iterable[int], mode: str,
           fmt: AudioFormat) -> list[Pass]:
    """The output files in track order: one per chosen track, or one mix of all chosen tracks."""
    by_index = {track.index: track for track in tracks}
    picked = [by_index[i] for i in sorted(set(chosen)) if i in by_index]
    stem = safe_stem(sequence.name)
    if not picked:
        return []
    if mode == MODE_MIX:
        codes = "-".join(track.code for track in picked)
        return [Pass(tuple(t.index for t in picked), f"{stem} - Mix {codes}.{fmt.ext}", "Mix")]
    return [Pass((track.index,), f"{stem} - {safe_stem(track_label(track), track.code)}.{fmt.ext}", track.code)
            for track in picked]


def exports_dir(root: Path) -> str:
    """The layout's exports folder (`exports_dir` of `<root>\\.premiere-deck\\layout.json`, default "Exports"); a
    value that is absolute, empty or leaves the root falls back to the default."""
    try:
        layout = json.loads((root / ".premiere-deck" / "layout.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_EXPORTS_DIR
    value = layout.get("exports_dir") if isinstance(layout, dict) else None
    if not isinstance(value, str):
        return DEFAULT_EXPORTS_DIR
    parts = [part.strip() for part in value.replace("\\", "/").split("/") if part.strip()]
    if not parts or any(part in (".", "..") or ":" in part for part in parts) or value.strip().startswith(("/", "\\")):
        return DEFAULT_EXPORTS_DIR
    return "/".join(parts)


def audio_folder(root: str | os.PathLike) -> Path:
    """`<root>\\<exports_dir>\\Audio`, always inside the root."""
    base = Path(root)
    folder = base.joinpath(*exports_dir(base).split("/"), AUDIO_DIR)
    if os.path.commonpath([os.path.abspath(folder), os.path.abspath(base)]) != os.path.abspath(base):
        return base / DEFAULT_EXPORTS_DIR / AUDIO_DIR
    return folder


def free_path(folder: Path, name: str) -> Path:
    """`name` in `folder`, or "<stem> (2).<ext>", "(3)", ... when it exists: nothing is overwritten."""
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def temp_path(folder: Path, token: str, fmt: AudioFormat) -> Path:
    """The temporary output of one pass: dot-free stem in the output folder, renamed when the file is complete."""
    return folder / f"{TEMP_PREFIX}{re.sub(r'[^A-Za-z0-9]', '', token)}.{fmt.ext}"


def is_own_temp(path: Path) -> bool:
    """True for a temporary output of this module: "deckaudio<digits>.<ext>" with one of the module's extensions."""
    exts = {fmt.ext for fmt in FORMATS}
    return bool(re.fullmatch(rf"{TEMP_PREFIX}\d+", path.stem)) and path.suffix.lstrip(".").lower() in exts


def stale_temps(folder: Path, now: float, max_age: float = STALE_TEMP_SECONDS) -> list[Path]:
    """The module's own temporary outputs in `folder` last written more than `max_age` seconds ago: leftovers of
    a pass whose file Premiere still held open when the pass ended (a timeout). Never a file of another name."""
    found = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            if is_own_temp(path) and path.is_file() and now - path.stat().st_mtime > max_age:
                found.append(path)
        except OSError:
            continue
    return sorted(found)


def max_volume(lines: Iterable[str]) -> Optional[float]:
    """The `max_volume` ffmpeg's volumedetect printed (dB; -inf for digital silence), or None when absent."""
    for line in lines:
        match = _MAX_VOLUME.search(line)
        if match:
            raw = match.group(1).lower()
            return float("-inf") if raw.endswith("inf") else float(raw)
    return None


def is_silent(volume: Optional[float]) -> bool:
    return volume is not None and volume <= SILENT_DB


def silence_args(path: str) -> list[str]:
    """ffmpeg arguments that print the loudest sample of a file and write nothing."""
    return ["-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]


def error_sentence(message: str) -> str:
    """The user sentence of a script error: a `deck:` sentence, the dialog case, or a generic one."""
    text = str(message or "")
    if USER_ERROR_PREFIX in text:
        sentence = text.split(USER_ERROR_PREFIX, 1)[1].strip()
        return sentence if sentence.endswith(".") else sentence + "."
    if text.strip().lower() == "timeout":
        return "Premiere is waiting for a dialog."
    return "Premiere reported a script error."


def plural(count: int, word: str, many: Optional[str] = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"
