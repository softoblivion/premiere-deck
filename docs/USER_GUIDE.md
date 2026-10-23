# Premiere Deck user guide

Everything about using the app. Installation and the first steps are in the [README](../README.md).

## Usage

1. Run `PremiereDeck.exe`. The start screen shows the Premiere status, **Launch Premiere Pro** and the three most
   recent projects; a click on a project starts Premiere with it.
2. Press **Launch Premiere Pro** (or start Premiere yourself). The window then shows **Waiting for the panel**: open
   **Window > Extensions > Premiere Deck** once, and Premiere keeps the panel in the workspace for later sessions.
   The window widens and shows the **Actions** and **Files** tabs. The waiting screen also has **Check panel
   update**, **Check for updates** and **Reinstall panel**.
3. **Files:** open a project and press **Set up project** in the panel or on the Files tab. The app creates the 13
   layout folders and the matching bins (see [Files](#files)). Setting up does not turn the switches on.
4. **Quick actions:** select clips or place the playhead in Premiere and press a tile in the panel; hover a tile for
   its full name. Codes are short on purpose; [Quick actions](#quick-actions) lists them.

Closing the window hides the app to the tray while Premiere runs; it exits when Premiere exits or on **Quit**.

| Tray menu | Action |
|---|---|
| **Open** | Shows the window |
| **Launch Premiere Pro** | Starts Premiere |
| **Quit** | Exits the app |

## Settings

In the **Settings** sheet (the gear in the title bar):

| Setting | Meaning |
|---|---|
| **Theme** | Crystal Black (default) or Crystal Sky, for the window and the panel. |
| **Keep running with Premiere** | Closing the window hides it to the tray while Premiere Pro is running (default on). |
| **Show activity** | Shows a strip of recent results under the Premiere card of the main window (default off, Behaviour group). |
| **Updates** | **Check for updates** and **Download updates automatically** (default on) for the app; the installed and bundled panel versions with **Check now** and **Reinstall**. |
| **Premiere** | The executable, detected automatically; **Choose file** overrides it, **Use detected** restores it. |
| **Panel** | Target folder, extension debug mode state, **Remove the panel from Premiere**. |
| **Modules** | One switch per module (Files, Quick actions). |
| **Tools** | Status and source of `ffmpeg` / `ffprobe` (the data folder first, then the system path, else a download). |
| **Data** | Location of the app's data, with **Open**. |
| **About** | Version, licence and the GitHub link. |

Module settings are on the module tabs: the Files tab has the **Auto copy** and **Auto sort** switches beside
**Set up project**, and below the fold the layout editor (folders, rules, overrides) and the project switches
(watch, enforce bins, mirror bins, import exports); the Actions tab holds the visibility of each quick action.

## Files

**Set up project** creates the 13 layout folders and the matching bins: `Footage`, `Stock`, `Generated`,
`Overlays`, `Audio/Music`, `Audio/SFX`, `Audio/Voice`, `Graphics`, `Motion`, `Media`, `Exports`, `Exports/Frames`
and `Project`. From then on:

- The two switches beside **Set up project** (on the Files tab of the panel and of the window) are off for a new
  project; a project set up with 2.0 keeps its automatic filing and has both on. **Auto copy** copies media
  imported from outside the project folder into `Media` and relinks it. **Auto sort** files new media of the
  project folder (copied by Auto copy, dropped into a layout folder or loose in the project folder) into the layout
  folders and bins by content. With both on, an outside file is copied straight into its layout folder. With both
  off the app only watches.
- Files dropped into a layout folder are imported into the matching bin; moving an item to another bin moves its
  file.
- **Sort project** files the media that already lies inside the project folder into the layout folders and bins by
  content. Files inside the project folder are moved; files outside it are copied and never touched.
- **Sync all media** copies every item whose file lies outside the project folder into `Media` next to the project;
  **Sync used media** does the same for the items placed in sequences. Nothing is sorted or deleted; run **Sort
  project** afterwards to file the synced media.
- **Make ZIP** checks for offline media and writes `<project name>.zip` into the project's `Exports` folder (the
  folder itself is left out of the archive), with a report file beside it. While media is still outside the folder
  it asks for a sync first.
- **Stop** ends a running job, and **Undo last** (and the journal's **Undo** in the window) reverse changes.

The Files tab has its own codes: SETUP = Set up project · SORT = Sort project · SYNC = Sync all media · USED = Sync
used media · ZIP = Make ZIP · AUTO COPY and AUTO SORT (the two switches).

## Quick actions

How a few of the actions work:

- **CLR** (Colour label) has a menu of Premiere's 16 label colours. A pick colours the selected timeline clips
  through Premiere's own Edit > Label menu (the host posts the menu command; with nothing selected in the timeline
  it colours the items selected in the Project panel, bins included) and the marker at the playhead, using the
  nearest of Premiere's 8 marker colours. The result line says what was coloured.
- **16:9**, **9:16** and **CUST** make a reframed copy of the active sequence (Auto Reframe) named `<name> 16x9`,
  `<name> 9x16` or `<name> <W>x<H>`; the frame size follows the ratio (a 1080x1920 sequence becomes 1920x1080 at
  16:9). **CUST** takes a size such as `1080x1350`.
- **EXP** opens Premiere's Export mode for the active sequence (the host posts File > Export > Media). Presets and
  settings are chosen in Premiere.
- **SLOW**, **MED** and **FAST** ease the keyframe pair around the playhead on every animated property of the
  selected clip; **LIN** sets the pair back to linear. The ease is written as one keyframe per frame (Premiere's
  scripting has no bezier handles), so a long pair adds many keyframes.
- **SOLO** and **LOCK** are switches: on mutes every other audio track (or locks every track without a selected
  clip); off restores the earlier states.
- The **Actions** tab of the window holds one switch per action for what the panel shows.

| Group | Codes = full names |
|---|---|
| Timeline | ADJ = Adjustment layer · RZR = Razor all tracks · EMPT = Delete empty tracks |
| Tracks | +V = Add video track · +A = Add audio track · −V = Remove video track · −A = Remove audio track · SOLO = Solo audio track · LOCK = Lock other tracks |
| Selection | SRC = Select same source · OFF = Select offline clips |
| Clips | FIT = Set to frame size · CLR = Colour label · BIN = Colour labels by bin |
| Project | SAVE = Save version · CONS = Consolidate duplicates · DIR = Open project folder · REV = Reveal clip file |
| Sequence | DUP = Duplicate sequence · 16:9 = Reframe 16:9 · 9:16 = Reframe 9:16 · CUST = Reframe custom size |
| Export | EXP = Export · FRM = Export frame |
| Keys | SLOW = Ease slow · MED = Ease medium · FAST = Ease fast · LIN = Linear keys |

<div align="center">

<img src="assets/files.png" alt="The Actions tab in Crystal Sky: one switch per quick action, in the eight groups Timeline, Tracks, Selection, Clips, Project, Sequence, Export and Keys" width="860">

<sub>The Actions tab in Crystal Sky: every quick action can be shown in or hidden from the panel; the Tracks, Export and Keys groups are in the second column.</sub>

</div>

Every action with its Premiere calls and limits: [QUICK_ACTIONS](QUICK_ACTIONS.md).

## Data and privacy

All runtime data lives in `premiere-deck_data\` next to `PremiereDeck.exe` (override with the
`PREMIERE_DECK_HOME` environment variable):

| Path | Content |
|---|---|
| `settings.json` | Preferences |
| `bin\` | `ffmpeg.exe` and `ffprobe.exe` when they are not taken from the system path |
| `panel\` | The panel staged for installation |
| `logs\` | `host.log` (rotating) and one log per job |
| `webview\` | The window's WebView2 profile |
| `updates\` | Downloaded app updates and the updater's log |

There is no models folder: the YAMNet audio model is part of the exe, is read from it and is never downloaded or
copied out. At start the app removes the `models\` and `cache\search\` folders that earlier versions left in the
data folder. A `presets\` folder left by version 2.0 is no longer used and can be deleted by hand; the app does not
touch it.

Two locations are outside the data folder, because Adobe dictates them:

- `%APPDATA%\Adobe\CEP\extensions\com.softoblivion.premieredeck\` holds the installed panel; the app owns this
  folder and writes, replaces and removes it.
- `HKCU\Software\Adobe\CSXS.<n>\PlayerDebugMode` is written only as the fallback when Premiere does not load the
  panel (the waiting screen offers it once, after a confirmation), and removed from **Settings**.

Inside each project folder the app keeps a hidden `.premiere-deck\` folder with the project's layout and settings,
and the Files journal. It travels with the project when the folder is copied; the two switches Auto copy and Auto
sort are stored there. An `index\` folder inside `.premiere-deck\` left by an earlier version is unused and can be
deleted by hand.

Network traffic:

| Destination | When |
|---|---|
| GitHub Releases of `softoblivion/premiere-deck` | Update check and download |
| `github.com/yt-dlp/FFmpeg-Builds` | One download of ffmpeg, only when no ffmpeg is found, verified by SHA-256 |

Nothing else leaves the computer: no accounts, no telemetry, and none of your media, file names or project data is
uploaded. The audio classification runs locally on the processor. The connection between the app and the panel
stays on `127.0.0.1`.

## Updates

The app checks GitHub Releases on every launch in the background and then every five minutes. A new version is
downloaded on **Download** (or automatically, when that option is on), verified by byte size and SHA-256 against
the release's `SHA256SUMS`, and installed by **Restart to update**: the downloaded exe waits for the app to exit,
keeps the old file as `PremiereDeck.exe.old` until the new one has started, and relaunches it. If anything fails
the current exe stays untouched. At the next start the app installs the matching panel version and an open panel
reloads; Premiere has to be restarted only when the panel's manifest changed. Every release ships two assets:
`PremiereDeck.exe` and `SHA256SUMS`. Release notes: [`releases/`](releases/).
