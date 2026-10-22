"""Model files of the AI search module (docs/AI_SEARCH.md 1): the pinned manifest, status and verified download.

The manifest (`models.json` beside this file) pins the Hugging Face repository, revision, file list, sizes and
SHA-256. Files land in `<data>/models/<model id>/`; every verified file is recorded in `<data>/models/models.json`
(source URL, size, SHA-256), so `status()` stays cheap and a copied models folder is accepted after one hash.
Downloads use HTTPS only; `PREMIERE_DECK_MODELS_URL` replaces the base URL for tests and offline mirrors.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from premiere_deck.core.paths import atomic_write_json, read_json, replace_file

MANIFEST_FILE = Path(__file__).with_name("models.json")
URL_OVERRIDE_ENV = "PREMIERE_DECK_MODELS_URL"
RECORD_FILE = "models.json"
CHUNK = 1 << 20
USER_AGENT = "PremiereDeck-model-download"

STATE_MISSING = "missing"
STATE_READY = "ready"


class ModelError(Exception):
    """A download or verification failed; the message is one sentence for the user."""


class DownloadCancelled(ModelError):
    def __init__(self) -> None:
        super().__init__("Download cancelled")


@dataclass(frozen=True)
class ModelFile:
    name: str
    source: str
    role: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    id: str
    title: str
    repo: str
    revision: str
    url: str
    dim: int
    files: tuple[ModelFile, ...]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def file(self, role: str) -> ModelFile:
        for f in self.files:
            if f.role == role:
                return f
        raise KeyError(role)

    def url_of(self, f: ModelFile) -> str:
        base = os.environ.get(URL_OVERRIDE_ENV, "").strip()
        if base:
            return base.rstrip("/") + "/" + f.source
        url = self.url.format(repo=self.repo, revision=self.revision, source=f.source)
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ModelError("Model downloads use HTTPS only")
        return url


@dataclass(frozen=True)
class ModelStatus:
    state: str
    present_bytes: int
    total_bytes: int
    missing: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY


def load_manifest(path: Path = MANIFEST_FILE) -> Manifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    files = tuple(ModelFile(name=f["name"], source=f["source"], role=f["role"], size=int(f["size"]),
                            sha256=str(f["sha256"]).lower()) for f in data["files"])
    return Manifest(id=data["id"], title=data.get("title", data["id"]), repo=data["repo"], revision=data["revision"],
                    url=data["url"], dim=int(data["dim"]), files=files)


def model_dir(models_dir: Path, manifest: Manifest) -> Path:
    return Path(models_dir) / manifest.id


def _record_path(models_dir: Path) -> Path:
    return Path(models_dir) / RECORD_FILE


def _records(models_dir: Path) -> dict:
    data = read_json(_record_path(models_dir), {})
    return data if isinstance(data, dict) else {}


def _recorded_sha(models_dir: Path, manifest: Manifest, name: str) -> Optional[str]:
    entry = _records(models_dir).get(manifest.id)
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), dict):
        return None
    record = entry["files"].get(name)
    return str(record.get("sha256", "")).lower() if isinstance(record, dict) else None


def _write_record(models_dir: Path, manifest: Manifest, f: ModelFile, url: str) -> None:
    records = _records(models_dir)
    entry = records.get(manifest.id)
    if not isinstance(entry, dict):
        entry = {}
    files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
    files[f.name] = {"url": url, "size": f.size, "sha256": f.sha256, "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    entry.update({"title": manifest.title, "repo": manifest.repo, "revision": manifest.revision, "files": files})
    records[manifest.id] = entry
    atomic_write_json(_record_path(models_dir), records)


def status(models_dir: Path, manifest: Manifest) -> ModelStatus:
    """Cheap check: every file present with the pinned size and a matching verification record."""
    folder = model_dir(models_dir, manifest)
    present = 0
    missing = []
    for f in manifest.files:
        path = folder / f.name
        size = path.stat().st_size if path.is_file() else 0
        if size == f.size and _recorded_sha(models_dir, manifest, f.name) == f.sha256:
            present += size
            continue
        part = path.with_name(path.name + ".part")
        if part.is_file():
            present += min(part.stat().st_size, f.size)
        missing.append(f.name)
    return ModelStatus(STATE_READY if not missing else STATE_MISSING, present, manifest.total_size, tuple(missing))


def sha256_of(path: Path, cancelled: Optional[Callable[[], bool]] = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK * 4)
            if not block:
                break
            if cancelled is not None and cancelled():
                raise DownloadCancelled()
            digest.update(block)
    return digest.hexdigest()


def download(models_dir: Path, manifest: Manifest, *, on_progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             urlopen: Callable = urllib.request.urlopen) -> ModelStatus:
    """Fetch every missing file into `<models_dir>/<id>/` through `<name>.part` (resumed with a Range request when
    a part exists), verify size and SHA-256, then move into place and record. Raises ModelError / DownloadCancelled;
    a cancelled download keeps its parts for the next attempt, a failed checksum deletes the part."""
    folder = model_dir(models_dir, manifest)
    folder.mkdir(parents=True, exist_ok=True)
    total = manifest.total_size
    done = 0
    is_cancelled = cancelled or (lambda: False)

    def report(extra: int = 0) -> None:
        if on_progress is not None:
            on_progress(done + extra, total)

    for f in manifest.files:
        target = folder / f.name
        url = manifest.url_of(f)
        if target.is_file() and target.stat().st_size == f.size:
            if _recorded_sha(models_dir, manifest, f.name) == f.sha256 or sha256_of(target, is_cancelled) == f.sha256:
                _write_record(models_dir, manifest, f, url)
                done += f.size
                report()
                continue
        if target.exists():
            target.unlink()
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.is_file() else 0
        if have > f.size:
            part.unlink()
            have = 0
        if have < f.size:
            have = _fetch(url, part, have, f.size, is_cancelled, lambda n: report(n), urlopen)
        if have != f.size:
            part.unlink(missing_ok=True)
            raise ModelError(f"{f.name}: expected {f.size} bytes, received {have}")
        if sha256_of(part, is_cancelled) != f.sha256:
            part.unlink(missing_ok=True)
            raise ModelError(f"{f.name}: checksum does not match the pinned file")
        replace_file(part, target)
        _write_record(models_dir, manifest, f, url)
        done += f.size
        report()
    return status(models_dir, manifest)


def _fetch(url: str, part: Path, have: int, size: int, cancelled: Callable[[], bool],
           report: Callable[[int], None], urlopen: Callable) -> int:
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urlopen(request, timeout=30)
    except Exception as exc:  # noqa: BLE001 - urllib raises many kinds; the user needs one sentence
        raise ModelError(f"Download failed: {exc}") from exc
    with response:
        code = getattr(response, "status", None) or 200
        mode = "ab" if have and code == 206 else "wb"
        if mode == "wb":
            have = 0
        with open(part, mode) as handle:
            while True:
                if cancelled():
                    raise DownloadCancelled()
                try:
                    block = response.read(CHUNK)
                except Exception as exc:  # noqa: BLE001
                    raise ModelError(f"Download interrupted: {exc}") from exc
                if not block:
                    break
                handle.write(block)
                have += len(block)
                if have > size:
                    break
                report(have)
    return have


def remove(models_dir: Path, manifest: Manifest) -> None:
    """Delete the model's own files (never anything outside `<models_dir>/<id>/`) and its record."""
    folder = model_dir(models_dir, manifest)
    for f in manifest.files:
        for path in (folder / f.name, folder / (f.name + ".part")):
            if path.is_file():
                path.unlink()
    records = _records(models_dir)
    if records.pop(manifest.id, None) is not None:
        atomic_write_json(_record_path(models_dir), records)
