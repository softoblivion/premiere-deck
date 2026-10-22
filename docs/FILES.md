# Files module

Owner: files worker (code and the details of this document), integrator (the contract). Module id `files`. Keeps
the media of a project sorted the same way on disk and in the project panel, and makes a project folder
self-contained so it can be copied to another machine without offline media. Package
`src/premiere_deck/modules/files/`: `module.py` (`FilesModule`), `layout.py` (layouts, exclusions, root detection,
folder <-> bin mapping), `classify.py` (rules and overrides over `MediaInfo`), `sync.py` (copies, suppression, disk
watcher, Collect plan, Pack), `journal.py` (`journal.jsonl` with undo), `layouts/default.json`.

## 1. Invariants (never violated by any code path)

1. Originals are never moved, renamed or deleted. The module copies; the user deletes. The only files the module
   deletes are copies it made itself: a file a `done` journal entry records as `copied` (or `recreated` by an
   undo), whose quick hash still equals the journaled hash, inside the root, referenced by no project item.
2. Nothing happens outside the project root except reading a source file for a copy, and the one exception the
   user asks for explicitly: the Pack archive `<root>.zip` beside the root (3.5).
3. Every change is journaled before it is made and marked done after; an interrupted run is resumed or rolled
   back from the journal on the next project open (5.2).
