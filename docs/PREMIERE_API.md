# Premiere facade (`PremiereApi`)

Owner: panel worker (implementation and jsx), integrator (this contract and `premiere/model.py`). The only way
modules reach Premiere. Every operation is a Python method backed by one jsx file; the method serialises the call
over the agent connection (`PROTOCOL.md` 4, 7), one script at a time, and converts the result into the
dataclasses of `premiere/model.py`. Known Premiere quirks are recorded in section 7, nowhere else.

## 1. Data model (`premiere/model.py`)

```python
@dataclass(frozen=True)
class ProjectInfo:   path: str; root: str; name: str; deck_dir: str
@dataclass(frozen=True)
class ProjectItem:
    node_id: str          # projectItem.nodeId, stable within a project file
    name: str
    kind: str             # "bin" | "clip" | "sequence" | "file"   (file = non-media import such as .mogrt / .psd without media)
    tree_path: str        # bins from the root, "/"-separated, without the root ("Footage/Day1"); "" at the root
    media_path: str | None   # projectItem.getMediaPath() for clips, None for bins and sequences
    is_offline: bool
    is_sequence: bool
    duration: float | None   # seconds, clips only
    has_video: bool | None
    has_audio: bool | None
    color_label: int | None
@dataclass(frozen=True)
class Snapshot:      project_path: str; taken_at: float; items: tuple[ProjectItem, ...]
@dataclass(frozen=True)
class SequenceInfo:  sequence_id: str; name: str; width: int; height: int; fps: float; duration: float; in_point: float | None; out_point: float | None
@dataclass(frozen=True)
class TrackItem:     sequence_id: str; track_kind: str ("video"|"audio"); track_index: int; index: int; name: str; node_id: str | None; start: float; end: float; in_point: float; out_point: float; selected: bool
@dataclass(frozen=True)
class Selection:     sequence_id: str; items: tuple[TrackItem, ...]
class PremiereError(Exception): message: str; line: int | None; source: str
class PremiereUnavailable(PremiereError): ...   # no agent connected
```

## 2. Operations

All methods are synchronous for the caller (they block the module thread for the round trip, typically 5-300 ms;
the snapshot of a big project up to a few seconds), raise `PremiereError` on a script error and
`PremiereUnavailable` when no agent is connected. Each row names its jsx file.

