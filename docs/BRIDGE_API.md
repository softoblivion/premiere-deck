# Host window bridge (JS <-> Python)

Owner: host worker. The only definition of what the host window's page (`src/premiere_deck/ui/`) and the Python
side (`src/premiere_deck/shell/bridge.py`, backed by `host/hub.py`) say to each other. The mock
(`ui/js/mock.js`, `index.html?mock=1`) implements the same contract with canned data. Same shape as Media Forge's
`docs/BRIDGE_API.md`; the update control and the theme follow the Liquid UI kit contract (kit docs, sections 10-11).

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
| `launch_premiere(project)` | `PremiereState` (3.2) | `project` = `.prproj` path or `null`. Premiere already running + project: opened through the panel; already running without a project: rejection "Premiere Pro is already running." |
| `pick_project()` | path or `null` | file dialog, `*.prproj` |
| `pick_premiere_exe()` | `Settings` or `null` | file dialog for `Adobe Premiere Pro.exe`; stores `premiere_exe` |
| `recent_projects()` | `Recent[]` (3.5) | |
| `modules()` | `ModuleInfo[]` (3.4) | |
| `set_module_enabled(id, enabled)` | `ModuleInfo` | starts / stops the module |
| `module_page(id)` | `{module: ModuleInfo, page: Page or null}` | `page` = the module's `render_host()` (section 5); `null` when the module has no page or is not running |
| `module_action(id, action, value, row)` | `{ok: bool, toast: [tone, text] or null}` | `action` = a node id from the page (`files.collect`, or local `collect`, or window-only `host:<local>`); `value` and `row` optional. The module's own toast also arrives as a `toast` event |
| `module_settings(id)` | `{module, fields: Field[], project: name or null}` | `Field` = 3.6 |
| `set_module_setting(id, key, value, scope)` | the stored value | `scope` = `host` or `project`; converted to the field's kind; rejection with a sentence when it cannot be |
| `panel_status()` | `PanelStatus` (3.7) | |
| `panel_reinstall()` | `PanelStatus` | copies the bundled panel again (docs/PANEL.md 4) |
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
| `log(level, message)` | `null` | `debug` / `info` / `warning` / `error`, logger `premiere_deck.ui` |

## 3. Payloads

### 3.1 `State`

```json
{"version": "0.1.0", "fake": false, "data_dir": "D:\\Apps\\PremiereDeck\\premiere-deck_data",
 "premiere": PremiereState, "panel": PanelStatus, "install": InstallResult or null, "modules": [ModuleInfo],
 "recent": [Recent], "activity": [Activity], "jobs": [Job], "ffmpeg": Ffmpeg, "settings": Settings,
 "server": {"port": 47800, "agent": {...} or null, "displays": 0}, "update": UpdateStatus}
```

### 3.2 `PremiereState`

```json
{"state": "not_running|starting|connected|no_panel", "running": true, "premiere_version": "26.0.0" or null,
 "panel_version": "1.0.0" or null, "project": {"path", "name", "root"} or null, "project_path": "..." or null,
 "exe": "C:\\...\\Adobe Premiere Pro.exe" or null, "installs": [{"exe", "version", "source"}],
 "fallback_offer": false, "displays": 1, "fake": false}
```

`starting`: launched by the host (or seen starting) less than 20 s ago and the agent has not connected yet.
`no_panel`: Premiere runs, no agent after 20 s; with `fallback_offer` true the Deck page offers "Enable Premiere's
extension debug mode" (docs/PANEL.md 4.3).

### 3.3 `Settings`

`{"premiere_exe": "auto" or path, "keep_running_with_premiere": true, "theme": "nova", "auto_download_updates": true,
"recent_projects": [path], "debug_mode_written": ["CSXS.11"]}`. Patchable: the first four. Themes: `dusk`, `nova`,
`frost`, `bloom` (default `nova`).

### 3.4 `ModuleInfo`

`{"id": "files", "package": "files", "title": "Files", "description": "...", "order": 10, "state":
"running|stopped|disabled|failed|missing|import_failed", "enabled": true, "error": "sentence" or null, "status":
{"text", "tone"} or null, "requires": ["ffmpeg"], "has_page": true, "has_settings": true}`. `missing`: the package
is not in this build; `import_failed`: it is but could not be loaded (the error says why).

### 3.5 `Recent`, `Activity`, `Job`

- `Recent`: `{"path", "name", "folder", "opened_at": ISO text or null, "source": "deck|premiere"}`, Deck's own
  list first, files that no longer exist left out.
