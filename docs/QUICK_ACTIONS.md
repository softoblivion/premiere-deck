# Quick actions module

Owner: actions worker (code and the details below), integrator (this contract). Module id `actions`. Buttons in the
panel for operations every editor performs many times a day that cost several steps in Premiere's own interface.

Code: `modules/quick_actions/module.py` (`QuickActionsModule`: rendering, routing, jobs, settings),
`action.py` (base class `Action`, `Menu`, `Outcome`, `ActionAbort`, helpers), `actions/<id>.py` (one class per action with
`id`, `label`, `short`, `icon`, `hint`, `group`, `feasibility`, `long`, `timeout_ms`, `menu(module) -> Menu | None`,
`button(module)`, `run(ctx, module) -> Outcome`; `actions/__init__.py` lists them in table order),
`jsx/<id>.jsx` (one script per scripted action) plus `jsx/_common.jsx` (helpers prepended to every script),
`presets.py` and `presets/` (section 2), `results.py` (tolerant readers of script results). The module keeps only
the choices of the menus, the visibility setting and the state of the two toggle actions (the panel keeps the
last result of a group, `PANEL.md` 2). Module title (the tab): "Actions", icon `actions`.

## 1. Actions

Every action was checked against the ExtendScript DOM and the QE DOM of Premiere Pro 2026 (26.x); the exact calls
and where they are documented are in each class's `feasibility` text. QE is undocumented: every QE call is
guarded (existence checked, result read back) and listed in the phase 2 checklist (`reports\actions.md`).
Steps saved = what the user does in Premiere today.

