# AI search module

Owner: search worker (code and the details below), integrator (this contract). Module id `search`, package
`modules/ai_search/`. Indexes the frames of a project's footage with a local image-text model and answers text
queries ("car", "woman laughing on a balcony") with clips and times. Everything runs on the user's machine; the
model is downloaded once when the user asks for it.

## 1. Model and runtime

| Part | Choice | Size | Source |
|---|---|---|---|
| Vision encoder | CLIP ViT-B/32, ONNX fp16 export (`vision_model_fp16.onnx`; input `pixel_values` float32 [N, 3, 224, 224], output `image_embeds` [N, 512]) on both providers | 176 MB | Hugging Face `Xenova/clip-vit-base-patch32`, revision `d15189d7028b43f1d3e65039190477f6af591c2a` |
| Text encoder | CLIP ViT-B/32 text, ONNX fp32 (`text_model.onnx`; input `input_ids` int64 [N, L], output `text_embeds` [N, 512]), always on the CPU | 254 MB | same repository and revision |
| Tokenizer | CLIP BPE through the `tokenizers` library (`tokenizer.json`), truncated at 77 tokens | 2 MB | same |
| Runtime | `onnxruntime-directml` 1.24: DirectML execution provider (any Windows GPU), CPU provider fallback | 30 MB wheel | pip |
| Frames | `ffmpeg` (section 2.3) | | `ctx.tools.ensure("ffmpeg")` before the first clip; the process is started with `ctx.tools.ffmpeg_pipe(args)` (`MODULES.md` 3) |

