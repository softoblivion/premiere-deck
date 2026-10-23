"""Layout templates, exclusions, root detection and the folder <-> bin mapping (docs/FILES.md 1.5, 2).

Paths are compared normalised and case-insensitively (`norm`); bin paths are "/"-separated and compared with
`norm_bin`. Everything here is pure except `load_layout*` (reads JSON) and `is_hidden` (reads file attributes).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

LAYOUTS_DIR = Path(__file__).with_name("layouts")
DEFAULT_LAYOUT_ID = "default"
DECK_DIR = ".premiere-deck"
CUSTOM_LAYOUT_FILE = "layout.json"

# docs/FILES.md 1.5: never touched, never imported.
PREMIERE_FOLDERS = (
    "adobe premiere pro auto-save",
    "adobe premiere pro video previews",
    "adobe premiere pro audio previews",
    "adobe premiere pro captured video",
    "adobe premiere pro captured audio",
)
PREMIERE_FOLDER_PREFIXES = ("media cache",)
TEMP_PATTERNS = ("~*", "*.tmp", "*.part", "*.crdownload", "thumbs.db", "desktop.ini", "*.prproj", "*.prmf")
MASKS_SUFFIX = " masks"  # Premiere's mask folder "<project stem> Masks" beside the project file
# Folders left out of the ZIP archive (docs/FILES.md 3.6): caches and previews, not the auto-save copies.
PACK_SKIP_FOLDERS = ("adobe premiere pro video previews", "adobe premiere pro audio previews")

MATCH_KEYS = {"kind", "alpha", "name", "ext", "min_duration", "max_duration", "has_audio", "has_video", "source",
              "audio_class"}
KINDS = {"video", "audio", "image", "other"}
SOURCES = ("camera", "stock", "generated", "unknown")
AUDIO_CLASSES = ("speech", "music", "other")
DEFAULT_MEDIA_DIR = "Media"


class LayoutError(ValueError):
    """A layout file is missing, not JSON or violates the schema of docs/FILES.md 2."""


# --- paths --------------------------------------------------------------------------------------------------------


def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def norm_bin(tree_path: Optional[str]) -> str:
    parts = [part for part in str(tree_path or "").replace("\\", "/").split("/") if part]
    return "/".join(parts).casefold()


def is_inside(path: str, root: str) -> bool:
    """`path` lies below `root` (not equal to it)."""
    p, r = norm(path), norm(root).rstrip("\\/")
    return p.startswith(r + os.sep)


def rel_parts(path: str, root: str) -> Optional[list[str]]:
    """Components of `path` below `root` with their original case, or None when outside."""
    if not is_inside(path, root):
        return None
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return [part for part in rel.replace("\\", "/").split("/") if part]


def same_path(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and norm(a) == norm(b)


def is_hidden(path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith("."):
        return True
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))


def is_premiere_folder(name: str) -> bool:
    low = name.casefold()
    return low in PREMIERE_FOLDERS or any(low.startswith(prefix) for prefix in PREMIERE_FOLDER_PREFIXES)


def is_temp_name(name: str) -> bool:
    low = name.casefold()
    return any(fnmatch.fnmatchcase(low, pattern) for pattern in TEMP_PATTERNS)


def masks_folder(project_path: Optional[str]) -> Optional[str]:
    """Premiere's mask folder name of a project file, case-folded ("spring masks" for Spring.prproj)."""
    if not project_path:
        return None
    stem = os.path.splitext(os.path.basename(str(project_path)))[0]
    return (stem + MASKS_SUFFIX).casefold() if stem else None


def is_excluded(path: str, root: str, *, check_hidden: bool = True, masks: Optional[str] = None) -> bool:
    """docs/FILES.md 1.5 for a path inside `root`: Premiere's folders, the mask folder `masks` ("<project stem>
    masks", from `masks_folder`) and `.prmf` files, `.premiere-deck`, temp and hidden files, project files. A path
    outside the root is excluded only by its own name."""
    parts = rel_parts(path, root)
    if parts is None:
        parts = [os.path.basename(path)]
    folders, name = parts[:-1], parts[-1] if parts else ""
    for folder in folders:
        low = folder.casefold()
        if low == DECK_DIR or is_premiere_folder(folder) or folder.startswith(".") or (masks and low == masks):
            return True
    low = name.casefold()
    if is_temp_name(name) or is_premiere_folder(name) or low == DECK_DIR or (masks and low == masks):
        return True
    return bool(check_hidden and is_hidden(path))


