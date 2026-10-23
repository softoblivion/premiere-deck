"""ffmpeg / ffprobe discovery, download and the `Tools` service of the modules (docs/MODULES.md 3).

Discovery order: `<data>/bin` first, then PATH. When neither has both tools,
`ensure()` downloads the static build of yt-dlp/FFmpeg-Builds once, verifies it against the release's
`checksums.sha256`, extracts the two exes through `.part` files and records them in `bin/versions.json`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from premiere_deck.core.media import MediaInfo

from . import procs
from .paths import AppPaths, atomic_write_json, remove_path, replace_file

log = logging.getLogger("premiere_deck.core.tools")

FFMPEG_BASE = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
FFMPEG_ZIP = "ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_SUMS = "checksums.sha256"
USER_AGENT = "PremiereDeck/1 (Windows)"
TOOL_NAMES = ("ffmpeg", "ffprobe")

ByteProgress = Callable[[int, Optional[int]], None]


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class FFmpeg:
    ffmpeg: str
    ffprobe: str
    source: str  # "bin" | "path"


def find_ffmpeg(paths: AppPaths, use_path: bool = True) -> Optional[FFmpeg]:
    """`bin/ffmpeg.exe` + `bin/ffprobe.exe`, else both from PATH; None when either is missing."""
    own_ff, own_fp = paths.binary("ffmpeg.exe"), paths.binary("ffprobe.exe")
    if own_ff.is_file() and own_fp.is_file():
        return FFmpeg(str(own_ff), str(own_fp), "bin")
    if use_path:
        ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if ff and fp:
            return FFmpeg(ff, fp, "path")
    return None


# --- downloads -------------------------------------------------------------------------------------------------

def http_get_text(url: str, timeout: float = 30.0, limit: int = 1 << 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit).decode("utf-8", errors="replace")


def http_download(url: str, dest: Path, on_progress: Optional[ByteProgress] = None,
                  cancel: Optional[threading.Event] = None, timeout: float = 60.0) -> str:
    """Download `url` into `dest` (a `.part` path); returns the SHA-256 hex of the bytes."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    done = 0
    last = 0.0
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        total_raw = resp.headers.get("Content-Length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else None
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = resp.read(1 << 17)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if on_progress is not None and now - last >= 0.15:
                last = now
                on_progress(done, total)
        out.flush()
        os.fsync(out.fileno())
    if on_progress is not None:
        on_progress(done, done)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """`<sha256>  <name>` lines (sha256sum format, optional `*` before the name)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match:
            result[match.group(2)] = match.group(1).lower()
    return result


def extract_ffmpeg(zip_path: Path, bin_dir: Path) -> dict[str, Path]:
    """Extract `bin/ffmpeg.exe` and `bin/ffprobe.exe` of a release zip into `bin_dir` through `.part` files."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                norm = "/" + member.filename.replace("\\", "/")
                base = norm.rsplit("/", 1)[-1].lower()
                if base in ("ffmpeg.exe", "ffprobe.exe") and "/bin/" in norm:
                    part = bin_dir / (base + ".part")
                    with zf.open(member) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    extracted[base] = part
        if set(extracted) != {"ffmpeg.exe", "ffprobe.exe"}:
            raise DownloadError("the ffmpeg archive does not contain bin/ffmpeg.exe and bin/ffprobe.exe")
        final: dict[str, Path] = {}
        for base, part in extracted.items():
            target = bin_dir / base
            replace_file(part, target)
            final[base] = target
        return final
    finally:
        for part in extracted.values():
            remove_path(part)


def download_ffmpeg(paths: AppPaths, on_progress: Optional[ByteProgress] = None,
                    cancel: Optional[threading.Event] = None, base_url: str = FFMPEG_BASE) -> FFmpeg:
    """Download, verify and install the static ffmpeg build into `<data>/bin`; record it in `versions.json`."""
    sums = parse_sums(http_get_text(base_url + FFMPEG_SUMS))
    expected = sums.get(FFMPEG_ZIP)
    if not expected:
        raise DownloadError(f"{FFMPEG_ZIP} is not listed in {FFMPEG_SUMS}")
    paths.cache_root.mkdir(parents=True, exist_ok=True)
    part = paths.cache_root / (FFMPEG_ZIP + ".part")
    try:
        got = http_download(base_url + FFMPEG_ZIP, part, on_progress, cancel)
        if got != expected:
            raise DownloadError(f"SHA-256 mismatch of {FFMPEG_ZIP}")
        exes = extract_ffmpeg(part, paths.bin_dir)
    finally:
        remove_path(part)
    version = ffmpeg_version(str(exes["ffmpeg.exe"]))
    atomic_write_json(paths.versions_file, {"ffmpeg": {
        "version": version, "source": base_url + FFMPEG_ZIP, "zip_sha256": expected,
        "ffmpeg_sha256": sha256_file(exes["ffmpeg.exe"]), "ffprobe_sha256": sha256_file(exes["ffprobe.exe"]),
        "installed_at": time.time()}})
    return FFmpeg(str(exes["ffmpeg.exe"]), str(exes["ffprobe.exe"]), "bin")


def parse_ffmpeg_version(text: str) -> Optional[str]:
    match = re.search(r"ffmpeg version (\S+)", text)
    if not match:
        return None
    raw = match.group(1)
    semver = re.match(r"n?(\d+(?:\.\d+)+)", raw)
    if semver:
        return semver.group(1)
    nightly = re.match(r"(N-\d+)", raw)
    return nightly.group(1) if nightly else raw


def ffmpeg_version(ffmpeg: str) -> Optional[str]:
    res = procs.run([ffmpeg, "-hide_banner", "-version"], capture=True, timeout=15)
    if res.returncode != 0:
        return None
    return parse_ffmpeg_version(res.stdout.decode("utf-8", errors="replace"))


# --- the Tools service ------------------------------------------------------------------------------------------

class HostTools:
    """`ctx.tools` (modules/base.Tools). Thread-safe; `ensure()` downloads at most once at a time."""

    def __init__(self, paths: AppPaths, use_path: bool = True) -> None:
        self.paths = paths
        self.use_path = use_path
        self._lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._pipe_job: Optional[procs.JobObject] = None
        self._found: Optional[FFmpeg] = None
        self._version: Optional[str] = None
        self.refresh()

    def refresh(self) -> Optional[FFmpeg]:
        found = find_ffmpeg(self.paths, self.use_path)
        with self._lock:
            if found is None or self._found is None or found.ffmpeg != self._found.ffmpeg:
                self._version = None
            self._found = found
        return found

    def path_of(self, tool: str) -> Optional[str]:
        found = self._found
        if found is None or tool not in TOOL_NAMES:
            return None
        return found.ffmpeg if tool == "ffmpeg" else found.ffprobe

    def has(self, tool: str) -> bool:
        return self.path_of(tool) is not None

    def ensure(self, tool: str, on_progress: Optional[Callable[[float, str], None]] = None) -> str:
        if tool not in TOOL_NAMES:
            raise ValueError(f"unknown tool {tool!r}")
        path = self.path_of(tool)
        if path:
            return path
        with self._download_lock:  # a second caller waits for the first download instead of starting its own
            if self.refresh() is None:
                def progress(done: int, total: Optional[int]) -> None:
                    if on_progress is not None:
                        value = (done / total) if total else 0.0
                        on_progress(value, f"Downloading ffmpeg · {done / 1048576:.1f} MB")

                self._found = download_ffmpeg(self.paths, progress)
        path = self.path_of(tool)
        if not path:
            raise DownloadError("ffmpeg is not available")
        return path

    def status(self) -> dict:
        """For the host window (docs/BRIDGE_API.md 3.2): source, path and version of ffmpeg."""
        found = self._found
        if found is None:
            return {"state": "missing", "source": None, "path": None, "version": None}
        if self._version is None:
            self._version = ffmpeg_version(found.ffmpeg)
        return {"state": "ready", "source": found.source, "path": found.ffmpeg, "version": self._version}

    def ffprobe(self, path: str) -> MediaInfo:
        from .probe import probe

        return probe(path, self.path_of("ffprobe"))

    def ffmpeg_pipe(self, args: Sequence[str]) -> subprocess.Popen:
        """Start ffmpeg with `args` (the executable is prepended) and hand the process to the caller: stdout and
        stderr are pipes, no console window, UTF-8 environment, stdin closed. The process is registered in a Job
        Object that lives as long as this service, so the host's exit kills it; the caller reads the pipes, waits
        and calls `kill()` on a cancel. Raises FileNotFoundError when ffmpeg is missing and OSError when it cannot
        start."""
        exe = self.path_of("ffmpeg")
        if not exe:
            raise FileNotFoundError("ffmpeg is not available")
        with self._lock:
            if self._pipe_job is None:
                self._pipe_job = procs.JobObject()
            job = self._pipe_job
        return procs.spawn([exe, *[str(a) for a in args]], job=job)

    def ffmpeg(self, args: Sequence[str], *, timeout: Optional[float] = None,
               on_line: Optional[Callable[[str], None]] = None, cancel: Optional[threading.Event] = None) -> int:
        """Run ffmpeg with `args` (the executable is prepended); `on_line` gets stdout and stderr lines. Returns the
        exit code; -1 when cancelled or timed out, -2 when ffmpeg is missing or cannot start."""
        exe = self.path_of("ffmpeg")
        if not exe:
            return -2
        job = procs.JobObject()
        try:
            try:
                proc = procs.spawn([exe, *[str(a) for a in args]], job=job)
            except OSError:
                log.exception("ffmpeg could not start")
                return -2
            out = procs.LinePump(proc.stdout, on_line, keep=20)  # type: ignore[arg-type]
            err = procs.LinePump(proc.stderr, on_line)  # type: ignore[arg-type]
            deadline = time.monotonic() + timeout if timeout else None
            while True:
                try:
                    proc.wait(timeout=0.1)
                    break
                except Exception:  # subprocess.TimeoutExpired
                    pass
                if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline):
                    job.kill()
                    proc.wait(timeout=10)
                    out.join(timeout=5)
                    err.join(timeout=5)
                    return -1
            out.join(timeout=5)
            err.join(timeout=5)
            if proc.returncode != 0:
                log.info("ffmpeg exited %s: %s", proc.returncode, " | ".join(err.lines()[-3:]))
            return int(proc.returncode)
        finally:
            job.close()
