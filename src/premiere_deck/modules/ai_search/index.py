"""The per-project frame index (docs/AI_SEARCH.md 2): catalogue, per-clip embeddings, eligibility and the
resumable indexing job.

Layout in `<project root>/.premiere-deck/index/`:
- `catalog.json`: `{"version": 2, "model", "dim", "clips": {<quick hash>: {path, node_id, name, kind, duration,
  frames, step, indexed_at}}, "paths": {<normalised path>: {hash, size, mtime}}}`. `paths` lets the status count
  pending clips with one `stat` per file instead of hashing it.
- `<quick hash>.npz`: `times` float32 [N] (seconds from the clip's first frame), `emb` float16 [N, dim]
  (L2-normalised).

A clip is written (npz, then catalogue) only when all its frames are embedded, so a cancelled or crashed job
leaves every finished clip in place and the next run continues with the rest.
"""

from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from premiere_deck.core.media import KIND_IMAGE, KIND_VIDEO, kind_from_extension
from premiere_deck.core.paths import atomic_write_bytes, atomic_write_json, read_json
from premiere_deck.modules.ai_search import frames as fr
from premiere_deck.premiere.model import KIND_CLIP, ProjectItem

try:  # the shared definition (docs/FILES.md: size + BLAKE2b of the first and last 4 MB)
    from premiere_deck.core.probe import quick_hash
except ImportError:  # pragma: no cover - only while core/probe.py is missing
    import hashlib

    def quick_hash(path: str) -> str:
        size = os.path.getsize(path)
        digest = hashlib.blake2b(digest_size=16)
        digest.update(int(size).to_bytes(8, "little"))
        block = 4 << 20
        with open(path, "rb") as handle:
            if size <= 2 * block:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            else:
                digest.update(handle.read(block))
                handle.seek(size - block)
                digest.update(handle.read(block))
        return digest.hexdigest()

CATALOG_VERSION = 2
CATALOG_FILE = "catalog.json"
INDEX_DIR = "index"


def norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


# --- catalogue ---------------------------------------------------------------------------------------------------


