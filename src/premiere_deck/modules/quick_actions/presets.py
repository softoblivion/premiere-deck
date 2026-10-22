"""Export presets of `export_sequence` (docs/QUICK_ACTIONS.md 2, presets/PRESETS.md).

Each built-in preset resolves to the first file that exists: the `.epr` shipped in `presets/`, then Adobe's own
system preset of the installed Premiere Pro or Media Encoder (`MediaIO/systempresets/<folder>/<file>.epr`, read
only). A user's own `.epr` files in `<data>/presets/` follow as extra options. A built-in preset with no file is
left out of the select.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

SHIPPED_DIR = Path(__file__).parent / "presets"
USER_PREFIX = "user:"

_H264 = "4E49434B_48323634"  # Adobe's system preset folder of the H.264 exporter


@dataclass(frozen=True)
class BuiltinPreset:
    id: str
    label: str
    shipped: str  # file name in presets/
    system: tuple[str, ...]  # fallbacks relative to a systempresets folder, first existing wins


BUILTIN: tuple[BuiltinPreset, ...] = (
    BuiltinPreset("youtube_1080p", "YouTube 1080p", "YouTube 1080p.epr", (f"{_H264}/YouTube 1080p HD.epr",)),
    BuiltinPreset("instagram_9x16", "Instagram 9:16", "Instagram 9x16.epr",
                  (f"{_H264}/01 - Match Source - High bitrate.epr",)),
    BuiltinPreset("tiktok_9x16", "TikTok 9:16", "TikTok 9x16.epr", (f"{_H264}/01 - Match Source - High bitrate.epr",)),
    BuiltinPreset("match_h264", "Match source H.264", "Match source H264.epr",
                  (f"{_H264}/01 - Match Source - High bitrate.epr",)),
)


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    path: str
    source: str  # shipped | system | user


def system_preset_roots(program_files: Optional[str] = None) -> list[Path]:
    """`MediaIO/systempresets` folders of the installed Premiere Pro and Media Encoder, newest version first."""
    base = Path(program_files or os.environ.get("ProgramFiles", r"C:\Program Files")) / "Adobe"
    if not base.is_dir():
        return []
    found = []
    for pattern in ("Adobe Premiere Pro*", "Adobe Media Encoder*"):
        for app_dir in base.glob(pattern):
            folder = app_dir / "MediaIO" / "systempresets"
            if folder.is_dir():
                found.append(folder)
    return sorted(found, key=lambda p: (_year(p.parent.parent.name), "Premiere" in p.parent.parent.name), reverse=True)


def _year(name: str) -> int:
    match = re.search(r"(\d{4})", name)
    return int(match.group(1)) if match else 0


def available_presets(user_dir: Optional[Path], *, shipped_dir: Path = SHIPPED_DIR,
                      system_roots: Optional[Sequence[Path]] = None) -> list[Preset]:
    roots = list(system_preset_roots() if system_roots is None else system_roots)
    presets: list[Preset] = []
    for builtin in BUILTIN:
        resolved = _resolve(builtin, shipped_dir, roots)
        if resolved is not None:
            presets.append(resolved)
    presets.extend(user_presets(user_dir))
    return presets


def user_presets(user_dir: Optional[Path]) -> list[Preset]:
    if user_dir is None or not Path(user_dir).is_dir():
        return []
    files = sorted((p for p in Path(user_dir).iterdir() if p.is_file() and p.suffix.lower() == ".epr"),
                   key=lambda p: p.stem.lower())
    return [Preset(USER_PREFIX + p.stem, p.stem, str(p), "user") for p in files]


def _resolve(builtin: BuiltinPreset, shipped_dir: Path, roots: Iterable[Path]) -> Optional[Preset]:
    shipped = shipped_dir / builtin.shipped
    if shipped.is_file():
        return Preset(builtin.id, builtin.label, str(shipped), "shipped")
    for root in roots:
        for relative in builtin.system:
            candidate = Path(root) / relative
            if candidate.is_file():
                return Preset(builtin.id, builtin.label, str(candidate), "system")
    return None
