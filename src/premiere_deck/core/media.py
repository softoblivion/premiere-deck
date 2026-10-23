"""Media description shared by the probe (core/probe.py) and the modules.

Shared by the host and the Files module. The probe fills `MediaInfo`; the Files classifier
(`modules/files/media_class.py`) adds `source` and `audio_class` with `dataclasses.replace`; other modules only read
it. Extension tables decide the kind when a probe is not possible and which files the Files watcher imports
(docs/FILES.md, sections 3.2 and 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Optional

VIDEO_EXTENSIONS = frozenset(
    "mp4 mov m4v mxf mkv avi webm mts m2ts mpg mpeg wmv flv 3gp r3d braw ari dng prores".split()
)
AUDIO_EXTENSIONS = frozenset("wav mp3 aac m4a flac ogg aif aiff wma opus ac3".split())
IMAGE_EXTENSIONS = frozenset("png jpg jpeg tif tiff bmp gif webp heic heif exr psd ai svg eps tga".split())
MOTION_EXTENSIONS = frozenset("mogrt aep aegraphic".split())
PROJECT_EXTENSIONS = frozenset("prproj".split())
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS | MOTION_EXTENSIONS

VIDEO_SOURCES = ("camera", "stock", "generated", "unknown")
AUDIO_CLASSES = ("speech", "music", "other")

KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_IMAGE = "image"
KIND_OTHER = "other"


def extension_of(path: str) -> str:
    return PurePath(path).suffix.lower().lstrip(".")


def is_media_extension(path: str) -> bool:
    """The extension is in one of the media tables (video, audio, image, motion)."""
    return extension_of(path) in MEDIA_EXTENSIONS


def kind_from_extension(path: str) -> str:
    """Kind by extension only; the probe refines it (an .mp4 without video is audio)."""
    ext = extension_of(path)
    if ext in VIDEO_EXTENSIONS:
        return KIND_VIDEO
    if ext in AUDIO_EXTENSIONS:
        return KIND_AUDIO
    if ext in IMAGE_EXTENSIONS:
        return KIND_IMAGE
    return KIND_OTHER


@dataclass(frozen=True)
class MediaInfo:
    """What the probe knows about one file. Unknown values are None; `kind` is always set.

    `tags`: container tags merged with the first video stream's tags (the stream wins), keys lower-cased.
    `nb_streams`: the container's stream count. `source` (video) and `audio_class` (audio) are None straight from
    the probe; the Files classifier fills them (docs/FILES.md 3.8, 4).
    """

    path: str
    kind: str
    ext: str
    size: int
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    has_video: Optional[bool] = None
    has_audio: Optional[bool] = None
    alpha: bool = False
    codec_video: Optional[str] = None
    codec_audio: Optional[str] = None
    probe_error: Optional[str] = None
    extra: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    nb_streams: Optional[int] = None
    source: Optional[str] = None
    audio_class: Optional[str] = None

    @property
    def is_media(self) -> bool:
        return self.kind in (KIND_VIDEO, KIND_AUDIO, KIND_IMAGE)
