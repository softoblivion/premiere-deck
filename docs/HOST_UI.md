# Host window

Owner: host worker (code), integrator (this contract). The window of `PremiereDeck.exe`: Liquid UI kit (vendored
`ui/vendor/liquid_ui`), fixed size 1040 x 680 (landscape; the pages hold lists and a thumbnail grid), frameless with
the kit's title bar, tray icon while Premiere runs. Copy neutral and factual (`_workspace\CONVENTIONS.md`).

## 1. Shell

- Single instance (named mutex); a second start brings the window to the front and, with `--project <path>`,
  launches Premiere with that project.
- Arguments: `--project <path>` (launch Premiere with it), `--from-premiere` (started by the agent: start
  minimised to the tray), `--apply-update` (the updater's mode), `--fake` (mock Premiere for UI work).
- Closing the window hides to the tray while Premiere runs (`keep_running_with_premiere`, default on) and exits
  otherwise; the tray menu: Open, Launch Premiere Pro, Quit.
- Theme: the kit's four themes, setting `theme`, default `Nova`.

## 2. Navigation

Left rail with five entries: Deck, Files, Actions, Search, Settings. The content area shows one page. The version
pill with the update popover and sheet (kit `update.js`) sits in the title bar.

## 3. Pages

3.1 **Deck** (home): Premiere status card (Not running / Starting / Connected · <project name> · Premiere 26.3 /
    Panel not loaded, with the panel fallback button of `PANEL.md` 4.3), the big "Launch Premiere Pro" pill (with
    a "Recent projects" list beside it: name, folder, last opened, from Premiere's recent list and Deck's own),
    module cards (title, one-line status, enabled switch), and the activity feed (the last 20 journal and job
    lines across modules).
3.2 **Files**: `FILES.md` 7: journal (filter by op, search by name, per-entry Undo), Collect plan (per-folder
    totals, Run), Pack options, layout editor (folders list, rules table with add/remove/reorder, overrides),
    project settings. "No project open" state shows the layout editor only.
3.3 **Actions**: the action list with visibility switches and the export presets folder (`QUICK_ACTIONS.md` 2, 3).
3.4 **Search**: `AI_SEARCH.md` 4: query field, thumbnail grid (4 columns), hit actions, index status and the
    model download card.
3.5 **Settings**: Premiere executable (auto-detected; override), panel status (installed version, Reinstall,
    Remove the panel, debug-mode fallback state), ffmpeg status, data folder (Open), "Keep running with Premiere",
    theme, Updates rows (kit), About (version, licence, GitHub link).

## 4. Module pages

A module provides `render_host() -> dict` (`MODULES.md` 2): a page model the host window renders with a fixed set
of blocks (`cards`, `table`, `list`, `form`, `grid`, `actions`), defined in `ui/js/blocks.js` and documented in
`BRIDGE_API.md` of this project once the host worker writes it (the host window's JS <-> Python contract, same
shape as Media Forge's). Module actions from the window reach `on_action` with the same ids as the panel plus
`host:` prefixed ones for window-only controls.

## 5. Bridge

`shell/bridge.py` exposes the window's JS API (`BRIDGE_API.md`): host state, modules list, settings, module page
models, module actions, launch Premiere, update methods (the kit's contract in `LIQUID_UI.md` 9), reveal / open.
Events Python -> JS: `state`, `module_page`, `activity`, `job`, `update`, `toast`.

## 6. Settings file (`settings.json`)

`premiere_exe` (auto / path), `keep_running_with_premiere` (true), `theme` (`Nova`), `auto_download_updates`
(true), `debug_mode_written` (list of CSXS keys the host wrote), `recent_projects` (list), `modules.<id>.*`
(module host settings), `modules_enabled.<id>` (bool).

Stored values: `theme` is the lower-case kit name (`dusk`, `nova`, `frost`, `bloom`; default `nova`); `premiere_exe`
is `"auto"` or a path ending in `.exe`; a bad or hand-edited value falls back to its default on load
(`core/settings.py`); keys of newer versions are kept.

## 7. Implementation notes (host worker)

- Rail: Deck, then one entry per module in `order` (taken from `modules()`, so a new module gets its page without a
  window change; a module missing from the build shows greyed with "Not installed in this build"), then Settings.
- Premiere states on the Deck card come from `PremiereState.state` (`BRIDGE_API.md` 3.2): `not_running`, `starting`
  (host launch or process seen less than 20 s ago), `connected`, `no_panel` (Premiere runs, no agent after 20 s; the
  card then offers the debug-mode fallback once, behind a confirmation).
- Recent projects: Deck's own list (`recent_projects`, every opened project) first, then Premiere's MRU from
  `Documents\Adobe\Premiere Pro\<version>\Profile-<user>\Adobe Premiere Pro Prefs` (`BE.Prefs.MRU.Document.<n>`);
  files that no longer exist are left out.
- Single instance: named mutex `Local\PremiereDeck.SingleInstance` (`.fake` appended in `--fake`). A second start
  sends its arguments to the first instance's window with `WM_COPYDATA` (`{"show", "project"}`); the first one shows
  its window and launches Premiere with the project.
- Close and tray: the window's close button hides it while Premiere runs and `keep_running_with_premiere` is on
  (the tray shows one balloon line); otherwise the host quits. While the window is hidden, Premiere's exit quits the
  host. `--from-premiere` starts hidden. Tray menu: Open, Launch Premiere Pro, Quit; double click opens.
- Confirmations from modules (`ctx.ui.confirm`) open a modal sheet in the window and show the window first; asked
  on the module thread they are answered "no" (a module must ask from a job).
