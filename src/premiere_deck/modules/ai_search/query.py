"""Query side of AI search (docs/AI_SEARCH.md 3): text embedding cache, cosine ranking with per-clip
non-maximum suppression, relative score bars and the thumbnail disk cache.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from premiere_deck.modules.ai_search.index import Matrix

DEFAULT_THRESHOLD = 0.25  # cosine; chosen on an evaluation set
NMS_SECONDS = 2.0
CANDIDATE_CAP = 20000  # frames above the threshold considered for grouping (argpartition first)


def prompt_for(text: str) -> str:
    """CLIP was trained on captions; a bare noun phrase scores better as "a photo of ...". Queries that already
    read like a caption are kept as typed."""
    text = " ".join(text.split())
    lowered = text.lower()
    if not text or len(text.split()) > 6 or lowered.startswith(("a ", "an ", "the ", "photo", "picture", "image")):
        return text
    return f"a photo of {text}"


class TextCache:
    """LRU of query text -> embedding (thread-safe)."""

    def __init__(self, embed: Callable[[str], np.ndarray], size: int = 64) -> None:
        self._embed = embed
        self._size = size
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self.misses = 0

    def get(self, text: str) -> np.ndarray:
        key = " ".join(text.lower().split())
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        vector = self._embed(prompt_for(text))
        with self._lock:
            self.misses += 1
            self._items[key] = vector
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return vector

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass(frozen=True)
class Ranked:
    key: str  # quick hash of the clip
    times: tuple[float, ...]  # best first
    scores: tuple[float, ...]

    @property
    def score(self) -> float:
        return self.scores[0]

    @property
    def time(self) -> float:
        return self.times[0]


def rank(matrix: Matrix, query: np.ndarray, *, threshold: float = DEFAULT_THRESHOLD, top: int = 30,
         per_clip: int = 3, nms_seconds: float = NMS_SECONDS) -> list[Ranked]:
    """Cosine of every frame against `query`; per clip the best `per_clip` frames at least `nms_seconds` apart;
    clips ranked by their best frame; frames below `threshold` dropped."""
    if matrix.emb.shape[0] == 0:
        return []
    scores = matrix.emb @ np.asarray(query, dtype=np.float32)
    idx = np.flatnonzero(scores >= threshold)
    if idx.size == 0:
        return []
    if idx.size > CANDIDATE_CAP:
        idx = idx[np.argpartition(-scores[idx], CANDIDATE_CAP)[:CANDIDATE_CAP]]
    idx = idx[np.argsort(-scores[idx], kind="stable")]
    picked: dict[int, list[tuple[float, float]]] = {}
    order: list[int] = []
    full: set[int] = set()
    for i in idx:
        owner = int(matrix.owner[i])
        if owner in full:
            continue
        t = float(matrix.times[i])
        chosen = picked.get(owner)
        if chosen is None:
            if len(order) >= top:
                continue  # a new clip after the top clips are known cannot rank anymore
            chosen = picked[owner] = []
            order.append(owner)
        if any(abs(t - other) < nms_seconds for other, _ in chosen):
            continue
        chosen.append((t, float(scores[i])))
        if len(chosen) >= per_clip:
            full.add(owner)
            if len(full) >= top and len(order) >= top:
                break
    return [Ranked(matrix.keys[o], tuple(t for t, _ in picked[o]), tuple(s for _, s in picked[o])) for o in order]


def bar_value(score: float, best: float, threshold: float) -> float:
    """Relative strength in 0.2..1: the best hit of the query is a full bar, a hit at the threshold a short one."""
    span = max(best - threshold, 1e-6)
    return float(min(1.0, max(0.2, 0.2 + 0.8 * (score - threshold) / span)))



def timecode(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# --- thumbnails --------------------------------------------------------------------------------------------------


class ThumbCache:
    """JPEG thumbnails on disk, least recently used evicted beyond `max_bytes` (access refreshes mtime)."""

    def __init__(self, folder: Path, max_bytes: int) -> None:
        self.folder = Path(folder)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()

    def path_for(self, key: str, seconds: float, width: int) -> Path:
        name = hashlib.blake2b(f"{key}|{seconds:.1f}|{width}".encode("utf-8"), digest_size=12).hexdigest()
        return self.folder / f"{name}.jpg"

    def get(self, key: str, seconds: float, width: int, make: Callable[[], bytes]) -> bytes:
        path = self.path_for(key, seconds, width)
        try:
            data = path.read_bytes()
            os.utime(path)
            return data
        except OSError:
            pass
        data = make()
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.evict()
        return data

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.folder.glob("*.jpg")) if self.folder.is_dir() else 0

    def evict(self) -> int:
        """Delete the oldest thumbnails until the cache fits; returns the number removed."""
        with self._lock:
            if not self.folder.is_dir():
                return 0
            files = []
            for p in self.folder.glob("*.jpg"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
            total = sum(size for _, size, _ in files)
            removed = 0
            for _, size, p in sorted(files, key=lambda f: f[0]):
                if total <= self.max_bytes:
                    break
                try:
                    p.unlink()
                    total -= size
                    removed += 1
                except OSError:
                    pass
            return removed

    def clear(self) -> None:
        with self._lock:
            for p in self.folder.glob("*.jpg"):
                try:
                    p.unlink()
                except OSError:
                    pass


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