# --- layout model -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    match: dict
    to: str


@dataclass(frozen=True)
class Layout:
    id: str
    name: str
    project_dir: str
    folders: tuple[str, ...]
    rules: tuple[Rule, ...]
    mirror_subfolders: bool = True
    exports_dir: str = "Exports"
    media_dir: str = DEFAULT_MEDIA_DIR
    source: str = ""
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def fallback_folder(self) -> str:
        return self.rules[-1].to if self.rules else (self.folders[0] if self.folders else "Footage")

    def all_folders(self) -> list[str]:
        """Layout folders plus their ancestors ("Audio" for "Audio/Music"), in layout order."""
        seen: dict[str, str] = {}
        for folder in self.folders:
            parts = folder.split("/")
            for depth in range(1, len(parts) + 1):
                sub = "/".join(parts[:depth])
                seen.setdefault(sub.casefold(), sub)
        return list(seen.values())

    def leaf_folders(self) -> list[str]:
        return list(self.folders)

    def canonical(self, rel_folder: str) -> Optional[str]:
        """The layout's spelling of a folder path given in any case, or None when it is not a layout folder."""
        return {f.casefold(): f for f in self.all_folders()}.get(rel_folder.casefold())

    def top_folders(self) -> set[str]:
        return {folder.split("/")[0].casefold() for folder in self.folders}

    def in_media_dir(self, folder: Optional[str]) -> bool:
        """`folder` ("/"-separated, relative) is the Sync folder or lies below it."""
        return is_under(folder, self.media_dir)

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "project_dir": self.project_dir, "folders": list(self.folders),
            "rules": [{"match": dict(rule.match), "to": rule.to} for rule in self.rules],
            "mirror_subfolders": self.mirror_subfolders, "exports_dir": self.exports_dir, "media_dir": self.media_dir,
        }


def is_under(folder: Optional[str], parent: Optional[str]) -> bool:
    """Relative "/"-separated folder `folder` equals `parent` or lies below it (case-insensitive)."""
    if not folder or not parent:
        return False
    a = [p.casefold() for p in folder.split("/") if p]
    b = [p.casefold() for p in parent.split("/") if p]
    return bool(b) and a[:len(b)] == b


def clean_folder(value: object) -> Optional[str]:
    """A safe relative folder path ("Audio/SFX") or None: no drive, no "..", no excluded names."""
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.replace("\\", "/").split("/") if part.strip()]
    if not parts:
        return None
    for part in parts:
        if part in (".", "..") or ":" in part or part.startswith(".") or is_premiere_folder(part):
            return None
        if any(ch in part for ch in '<>"|?*') or part.casefold() == DECK_DIR:
            return None
    return "/".join(parts)


def _clean_match(match: object) -> dict:
    if not isinstance(match, dict):
        raise LayoutError("a rule's match is an object")
    out: dict = {}
    for key, value in match.items():
        if key not in MATCH_KEYS:
            raise LayoutError(f"unknown match field {key!r}")
        if key == "kind":
            if value not in KINDS:
                raise LayoutError(f"unknown kind {value!r}")
            out[key] = value
        elif key in ("alpha", "has_audio", "has_video"):
            if not isinstance(value, bool):
                raise LayoutError(f"{key} is a boolean")
            out[key] = value
        elif key == "name":
            if not isinstance(value, str) or not value:
                raise LayoutError("name is a non-empty regular expression")
            try:
                re.compile(value, re.I)
            except re.error as exc:
                raise LayoutError(f"name is not a valid regular expression: {exc}") from exc
            out[key] = value
        elif key in ("source", "audio_class"):
            allowed = SOURCES if key == "source" else AUDIO_CLASSES
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise LayoutError(f"{key} is one value or a list of values")
            for item in values:
                if item not in allowed:
                    raise LayoutError(f"unknown {key} {item!r}")
            out[key] = values[0] if isinstance(value, str) else list(values)
        elif key == "ext":
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise LayoutError("ext is a list of extensions")
            out[key] = [v.lower().lstrip(".") for v in values]
        else:  # durations
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise LayoutError(f"{key} is a number of seconds")
            out[key] = float(value)
    return out