Pinned files, sizes and SHA-256: `modules/ai_search/models.json`. Total download 432 MB ("Download model
(432 MB)").

Why these files: the fp16 vision export runs on DirectML and, with ONNX Runtime's basic graph optimisation level,
on the CPU as well (the extended CPU fusions reject it), so one vision file serves both providers; fp16 and fp32
vision embeddings agree to cosine 0.9999. The fp16 text export does not load on the CPU provider; the text encoder
runs once per query on the CPU, so the fp32 file is used.

Measured on the development machine (RTX 4070 Ti SUPER, i7-14700KF, 2026-10-21; `reports/ai_search_eval.md`):

| | DirectML | CPU |
|---|---|---|
| vision model, batches of 32 incl. preprocessing | 816 frames/s | 85 frames/s |
| indexing end to end, 120 s of 1080p H.264, step 2 s | 1.3 s (90 x real time) | 2.2 s (54 x real time) |
| text query embedding (always CPU) | about 4 ms | same |
| GPU memory of the vision session | about 365 MB | |

Indexing is decode-bound on the GPU path; ffmpeg is the cost, not the model.

Windows power throttling: Windows 11 throttles a process that has no foreground window (EcoQoS); CPU inference
then drops from about 90 to about 16 frames/s after two seconds. When the vision session runs on the CPU,
`embed.disable_power_throttling()` opts the host process out of execution-speed throttling
(`SetProcessInformation(ProcessPowerThrottling)`), once per process.

Download (`models.py`): every file goes to `<data>/models/clip-vit-b32/<name>.part` (resumed with an HTTP Range
request when a part exists; a server answering 200 restarts it), its size and SHA-256 are checked against the
manifest, then it replaces the target and is recorded in `<data>/models/models.json` (`{<model id>: {title, repo,
revision, files: {<name>: {url, size, sha256, verified_at}}}}`). A checksum mismatch deletes the part; a cancel keeps
it. `status()` is cheap (size plus record); files copied in by hand are hashed once and recorded. URLs are HTTPS
only; `PREMIERE_DECK_MODELS_URL` replaces the base URL (`<base>/<source path>`) for tests and offline mirrors. The
module is usable as soon as all files are present; until then the panel and the host page offer the download.

## 2. Index

### 2.1 Layout

Per project, in `<root>\.premiere-deck\index\`:

- `catalog.json`: `{"version": 2, "model": "clip-vit-b32", "dim": 512, "clips": {"<quick_hash>": {"path",
  "node_id", "name", "kind" ("video" | "image"), "duration", "frames", "step", "indexed_at"}}, "paths":
  {"<normalised path>": {"hash", "size", "mtime"}}}`. `paths` lets the status count pending clips with one `stat`
  per file instead of hashing it.
- `<quick_hash>.npz`: `times` (float32[N], seconds from the clip's first frame) and `emb` (float16[N, 512],
  L2-normalised).

Keyed by the file's quick hash (`core.probe.quick_hash`: BLAKE2b-128 of the size and the first and last 4 MB), so
a relinked or copied file keeps its index (its `node_id`, `path` and `name` are updated) and a changed file is
re-indexed.

Catalogue migration on load: version 1 (no `version`, the original shape) gains defaults and an empty `paths`
and is saved as version 2; a different `model` or `dim` drops every clip and its npz (both models may not be
mixed); an unreadable file, a non-object or a newer version is renamed to `catalog.json.bad-<timestamp>` and the
index starts empty. Nothing crashes on a hand-edited file.

### 2.2 What is indexed

Project items of kind `clip` with a media path, not sequences: video files and images (one frame) by extension.
Skipped, with the reason shown in the host page's clip table: `No picture` (audio files, `has_video` false),
`Offline`, `Excluded bin`, `Too short` (video shorter than `min_duration`). `exclude_bins`: an entry with a `/`
excludes that bin path and everything below it; a bare name excludes every bin of that name at any depth;
case-insensitive. The same file imported twice is indexed once.

### 2.3 Frames

One ffmpeg process per clip, started with `ctx.tools.ffmpeg_pipe(args)` (the host owns its Job Object; a cancel kills
the process): `scale=224:224:force_original_aspect_ratio=increase,crop=224:224` (shorter side to
224 px, centre crop), then `select` of the first frame, every frame at least `step_seconds` after the last picked
one, and every scene cut (`scene > 0.3`); `showinfo` on stderr gives each picked frame's time; raw RGB24 on stdout.
Scaling before `select` keeps the scene score cheap on 4K footage. Capped at `max_frames_per_clip`; when
`duration / step` would exceed 80 % of the cap the step is widened to `duration / (0.8 * cap)` so the whole clip is
covered and a fifth of the cap is left for cuts. Times start at 0 at the first frame (ffmpeg shifts the input
start time away), which is the clip time Premiere uses for source in points. The process runs hidden in a Job
Object (`core.procs`), killed on cancel.

### 2.4 The indexing job

`run_index` processes one clip at a time: hash (or the remembered hash when size and mtime are unchanged), skip
when indexed, extract, embed in batches of 32, then write the npz and the catalogue. A clip is written only when all
its frames are embedded, so a cancelled or crashed job keeps every finished clip and the next run continues with the
rest (resume = "index the pending clips again"). Progress label "Indexing 12 of 48 clips · 3 min left" (estimate
from the clips that needed work, weighted by duration).

Triggers: "Index project", "Rebuild" (host page; clears the project's index first, with a confirmation), the
project opening when `auto_index` is on, the model download finishing, and new clips 10 s after the last
`project.items_added` / `project.changed` event (a newer event restarts the wait).

Priority `low`. Yielding: one ffmpeg process and one batch of 32 frames at a time; between clips the job waits
("Paused while files are copied") while `ctx.jobs.high_priority_running()` returns true (`MODULES.md` 3: any module's `high` job, a Files copy, is
queued or running). The host's reserved high-priority worker also keeps Files jobs from queueing behind indexing.

## 3. Query

`AiSearchModule.search(text)` (and the panel's search job): the text becomes `a photo of <text>` unless it already
reads like a caption (starts with a / an / the / photo / picture / image, or has more than six words); its embedding
is cached (LRU of 64, keyed by the lower-cased text). Cosine similarity against every frame of every indexed clip
(one float32 matrix, built from the npz files and cached until the catalogue changes); frames below the threshold
are dropped; per clip the best 3 frames at least 2 s apart (non-maximum suppression); clips ranked by their best
frame; top 30 clips. One hit per clip: best time plus the other times ("00:01:12 · also 00:01:40, 00:02:03").

Threshold 0.25 cosine (`query.DEFAULT_THRESHOLD`), chosen on the evaluation set: correct scenes scored 0.284-0.347,
the best frame of six queries with no matching footage 0.211-0.247 (`reports/ai_search_eval.md`). Re-check it on
real footage; it lives in one constant.

Scores are never shown as numbers: the panel row carries `bar` (0.2..1, relative to the query's best hit), which
the panel draws as a thin bar under the subtitle (`PROTOCOL.md` 6.1); the host grid item carries the same `bar`.

Thumbnails: 160 px wide JPEG at the hit time (`ffmpeg -ss <t> -i <file> -frames:v 1`), made by a job after the
results arrive (rows fill in as they are ready), cached in `<data>\cache\search\` and evicted least recently used
(file mtime, refreshed on every hit) beyond `thumb_cache_mb`.

## 4. UI

Panel section (ids without the `search.` prefix):

| State | Nodes |
|---|---|
| model missing | text "Enable AI search in the Premiere Deck window", button `download` "Download model (432 MB)"; a failed download shows its sentence in red above |
| downloading | progress "Downloading model · 120 of 432 MB", button `cancel_download` "Stop download" |
| no project | text "Open a project to search its footage" |
| off for this project | text, toggle `enabled` |
| ready / indexing | input `query` "Find frames" (submit "Search"); progress "Searching" while a search runs; list `hits` (row id = clip quick hash; thumb; title = clip name; subtitle = times; `bar`; row buttons `open`, `insert`, `mark`; empty text "Describe what the frame shows" / "No matching frames"); button `clear` when there are hits; divider; progress + `index` (busy) + `cancel` "Stop" while indexing, else button `index` "Index project" (hint: pending count); toggle `auto_index` "Index new clips" |

Section status: "Model not downloaded" (warn), "Downloading model", "No project", "Off for this project",
"Indexing 12 of 48 clips" (accent), "48 clips indexed · 2 pending" (+ " · CPU" when the model runs on the CPU).

Actions (`search.<id>`; window-only controls may arrive as `host:<id>` or `search.host:<id>`, both accepted):

| id | value / row | effect |
|---|---|---|
| `query` | text | search (empty text clears) |
| `clear` | | clear results |
| `open` | row = hit; value = seconds (optional, host grid times) | `open_in_source(node_id, seconds=t)` |
| `insert` | row, value as above | `insert_clip(node_id, start=t - clip_pad, end=t + clip_pad)` clamped to the clip; images 0..2 x clip_pad |
| `mark` | row, value as above | `add_marker(node_id, t, "Deck: <query>")` |
| `index`, `rebuild`, `cancel` | | start, rebuild, stop indexing |
| `download`, `cancel_download`, `remove_model` | | model files (remove asks for confirmation; indexes stay) |
| `clear_thumbs` | | empty the thumbnail cache |
| `auto_index`, `enabled` | bool | project settings |
| `setting` | `{"key", "value"}` | any setting of section 6, validated |

A hit's node id is resolved from the current snapshot by media path, falling back to the catalogue's; a missing
node or a Premiere error becomes a toast.

Host page (`render_host()`): `{"blocks": [...]}` with

- `cards` `search.status`: items `{id, title, value, detail, tone, progress (0..1 or null), actions: [button
  nodes]}` for the model (status, size, device, Download / Stop download / Remove model) and the index (clip
  count, frames, pending, failed; Index project / Rebuild / Stop).
- `form` `search.find` (ready or indexing): `fields` = protocol nodes (the `query` input).
- `grid` `search.hits` (ready or indexing): `columns` 4, `busy`, `empty`, `items: [{id, title, subtitle, thumb,
  bar, times: [{seconds, label, bar}], actions: [button nodes]}]`.
- `table` `search.clips` (project open): `columns: [{key, label}]`, `rows: [{id, name, bin, state, tone}]`
  (Indexed / Pending / the failure sentence / "Skipped: <reason>"), at most 500 rows, `empty`.
- `form` `search.settings`: `action` "search.setting", `fields: [{key, label, kind, value, options, hint,
  scope}]` (project fields only while a project is open).

## 5. Privacy and resources

No frame, embedding or query leaves the machine. The index lives in the project folder (it travels with the
project; a user who shares a project shares the index, which is derived data of their own footage). Model files
are downloaded from the pinned Hugging Face revision over HTTPS and verified; the download is the only network
access of the module. GPU memory use about 365 MB; CPU fallback works with "CPU" in the status.

## 6. Settings

Read through `AiSearchModule.setting(key)`: a missing or invalid value falls back to the default (logged).

| Key | Scope | Kind | Default | Valid |
|---|---|---|---|---|
| `provider` | host | select | `auto` | `auto` / `directml` / `cpu`; `auto` and `directml` use DirectML when available |
| `step_seconds` | host | int | 2 | 0.5-60 |
| `max_frames_per_clip` | host | int | 600 | 1-10000 |
| `clip_pad` | host | select (`1`, `2`, `3`, `5`) | 2 | 0.1-30 |
| `thumb_cache_mb` | host | int | 200 | 10-10000 |
| `enabled` | project | bool | true | |
| `auto_index` | project | bool | true | |
| `exclude_bins` | project | text (comma-separated; a list is accepted) | `Overlays, Graphics` | |
| `min_duration` | project | int (seconds) | 1 | 0-3600 |

Changing `provider` drops the loaded sessions; changing `exclude_bins` or `min_duration` refreshes the clip list.

## 7. Upgrade path

SigLIP (`Xenova/siglip-base-patch16-224`) scores better on short queries and is a drop-in behind the same
`Embedder` interface (`embed.py`: `embed_images(frames uint8 [N, H, W, 3]) -> float32 [N, dim]`,
`embed_text(text) -> float32 [dim]`, both L2-normalised; `dim`, `model_id`, `device`). The catalogue carries
`model`, so a model switch re-indexes; both models may not be mixed in one index. A later module for speech
(transcripts) reuses the index layout with a `kind: "speech"` catalogue.

## 8. Tests

`tests/test_modules_ai_search.py` (fixtures in `tests/test_modules_ai_fixtures.py`), no network: the model is a
tiny random-weight ONNX graph with the real input and output names and shapes (written as ONNX protobuf by the
fixture, since the `onnx` package is not a dependency) plus a tiny word-level `tokenizer.json`; media are
ffmpeg-generated clips and a Pillow image. Covered: manifest pinning, HTTPS rule and override, download with
verification, resume by Range, restart on 200, checksum failure, cancel, copied files; preprocessing; CPU and auto
embeddings; frames (step plus cut, cap, widened step, image, errors, cancel), thumbnails; catalogue round trip,
matrix cache, migration from version 1, model change, broken and future catalogues; candidates and exclusions;
indexing, skip of known clips, resume after cancel, relinked copy, pausing; NMS, ranking, threshold, bars,
prompt template, query cache, thumb cache eviction; the module's render in every state (model missing,
downloading, no project, off, ready, indexing, searching, results, empty, nothing indexed, failed clip), every action
id, the debounced auto index, settings fallback and the settings action.

The real-model check (throughput on both providers, ten-plus queries with the expected clip and scene, the
threshold) is recorded in `reports/ai_search_eval.md`.
