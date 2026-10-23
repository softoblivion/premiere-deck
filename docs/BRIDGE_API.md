# Host window bridge (JS <-> Python)

The only definition of what the host window's page (`src/premiere_deck/ui/`) and the Python
side (`src/premiere_deck/shell/bridge.py`, backed by `host/hub.py`) say to each other. The mock
(`ui/js/mock.js`, `index.html?mock=1`) implements the same contract with canned data. Same shape as Media Forge's
`docs/BRIDGE_API.md`; the update control follows the Liquid UI kit contract (kit docs, section 11), the themes are
the project's own two (3.3, `HOST_UI.md` 6).

## 1. Transport

- pywebview `js_api`: `window.pywebview.api.<method>(...)` returns a Promise. A rejected Promise carries one English
  sentence for the user (`error.message`); the page shows it as a toast and never shows a stack.
- Python -> JS events: `window.premiereDeck.dispatch(name, payload)`, delivered in batches by one pump thread,
  buffered until the page called `ready()`. `job` events are coalesced per job id (only the newest state of a job is
  delivered when the page is slower than the jobs).
- The page wraps both in `ui/js/api.js`: `call(name, ...args)` and `on(name, fn)`; with `?mock=1` they go to
  `mock.js` instead.

## 2. Methods

| Method | Returns | Notes |
|---|---|---|
| `ready()` | `State` (3.1) | starts event delivery; call once after the page is built |
| `get_state()` | `State` | |
| `get_settings()` | `Settings` (3.3) | |
| `set_settings(patch)` | `Settings` | keys of 3.3 only; refused patch -> rejection with the sentence; a `settings` event re-sends the confirmed values |
| `set_scale(value)` | `Settings` | the Scale setting (3.3 `ui_scale`): saves the step (a number between the steps snaps to the nearest) and applies it to the window at once (section 6); a patch of `ui_scale` through `set_settings` does the same |
| `launch_premiere(project)` | `PremiereState` (3.2) | `project` = `.prproj` path or `null`. Premiere already running + project: opened through the panel; already running without a project: rejection "Premiere Pro is already running." |
| `pick_project()` | path or `null` | file dialog, `*.prproj` |
| `pick_premiere_exe()` | `Settings` or `null` | file dialog for `Adobe Premiere Pro.exe`; stores `premiere_exe` |
| `recent_projects()` | `Recent[]` (3.5) | |
| `modules()` | `ModuleInfo[]` (3.4) | |
| `set_module_enabled(id, enabled)` | `ModuleInfo` | starts / stops the module |
| `module_page(id)` | `{module: ModuleInfo, page: Page or null}` | `page` = the module's `render_host()` (section 5); `null` when the module has no page or is not running |
| `module_action(id, action, value, row)` | `{ok: bool, toast: [tone, text] or null}` | `action` = a node id from the page (`files.sort`, or local `sort`, or window-only `host:<local>`); `value` and `row` optional. The module's own toast also arrives as a `toast` event, its result (`ctx.ui.result`) as a `result` event |
| `module_settings(id)` | `{module, fields: Field[], project: name or null}` | `Field` = 3.6 |
| `set_module_setting(id, key, value, scope)` | the stored value | `scope` = `host` or `project`; converted to the field's kind; rejection with a sentence when it cannot be |
| `panel_status()` | `PanelStatus` (3.7) | |
| `panel_reinstall()` | `PanelStatus` | copies the bundled panel again (docs/PANEL.md 4); the waiting state's "Reinstall panel" |
| `panel_check_update()` | `{"updated": bool, "installed": version or null, "bundled": version}` | compares the installed panel with the bundled one and reinstalls when they differ, through the same code path as the start-up check (docs/PANEL.md 4.2); `installed` is the version found before the call; the waiting state's "Check panel update". A `panel` event follows when files were replaced |
| `panel_remove()` | `{removed: bool, registry_removed: string[]}` | docs/PANEL.md 4.4 |
| `panel_debug_mode(enable)` | `{written_by_host: string[], enabled: string[]}` | writes / removes `PlayerDebugMode`; the page asks for confirmation BEFORE calling with `true` |
| `ffmpeg_status()` | `Ffmpeg` (3.8) | |
| `ffmpeg_download()` | `Ffmpeg` | starts the download as a job (`job` events); resolves when it finished |
| `answer_confirm(id, yes)` | bool | answer to a `confirm` event |
| `open_data_folder()` | bool | |
| `reveal(path)` / `open_path(path)` / `open_url(url)` | bool | `open_url` accepts `https://` only |
| `window_minimize()` | `null` | |
| `window_close()` | `null` | hides to the tray while Premiere runs and `keep_running_with_premiere` is on, else quits |
| `quit()` | `null` | quits even while Premiere runs |
| `update_status()` / `check_updates()` / `download_update()` | `UpdateStatus` | kit contract (section 11) |
| `apply_update()` | bool | the app closes after `true` |
| `fake_premiere(state)` | `PremiereState` | `--fake` only (else rejected "Only available in the simulated mode."): `not_running` stops the fake agent and its display, `waiting` runs the agent without a display, `connected` adds the display; the `premiere` and `window` events follow from the real connections. The page binds it to Alt+1 / Alt+2 / Alt+3 in `--fake` and in the mock |
| `log(level, message)` | `null` | `debug` / `info` / `warning` / `error`, logger `premiere_deck.ui` |

