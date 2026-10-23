<div align="center">

<img src="src/premiere_deck/ui/assets/glyph.svg" alt="" width="96" height="96">

# Premiere Deck: Adobe Premiere Pro panel with a media organizer and quick actions for Windows

**Premiere Deck is a Windows application and a small panel inside Adobe Premiere Pro. It sorts project media into folders and bins by content, syncs outside media into the project folder, packs the project into a ZIP, and adds one-button versions of routine timeline and project operations. One portable exe, no installer, no account; it installs the panel itself and updates from GitHub Releases.**

[![Download](https://img.shields.io/badge/Download-PremiereDeck.exe-ff8a4a?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/softoblivion/premiere-deck/releases/latest/download/PremiereDeck.exe)
[![Release](https://img.shields.io/github/v/release/softoblivion/premiere-deck?display_name=release&label=latest&style=for-the-badge)](https://github.com/softoblivion/premiere-deck/releases/latest)

![Platform](https://img.shields.io/badge/platform-Windows_10_|_11-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-ES_modules-F7DF1E?logo=javascript&logoColor=black)
![HTML5](https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white)
![CSS](https://img.shields.io/badge/CSS-663399?logo=css&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-007808?logo=ffmpeg&logoColor=white)
[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)

<img src="docs/assets/screenshot.png" alt="Premiere Deck main window on the Files tab in Crystal Black: Sort project, Sync all media, Sync used media and Make ZIP" width="860">

<img src="docs/assets/idle.png" alt="Premiere Deck start screen with the Launch Premiere Pro button and the recent projects" width="36%">&nbsp;<img src="docs/assets/settings.png" alt="Premiere Deck settings sheet with the Crystal Black and Crystal Sky theme cards" width="62%">

<sub>Launch Premiere Pro from the Deck · open the panel once · sort and sync project media, run quick actions from the panel.</sub>

</div>

---

## Contents

- [Why Premiere Deck](#why-premiere-deck)
- [Quick start](#quick-start)
- [Features](#features)
- [How it works](#how-it-works)
- [Usage](#usage)
- [Settings](#settings)
- [Data and privacy](#data-and-privacy)
- [Updates](#updates)
- [Building from source](#building-from-source)
- [Project structure](#project-structure)
- [Third-party components](#third-party-components)
- [Disclaimer](#disclaimer)
- [License](#license)

## Why Premiere Deck

- **Routine out of the way.** Operations that take several steps in Premiere's own interface (an adjustment layer over the selection, a numbered project version, a copy of a sequence as its next version, a 9:16 copy) become single icon tiles in a panel.
- **Media sorted by what it is.** One button files every media item of the project folder into the layout folders and the matching bins: camera footage, stock downloads and generated clips apart; audio as voice, music or effects. Files imported from anywhere are copied into the layout, relinked and filed; a project item moved between bins moves its file.
- **Your originals are safe.** Files outside the project folder are only copied, never moved, renamed or deleted. Inside the project folder files move between layout folders only when you press Sort project or move an item between bins, and every change is journaled and can be undone.
- **A window that follows Premiere.** A small start screen launches Premiere Pro and lists recent projects; when the panel connects, the window widens into the Files and Actions tabs. Two themes, Crystal Black and Crystal Sky, are shared by the window and the panel.
- **Free and open source.** GPL-3.0, no trial, no paid tier, no account, no telemetry; build it yourself if you prefer.
- **Portable and offline.** One `PremiereDeck.exe` with a data folder next to it. The audio model ships inside the exe; nothing is downloaded for it and none of your media is uploaded.

## Quick start

1. Download [`PremiereDeck.exe`](https://github.com/softoblivion/premiere-deck/releases/latest/download/PremiereDeck.exe) into a folder of its own, for example `D:\Apps\PremiereDeck\`.
2. Run it. The app installs its panel into Premiere's per-user extensions folder.
3. Press **Launch Premiere Pro** (or start Premiere yourself), then open **Window > Extensions > Premiere Deck** once. Premiere keeps the panel in the workspace, and the Deck window widens when the panel connects.

**Requirements:** Windows 10 or 11, Adobe Premiere Pro 2026 (the panel manifest accepts Premiere Pro 14.0 and newer; 2026 is the version it is tested with), and the Microsoft Edge WebView2 Runtime (preinstalled on Windows 11 and on up-to-date Windows 10). No installer, no admin rights, no GPU, no model download.

> [!NOTE]
> The executable is not code-signed, so Windows SmartScreen may warn on first start (**More info → Run anyway**). The panel is not signed with a certificate trusted by Adobe either: if Premiere does not load it, the waiting screen offers once, after a confirmation, to turn on Premiere's extension debug mode (a per-user `PlayerDebugMode` value). **Settings** shows the state and can undo it.

## Features

| | |
|---|---|
| **Files** | Keeps project media sorted the same way on disk and in the project panel. **Sort project** files the media inside the project folder by content; **Sync all media** and **Sync used media** copy media from outside into a `Media` folder next to the project and relink it; **Make ZIP** packs the project folder into a `.zip` beside it after a check for offline media. Imported media is copied into the layout and filed, files dropped into the layout are imported, and bin moves are mirrored to disk. A journal lists every change with a per-entry **Undo**, and a running job has a **Stop** button. |
| **Folders by content** | Video is told apart by camera tags, camera file names, stock-site names and generator names: camera footage goes to `Footage`, stock downloads to `Stock`, generated clips to `Generated`. Audio goes to `Audio/Voice`, `Audio/Music` or `Audio/SFX`, decided by file names, your own overrides and a small audio model (YAMNet) that runs on the processor in well under a second per file. A wrong guess is corrected by moving the item to another bin. |
| **Quick actions** | 17 icon tiles for routine timeline and project operations in seven groups: Timeline, Selection, Clips, Tracks, Project, Sequence and Export, among them **Duplicate sequence** (copies the active sequence as `name v2`, `v3`, ...), **Save version** and **Social copy**. The window can hide any tile from the panel. Details in [QUICK_ACTIONS](docs/QUICK_ACTIONS.md). |
| **Panel with tabs and tiles** | Files and Actions tabs inside Premiere Pro; quick actions are icon tiles in collapsible groups; hovering a tile shows its full name and what it does; a pressed tile turns into a check or a cross, the group keeps the last result line, and a failed result opens a one-line reason. The panel follows the window's theme. |
| **Window that follows Premiere** | A compact start screen with **Launch Premiere Pro** and the three most recent projects; a waiting screen that shows the menu path to open the panel; a wide main view with the Files and Actions tabs once the panel is connected. |
| **Two themes** | Crystal Black (dark, default) and Crystal Sky (light), set in **Settings** and applied to the window and the panel together. |
| **Panel installs itself** | The app installs, verifies and updates its Premiere panel; **Settings** can check, reinstall or remove it. The panel only shows what the app sends, so new modules need no panel update. |
| **Self-updating app** | New releases are detected on launch and then every five minutes, downloaded in the background (or on request), verified by size and SHA-256 and installed on **Restart**. The panel updates with the app. |
| **Portable** | One exe and a data folder next to it. The only places outside it are Premiere's per-user extensions folder and the optional debug-mode value. |

## How it works

| Component | Role |
|---|---|
| **Host** (`PremiereDeck.exe`) | Window (compact start and waiting screens, wide Files and Actions tabs, Settings sheet), tray icon, WebSocket server on `127.0.0.1`, Premiere launcher, panel installer, updater. It owns all logic and state. |
| **Panel** (CEP extension in Premiere Pro) | A thin client: a headless agent runs the ExtendScript the host sends and forwards Premiere events; the visible panel (Files and Actions tabs) renders what the host sends and returns button presses. |
| **Modules** (Files, Quick actions) | Each module is code in the host behind one contract: a status, settings, actions, a panel section and a window page. A new module touches nothing in the panel. |

The window has three states that follow Premiere: **idle** (Premiere is not running), **waiting** (Premiere runs, the panel is not open yet) and **connected** (the panel is open; the window widens). The app never opens the panel itself.

Contracts: [ARCHITECTURE](docs/ARCHITECTURE.md) · [PROTOCOL](docs/PROTOCOL.md) · [MODULES](docs/MODULES.md) · [PANEL](docs/PANEL.md)

## Usage

1. Run `PremiereDeck.exe`. The start screen shows the Premiere status, **Launch Premiere Pro** and the three most recent projects; a click on a project starts Premiere with it.
2. Press **Launch Premiere Pro** (or start Premiere yourself). The window then shows **Waiting for the panel**: open **Window > Extensions > Premiere Deck** once, and Premiere keeps the panel in the workspace for later sessions. The window widens and shows the **Files** and **Actions** tabs. The waiting screen also has **Check panel update**, **Check for updates** and **Reinstall panel**.
3. **Files:** open a project and press **Set up project** in the panel or on the Files tab. The app creates the 13 layout folders and the matching bins: `Footage`, `Stock`, `Generated`, `Overlays`, `Audio/Music`, `Audio/SFX`, `Audio/Voice`, `Graphics`, `Motion`, `Media`, `Exports`, `Exports/Frames` and `Project`. From then on:
   - Media imported from outside the project folder is copied into the layout, relinked and filed; files dropped into a layout folder are imported into the matching bin; moving an item to another bin moves its file.
   - **Sort project** files the media that already lies inside the project folder into the layout folders and bins by content. Files inside the project folder are moved; files outside it are copied and never touched.
   - **Sync all media** copies every item whose file lies outside the project folder into `Media` next to the project; **Sync used media** does the same for the items placed in sequences. Nothing is sorted or deleted; run **Sort project** afterwards to file the synced media.
   - **Make ZIP** checks for offline media and writes `<project folder>.zip` beside the project folder. While media is still outside the folder it asks for a sync first.
   - **Stop** ends a running job, and **Undo last** and the journal's **Undo** reverse changes.
4. **Quick actions:** select clips or place the playhead in Premiere and press a tile in the panel. **Social copy** and **Export sequence** carry a badge (a format such as 9:16, or an export preset) that opens a small menu; presets are the built-in ones plus any `.epr` file placed in `premiere-deck_data\presets\`. The **Actions** tab of the window holds one switch per action for what the panel shows.

Closing the window hides the app to the tray while Premiere runs; it exits when Premiere exits or on **Quit**.

| Tray menu | Action |
|---|---|
| **Open** | Shows the window |
| **Launch Premiere Pro** | Starts Premiere |
| **Quit** | Exits the app |

<p align="center">
  <img src="docs/assets/files.png" alt="The Actions tab in Crystal Sky: one switch per quick action, grouped, and the export presets folder" width="860">
</p>

<p align="center"><sub>The Actions tab in Crystal Sky: every quick action can be shown in or hidden from the panel.</sub></p>

## Settings

In the **Settings** sheet (the gear in the title bar):

| Setting | Meaning |
|---|---|
| **Theme** | Crystal Black (default) or Crystal Sky, for the window and the panel. |
| **Keep running with Premiere** | Closing the window hides it to the tray while Premiere Pro is running (default on). |
| **Updates** | **Check for updates** and **Download updates automatically** (default on) for the app; the installed and bundled panel versions with **Check now** and **Reinstall**. |
| **Premiere** | The executable, detected automatically; **Choose file** overrides it, **Use detected** restores it. |
| **Panel** | Target folder, extension debug mode state, **Remove the panel from Premiere**. |
| **Modules** | One switch per module (Files, Quick actions). |
| **Tools** | Status and source of `ffmpeg` / `ffprobe` (the data folder first, then the system path, else a download). |
| **Data** | Location of the app's data, with **Open**. |
| **About** | Version, licence and the GitHub link. |

Module settings are on the module tabs: below the fold of the Files tab are the layout editor (folders, rules, overrides) and the project switches (watch, enforce bins, mirror bins, import exports); the Actions tab holds the visibility of each quick action and the export presets folder.

## Data and privacy

All runtime data lives in `premiere-deck_data\` next to `PremiereDeck.exe` (override with the `PREMIERE_DECK_HOME` environment variable):

| Path | Content |
|---|---|
| `settings.json` | Preferences |
| `bin\` | `ffmpeg.exe` and `ffprobe.exe` when they are not taken from the system path |
| `panel\` | The panel staged for installation |
| `presets\` | Your own Media Encoder export presets (`.epr`) |
| `logs\` | `host.log` (rotating) and one log per job |
| `webview\` | The window's WebView2 profile |
| `updates\` | Downloaded app updates and the updater's log |

There is no models folder: the YAMNet audio model is part of the exe, is read from it and is never downloaded or copied out. At start the app removes the `models\` and `cache\search\` folders that earlier versions left in the data folder.

Two locations are outside the data folder, because Adobe dictates them:

- `%APPDATA%\Adobe\CEP\extensions\com.softoblivion.premieredeck\` holds the installed panel; the app owns this folder and writes, replaces and removes it.
- `HKCU\Software\Adobe\CSXS.<n>\PlayerDebugMode` is written only as the fallback described in the note under Quick start, after a confirmation, and removed from **Settings**.

Inside each project folder the app keeps a hidden `.premiere-deck\` folder with the project's layout and settings, and the Files journal. It travels with the project when the folder is copied. An `index\` folder inside `.premiere-deck\` left by an earlier version is unused and can be deleted by hand.

Network traffic:

| Destination | When |
|---|---|
| GitHub Releases of `softoblivion/premiere-deck` | Update check and download |
| `github.com/yt-dlp/FFmpeg-Builds` | One download of ffmpeg, only when no ffmpeg is found, verified by SHA-256 |

Nothing else leaves the computer: no accounts, no telemetry, and none of your media, file names or project data is uploaded. The audio classification runs locally on the processor. The connection between the app and the panel stays on `127.0.0.1`.

## Updates

The app checks GitHub Releases on every launch in the background and then every five minutes. A new version is downloaded on **Download** (or automatically, when that option is on), verified by byte size and SHA-256 against the release's `SHA256SUMS`, and installed by **Restart to update**: the downloaded exe waits for the app to exit, keeps the old file as `PremiereDeck.exe.old` until the new one has started, and relaunches it. If anything fails the current exe stays untouched. At the next start the app installs the matching panel version and an open panel reloads; Premiere has to be restarted only when the panel's manifest changed. Every release ships two assets: `PremiereDeck.exe` and `SHA256SUMS`. Release notes live in [`docs/releases/`](docs/releases/).

## Building from source

Requirements: Windows, Python 3.14 (via the `py` launcher), PowerShell 5.1+. Node.js is needed only for the panel tests.

```powershell
git clone https://github.com/softoblivion/premiere-deck.git
cd premiere-deck
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

Runtime dependencies (`requirements.txt`): pywebview, pythonnet, websockets, watchdog, numpy and onnxruntime (CPU only). `requirements-dev.txt` adds PyInstaller and Pillow.

The script runs these steps and produces `release\PremiereDeck.exe` (one-file PyInstaller build) with `release\SHA256SUMS`:

1. Creates `.venv`, installs the dependencies and runs the unit tests.
2. Draws the application icon (`build\make_icon.py`).
3. Renders the two themes from `src\premiere_deck\ui\themes.json` into the kit's `themes.css` and the token block of the panel's stylesheet (`build\gen_themes.py`).
4. Writes the panel's version and manifest versions from the application version.
5. Signs the panel; without a signing certificate (`DECK_SIGN_P12`, `DECK_SIGN_PASSWORD`) it bundles an unsigned panel and prints a warning.
6. Bundles the window, the panel, the audio model and the layouts with PyInstaller.

| Task | Command |
|---|---|
| Run from source | `scripts\run_dev.bat` |
| Run without Premiere (mock agent) | `python -m premiere_deck --fake` with `PYTHONPATH=src`; `--fake-state not_running`, `waiting` or `connected` picks the start state, and Alt+1 / Alt+2 / Alt+3 switch it in the window |
| Regenerate the theme CSS | `python build\gen_themes.py` (`--check` only compares) |
| Unit tests | `set PYTHONPATH=src` and `set PREMIERE_DECK_HOME=%TEMP%\deck-tests`, then `.venv\Scripts\python.exe -m unittest discover -s tests -t .` |
| Panel tests | `node tests\panel\run.js` |
| Panel development | `powershell -NoProfile -File scripts\panel_dev.ps1` |

Architecture, contracts and build notes: [ARCHITECTURE](docs/ARCHITECTURE.md) · [BUILD](docs/BUILD.md) · [FILES](docs/FILES.md) · [QUICK_ACTIONS](docs/QUICK_ACTIONS.md) · [HOST_UI](docs/HOST_UI.md)

## Project structure

```
premiere-deck/
├── src/premiere_deck/
│   ├── core/          paths, settings, logging, subprocesses, ffmpeg tools, probing, jobs, updater setup
│   ├── host/          WebSocket server, Premiere launcher, panel installer, module registry, host facade
│   ├── premiere/      Premiere facade (typed operations) and the ExtendScript files behind it
│   ├── modules/       Files and Quick actions modules behind one contract
│   │   └── files/     sorting, sync, ZIP, journal, layouts and models/ (YAMNet audio model, class map, licence)
│   ├── panel/         the CEP extension installed into Premiere Pro (tabs, tiles, tooltips, fonts)
│   ├── shell/         entry point, window, size animation, JS <-> Python bridge, Windows helpers
│   ├── ui/            HTML / CSS / JS of the host window, themes.json and the Liquid UI kit copy (Crystal themes, Sora)
│   └── vendor/        vendored self-update library
├── tests/             unittest suite and panel JS tests
├── build/             PyInstaller spec, build script, theme generator, panel signing script, icon
├── scripts/           development run, panel development link
└── docs/              architecture, protocol, module docs, build notes, release notes, decisions, screenshots
```

## Third-party components

Bundled into the executable:

| Component | License |
|---|---|
| [CPython](https://www.python.org/) | PSF-2.0 |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause |
| [pythonnet](https://github.com/pythonnet/pythonnet), clr_loader | MIT |
| bottle, proxy_tools | MIT |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause |
| [watchdog](https://github.com/gorakhargosh/watchdog) | Apache-2.0 |
| [NumPy](https://numpy.org/) | BSD-3-Clause |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (CPU) | MIT |
| [YAMNet](https://www.kaggle.com/models/google/yamnet) audio classifier (ONNX export; source and checksums in `src/premiere_deck/modules/files/models/LICENSE-yamnet.txt`) | Apache-2.0; AudioSet class labels CC BY 4.0 |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL-2.0-or-later with the bootloader exception |
| Liquid UI kit copy | MIT |
| [Sora](https://github.com/sora-xor/sora-font) and [Geist Mono](https://github.com/vercel/geist-font) fonts | SIL OFL 1.1 |
| `CSInterface.js` ([Adobe CEP](https://github.com/Adobe-CEP/CEP-Resources)) | Adobe license, see the file header |

Downloaded at runtime:

| Component | License |
|---|---|
| [FFmpeg](https://ffmpeg.org/) / ffprobe (static GPL build from [yt-dlp/FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds)) | GPL, see the build |

## Disclaimer

Adobe and Premiere Pro are trademarks of Adobe Inc. Premiere Deck is an independent project and is not affiliated with, endorsed by or sponsored by Adobe.

## License

Released under the [GNU General Public License v3.0](LICENSE). Any distributed copy or derivative work must stay open source under the same license and keep the copyright notices.