class Catalog:
    """Thread-safe: the indexing job writes while the module thread renders and searches."""

    def __init__(self, folder: Path, model_id: str, dim: int) -> None:
        self.folder = Path(folder)
        self.model_id = model_id
        self.dim = dim
        self.clips: dict[str, dict] = {}
        self.paths: dict[str, dict] = {}
        self.revision = 0
        self.notes: list[str] = []  # what load() had to repair, for the log
        self._lock = threading.RLock()
        self._matrix: Optional[tuple[int, "Matrix"]] = None

    @classmethod
    def for_project(cls, deck_dir: str, model_id: str, dim: int) -> "Catalog":
        catalog = cls(Path(deck_dir) / INDEX_DIR, model_id, dim)
        catalog.load()
        return catalog

    @property
    def file(self) -> Path:
        return self.folder / CATALOG_FILE

    def npz_path(self, key: str) -> Path:
        return self.folder / f"{key}.npz"

    # persistence
    def load(self) -> None:
        with self._lock:
            self.notes = []
            data = read_json(self.file, None)
            if data is None:
                if self.file.exists():
                    self._quarantine("unreadable")
                self.clips, self.paths = {}, {}
                self._bump()
                return
            if not isinstance(data, dict) or not isinstance(data.get("clips", {}), dict):
                self._quarantine("unexpected shape")
                self.clips, self.paths = {}, {}
                self._bump()
                return
            version = data.get("version", 1)
            if not isinstance(version, int) or version > CATALOG_VERSION:
                self._quarantine(f"version {version!r}")
                self.clips, self.paths = {}, {}
                self._bump()
                return
            if data.get("model") != self.model_id or int(data.get("dim", self.dim) or 0) != self.dim:
                self.notes.append(f"model changed from {data.get('model')!r}; the index is rebuilt")
                self._drop_npz(data.get("clips", {}).keys())
                self.clips, self.paths = {}, {}
                self._bump()
                self.save()
                return
            clips = {}
            for key, entry in data.get("clips", {}).items():
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    clips[str(key)] = _migrate_entry(entry)
            paths = {}
            raw_paths = data.get("paths", {}) if version >= 2 else {}
            for key, entry in (raw_paths or {}).items():
                if isinstance(entry, dict) and entry.get("hash") in clips:
                    paths[str(key)] = {"hash": entry["hash"], "size": entry.get("size"), "mtime": entry.get("mtime")}
            if version < CATALOG_VERSION:
                self.notes.append(f"migrated catalogue version {version} to {CATALOG_VERSION}")
            self.clips, self.paths = clips, paths
            self._bump()
            if version < CATALOG_VERSION:
                self.save()

    def save(self) -> None:
        with self._lock:
            data = {"version": CATALOG_VERSION, "model": self.model_id, "dim": self.dim,
                    "clips": self.clips, "paths": self.paths}
            atomic_write_json(self.file, data)

    def _quarantine(self, reason: str) -> None:
        target = self.file.with_name(f"{CATALOG_FILE}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(self.file, target)
            self.notes.append(f"catalogue {reason}; moved to {target.name}")
        except OSError:
            self.notes.append(f"catalogue {reason}")

    def _drop_npz(self, keys: Iterable[str]) -> None:
        for key in keys:
            try:
                self.npz_path(str(key)).unlink()
            except OSError:
                pass

    def _bump(self) -> None:
        self.revision += 1
        self._matrix = None

    # queries
    def has(self, key: str) -> bool:
        with self._lock:
            return key in self.clips and self.npz_path(key).is_file()

    def known_hash(self, path: str, size: int, mtime: float) -> Optional[str]:
        with self._lock:
            entry = self.paths.get(norm_path(path))
            if entry and entry.get("size") == size and entry.get("mtime") == mtime:
                return entry.get("hash")
            return None

    def is_indexed_path(self, path: str) -> bool:
        """Cheap check for the status line: the file is unchanged since it was hashed and its clip is indexed."""
        try:
            st = os.stat(path)
        except OSError:
            return False
        key = self.known_hash(path, st.st_size, st.st_mtime)
        return key is not None and key in self.clips

    def count(self) -> int:
        with self._lock:
            return len(self.clips)

    def frame_count(self) -> int:
        with self._lock:
            return sum(int(entry.get("frames", 0)) for entry in self.clips.values())

    def entry(self, key: str) -> Optional[dict]:
        with self._lock:
            entry = self.clips.get(key)
            return dict(entry) if entry else None

    # changes
    def remember_path(self, path: str, key: str, size: int, mtime: float) -> None:
        with self._lock:
            self.paths[norm_path(path)] = {"hash": key, "size": size, "mtime": mtime}

    def touch(self, key: str, *, node_id: str, path: str, name: str) -> None:
        """A known clip seen under a new node id or path (relinked, copied, re-imported)."""
        with self._lock:
            entry = self.clips.get(key)
            if entry and (entry.get("node_id"), entry.get("path"), entry.get("name")) != (node_id, path, name):
                entry.update({"node_id": node_id, "path": path, "name": name})
                self._bump()
                self.save()

    def put(self, key: str, entry: dict, times: np.ndarray, emb: np.ndarray) -> None:
        buffer = io.BytesIO()
        np.savez(buffer, times=np.asarray(times, dtype=np.float32), emb=np.asarray(emb, dtype=np.float16))
        atomic_write_bytes(self.npz_path(key), buffer.getvalue())
        with self._lock:
            self.clips[key] = dict(entry, frames=int(len(times)))
            self._bump()
            self.save()

    def clear(self) -> None:
        with self._lock:
            self._drop_npz(list(self.clips.keys()))
            self.clips, self.paths = {}, {}
            self._bump()
            self.save()

    def load_clip(self, key: str) -> Optional[tuple[np.ndarray, np.ndarray]]:
        try:
            with np.load(self.npz_path(key)) as data:
                return np.asarray(data["times"], dtype=np.float32), np.asarray(data["emb"])
        except (OSError, KeyError, ValueError):
            return None

    def matrix(self) -> "Matrix":
        """All frames of all clips as one float32 matrix, cached until the catalogue changes."""
        with self._lock:
            if self._matrix is not None and self._matrix[0] == self.revision:
                return self._matrix[1]
            revision = self.revision
            keys = list(self.clips.keys())
        parts, owners, times, kept = [], [], [], []
        for key in keys:
            loaded = self.load_clip(key)
            if loaded is None or len(loaded[0]) == 0 or loaded[1].shape[-1] != self.dim:
                continue
            owners.append(np.full(len(loaded[0]), len(kept), dtype=np.int32))
            kept.append(key)
            times.append(loaded[0])
            parts.append(loaded[1].astype(np.float32))
        if parts:
            matrix = Matrix(kept, np.concatenate(parts), np.concatenate(owners), np.concatenate(times))
        else:
            matrix = Matrix([], np.zeros((0, self.dim), np.float32), np.zeros(0, np.int32), np.zeros(0, np.float32))
        with self._lock:
            if self.revision == revision:
                self._matrix = (revision, matrix)
        return matrix


@dataclass(frozen=True)
class Matrix:
    keys: list
    emb: np.ndarray
    owner: np.ndarray
    times: np.ndarray


def _migrate_entry(entry: dict) -> dict:
    out = dict(entry)
    out.setdefault("node_id", "")
    out.setdefault("name", Path(entry["path"]).name)
    out.setdefault("kind", KIND_IMAGE if kind_from_extension(entry["path"]) == KIND_IMAGE else KIND_VIDEO)
    out.setdefault("duration", None)
    out.setdefault("frames", 0)
    out.setdefault("indexed_at", "")
    return out


# --- eligibility -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    node_id: str
    name: str
    path: str
    kind: str  # video | image
    duration: Optional[float]
    tree_path: str = ""


@dataclass(frozen=True)
class Skipped:
    node_id: str
    name: str
    reason: str


def bin_excluded(tree_path: str, exclude_bins: Sequence[str]) -> bool:
    """`A/B` excludes that bin and everything below it; a bare name (`Overlays`) excludes every bin of that name
    at any depth. Case-insensitive."""
    parts = [p.lower() for p in tree_path.split("/") if p]
    joined = "/".join(parts)
    for raw in exclude_bins:
        entry = "/".join(p for p in str(raw).strip().lower().split("/") if p)
        if not entry:
            continue
        if "/" in entry:
            if joined == entry or joined.startswith(entry + "/"):
                return True
        elif entry in parts:
            return True
    return False


def select_candidates(items: Sequence[ProjectItem], *, exclude_bins: Sequence[str],
                      min_duration: float) -> tuple[list[Candidate], list[Skipped]]:
    chosen: list[Candidate] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    for item in items:
        if item.kind != KIND_CLIP or item.is_sequence or not item.media_path:
            continue
        kind = kind_from_extension(item.media_path)
        if kind not in (KIND_VIDEO, KIND_IMAGE) or item.has_video is False:
            skipped.append(Skipped(item.node_id, item.name, "No picture"))
            continue
        if item.is_offline:
            skipped.append(Skipped(item.node_id, item.name, "Offline"))
            continue
        if bin_excluded(item.tree_path, exclude_bins):
            skipped.append(Skipped(item.node_id, item.name, "Excluded bin"))
            continue
        if kind == KIND_VIDEO and item.duration is not None and item.duration < min_duration:
            skipped.append(Skipped(item.node_id, item.name, "Too short"))
            continue
        key = norm_path(item.media_path)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(Candidate(item.node_id, item.name, item.media_path, kind, item.duration, item.tree_path))
    return chosen, skipped


def pending(catalog: Catalog, candidates: Sequence[Candidate]) -> list[Candidate]:
    return [c for c in candidates if not catalog.is_indexed_path(c.path)]


# --- the indexing job --------------------------------------------------------------------------------------------


@dataclass
class IndexResult:
    indexed: int = 0
    known: int = 0
    failed: dict = field(default_factory=dict)  # node_id -> reason
    frames: int = 0
    cancelled: bool = False
    seconds: float = 0.0


def eta_text(seconds: float) -> str:
    if seconds < 60:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min left"
    return f"{minutes // 60} h {minutes % 60} min left"


def run_index(job, *, catalog: Catalog, candidates: Sequence[Candidate], embed: Callable[[np.ndarray], np.ndarray],
              ffmpeg: str, step: float, max_frames: int, batch: int = 32,
              probe_duration: Optional[Callable[[str], Optional[float]]] = None,
              paused: Optional[Callable[[], bool]] = None, pause_poll: float = 0.5,
              pipe: Optional[Callable[[Sequence[str]], Any]] = None) -> IndexResult:
    """Index `candidates` one clip at a time; checks `job.cancelled` between batches and waits while `paused()`.
    `pipe` is `Tools.ffmpeg_pipe`; frames are read through it when given."""
    result = IndexResult()
    started = time.monotonic()
    total = len(candidates)
    weights = [max(1.0, c.duration or 30.0) if c.kind == KIND_VIDEO else 1.0 for c in candidates]
    weight_total = sum(weights) or 1.0
    weight_done = 0.0
    work_seconds = 0.0  # time spent on clips that needed embedding (excludes already indexed ones)
    work_weight = 0.0

    def cancelled() -> bool:
        return bool(getattr(job, "cancelled", False))

    def label(i: int) -> str:
        text = f"Indexing {min(i + 1, total)} of {total} clips"
        if work_weight > 0:
            remaining = (weight_total - weight_done) * work_seconds / work_weight
            text += " · " + eta_text(remaining)
        return text

    for i, cand in enumerate(candidates):
        while paused is not None and paused() and not cancelled():
            job.progress(weight_done / weight_total, "Paused while files are copied")
            time.sleep(pause_poll)
        if cancelled():
            result.cancelled = True
            break
        job.progress(weight_done / weight_total, label(i))
        clip_started = time.monotonic()
        try:
            st = os.stat(cand.path)
        except OSError:
            result.failed[cand.node_id] = "File not found"
            weight_done += weights[i]
            continue
        key = catalog.known_hash(cand.path, st.st_size, st.st_mtime)
        if key is None:
            try:
                key = quick_hash(cand.path)
            except OSError as exc:
                result.failed[cand.node_id] = f"Cannot read the file: {exc.strerror or exc}"
                weight_done += weights[i]
                continue
            catalog.remember_path(cand.path, key, st.st_size, st.st_mtime)
        if catalog.has(key):
            catalog.touch(key, node_id=cand.node_id, path=cand.path, name=cand.name)
            result.known += 1
            weight_done += weights[i]
            continue
        duration = cand.duration
        if duration is None and cand.kind == KIND_VIDEO and probe_duration is not None:
            try:
                duration = probe_duration(cand.path)
            except Exception:  # noqa: BLE001 - a failed probe only loses the step widening
                duration = None
        plan = fr.plan_for(duration, step, max_frames, image=cand.kind == KIND_IMAGE)
        times: list[float] = []
        chunks: list[np.ndarray] = []
        buffer: list[np.ndarray] = []
        try:
            for t, frame in fr.iter_frames(ffmpeg, cand.path, plan, cancelled=cancelled, pipe=pipe):
                times.append(t)
                buffer.append(frame)
                if len(buffer) >= batch:
                    chunks.append(embed(np.stack(buffer)))
                    buffer = []
                    if cancelled():
                        break
            if buffer and not cancelled():
                chunks.append(embed(np.stack(buffer)))
        except fr.FrameError as exc:
            result.failed[cand.node_id] = str(exc)[:200] or "ffmpeg failed"
            weight_done += weights[i]
            continue
        if cancelled():
            result.cancelled = True
            break
        if not chunks:
            result.failed[cand.node_id] = "No frames could be read"
            weight_done += weights[i]
            continue
        emb = np.concatenate(chunks)
        times_arr = np.asarray(times[:len(emb)], dtype=np.float32)
        entry = {"path": cand.path, "node_id": cand.node_id, "name": cand.name, "kind": cand.kind,
                 "duration": duration, "step": round(plan.step, 3), "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        catalog.put(key, entry, times_arr, emb)
        result.indexed += 1
        result.frames += len(emb)
        weight_done += weights[i]
        work_seconds += time.monotonic() - clip_started
        work_weight += weights[i]
    catalog.save()
    result.seconds = time.monotonic() - started
    job.progress(1.0 if not result.cancelled else weight_done / weight_total,
                 "Index cancelled" if result.cancelled else "Index up to date")
    return result