4. A change the module made never triggers the module again (suppression, section 5.4).
5. The project file itself (`.prproj`), Premiere's own folders (`Adobe Premiere Pro Auto-Save`, `Adobe Premiere
   Pro Video Previews`, `Adobe Premiere Pro Audio Previews`, `Adobe Premiere Pro Captured Video/Audio`, any
   `Media Cache*`), `.premiere-deck\` and hidden or temporary files (`~*`, `*.tmp`, `*.part`, `*.crdownload`,
   `Thumbs.db`, `desktop.ini`, names starting with `.`, files with the Windows hidden attribute) are never touched
   and never imported. Exclusion is by name anywhere below the root (`layout.is_excluded`).

## 2. Layout templates

A layout is a JSON file: `modules/files/layouts/default.json` ships with the exe; a project may carry its own in
`<root>\.premiere-deck\layout.json` (project setting `layout` = `"custom"`; written by the module when the host
window saves a layout, 7).

```json
{
  "id": "default", "name": "Default",
  "project_dir": "Project",
  "folders": ["Footage", "Audio/Music", "Audio/SFX", "Audio/Voice", "Graphics", "Overlays", "Motion", "Exports", "Exports/Frames", "Project"],
  "rules": [
    {"match": {"kind": "video", "alpha": true}, "to": "Overlays"},
    {"match": {"kind": "video", "name": "overlay|film|dust|grain|leak|burn|flare|vhs|texture|transition|glitch|smoke|particle"}, "to": "Overlays"},
    {"match": {"kind": "video"}, "to": "Footage"},
    {"match": {"kind": "audio", "name": "sfx|whoosh|hit|swoosh|click|pop|riser|impact|transition"}, "to": "Audio/SFX"},
    {"match": {"kind": "audio", "name": "vo|voice|narration|rec|interview|dialog"}, "to": "Audio/Voice"},
    {"match": {"kind": "audio", "max_duration": 20}, "to": "Audio/SFX"},
    {"match": {"kind": "audio"}, "to": "Audio/Music"},
    {"match": {"kind": "image"}, "to": "Graphics"},
    {"match": {"ext": ["mogrt", "aep", "aegraphic"]}, "to": "Motion"},
    {"match": {"ext": ["psd", "ai", "svg", "eps"]}, "to": "Graphics"},
    {"match": {}, "to": "Footage"}
  ],
  "mirror_subfolders": true,
  "exports_dir": "Exports"
}
```

Validation (`layout.parse_layout`): `folders` and `rules` are non-empty lists; every folder and rule target is a
safe relative path (no drive, no `..`, no excluded name of 1.5); unknown `match` fields, unknown kinds and invalid
regular expressions are errors. A custom layout that fails validation is not used: the module falls back to the
shipped layout and the panel shows "The project's layout.json could not be read; the default layout is used."

Rule evaluation: first match wins, top to bottom; the last rule's target is the fallback. `match` fields: `kind`
(`video`/`audio`/`image`/`other`, from the probe), `alpha` (`MediaInfo.alpha`, filled by `core/probe.py`: pixel
format with an alpha plane, ProRes 4444, VP8/VP9/AV1 with `alpha_mode=1`), `name` (case-insensitive regular
expression searched in the file stem, anchored at the start of a word: a match must not follow a letter, so `vo`
matches `VO_take2` and `client vo` but not `devoted`, and `pop` does not match `lollipop`), `ext` (list),
`min_duration` / `max_duration` (seconds; an unknown duration does not match), `has_audio`, `has_video` (unknown
values are filled from the kind). A probe failure classifies by extension only.

Overrides: project setting `overrides` (`project.json`, `modules.files.overrides`), a list evaluated before the
rules, each `{"source": "<folder>", "to": "<layout folder>"}` (every file below that folder, normalised prefix test)
or `{"name": "<glob>", "to": ...}` (case-insensitive glob over the file name, `*_sfx*`). Bad entries are skipped.
The host window adds and removes them through the module (7).

`mirror_subfolders` on: any non-excluded folder below the root except `project_dir` is a bin of the same path
(`Footage/Day1` <-> bin `Footage/Day1`; leading components take the layout's spelling); a bin maps back to a folder
when it is a layout folder, lies under a top-level layout folder, or already exists on disk (a `Footage2` the user
made). Off: a file is filed under the deepest layout folder that contains it; files in non-layout folders are
loose; only layout folders map to bins. Media directly in the root or in `project_dir` is loose (outside the
layout) in both modes.

Project root detection (`layout.detect_root`): the folder of the `.prproj`, unless the project file sits in the
layout's `project_dir` and the parent holds `.premiere-deck\` or any layout folder; then the parent is the root.
The host binds the parent folder as the root whenever the parent holds a `.premiere-deck` folder and the project's
own folder does not (`project_info_for` in `host/hub.py`). The host binds `project.json` by its own rule (parent holds `.premiere-deck\project.json`, `PREMIERE_API.md` 2);
when the two differ at setup, the module creates `<root>\.premiere-deck\project.json` (create-only, `modules.files`
with `enabled`, `layout`, `watch`) so the host agrees from the next open. A project adopts the layout when the user
runs Setup; until then the module only observes (snapshot, offline list, external count) and the panel shows "Set
up this project".

## 3. Behaviour

Threads: hooks and job callbacks run on the module thread and are the only callers of Premiere; probes, copies, the
Collect plan and the Pack run as jobs (copies with priority `high`); the watcher settles changes on its own thread
and reaches the module thread by submitting a small job whose `on_done` applies the change.

### 3.1 Import intercept (Premiere -> disk)

Trigger: `project.items_added` or `project.changed` (the facade's 2 s poll fires them). Premiere fires several events
per user action and the module's own relinks and bin moves fire more, while a snapshot is a full project scan, so
events are coalesced: each event re-arms a one-shot timer of `SYNC_DELAY` (0.3 s, class attribute) and the timer posts
the sync to the module thread (`ctx.post`); a burst that never goes quiet is scanned at most `SYNC_MAX_WAIT` (2.0 s)
after its first event. `project_closed` cancels a pending timer, and a timer callback of a closed or replaced project
is ignored. The module then takes one snapshot (the non-event snapshots of `project_opened`, Collect, Pack and Undo
stay direct) and diffs it against its cached one; a diff runs only when the cached snapshot is real (a failed first snapshot
never makes every existing item look new). For every new clip or file item with a media path that exists, is not
excluded and lies outside the root, or inside the root but outside the layout (loose files next to the project
file), in one high-priority job per batch:

1. Probe the file (`ctx.tools.ffprobe`; a failure classifies by extension); classify by overrides and rules;
   target `<root>\<folder>\<file name>`.
2. Collision: a target of the same name exists -> compare size and quick hash (`core/probe.quick_hash`: BLAKE2b of
   the size, the first and the last 4 MB). Equal: reuse the existing file (no copy, `copied: false`). Different or
   a `.part` of that name present: suffix ` (2)`, ` (3)` … on the stem (an equal suffixed file is reused).
3. Journal `filed` `pending` (source, target, node id, old bin path, hash, copied, size), then copy to
   `<target>.part` (exclusive create), fsync, verify the size, copy the timestamps, rename into place (never
   overwrites). A failed copy is journaled `failed` and reported "Could not copy <name>".
4. On the module thread: `relink(node_id, target)`; if the item is not online afterwards, delete the copy if the
   module made it, journal `failed` ("relink failed") and report "Could not relink <name>".
5. `move_to_bin([node_id], folder-as-bin-path)` (bins created as needed; bin names = folder names).
6. Journal `done` (with the final `bin_to`).
7. Toast "Filed <name> into <folder>"; filings within 2 s are merged ("Filed 6 files"). Host setting `toast_filed`
   turns the toast off.

Files already inside the layout but imported into a bin that is not their folder's bin are moved to that bin when
`enforce_bins` is on (default on), without any disk operation; journal `binned`. Offline items, sequences, bins and
items without media are ignored.

### 3.2 Disk watcher (disk -> Premiere)

`watchdog` observer on the root (recursive) while the project is set up and `watch` is on. Events are debounced
1.5 s per path; a created or moved-in file is handled once its size has been stable for `stable_seconds` (default
2 s) and it opens for reading (copies in progress). A delete is held for `stable_seconds + 1` s so that a delete
and a create of the same file name pair into one move (Windows reports moves between folders that way). A folder
created or moved in is expanded into its files. Ignored: section 1.5, and anything in the layout's `exports_dir`
(renders are not imported automatically; project setting `import_exports`, default off).

- New or moved-in file under a layout folder (or any mirrored folder): if no project item already points at it
  (lookup by normalised path), `import_files([path], folder-as-bin-path)`; journal `imported` with the new node ids.
  Loose files in the root or in `project_dir` are not imported.
- File renamed on disk (same folder): the items pointing at it are relinked; an item whose name equals the old file
  name (or its stem) is renamed to the new file name (or stem), a custom item name stays; journal `renamed` with
  `name_from` / `name_to`.
- File moved between folders on disk: relink, and `move_to_bin` when the new folder maps to a bin; journal
  `moved_disk`.
- File deleted on disk: nothing is done to the project; the items go to the panel's "Offline" list with "Locate"
  (reveals the nearest existing folder) and "Remove" (confirmed; removes the item, never the file).
- A new subfolder: a bin is created when its first file is imported (empty folders create no bins).

### 3.3 Bin moves (Premiere -> disk)

On a snapshot diff, an item whose `tree_path` changed (and is not the module's own move) is mirrored when
`mirror_bins` is on (default on), its file exists inside the root in the layout, and the new bin maps to a folder
(2). In a high-priority job the file is copied to the new folder (collision rules of 3.1, journal `moved_bin`
pending first); on the module thread the item is relinked to the copy, and the old file is deleted only under
invariant 1.1 (a copy the module made, unchanged, unreferenced); the entry records `deleted_from`. A file the user
put into the layout is therefore copied, not moved: the old file stays. An item moved to a bin without a folder (a
"Selects" bin) or to the root bin is left alone and nothing on disk changes. A relink failure deletes the new copy.

### 3.4 Collect project

Action `files.collect`: a dry-run job (probe and classify every external or loose item, resolve targets with the
collision rules, targets reserved within the plan) builds the plan: entries with folder, target, size and whether
an equal file is already there, per-folder totals, and the offline items (listed, not fixed). The panel shows
"<n> files · <size> · Run" with Run (confirmed: "Copy <n> files (<size>) into the project folder?") and Discard;
the host window shows the totals table and the file list. `files.collect_run` (also without a plan: plans first)
copies the plan as one high-priority job with progress (bytes copied), one `collected` journal entry per item,
relink and bin move as in 3.1. `files.cancel` stops it between or during files; finished items stay filed and the
toast says "Collect stopped · <k> of <n> filed".

### 3.5 Pack for sharing

Action `files.pack` (confirmed when anything is outside the root: "Copy <n> files from outside into the project
folder, then pack?"): runs Collect (plan and run) if anything is pending, takes a fresh snapshot, then in a job
counts every clip with media (inside, outside, offline, total size of the unique files inside), writes
`<root>\.premiere-deck\pack-report.txt`, and when the project setting `pack_zip` is on writes `<root>.zip` beside
the root (a free name: `<root> (2).zip` when taken; through a `.part` file; never overwrites) with the project
folder minus Premiere's preview folders, `Media Cache*` folders and temporary files (the auto-save copies and
`.premiere-deck` travel with it). The result line ("Pack ready · 120 files · 3.2 GB" or "Pack incomplete · 2
offline: a.mp4, b.wav") is the toast, the panel line and the report's summary. The Pack form's submit may carry
`{"pack_zip": bool}`, which is stored first.

### 3.6 Undo

"Undo last" (panel and host window) and the journal's per-entry Undo (host window, `files.undo` with `row` = entry
id) reverse one `done` entry among the last 50 journal entries of the current project, not yet undone:

| Entry | Undo |
|---|---|
| `filed`, `collected` | relink to the original (refused when it no longer exists), move back to the old bin, delete the copy under 1.1 |
| `binned` | move back to the old bin |
| `imported` | remove the created items from the project (`remove_items`, `PREMIERE_API.md` 2); the file stays on disk |
| `renamed` | relink to the old path when it still exists; rename the item back |
| `moved_disk` | relink to the old path when it still exists; move back to the old bin |
| `moved_bin` | when the old file was deleted, recreate it from the new copy (`recreated`), relink to it, move back to the old bin, delete the new copy under 1.1 |

`removed` and `setup` entries are not undoable. The undo is journaled as a new entry with `status: "undone"` and
`undo_of` = the reversed entry's id (from / to and bin_from / bin_to swapped).

## 4. Probe fields used (from `core/probe.py`, `MediaInfo`)

`kind`, `ext`, `duration`, `has_video`, `has_audio`, `alpha`, `size`, `probe_error`; `core.probe.quick_hash` for
collisions and copy identity.

## 5. State

5.1 `project.json` (`<root>\.premiere-deck\`, `modules.files`): `enabled` (bool), `layout` (`"default"` or another
    shipped layout id, or `"custom"`), `overrides` (list, 2), `enforce_bins`, `mirror_bins`, `import_exports`,
    `watch`, `pack_zip` (bools). Every value is validated on read; a wrong type or an unknown layout falls back to
    the default (`enabled` must be exactly `true`), a settings store that raises gives the defaults.
5.2 `journal.jsonl`: append-only. The first line of an entry carries its fields `{"id", "at" (epoch s), "op",
    "node_id", "name", "from", "to", "bin_from", "bin_to", "hash", "copied", "status": "pending|done|failed|undone",
    "undo_of"}` plus op-specific ones (`size`, `node_ids`, `name_from`, `name_to`, `deleted_from`, `deleted`,
    `recreated`, `error`, `resumed`, `recovered`, `folders_created`, `bins`, `layout`); later lines with the same
    `id` carry only changed fields (usually `status`) and are folded over the entry when read. A line is written,
    flushed and fsynced unless it is a `pending` line; torn or hand-edited lines are skipped. Ops: `filed`,
    `collected`, `binned`, `imported`, `renamed`, `moved_disk`, `moved_bin`, `removed`, `setup`.
    Recovery on project open (enabled projects): stale `<to>.part` files are removed; a pending copy whose item
    already points at the target is marked done; a complete copy whose item still points at the source is resumed
    (relink, bin move); anything else is rolled back (the copy deleted under 1.1) and marked failed.
5.3 In-memory: the last snapshot (patched with the outcome of the module's own changes, so its next diff does not
    see them), the path index (normalised path -> node ids), the offline and external lists, the Collect plan, the
    last pack report, the suppression set.
5.4 Suppression: before the module relinks, moves, imports, creates or deletes, it records the expected outcome
    with a 15 s TTL: `("disk", path)` for files it creates or deletes, `("import", path)` for its own imports,
    `("bin", node id, bin path)` and `("media", node id, path)`. Watcher hits and snapshot changes matching an
    entry are consumed silently; after the TTL the handlers are idempotent (a known path is not imported twice, a
    file already in its bin's folder is not moved).

## 6. Panel section (render)

Section status: "No project", "Set up this project" (warn), "Watching · <root name>" (ok), "Paused", "Watcher
stopped" (error), or the running job's label (accent).

- No project: one line "Open a project in Premiere Pro to sort its media."
- Not set up: a line on what Setup does, `setup` ("Set up project": creates the layout folders under the root,
  `.premiere-deck\` and the bins of every layout folder except `project_dir`), the external count, the Offline
  list. Every other action answers "Set up this project first" (except `locate` and `remove`).
- Set up: up to three progress bars of running jobs with `cancel` ("Stop"); `collect` ("Collect project · 14
  external", busy while planning) or the plan line with `collect_run` / `collect_discard`; `pack`; the last pack
  result line; `undo_last` (disabled when nothing is undoable, hint = what it would undo); toggle `watch`; list
  `recent` (last 8 journal entries: `title` = file name, `subtitle` = "Downloads → Overlays · 2 min ago",
  "Imported into Footage", "Renamed from x.mp4", "Undone · filed", "Failed · <error>"); list `offline` (when any:
  rows per item with `locate` and `remove`, confirmed "Remove <name> from the project? The file on disk is not
  touched.").
- Warnings as text lines: an unusable `layout.json`, Premiere not answering.

## 7. Host window page (`render_host`)

Block shapes of `BRIDGE_API.md` 5. `{"title": "Files", "status": {...}, "blocks": [...]}` with block ids:

- No project: `no_project` (text) and the layout blocks of the default layout (read only).
- `status` (cards: Status with the root, Filed today, Outside the folder, Offline), `main` (actions: `collect`
  "Plan collect", `undo_last`, `watch`, `cancel`, `host:reveal`; before setup `setup` and `host:reveal`), one
  `job_<id>` progress block per running job, `layout_error` (text) when the custom layout is unusable.
- Set up only: `journal` (table: When, Change, Name, From, To, Status; per-row `undo` for undoable entries; filter
  select `host:journal_op`, search input `host:journal_query`), `collect_plan` (list; with a plan also
  `collect_totals` table per folder and `collect_actions` with `collect_run` / `collect_discard`), `pack_options`
  (form: `pack_zip` via `host:project_setting`, submit `pack`). The project switches (`enforce_bins`,
  `mirror_bins`, `import_exports`, `watch`) are drawn once by the host window's Settings card from
  `settings_schema()`; a stored `watch` value reaches `on_setting_changed`, which starts or stops the watcher
  (`MODULES.md` 5). `host:project_setting` with `{"key", "value"}` stays for the pack form and scripts.
- `layout` (form: setting-style `layout` select via `host:layout_select`; input node `host:layout_save` with the
  layout JSON text, submit "Save layout": validated, written to `.premiere-deck\layout.json`, `layout` becomes
  `custom`), `layout_folders` (list), `layout_rules` (table: #, Match, Folder), `overrides` (table with per-row
  `host:override_remove`), `override_add` (form: match text and folder select, submit `host:override_add`; a value
  with `\`, `/` or `:` and no wildcard is a source folder, anything else a name pattern).

Window-only actions arrive as `host:<name>` (the registry strips the module prefix): `project_setting`,
`layout_select`, `layout_save`, `layout_reset`, `overrides_save` (whole list), `override_add`, `override_remove`
(`row` = index), `journal_filter` (`{"op", "query"}`), `journal_op`, `journal_query`, `reveal`.

## 8. Settings (`settings_schema`)

Host level: `toast_filed` (bool, on), `copy_buffer_mb` (int 1..256, 16), `stable_seconds` (int 1..60, 2),
`default_layout` (select from the shipped layouts). Project level (`scope="project"`): `enforce_bins`,
`mirror_bins`, `import_exports`, `watch`, `pack_zip`; `enabled`, `layout` and `overrides` are set by Setup and the
host window controls of 7, not by the settings form.

## 9. Scripts

The module has no jsx of its own. "Remove" on the offline list and the undo of `imported` call the facade operation
`ctx.premiere.remove_items(node_ids) -> int` (`PREMIERE_API.md` 2, `premiere/jsx/remove_items.jsx`): ExtendScript has
no delete for clips, so the items are moved into a fresh holding bin and the bin is deleted; files on disk stay.

## 10. Tests

Scratch root under `PREMIERE_DECK_HOME`; media generated with ffmpeg (a 1 s VP9 WebM with alpha `yuva420p`, a 1 s
mp4 without alpha, a 0.5 s and a 40 s wav, a PNG) and probed with the real ffprobe through `core/probe.py`.

| File | Covers |
|---|---|
| `tests/test_modules_files_layout.py` | every rule of the default layout, word-start name matching, overrides, broken layouts, root detection, the exclusions of 1.5, folder <-> bin mapping with and without mirroring |
| `tests/test_modules_files_media.py` | classification of the real generated media |
| `tests/test_modules_files_sync.py` | copies (`.part`, timestamps, never overwrite, cancel), collision equal and different, plan reservations, suppression TTL, journal fold / torn lines / undo window, pack files, zip, report |
| `tests/test_modules_files.py` | render in every state and the host page blocks; intercept (copy, alpha, merged toasts, collisions, relink failure with roll back, loose files, enforce_bins, exclusions, probe failure, failed first snapshot); bin move mirror; Collect plan, run, cancel, discard; Pack with an offline item and zip; undo of every op; suppression; broken `project.json` values and layout; recovery of pending entries; every action id |
| `tests/test_modules_files_watch.py` | real `watchdog` observer: import into bins, new subfolder, copy in progress, rename and move, delete -> offline, ignored paths and the module's own copies, `import_exports`, paused watch; the watcher's pairing and folder expansion |
| `tests/test_modules_files_support.py` | helpers only (scratch folders, media generation, the FakeHost case) |