def parse_layout(data: object, source: str = "") -> Layout:
    if not isinstance(data, dict):
        raise LayoutError("a layout is a JSON object")
    folders_raw = data.get("folders")
    if not isinstance(folders_raw, list) or not folders_raw:
        raise LayoutError("folders is a non-empty list")
    folders: list[str] = []
    for value in folders_raw:
        folder = clean_folder(value)
        if folder is None:
            raise LayoutError(f"bad folder {value!r}")
        if folder.casefold() not in {f.casefold() for f in folders}:
            folders.append(folder)
    rules_raw = data.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise LayoutError("rules is a non-empty list")
    media_dir = clean_folder(data.get("media_dir", DEFAULT_MEDIA_DIR))
    if media_dir is None:
        raise LayoutError(f"bad media_dir {data.get('media_dir')!r}")
    if media_dir.casefold() not in {f.casefold() for f in folders}:
        folders.append(media_dir)  # a 1.x layout without the Sync folder
    rules: list[Rule] = []
    for value in rules_raw:
        if not isinstance(value, dict):
            raise LayoutError("a rule is an object")
        to = clean_folder(value.get("to"))
        if to is None:
            raise LayoutError(f"bad target folder {value.get('to')!r}")
        if is_under(to, media_dir):
            raise LayoutError(f"a rule cannot file into the Sync folder {media_dir!r}")
        rules.append(Rule(_clean_match(value.get("match", {})), to))
    project_dir = clean_folder(data.get("project_dir", "Project")) or "Project"
    exports_dir = clean_folder(data.get("exports_dir", "Exports")) or "Exports"
    mirror = data.get("mirror_subfolders", True)
    return Layout(
        id=str(data.get("id") or "custom"),
        name=str(data.get("name") or data.get("id") or "Custom"),
        project_dir=project_dir,
        folders=tuple(folders),
        rules=tuple(rules),
        mirror_subfolders=mirror if isinstance(mirror, bool) else True,
        exports_dir=exports_dir,
        media_dir=media_dir,
        source=source,
        raw=dict(data),
    )


def load_layout_file(path: str | Path) -> Layout:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LayoutError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LayoutError(f"{Path(path).name} is not valid JSON: {exc}") from exc
    return parse_layout(data, source=str(path))


def shipped_layouts() -> dict[str, Layout]:
    layouts: dict[str, Layout] = {}
    for path in sorted(LAYOUTS_DIR.glob("*.json")):
        try:
            layout = load_layout_file(path)
        except LayoutError:
            continue
        layouts[layout.id] = layout
    return layouts


def default_layout() -> Layout:
    return load_layout_file(LAYOUTS_DIR / f"{DEFAULT_LAYOUT_ID}.json")


# --- root detection -----------------------------------------------------------------------------------------------


def detect_root(project_path: str, layout: Layout) -> str:
    """docs/FILES.md 2: the folder of the `.prproj`, or its parent when the project file sits in the layout's
    `project_dir` and the parent holds `.premiere-deck` or any layout folder."""
    folder = os.path.dirname(os.path.abspath(project_path))
    parent = os.path.dirname(folder)
    if os.path.basename(folder).casefold() != layout.project_dir.split("/")[-1].casefold() or parent == folder:
        return folder
    if os.path.isdir(os.path.join(parent, DECK_DIR)):
        return parent
    for top in {f.split("/")[0] for f in layout.folders}:
        if top.casefold() == layout.project_dir.split("/")[0].casefold():
            continue
        if os.path.isdir(os.path.join(parent, top)):
            return parent
    return folder


# --- folder <-> bin -----------------------------------------------------------------------------------------------


