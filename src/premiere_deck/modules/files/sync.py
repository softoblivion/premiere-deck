"""Disk side of the Files module (docs/FILES.md 3, 5.4): copies with `.part` and verification, collision handling,
the suppression set, the `watchdog` disk watcher, the Collect plan, and the Pack report and archive.

Nothing here talks to Premiere; the module applies results on its own thread.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from premiere_deck.core.probe import quick_hash

from .layout import PACK_SKIP_FOLDERS, norm, same_path

log = logging.getLogger("module.files.sync")

PART_SUFFIX = ".part"


class CopyError(OSError):
    """A copy did not complete or did not verify; the `.part` file is gone."""


class CopyCancelled(Exception):
    """The job was cancelled during a copy; the `.part` file is gone."""


# --- sizes --------------------------------------------------------------------------------------------------------


def fmt_size(size: int | float) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def safe_hash(path: str) -> Optional[str]:
    try:
        return quick_hash(path)
    except OSError:
        return None


# --- copies -------------------------------------------------------------------------------------------------------


def resolve_target(source: str, folder: str, *, source_hash: Optional[str] = None,
                   reserved: Optional[dict] = None) -> tuple[str, bool, Optional[str]]:
    """Target path in `folder` for a copy of `source` (docs/FILES.md 3.1 step 2).

    Returns `(target, reuse, source_hash)`: `reuse` is True when a file with equal content (size + quick hash) is
    already there, or `source` itself is the target. A name taken by a different file gets " (2)", " (3)", ...
    `reserved` (normalised target -> hash) holds targets earlier entries of a dry-run plan will create.
    """
    name = os.path.basename(source)
    stem, ext = os.path.splitext(name)
    n = 1
    candidate = os.path.join(folder, name)
    while True:
        key = norm(candidate)
        taken = os.path.exists(candidate) or os.path.exists(candidate + PART_SUFFIX)
        if reserved is not None and key in reserved:
            if source_hash is None:
                source_hash = safe_hash(source)
            if reserved[key] is not None and reserved[key] == source_hash:
                return candidate, True, source_hash
            taken = True
        elif os.path.isfile(candidate):
            if same_path(candidate, source):
                return candidate, True, source_hash
            if file_size(candidate) == file_size(source):
                if source_hash is None:
                    source_hash = safe_hash(source)
                if source_hash is not None and safe_hash(candidate) == source_hash:
                    return candidate, True, source_hash
        if not taken:
            return candidate, False, source_hash
        n += 1
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")


def copy_file(source: str, target: str, *, buffer_bytes: int = 16 << 20,
              on_bytes: Optional[Callable[[int], None]] = None,
              cancelled: Optional[Callable[[], bool]] = None) -> int:
    """Copy `source` to `target` through `target.part`: verify the size, keep the timestamps, rename into place.

    Never overwrites: an existing `target` raises `CopyError`. Returns the number of bytes copied.
    """
    part = target + PART_SUFFIX
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        raise CopyError(f"{target} already exists")
    copied = 0
    try:
        with open(source, "rb") as src, open(part, "xb") as dst:
            while True:
                if cancelled is not None and cancelled():
                    raise CopyCancelled()
                chunk = src.read(max(64 * 1024, int(buffer_bytes)))
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                if on_bytes is not None:
                    on_bytes(len(chunk))
            dst.flush()
            os.fsync(dst.fileno())
        expected = os.path.getsize(source)
        if os.path.getsize(part) != expected:
            raise CopyError(f"size mismatch after copying {os.path.basename(source)}")
        shutil.copystat(source, part)
        os.rename(part, target)  # fails when target appeared meanwhile; nothing is overwritten
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return copied


# --- suppression (docs/FILES.md 5.4) ------------------------------------------------------------------------------


class Suppression:
    """Expected outcomes of the module's own changes with a TTL; a matching event is consumed silently.

    Keys: ("disk", norm path) for files the module creates or deletes, ("import", norm path) for its own imports,
    ("bin", node id, norm bin path) for its own bin moves, ("media", node id, norm path) for its own relinks.
    """

    def __init__(self, ttl: float = 15.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple, float] = {}

    def add(self, key: tuple) -> None:
        with self._lock:
            self._entries[key] = self._clock() + self.ttl

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, until in self._entries.items() if until < now]:
            del self._entries[key]

    def consume(self, key: tuple) -> bool:
        with self._lock:
            self._purge()
            return self._entries.pop(key, None) is not None

    def contains(self, key: tuple) -> bool:
        with self._lock:
            self._purge()
            return key in self._entries

    def discard(self, key: tuple) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._entries)


# --- disk watcher (docs/FILES.md 3.2) -----------------------------------------------------------------------------


class DiskWatcher:
    """`watchdog` observer on the project root. Emits settled changes to `on_change` from its own thread:

    - {"type": "created", "path"}: a file appeared and its size has been stable for `stable` seconds;
    - {"type": "moved", "src", "path"}: a rename or move inside the root (also a delete and a create of the same
      file name within the hold time, which is how Windows reports moves between folders);
    - {"type": "deleted", "path"} and {"type": "deleted_dir", "path"}: held for `hold` seconds so a move can pair.

    Events are debounced `debounce` seconds per path; paths for which `ignored(path)` is true are dropped.
    """

    def __init__(self, root: str, on_change: Callable[[dict], None], *, ignored: Callable[[str], bool],
                 debounce: float = 1.5, stable: float = 2.0, hold: Optional[float] = None,
                 poll: float = 0.2) -> None:
        self.root = os.path.abspath(root)
        self.on_change = on_change
        self.ignored = ignored
        self.debounce = float(debounce)
        self.stable = float(stable)
        self.hold = float(hold if hold is not None else stable + 1.0)
        self.poll = float(poll)
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # noqa: D401 - watchdog callback
                try:
                    watcher._on_event(event)
                except Exception:  # noqa: BLE001 - never kill the observer thread
                    log.exception("watcher event failed")

        self._stop.clear()
        observer = Observer()
        observer.schedule(Handler(), self.root, recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._thread = threading.Thread(target=self._run, name="files-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer.stop()
                observer.join(5)
            except Exception:  # noqa: BLE001
                log.debug("observer stop failed", exc_info=True)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
        with self._lock:
            self._pending.clear()

    # -- events
    def _put(self, kind: str, path: str, src: Optional[str] = None) -> None:
        key = norm(path)
        now = time.monotonic()
        with self._lock:
            current = self._pending.get(key)
            if kind in ("deleted", "deleted_dir"):
                # a pending "created" is replaced: a temporary file yields a harmless "deleted" of an unknown path,
                # and a known file that was only touched before it went still reaches the module
                self._pending[key] = {"type": kind, "path": path, "at": now}
                return
            if current and current["type"] == "moved" and kind == "created":
                current["at"] = now  # a modification of a freshly moved file keeps it a move
                return
            if src is not None:
                self._pending.pop(norm(src), None)
            entry = {"type": kind, "path": path, "src": src, "at": now, "size": None, "size_at": now}
            self._pending[key] = entry

    def _expand_dir(self, folder: str, src_folder: Optional[str]) -> None:
        for base, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not self.ignored(os.path.join(base, d))]
            for name in files:
                path = os.path.join(base, name)
                if self.ignored(path):
                    continue
                if src_folder:
                    rel = os.path.relpath(path, folder)
                    self._put("moved", path, os.path.join(src_folder, rel))
                else:
                    self._put("created", path)

    def _on_event(self, event) -> None:
        kind = event.event_type
        src = os.fsdecode(event.src_path)
        dest = os.fsdecode(getattr(event, "dest_path", "") or "")
        if event.is_directory:
            if kind == "created" and not self.ignored(src):
                self._expand_dir(src, None)
            elif kind == "moved":
                if dest and not self.ignored(dest) and os.path.isdir(dest):
                    self._expand_dir(dest, None if self.ignored(src) else src)
                elif not self.ignored(src):
                    self._put("deleted_dir", src)
            elif kind == "deleted" and not self.ignored(src):
                self._put("deleted_dir", src)
            return
        if kind in ("created", "modified", "closed"):
            if not self.ignored(src):
                self._put("created", src)
        elif kind == "deleted":
            if not self.ignored(src):
                self._put("deleted", src)
        elif kind == "moved":
            src_ok, dest_ok = not self.ignored(src), bool(dest) and not self.ignored(dest)
            if src_ok and dest_ok:
                self._put("moved", dest, src)
            elif dest_ok:
                self._put("created", dest)
            elif src_ok:
                self._put("deleted", src)

    # -- settling
    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                ready = self._settle()
            except Exception:  # noqa: BLE001
                log.exception("watcher settle failed")
                continue
            for change in ready:
                if self._stop.is_set():
                    return
                try:
                    self.on_change(change)
                except Exception:  # noqa: BLE001 - a module error must not stop the watcher
                    log.exception("watcher callback failed")

    def _settle(self) -> list[dict]:
        now = time.monotonic()
        ready: list[dict] = []
        with self._lock:
            for key, entry in list(self._pending.items()):
                if now - entry["at"] < self.debounce:
                    continue
                if entry["type"] in ("deleted", "deleted_dir"):
                    continue
                try:
                    size = os.path.getsize(entry["path"])
                except OSError:
                    del self._pending[key]
                    continue
                if entry["size"] != size:
                    entry["size"], entry["size_at"] = size, now
                    continue
                if now - entry["size_at"] < self.stable:
                    continue
                if not _readable(entry["path"]):
                    entry["size_at"] = now
                    continue
                del self._pending[key]
                change = {"type": entry["type"], "path": entry["path"]}
                if entry["type"] == "moved":
                    change["src"] = entry["src"]
                else:
                    paired = self._pair_delete(entry["path"])
                    if paired is not None:
                        change = {"type": "moved", "path": entry["path"], "src": paired}
                ready.append(change)
            for key, entry in list(self._pending.items()):
                if entry["type"] in ("deleted", "deleted_dir") and now - entry["at"] >= self.debounce + self.hold:
                    del self._pending[key]
                    ready.append({"type": entry["type"], "path": entry["path"]})
        return ready

    def _pair_delete(self, path: str) -> Optional[str]:
        name = os.path.basename(path).casefold()
        for key, entry in list(self._pending.items()):
            if entry["type"] == "deleted" and os.path.basename(entry["path"]).casefold() == name:
                del self._pending[key]
                return entry["path"]
        return None


def _readable(path: str) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


# --- collect plan (docs/FILES.md 3.4) -----------------------------------------------------------------------------


@dataclass
class PlanEntry:
    node_id: str
    name: str
    source: str
    folder: str
    target: str
    size: int
    reuse: bool
    bin_from: str
    hash: Optional[str] = None
    reason: str = ""


@dataclass
class CollectPlan:
    entries: list[PlanEntry] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)  # {"node_id", "name", "path"}
    created_at: float = field(default_factory=time.time)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries if not entry.reuse)

    def groups(self) -> list[dict]:
        out: dict[str, dict] = {}
        for entry in self.entries:
            group = out.setdefault(entry.folder.casefold(), {"folder": entry.folder, "count": 0, "bytes": 0})
            group["count"] += 1
            group["bytes"] += 0 if entry.reuse else entry.size
        return sorted(out.values(), key=lambda g: g["folder"].casefold())

    def summary(self) -> str:
        count = len(self.entries)
        return f"{count} file{'s' if count != 1 else ''} · {fmt_size(self.total_bytes)}"


# --- pack (docs/FILES.md 3.5) -------------------------------------------------------------------------------------


PACK_SKIP_NAMES = ("*.part", "*.crdownload", "*.tmp", "~*", "thumbs.db", "desktop.ini")
DEFLATE_EXTENSIONS = {"json", "jsonl", "txt", "xml", "csv", "srt", "vtt", "md", "edl"}


def _skip_in_pack(name: str) -> bool:
    low = name.casefold()
    return any(fnmatch.fnmatchcase(low, pattern) for pattern in PACK_SKIP_NAMES)


def _skip_folder_in_pack(name: str) -> bool:
    low = name.casefold()
    return low in PACK_SKIP_FOLDERS or low.startswith("media cache")


def pack_files(root: str) -> list[tuple[str, str, int]]:
    """(absolute path, archive name, size) of every file a pack archive holds: the project folder minus Premiere's
    cache and preview folders and temporary files."""
    root = os.path.abspath(root)
    top = os.path.basename(root.rstrip("\\/"))
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skip_folder_in_pack(d))
        for name in sorted(files):
            if _skip_in_pack(name):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            out.append((path, f"{top}/{rel}", file_size(path)))
    return out


def free_zip_path(root: str) -> str:
    root = os.path.abspath(root).rstrip("\\/")
    candidate = root + ".zip"
    n = 1
    while os.path.exists(candidate) or os.path.exists(candidate + PART_SUFFIX):
        n += 1
        candidate = f"{root} ({n}).zip"
    return candidate


def make_zip(root: str, target: Optional[str] = None, *, on_bytes: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None, buffer_bytes: int = 16 << 20) -> str:
    """Write `<root>.zip` beside the root (a free name unless `target` is given; never overwrites) through a
    `.part` file. The only write outside the project root the module makes (docs/FILES.md 3.5)."""
    files = pack_files(root)
    total = sum(size for *_, size in files) or 1
    target = target or free_zip_path(root)
    if os.path.exists(target):
        raise CopyError(f"{target} already exists")
    part = target + PART_SUFFIX
    done = 0
    try:
        with zipfile.ZipFile(part, "x", allowZip64=True) as archive:
            for path, arcname, size in files:
                info = zipfile.ZipInfo.from_file(path, arcname)
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                info.compress_type = zipfile.ZIP_DEFLATED if ext in DEFLATE_EXTENSIONS else zipfile.ZIP_STORED
                with open(path, "rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                    while True:
                        if cancelled is not None and cancelled():
                            raise CopyCancelled()
                        chunk = src.read(max(64 * 1024, int(buffer_bytes)))
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        if on_bytes is not None:
                            on_bytes(done, total)
        os.rename(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return target


@dataclass
class PackReport:
    project: str
    root: str
    media_items: int
    inside: int
    outside: list[dict]
    offline: list[dict]
    total_bytes: int
    archive: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return not self.outside and not self.offline

    def line(self) -> str:
        if self.ok:
            text = f"Pack ready · {self.inside} files · {fmt_size(self.total_bytes)}"
            return text + (f" · {os.path.basename(self.archive)}" if self.archive else "")
        problems = self.offline + self.outside
        names = ", ".join(entry["name"] for entry in problems[:3])
        more = f" and {len(problems) - 3} more" if len(problems) > 3 else ""
        kinds = []
        if self.offline:
            kinds.append(f"{len(self.offline)} offline")
        if self.outside:
            kinds.append(f"{len(self.outside)} outside the folder")
        return f"Pack incomplete · {', '.join(kinds)}: {names}{more}"

    def text(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at))
        lines = [
            "Premiere Deck pack report",
            f"Project: {self.project}",
            f"Folder: {self.root}",
            f"Created: {stamp}",
            "",
            f"Media items: {self.media_items}",
            f"Inside the project folder: {self.inside}",
            f"Outside the project folder: {len(self.outside)}",
            f"Offline: {len(self.offline)}",
            f"Total media size: {fmt_size(self.total_bytes)}",
            f"Archive: {self.archive or 'not created'}",
            "",
            "Result: " + ("ready to share" if self.ok else "media missing, see below"),
        ]
        if self.offline:
            lines += ["", "Offline items:"] + [f"  {e['name']}  {e['path'] or '(no path)'}" for e in self.offline]
        if self.outside:
            lines += ["", "Outside the project folder:"] + [f"  {e['name']}  {e['path']}" for e in self.outside]
        return "\n".join(lines) + "\n"


def build_pack_report(project: str, root: str, items: Iterable, is_inside: Callable[[str], bool]) -> PackReport:
    """Counts over clip items with media (`ProjectItem`); file sizes are read from disk once per unique path."""
    inside, outside, offline = 0, [], []
    sizes: dict[str, int] = {}
    count = 0
    for item in items:
        path = item.media_path
        if not path:
            continue
        count += 1
        exists = os.path.isfile(path)
        if item.is_offline or not exists:
            offline.append({"node_id": item.node_id, "name": item.name, "path": path})
            continue
        if not is_inside(path):
            outside.append({"node_id": item.node_id, "name": item.name, "path": path})
            continue
        inside += 1
        sizes.setdefault(norm(path), file_size(path))
    return PackReport(project=project, root=root, media_items=count, inside=inside, outside=outside,
                      offline=offline, total_bytes=sum(sizes.values()))


__all__ = [name for name in globals() if not name.startswith("_")]