| Group | id | Label | Does | Steps saved today |
|---|---|---|---|---|
| Timeline | `adjustment_layer` | Adjustment layer | reuses an adjustment layer already in the project (`ProjectItem.isAdjustmentLayer()`; the top-level `Deck` bin first, then the whole project depth-first; a reused item stays where it is); Premiere 26.5.2 offers no scripting call that creates one (QE `qe.project` has no `newAdjustmentLayer`), so without one it fails with "No adjustment layer in the project. Create one once with File > New > Adjustment Layer; Deck reuses it."; only where QE offers `newAdjustmentLayer()` is the item `Adjustment <W>x<H>` created in `Deck`. It places it (`Track.overwriteClip`) on the first unlocked video track above the selection that is free over the placed span, spanning the selection's full extent, then trims it (`TrackItem.end`); 5 s at the playhead when nothing is selected; adds a video track (QE `addTracks`) when no track is free | Project panel > New Item > Adjustment Layer > OK > drag > trim both ends |
| Timeline | `razor_all` | Razor all tracks | cuts every unlocked track that has a clip under the playhead (QE track `razor(timecode)`, timecode from `Time.getFormatted`) | Ctrl+Shift+K, which many never find |
| Timeline | `delete_empty_tracks` | Delete empty tracks | removes unlocked video and audio tracks without clips, highest first (QE `removeVideoTrack` / `removeAudioTrack`); one track of each kind stays | right-click track header > Delete Tracks > tick boxes |
| Selection | `select_same_source` | Select same source | selects every clip in the active sequence whose project item is the first selected clip's; the rest is deselected | click each, one by one |
| Selection | `select_offline` | Select offline clips | selects every clip whose media is offline; the selection is unchanged when none is | none exists |
| Clips | `scale_to_frame` | Set to frame size | turns on "Set to Frame Size" for the selected video clips: per clip through the QE track item where it offers `setScaleToFrameSize` (called as `setScaleToFrameSize(true)`: 26.5.2 rejects the call without the flag; the Motion Scale value stays 100, the clip's fit changes), else on the clip's project item (`ProjectItem.setScaleToFrameSize`) | right-click each > Set to Frame Size |
| Clips | `label_by_bin` | Colour labels by bin | gives every clip and file item the colour of its top-level bin: Footage Cerulean, Audio Forest, Graphics Lavender, Overlays Mango, Motion Magenta; other top-level bins take Teal, Rose, Yellow, Purple, Caribbean, Tan, Green, Brown, Blue, Violet, Iris in alphabetical order, cycling; items at the root, bins and sequences are left alone; calls in chunks of 50. Limit: the action labels project items only; ExtendScript 26.5.2 has no colour getter or setter on a `TrackItem`, and clips already in a sequence do not reliably take the new colour (live run: two placed clips of one item changed, one placed clip of another item kept its old colour) | select items per bin > Label > colour, per bin |
| Tracks | `solo_track` | Solo audio track | mutes every audio track except the one of the first selected audio clip; the button then reads "Restore audio" and the second press restores the mute states from before | click M on every other track |
| Tracks | `lock_others` | Lock other tracks | locks every track that holds no selected clip; the button then reads "Restore locks" and the second press restores the lock states from before | click the lock on every track |
| Project | `save_version` | Save version | saves the project as `<stem>_v<NNN>.prproj` (a stem ending in `_v` + three or more digits is incremented, any other stem starts at `_v001`; the number grows past existing files) in the folder of the open project file, or in the layout's project folder (`project_dir` of `<root>\.premiere-deck\layout.json`, default `Project`, created when missing) when `<root>\.premiere-deck\project.json` exists; keeps editing the new file (facade `save_project_as`) | File > Save As > rename by hand |
| Project | `consolidate_duplicates` | Consolidate duplicates | `app.project.consolidateDuplicates()` | Edit > Consolidate Duplicates (menu, no shortcut) |
| Project | `open_project_folder` | Open project folder | Explorer at the project root (host side) | Reveal in Explorer via a clip |
| Project | `reveal_clip` | Reveal clip file | Explorer with the first selected clip's file selected (host side, from the facade's `item().media_path`) | right-click > Reveal in Explorer |
| Sequence | `duplicate_sequence` | Duplicate sequence | `Sequence.clone()` of the active sequence (the copy lands in the original's bin; the script finds it by comparing `app.project.sequences` before and after) and renames the copy `<name> v2`, `v3`, ...: a name already ending in ` v<n>` is taken as the base without that suffix and counting starts at `n + 1`, otherwise at 2; the first number whose name no sequence of the project has is used (`Spring` -> `Spring v2`; `Spring v2` -> `Spring v3`, or `Spring v4` when `Spring v3` exists). The original stays the active sequence | right-click the sequence > Duplicate > rename by hand |
| Sequence | `social_copy` | Social copy | menu "Format" (9:16, 1:1, 4:5); creates `<name> 9x16` (or `1x1`, `4x5`) with `sequence.autoReframeSequence(n, d, "default", name, false)`, then sets its frame size: the source's short side becomes the width, the height follows the ratio (1920x1080 -> 1080x1920, 1080x1080, 1080x1350) | Sequence > Auto Reframe Sequence > dialog > Sequence Settings |
| Export | `export_frame` | Export frame | writes the frame at the playhead as `<root>\Exports\Frames\<sequence>_<HH-MM-SS-FF>.png` (QE `exportFramePNG`; `_2`, `_3` on a collision) and reveals it | Export Frame button > dialog > path > OK |
| Export | `export_sequence` | Export sequence | menu "Preset" (section 2); queues the active sequence in Adobe Media Encoder (`app.encoder.encodeSequence`, in to out when both points are set, else entire) into `<root>\Exports\<sequence>.<ext>` (extension from `getExportFileExtension`; `_2`, `_3` on a collision); the queue is not started | Ctrl+M > preset > name > folder > Queue |

Dropped: `nest_selection` (Nest selection). ExtendScript has no Nest command: `sequence.createSubsequence()` only
creates a separate sequence from the in/out range and leaves the selected clips in place, and neither the DOM nor
QE offers a call that replaces clips with a nested sequence. Its `input` node is dropped with it. Dropped in 2.0:
`remove_gaps` (Remove all gaps); `duplicate_sequence` took its place in the set.

Tiles: every action declares a sprite `icon` (`PANEL.md` 1) and a `short` tile label (at most 16 characters); the
full `label` heads the tooltip, the `hint` is its second line (one sentence on what the action does).

| id | `icon` | `short` |
|---|---|---|
| `adjustment_layer` | `adjustment-layer` | Adjustment layer |
| `razor_all` | `razor-all` | Razor all |
| `delete_empty_tracks` | `delete-empty-tracks` | Empty tracks |
| `select_same_source` | `select-same-source` | Same source |
| `select_offline` | `select-offline` | Offline clips |
| `scale_to_frame` | `scale-to-frame` | Frame size |
| `label_by_bin` | `label-by-bin` | Label by bin |
| `solo_track` | `solo-track` | Solo track (while soloed: Restore audio) |
| `lock_others` | `lock-others` | Lock others (while locked: Restore locks) |
| `save_version` | `save-version` | Save version |
| `consolidate_duplicates` | `consolidate` | Consolidate |
| `open_project_folder` | `open-folder` | Project folder |
| `reveal_clip` | `reveal-clip` | Reveal clip |
| `duplicate_sequence` | `duplicate-sequence` | Duplicate |
| `social_copy` | `social-copy` | Social copy |
| `export_frame` | `export-frame` | Export frame |
| `export_sequence` | `export-sequence` | Export |

Rendering: the section is the Actions tab of the panel (module `icon` `actions`). One `group` node per group of the
table, in the table's order, with `id` = the group name in lower case (`timeline`, `selection`, `clips`, `tracks`,
`project`, `sequence`, `export`), titled with the group name, `tiles` true (three columns), `collapsible` true, so
the panel lays the actions out as icon tiles under a caption that collapses the group (the panel remembers it per
group id). Every visible action is one `button` with `id`, `label`, `short`, `icon` and `hint`. An action with a
choice carries it as the button's `menu` instead of a separate control: `social_copy` the menu `social_copy.format`
(options 9:16, 1:1, 4:5; `badge` = the current option, "9:16"), `export_sequence` the menu `export_sequence.preset`
(options = the presets of section 2; `badge` = a short form of the current preset, at most 8 characters:
`youtube_1080p` "YT 1080p", `instagram_9x16` "IG 9:16", `tiktok_9x16` "TT 9:16", `match_h264` "H.264", a user
preset its first 8 characters). A pick sends the menu id with the value, which the module stores as the choice (section 3) and does
not run the action; a press on the tile runs it with the current choice. Results go through `ctx.ui.result` with
the action's button id (section 4); the panel morphs the tile and keeps the group's result line, so the module
renders no result line of its own. Long actions (`label_by_bin`, `social_copy`, `export_frame`,
`export_sequence`) run as jobs with `busy` on their button, the section status "Running · <Label>", and a second
press ignored while it runs; their result is sent when the job ends. A job reaches Premiere through the facade's
`run_on_module_thread` when the facade has it (`MODULES.md` 2). `export_sequence` is disabled when no preset is
found.

