# Quick actions module

Owner: actions worker (code and the details below), integrator (this contract). Module id `actions`. Tiles in the
panel for operations every editor performs many times a day that cost several steps in Premiere's own interface.

Code: `modules/quick_actions/module.py` (`QuickActionsModule`: rendering, routing, jobs, settings),
`action.py` (base class `Action`, `Menu`, `Outcome`, `ActionAbort`, helpers), `actions/<id>.py` (one class per action
with `id`, `label`, `title` (host row title when `label` is longer than 16 characters), `short`, `icon`, `hint`, `group`, `feasibility`, `long`, `switch`, `timeout_ms`,
`menu(module) -> Menu | None`, `field(module) -> dict | None`, `submit(module, name, value)`, `is_on(module)`,
`button(module)`, `run(ctx, module) -> Outcome`; actions of one kind share a base class in the first file of the
kind: `AddTrack`, `RemoveTrack`, `Reframe`, `Ease`; `actions/__init__.py` lists them in table order),
`jsx/<script>.jsx` (one script per scripted action or per kind: `add_track`, `remove_track`, `reframe`,
`ease_read` + `ease_apply`) plus `jsx/_common.jsx` (helpers prepended to every script), `easing.py` (section 1.1),
`premiere_window.py` (Premiere's menu commands, section 2.1), `results.py` (tolerant readers of script results).
The module keeps only the choices of the menus and the input, the visibility setting and the state of the two switch
actions (the panel keeps the last result of a group, `PANEL.md` 2). Module title (the tab): "Actions", icon
`actions`, `order` 5 (Files has 10), so the tree, the panel's tabs and the host window list Actions first and the
panel opens on it.

## 1. Actions

Every action was checked against the ExtendScript DOM and the QE DOM of Premiere Pro 2026 (26.5.2); the exact calls
and where they are documented are in each class's `feasibility` text, the live runs in `reports\actions_phase2.md`
(2.0) and `reports\r21_actions.md` (2.1). QE is undocumented: every QE call is guarded (existence checked, result
read back). Steps saved = what the user does in Premiere today.

| Group | id | Code | Label | Does | Calls (26.5.2): worked / did not | Steps saved today |
|---|---|---|---|---|---|---|
| Timeline | `adjustment_layer` | ADJ | Adjustment layer | reuses an adjustment layer already in the project (`ProjectItem.isAdjustmentLayer()`; the top-level `Deck` bin first, then the whole project depth-first; a reused item stays where it is); Premiere 26.5.2 offers no scripting call that creates one, so without one it fails with "No adjustment layer in the project. Create one once with File > New > Adjustment Layer; Deck reuses it."; only where QE offers `newAdjustmentLayer()` is the item `Adjustment <W>x<H>` created in `Deck`. It places it (`Track.overwriteClip`) on the first unlocked video track above the selection that is free over the placed span, spanning the selection's full extent, then trims it (`TrackItem.end`); 5 s at the playhead when nothing is selected; adds a video track (QE `addTracks`) when no track is free | worked: `overwriteClip`, `end` setter, QE `addTracks`; not offered: `qe.project.newAdjustmentLayer` | Project panel > New Item > Adjustment Layer > OK > drag > trim both ends |
| Timeline | `razor_all` | RZR | Razor all tracks | cuts every unlocked track that has a clip under the playhead (QE track `razor(timecode)`, timecode from `Time.getFormatted`) | worked (2.0) | Ctrl+Shift+K, which many never find |
| Timeline | `delete_empty_tracks` | EMPT | Delete empty tracks | removes unlocked video and audio tracks without clips, highest first (QE `removeVideoTrack` / `removeAudioTrack`); one track of each kind stays | worked (2.0) | right-click track header > Delete Tracks > tick boxes |
| Tracks | `add_video_track` | +V | Add video track | adds one video track above the highest: QE `addTracks(1, numVideo, 0, 1, numAudio, 0, 0)`; result "Added V3" | worked: QE `addTracks`, count read back from `videoTracks.numTracks` | Sequence > Add Tracks > dialog |
| Tracks | `add_audio_track` | +A | Add audio track | adds one audio track below the lowest: QE `addTracks(0, numVideo, 1, 1, numAudio, 0, 0)` (audio type 1, stereo); "Added A3" | worked: QE `addTracks`; the channel type of the new track is not readable by script (QE track `type` reads "Audio" for every audio track), the header looks like A1 / A2 | Sequence > Add Tracks > dialog |
| Tracks | `remove_video_track` | −V | Remove video track | removes the highest video track when it holds no clip and is unlocked: "Removed V3"; else `warn` "V3 is not empty" / "V3 is locked"; with one video track `neutral` "V1 is the only video track" | worked: QE `removeVideoTrack(index)`, count read back | right-click track header > Delete Tracks > tick |
| Tracks | `remove_audio_track` | −A | Remove audio track | the same for the lowest audio track (QE `removeAudioTrack`) | worked | right-click track header > Delete Tracks > tick |
| Tracks | `solo_track` | SOLO | Solo audio track | a `switch` (section 2.2): on = mutes every audio track except the one of the first selected audio clip ("Soloed A2"); off = restores the mute states from before ("Audio tracks restored") | worked (2.0): `Track.isMuted` / `setMute` | click M on every other track |
| Tracks | `lock_others` | LOCK | Lock other tracks | a `switch`: on = locks every track that holds no selected clip ("Locked 5 tracks"); off = restores the lock states ("Track locks restored") | worked (2.0): `Track.isLocked` / `setLocked` | click the lock on every track |
| Selection | `select_same_source` | SRC | Select same source | selects every clip in the active sequence whose project item is the first selected clip's; the rest is deselected | worked (2.0) | click each, one by one |
| Selection | `select_offline` | OFF | Select offline clips | selects every clip whose media is offline; the selection is unchanged when none is | worked (2.0) | none exists |
| Clips | `scale_to_frame` | FIT | Set to frame size | turns on "Set to Frame Size" for the selected video clips: per clip through the QE track item `setScaleToFrameSize(true)` (26.5.2 rejects the call without the flag), else on the clip's project item (`ProjectItem.setScaleToFrameSize`) | worked (2.0) | right-click each > Set to Frame Size |
| Clips | `colour` | CLR | Colour label | a `menu` of Premiere's 16 label colours (`model.COLOR_LABELS`: Violet, Iris, Caribbean, Lavender, Cerulean, Forest, Rose, Mango, Purple, Blue, Teal, Magenta, Tan, Green, Brown, Yellow; default Cerulean); a pick stores the colour and applies it at once, a press on the tile applies the stored one. Selected timeline clips: Premiere's own Edit > Label > <colour> on the Timeline (section 2.1); with no timeline selection, the items selected in the Project panel, bins included (`ProjectItem.setColorLabel`); the sequence marker and the selected clips' item markers at the playhead take the nearest of the 8 marker colours (`Marker.setColorByIndex`; Violet, Lavender, Purple -> Purple; Iris, Cerulean, Blue -> Blue; Caribbean, Teal -> Cyan; Forest, Green -> Green; Rose, Magenta -> Red; Mango, Brown -> Orange; Tan -> White; Yellow -> Yellow). Results "Coloured 2 clips Rose · 1 marker Red", "Coloured 2 items and 1 bin Forest", "Coloured 1 marker Blue", `warn` "Nothing selected". When the Label menu cannot be reached the clips' project items are labelled instead (`warn`, detail says clips in a sequence may keep their colour) | worked: Edit > Label menu command (13124 + label index) after Window > Timelines > <sequence>; `ProjectItem.setColorLabel` on items and bins; `Marker.setColorByIndex(0..7)`. Did not: no colour setter on a DOM `TrackItem` or a QE track item (reflected); a placed clip does not follow `ProjectItem.setColorLabel` (item read back as Mango, its timeline clip stayed Iris); a Label command posted while the Deck panel has the focus does nothing | right-click each > Label > colour |
| Clips | `label_by_bin` | BIN | Colour labels by bin | gives every clip and file item the colour of its top-level bin: Footage Cerulean, Audio Forest, Graphics Lavender, Overlays Mango, Motion Magenta; other top-level bins take Teal, Rose, Yellow, Purple, Caribbean, Tan, Green, Brown, Blue, Violet, Iris in alphabetical order, cycling; items at the root, bins and sequences are left alone; calls in chunks of 50. Limit: project items only; clips already in a sequence keep their own colour (see `colour`) | worked (2.0) | select items per bin > Label > colour, per bin |
| Project | `save_version` | SAVE | Save version | saves the project as `<stem>_v<NNN>.prproj` (a stem ending in `_v` + three or more digits is incremented, any other stem starts at `_v001`; the number grows past existing files) in the folder of the open project file, or in the layout's project folder (`project_dir` of `<root>\.premiere-deck\layout.json`, default `Project`, created when missing) when `<root>\.premiere-deck\project.json` exists; keeps editing the new file (facade `save_project_as`) | worked (2.0) | File > Save As > rename by hand |
| Project | `consolidate_duplicates` | CONS | Consolidate duplicates | `app.project.consolidateDuplicates()` | worked (2.0) | Edit > Consolidate Duplicates (menu, no shortcut) |
| Project | `open_project_folder` | DIR | Open project folder | Explorer at the project root (host side) | worked (2.0) | Reveal in Explorer via a clip |
| Project | `reveal_clip` | REV | Reveal clip file | Explorer with the first selected clip's file selected (host side, from the facade's `item().media_path`) | worked (2.0) | right-click > Reveal in Explorer |
| Sequence | `duplicate_sequence` | DUP | Duplicate sequence | `Sequence.clone()` of the active sequence, renamed `<name> v2`, `v3`, ... (`next_sequence_name`: a name ending in ` v<n>` counts from `n + 1`; the first free number is used); the original stays the active sequence | worked (2.0) | right-click the sequence > Duplicate > rename by hand |
| Sequence | `create_16_9` | 16:9 | Reframe 16:9 | creates `<name> 16x9` with `Sequence.autoReframeSequence(16, 9, "default", name, false)`, then sets the frame size: the source's short side is kept, the long side follows the ratio (1080x1920 -> 1920x1080; 1920x1080 stays); a name some sequence already has gets ` 2`, ` 3`, ... (`free_name`). "Created Spring 16x9 · 1920x1080" (`warn` " · frame size not changed to ..." when Premiere kept another size) | worked: `autoReframeSequence`, `getSettings` / `setSettings` frame size | Sequence > Auto Reframe Sequence > dialog > Sequence Settings |
| Sequence | `create_9_16` | 9:16 | Reframe 9:16 | the same at 9:16 (1920x1080 -> 1080x1920), name `<name> 9x16` | worked | as above |
| Sequence | `create_custom` | CUST | Reframe custom size | the `input` "Custom size" after the Sequence tiles (`create_custom.size`, placeholder "Width x height", button "Create"; `1080x1350`, `1080 x 1350`, `1080×1350` and `1080*1350` are accepted, each side 16 to 16384): a submit stores the size and runs the action; a press on the tile runs it with the stored size (default 1080x1350). Auto Reframe at the reduced ratio (1080x1350 -> 4:5), then that frame size, name `<name> <W>x<H>`. A refused value reports `warn` "Enter a size like 1080x1350" on the tile and stores nothing | worked | as above, with the ratio and size typed by hand |
| Export | `export_open` | EXP | Export | opens Premiere's Export mode for the active sequence: `Project.openSequence` in the script, then the host posts Window > Timelines > <sequence> and File > Export > Media... to Premiere's main window (section 2.1); "Export opened · Spring"; error "Export failed" when the menu command is not found | worked: the posted menu command (11240), also with the Deck panel or the Project panel focused (Export showed the active sequence as its source). Did not: `app.executeCommand` and `app.findMenuCommandId` do not exist; a synthetic Ctrl+M (keybd_event, with or without scan codes) reached Premiere as a plain M (it added a marker) while the Deck panel had the focus; `Project.openSequence` does not move the keyboard focus | Ctrl+M or File > Export > Media |
| Export | `export_frame` | FRM | Export frame | writes the frame at the playhead as `<root>\Exports\Frames\<sequence>_<HH-MM-SS-FF>.png` (QE `exportFramePNG`; `_2`, `_3` on a collision) and reveals it | worked (2.0) | Export Frame button > dialog > path > OK |
| Keys | `ease_slow` | SLOW | Ease slow | eases the keyframe pair around the playhead on every animated property of the first selected clip (a video clip first) with the cubic bezier (0.3, 0, 0.7, 1), baked (section 1.1); "Eased 2 properties · 98 keys", the property names as `detail` | worked: `getKeys`, `getValueAtKey`, `addKey`, `setValueAtKey`, `removeKey`, `keyExistsAtTime`, `setInterpolationTypeAtKey(t, 0)`. Did not: no bezier handle or keyframe selection accessor (DOM `ComponentParam`, QE component `getParamKeyframes` returns timecodes only) | Effect Controls: select keys > right-click > Bezier > drag handles per property |
| Keys | `ease_med` | MED | Ease medium | the same with (0.5, 0, 0.5, 1) | as above | as above |
| Keys | `ease_fast` | FAST | Ease fast | the same with (0.7, 0, 0.3, 1) | as above | as above |
| Keys | `ease_linear` | LIN | Linear keys | removes every key strictly between the pair (an earlier bake) and sets the pair to linear: "Linear · 2 properties · 98 keys removed" | as above | delete the keys by hand > right-click > Linear |