| Method | jsx | Behaviour |
|---|---|---|
| `is_connected() -> bool` | | agent connected and handshaken |
| `project() -> ProjectInfo | None` | `project_info.jsx` | `app.project.path`; `root` = the folder of the `.prproj`, or its parent when the parent holds a `.premiere-deck` folder and the project's own folder does not (Files layout with the project file in `Project\`; same rule as `hub.project_info_for`) |
| `snapshot() -> Snapshot` | `snapshot.jsx` | walks `app.project.rootItem` depth-first; paged when large (`PROTOCOL.md` 6.4) |
| `item(node_id) -> ProjectItem | None` | `item.jsx` | |
| `create_bin_path(tree_path) -> str` | `create_bin_path.jsx` | creates missing bins along the path, returns the leaf bin's node id; existing bins are reused by exact name |
| `move_to_bin(node_ids, tree_path)` | `move_to_bin.jsx` | `projectItem.moveBin(bin)` for each id; the bin path is created when missing; ids that do not exist are skipped and, after the existing ones were moved, reported as `PremiereError("project items not found: ...")` |
| `remove_items(node_ids) -> int` | `remove_items.jsx` | removes the project items from the project (files on disk are not touched). ExtendScript has no delete for clips, so the items are moved into a fresh holding bin and that bin is deleted. Returns the number removed; an id that no longer exists is skipped (the goal is reached) and logged at warning level, not raised |
| `relink(node_id, media_path) -> bool` | `relink.jsx` | `projectItem.changeMediaPath(path, true)`; returns whether the item is online afterwards |
| `import_files(paths, tree_path, *, suppress_dialogs=True) -> list[ProjectItem]` | `import_files.jsx` | `app.project.importFiles(paths, suppress, bin, false)`; returns only the items created (diff of the bin's children before and after); paths that do not exist (`missing`) or produced no item (`failed`) are logged at warning level and not raised, so the caller compares the result with its paths (7.2) |
| `rename_item(node_id, name)` | `rename_item.jsx` | |
| `set_color_label(node_ids, label)` | `set_color_label.jsx` | label index 0..15 (Premiere's order; the names are in `model.COLOR_LABELS`); missing ids are reported like in `move_to_bin` |
| `active_sequence() -> SequenceInfo | None` | `active_sequence.jsx` | |
| `sequences() -> list[SequenceInfo]` | `sequences.jsx` | |
| `selection() -> Selection` | `selection.jsx` | selected track items of the active sequence |
| `track_items(sequence_id) -> list[TrackItem]` | `track_items.jsx` | every clip on every track |
| `playhead() -> float` and `set_playhead(seconds)` | `playhead.jsx` | active sequence |
| `open_in_source(node_id, *, seconds=None)` | `open_in_source.jsx` | `app.sourceMonitor.openProjectItem(item)`; with `seconds`, the Source Monitor is positioned through the QE source player, else a clip marker "Deck" is added at that time (7.4) |
| `insert_clip(node_id, *, start, end, at=None, video_track=0, audio_track=0)` | `insert_clip.jsx` | sets in/out on the project item (`setInPoint/setOutPoint`), inserts at `at` (default: playhead) with `sequence.insertClip` |
| `add_marker(node_id_or_sequence_id, seconds, name, comment="")` | `add_marker.jsx` | clip marker on a project item or sequence marker |
| `save_project()` and `save_project_as(path)` | `save.jsx` | `app.project.save()` / `saveAs(path)`; `saveAs` makes `path` the open project |
| `open_project(path)` | `open_project.jsx` | `app.openDocument(path)` |
| `workspaces() -> list[str]` and `set_workspace(name)` | `workspace.jsx` | |
| `premiere_version() -> str` | `version.jsx` | `app.version` |
| `eval(name, code, args=None, *, timeout_ms=30000) -> Any` | | raw script (`PROTOCOL.md` 7); `code` may be a path to a `.jsx` file (read once and cached) |
| `script(path) -> str` | | the cached text of a jsx file |

Operations of the quick actions that need QE (`app.enableQE()`) or Media Encoder (`app.encoder`) live in the
module's own jsx (`QUICK_ACTIONS.md`); a second module that needs one asks the integrator to promote it here.

## 3. Serialisation and queueing

`PremiereApi(transport: AgentTransport, jsx_dir)` (`host/protocol.py` defines `AgentTransport`: `send(message)`,
`set_handler(fn)`, `set_presence_handler(fn)`, `connected`). The server implements it; a test passes a fake.

- One in-flight script per agent. Callers block on a `Future`; the facade's dispatcher runs on the server's
  asyncio loop. Timeout per call (`timeout_ms`), default 30 s; the snapshot uses 120 s.
- Results are decoded into the dataclasses above in Python (jsx returns plain objects with the field names of
  the dataclasses, snake_case).
- The facade logs every call as `name ms ok` at debug level and the error at warning level.

Hooks the hub uses (contract, not convention; the facade offers them besides the operations of section 2):

| Member | Behaviour |
|---|---|
| `bind_module_thread(thread=None, submit=None)` | from now on operations run on that thread only (default: the caller's); `submit(fn)` schedules `fn` on it |
| `run_on_module_thread(fn, *a, **kw) -> Future` | runs `fn` on the module thread (at once when already there) |
| `on_event(callback(name, data))` | every Premiere event (`PROTOCOL.md` 8) and the derived `project.opened` / `project.closed`; called on the transport thread, the receiver marshals onto its own |
| `set_events(names)` | the union of the modules' events: sent as `subscribe` now and after every reconnect |
| `close()` | host shutdown: queued and running calls fail with `PremiereUnavailable`, later calls fail at once |
| `project_path`, `active_sequence_id`, `hello` | read-only state from the last `hello` / `heartbeat` / event; no round trip |

`set_event_handler`, `set_subscriptions` and `set_module_executor` are aliases of `on_event`, `set_events` and the
`submit` argument of `bind_module_thread`; the hub uses the names in the table.

## 4. Events

`PremiereApi` owns the `subscribe` list (the union of all modules' `events`) and the poll fallback: it asks the
agent to poll `app.project.path` and the root item count every 2 s (`heartbeat` carries the path; a change in
the root item count or in the project path fires the matching `project.*` event). The registry delivers events to
the modules (`MODULES.md` 2).

## 5. jsx conventions

- Each file is a complete function body: it may use `__args` (the JSON arguments) and must `return` a plain
  value. `// @uses lib` in the first three lines prepends `lib.jsx`; the facade replaces the marker line with
  `// (uses lib)` before sending (7.6), and no other comment may start with `// @`.
- `lib.jsx` provides: `walk(root, fn)`, `findByNodeId(root, id)`, `binByPath(path, create)`, `itemInfo(item)`,
  `ticksToSeconds(t)`, `secondsToTicks(s)`, `pathOf(item)`, `safeMediaPath(item)`.
- Times: ExtendScript gives `Time` objects with `ticks` (strings) and `seconds`; scripts return seconds as numbers.
  `TICKS_PER_SECOND = 254016000000`.
- No dialogs (`alert`, `confirm`, `prompt`) and no `$.writeln`; errors are thrown and reach the host through
  `script.result`.
- Paths from Premiere are Windows paths with backslashes; the facade normalises case-insensitively when comparing.
- Everything a script does must be undoable by Premiere's own Undo where Premiere offers it; a script never saves
  the project on its own except `save_project*`.

## 6. Verified event names

Verified by the panel worker on Premiere Pro 2026, `app.version` = `26.5.2` (CEP 12.0, PlugPlug 12.0.1.2), on
2026-10-21/22, by triggering each case from a script and recording what reached the host
(`tests/panel/live_server.py`). The binding table lives in `panel/js/agent.js` (`EVENT_BINDINGS`); the payloads
are built in `panel/host.jsx` (`__deckBindingData`) from the current state, because no handler gets arguments.

| Protocol event | Premiere binding (26.5.2) | Verified | Notes |
|---|---|---|---|
| `project.items_added` | `onItemsAddedToProjectSuccess` (plural) | yes | fires for `importFiles` and `createBin`; no arguments, so `data` is `{}` and the host diffs snapshots. The singular `onItemAddedToProjectSuccess` is bound as well but never fired in 26.5.2. The 2 s poll adds the event when the root item count grows. |
| `project.changed` | `onProjectChanged` | yes | fires for nearly every change (import, bin, rename, timeline edit), often twice in a row; the agent coalesces 100 ms |
| `sequence.activated` | `onSequenceActivated` | yes | fires when another sequence becomes active (`openSequence`, `activeSequence = `, a new sequence from clips); not when the active one is opened again |
| `sequence.structure_changed` | `onActiveSequenceChanged`, `onActiveSequenceTrackItemAdded`, `onActiveSequenceTrackItemRemoved` | yes | `onActiveSequenceStructureChanged` is bound but never fired in 26.5.2; insert and remove fire the three listed names. `onActiveSequenceChanged` also fires when a project item used in the active sequence is renamed. |
| `sequence.selection_changed` | `onActiveSequenceSelectionChanged` | yes | fires for `setSelected` from a script, select and deselect; `data.count` is the new selection size |
| `project.opened` / `project.closed` | 2 s poll of `app.project.path` (agent) + `heartbeat` and `hello` (facade) | yes | `openDocument`, `newProject` and `saveAs` all switch the path; the facade emits `closed` for the old path then `opened` for the new one, once. With several open projects `app.project` is the active one, so `closed` means "no longer the current project". |
| `panel.opened` / `panel.closed` | host side | yes (dev server) | the display connects / disconnects |

Not mapped, seen while testing: `onSourceClipSelectedInProjectPanel` (fires several times per import and per bin
creation).

## 7. Known quirks (recorded here only)

7.1 `projectItem.changeMediaPath(path, overrideChecks)` returns a number in some versions and a boolean in others;
    `relink.jsx` ignores the value and reports `!isOffline()` afterwards (verified 26.5.2: relink to a copy, item
    online, `getMediaPath()` = the new path).
7.2 `importFiles(paths, true, bin, false)` and `changeMediaPath` are not reliable on 26.5.2. Observed: (a) for some
    files `importFiles` returns `true` and creates nothing (seen for audio-only files: one 44.1 kHz mono WAV and one
    MP3, while other WAV / MP3 files with the same format imported fine); (b) seven times a modal dialog appeared
    in spite of `suppressUI = true` and blocked the script until it was closed (7.3): title "File Import Failure",
    an empty file list, "Error Message: The importer reported a generic error." (text read on the screen on
    2026-10-22). It came from `importFiles` and, twice in the end-to-end run, from `changeMediaPath` to an
    outside file, also in a fresh project with fresh ffmpeg media. The failing and the working files had the same
    encoding (H.264 High yuv420p, 25 fps, `mdat` before `moov`, with or without AAC; ffprobe and the atom order
    compared): one file of a batch failed while its sibling from the same ffmpeg run imported, and the same file
    kept failing while later files worked. No root cause and no format workaround found. Handling:
    `import_files.jsx` checks every path with `File.exists` first (`missing`), imports images one at a time, and
    returns `failed` = requested paths with no new item; `relink.jsx` reports `online` / the media path after the
    call; the facade logs both lists at warning level and returns only the created items, so the module compares
    them with its paths. A dialog shows up to the module as a `timeout` of that script.
    Relink direction is not the trigger: in the end-to-end run the facade relinked inside the root four times
    (intercept to `Footage` and `Audio/SFX`, Collect to `Audio/SFX`) and outside the root once (Undo back to the original in
    `outside\`), all without a dialog, and the first live run relinked to a copy outside any project root
    without one; the two dialog relinks were test scripts, to `testpattern.mp4` (same ffmpeg batch as the failing
    import) and to a file written one second before the call. So the facade keeps `relink` for any target; no holding-bin reimport is needed for the Files cases.
7.3 Scripts that run while Premiere shows a modal dialog block until the dialog closes; the agent reports
    `timeout` after `timeout_ms` (the next script waits behind the blocked one), the facade's own watchdog fires
    5 s later if the agent is gone. The module shows "Premiere is waiting for a dialog".
7.4 The Source Monitor has no position setter in the DOM (`app.sourceMonitor` offers `getPosition` only, and
    setting the item's in point before `openProjectItem` does not move it). `open_in_source.jsx` positions it
    through QE: `app.enableQE(); qe.source.player.startScrubbing(); scrubTo("HH:MM:SS:FF"); endScrubbing()`, with a
    non-drop timecode at the clip's rounded frame rate (`getFootageInterpretation().frameRate`), then checks
    `getPosition()` within 1.5 frames. Verified 26.5.2 on 25 and 30 fps clips. Audio-only items (frame rate below
    1) and any failed check fall back to a clip marker named "Deck" at that time (`add_marker`).
7.5 Time remapping and clip speed cannot be set from ExtendScript (Premiere 26.3); nothing in Deck promises it.
7.6 ExtendScript reads a comment starting with `//@` or `// @` as a preprocessor directive; an unknown one
    (`// @uses lib`) is a "Syntax error" for the whole script. The facade replaces the marker line with
    `// (uses lib)` before sending (line count unchanged). Never start another comment in a jsx with `// @`.
7.7 ExtendScript (ES3) rejects a function declaration inside a block, including `try { }`, with "Syntax error",
    while Node and browsers accept it. The agent's wrapper therefore makes the script the body of a nested
    function and puts the `try` around its call (`panel/js/agent.js` `wrapScript`); scripts may declare
    functions anywhere at their top level.
7.8 A script that does not parse makes `evalScript` return only "EvalScript error."; the agent then compiles the
    same program with `new Function` inside `__deckCheckSyntax` to report the message and the line
    ("SyntaxError: ... does not have a value", line relative to the script).
7.9 `app.bind(name, fn)` keeps one handler per name for the whole ExtendScript engine, which every CEP extension
    shares: a later `app.bind` of the same name by any extension replaces Deck's handler (verified 26.5.2). No
    installed extension on the development machine binds these names today; if one does, Deck loses those events
    and relies on the 2 s poll. Deck never re-binds on a timer, so it does not steal events back.
7.10 Unset in / out points of a sequence read as `-400000` (seconds and ticks alike); `sequenceInfo` reports
    them as `null`. A project item without marks reports in = 0 and out = the media duration.
7.11 `getProjectColumnsMetadata()` is fast (under 1 ms per item) and holds `Column.Intrinsic.MediaDuration` in
    ticks plus `VideoInfo` / `AudioInfo` texts; `itemInfo` takes `duration`, `has_video` and `has_audio` from it.
    Stills have an empty `MediaDuration`, so their `duration` is `null`.
7.12 `insert_clip` ranges snap to sequence frames: inserting 1.0..2.5 s of a 30 fps clip into a 25 fps sequence
    gives 1.48 s on the timeline. The item's own in / out marks are restored after the insert (cleared when they
    spanned the whole media before).
7.13 `app.newProject(path)` returned `false` once while Premiere showed its Home screen and the project did not
    open; the same call succeeded later in the same session. Only the scratch tests create projects; Deck itself
    does not.
7.14 Premiere 26.5 started with a project path on its command line opens the project only when the path uses
    backslashes; with forward slashes Premiere starts, the agent connects, but `app.project.path` stays empty
    (verified 2026-10-22). `host/launcher.launch()` passes `os.path.normpath(project)`; the Windows file picker
    yields backslashes, recent-project entries and command-line hand-over may not.
7.15 `explorer /select,"<path>"` (host side, `shell/winutil.reveal_in_explorer`) opened a new window without the
    selection on the first call into a folder in 2 of 4 live calls; a second call selected the file. Minor, not
    worked around.