class Mapper:
    """Maps disk folders under a root to bin paths and back for one layout (docs/FILES.md 2, mirror_subfolders)."""

    def __init__(self, root: str, layout: Layout, masks: Optional[str] = None) -> None:
        self.root = os.path.abspath(root)
        self.layout = layout
        self.masks = masks  # "<project stem> masks", case-folded (masks_folder)

    def excluded(self, path: str, *, check_hidden: bool = True) -> bool:
        return is_excluded(path, self.root, check_hidden=check_hidden, masks=self.masks)

    # disk -> project
    def folder_of(self, path: str) -> Optional[list[str]]:
        """Folder components of a file path below the root ([] for a loose file at the root), None outside."""
        parts = rel_parts(path, self.root)
        return None if parts is None else parts[:-1]

    def layout_folder_of(self, path: str) -> Optional[str]:
        """The folder (relative, "/"-separated) that files at `path` are filed under, or None when the file is
        loose (root, project folder, a non-layout folder without mirroring) or outside the root."""
        parts = self.folder_of(path)
        if not parts:
            return None
        if self.excluded(path, check_hidden=False):
            return None
        rel = "/".join(parts)
        if rel.casefold() == self.layout.project_dir.casefold() or parts[0].casefold() == self.layout.project_dir.split("/")[0].casefold():
            return None
        if self.layout.mirror_subfolders:
            return self.spell(parts)
        # no mirroring: the deepest layout folder that contains the file
        for depth in range(len(parts), 0, -1):
            canonical = self.layout.canonical("/".join(parts[:depth]))
            if canonical:
                return canonical
        return None

    def in_layout(self, path: str) -> bool:
        return self.layout_folder_of(path) is not None

    def deepest_layout_folder(self, path: str) -> Optional[str]:
        """The deepest layout folder (layout spelling) that contains `path`, or None (loose, project_dir, a folder
        outside the layout, outside the root)."""
        parts = self.folder_of(path)
        if not parts or self.excluded(path, check_hidden=False):
            return None
        if parts[0].casefold() == self.layout.project_dir.split("/")[0].casefold():
            return None
        for depth in range(len(parts), 0, -1):
            canonical = self.layout.canonical("/".join(parts[:depth]))
            if canonical:
                return canonical
        return None

    def rel_folder(self, path: str) -> Optional[str]:
        """The folder of `path` relative to the root ("/"-separated, "" for the root), None outside."""
        parts = self.folder_of(path)
        return None if parts is None else "/".join(parts)

    def bin_for_folder(self, folder: str) -> str:
        """Bin path of a folder: the same path with "/" (bin names = folder names)."""
        return "/".join(part for part in folder.split("/") if part)

    def is_exports(self, path: str) -> bool:
        parts = self.folder_of(path)
        if not parts:
            return False
        exports = [p.casefold() for p in self.layout.exports_dir.split("/")]
        return [p.casefold() for p in parts[:len(exports)]] == exports

    # project -> disk
    def folder_for_bin(self, tree_path: str) -> Optional[str]:
        """The disk folder a bin maps to, or None (a "Selects" bin with no folder; the root bin)."""
        parts = [part for part in str(tree_path or "").split("/") if part]
        if not parts:
            return None
        rel = "/".join(parts)
        if clean_folder(rel) is None:
            return None
        if parts[0].casefold() == self.layout.project_dir.split("/")[0].casefold():
            return None
        canonical = self.layout.canonical(rel)
        if canonical:
            return canonical
        if not self.layout.mirror_subfolders:
            return None
        # under a layout folder, or a folder that exists on disk (a "Footage2" the user made)
        if self.layout.canonical(parts[0]) is not None or os.path.isdir(os.path.join(self.root, *parts)):
            return self.spell(parts)
        return None

    def spell(self, parts: list[str]) -> str:
        """Folder path with the layout's spelling for every leading component that is a layout folder."""
        out: list[str] = []
        for part in parts:
            canonical = self.layout.canonical("/".join(out + [part]))
            out.append(canonical.split("/")[-1] if canonical else part)
        return "/".join(out)

    def abs_folder(self, folder: str) -> str:
        return os.path.join(self.root, *folder.split("/"))


def match_glob(name: str, patterns: Iterable[str]) -> bool:
    low = name.casefold()
    return any(fnmatch.fnmatchcase(low, pattern.casefold()) for pattern in patterns)
