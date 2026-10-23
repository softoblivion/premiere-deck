# Files module

Owner: files worker (code and the details of this document), integrator (the contract). Module id `files`. Keeps
the media of a project sorted the same way on disk and in the project panel, and makes a project folder
self-contained so it can be copied to another machine without offline media. Package
`src/premiere_deck/modules/files/`: `module.py` (`FilesModule`), `layout.py` (layouts, exclusions, root detection,
folder <-> bin mapping), `classify.py` (rules and overrides over `MediaInfo`), `media_class.py` (the classifier of
3.8: video source and audio class), `sync.py` (copies, suppression, disk watcher, the Sort plan, Sync, ZIP),
`journal.py` (`journal.jsonl` with undo), `layouts/default.json`, `models/` (the audio model of 3.8 with its licence
and class map).

## 1. Invariants (never violated by any code path)

1. Files outside the root are never moved, renamed or deleted: the module copies them in; the user deletes.
   Inside the root the module moves files between layout folders only on the user's explicit command (Sort
   project, 3.4), mirror (a bin move, 3.3) or, for a file dropped into `media_dir`, the Auto sort switch (3.1),
   always journaled and undoable, never across volumes (the root is one folder tree, so a move is a rename). The only files the module deletes are copies it made itself: a file a
   `done` journal entry records as `copied` (or `recreated` by an undo), whose quick hash still equals the
   journaled hash, inside the root, referenced by no project item.
2. Nothing happens outside the project root except reading a source file for a copy. The ZIP archive of "Make ZIP"
   (3.6) is written inside the root, into `<root>\<exports_dir>`.
3. Every change is journaled before it is made and marked done after; an interrupted run is resumed or rolled
   back from the journal on the next project open (5.2).
