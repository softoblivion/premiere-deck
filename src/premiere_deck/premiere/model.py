"""Data model of the Premiere facade (docs/PREMIERE_API.md, section 1).

Owner: integrator. Plain frozen dataclasses built from the JSON the jsx scripts return; field names equal the keys
the scripts produce (snake_case). `PremiereLike` is the interface modules code against; the real implementation
is `premiere.api.PremiereApi`, the test double is `modules.base.FakePremiere`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

TICKS_PER_SECOND = 254016000000

COLOR_LABELS = (
    "Violet", "Iris", "Caribbean", "Lavender", "Cerulean", "Forest", "Rose", "Mango",
    "Purple", "Blue", "Teal", "Magenta", "Tan", "Green", "Brown", "Yellow",
)

KIND_BIN = "bin"
KIND_CLIP = "clip"
KIND_SEQUENCE = "sequence"
KIND_FILE = "file"


@dataclass(frozen=True)
class ProjectInfo:
    path: str
    root: str
    name: str
    deck_dir: str


@dataclass(frozen=True)
class ProjectItem:
    node_id: str
    name: str
    kind: str
    tree_path: str = ""
    media_path: Optional[str] = None
    is_offline: bool = False
    is_sequence: bool = False
    duration: Optional[float] = None
    has_video: Optional[bool] = None
    has_audio: Optional[bool] = None
    color_label: Optional[int] = None

    @property
    def parent_path(self) -> str:
        return self.tree_path

    @property
    def full_path(self) -> str:
        """Bin path including the item's own name ("Footage/Day1/clip.mp4")."""
        return f"{self.tree_path}/{self.name}" if self.tree_path else self.name


@dataclass(frozen=True)
class Snapshot:
    project_path: str
    taken_at: float
    items: tuple[ProjectItem, ...] = ()

    def by_node_id(self) -> dict[str, ProjectItem]:
        return {item.node_id: item for item in self.items}

    def clips(self) -> tuple[ProjectItem, ...]:
        return tuple(item for item in self.items if item.kind == KIND_CLIP)

    def bins(self) -> tuple[ProjectItem, ...]:
        return tuple(item for item in self.items if item.kind == KIND_BIN)


@dataclass(frozen=True)
class SequenceInfo:
    sequence_id: str
    name: str
    width: int
    height: int
    fps: float
    duration: float
    in_point: Optional[float] = None
    out_point: Optional[float] = None


@dataclass(frozen=True)
class TrackItem:
    sequence_id: str
    track_kind: str
    track_index: int
    index: int
    name: str
    node_id: Optional[str]
    start: float
    end: float
    in_point: float
    out_point: float
    selected: bool = False


@dataclass(frozen=True)
class Selection:
    sequence_id: str
    items: tuple[TrackItem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)


class PremiereError(Exception):
    """A script failed inside Premiere (docs/PROTOCOL.md, section 9)."""

    def __init__(self, message: str, line: Optional[int] = None, source: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.source = source


class PremiereUnavailable(PremiereError):
    """No agent is connected."""

    def __init__(self, message: str = "Premiere Pro is not connected") -> None:
        super().__init__(message)


class PremiereLike(Protocol):
    """The operations of docs/PREMIERE_API.md, section 2, as seen by a module."""

    def is_connected(self) -> bool: ...
    def project(self) -> Optional[ProjectInfo]: ...
    def snapshot(self) -> Snapshot: ...
    def item(self, node_id: str) -> Optional[ProjectItem]: ...
    def create_bin_path(self, tree_path: str) -> str: ...
    def move_to_bin(self, node_ids: Sequence[str], tree_path: str) -> None: ...
    def relink(self, node_id: str, media_path: str) -> bool: ...
    def import_files(self, paths: Sequence[str], tree_path: str, *, suppress_dialogs: bool = True) -> list[ProjectItem]: ...
    def rename_item(self, node_id: str, name: str) -> None: ...
    def remove_items(self, node_ids: Sequence[str]) -> int: ...
    def set_color_label(self, node_ids: Sequence[str], label: int) -> None: ...
    def active_sequence(self) -> Optional[SequenceInfo]: ...
    def sequences(self) -> list[SequenceInfo]: ...
    def selection(self) -> Selection: ...
    def track_items(self, sequence_id: str) -> list[TrackItem]: ...
    def playhead(self) -> float: ...
    def set_playhead(self, seconds: float) -> None: ...
    def open_in_source(self, node_id: str, *, seconds: Optional[float] = None) -> None: ...
    def insert_clip(self, node_id: str, *, start: float, end: float, at: Optional[float] = None,
                    video_track: int = 0, audio_track: int = 0) -> None: ...
    def add_marker(self, target_id: str, seconds: float, name: str, comment: str = "") -> None: ...
    def save_project(self) -> None: ...
    def save_project_as(self, path: str) -> None: ...
    def open_project(self, path: str) -> None: ...
    def workspaces(self) -> list[str]: ...
    def set_workspace(self, name: str) -> None: ...
    def premiere_version(self) -> str: ...
    def eval(self, name: str, code: str, args: Any = None, *, timeout_ms: int = 30000) -> Any: ...
    def script(self, path: str) -> str: ...


def item_from_json(data: dict) -> ProjectItem:
    return ProjectItem(
        node_id=str(data["node_id"]),
        name=str(data.get("name", "")),
        kind=str(data.get("kind", KIND_FILE)),
        tree_path=str(data.get("tree_path", "")),
        media_path=data.get("media_path") or None,
        is_offline=bool(data.get("is_offline", False)),
        is_sequence=bool(data.get("is_sequence", False)),
        duration=_opt_float(data.get("duration")),
        has_video=_opt_bool(data.get("has_video")),
        has_audio=_opt_bool(data.get("has_audio")),
        color_label=_opt_int(data.get("color_label")),
    )


def snapshot_from_json(data: dict, taken_at: float) -> Snapshot:
    return Snapshot(
        project_path=str(data.get("project_path", "")),
        taken_at=taken_at,
        items=tuple(item_from_json(item) for item in data.get("items", [])),
    )


def _opt_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _opt_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


__all__ = [name for name in globals() if not name.startswith("_")] + ["field"]
