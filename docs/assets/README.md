# Assets

Screenshots referenced by the project README. Each is a PNG under 500 KB.

| File | Content | Shown at |
|---|---|---|
| `screenshot.png` | Main window (720 x 560) on the Files tab in Crystal Black, project set up, Auto copy and Auto sort on, version pill v2.1.0 | `width="860"` |
| `idle.png` | Start screen (420 x 560): Launch Premiere Pro and the recent projects, version pill v2.1.0 | `width="36%"` |
| `settings.png` | Settings sheet over the main window: Appearance with the two theme cards and Behaviour with Keep running and Show activity | `width="62%"` |
| `files.png` | Main window on the Actions tab in Crystal Sky: one visibility switch per quick action in the eight groups (file name kept from 2.0) | `width="860"` |

## How they were taken

The host runs from source in fake mode (no Premiere): `python -m premiere_deck --fake --fake-state <state>` with
`PYTHONPATH=src`, a scratch `PREMIERE_DECK_HOME` and `PREMIERE_DECK_CEP_DIR`, started through a small launcher that
sets `webview.settings["REMOTE_DEBUGGING_PORT"]` before importing `premiere_deck.shell.main`. The page is driven over
the Chrome DevTools Protocol (Node, no packages): `window.pywebview.api.fake_premiere('connected')` for the state,
`click()` on the Files / Actions tab and the gear, `set_settings({theme})` for the theme, and for the Files shot
`module_action('files', 'files.setup')` followed by `module_action('files', 'files.auto_copy', true)` and
`'files.auto_sort'` to show both switches on.

Each capture is `Page.captureScreenshot` with `Emulation.setDeviceMetricsOverride` at `deviceScaleFactor` 2, then
Pillow: resize to 0.75 (Lanczos), `quantize(256, MEDIANCUT, FLOYDSTEINBERG)`, `optimize=True`. Before the capture a
DOM pass replaces local scratch paths and user names with neutral ones (`D:\Footage\...`, `D:\Apps\PremiereDeck\...`);
the project media is the fake agent's canned data, never real files. The version pill shows the source version
(v2.1.0 for all four since the 2.1 round). The idle shot uses three empty `.prproj` files in the scratch home as the
recent projects, shown as `D:\Footage\...`.