4. A change the module made never triggers the module again (suppression, section 5.4).
5. The project file itself (`.prproj`), Premiere's own folders (`Adobe Premiere Pro Auto-Save`, `Adobe Premiere
   Pro Video Previews`, `Adobe Premiere Pro Audio Previews`, `Adobe Premiere Pro Captured Video/Audio`, any
   `Media Cache*`, the mask folder `<project stem> Masks` with its `.prmf` files), `.premiere-deck\` and hidden or
   temporary files (`~*`, `*.tmp`, `*.part`, `*.crdownload`, `Thumbs.db`, `desktop.ini`, names starting with `.`,
   files with the Windows hidden attribute) are never touched and never imported. Exclusion is by name anywhere
   below the root (`layout.is_excluded`; `<project stem>` is the stem of the open project file).

## 2. Layout templates

A layout is a JSON file: `modules/files/layouts/default.json` ships with the exe; a project may carry its own in
`<root>\.premiere-deck\layout.json` (project setting `layout` = `"custom"`; written by the module when the host
window saves a layout, 7).

```json
{
  "id": "default", "name": "Default",
  "project_dir": "Project",
  "media_dir": "Media",
  "folders": ["Footage", "Stock", "Generated", "Overlays", "Audio/Music", "Audio/SFX", "Audio/Voice", "Graphics", "Motion", "Media", "Exports", "Exports/Frames", "Project"],
  "rules": [
    {"match": {"kind": "video", "alpha": true}, "to": "Overlays"},
    {"match": {"kind": "video", "name": "overlay|film|dust|grain|leak|burn|flare|vhs|texture|transition|glitch|smoke|particle"}, "to": "Overlays"},
    {"match": {"kind": "video", "source": "generated"}, "to": "Generated"},
    {"match": {"kind": "video", "source": "stock"}, "to": "Stock"},
    {"match": {"kind": "video"}, "to": "Footage"},
    {"match": {"kind": "audio", "name": "sfx|whoosh|hit|swoosh|click|pop|riser|impact|transition"}, "to": "Audio/SFX"},
    {"match": {"kind": "audio", "name": "vo|voice|narration|rec|interview|dialog"}, "to": "Audio/Voice"},
    {"match": {"kind": "audio", "audio_class": "speech"}, "to": "Audio/Voice"},
    {"match": {"kind": "audio", "audio_class": "music"}, "to": "Audio/Music"},
    {"match": {"kind": "audio", "audio_class": "other"}, "to": "Audio/SFX"},
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

Folder roles: `Footage` camera video and anything uncertain, `Stock` stock video, `Generated` generated video
(3.8), `Media` the flat target of Sync (3.5). `media_dir` names the Sync folder (default `"Media"` when a 1.x
layout lacks the key; it is appended to `folders` when missing). `media_dir` is never a sort target: a rule or an
override whose `to` is `media_dir` (or lies below it) is a validation error for a rule and a skipped entry for an
override.

Validation (`layout.parse_layout`): `folders` and `rules` are non-empty lists; every folder, rule target and
`media_dir` is a safe relative path (no drive, no `..`, no excluded name of 1.5); unknown `match` fields, unknown
kinds, unknown `source` or `audio_class` values (also an empty list) and invalid regular expressions are errors. A custom layout that fails validation
is not used: the module falls back to the shipped layout and the panel shows "The project's layout.json could not
be read; the default layout is used."

Rule evaluation: first match wins, top to bottom; the last rule's target is the fallback. `match` fields: `kind`
(`video`/`audio`/`image`/`other`, from the probe), `alpha` (`MediaInfo.alpha`, filled by `core/probe.py`: pixel
format with an alpha plane, ProRes 4444, VP8/VP9/AV1 with `alpha_mode=1`), `name` (case-insensitive regular
expression searched in the file stem, anchored at the start of a word: a match must not follow a letter, so `vo`
matches `VO_take2` and `client vo` but not `devoted`, and `pop` does not match `lollipop`), `ext` (list),
`min_duration` / `max_duration` (seconds; an unknown duration does not match), `has_audio`, `has_video` (unknown
values are filled from the kind), `source` (`camera`, `stock`, `generated`, `unknown`; one value or a list;
`MediaInfo.source`, 3.8) and `audio_class` (`speech`, `music`, `other`; one value or a list;
`MediaInfo.audio_class`, 3.8; an unknown class, `None`, matches no value). A probe failure classifies by extension
only (`source` is then `unknown`, `audio_class` `None`). The shipped rules put the classifier fields after the name
rules and before the kind fallbacks, so an explicit name rule or an override always wins over the classifier. The
classifier runs lazily (`classify(..., enrich=...)`): once per file, when the evaluation first reaches a rule with a
`source` or `audio_class` field whose other fields match, so an override, an earlier name rule or a rule of another
kind never decodes audio. The `Decision` carries the enriched `MediaInfo`.

Overrides: project setting `overrides` (`project.json`, `modules.files.overrides`), a list evaluated before the
rules, each `{"source": "<folder>", "to": "<layout folder>"}` (every file below that folder, normalised prefix test)
or `{"name": "<glob>", "to": ...}` (case-insensitive glob over the file name, `*_sfx*`). Bad entries are skipped.
The host window adds and removes them through the module (7). (The override key `source` is a folder path and has
nothing to do with the rule field `source` of the classifier.)

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
up this project". Layout folders that a project set up with an older layout lacks (`Stock`, `Generated`, `Media`)
are created when a copy first needs them; their bins likewise.

## 3. Behaviour

Threads: hooks and job callbacks run on the module thread and are the only callers of Premiere; probes, the
classifier, copies, the Sort plan, Sync and ZIP run as jobs (copies with priority `high`); the watcher settles
changes on its own thread and reaches the module thread by submitting a small job whose `on_done` applies the
change. At most one of Sort, Sync all, Sync used and Make ZIP runs at a time: while one runs, its tile is `busy`
and the other three are disabled.

### 3.1 Import intercept (Premiere -> disk)

Two project switches decide what the intercept does (5.1, 6): `auto_copy` ("Auto copy", the copy step) and
`auto_sort` ("Auto sort", the filing step). Both default to off and a project that is not set up never runs the
intercept; with both off the module only observes (snapshot, offline list, external count).

| `auto_copy` | `auto_sort` | A new item whose file lies ... | What happens |
|---|---|---|---|
| on | off | outside the root | copied into `media_dir`, relinked, moved to the bin of `media_dir`; no classification; journal `synced`, toast "Copied <name> into Media" |
| on | off | in the root | nothing |
| off | on | outside the root | nothing (the item stays in the "outside" count) |
| off | on | loose in the root (root folder, `project_dir`, a folder outside the layout) | classified, copied into its layout folder (the original stays), relinked, binned; journal `filed` |
| off | on | in `media_dir` | classified and moved into its layout folder (a rename, like Sort project 3.4), relinked, binned; journal `sorted`, undoable |
| off | on | in another layout folder | moved to the bin of its folder when `enforce_bins` is on (journal `binned`) |
| on | on | outside the root | classified and copied straight into its layout folder, relinked, binned; journal `filed` (the 2.0 behaviour, one copy) |
| on | on | in the root | as in the row of Auto sort alone |

Trigger: `project.items_added` or `project.changed` (the facade's 2 s poll fires them). Premiere fires several events
per user action and the module's own relinks and bin moves fire more, while a snapshot is a full project scan, so
events are coalesced: each event re-arms a one-shot timer of `SYNC_DELAY` (0.3 s, class attribute) and the timer posts
the sync to the module thread (`ctx.post`); a burst that never goes quiet is scanned at most `SYNC_MAX_WAIT` (2.0 s)
after its first event. `project_closed` cancels a pending timer, and a timer callback of a closed or replaced project
is ignored. The module then takes one snapshot (the non-event snapshots of `project_opened`, Sort, Sync, ZIP and
Undo stay direct) and diffs it against its cached one; a diff runs only when the cached snapshot is real (a failed
first snapshot never makes every existing item look new). For every new clip or file item with a media path that
exists and is not excluded, the table above picks the step; the copy and sort steps run in one high-priority job per
batch and do, in order:

1. Probe the file (`ctx.tools.ffprobe`; a failure classifies by extension) and run the classifier (3.8); classify
   by overrides and rules; target `<root>\<folder>\<file name>`. The copy step of Auto copy alone has no
   classification: the folder is `media_dir`.
2. Collision: a target of the same name exists -> compare size and quick hash (`core/probe.quick_hash`: BLAKE2b of
   the size, the first and the last 4 MB). Equal: reuse the existing file (no copy, `copied: false`). Different or
   a `.part` of that name present: suffix ` (2)`, ` (3)` ... on the stem (an equal suffixed file is reused).
3. Journal `filed` (Auto sort) or `synced` (Auto copy alone) `pending` (source, target, node id, old bin path, hash,
   copied, size), then copy to `<target>.part` (exclusive create), fsync, verify the size, copy the timestamps,
   rename into place (never overwrites). A failed copy is journaled `failed` with the reason of 3.9 in `error` and
   reported "Could not copy <name> · <reason>".
4. On the module thread: `relink(node_id, target)`; if the item is not online afterwards, delete the copy if the
   module made it, journal `failed` ("relink failed") and report "Could not relink <name>".
5. `move_to_bin([node_id], folder-as-bin-path)` (bins created as needed; bin names = folder names).
6. Journal `done` (with the final `bin_to`).
7. Toast "Filed <name> into <folder>" ("Copied <name> into Media" for Auto copy alone); filings within 2 s are merged
   ("Filed 6 files"). Host setting `toast_filed` turns the toast off. The intercept is automatic work, so it reports
   through toasts, never `ctx.ui.result` (`MODULES.md` 4).

A file of `media_dir` that Auto sort files (table row "in `media_dir`") goes through the plan step of Sort project (3.4,
step 3: journal `sorted`, a rename inside the root, a copy when another item uses the file) as a small job of its own;
it is not one of the four long actions, so no tile is disabled while it runs. It reports with the same toast.

Files already inside a layout folder but imported into a bin that is not their folder's bin are moved to that bin
by Auto sort when `enforce_bins` is on (default on), without any disk operation; journal `binned`. Offline items,
sequences, bins and items without media are ignored. The first copy of a project session runs the write check of 3.9
first; when it fails the intercept stops for the session with one toast "No write access to the project folder".

### 3.2 Disk watcher (disk -> Premiere)

`watchdog` observer on the root (recursive) while the project is set up and `watch` is on. Events are debounced
1.5 s per path; a created or moved-in file is handled once its size has been stable for `stable_seconds` (default
2 s) and it opens for reading (copies in progress). A delete is held for `stable_seconds + 1` s so that a delete
and a create of the same file name pair into one move (Windows reports moves between folders that way). A folder
created or moved in is expanded into its files. Ignored: section 1.5 (including Premiere's `<project stem> Masks`
folder), anything in the layout's `exports_dir` (renders are not imported automatically; project setting
`import_exports`, default off), and every file whose extension is not in the media tables of `core/media.py`
(`VIDEO_EXTENSIONS`, `AUDIO_EXTENSIONS`, `IMAGE_EXTENSIONS`, `MOTION_EXTENSIONS`; `media.is_media_extension`). An
ignored file gets no journal entry, no import and no toast. Implementation: the mask folder is matched by name
(`layout.masks_folder`, case-insensitive) in `layout.is_excluded(..., masks=...)`, and `*.prmf` is excluded by name
anywhere below the root (`layout.TEMP_PATTERNS`); the extension filter is the watcher's `accept_file` and applies to
file events only, so a folder named `Day.1` is still expanded.

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

Clean-up on project open (enabled projects): `failed` entries of 1.x journals whose file is excluded by 1.5 or has
an extension outside the media tables (the mask files a 1.x watcher tried to import) are dropped. The journal is
then rewritten once without them (atomic replace through `core/paths`), the only rewrite of the journal; when
nothing matches, the file is not touched. The file checked is the entry's `to`, else its `from`; `zipped` and
`setup` entries are never dropped; every other line (torn ones included) is kept verbatim (`Journal.drop`).

### 3.3 Bin moves (Premiere -> disk)

On a snapshot diff, an item whose `tree_path` changed (and is not the module's own move) is mirrored when
`mirror_bins` is on (default on), its file exists inside the root in the layout, and the new bin maps to a folder
(2). In a high-priority job the file is moved to the new folder (a rename inside the root; collision rules of 3.1
for the name; journal `moved_bin` pending first with `from` and `to`, and `moved: true`), and on the module thread
the item is relinked to the new path. A file referenced by another project item as well is copied instead of moved
(both items keep a file; `copied: true`). When an equal file already sits in the new folder it is reused (relink
only; a module copy left at the old place is deleted under 1.1). An item moved to a bin without a folder (a "Selects" bin) or to the root bin is left alone and nothing on
disk changes. A relink failure moves the file back.
This is also how the user corrects a wrong classification: moving an item from `Footage` to `Stock` in Premiere
files its media under `Stock`.

### 3.4 Sort project

Action `files.sort` ("Sort project", confirmed: "Move <n> files into the layout folders? Every move is journaled
and can be undone.", where `<n>` is the estimate shown as the tile's meta). One high-priority job:

1. Fresh snapshot; the write check of 3.9; every clip or file item whose media exists inside the root and is not
   excluded is a candidate.
2. Plan: probe, classifier (3.8), overrides and rules give each candidate its target folder. A candidate is
   planned when its file lies loose (root, `project_dir`, a folder outside the layout), in `media_dir`, or in a
   top-level layout folder other than the target's, unless the user placed it there: a `done`, not undone journal
   entry `moved_bin` or `moved_disk` with that file as `to`, or a matching override, keeps it where it is. A file
   already under its target folder (also in a subfolder of it) is not copied; when its item sits in another bin,
   it is only moved to its folder's bin (journal `binned`). Implementation details: "top-level" compares the first
   path component of the file's deepest layout folder with the target's (`Audio/SFX` and `Audio/Music` share
   `Audio`, so Sort never moves audio between the Audio subfolders); a matching override decides the target, so a
   file under its override's folder stays; the plan phase reports progress "Checking media <k> / <n>".
3. Per planned file, in order: journal `sorted` `pending` (from, to, node id, old bin, hash, size, `moved`,
   `copied`), move the file (a rename inside the root with the collision rules of 3.1 for the name; a file
   referenced by more than one project item is copied instead, so every item keeps a file; an equal file already at
   the target is reused, both flags false), relink on the module thread (`ctx.post` per file), `move_to_bin` to the
   target's bin, journal `done`. A relink failure moves the file back and journals `failed`. A failed file is
   journaled `failed` with its reason (3.9) and the job goes on.
4. Progress: `value` = files done / planned, `label` "Sorting project <k> / <n>", `detail` "<file name> > <target
   folder>".

Result (`ctx.ui.result("sort", ...)`): `ok` "Sorted <n> files · <size>", `neutral` "Nothing to sort", `warn` "Sorted
<k> of <n> files · <m> failed" with `detail` = the first failure "<name>: <reason>", `neutral` "Sort stopped · <k>
of <n> filed" after `files.stop`. The tile's meta before a run: "<n> items to file" (an estimate from the cached
snapshot without probing: loose files and files in `media_dir`) or "Nothing to file". With an estimate of 0 the
confirmation reads "Sort the media of the project folder into the layout folders? Every move is journaled and can
be undone." Bin-only moves count as sorted files (size 0). A press while another of the four actions runs answers
`neutral` "Another action is running"; a failed snapshot answers `error` "Premiere Pro did not answer".

### 3.5 Sync all media, Sync used media

Action `files.sync_all` ("Sync all media", confirmed: "Copy <n> files from outside the project folder into
Media?") copies every clip or file item whose media exists outside the root into `<root>\<media_dir>\`;
`files.sync_used` ("Sync used media", confirmed: "Copy the files used in sequences from outside the project folder
into Media?") does the same for the items used in at least one sequence (a node id that appears in
`track_items(id)` of any sequence of `sequences()`, `PREMIERE_API.md` 2; computed at the start of the job). No
classification: the folder is flat, names keep the file name, collisions take the suffixes of 3.1 (an equal file is
reused). One high-priority job with the write check of 3.9 and a free-space check (when the planned copies need
more than the free space of the root's drive nothing is copied and the result is `error` "Not enough free space ·
needs <size>"), then per file: journal
`synced` `pending`, copy, relink, `move_to_bin` to the bin `Media` (the bin of `media_dir`), journal `done`.
Offline items are skipped and counted. Progress label "Syncing media <k> / <n>", `detail` the file name.

Results (`sync_all` / `sync_used`): `ok` "Synced <n> files into Media · <size>", `neutral` "Nothing outside the
project folder" / "No used media outside the project folder", `warn` with failures or offline items ("Synced <k>
of <n> files · 2 offline", `detail` the names or the first failure's reason), `neutral` "Sync stopped · <k> of <n>
copied". Details: the offline items counted are those whose path lies outside the root; with offline items and
nothing to copy the `warn` text is "Nothing to copy · <m> offline"; `detail` is "<name>: <reason>" of the first
failure, else "Offline: <names>" (five at most); the copy target is planned with the collision rules first, so two
items of one file share one copy and the free-space check counts it once. Tile meta: "<n> outside the root" (from the cached external list) and "<n> in sequences" (the last count
of used external items, recomputed on project open and after a snapshot diff that changed anything, at most once
per 10 s, on the module thread; "Used in sequences" before the first count).

Sort works on files inside the root and Sync on files outside it; running Sync and then Sort files the synced
media from `Media` into the layout.

### 3.6 Make ZIP and Stop

Action `files.zip` ("Make ZIP"): a job that takes a fresh snapshot and checks the media first. When any clip has
media outside the root, no archive is made and the result is `warn` "Sync all media first · <n> files outside the
folder". Otherwise it counts every clip with media (inside, offline, total size of the unique files inside) and writes
`<root>\<exports_dir>\<project name>.zip` (the folder is created when missing; `<project name>` is the project's name
without `.prproj`; a free name: `<project name> (2).zip` when taken; through a `.part` file; never overwrites). The
archive holds the project folder minus the `exports_dir` folder itself (so the archive never contains an older archive
or a report), Premiere's preview folders, `Media Cache*` folders and temporary files (the auto-save copies and
`.premiere-deck` travel with it); the top-level folder inside the archive is the root's name. The report file
`<zip name> report.txt` (`Sample report.txt`, `Sample (2) report.txt`) is written beside the archive after it is
complete. Offline items do not block the archive; they are reported. Journal `zipped` (`to` = the archive, `size`,
`offline` = the names). Progress label "Making ZIP <k> / <n>", `detail` the file being added. Result: `ok`
"ZIP ready · 120 files · 3.2 GB", `warn` "ZIP ready · 2 offline: a.mp4, b.wav", `error` with the reason of 3.9 when
writing fails (the `.part` file is removed and no report is written). The tile's meta: "Checks offline first".
Details: the file count is the number of files in the archive and the size the archive's size; `detail` of `ok` /
`warn` is the archive path; a write failure reads "Could not write the ZIP · <reason>"; a stop reads "ZIP stopped ·
<k> of <n> files"; the journal entry carries `to` (archive), `size` (archive bytes) and `offline` (names). The
1.x/2.0 `<root>.zip` beside the root and `.premiere-deck\pack-report.txt` are no longer written.

Action `files.stop` (the `cancel` of the progress node): cancels every running job of the module between or during
files; finished files stay filed, a partial `.part` is removed, and the job's result is `neutral` "<Sort | Sync |
ZIP> stopped · ...".

### 3.7 Undo

"Undo last" (panel and host window) and the journal's per-entry Undo (host window, `files.undo` with `row` = entry
id) reverse one `done` entry among the last 50 journal entries of the current project, not yet undone:

| Entry | Undo |
|---|---|
| `filed`, `collected` (1.x), `synced` | relink to the original (refused when it no longer exists), move back to the old bin, delete the copy under 1.1 |
| `sorted` | move the file back to `from` (refused when `to` no longer exists or `from` is taken by another file), relink, move back to the old bin; a `sorted` entry that copied (a shared file) is undone like `synced`. The disk move and the bin move are one entry and are undone together |
| `binned` | move back to the old bin |
| `imported` | remove the created items from the project (`remove_items`, `PREMIERE_API.md` 2); the file stays on disk |
| `renamed` | relink to the old path when it still exists; rename the item back |
| `moved_disk` | relink to the old path when it still exists; move back to the old bin |
| `moved_bin` | move the file back to `from` (refused when `to` no longer exists or `from` is taken), relink, move back to the old bin; a 1.x entry that copied (`copied: true`) is undone like `synced` |

Undo reads the entry's flags: a `sorted` or `moved_bin` entry with `moved: true` is moved back (a rename); one with
`copied: true` is undone like `synced`; a 1.x `moved_bin` copy whose old file the 1.x module deleted
(`deleted_from`) recreates that file first (`recreated`). A failed relink after a move back moves the file forward
again, so the item stays online.

`removed`, `setup` and `zipped` entries are not undoable (the archive in the Exports folder is the user's to delete), nor
is any op this version does not know (`packed` and others of older journals: shown, never undone). The undo is
journaled as a new entry with `status: "undone"` and `undo_of` = the reversed entry's id (from / to and bin_from /
bin_to swapped). Results: `ok` "Undone · <op> <name>", `warn` "Cannot undo · <reason>".

### 3.8 Classifier (`media_class.py`)

Fills `MediaInfo.source` for video and `MediaInfo.audio_class` for audio (`dataclasses.replace` over the probe's
`MediaInfo`), inside the probe step of 3.1, 3.4 and the host page's rule preview. Results are cached per (path,
size, mtime) for the host run. The layout decides what a class means (section 2); the classifier only reports.

Video source, no pixel model, metadata and names only. "Camera tag": a `make`, `model`,
`com.apple.quicktime.make`, `com.apple.quicktime.model` or `com.android.manufacturer` tag, a `major_brand` that only
cameras write (`XAVC`: Sony XAVC S / HS; `CAEP`: Canon MP4), or an `encoder` / `handler_name` tag naming a camera
vendor as a whole word (Sony, Canon, Panasonic, Nikon, Fujifilm, Blackmagic, RED, ARRI, DJI, GoPro, Apple, Samsung,
Google). A name token matches as a whole word of the file stem (not preceded or followed by
a letter, case-insensitive: `kling_0012` and `veo3 shot` match, `wanderlust` does not match `wan`). First match
wins:

1. `generated`: a generator name in the stem (runway, kling, veo, sora, pika, luma, hailuo, minimax, wan, gen-3,
   gen-4, midjourney, generated).
2. `stock`: a stock site name in the stem (pexels, pixabay, shutterstock, istock, adobestock, artgrid, envato,
   storyblocks, videvo, videezy, mixkit, coverr).
3. `camera`: a camera tag.
4. `camera`: a camera file name (stem starting with `C` + 4 digits, `DSC_`, `IMG_`, `MVI_` or `MVI` + digit,
   `DJI_`, `GX01`, `GH01`, or the pattern `A001_C001` / `A001C001`, letters and digits as placeholders), also after
   a date prefix an import tool adds (`20260910_C1195`, `2026-09-10_DSC_0001`), or a raw / camera format (`mxf`,
   `braw`, `r3d`, `mts`, `m2ts`).
5. `generated`: no audio stream and no other stream (`nb_streams` 1 when known), duration 4 to 15 s, exactly 24 fps
   (23.976 does not count), a generator frame size in either orientation (1280x720, 1024x576, 1344x768, 848x480,
   1920x1080, and 720x1280 ...) and no camera tag.
6. `stock`: a numeric-id stem (5 or more digits, optionally followed by `-` or `_` and more text, as in
   `3571264-hd_1920_1080_30fps`; a leading `YYYYMMDD` date such as a phone's `20240101_123456` is not an id), or an
   encoder tag starting with `Lavf` (ffmpeg's muxer) or `Lavc` (ffmpeg's encoder, the first video stream's tag that
   wins over the container's in `tags`) or naming HandBrake, without a camera tag.
7. `unknown` (filed as `Footage` by the shipped rules).

Audio class: YAMNet (AudioSet, Apache-2.0) as an ONNX model, file `models/yamnet.onnx` with `models/LICENSE-yamnet.txt`
and the class map `models/yamnet_class_map.csv`, bundled inside the exe (no download, no file in the data folder).
Model file: the tf2onnx export (opset 15, weights unchanged, mel front end inside the graph, input `waveform`
float32 `[samples]`, output `output_0` `[frames, 521]`) of Google's YAMNet v1 from
`https://huggingface.co/audiomagic/yamnet-onnx`, revision `f25b741c2f0bdc6d7e6db24b5fddda23347dbafd`, 16 093 355
bytes, SHA-256 `d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e`; class map 14 096 bytes, SHA-256
`cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2`, verbatim from the SavedModel. Licence Apache-2.0
(Copyright 2022 Google LLC; AudioSet labels CC BY 4.0). An independent export
(`zeropointnine/yamnet-onnx` at `ac2ca3bd45d12ec1f19f1144205ea529b4e9dedf`) gives identical scores; the full record
is in `models/LICENSE-yamnet.txt`.
`onnxruntime` with the CPU execution provider only; the session is created on first use and kept for the host run.
Input: the first 20 s of the file decoded by ffmpeg (`ctx.tools.ffmpeg_pipe`, `-t 20`, mono, 16 kHz, `f32le` on
stdout); a shorter file is used whole. The per-frame scores are averaged; the speech group (the `Speech` class and
its children in the AudioSet ontology, from the class map) and the music group (`Music`, its genres and
instruments) are summed. `speech` when the speech group is the larger of the two and at least 0.2, `music` when
the music group is, else `other`. About a second or less per file on the CPU. Any failure (no ffmpeg, a decode
error, a model error) leaves `audio_class` `None` and is logged once per file; the duration rules then decide.
Details: decode arguments `-nostdin -v error -t 20 -i <file> -map 0:a:0 -vn -ac 1 -ar 16000 -f f32le pipe:1` (`-t`
is an input option, so only 20 s are read); the groups are the contiguous class-map rows from MID `/m/09x0r`
(Speech) to `/m/0brhx` (Speech synthesizer), 6 classes, and from `/m/04rlf` (Music) to `/t/dd00037` (Scary music),
145 classes; the session uses 2 intra-op threads. Measured on the development machine (2026-10-22): session
start-up with the import about 0.22 s once per host run; per audio file about 0.05 to 0.14 s (median 0.07 s:
decode of 20 s about 0.04 to 0.07 s, model about 0.03 s); the video source is a pure function of the probe (under
0.1 ms; the ffprobe call itself takes about 0.05 s).

Explicit wins: an override (section 2) or a name rule that matches is applied before any classifier rule, so the
classifier never moves a file the user named or pointed.

### 3.9 Copy failures and the write check

A failed copy (3.1, 3.3, 3.4, 3.5) or a failed archive write (3.6) carries a reason, stored in the journal entry's
`error` and shown in the toast or the result's `detail`: "access denied" (`PermissionError`, Windows errors 5 and
1314), "file in use" (Windows errors 32 and 33), "disk full" (`ENOSPC`, Windows errors 39 and 112), "path too long"
(Windows error 206, `ENAMETOOLONG`, or a target path over 259 characters when the system has no long-path
support), "source not found" (the source vanished), else "copy error" with the exception in the log
(`sync.failure_reason`; checked in that order after "file in use"; a cancel is journaled as "cancelled"). The write
check reports any reason other than these as "access denied".

Write check: Setup, Sort, Sync and ZIP start by creating and deleting a temporary file
`<root>\.premiere-deck\write-check-<random>.tmp` (`.premiere-deck` is created first when missing). When that
fails, nothing else happens and the result is `error` "No write access to the project folder" with the reason in
`detail`; the intercept does the same once per session (3.1).

## 4. Probe fields used (from `core/probe.py`, `MediaInfo`)

`kind`, `ext`, `duration`, `width`, `height`, `fps`, `has_video`, `has_audio`, `alpha`, `size`, `probe_error`,
and since 2.0:

- `tags`: `dict[str, str]`, the container (format) tags merged with the tags of the first video stream (the
  stream's value wins on a clash), keys lower-cased, values as strings; `{}` when none.
- `nb_streams`: `int | None`, the container's stream count (`format.nb_streams`; the number of streams listed when
  the format lacks it).
- `fps`: the first video stream's `avg_frame_rate`, else its `r_frame_rate` (a `0/0` average falls back), rounded to
  3 decimals (`24000/1001` -> 23.976); both raw rates are kept in `extra`.
- `source`: `"camera" | "stock" | "generated" | "unknown" | None`, set by the classifier (3.8), `None` straight
  from the probe and for non-video.
- `audio_class`: `"speech" | "music" | "other" | None`, set by the classifier (3.8), `None` straight from the probe,
  for non-audio and on a classifier failure.

`core.probe.quick_hash` for collisions and copy identity. `core/media.py` and `core/probe.py` are owned by the
files worker for the 2.0 piece (`ARCHITECTURE.md` 4).

## 5. State

5.1 `project.json` (`<root>\.premiere-deck\`, `modules.files`): `enabled` (bool), `layout` (`"default"` or another
    shipped layout id, or `"custom"`), `overrides` (list, 2), `enforce_bins`, `mirror_bins`, `import_exports`,
    `watch`, `auto_copy`, `auto_sort` (bools; `auto_copy` and `auto_sort` default to false). Every value is validated
    on read; a wrong type or an unknown layout falls back to the default (`enabled` must be exactly `true`), a
    settings store that raises gives the defaults. A `pack_zip` key left by 1.x is ignored and kept.
    Mapping of 2.0 on load (`project_opened`): 2.0 had no switches and filed every import of a set-up project, so a
    project with `enabled` `true` that stores neither `auto_copy` nor `auto_sort` gets both stored as `true`, once.
    Setup stores both as `false` when they are absent (and writes them into the `project.json` it creates at the
    root, `_mark_root`), so a project set up in 2.1 is never mapped; Setup never changes a value that is stored.
    `enabled` stays: it is the "set up" marker that gates every action, not an intercept switch any more.
5.2 `journal.jsonl`: append-only (except the one clean-up of 3.2). The first line of an entry carries its fields
    `{"id", "at" (epoch s), "op", "node_id", "name", "from", "to", "bin_from", "bin_to", "hash", "copied",
    "status": "pending|done|failed|undone", "undo_of"}` plus op-specific ones (`size`, `node_ids`, `name_from`,
    `name_to`, `deleted_from`, `deleted`, `recreated`, `error`, `resumed`, `recovered`, `folders_created`, `bins`,
    `layout`, `offline`); later lines with the same `id` carry only changed fields (usually `status`) and are folded
    over the entry when read. A line is written, flushed and fsynced unless it is a `pending` line; torn or
    hand-edited lines are skipped. Ops written by 2.0: `filed`, `sorted`, `synced`, `zipped`, `binned`,
    `imported`, `renamed`, `moved_disk`, `moved_bin`, `removed`, `setup`; `collected` and other 1.x ops stay
    readable (3.7). Recovery on project open (enabled projects): stale `<to>.part` files are removed; a pending
    copy (`filed`, `sorted`, `synced`, `collected`, `moved_bin`) whose item already points at the target is marked
    done; a complete copy whose item still points at the source is resumed (relink, bin move); anything else is
    rolled back (the copy deleted under 1.1) and marked failed. A pending `zipped` entry is marked failed and its
    `.part` removed. An entry with `moved: true` (a `sorted` or 2.0 `moved_bin` rename) is resumed only when the
    file is at `to` and gone from `from`; its roll-back moves the file back when `from` is free.
5.3 In-memory: the last snapshot (patched with the outcome of the module's own changes, so its next diff does not
    see them), the path index (normalised path -> node ids), the offline and external lists, the Sort estimate,
    the used-in-sequences count, the last result per action, the last ZIP report, the classifier cache, the
    suppression set, and the result of the intercept's write check for the project session.
5.4 Suppression: before the module relinks, moves, imports, creates or deletes, it records the expected outcome
    with a 15 s TTL: `("disk", path)` for files it creates or deletes, `("import", path)` for its own imports,
    `("bin", node id, bin path)` and `("media", node id, path)`. Watcher hits and snapshot changes matching an
    entry are consumed silently; after the TTL the handlers are idempotent (a known path is not imported twice, a
    file already in its bin's folder is not moved).

## 6. Panel section (render)

The section is the Files tab of the panel (`icon` `folder`, one screen). Section status: "No project", "Set up
this project" (warn), "Watching · <root name>" (ok), "Paused", "Watcher stopped" (error), or the running job's
label (accent).

Tile codes (`short`, at most 16 characters; the panel draws compact tiles with the code and keeps the full `label` and
the one-line `hint` in the tooltip, `PANEL.md`):

| id | Node | Code | Full name | Icon |
|---|---|---|---|---|
| `setup` | button | `SETUP` | Set up project | `folder` |
| `sort` | button | `SORT` | Sort project | `sort` |
| `sync_all` | button | `SYNC` | Sync all media | `sync-all` |
| `sync_used` | button | `USED` | Sync used media | `sync-used` |
| `zip` | button | `ZIP` | Make ZIP | `zip` |
| `auto_copy` | switch | `AUTO COPY` | Auto copy | `sync-all` |
| `auto_sort` | switch | `AUTO SORT` | Auto sort | `sort` |

The two switches carry the word AUTO in their code so that they read apart from the Sort project button; the Files group has two columns, so the longer codes fit.

- No project: one line "Open a project in Premiere Pro to sort its media."
- Not set up: a line on what Setup does and the group `actions` (`tiles`, `columns` 2, no title, not collapsible) with
  the tile `setup` ("Set up project": the write check of 3.9, then creates the layout folders under the root,
  `.premiere-deck\` and the bins of every layout folder except `project_dir`; it does not change the switches) and,
  right beside it in the same block, the two `switch` tiles; then the external count and the Offline list. Every
  other action answers "Set up this project first" (except `locate`, `remove` and `open_folder`), a switch flip
  included: the answer is the result line, the stored value and the tile keep the old (off) value.
- Set up, top to bottom:
  - header `row`: `text` with the project name and the button `open_folder` (`variant` `icon`, icon
    `open-folder`, label "Open project folder", hint "Opens the project folder in Explorer."); under it a small
    `text` with the root path.
  - status `row` of three `text` chips: "<n> in the root" (neutral), "<n> outside" (warn when more than 0, else
    neutral), "<n> offline" (error when more than 0, else neutral).
  - group `actions` (`tiles`, `columns` 2, no title, not collapsible) with the four button tiles `sort`, `sync_all`,
    `sync_used`, `zip` (3.4 to 3.6; each with its full `label`, a one-line `hint`, its `meta` and its `confirm`; the
    running one `busy`, the others `disabled` while it runs) followed by the two `switch` tiles `auto_copy` and
    `auto_sort` (`label` "Auto copy" / "Auto sort"; `hint` "Copies files imported from outside the project folder
    into Media and relinks them." / "Files new media of the project folder into the layout folders and bins."; `value`
    = the stored setting; never `busy` or `disabled`: a flip while a job runs only changes what the next import does).
    The display keeps the group's result line from `ctx.ui.result`.
  - while a job runs: one `progress` node (`label` "Sorting project 38 / 142", `value`, `detail` "C0042.MP4 >
    Footage", `cancel` = `stop`); a filing or watcher job shows its own label the same way.
  - bottom `row`: button `undo_last` (`variant` `icon`, icon `undo`, label "Undo last", disabled when nothing is
    undoable, hint = what it would undo). There is no Journal tile in the panel (2.1); the journal is on the host
    page (7).
  - group `offline` (collapsible, title "Offline", only when any item is offline): list `offline_list` (its own id,
    so the group id `files.offline` stays unique in the tree) with rows per item (50 at most) and `locate` and
    `remove`, confirmed "Remove <name> from the project? The file on disk is not touched.".
- Warnings as text lines: an unusable `layout.json`, Premiere not answering.
- The switch node is `{"kind": "switch", "id": "files.auto_copy", "label": "Auto copy", "value": false, "short":
  "AUTO COPY", "icon": "sync-all", "hint": ...}` built by `Module.switch(...)` (`PROTOCOL.md` 6.1). A click sends
  `files.auto_copy` with `value` = the new state (a bool; any other value flips the stored one); the module stores it
  in the project setting and the next tree carries it. A successful flip shows no result line.
- Results of the other controls: `setup` `ok` "Project set up · <n> folders" (or `error` "No write access to the
  project folder"), `open_folder` `ok` "Opened <root name>" (`ctx.host.open_path(root)`), `undo_last` / `undo` `ok`
  "Undone · <op> <name>" / `warn` "Cannot undo · <reason>" / `neutral` "Nothing to undo", `locate` `ok` "Showing
  <folder>", `remove` `ok` "Removed <name> from the project", `stop` `neutral` "Nothing to stop" when no job runs (a
  stopped job reports itself). A press before Setup answers `warn` "Set up this project first", a press without a
  project `neutral` "Open a project in Premiere Pro first". A host without `ctx.ui.result` gets the same line as a
  toast.

The watch switch and the recent-entries list of 1.x are not in the panel any more: `watch` is a project setting of
the host window's Settings card (8), the journal is on the host page (7).

Action ids of the module (panel and host window): `files.setup`, `files.sort`, `files.sync_all`,
`files.sync_used`, `files.zip`, `files.stop`, `files.open_folder`, `files.auto_copy` and `files.auto_sort` (with
`value`, a bool), `files.undo_last`, `files.undo` (with `row`), `files.locate` and `files.remove` (with `row`),
plus the window-only `files.host:*` of 7. `files.journal` (brings the host window to the front on the Files tab,
`ctx.host.show("files")`, result `ok` "Journal opened in the host window") has no control in the panel since 2.1 and
is kept for scripts. `files.collect`, `files.collect_run`, `files.collect_discard`, `files.pack` and `files.cancel` are gone (an old
id is answered with nothing and logged).

## 7. Host window page (`render_host`)

Block shapes of `BRIDGE_API.md` 5. `{"title": "Files", "status": {...}, "subtitle": <root path>,
"header_actions": [open_folder], "blocks": [...]}`; the page is the Files tab of the window's main state
(`HOST_UI.md` 3). The first screen holds the status, the four actions, the last result and the recent journal;
the layout editor follows below the fold (the pane scrolls).

- No project: `no_project` (text) and the layout blocks of the default layout (read only).
- Not set up: `setup_actions` (actions: `setup`), `auto` (the two switches), `status_chips`, the layout blocks.
- Set up:
  - `status_chips` (chips: in the root, outside, offline; as in 6).
  - `main` (actions with `tiles`, `columns` 2: `sort`, `sync_all`, `sync_used`, `zip`, the same buttons, meta and
    confirmations as the panel, with their full names as `short`; the host window has room for them).
  - `auto` (list, two rows `auto_copy` "Auto copy" and `auto_sort` "Auto sort", `subtitle` = the hint of 6, each with a
    row `toggle` `{id: "files.auto_copy" | "files.auto_sort", value, disabled: false}`; a flip sends the toggle id
    with the new value and `row` = the row id, handled like the panel switch). Before Setup the block follows
    `setup_actions` directly, so the two switches sit next to Setup (a flip is refused as in 6).
  - one `job_<id>` progress block per running job (`label`, `detail`, `cancel` = `stop`).
  - `last_result` (list, one row: `title` the last result's text, `subtitle` its detail, `meta` the time, `tone`
    = its outcome; `actions` `undo_last` when anything is undoable), absent before the first result of the project
    session.
  - `journal` (table: When, Change, Name, From, To, Status; per-row `undo` for undoable entries): the last three
    entries, followed by `journal_actions` (actions: `host:journal_all` "Show all"); after it the whole journal
    with the filter select `host:journal_op` and the search input `host:journal_query`, and the button reads "Show
    fewer" (`host:journal_all` toggles).
  - `layout_error` (text) when the custom layout is unusable.
- `layout` (form: setting-style `layout` select via `host:layout_select`; input node `host:layout_save` with the
  layout JSON text, submit "Save layout": validated, written to `.premiere-deck\layout.json`, `layout` becomes
  `custom`), `layout_folders` (list; subtitles name the roles "Project file", "Sync target", "Renders"),
  `layout_rules` (table: #, Match, Folder), `overrides` (table with per-row `host:override_remove`), `override_add`
  (form: match text and folder select without `media_dir`, submit `host:override_add`; a value with `\`, `/` or `:`
  and no wildcard is a source folder, anything else a name pattern). Journal rows show a failed entry's status as
  "failed · <error>".

The project switches (`enforce_bins`, `mirror_bins`, `import_exports`, `watch`) are drawn once by the host
window's Settings card from `settings_schema()`; a stored `watch` value reaches `on_setting_changed`, which starts
or stops the watcher (`MODULES.md` 5). `host:project_setting` with `{"key", "value"}` stays for scripts.

Window-only actions arrive as `host:<name>` (the registry strips the module prefix): `project_setting`,
`layout_select`, `layout_save`, `layout_reset`, `overrides_save` (whole list), `override_add`, `override_remove`
(`row` = index), `journal_filter` (`{"op", "query"}`), `journal_op`, `journal_query`, `journal_all`.

## 8. Settings (`settings_schema`)

Host level: `toast_filed` (bool, on), `copy_buffer_mb` (int 1..256, 16), `stable_seconds` (int 1..60, 2),
`default_layout` (select from the shipped layouts). Project level (`scope="project"`): `enforce_bins`,
`mirror_bins`, `import_exports`, `watch`; `enabled`, `layout` and `overrides` are set by Setup and the host window
controls of 7, not by the settings form. `auto_copy` and `auto_sort` (project scope, bool, default false) are also
not in the form: they have the switches of 6 and 7 (the form would offer a second, unguarded control that works
before Setup); `host:project_setting` accepts them for scripts. The 1.x setting `pack_zip` is gone: Make ZIP is a button (3.6).

## 9. Scripts

The module has no jsx of its own. "Remove" on the offline list and the undo of `imported` call the facade operation
`ctx.premiere.remove_items(node_ids) -> int` (`PREMIERE_API.md` 2, `premiere/jsx/remove_items.jsx`): ExtendScript has
no delete for clips, so the items are moved into a fresh holding bin and the bin is deleted; files on disk stay.
Sync used reads sequence usage through the facade's `sequences()` and `track_items(id)`.

## 10. Tests

Scratch root under `PREMIERE_DECK_HOME`; media made by ffmpeg (a 1 s VP9 WebM with alpha `yuva420p`, a 1 s
mp4 without alpha, a 0.5 s and a 40 s wav, a PNG; for the classifier: mp4 files with `make` / `model` and `encoder`
tags written by ffmpeg `-metadata`, a 5 s 1280x720 24 fps mp4 without audio, a 20 s sine tone, generated noise and a
speech-like signal where no speech sample can be generated) and probed with the real ffprobe through
`core/probe.py`.

| File | Covers |
|---|---|
| `tests/test_modules_files_layout.py` | every rule of the default layout (including `source` and `audio_class`), word-start name matching, overrides, broken layouts (unknown `source` / `audio_class` values, a rule into `media_dir`), a 1.x layout without `media_dir`, root detection, the exclusions of 1.5 (the `<stem> Masks` folder), folder <-> bin mapping with and without mirroring |
| `tests/test_modules_files_media.py` | classification of the real generated media |
| `tests/test_modules_files_classify.py` | the video source signals of 3.8 one by one and their order (tags, names, whole-word tokens, formats, the generator heuristic, numeric ids, `Lavf` / `Lavc`), the audio class with a fake scorer (decision thresholds, cache, failures leaving `None`) and with the bundled model on the CPU (a chord with tremolo -> music; noise, a sine tone, silence -> other; a sentence from the Windows speech synthesizer -> speech when System.Speech is available; the model file is present, under 20 MB and loads on the CPU provider), the 20 s / 16 kHz mono decode arguments, explicit name rules and overrides winning, tags written by ffmpeg `-metadata` read back through ffprobe |
| `tests/test_modules_files_sync.py` | copies (`.part`, timestamps, never overwrite, cancel), collision equal and different, the failure reasons of 3.9, the write check, the free-space check, suppression TTL, journal fold / torn lines / undo window, the clean-up rewrite of 3.2, ZIP files and report, the excluded folder and the archive name with collision suffixes in a folder, ZIP refused with media outside the root |
| `tests/test_modules_files_auto.py` | Auto copy and Auto sort: the 2.0 mapping on load (once, never over a stored key, not for a project set up in 2.1), Setup leaving the switches alone, the switch tiles before and after Setup, the refusal before Setup, the flip, the host page rows, and the intercept with each combination of the switches on generated media (both off: observe only; copy only: flat `Media` copy without classification; sort only: loose and `Media` files filed, external imports untouched; both on: one classified copy; a flip affects only the next import) |
| `tests/test_modules_files.py` | render in every state (tiles with their codes and the two switch tiles, no Journal tile, meta, chips, busy and disabled tiles, progress with `cancel`) and the host page blocks; intercept with both switches on (copy, alpha, merged toasts, collisions, relink failure with roll back, loose files, enforce_bins, exclusions, probe failure, failed first snapshot, failure reason in the toast); bin move mirror; Sort (plan rules, user-placed files kept, bin-only moves, failures, stop), Sync all and Sync used (flat `Media`, bin `Media`, offline skipped, stop), Make ZIP (archive and report in `Exports`, nothing beside the root, the Exports folder left out, collision suffix); every result through `ctx.ui.result` and no toast for a press; undo of every op including `sorted`, `synced` and a 1.x `collected`; suppression; broken `project.json` values and layout; recovery of pending entries; every action id, and the removed ids answering nothing |
| `tests/test_modules_files_watch.py` | real `watchdog` observer: import into bins, new subfolder, copy in progress, rename and move, delete -> offline, ignored paths and the module's own copies, `.prmf` files in `<stem> Masks` and non-media extensions ignored without a journal entry, `import_exports`, paused watch; the watcher's pairing and folder expansion |
| `tests/test_core_probe_tools.py` | the new probe fields `tags` (merge, lower-cased keys) and `nb_streams` |
| `tests/test_modules_files_support.py` | helpers only (scratch folders, media generation, the FakeHost case) |