## 3. Payloads

### 3.1 `State`

```json
{"version": "0.1.0", "fake": false, "data_dir": "D:\\Apps\\PremiereDeck\\premiere-deck_data",
 "premiere": PremiereState, "panel": PanelStatus, "install": InstallResult or null, "modules": [ModuleInfo],
 "recent": [Recent], "activity": [Activity], "jobs": [Job], "ffmpeg": Ffmpeg, "settings": Settings,
 "server": {"port": 47800, "agent": {...} or null, "displays": 0}, "update": UpdateStatus,
 "window": {"mode": "compact|wide", "focused": true}}
```

### 3.2 `PremiereState`

```json
{"state": "not_running|starting|waiting|connected", "running": true, "agent_connected": false,
 "premiere_version": "26.0.0" or null, "panel_version": "1.0.0" or null,
 "project": {"path", "name", "root"} or null, "project_path": "..." or null,
 "exe": "C:\\...\\Adobe Premiere Pro.exe" or null, "installs": [{"exe", "version", "source"}],
 "fallback_offer": false, "displays": 1, "fake": false}
```

- `not_running`: no Premiere process.
- `starting`: Premiere was launched by the host (or seen starting) less than 20 s ago and the agent has not connected yet.
- `waiting`: Premiere runs and no display (the visible panel, `PROTOCOL.md` 1) is connected. The headless agent
  may or may not be connected (`agent_connected`). With no agent 20 s after the start, `fallback_offer` is true
  (unless the host already wrote `PlayerDebugMode`) and the home page offers "Enable Premiere's extension debug
  mode" (docs/PANEL.md 4.3).
- `connected`: at least one display is connected (`displays` >= 1).

`agent_connected` is true while the agent is handshaken, in every state. The home page state of `HOST_UI.md` 3
follows `state` one to one: `not_running` -> idle, `starting` and `waiting` -> waiting, `connected` -> main. A
connected agent without a display is `waiting`, never `connected`.

### 3.3 `Settings`

`{"premiere_exe": "auto" or path, "keep_running_with_premiere": true, "theme": "black", "auto_download_updates": true,
"show_activity": false, "ui_scale": 100, "recent_projects": [path], "debug_mode_written": ["CSXS.11"]}`. Patchable:
the first six. `ui_scale` (host scope, default 100) is one of 100, 125, 150, 175, 200: the size of the window and
its text on top of the Windows display scaling (`HOST_UI.md` 3.4, 7); a number between the steps snaps to the
nearest (the larger on a tie), anything else is refused ("The scale needs a percentage.").
`show_activity` (bool, default `false`, host scope) turns the activity strip of the main screen on (`HOST_UI.md` 3.3);
a patch with a value that is not a bool is refused ("The setting needs on or off."). Themes: `black`
(Crystal Black) and `sky` (Crystal Sky), default `black`; a stored value that is not one of them (including the
1.x names `dusk`, `nova`, `frost`, `bloom`) falls back to `black` on load. A patch with another value is refused.

`settings.json` also holds `pins` (host scope, default `[]`): the action ids of the panel's pinned row, in order,
at most five (`PROTOCOL.md` 6.1 `pins`). It is not part of this payload and not patchable: the panel edits it with
`panel.pin` / `panel.unpin`, the hub stores it and sends the tree again, and the window shows nothing of it. On load
entries that are not `<module>.<action>` strings, repeats and everything past the fifth are dropped (a value that is
not a list becomes `[]`); at start the pins of modules this build does not know are dropped too. A stored pin whose
tile is not in the current tree (its module is off, the project is not set up) stays stored and is left out of the
tree.

