"""AI search module (docs/AI_SEARCH.md): model download, per-project frame index, text queries, panel section and
host page.

Threads: hooks run on the module thread; the download, indexing, search and thumbnail work runs in jobs. Job
callbacks arrive on the module thread (the host marshals them); the thumbnail job writes into `_thumbs` under the
module lock and calls `ctx.ui.invalidate()`, which is coalesced and thread-safe.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from premiere_deck.host import protocol as P
from premiere_deck.modules.ai_search import embed as emb
from premiere_deck.modules.ai_search import frames as fr
from premiere_deck.modules.ai_search import index as ix
from premiere_deck.modules.ai_search import models as md
from premiere_deck.modules.ai_search import query as qy
from premiere_deck.modules.base import ActionResult, Module, SettingField
from premiere_deck.premiere.model import PremiereError, ProjectInfo

THUMB_WIDTH = 160
TOP_HITS = 30
PER_CLIP = 3
MAX_TABLE_ROWS = 500

# Settings (docs/AI_SEARCH.md 6): key -> (default, validator returning the clean value or raising ValueError).


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(value)


def _number(low: float, high: float, integer: bool = False) -> Callable[[Any], float]:
    def check(value: Any):
        if isinstance(value, bool):
            raise ValueError(value)
        number = float(value)
        if not (low <= number <= high) or number != number:
            raise ValueError(value)
        return int(round(number)) if integer else number
    return check


def _choice(options: tuple) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if str(value) not in options:
            raise ValueError(value)
        return str(value)
    return check


def _bins(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(value)
    return [v.strip().strip("/") for v in value if v.strip().strip("/")]


CLIP_PAD_OPTIONS = ("1", "2", "3", "5")
HOST_SETTINGS: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "provider": (emb.PROVIDER_AUTO, _choice(emb.PROVIDERS)),
    "step_seconds": (2.0, _number(0.5, 60.0)),
    "max_frames_per_clip": (600, _number(1, 10000, integer=True)),
    "clip_pad": (2.0, _number(0.1, 30.0)),
    "thumb_cache_mb": (200, _number(10, 10000, integer=True)),
}
PROJECT_SETTINGS: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "enabled": (True, _bool),
    "auto_index": (True, _bool),
    "exclude_bins": (["Overlays", "Graphics"], _bins),
    "min_duration": (1.0, _number(0.0, 3600.0)),
}


@dataclass
class Hit:
    key: str  # quick hash of the clip
    node_id: str
    name: str
    path: str
    kind: str
    duration: Optional[float]
    times: tuple[float, ...]
    scores: tuple[float, ...]
    bar: float

    @property
    def time(self) -> float:
        return self.times[0]

    def subtitle(self) -> str:
        text = qy.timecode(self.time)
        if len(self.times) > 1:
            text += " · also " + ", ".join(qy.timecode(t) for t in self.times[1:])
        return text


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.0f}"


class AiSearchModule(Module):
    id = "search"
    title = "AI search"
    description = "Finds frames in the project's footage by a text description, on this computer."
    order = 30
    events = ["project.items_added", "project.changed"]
    requires = ["ffmpeg", "gpu"]
    default_enabled = True

    AUTO_INDEX_DELAY = 10.0  # seconds after the last items_added before new clips are indexed

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.manifest: Optional[md.Manifest] = None
        self.embedder: Optional[emb.Embedder] = None
        self.threshold = qy.DEFAULT_THRESHOLD
        self._lock = threading.RLock()
        self._project: Optional[ProjectInfo] = None
        self._catalog: Optional[ix.Catalog] = None
        self._model: Optional[md.ModelStatus] = None
        self._model_error = ""
        self._download_job = None
        self._download_progress: tuple[int, int] = (0, 0)
        self._candidates: list[ix.Candidate] = []
        self._skipped: list[ix.Skipped] = []
        self._pending: list[ix.Candidate] = []
        self._node_by_path: dict[str, str] = {}
        self._index_job = None
        self._index_progress: tuple[Optional[float], str] = (None, "")
        self._index_failed: dict[str, str] = {}
        self._index_error = ""
        self._refresh_job = None
        self._rescan_after_index = False
        self._search_job = None
        self._thumb_job = None
        self._search_seq = 0
        self._query = ""
        self._searched = ""
        self._hits: list[Hit] = []
        self._thumbs: dict[tuple[str, float], str] = {}
        self._text_cache: Optional[qy.TextCache] = None
        self._finished: set[str] = set()

    # --- settings ----------------------------------------------------------------------------------------------

    def setting(self, key: str) -> Any:
        """A host or project setting, validated; a missing or bad value falls back to the default."""
        if key in HOST_SETTINGS:
            default, check = HOST_SETTINGS[key]
            store = self.ctx.settings
        else:
            default, check = PROJECT_SETTINGS[key]
            store = getattr(self.ctx.settings, "project", None)
        try:
            raw = store.get(key, default) if store is not None else default
        except Exception:  # noqa: BLE001 - no project open, broken store
            raw = default
        try:
            return check(raw)
        except (TypeError, ValueError):
            if raw is not default:
                self.log.warning("setting %s has a bad value %r; using %r", key, raw, default)
            return check(default) if not isinstance(default, list) else list(default)

    def _set_setting(self, key: str, value: Any) -> None:
        if key in HOST_SETTINGS:
            clean = HOST_SETTINGS[key][1](value)
            self.ctx.settings.set(key, clean)
        elif key in PROJECT_SETTINGS:
            clean = PROJECT_SETTINGS[key][1](value)
            self.ctx.settings.project.set(key, clean)
        else:
            raise KeyError(key)
        if key == "provider":
            self._reset_embedder()

    def settings_schema(self) -> list[SettingField]:
        providers = [{"value": emb.PROVIDER_AUTO, "label": "Automatic"},
                     {"value": emb.PROVIDER_DIRECTML, "label": "GPU (DirectML)"},
                     {"value": emb.PROVIDER_CPU, "label": "CPU"}]
        return [
            SettingField("provider", "Run the model on", "select", emb.PROVIDER_AUTO, options=providers,
                         hint="Automatic uses the GPU when DirectML is available."),
            SettingField("step_seconds", "Frame step, seconds", "int", 2,
                         hint="One frame every this many seconds, plus every cut."),
            SettingField("max_frames_per_clip", "Frames per clip, at most", "int", 600,
                         hint="Long clips get a wider step so the whole clip is covered."),
            SettingField("clip_pad", "Inserted length, seconds each side", "select", "2",
                         options=[{"value": v, "label": f"{v} s"} for v in CLIP_PAD_OPTIONS]),
            SettingField("thumb_cache_mb", "Thumbnail cache, MB", "int", 200),
            SettingField("enabled", "Search this project", "bool", True, scope="project"),
            SettingField("auto_index", "Index new clips", "bool", True, scope="project",
                         hint="New clips are indexed 10 seconds after they are imported."),
            SettingField("exclude_bins", "Skip bins", "text", "Overlays, Graphics", scope="project",
                         hint="Comma-separated bin names or paths."),
            SettingField("min_duration", "Skip clips shorter than, seconds", "int", 1, scope="project"),
        ]

    # --- lifecycle ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self.manifest is None:
            self.manifest = md.load_manifest()
        self._refresh_model()

    def stop(self) -> None:
        for job in (self._download_job, self._index_job, self._refresh_job, self._search_job, self._thumb_job):
            if job is not None:
                job.cancel()
        self._download_job = self._index_job = self._refresh_job = self._search_job = self._thumb_job = None
        self._reset_embedder()

    def project_opened(self, project: ProjectInfo) -> None:
        self._close_project()
        self._project = project
        if self.manifest is None:
            self.manifest = md.load_manifest()
        self._catalog = ix.Catalog.for_project(project.deck_dir, self.manifest.id, self.manifest.dim)
        for note in self._catalog.notes:
            self.log.info("index of %s: %s", project.name, note)
        self._refresh_items()
        if self._auto_ready():
            self._start_index(auto=True)

    def project_closed(self) -> None:
        self._close_project()

    def _close_project(self) -> None:
        for job in (self._index_job, self._refresh_job, self._search_job, self._thumb_job):
            if job is not None:
                job.cancel()
        self._index_job = self._refresh_job = self._search_job = self._thumb_job = None
        self._project = None
        self._catalog = None
        self._candidates, self._skipped, self._pending = [], [], []
        self._node_by_path = {}
        self._hits, self._thumbs = [], {}
        self._query = self._searched = ""
        self._index_failed, self._index_error = {}, ""
        self._index_progress = (None, "")
        self._rescan_after_index = False

    def on_event(self, name: str, data: Any) -> None:
        if name in ("project.items_added", "project.changed") and self._project is not None:
            if self._index_job is not None:
                self._rescan_after_index = True  # the running job has a fixed plan; look again when it ends
            else:
                self._schedule_refresh()

    # --- model -------------------------------------------------------------------------------------------------

    def _refresh_model(self) -> None:
        if self.manifest is not None:
            self._model = md.status(self.ctx.paths.models_dir, self.manifest)

    @property
    def model_ready(self) -> bool:
        return self._model is not None and self._model.ready

    def _get_embedder(self) -> emb.Embedder:
        with self._lock:
            if self.embedder is None:
                self.embedder = emb.ClipOnnxEmbedder(self.manifest, self.ctx.paths.models_dir,
                                                     self.setting("provider"))
            if self._text_cache is None:
                self._text_cache = qy.TextCache(self.embedder.embed_text)
            return self.embedder

    def _reset_embedder(self) -> None:
        with self._lock:
            if self.embedder is not None and hasattr(self.embedder, "close"):
                self.embedder.close()
            self.embedder = None
            self._text_cache = None

    def _start_download(self) -> ActionResult:
        if self.model_ready:
            return ActionResult(toast=("neutral", "The search model is already downloaded"))
        if self._download_job is not None:
            return ActionResult(rerender=False)
        self._model_error = ""
        models_dir = self.ctx.paths.models_dir
        manifest = self.manifest

        def work(job):
            return md.download(models_dir, manifest, cancelled=lambda: job.cancelled,
                               on_progress=lambda done, total: job.progress(done / total if total else None,
                                                                            f"{_mb(done)} of {_mb(total)} MB"))

        def progress(job, value, label):
            if self._model is not None:
                self._download_progress = (int((value or 0) * manifest.total_size), manifest.total_size)
            self.ctx.ui.invalidate()

        def done(job, result):
            self._finish(job)
            self._download_job = None
            self._refresh_model()
            if self.model_ready:
                self.ctx.ui.toast("ok", "Search model ready")
                if self._auto_ready():
                    self._start_index(auto=True)
            self.ctx.ui.invalidate()

        def error(job, exc):
            self._finish(job)
            self._download_job = None
            self._refresh_model()
            if isinstance(exc, md.DownloadCancelled) or getattr(job, "cancelled", False):
                self.ctx.ui.toast("neutral", "Download stopped; it continues where it left off next time")
            else:
                self._model_error = str(exc) or type(exc).__name__
                self.ctx.ui.toast("error", f"AI search: {self._model_error}")
            self.ctx.ui.invalidate()

        self._download_progress = (self._model.present_bytes if self._model else 0, manifest.total_size)
        job = self.ctx.jobs.submit(work, title="Downloading the search model", priority="normal",
                                   on_progress=progress, on_done=done, on_error=error)
        if not self._is_finished(job):
            self._download_job = job
        return ActionResult()

    # --- items and index ---------------------------------------------------------------------------------------

    def _auto_ready(self) -> bool:
        return (self.model_ready and self._project is not None and self.setting("enabled")
                and self.setting("auto_index"))

    def _refresh_items(self) -> None:
        """Snapshot -> candidates, skipped, pending (module thread: the facade call and one stat per clip)."""
        if self._project is None or self._catalog is None:
            return
        try:
            snapshot = self.ctx.premiere.snapshot()
        except PremiereError as exc:
            self.log.warning("snapshot failed: %s", exc.message)
            return
        candidates, skipped = ix.select_candidates(snapshot.items, exclude_bins=self.setting("exclude_bins"),
                                                   min_duration=self.setting("min_duration"))
        self._candidates, self._skipped = candidates, skipped
        self._node_by_path = {ix.norm_path(c.path): c.node_id for c in candidates}
        self._pending = ix.pending(self._catalog, candidates)

    def _schedule_refresh(self) -> None:
        if self._refresh_job is not None:
            self._refresh_job.cancel()
        delay = float(self.AUTO_INDEX_DELAY)

        def wait(job):
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if job.cancelled:
                    return False
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            return not job.cancelled

        def done(job, go):
            self._finish(job)
            if self._refresh_job is job:
                self._refresh_job = None
            if not go or self._project is None:
                return
            self._refresh_items()
            if self._auto_ready() and self._pending:
                self._start_index(auto=True)
            self.ctx.ui.invalidate()

        def error(job, exc):
            self._finish(job)
            if self._refresh_job is job:
                self._refresh_job = None

        job = self.ctx.jobs.submit(wait, title="Waiting for new clips", priority="low", on_done=done, on_error=error)
        if not self._is_finished(job):
            self._refresh_job = job

    def _high_priority_busy(self) -> bool:
        """True while a high-priority job (a Files copy) is queued or running (`ctx.jobs.high_priority_running()`,
        docs/MODULES.md 3); indexing runs at normal or low priority, so it never waits on itself."""
        try:
            return bool(self.ctx.jobs.high_priority_running())
        except Exception:  # noqa: BLE001 - a failing probe must not stop indexing
            return False

    def _start_index(self, *, auto: bool = False, rebuild: bool = False) -> ActionResult:
        if not self.model_ready:
            return ActionResult(toast=("warn", "Download the search model first"))
        if self._project is None or self._catalog is None:
            return ActionResult(toast=("warn", "Open a project first"))
        if not self.setting("enabled"):
            return ActionResult(toast=("warn", "AI search is off for this project"))
        if self._index_job is not None:
            return ActionResult(toast=("neutral", "Indexing is already running"), rerender=False)
        self._refresh_items()
        if rebuild:
            self._catalog.clear()
            self._pending = list(self._candidates)
        plan = list(self._pending)
        if not plan:
            return ActionResult(toast=None if auto else ("ok", "The index is up to date"))
        catalog = self._catalog
        tools = self.ctx.tools
        step = self.setting("step_seconds")
        max_frames = self.setting("max_frames_per_clip")
        embedder = self._get_embedder()
        self._index_failed, self._index_error = {}, ""
        self._index_progress = (0.0, f"Indexing 1 of {len(plan)} clips")

        def probe(path: str) -> Optional[float]:
            return tools.ffprobe(path).duration

        def work(job):
            ffmpeg = tools.path_of("ffmpeg") or tools.ensure("ffmpeg")
            if hasattr(embedder, "load"):
                embedder.load()
            return ix.run_index(job, catalog=catalog, candidates=plan, embed=embedder.embed_images, ffmpeg=ffmpeg,
                                step=step, max_frames=max_frames, probe_duration=probe,
                                paused=self._high_priority_busy, pipe=tools.ffmpeg_pipe)

        def progress(job, value, label):
            self._index_progress = (value, label)
            self.ctx.ui.invalidate()

        def done(job, result: ix.IndexResult):
            self._finish(job)
            if self._index_job is job:
                self._index_job = None
            if self._catalog is not catalog:
                return  # the project changed meanwhile
            self._index_failed = dict(result.failed)
            self._index_progress = (None, "")
            self._pending = ix.pending(catalog, self._candidates)
            if result.cancelled:
                self.ctx.ui.toast("neutral", f"Indexing stopped · {result.indexed} clips indexed")
            elif result.failed:
                self.ctx.ui.toast("warn", f"Indexed {result.indexed} clips · {len(result.failed)} could not be read")
            elif not auto:
                self.ctx.ui.toast("ok", f"Indexed {result.indexed} clips" if result.indexed else
                                  "The index is up to date")
            self.log.info("index: %d new, %d known, %d failed, %d frames in %.1f s", result.indexed, result.known,
                          len(result.failed), result.frames, result.seconds)
            if self._rescan_after_index and not result.cancelled:
                self._rescan_after_index = False
                self._schedule_refresh()
            self.ctx.ui.invalidate()

        def error(job, exc):
            self._finish(job)
            if self._index_job is job:
                self._index_job = None
            self._index_progress = (None, "")
            if getattr(job, "cancelled", False):
                return
            self._index_error = str(exc) or type(exc).__name__
            self.log.warning("indexing failed: %s", self._index_error)
            self.ctx.ui.toast("error", f"AI search: {self._index_error}")
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title="Indexing footage", priority="low", on_progress=progress,
                                   on_done=done, on_error=error)
        if not self._is_finished(job):
            self._index_job = job
        return ActionResult()

    # --- search ------------------------------------------------------------------------------------------------

    def _start_search(self, text: str) -> ActionResult:
        text = " ".join(str(text or "").split())
        self._query = text
        if not text:
            return self._clear_results()
        if not self.model_ready:
            return ActionResult(toast=("warn", "Download the search model first"))
        if self._catalog is None:
            return ActionResult(toast=("warn", "Open a project first"))
        if self._catalog.count() == 0:
            self._searched, self._hits = text, []
            return ActionResult(toast=("neutral", "Nothing is indexed yet; use Index project"))
        if self._search_job is not None:
            self._search_job.cancel()
        if self._thumb_job is not None:
            self._thumb_job.cancel()
            self._thumb_job = None
        self._search_seq += 1
        seq = self._search_seq
        catalog = self._catalog
        self._get_embedder()
        cache = self._text_cache
        threshold = self.threshold

        def work(job):
            vector = cache.get(text)
            ranked = qy.rank(catalog.matrix(), vector, threshold=threshold, top=TOP_HITS, per_clip=PER_CLIP)
            return ranked

        def done(job, ranked):
            self._finish(job)
            if self._search_job is job:
                self._search_job = None
            if seq != self._search_seq or catalog is not self._catalog:
                return
            self._searched = text
            self._hits = self._make_hits(catalog, ranked)
            self._start_thumbs(seq)
            self.ctx.ui.invalidate()

        def error(job, exc):
            self._finish(job)
            if self._search_job is job:
                self._search_job = None
            if getattr(job, "cancelled", False) or seq != self._search_seq:
                return
            self.ctx.ui.toast("error", f"AI search: {exc}")
            self.ctx.ui.invalidate()

        job = self.ctx.jobs.submit(work, title="Searching", priority="normal", on_done=done, on_error=error)
        if not self._is_finished(job):
            self._search_job = job
        return ActionResult()

    def _make_hits(self, catalog: ix.Catalog, ranked: list[qy.Ranked]) -> list[Hit]:
        hits = []
        best = ranked[0].score if ranked else 0.0
        for r in ranked:
            entry = catalog.entry(r.key)
            if entry is None:
                continue
            path_key = ix.norm_path(entry["path"])
            if self._node_by_path and path_key not in self._node_by_path:
                continue  # indexed earlier, no longer a searchable clip of this project
            node_id = self._node_by_path.get(path_key, entry.get("node_id", ""))
            hits.append(Hit(r.key, node_id, entry.get("name") or os.path.basename(entry["path"]), entry["path"],
                            entry.get("kind", "video"), entry.get("duration"), r.times, r.scores,
                            qy.bar_value(r.score, best, self.threshold)))
        return hits

    def _start_thumbs(self, seq: int) -> None:
        hits = [h for h in self._hits if (h.key, round(h.time, 1)) not in self._thumbs]
        if not hits:
            return
        tools = self.ctx.tools
        cache = qy.ThumbCache(self.ctx.paths.cache_dir(self.id), self.setting("thumb_cache_mb") * 1_000_000)

        def work(job):
            ffmpeg = tools.path_of("ffmpeg") or tools.ensure("ffmpeg")
            made = 0
            for hit in hits:
                if job.cancelled or seq != self._search_seq:
                    break
                try:
                    jpeg = cache.get(hit.key, hit.time, THUMB_WIDTH,
                                     lambda h=hit: fr.thumbnail_jpeg(ffmpeg, h.path, h.time, THUMB_WIDTH,
                                                                     image=h.kind == "image"))
                except (fr.FrameError, OSError) as exc:
                    self.log.debug("thumb of %s at %.1f failed: %s", hit.name, hit.time, exc)
                    continue
                with self._lock:
                    self._thumbs[(hit.key, round(hit.time, 1))] = qy.data_url(jpeg)
                    if len(self._thumbs) > 300:
                        for stale in list(self._thumbs)[:100]:
                            self._thumbs.pop(stale, None)
                made += 1
                self.ctx.ui.invalidate()
            return made

        def finished(job, *_):
            self._finish(job)
            if self._thumb_job is job:
                self._thumb_job = None

        job = self.ctx.jobs.submit(work, title="Search thumbnails", priority="normal", on_done=finished,
                                   on_error=finished)
        if not self._is_finished(job):
            self._thumb_job = job

    def _clear_results(self) -> ActionResult:
        self._search_seq += 1
        for job in (self._search_job, self._thumb_job):
            if job is not None:
                job.cancel()
        self._search_job = self._thumb_job = None
        self._query = self._searched = ""
        self._hits = []
        return ActionResult()

    def search(self, text: str) -> list[Hit]:
        """Synchronous search for scripts and the evaluation (no thumbnails)."""
        if self._catalog is None:
            return []
        self._get_embedder()
        ranked = qy.rank(self._catalog.matrix(), self._text_cache.get(text), threshold=self.threshold,
                         top=TOP_HITS, per_clip=PER_CLIP)
        return self._make_hits(self._catalog, ranked)

    # --- hit actions -------------------------------------------------------------------------------------------

    def _hit(self, row: Optional[str]) -> Optional[Hit]:
        return next((h for h in self._hits if h.key == row), None)

    def _hit_action(self, action: str, row: Optional[str], value: Any) -> ActionResult:
        hit = self._hit(row)
        if hit is None:
            return ActionResult(toast=("warn", "That result is gone; search again"))
        seconds = hit.time
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            seconds = float(value)
        if not hit.node_id:
            return ActionResult(toast=("warn", f"{hit.name} is not in the project anymore"))
        try:
            if action == "open":
                self.ctx.premiere.open_in_source(hit.node_id, seconds=seconds)
                return ActionResult(toast=("ok", f"Opened {hit.name} at {qy.timecode(seconds)}"))
            if action == "insert":
                pad = self.setting("clip_pad")
                if hit.kind == "image":
                    start, end = 0.0, 2 * pad
                else:
                    start = max(0.0, seconds - pad)
                    end = seconds + pad
                    if hit.duration:
                        end = min(end, float(hit.duration))
                    if end <= start:
                        end = start + 2 * pad
                self.ctx.premiere.insert_clip(hit.node_id, start=start, end=end)
                return ActionResult(toast=("ok", f"Inserted {end - start:.0f} s of {hit.name} at the playhead"))
            if action == "mark":
                name = f"Deck: {self._searched or self._query}"[:120]
                self.ctx.premiere.add_marker(hit.node_id, seconds, name)
                return ActionResult(toast=("ok", f"Marker added to {hit.name} at {qy.timecode(seconds)}"))
        except PremiereError as exc:
            return ActionResult(toast=("error", f"Premiere: {exc.message}"))
        return ActionResult(toast=("warn", f"AI search: unknown action {action}"), rerender=False)

    # --- actions -----------------------------------------------------------------------------------------------

    def _finish(self, job) -> None:
        self._finished.add(getattr(job, "id", id(job)))

    def _is_finished(self, job) -> bool:
        return getattr(job, "id", id(job)) in self._finished

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        local = action_id[5:] if action_id.startswith("host:") else action_id
        local = self.strip_prefix(local)
        if local.startswith("host:"):
            local = local[5:]
        if local == "query":
            return self._start_search(value if isinstance(value, str) else "")
        if local == "clear":
            return self._clear_results()
        if local in ("open", "insert", "mark"):
            return self._hit_action(local, row, value)
        if local == "index":
            return self._start_index()
        if local == "rebuild":
            return self._start_index(rebuild=True)
        if local == "cancel":
            if self._index_job is not None:
                self._index_job.cancel()
            return ActionResult()
        if local == "download":
            return self._start_download()
        if local == "cancel_download":
            if self._download_job is not None:
                self._download_job.cancel()
            return ActionResult()
        if local == "remove_model":
            if self._index_job is not None or self._download_job is not None:
                return ActionResult(toast=("warn", "Stop indexing and downloads first"))
            self._reset_embedder()
            md.remove(self.ctx.paths.models_dir, self.manifest)
            self._refresh_model()
            return ActionResult(toast=("ok", "Search model removed"))
        if local == "clear_thumbs":
            qy.ThumbCache(self.ctx.paths.cache_dir(self.id), 0).clear()
            self._thumbs = {}
            return ActionResult(toast=("ok", "Thumbnail cache cleared"))
        if local in ("auto_index", "enabled"):
            if self._project is None:
                return ActionResult(toast=("warn", "Open a project first"))
            try:
                self._set_setting(local, value)
            except (TypeError, ValueError):
                return ActionResult(toast=("warn", "AI search: that value is not valid"), rerender=False)
            if local == "auto_index" and self.setting("auto_index") and self._auto_ready() and self._pending:
                self._start_index(auto=True)
            return ActionResult()
        if local == "setting":
            return self._setting_action(value)
        return super().on_action(action_id, value, row)

    def _setting_action(self, value: Any) -> ActionResult:
        if not isinstance(value, dict) or "key" not in value:
            return ActionResult(toast=("warn", "AI search: that value is not valid"), rerender=False)
        key = str(value["key"])
        if key in PROJECT_SETTINGS and self._project is None:
            return ActionResult(toast=("warn", "Open a project first"))
        try:
            self._set_setting(key, value.get("value"))
        except KeyError:
            return ActionResult(toast=("warn", f"AI search: unknown setting {key}"), rerender=False)
        except (TypeError, ValueError):
            return ActionResult(toast=("warn", "AI search: that value is not valid"), rerender=False)
        if key in ("exclude_bins", "min_duration"):
            self._refresh_items()
        return ActionResult()

    # --- rendering ---------------------------------------------------------------------------------------------

    def _state(self) -> str:
        """missing | downloading | no_project | off | indexing | ready"""
        if self._download_job is not None:
            return "downloading"
        if not self.model_ready:
            return "missing"
        if self._project is None:
            return "no_project"
        if not self.setting("enabled"):
            return "off"
        if self._index_job is not None:
            return "indexing"
        return "ready"

    def _device_text(self) -> str:
        device = getattr(self.embedder, "device", "") if self.embedder is not None else ""
        if not device:
            device = emb.resolve_device(self.setting("provider"))
        return emb.device_label(device)

    def section_status(self) -> Optional[dict]:
        state = self._state()
        if state == "missing":
            return P.status("Model not downloaded", "warn")
        if state == "downloading":
            return P.status("Downloading model", "accent")
        if state == "no_project":
            return P.status("No project", "neutral")
        if state == "off":
            return P.status("Off for this project", "neutral")
        if state == "indexing":
            value, label = self._index_progress
            return P.status(label.split(" · ")[0] or "Indexing", "accent")
        text = f"{self._catalog.count()} clips indexed · {len(self._pending)} pending"
        if emb.resolve_device(self.setting("provider")) == emb.PROVIDER_CPU:
            text += " · CPU"
        return P.status(text, "ok" if not self._pending else "neutral")

    def _hit_rows(self, with_actions: bool = True) -> list:
        rows = []
        for hit in self._hits:
            thumb = self._thumbs.get((hit.key, round(hit.time, 1)))
            actions = [self.row_action("open", "Open", hint="Open in the Source Monitor at this frame"),
                       self.row_action("insert", "Insert", hint="Insert around this frame at the playhead"),
                       self.row_action("mark", "Mark", hint="Add a clip marker at this frame")] if with_actions else []
            rows.append(self.list_row(hit.key, hit.name, subtitle=hit.subtitle(), thumb=thumb,
                                      actions=actions, bar=hit.bar))
        return rows

    def render(self) -> list:
        state = self._state()
        nodes: list = []
        if state in ("missing", "downloading"):
            if state == "downloading":
                done, total = self._download_progress
                nodes.append(self.progress(done / total if total else None,
                                           f"Downloading model · {_mb(done)} of {_mb(total)} MB"))
                nodes.append(self.button("cancel_download", "Stop download"))
            else:
                if self._model_error:
                    nodes.append(self.text(self._model_error, tone="error", small=True))
                nodes.append(self.text("Enable AI search in the Premiere Deck window"))
                size = _mb(self.manifest.total_size) if self.manifest else "?"
                nodes.append(self.button("download", f"Download model ({size} MB)",
                                         hint="One download from Hugging Face; search runs on this computer."))
            return nodes
        if state == "no_project":
            return [self.text("Open a project to search its footage")]
        if state == "off":
            return [self.text("AI search is off for this project"),
                    self.toggle("enabled", "Search this project", False)]
        searching = self._search_job is not None
        nodes.append(self.input("query", "Find frames", self._query, placeholder="a car on a bridge",
                                submit_label="Search"))
        if searching:
            nodes.append(self.progress(None, "Searching"))
        empty = "No matching frames" if self._searched else "Describe what the frame shows"
        nodes.append(self.list_("hits", self._hit_rows(), empty=empty))
        if self._hits:
            nodes.append(self.button("clear", "Clear results"))
        nodes.append(self.divider())
        if state == "indexing":
            value, label = self._index_progress
            nodes.append(self.progress(value, label or "Indexing"))
            nodes.append(self.row([self.button("index", "Index project", busy=True),
                                   self.button("cancel", "Stop")]))
        else:
            if self._index_error:
                nodes.append(self.text(self._index_error, tone="error", small=True))
            hint = f"{len(self._pending)} clips not indexed yet" if self._pending else "Every clip is indexed"
            nodes.append(self.button("index", "Index project", hint=hint))
        nodes.append(self.toggle("auto_index", "Index new clips", self.setting("auto_index")))
        return nodes

    def render_host(self) -> Optional[dict]:
        """Page model for the host window (docs/HOST_UI.md 4, docs/AI_SEARCH.md 4)."""
        state = self._state()
        blocks: list[dict] = [{"type": "cards", "id": "search.status", "items": [self._model_card(state),
                                                                                  self._index_card(state)]}]
        if state in ("indexing", "ready"):
            blocks.append({"type": "form", "id": "search.find", "fields": [
                self.input("query", "Find frames", self._query, placeholder="a car on a bridge",
                           submit_label="Search")]})
            blocks.append({
                "type": "grid", "id": "search.hits", "columns": 4,
                "busy": self._search_job is not None,
                "empty": "No matching frames" if self._searched else "Describe what the frame shows",
                "items": [{
                    "id": hit.key, "title": hit.name, "subtitle": hit.subtitle(),
                    "thumb": self._thumbs.get((hit.key, round(hit.time, 1))),
                    "bar": round(hit.bar, 3),
                    "times": [{"seconds": round(t, 3), "label": qy.timecode(t), "bar": round(
                        qy.bar_value(s, self._hits[0].scores[0], self.threshold), 3)}
                        for t, s in zip(hit.times, hit.scores)],
                    "actions": [self.row_action("open", "Open"), self.row_action("insert", "Insert"),
                                self.row_action("mark", "Mark")],
                } for hit in self._hits],
            })
        if self._project is not None and self._catalog is not None:
            blocks.append(self._clips_table())
        # The settings fields are drawn once by the host window's Settings card (docs/MODULES.md 5).
        return {"blocks": blocks}

    def _model_card(self, state: str) -> dict:
        size = _mb(self.manifest.total_size) if self.manifest else "?"
        title = self.manifest.title if self.manifest else "CLIP"
        if state == "downloading":
            done, total = self._download_progress
            return {"id": "model", "title": "Model", "value": "Downloading", "tone": "accent",
                    "detail": f"{title} · {_mb(done)} of {_mb(total)} MB",
                    "progress": done / total if total else None,
                    "actions": [self.button("cancel_download", "Stop download")]}
        if state == "missing":
            return {"id": "model", "title": "Model", "value": "Not downloaded", "tone": "warn",
                    "detail": self._model_error or f"{title} · {size} MB from Hugging Face, once; "
                                                   "search runs on this computer",
                    "progress": None, "actions": [self.button("download", f"Download {size} MB", tone="accent")]}
        return {"id": "model", "title": "Model", "value": "Ready", "tone": "ok",
                "detail": f"{title} · {size} MB · {self._device_text()}", "progress": None,
                "actions": [self.button("remove_model", "Remove model",
                                        confirm="Remove the search model files? Indexes stay in the projects.")]}

    def _index_card(self, state: str) -> dict:
        if self._project is None or self._catalog is None:
            return {"id": "index", "title": "Index", "value": "No project", "tone": "neutral",
                    "detail": "Open a project in Premiere Pro", "progress": None, "actions": []}
        count, frames = self._catalog.count(), self._catalog.frame_count()
        detail = f"{frames} frames · {len(self._pending)} clips pending"
        if self._index_failed:
            detail += f" · {len(self._index_failed)} could not be read"
        if state == "indexing":
            value, label = self._index_progress
            return {"id": "index", "title": "Index", "value": f"{count} clips", "tone": "accent",
                    "detail": label or detail, "progress": value,
                    "actions": [self.button("cancel", "Stop")]}
        disabled = state != "ready"
        return {"id": "index", "title": "Index", "value": f"{count} clips",
                "tone": "ok" if not self._pending else "neutral", "detail": detail, "progress": None,
                "actions": [self.button("index", "Index project", disabled=disabled),
                            self.button("rebuild", "Rebuild", disabled=disabled,
                                        confirm="Delete this project's index and build it again?")]}

    def _clips_table(self) -> dict:
        rows = []
        failed = self._index_failed
        pending = {c.node_id for c in self._pending}
        for cand in self._candidates[:MAX_TABLE_ROWS]:
            if cand.node_id in failed:
                state, tone = failed[cand.node_id], "error"
            elif cand.node_id in pending:
                state, tone = "Pending", "neutral"
            else:
                state, tone = "Indexed", "ok"
            rows.append({"id": cand.node_id, "name": cand.name, "bin": cand.tree_path or "(root)", "state": state,
                         "tone": tone})
        for skip in self._skipped[:max(0, MAX_TABLE_ROWS - len(rows))]:
            rows.append({"id": skip.node_id, "name": skip.name, "bin": "", "state": f"Skipped: {skip.reason}",
                         "tone": "neutral"})
        return {"type": "table", "id": "search.clips",
                "columns": [{"key": "name", "label": "Clip"}, {"key": "bin", "label": "Bin"},
                            {"key": "state", "label": "Index"}],
                "rows": rows, "empty": "No video clips in this project"}

    def on_setting_changed(self, key: str, scope: str, value: Any) -> None:
        """The host window's Settings card stored a value (docs/MODULES.md 5): same side effects as the action."""
        if key == "provider":
            self._reset_embedder()
        elif key in ("exclude_bins", "min_duration") and self._project is not None:
            self._refresh_items()
