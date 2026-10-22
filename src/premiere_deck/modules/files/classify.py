"""Rule evaluation over `MediaInfo` (docs/FILES.md 2): overrides first, then the layout's rules, first match wins.

`name` patterns are case-insensitive regular expressions searched in the file stem, anchored at the start of a word:
"vo" matches "VO_take2" and "client vo" but not "devoted". A word starts after anything that is not a letter, so
"_", "-", digits and spaces all separate words.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePath
from typing import Optional, Sequence

from premiere_deck.core.media import KIND_AUDIO, KIND_IMAGE, KIND_VIDEO, MediaInfo, kind_from_extension

from .layout import Layout, clean_folder, norm

_INLINE_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")


@dataclass(frozen=True)
class Override:
    to: str
    source: Optional[str] = None  # folder prefix of the source file
    name: Optional[str] = None  # glob over the file name ("*_sfx*")


@dataclass(frozen=True)
class Decision:
    folder: str
    reason: str  # "override: ..." / "rule 3" / "fallback"


def parse_overrides(value: object) -> list[Override]:
    """`modules.files.overrides` of project.json; bad entries are skipped (docs/MODULES.md 5)."""
    if not isinstance(value, list):
        return []
    out: list[Override] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        to = clean_folder(entry.get("to"))
        source = entry.get("source")
        name = entry.get("name")
        if to is None:
            continue
        if isinstance(source, str) and source.strip():
            out.append(Override(to=to, source=source.strip()))
        elif isinstance(name, str) and name.strip():
            out.append(Override(to=to, name=name.strip()))
    return out


@lru_cache(maxsize=256)
def _word_regex(pattern: str) -> re.Pattern:
    flags = _INLINE_FLAGS.match(pattern)
    prefix = flags.group(0) if flags else ""
    body = pattern[len(prefix):]
    return re.compile(prefix + r"(?<![^\W\d_])(?:" + body + ")", re.IGNORECASE)


def name_matches(pattern: str, stem: str) -> bool:
    try:
        return _word_regex(pattern).search(stem) is not None
    except re.error:
        return False


def effective(info: MediaInfo) -> dict:
    """Values the match fields compare against, with gaps of the probe filled from the kind."""
    kind = info.kind or kind_from_extension(info.path)
    has_video = info.has_video if info.has_video is not None else kind in (KIND_VIDEO, KIND_IMAGE)
    has_audio = info.has_audio if info.has_audio is not None else kind == KIND_AUDIO
    ext = (info.ext or PurePath(info.path).suffix.lstrip(".")).lower()
    return {"kind": kind, "alpha": bool(info.alpha), "ext": ext, "duration": info.duration,
            "has_video": bool(has_video), "has_audio": bool(has_audio), "stem": PurePath(info.path).stem}


def rule_matches(match: dict, info: MediaInfo, values: Optional[dict] = None) -> bool:
    v = values or effective(info)
    for key, expected in match.items():
        if key == "kind" and v["kind"] != expected:
            return False
        if key == "alpha" and v["alpha"] != expected:
            return False
        if key == "has_video" and v["has_video"] != expected:
            return False
        if key == "has_audio" and v["has_audio"] != expected:
            return False
        if key == "ext" and v["ext"] not in expected:
            return False
        if key == "name" and not name_matches(expected, v["stem"]):
            return False
        if key == "min_duration" and (v["duration"] is None or v["duration"] < expected):
            return False
        if key == "max_duration" and (v["duration"] is None or v["duration"] > expected):
            return False
    return True


def override_matches(override: Override, source_path: str) -> bool:
    if override.source:
        folder = norm(override.source).rstrip("\\/")
        path = norm(source_path)
        return path.startswith(folder + os.sep)
    if override.name:
        return fnmatch.fnmatchcase(os.path.basename(source_path).casefold(), override.name.casefold())
    return False


def classify(info: MediaInfo, layout: Layout, overrides: Sequence[Override] = (),
             source_path: Optional[str] = None) -> Decision:
    source = source_path or info.path
    for override in overrides:
        if override_matches(override, source):
            return Decision(override.to, f"override: {override.source or override.name}")
    values = effective(info)
    for index, rule in enumerate(layout.rules, start=1):
        if rule_matches(rule.match, info, values):
            return Decision(rule.to, f"rule {index}")
    return Decision(layout.fallback_folder, "fallback")


__all__ = ["Decision", "Override", "classify", "effective", "name_matches", "override_matches",
           "parse_overrides", "rule_matches"]
