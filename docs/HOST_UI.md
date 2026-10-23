# Host window

The window of `PremiereDeck.exe`: the project-owned copy of
the Liquid UI kit (`ui/vendor/liquid_ui`, edited in place, never synced), frameless with the kit's title bar,
fixed size with two locked sizes (compact 420 x 560 logical px, 3:4; wide 720 x 560) times the Scale setting,
tray icon while Premiere runs. Copy is neutral and factual, friendlier through hierarchy, spacing and feedback,
never through exclamation marks.

## 1. Shell

- Single instance (named mutex); a second start brings the window to the front and, with `--project <path>`,
  launches Premiere with that project.
- Arguments: `--project <path>` (launch Premiere with it), `--from-premiere` (started by the agent: start
  minimised to the tray), `--apply-update` (the updater's mode), `--fake` (mock Premiere for UI work) with
  `--fake-state not_running|waiting|connected` (the simulated state at start, default `connected`).
- Closing the window hides to the tray while Premiere runs (`keep_running_with_premiere`, default on) and exits
  otherwise; the tray menu: Open, Launch Premiere Pro, Quit.
- Size: compact in the idle and waiting states, wide in main (2). The user never resizes the window; the shell
  changes the size itself (`BRIDGE_API.md` 4 `window`, 6). Scale (setting `ui_scale`, 100 / 125 / 150 / 175 /
  200 %, default 100) multiplies both locked sizes and the page's density on top of the Windows display scaling;
  the logical layout stays the same (3.4, 7).
- Theme: two project themes, setting `theme`: `black` (Crystal Black, default) and `sky` (Crystal Sky) (7).
- Never opens the Premiere panel: there is no "Open panel" control anywhere and the host sends no `open_display`
  (`PROTOCOL.md` 3).

## 2. States and layout

The window shows one home page whose state follows `PremiereState.state` (`BRIDGE_API.md` 3.2) one to one:

| Window state | `PremiereState.state` | Size | Title bar |
|---|---|---|---|
| idle | `not_running` | compact | the window cluster only (settings, minimize, close); the app name and the version pill sit in the hero block |
| waiting | `starting`, `waiting` | compact | as idle |
| main | `connected` | wide | the left cluster (20 px glyph, "Premiere Deck", the version pill with the kit's update popover and sheet), the module tabs in the centre slot, the window cluster at the right |

The title bar is the kit copy's component (`vendor/liquid_ui/js/titlebar.js`, `css/titlebar.css`), the same layout
as every other app of the family: 44 px high, a grid of three columns (`1fr auto 1fr`, 10 px side padding, 10 px
gap) so the centre slot is centred on the window. Left (gap 8): the glyph with the `--glyph-glow` shadow, the name
in the label role, the version pill (24 px capsule lens, mono-s). Centre: the module tabs (3.3). Right: one 34 px
capsule lens holding settings | separator | minimize, close as 30 x 28 fills (icons 15, the gear 17 and turning 60
degrees on hover and while the sheet is open; close turns red on hover); no maximize. The drag layer lies under
everything; the left cluster lets the pointer through to it except the pill. In compact the left cluster and the
centre slot are hidden (`visibility`, so the columns keep their places). Every window button presses with the
kit's gel (`gel(btn, { amount: 0.7 })`). The buttons keep the window sprite's icons (`settings`, `minimize`,
`close`).

There is no left rail and no page list: the module pages are the tabs of the main state (3.3), Settings is the
sheet of the gear (3.4). The `navigate` event (`BRIDGE_API.md` 4): `deck` shows the home page, `settings` opens the
sheet over it, a module id selects that module's tab (outside main the choice is remembered and applied when the
window widens).

Widen and narrow. On entering main the compact page first shows "Panel connected" with a green ring around the
glyph; 0.45 s later the shell sends `window` `{"mode": "wide", "ms": 300}` and grows the window from 420 to 720 px
around its centre in 300 ms (`BRIDGE_API.md` 6). The page switches to the main layout at the first step: the left
column has its final 248 px at once; the right pane is clipped by the growing window, and its content, laid out
at a fixed 430 px width so it never reflows, fades in and slides 22 px in from the right, on
`cubic-bezier(0.2, 0.8, 0.2, 1)` (the native steps use the same curve). Leaving main (the panel closed, Premiere
exited) plays the same animation backwards; the compact page (waiting or idle) replaces the main layout when it
ends. With reduced motion (Windows' animation effects off) the size and the layout change at once.

## 3. Screens

3.1 **Idle** (compact), top to bottom: the 84 px glyph, "Premiere Deck" with the version pill, a status line with a
    neutral dot "Premiere Pro is not running", the 54 px hero pill "Launch Premiere Pro" (rocket icon, accent glass;
    `launch_premiere(null)`), and at the bottom the "Recent projects" card: the three most recent projects
    (`recent_projects()`), one row each with the name, the folder in mono and the time ago (empty when the entry
    has no `opened_at`); a click on a row launches Premiere with that project; a small icon button in the card's
    header ("Choose project", icon `folder`, hidden while the card is collapsed) picks another `.prproj`
    (`pick_project()`) and launches it; a chevron collapses the card, and the collapsed state is remembered
    (`localStorage` `pd.recent.collapsed`). Without a Premiere executable the status line reads "Premiere Pro was not
    found" (amber) and the hero pill "Choose Premiere Pro" opens Settings. The glyph is drawn in code (symbol `glyph-pr` of the window's
    icon sprite: a violet rounded square with a "Pr" mark of round strokes and the kit's glass highlights; no
    Adobe asset file).
3.2 **Waiting** (compact): the glyph with two breathing rings (2.4 s loop), the title "Waiting for the panel", an
    amber status line "Premiere Pro <version> is running" ("Premiere Pro is starting" in `starting`), a help card
    with the line "Open the panel once; Premiere keeps it in the workspace." over the menu path drawn as key caps
    (Window > Extensions > Premiere Deck), then three secondary actions as icon tiles: "Check panel update"
    (`panel_check_update()`; result "Panel is up to date · <version>" or "Panel updated · <old> to <new>"),
    "Check for updates" (the kit's update check, `check_updates()`; opens the kit's update sheet when a release is
    found, else "Premiere Deck is up to date"), "Reinstall panel" (`panel_reinstall()`). With `fallback_offer`
    the help card adds "Enable Premiere's extension debug mode" behind a confirmation (`panel_debug_mode(true)`,
    `PANEL.md` 4.3). Recent projects stay, collapsed, with their count (pinned to the bottom; expanded, the card
    opens upwards over the tiles; the waiting state always starts collapsed). The outcome of the three actions shows
    on the pressed tile as in the panel (check or cross for 2 s), as a result line under the tiles until the next
    action or screen change, and as a line in the activity feed.
3.3 **Main** (wide). Left column, 248 px: the Premiere card (36 px glyph, "Connected", the Premiere version, the
    project name and its root path in mono) and, at the bottom, the activity strip (state icon, text, time per
    line; newest at the bottom; it grows upwards into the free space and shows as many of the last 20 lines of
    `State.activity` as fit; the icon is a check, warning or cross for the line's tone, else the module's icon; the
    time is `HH:MM`). The strip exists only while the setting `show_activity` is on (default off, 6, row "Show
    activity" in 3.4); with it off the left column holds the Premiere card only, and a change of the setting shows
    or hides the strip at once (the activity events are still received and kept). Right pane: a glass panel showing one module, chosen by the tabs in the title bar's centre slot,
    one per module in `order` (`modules()`; the kit's segmented control with the module icon and title, "Actions"
    for Quick actions; the order is the modules' `order`, so with Quick actions at 5 and Files at 10 the tabs read
    Actions | Files and Actions is the tab shown until another one is chosen; the chosen tab is remembered in
    `localStorage` `pd.tab`); each tab is one screen, its remaining blocks below the fold scroll inside the pane. A
    module's head hint (the Actions page's "Shown in the panel") is the pane's first line, right-aligned; without a
    hint the line is not drawn. The page draws a module page with the narrow-pane rules of `BRIDGE_API.md` 5.
    - **Files**: `FILES.md` 7: the root path with the "Open project folder" icon button, the Set up project button with the two
      `switch` nodes Auto copy and Auto sort beside it (drawn by `blocks.js` as a pill with the icon, the label and a
      capsule; a change sends the node id with the new value), the three status chips
      (in the root, outside, offline), the four file actions as 2 x 2 tiles with a one-line live meta, the last
      result line with Undo (7, shown for a few seconds after a result), the last three journal entries with Undo and "Show all"; below the fold the layout editor
      (folders list, rules table, overrides) and the module's Settings card. "No project open" shows the layout
      editor only.
    - **Actions**: the configuration of the set (`QUICK_ACTIONS.md` 3): one switch per action ("Shown in the
      panel") in two columns grouped by the action's group (Timeline, Tracks, Selection, Clips, Project, Sequence, Export,
      Keys). The rows come from the module's page model (`render_host`, one row per action with the action's
      `icon` symbol, `title` and `subtitle` "<group> · <hint>"); the page has no presets block. Actions run from
      the panel only.
    - A module that is not running or missing from the build shows its title, state and error in its tab ("Not
      installed in this build").
3.4 **Settings** (a sheet, `ui/js/settings.js`, not a page): the kit's modal sheet (`Overlay`, preset `sheet`) that
    grows out of the title-bar gear, with kit rows and its own scroll. Groups in this order: Appearance (the theme
    as two large cards, Crystal Black and Crystal Sky, each with a mini preview of its scene, a glass card and an
    accent pill; the selected card carries the accent rim; below them the Scale row of the kit copy,
    `js/scale.js` `mountScaleControl`: "Scale", "Size of the window and its text", a segmented control 100 % ..
    200 %, stacked in the narrow sheet so the five steps take the row's width; a choice calls `set_scale` and the
    window changes at once), Behaviour ("Keep running with Premiere"; "Show activity": the activity strip of the main screen, 3.3), Updates (the
    host version with the kit's update rows and "Check now"; the panel's installed and bundled versions with
    "Check now" (`panel_check_update()`) and "Reinstall"), Premiere (executable with Choose file / Use detected,
    version), then below the fold Panel (target folder, debug-mode fallback state, Remove the panel), Modules (one
    enabled switch per module, `set_module_enabled`), Tools (ffmpeg status and Download), Data (data folder, Open),
    About (version, licence, GitHub link). Changes are saved at once; a refusal restores the control and shows a
    toast. The values are refreshed each time the sheet opens.

## 4. Module pages

A module provides `render_host() -> dict` (`MODULES.md` 2): a page model the host window renders with a fixed set
of blocks (`cards`, `table`, `list`, `form`, `grid`, `actions`, `chips`, `text`, `progress`), drawn by
`ui/js/blocks.js` and defined in `BRIDGE_API.md` 5 (the host window's JS <-> Python contract, same shape as Media
Forge's). Module actions from the window reach `on_action` with the same ids as the panel plus
`host:` prefixed ones for window-only controls.

## 5. Bridge

`shell/bridge.py` exposes the window's JS API (`BRIDGE_API.md`): host state, modules list, settings, module page
models, module actions, launch Premiere, panel install and check, update methods (the kit's contract in
`LIQUID_UI.md` 9), reveal / open. Events Python -> JS: `BRIDGE_API.md` 4 (`premiere`, `modules`, `job`,
`activity`, `toast`, `result`, `confirm`, `settings`, `panel`, `navigate`, `update`, `window`).

## 6. Settings file (`settings.json`)

`premiere_exe` (auto / path), `keep_running_with_premiere` (true), `theme` (`black`), `auto_download_updates`
(true), `show_activity` (false), `ui_scale` (100), `debug_mode_written` (list of CSXS keys the host wrote), `recent_projects` (list), `recent_opened`
(`{path: ISO 8601 UTC}` of the last open through Deck, pruned with the list; the "time ago" of a Deck row), `modules.<id>.*`
(module host settings), `modules_enabled.<id>` (bool).

Stored values: `theme` is `black` or `sky` (default `black`; the 1.x kit names `dusk`, `nova`, `frost`, `bloom` and
any other value fall back to `black` on load); `premiere_exe` is `"auto"` or a path ending in `.exe`; `ui_scale` is one of 100, 125, 150, 175, 200 (a number
between the steps snaps to the nearest, the larger on a tie; anything that is not a positive number is refused with
"The scale needs a percentage." and falls back to 100 on load); `show_activity` is a bool (a patch with another type is
refused with "The setting needs on or off."); a bad or
hand-edited value falls back to its default on load (`core/settings.py`); keys of newer versions are kept.

## 7. Implementation notes

- Tabs of main: one per module in `order` (taken from `modules()`, so a new module gets its tab without a window
  change); Settings is the title-bar sheet (3.4), not a tab.
- Window states come from `PremiereState.state` (`BRIDGE_API.md` 3.2): `not_running` -> idle; `starting` (host
  launch or process seen less than 20 s ago, no agent yet) and `waiting` (Premiere runs, no display connected;
  `agent_connected` tells whether the headless agent is there) -> waiting; `connected` (a display is connected)
  -> main. The debug-mode fallback is offered once in waiting, behind a confirmation, while `fallback_offer` is
  true.
- Recent projects: Deck's own list (`recent_projects`, every opened project) first, then Premiere's MRU from
  `Documents\Adobe\Premiere Pro\<version>\Profile-<user>\Adobe Premiere Pro Prefs` (`BE.Prefs.MRU.Document.<n>`);
  files that no longer exist are left out; the card shows the first three.
- Single instance: named mutex `Local\PremiereDeck.SingleInstance` (`.fake` appended in `--fake`). A second start
  sends its arguments to the first instance's window with `WM_COPYDATA` (`{"show", "project"}`); the first one shows
  its window and launches Premiere with the project.
- Close and tray: the window's close button hides it while Premiere runs and `keep_running_with_premiere` is on
  (the tray shows one balloon line, once per host run); otherwise the host quits. While the window is hidden, Premiere's exit quits the
  host. `--from-premiere` starts hidden. Tray menu: Open, Launch Premiere Pro, Quit; double click opens. A show
  from the tray takes the size of the current state without an animation.
- Window size: `shell/winutil.SizeAnimator` moves the native size on the WinForms UI thread (`BRIDGE_API.md` 6);
  the bridge decides the mode from every `premiere` event; the window and DWM border take the theme's `--bg-mid`.
- `--fake` and the mock (`index.html?mock=1`, flags in `ui/js/mock.js`): Alt+1 / Alt+2 / Alt+3 switch the simulated
  Premiere to not running / waiting / connected (`fake_premiere`), so every screen and both animations can be
  checked without Premiere.
- Confirmations from modules (`ctx.ui.confirm`) open a modal sheet in the window and show the window first; asked
  on the module thread they are answered "no" (a module must ask from a job).
- Themes: Crystal Black (`black`) is a neutral near-black scene with crystal glass and cool white highlights;
  Crystal Sky (`sky`) is a light theme: a pale sky-blue scene with soft blue fields, white frosted glass and dark
  ink. Both use the violet accent of the glyph. Colour marks state only: mint ok, amber warn, rose error, the
  accent for the primary action, the selected theme card and the running job; everything else is ink on glass.
  Body text keeps at least 4.5:1 contrast over the glass in both themes. The values live in
  `ui/themes.json` (name, label, hue, accent and every token of each theme); `build/gen_themes.py` renders them
  into the kit copy's `css/themes.css` and the panel's token block (`PANEL.md` 3); the light theme also overrides
  the ink, shadow, tint and well tokens the kit derives for dark scenes, and the kit's dark-scene literals are
  tokens. Besides the kit's tokens: `--ok-ink`, `--warn-ink`, `--error-ink` (text in a state colour), `--hero-ink`
  (label on the accent glass), `--rim-tone`, `--line-soft` (hairlines between rows).
- Theme switch: `document.startViewTransition` with a sweep from top to bottom: the new state is revealed with
  `clip-path: inset(0 0 100% 0)` -> `inset(0)` over 600 ms on `cubic-bezier(0.65, 0, 0.35, 1)`, while a thin
  bright line (white with a blue glow; an element with `view-transition-name: theme-line`, added inside the update
  callback and removed when the transition ends) moves with the edge (`translateY(0)` -> `100vh`, same curve). The
  scene retints underneath. Reduced motion: an instant switch. Without the API the switch is instant.
- Font: Sora (SIL OFL, latin subset, variable woff2 with its licence text in the kit copy's `fonts/`) for all text;
  Geist Mono for numbers, paths and times. The kit copy's `css/tokens.css` is the only place that names the text
  font. Weights: body 450, medium 550, strong 600, title 650.
- Type scale: every text of the window is one of the named roles of the kit copy's `css/tokens.css`
  (`font: var(--t-<role>)` with its tracking `--tr-<role>`); no other rule carries a font size, and
  `tests/test_ui_type_scale.py` fails on one or on an unknown role. Roles (weight size / line-height, tracking):
  display 650 20 / 1.2, -0.02em (sheet titles); title 650 16 / 1.25, -0.015em (popover titles, empty states, the
  hero pill, the project name, card values); section 600 11 / 1.3, +0.06em, uppercase (group headers, captions,
  table heads, tags, badges); body 450 13 / 1.45 (text, toasts, hints, status lines); label 550 12.5 / 1.3,
  -0.004em (buttons, tabs, setting labels, row titles, the app name); caption 450 11.5 / 1.35 (meta lines, chips,
  times, counters); mono 450 12 / 1.4 (paths, versions); label-s 550 11.5 / 1.3 (xs buttons, key caps); mono-s
  450 11 / 1.3 (the version pill, table codes, inline code).
- Density: compact controls on the kit copy's height tokens `--h-l` 40, `--h-m` 32, `--h-s` 28, `--h-xs` 24
  (buttons 40 / 32 / 28 / 24, icon buttons 32 / 28 / 24, segmented options 28, switch 40 x 24, field 32, select 28,
  setting rows 46 with radius-18 cards, the sheet 560 wide at most, popover 352, hero pill 48, title bar 44); radii
  28 / 22 / 18 / 14 / 10 / 8, spacing on the 4 px grid.
- Scale: the shell sets the WebView2 zoom factor to `ui_scale / 100` and locks the window at the logical size x
  the factor x the Windows DPI scale, so text and glass render at the new density while the page keeps its 420 /
  720 x 560 CSS viewport (`BRIDGE_API.md` 6). The saved step is applied with the size lock before the page shows
  anything; `set_scale` applies a change at once and keeps the window's centre, inside the work area. A window
  larger than the work area (200 % on a 1080p screen) keeps its top-left corner on the work area's.
- Notices: every notice of the window closes on a
  click anywhere on it, except on a control inside it (a button, link or field does its own thing and the notice
  stays). Helpers: `ui/js/ui.js` `closeOnClick(el, close)` and `noticeMs(tone)`.
  - Toasts (kit copy `ui/vendor/liquid_ui/js/toast.js`): a click on the toast closes it, sticky ones included; the
    option `onClick` runs first when the toast also does something (the sticky "<product> <version> is ready" toast
    of the update control opens the update sheet; its Restart / View button runs its own action and closes the
    toast). Toasts sit above the modal sheets, so they can be clicked while a sheet is open.
  - Result lines: the Files page's `last_result` list (the line with Undo) is shown only after a `result` event of
    its module, for 3 s (outcome `ok`, `neutral`) or 10 s (`warn`, `error`; the panel's "error" timer also covers
    warnings here because they carry text to read), and closes on a click outside its Undo button. A line that is
    already in the page model when the tab opens is not shown, and a repeated result with the same text shows the
    line again (the timer restarts with every `result` event). The same 3 s / 10 s rule and click apply to the
    result line under the waiting tiles (3.2); it also clears on a screen change.
  - Other inline notices close on a click and come back when their cause changes: the waiting hint card (menu path)
    and the debug-mode offer inside it (the offer closes on its own, the card closes with everything in it; both
    return when the screen changes), and the module notice of a tab ("Not installed in this build", "turned off",
    "no page yet"; it stays closed until its text changes). Modal confirmations, the update popover and sheet,
    empty-state lines and the page's own `text` blocks are not notices.
- Icons: every function has its own inline SVG icon (24 px grid, 1.75 px stroke, round caps) plus a short label,
  from one sprite of symbols shared in names with the panel's `icons.svg` (`PANEL.md` 1): `ui/assets/icons.svg`,
  inlined into the page at start (`ui/js/icons.js`); `ui/assets/glyph.svg` is the glyph as the page icon.