### 3.4 `ModuleInfo`

`{"id": "files", "package": "files", "title": "Files", "description": "...", "order": 10, "state":
"running|stopped|disabled|failed|missing|import_failed", "enabled": true, "error": "sentence" or null, "status":
{"text", "tone"} or null, "requires": ["ffmpeg"], "has_page": true, "has_settings": true}`. `missing`: the package
is not in this build; `import_failed`: it is but could not be loaded (the error says why).

### 3.5 `Recent`, `Activity`, `Job`

- `Recent`: `{"path", "name", "folder", "opened_at": ISO text or null, "source": "deck|premiere"}`, Deck's own
  list first, files that no longer exist left out.
- `Activity`: `{"at": epoch s, "module": id or null, "text": "Sort project finished", "tone"}`, newest first,
  at most 20. Sources: finished / failed jobs, module events whose data carries `text`, and every action result
  (`ctx.ui.result`, `MODULES.md` 3; `tone` = the result's `outcome`).
- `Job`: `{"id", "title", "module", "priority", "state": "queued|running|done|failed|cancelled", "progress": 0..1 or
  null, "label", "error", "created_at", "started_at", "finished_at"}`.

### 3.6 `Field` (module settings)

`{"key", "label", "kind": "bool|int|text|select|path", "default", "options": [{"value", "label"}] or null, "hint",
"scope": "host|project", "value", "available": bool}`. `available` is false for project fields while no project is
open (the page shows them disabled with "Open a project in Premiere Pro").

### 3.7 `PanelStatus`

`{"target", "extensions_dir", "installed": bool, "installed_version", "bundled_version", "host_version",
"junction": bool, "up_to_date": bool, "manifest_changed": bool, "debug_mode": {"written_by_host": [],
"enabled": ["CSXS.11", "CSXS.12"]}, "last_result": InstallResult or null}`; `InstallResult` = `{"action":
"none|installed|updated|skipped_junction|failed|no_source", "version", "previous_version", "manifest_changed",
"message"}`.

### 3.8 `Ffmpeg`

`{"state": "ready|missing", "source": "bin|path" or null, "path", "version"}`.

## 4. Events

| Event | Payload | When |
|---|---|---|
| `premiere` | `PremiereState` | process start / exit, agent or display connect / disconnect, project switch, launch |
| `modules` | `ModuleInfo[]` | after every module re-render (actions, events, jobs, `invalidate()`); the page also re-fetches the visible module page (`module_page`) on it, throttled to one call per 150 ms |
| `job` | `Job` | every state and progress change |
| `activity` | `Activity` | new line |
| `toast` | `{"kind": "info|success|warning|error", "message"}` | module toasts, host notices; every toast closes on a click (`HOST_UI.md` 7) |
| `result` | `{"module", "id", "outcome": "ok|warn|error|neutral", "text", "detail", "at"}` | an action result (`ctx.ui.result`); the same fields as the panel's `action_result` (`PROTOCOL.md` 6.6) plus the module id. The page morphs the control with that `id` when it is visible (as the panel does), adds the line to the activity strip (kept even while the strip is off, 3.3) and shows the module's `last_result` line for 3 s (`ok`, `neutral`) or 10 s (`warn`, `error`) unless a click closes it first (`HOST_UI.md` 7) |
| `confirm` | `{"id", "question", "module"}` | a module asks yes / no from a job thread; the page opens a modal sheet and answers with `answer_confirm(id, yes)` (no answer in 10 min = no) |
| `settings` | `Settings` | confirmed host settings after a change |
| `panel` | `PanelStatus` | after install / reinstall / remove |
| `navigate` | `{"page": "deck|settings|<module id>"}` | the panel's "Open host" link, the tray and `ctx.host.show(page)` (`MODULES.md` 3). `deck` = the home page; `settings` opens the settings sheet; a module id selects that module's tab of the main state (outside `main` the tab is remembered and shown once the window widens) |
| `update` | `UpdateStatus` | kit contract |
| `window` | `{"mode": "compact|wide", "focused": bool, "ms": 0 or 300}` | window focus change (pauses the scene; `ms` 0, same `mode`) and the start of a size change (`ms` = its duration; the page animates its layout in the same time, `HOST_UI.md` 2). The page never asks for a size: the shell picks `wide` for `PremiereState.state` `connected` and `compact` otherwise. The widen starts 0.45 s after the state entered `connected` (the compact page shows "Panel connected" meanwhile; no widen when the state left `connected` again within that time); the narrow starts at once. The event is sent before the native steps start and is delivered without the pump's batching wait. `ms` is 0 (an instant change) while the window is hidden or minimized, on a show from the tray, and when Windows' animation effects are off (the page's `prefers-reduced-motion`) |

## 5. Module page model (`render_host()`)

`{"title": optional, "status": {"text", "tone"} optional, "subtitle": optional, "header_actions": [button] optional,
"blocks": [Block]}`. `subtitle` is one mono line under the title (the Files page shows the project root there);
`header_actions` are buttons drawn at the right of the title, normally with `variant` `icon` (the Files page's
"Open project folder"). A block is a dict with `type` (`kind` is accepted as an alias), an optional stable `id`,
optional `title` and `hint`. Interactive elements inside blocks are the panel's node shapes (docs/PROTOCOL.md 6.1:
`button`, `toggle`, `select`, `input`, `text`, `progress`, including the button fields `icon`, `short`, `meta`,
`menu`, `variant` and the progress field `cancel`), built with the module's helpers (`self.button(...)` etc.), so
ids carry the module prefix. Using a control calls `module_action(<module id>, <node id>, value, row)`; a menu pick
sends the menu's `id` with the picked `value`, as in the panel. The button fields `auto` and `prompt` of the panel
(3.0) are not used on host pages: a module draws the same automation as a row `toggle` there (Files, section 7 of
its document).

| `type` | Fields | Rendered as |
|---|---|---|
| `cards` | `items: [{id, title, value, detail (alias subtitle), tone, progress (0..1 or null), actions: [button]}]` | a row of glass-lite tiles (2-4 per row): small title, big value, one line of detail, optional progress bar, buttons |
| `table` | `columns: [{key, label, align: left|right, mono: bool}]`, `rows: [{id, <key>: value or {text, tone}, tone, actions: [button]}]`, `empty`, optional `filters: [select]`, optional `search: input` | a header row, scrolling body (max 360 px), row buttons at the right; a row `tone` colours its first cell's dot; filter / search controls send their own action |
| `list` | `rows: [{id, title, subtitle, meta, tone, thumb, actions: [button], toggle: {id, value, disabled} optional}]`, `empty` | rows like the panel's list, with a thumbnail when given; a row `toggle` is a switch at the right that sends its `id` with the new value and `row` = the row id |
| `form` | `fields: [Field-like or node]`, optional `action`, optional `submit: button` | node fields (`input`, `toggle`, `select`) send their own id; setting-style fields (`{key, label, kind, value, options, hint, scope}`) send `action` with `value = {"key", "value"}`; with `submit` the form sends the submit button's id with `value = {key or id: value, ...}` |
| `grid` | `columns` (default 4), `items: [{id, title, subtitle, thumb, badge, bar (0..1), times: [{seconds, label, bar}], actions: [button]}]`, `busy`, `empty` | thumbnail grid; a time chip sends the item's first action with `value` = the seconds (a number), `row` = item id |
| `actions` | `items: [button]`, optional `tiles` (bool), optional `columns` (1..3, default 2 with `tiles`) | a row of buttons (tone `accent` = the tinted primary, at most one per page); with `tiles` a grid of glass tiles: icon well, `short` label and the one-line `meta` under it, `columns` per row (the Files page draws its four actions as 2 x 2 tiles) |
| `chips` | `items: [{id, text, tone, icon}]` | a row of small status chips (a dot or icon in the tone's colour and the text); not interactive |
| `text` | `text`, `tone`, `small` | a line of text |
| `progress` | `value` (0..1 or null), `label`, optional `detail` (one mono line: the current file), optional `cancel` (button node id) | a progress card: label with the count, bar, `detail`, and a Stop pill that sends `cancel` when given |

A `switch` node (`PROTOCOL.md` 6.1: `id`, `label`, `short`, `icon`, `hint`, `value`, `disabled`, `busy`) is accepted wherever a
`toggle` is: in an `actions` block (a pill with the icon, the label and a capsule; in a `tiles` grid it takes a tile's
place) and in a `form`. A change calls `module_action(<module id>, <node id>, <bool>, null)`; a refused change puts the
capsule back to its old value. The Files page's Auto copy and Auto sort are drawn this way.

The list with the id `last_result` is a notice: the window shows it only after a `result` event of its module and
hides it after 3 s or 10 s or on a click (section 4 `result`, `HOST_UI.md` 7); a page model that carries it with no
recent result draws nothing.

List rows may carry `icon` (a symbol of the window's sprite, `ui/assets/icons.svg`; rows with a `toggle` and no
`icon` use the symbol named like the row id with `-` for `_` when the sprite has it); the Quick actions page sets `icon` from the action's own symbol name, because the 2.1 ids and symbols differ (`add_video_track` / `track-video-add`), and a row whose symbol is missing from the sprite is drawn without an icon, never as an error and `group`; a `list` block may
carry `actions` (buttons drawn at the right of its title) and `columns` (2: the rows are laid out in two columns
grouped by `group`, or by the part of `subtitle` before " · "; the id `visibility` implies it). A `button` with
`variant` `icon` is a round plate with its `icon` and the `label` as its accessible name; `ghost` is a borderless
button. A `chips` item text that starts with a number shows the number in bold.

The window's right pane is 430 px wide, so a few blocks are drawn compact there: a `table` with more than four
columns leaves its `mono` columns out (their values become the row's tooltip); an `actions` block whose id is
`<table id>_actions` is drawn in that table's title row (the Files page's `journal_actions`); the `list` with the id
`last_result` is drawn as a tinted result line (state icon, text, `meta`, the row's buttons); the page `title` and
`status` are not repeated (the tab names the module), `subtitle` and `header_actions` form the first line. A
`result` event morphs the node with that id (`data-node`) for 2 s: its icon turns into a check or a cross with a
mint or rose glow.

Row and item buttons send `row` = the row / item id. A `button` with `confirm` asks in a small in-page sheet first.
Unknown block types are skipped (logged with `log`). A module that is not written yet has no page: the window shows
its title, state and error only. `render_host()` is pure and called on the module thread; keep it fast.

## 6. Shell rules

- `Bridge` exposes exactly the methods of section 2 as public names (`CONTRACT_METHODS`, checked by
  `tests/test_shell_bridge.py`); everything else starts with an underscore (pywebview exposes every public
  attribute of the js_api object).
- The window has two locked sizes, compact 420 x 560 and wide 720 x 560 logical px, times the Scale factor
  (`ui_scale / 100`); it is frameless and never resizable or maximizable by the user (`shell/winutil.FixedSizeHook`), DWM rounded corners and border colour =
  page background (the theme's `--bg-mid`, retinted on a theme change). The shell changes the size itself when
  `PremiereState.state` enters or leaves `connected`: it sends the `window` event with `ms` 300, then moves
  `SetBounds` in steps on the UI thread over 300 ms (one step per 60 Hz frame, timed from the start, on the page's
  curve `cubic-bezier(0.2, 0.8, 0.2, 1)`; the window keeps its centre and stays inside the monitor's work area) and
  updates the size hook's locked size before every step, so the hook never fights the animation
  (`shell/winutil.SizeAnimator`; the log has the physical rect at the start and the end). A new change during an
  animation starts from the current rect. A rect larger than the work area keeps its top-left corner on the work
  area's. The page is served by pywebview's HTTP server on a port from
  42301..42340 so the origin (and its `localStorage`) stays stable.
- Scale: `ui_scale / 100` is the WebView2 `ZoomFactor` of the window's control (`window.native.webview`, set on
  the UI thread; `shell/winutil.set_zoom_factor`) and multiplies the locked sizes (`FixedSizeHook.lock(..., zoom=)`,
  `SizeAnimator.zoom`). Zoom is a device-scale change: the page keeps its logical CSS viewport (420 / 720 x 560),
  `devicePixelRatio` becomes the factor times the DPI scale, text and glass stay crisp. The saved value sets the
  first size of the window, the zoom and the size lock in the native setup, before the page is shown; `set_scale`
  sets the zoom and the new size in one UI-thread call (`SizeAnimator.rescale`: the current mode's size at the new
  factor, the centre kept, inside the work area; a running size animation stops at its target).
- Close: hide to the tray while Premiere runs and `keep_running_with_premiere` is on; otherwise quit. The tray menu:
  Open, Launch Premiere Pro, Quit. The process exits with `os._exit(0)` after a clean shutdown.
- `--fake`: an in-process fake agent (`host/fake_agent.py`) connects over the real WebSocket and answers scripts with
  canned data (`create_bin_path` answers `{"node_id": "bin:<path>"}`), and a fake display (the visible panel) joins
  it; `--fake-state not_running|waiting|connected` (default `connected`) picks the start, `fake_premiere(state)`
  switches later. The CEP target is `<data>/fake-cep/...` unless `PREMIERE_DECK_CEP_DIR` is set, so a fake run
  never writes Adobe's folder.