Dropped: `nest_selection` (Nest selection): ExtendScript has no Nest command (`sequence.createSubsequence()` makes a
separate sequence and leaves the clips in place). Dropped in 2.0: `remove_gaps`. Dropped in 2.1: `social_copy`
(replaced by the three Sequence tiles) and `export_sequence` with its export presets (`presets.py`, `presets\`, the
host page's presets block and the `<data>\presets\` folder); `export_open` hands the export to Premiere's own
Export mode instead.

Tiles: every action declares a sprite `icon` (`PANEL.md` 1) and its code as `short` (the Code column, at most 16
characters); the full `label` heads the tooltip, the `hint` is its second line (one sentence on what the action
does). Icons (2.0 ones unchanged): `track-video-add`, `track-audio-add`, `track-video-remove`, `track-audio-remove`,
`colour`, `ratio-16-9`, `ratio-9-16`, `ratio-custom`, `export`, `ease-slow`, `ease-med`, `ease-fast`,
`ease-linear` for the 2.1 actions.

Rendering: the section is the Actions tab of the panel (module `icon` `actions`). One `group` node per group of the
table, in the table's order (Timeline, Tracks, Selection, Clips, Project, Sequence, Export, Keys), with `id` = the
group name in lower case, titled with the group name, `tiles` true, `collapsible` true. Every visible action is one
`button` (or `switch`) with `id`, `label`, `short`, `icon` and `hint`; an action's `input` follows the tiles of its
group (only `create_custom.size`), so it never splits the tile grid. `colour` carries its choice as the button's
`menu` `colour.label` (`badge` = the colour name; every option carries a `swatch`, the hue of Premiere's default
label colour from `BE.Prefs.LabelColors.<n>` lifted so its brightest channel is 224, `colour.SWATCHES`). A pick of a `runs` menu (only `colour`) stores the choice and runs
the action; a submit of the input stores the value and runs the action. Results go through `ctx.ui.result` with the
action's id (section 4). Long actions (`label_by_bin`, `create_16_9`, `create_9_16`, `create_custom`,
`export_frame`, the four Keys actions) run as jobs with `busy` on their tile (and `disabled` on the input), the
section status "Running · <Label>", and a second press ignored while it runs. A job reaches Premiere through the
facade's `run_on_module_thread` when the facade has it (`MODULES.md` 2).

### 1.1 Keyframe easing (baked)

ExtendScript exposes key times, values and an interpolation type per key, but no bezier handles and no keyframe
selection (`PREMIERE_API.md` 7.17), so an ease is baked:

- Target: every `ComponentParam` with `isTimeVarying()` of the first selected clip (a video clip first) whose key
  values are numbers or lists of numbers (Position, Anchor Point); other properties are skipped. Key times are in
  the clip's media time: playhead in that time = playhead − clip start + clip in point.
- Pair: the keys k0 <= playhead < k1 (`easing.find_pair`). A run of keys exactly one sequence frame apart around the
  playhead (the last step of a run may be shorter) is taken as an earlier bake and the pair widens to the run's
  outer keys, so SLOW over FAST replaces the bake and LIN removes it with the playhead anywhere inside it. Premiere
  stores nothing that marks a baked key; two keys of the user's own one frame apart are treated the same way.
- Bake (`easing.plan`): keep k0 and k1, remove every key between them, add one key per sequence frame from k0 + 1
  frame while before k1, value = v0 + (v1 − v0) · y(x) with x the time fraction and y from the cubic bezier
  (`easing.bezier_y`, CSS `cubic-bezier` semantics; Newton steps with a bisection fallback); interpolation type 0
  (linear) on k0, every added key and k1. Presets: SLOW (0.3, 0, 0.7, 1), MED (0.5, 0, 0.5, 1), FAST (0.7, 0, 0.3,
  1): symmetric, so the middle of the pair is always the midpoint value; FAST pulls the handles furthest.
- LIN: remove every key strictly between the pair, type 0 on k0 and k1.
- Two scripts per press: `ease_read.jsx` (keys, values, frame, playhead) and `ease_apply.jsx` (removes, adds, sets
  linear; the clip is found again by track and start). Timeout 60 s each; the action runs as a job. No pair:
  `warn` "Place the playhead between two keyframes"; no animated property: `warn` "The selected clip has no
  keyframes". How Premiere's Undo groups the key changes was not checked live.
- Measured on 26.5.2 (25 fps, keys 1 s / 3 s, Scale 100 -> 150): MED wrote 49 keys per property, Scale 100.027 at
  1.04 s, 125 at 2 s, 149.973 at 2.96 s, equal to `easing.bake`; LIN went back to 2 keys and linear values (112.5
  at 1.5 s); SLOW over a FAST bake replaced it (51 keys, SLOW values).

## 2. Premiere's menu commands and switches

### 2.1 Menu commands (`premiere_window.py`)

Where ExtendScript has no call (Export mode, the Label of timeline clips) the host runs Premiere's own menu command:
it finds the main window (visible, title starting "Adobe Premiere", with a menu bar), reads its native menu bar
(Win32 `GetMenu`), resolves each command by its id when the bar holds it, else by its label path ("File" >
"Export" > "Media..."; mnemonic `&` and accelerators ignored), and posts them in order as `WM_COMMAND`. Commands
that act on the focused panel are preceded by Window > Timelines > <sequence name>, which focuses that Timeline;
the next posted command runs after it. A required command that is missing makes the whole run fail before anything
is posted (`colour` then labels the project items; `export_open` reports the error). Ids on 26.5.2: Media... 11240,
Edit > Label > Violet ... Yellow 13124 ... 13139 (label order), Window > Timelines entries are dynamic. Menu states
are refreshed only when a menu opens, so a greyed flag is not checked. Windows only; elsewhere `run` returns False.

### 2.2 Switches

`solo_track` and `lock_others` render as `switch` nodes (`PROTOCOL.md` 6.1): `value` = the module remembers the
states to restore (on = applied). A click sends `action` with the requested `value`: a value equal to the current
state runs nothing (the next tree shows it); otherwise the action runs (apply or restore) and reports one result. A
refused apply ("Select a clip with audio", "No other tracks to lock") leaves the state off, so the next tree shows
the switch off again with the result. The state is forgotten when the project closes. Labels stay the same in both
states (the 2.0 "Restore audio" / "Restore locks" labels are gone).

## 3. Visibility, choices and the host page

The host window lists every action with a switch (hide actions the user never needs); hidden actions are not
rendered (nor their input), a group without visible actions is not rendered, and when all are hidden the section
shows one line pointing to the host window. Settings (host scope, under `modules.actions`):

| Key | Value | On a bad value |
|---|---|---|
| `hidden` | list of action ids | not a list: nothing hidden; unknown ids and non-strings ignored |
| `choices` | `{"colour.label": "<colour name>", "create_custom.size": "<W>x<H>"}` | not a dict, an unknown colour or a size that does not parse: the default (Cerulean, 1080x1350) |

Actions run from the panel only; the host window's Actions tab is the configuration of the set. `render_host()`
returns `{"title", "blocks": [...]}` with one `list` block `visibility` (`BRIDGE_API.md` 5; a row per action: `id`,
`title` = the action's `title` or else its `label`, at most 16 characters so the two-column list does not cut it
("Remove V track", "Add A track", "Reframe custom"), `icon` = the action's sprite icon, `subtitle` = "<Group> ·
<hint>", `toggle` = `{"id": "actions.host:visible.<id>", "value"}`), in table order. `settings_schema()` is empty.

## 4. Results and errors

Each press reports one result through `ctx.ui.result(<action id>, outcome, text, detail)` (`MODULES.md` 4), never
a toast; outcomes: `ok` done, `neutral` nothing to do or a restore, `warn` a precondition or a partial result,
`error` a failure. The panel shows it on the tile and in the group's result line; a line with `detail` opens it on
click.

| Case | Result text |
|---|---|
| no project / no active sequence / empty selection | "No project open", "No active sequence", "Nothing selected" (warn) |
| agent not connected | "Premiere Pro is not connected" (warn) |
| `adjustment_layer` | "Adjustment layer added · V3 · 00:00:12 to 00:00:18" (+ " · new item in the Deck bin" where QE could create it); error "Adjustment layer failed" with `detail` "No adjustment layer in the project. Create one once with File > New > Adjustment Layer; Deck reuses it." |
| `razor_all` | "Cut 3 tracks at 00:01:02" (+ " · 1 locked track skipped"), "No clip under the playhead", warn "Nothing cut" with `detail` "The clips under the playhead are on locked tracks." |
| `delete_empty_tracks` | "Deleted 2 video tracks and 1 audio track", "No empty tracks" |
| track tiles | "Added V3", "Added A3", "Removed V3", warn "V3 is not empty", warn "V3 is locked", "V1 is the only video track" |
| `solo_track` / `lock_others` | "Soloed A2", "Audio tracks restored", "Locked 5 tracks", "Track locks restored", "No other tracks to lock", warn "Select a clip with audio" |
| `select_same_source` / `select_offline` | "Selected 7 clips of interview.mp4", "Selected 2 offline clips" (warn), "No offline clips" |
| `scale_to_frame` | "Set to frame size · 3 clips", "Select a video clip" (warn) |
| `colour` | "Coloured 2 clips Rose · 1 marker Red", "Coloured 2 items and 1 bin Forest", "Coloured 1 marker Blue", warn "Nothing selected" |
| `label_by_bin` | "Labelled 124 items in 6 bins", "Labels already match the bins", "No items in bins" |
| `save_version` | "Saved as Spring_v004.prproj" |
| `duplicate_sequence` | "Duplicated as Spring v2" |
| reframe tiles | "Created Spring 9x16 · 1080x1920" (warn with " · frame size not changed to ..."), warn "Enter a size like 1080x1350" |
| `export_open` / `export_frame` | "Export opened · Spring", "Frame saved · Spring_00-00-12-05.png" |
| Keys tiles | "Eased 3 properties · 147 keys", "Linear · 3 properties · 147 keys removed", warn "Place the playhead between two keyframes", warn "The selected clip has no keyframes" |

A `PremiereError` becomes the result `error` "<Label> failed" with `detail` = the sentence below, and message, line
and source in the log. Scripts throw `deckFail(sentence)` (message `deck: <sentence>`) for conditions the user can
act on; only those sentences are shown. A `timeout` shows "Premiere is waiting for a dialog." (`PREMIERE_API.md`
7.3); any other script error shows "Premiere reported a script error."; a Python exception in an action shows "An
internal error occurred. Details are in the log." Actions never show Premiere dialogs (Export mode is Premiere's
own screen, opened on purpose).

## 5. Scripts

Each action script follows `PREMIERE_API.md` 5 (a function body using `__args`, returning a plain object, ES3, no
dialogs). The module sends `_common.jsx` + `<script>.jsx` through `ctx.premiere.eval("actions.<script>", text, args,
timeout_ms)`; a `// @uses lib` line in a script's first three lines is moved to the top. Helpers of `_common.jsx`
start with `deck` (`deckFail`, `deckTracks`, `deckTimecode`, `deckFindQeClip`, `deckLocate`, `deckTopBin`,
`deckFreePath`, ...) and are function expressions. Ticks cross the bridge as strings. Timeouts: 30 s default, 60 s
`export_frame` and the Keys scripts, 600 s the reframe tiles (Auto Reframe analyses the whole sequence).

## 6. Tests

`tests/test_modules_quick_actions.py` with `FakeHost` (canned sequence, selection, snapshot and per-script eval
results; `premiere_window.run` is patched in every test, so no test reaches a Premiere window): every action's
happy path and results, no active sequence, nothing selected, no project, not connected, script errors (user
sentence, raw error, timeout, Python bug); the track tiles' four outcomes; the switches' state round trip (on, on
again runs nothing, off, refused apply stays off, project closed); `colour` (the 16 options, a pick runs, the menu
commands, the project-item fallback, items, bins, markers, nothing selected); reframe arguments, sizes and free
names; the custom size input (render, submit, refused values, stored value, hidden with its action, disabled while
running); `export_open` with its menu commands; the easing math (bezier against the parametric curve, strength
order, bake times and values incl. the live values, lists, a short last step, pair detection over a bake, plans for
a preset, a re-ease and LIN) and the Keys actions end to end; render order, group ids, kinds, codes and icons;
removed actions and the presets module absent; settings fallback; the host page; every script ES3-clean and
parsed by `node --check` when Node is installed.