- `Activity`: `{"at": epoch s, "module": id or null, "text": "Collect project finished", "tone"}`, newest first,
  at most 20. Sources: finished / failed jobs and module events whose data carries `text`.
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
| `premiere` | `PremiereState` | process start / exit, agent connect / disconnect, project switch, launch |
| `modules` | `ModuleInfo[]` | after every module re-render (actions, events, jobs, `invalidate()`); the page also re-fetches the visible module page (`module_page`) on it, throttled to one call per 150 ms |
| `job` | `Job` | every state and progress change |
| `activity` | `Activity` | new line |
| `toast` | `{"kind": "info|success|warning|error", "message"}` | module toasts, host notices |
| `confirm` | `{"id", "question", "module"}` | a module asks yes / no from a job thread; the page opens a modal sheet and answers with `answer_confirm(id, yes)` (no answer in 10 min = no) |
| `settings` | `Settings` | confirmed host settings after a change |
| `panel` | `PanelStatus` | after install / reinstall / remove |
| `navigate` | `{"page": "deck|settings|<module id>"}` | the panel's "Open host" link and the tray |
| `update` | `UpdateStatus` | kit contract |
| `window` | `{"focused": bool}` | window focus (pauses the scene) |

## 5. Module page model (`render_host()`)

`{"title": optional, "status": {"text", "tone"} optional, "blocks": [Block]}`. A block is a dict with `type` (`kind`
is accepted as an alias), an optional stable `id`, optional `title` and `hint`. Interactive elements inside blocks
are the panel's node shapes (docs/PROTOCOL.md 6.1: `button`, `toggle`, `select`, `input`, `text`, `progress`),
built with the module's helpers (`self.button(...)` etc.), so ids carry the module prefix. Using a control calls
`module_action(<module id>, <node id>, value, row)`.

| `type` | Fields | Rendered as |
|---|---|---|
| `cards` | `items: [{id, title, value, detail (alias subtitle), tone, progress (0..1 or null), actions: [button]}]` | a row of glass-lite tiles (2-4 per row): small title, big value, one line of detail, optional progress bar, buttons |
| `table` | `columns: [{key, label, align: left|right, mono: bool}]`, `rows: [{id, <key>: value or {text, tone}, tone, actions: [button]}]`, `empty`, optional `filters: [select]`, optional `search: input` | a header row, scrolling body (max 360 px), row buttons at the right; a row `tone` colours its first cell's dot; filter / search controls send their own action |
| `list` | `rows: [{id, title, subtitle, meta, tone, thumb, actions: [button], toggle: {id, value, disabled} optional}]`, `empty` | rows like the panel's list, with a thumbnail when given; a row `toggle` is a switch at the right that sends its `id` with the new value and `row` = the row id |
| `form` | `fields: [Field-like or node]`, optional `action`, optional `submit: button` | node fields (`input`, `toggle`, `select`) send their own id; setting-style fields (`{key, label, kind, value, options, hint, scope}`) send `action` with `value = {"key", "value"}`; with `submit` the form sends the submit button's id with `value = {key or id: value, ...}` |
| `grid` | `columns` (default 4), `items: [{id, title, subtitle, thumb, badge, bar (0..1), times: [{seconds, label, bar}], actions: [button]}]`, `busy`, `empty` | thumbnail grid; a time chip sends the item's first action with `value` = the seconds (a number), `row` = item id |
| `actions` | `items: [button]` | a row of buttons (tone `accent` = the tinted primary, at most one per page) |
| `text` | `text`, `tone`, `small` | a line of text |
| `progress` | `value` (0..1 or null), `label` | a progress bar |

Row and item buttons send `row` = the row / item id. A `button` with `confirm` asks in a small in-page sheet first.
Unknown block types are skipped (logged with `log`). A module that is not written yet has no page: the window shows
its title, state and error only. `render_host()` is pure and called on the module thread; keep it fast.

## 6. Shell rules

- `Bridge` exposes exactly the methods of section 2 as public names (`CONTRACT_METHODS`, checked by
  `tests/test_shell_bridge.py`); everything else starts with an underscore (pywebview exposes every public
  attribute of the js_api object).
- The window is fixed at 1040 x 680 logical px, frameless, not resizable or maximizable (`shell/winutil.FixedSizeHook`),
  DWM rounded corners and border colour = page background. The page is served by pywebview's HTTP server on a port
  from 42301..42340 so the origin (and its `localStorage`) stays stable.
- Close: hide to the tray while Premiere runs and `keep_running_with_premiere` is on; otherwise quit. The tray menu:
  Open, Launch Premiere Pro, Quit. The process exits with `os._exit(0)` after a clean shutdown.
- `--fake`: an in-process fake agent (`host/fake_agent.py`) connects over the real WebSocket and answers scripts with
  canned data; the CEP target is `<data>/fake-cep/...` unless `PREMIERE_DECK_CEP_DIR` is set, so a fake run never
  writes Adobe's folder.