## 2. Presets

The menu of `export_sequence` lists four built-in presets (YouTube 1080p, Instagram 9:16, TikTok 9:16, Match
source H.264), each resolved to the first file that exists: the `.epr` shipped in `modules/quick_actions/presets/`,
then Adobe's own system preset of the installed Premiere Pro or Media Encoder (read only); a preset without a file
is not listed. Every `.epr` the user drops into `<data>\presets\` follows as an extra option named after the file.
The list is read at start, on project open and on "Rescan" in the host window. File names, fallbacks, the exact
export settings and the export status are in `presets/PRESETS.md`. No `.epr` is shipped yet: ExtendScript cannot
save an export preset, and saving one from the Export dialog writes into the user's own preset folder, so the
system-preset fallback is what runs (verified live on 26.5.2); the manual steps to make the four files are in
`PRESETS.md`. Without shipped files the three social options (Instagram 9:16, TikTok 9:16, Match source H.264)
all resolve to the same system preset (Media Encoder shows it as "Match Source - Adaptive High Bitrate") and export
the sequence's own frame size; only YouTube 1080p differs ("YouTube 1080p Full HD").

## 3. Visibility, choices and the host page

The host window lists every action with a switch (hide actions the user never needs); hidden actions are not
rendered, a group without visible actions is not rendered, and when all are hidden the section shows one line
pointing to the host window. Settings (host scope, under `modules.actions`):

| Key | Value | On a bad value |
|---|---|---|
| `hidden` | list of action ids | not a list: nothing hidden; unknown ids and non-strings ignored |
| `choices` | `{"<action>.<control>": value}` (`social_copy.format`, `export_sequence.preset`) | not a dict or a value that is no longer an option: the menu's default |

Actions run from the panel only; the host window's Actions tab is the configuration of the set. `render_host()`
returns `{"title", "blocks": [...]}` with two `list` blocks (`BRIDGE_API.md` 5; the window lays the `visibility`
rows out in two columns grouped by the action's group): `visibility` (a row per action: `id`, `title`, `subtitle` =
"<Group> · <hint>", `toggle` = `{"id": "actions.host:visible.<id>", "value"}`) and `presets` (a row per preset
with its source, `actions`: `actions.host:open_presets` opens `<data>\presets\` creating it, and
`actions.host:refresh_presets` rescans). `settings_schema()` is empty: both settings are edited on this page.

## 4. Results and errors

Each press reports one result through `ctx.ui.result(<action id>, outcome, text, detail)` (`MODULES.md` 4), never
a toast; outcomes: `ok` done, `neutral` nothing to do or a restore, `warn` a precondition or a partial result,
`error` a failure. The panel shows it on the tile for 2 s and in the group's result line with the time
("Adjustment layer added · 12:03"); a line with `detail` opens it on click.

| Case | Result text |
|---|---|
| no project / no active sequence / empty selection | "No project open", "No active sequence", "Nothing selected" (warn) |
| agent not connected | "Premiere Pro is not connected" (warn) |
| `adjustment_layer` | "Adjustment layer added · V3 · 00:00:12 to 00:00:18" (+ " · new item in the Deck bin" where QE could create it); error "Adjustment layer failed" with `detail` "No adjustment layer in the project. Create one once with File > New > Adjustment Layer; Deck reuses it." |
| `duplicate_sequence` | "Duplicated as Spring v2" (the name is chosen in the host from `Project.sequences`, `next_sequence_name`; the script clones, finds the copy, renames it and makes the original active again) |
| `razor_all` | "Cut 3 tracks at 00:01:02" (+ " · 1 locked track skipped"), "No clip under the playhead", warn "Nothing cut" with `detail` "The clips under the playhead are on locked tracks." |
| `delete_empty_tracks` | "Deleted 2 video tracks and 1 audio track", "No empty tracks" |
| `select_same_source` / `select_offline` | "Selected 7 clips of interview.mp4", "Selected 2 offline clips" (warn), "No offline clips" |
| `scale_to_frame` | "Set to frame size · 3 clips", "Select a video clip" (warn) |
| `label_by_bin` | "Labelled 124 items in 6 bins", "Labels already match the bins", "No items in bins" |
| `solo_track` / `lock_others` | "Soloed A2", "Audio tracks restored", "Locked 5 tracks", "Track locks restored" |
| `save_version` | "Saved as Spring_v004.prproj" |
| `social_copy` | "Created Spring 9x16 · 1080x1920" (warn with " · frame size not changed to ..." when Premiere kept another size) |
| `export_frame` / `export_sequence` | "Frame saved · Spring_00-00-12-05.png", "Queued in Media Encoder · Spring.mp4" |

A `PremiereError` becomes the result `error` "<Label> failed" with `detail` = the sentence below, and message, line
and source in the log.
Scripts throw `deckFail(sentence)` (message `deck: <sentence>`) for conditions the user can act on; only those
sentences are shown. A `timeout` shows "Premiere is waiting for a dialog." (`PREMIERE_API.md` 7.3); any other script
error shows "Premiere reported a script error."; a Python exception in an action shows "An internal error
occurred. Details are in the log." Actions never show Premiere dialogs.

## 5. Scripts

Each action script follows `PREMIERE_API.md` 5 (a function body using `__args`, returning a plain object, ES3, no
dialogs). The module sends `_common.jsx` + `<id>.jsx` through `ctx.premiere.eval("actions.<id>", text, args,
timeout_ms)`; a `// @uses lib` line in a script's first three lines is moved to the top. Helpers of `_common.jsx`
start with `deck` (`deckFail`, `deckTracks`, `deckTimecode`, `deckFindQeClip`, `deckTopBin`, `deckFreePath`, ...)
and are function expressions (no declarations inside the agent's `try` block). Timeouts: 30 s default, 60 s
`export_frame`, 120 s `export_sequence` (the first call starts Media Encoder), 600 s `social_copy` (Auto Reframe
analyses the whole sequence).

## 6. Tests

`tests/test_modules_quick_actions.py` with `FakeHost` (canned sequence, selection, snapshot and per-script eval
results): every action's happy path and results (`ctx.ui.result` with the action id, outcome, text and detail; no
toast for a press), no active sequence, nothing selected, no project, not connected, script errors (user sentence
as `detail`, raw error, timeout, Python bug); `save_version` numbering on a scratch folder (`_v003` -> `_v004`, no
version -> `_v001`, collisions, `_v999` -> `_v1000`) and the layout folder rules; `duplicate_sequence` naming
(`Spring` -> `Spring v2`, `Spring v2` -> `Spring v3`, taken numbers skipped, a name like `Mix v` or `v2` alone);
arguments of `social_copy` (ratio, name, size, timeout) and `export_*` (preset path, folder, name, in to out);
preset resolution (shipped, system, user, none); busy state of a job; render order, group ids, `tiles` and
`collapsible`, every action's `icon` and `short` (at most 16 characters), the two menus (options, badge, a pick
stores the choice and runs nothing), hidden actions, all hidden, `remove_gaps` absent;
settings fallback on broken values; the host page; every script ES3-clean and parsed by `node --check` when Node
is installed.
