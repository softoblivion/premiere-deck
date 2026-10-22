# Premiere Deck: architecture

Owner: integrator. The map of the project: goal, stack, layout, module table, data flow, runtime data. Every
module has one owning document (table in section 4) that holds its contract; a contract changes in the document
first (`D:\Projects\_workspace\PARALLEL.md`).

## 1. Goal

A Windows desktop application (one portable `PremiereDeck.exe`, the host) plus a thin panel inside Adobe Premiere
Pro (a CEP extension the host installs and updates itself). Together they remove routine from editing:

1. The host launches Premiere (with a project or without); the panel connects to the host by itself as soon as
   Premiere is up, whichever side started first.
2. **Files**: project media stays sorted the same way on disk and in the project panel. Media imported from
   outside the project folder is copied into the project's layout, relinked to the copy and filed in the matching
   bin; files dropped into the layout on disk are imported into the matching bin; a project item moved between
   bins moves its file; "Collect project" fixes an existing project; "Pack for sharing" guarantees that a copied
   project folder opens elsewhere without offline media. Originals are never moved or deleted.
3. **Quick actions**: buttons in the panel for operations editors do many times a day that take several steps in
   Premiere's own interface (adjustment layer over the selection, remove all gaps, save a numbered version,
   social-format copy of a sequence, export frame or sequence into the layout, selection and track helpers).
4. **AI search**: a local model indexes the frames of the project's footage; "find frames with a car" returns
   clips and times; a result opens in the Source Monitor or is inserted at the playhead. Nothing leaves the
   machine; the model is downloaded once when the module is enabled.
5. Every function is a module behind one contract (`MODULES.md`), so later modules (captions, silence removal,
   punch-in zooms) add code to the host only; the panel never changes for a new module.

Design principles (decisions `docs/decisions/0001-host-owns-logic.md`):

- The host owns all logic and state; the panel is a thin client that renders what the host sends, runs the
  ExtendScript the host sends and forwards Premiere events. A new module needs no panel change.
- Local only: no accounts, no telemetry, no cloud calls except GitHub Releases (self-update) and model downloads.
- Safe by default on the user's media: copy, never move or delete without an explicit confirmation; every
  operation is journaled and reversible from the journal.
- English-only product (global rule 2026-10-21): every user-facing string, in the host and in the panel.

## 2. Stack and why

| Layer | Choice | Why |
|---|---|---|
| Host window | pywebview 6.2.1 (`edgechromium`, WebView2), fixed-size window, Liquid UI kit vendored as `src/premiere_deck/ui/vendor/liquid_ui` | The workspace default (`_workspace\RECIPES.md` section 4); same family as Media Forge and Media Saver. |
| Host runtime | Python 3.14 | pywebview, pythonnet, PyInstaller, websockets, watchdog, numpy, onnxruntime-directml and tokenizers ship 3.14 wheels (checked 2026-10-21). |
| Host <-> panel transport | WebSocket on `127.0.0.1`, library `websockets` (asyncio) in a background thread; JSON messages (`PROTOCOL.md`) | CEF inside Premiere speaks WebSocket natively; one persistent connection per panel instance, push both ways, no polling files. |
| Panel | CEP 11/12 extension (CSXS manifest 7.0, Premiere 14.0 and newer): HTML / CSS / plain JS (ES2015, no build step), `CSInterface.js`, ExtendScript through `evalScript` | The only platform with full project and timeline scripting today; UXP lacks captions and parts of the timeline API (2026). Thin by design so a UXP port replaces only this layer. |
| Premiere scripting | ExtendScript (ES3) snippets shipped by the host at runtime, one `.jsx` file per operation under `src/premiere_deck/premiere/jsx/` and the modules' own `jsx/` folders, plus a small shared library (`lib.jsx`, `json2.js`) | Scripts live with the Python code that calls them and are replaceable without touching the panel. |
| Media probing | `ffprobe.exe` / `ffmpeg.exe` (data `bin\` first, then PATH, else downloaded once; `RECIPES.md` section 3) | Streams, duration, alpha, frame extraction for the AI index. |
| File watching | `watchdog` 6 | Disk-side changes of the project folder. |
| AI search | CLIP ViT-B/32 as ONNX (vision and text encoders), `onnxruntime-directml` (any Windows GPU, CPU fallback), `tokenizers` for the CLIP BPE | Small (432 MB of model files per `models.json`, downloaded on enabling), no CUDA toolkit, runs on NVIDIA, AMD and Intel GPUs alike. SigLIP is the planned upgrade (`AI_SEARCH.md` 7). |
| Self-update | vendored `selfupdate` (`src/premiere_deck/vendor/selfupdate`), GitHub Releases of `softoblivion/premiere-deck`, assets `PremiereDeck.exe` + `SHA256SUMS` | The workspace's shared updater; the panel updates with the exe (section 6). |
| Packaging | PyInstaller one-file, windowed; the panel folder bundled as data | One exe to download. |

## 3. Repository layout

```
premiere-deck/
  README.md, LICENSE (GPL-3.0), pyproject.toml, requirements.txt, requirements-dev.txt
  docs/ARCHITECTURE.md      this file
  docs/PROTOCOL.md          host <-> panel protocol: discovery, handshake, messages, UI tree, scripts, events
  docs/MODULES.md           module contract: manifest, lifecycle, host services, UI, actions, settings, tests
  docs/PREMIERE_API.md      the Premiere facade: typed operations, snapshot model, jsx conventions, known quirks
  docs/PANEL.md             the CEP panel: manifest, two extensions, install and update, renderer, event forwarding
  docs/HOST_UI.md           the host window: screens, states, copy
  docs/FILES.md             Files module
  docs/QUICK_ACTIONS.md     Quick actions module
  docs/AI_SEARCH.md         AI search module
  docs/BUILD.md             toolchain, dev run, tests, build, signing the panel, release
  docs/releases/vX.Y.Z.md   release notes
  docs/decisions/           project decision records
  docs/assets/              README screenshots
  src/premiere_deck/__init__.py          __version__ (the only place the version lives; the panel's VERSION is generated from it)
  src/premiere_deck/core/paths.py        AppPaths: data folder, atomic writes (the only module that builds data paths)
  src/premiere_deck/core/settings.py     host settings (persistence, validation)
  src/premiere_deck/core/log.py          logging setup (rotating file, per-module loggers)
  src/premiere_deck/core/procs.py        subprocess runner (no console, UTF-8, Job Object kill tree)
  src/premiere_deck/core/tools.py        ffmpeg / ffprobe discovery and download
  src/premiere_deck/core/probe.py        media probing (ffprobe JSON -> MediaInfo), quick hash
  src/premiere_deck/core/jobs.py         background job queue with progress events
  src/premiere_deck/core/selfupdate.py   configuration of the vendored updater
  src/premiere_deck/host/server.py       WebSocket server, discovery file, client registry (agent / display)
  src/premiere_deck/host/protocol.py     message types, UI tree schema and validation (integrator-owned)
  src/premiere_deck/host/launcher.py     Premiere discovery and launch, process watch
  src/premiere_deck/host/panel_install.py install / update / verify the panel folder, PlayerDebugMode fallback
  src/premiere_deck/host/registry.py     module registry: discovery, lifecycle, routing of actions and events
  src/premiere_deck/host/hub.py          the host facade used by the window bridge and the modules (HostServices)
  src/premiere_deck/premiere/api.py      PremiereApi: typed operations over the agent connection (contract in PREMIERE_API.md)
  src/premiere_deck/premiere/model.py    Snapshot, ProjectItem, Sequence, Selection dataclasses
  src/premiere_deck/premiere/jsx/        lib.jsx, json2.js, one .jsx per facade operation
  src/premiere_deck/modules/base.py      Module base class, ModuleContext, FakeHost for tests (integrator-owned)
  src/premiere_deck/modules/files/       Files module (module.py, layout.py, classify.py, sync.py, journal.py, jsx/)
  src/premiere_deck/modules/quick_actions/ Quick actions module (module.py, actions/*.py, jsx/)
  src/premiere_deck/modules/ai_search/   AI search module (module.py, index.py, embed.py, models.py, jsx/)
  src/premiere_deck/panel/               the CEP extension as shipped: CSXS/manifest.xml, index.html, css/, js/, host.jsx, CSInterface.js, VERSION
  src/premiere_deck/shell/main.py        entry: single instance, window, lifecycle, server start
  src/premiere_deck/shell/bridge.py      pywebview js_api for the host window, event pump
  src/premiere_deck/shell/winutil.py     DWM corners, fixed-size window, Explorer, mutex
  src/premiere_deck/ui/                  host window page: index.html, css/, js/ (incl. mock.js), assets/
  src/premiere_deck/ui/vendor/liquid_ui/ vendored design kit (synced by _tools\vendor-sync.ps1)
  src/premiere_deck/vendor/selfupdate/   vendored updater (synced by _tools\vendor-sync.ps1)
  tests/                     unittest suite, mirrors src (test_core_*, test_host_*, test_premiere_*, test_modules_*, test_panel_*)
  build/                     entry.py, PremiereDeck.spec, build.ps1, sign_panel.ps1, icon
  scripts/run_dev.bat        run from source; scripts/panel_dev.ps1 links the panel folder for live editing
```

Not tracked: `release/`, `build/work`, `build/dist`, `.venv/`, `premiere-deck_data/`, `reports/`.

## 4. Module table

Owner = the session that may write there during the current task (`_workspace\PARALLEL.md`). Shared files
(contracts, this file, `__init__.py`, `protocol.py`, `modules/base.py`, `premiere/model.py`) belong to the
integrator; a worker that needs a change there asks.

| Module | Path | Owning doc | Owner |
|---|---|---|---|
| Core | `src/premiere_deck/core/**`, `tests/test_core_*` | `BUILD.md` (toolchain), `FILES.md` 4 (probe fields) | host worker |
| Host (server, launcher, panel install, registry, hub) | `src/premiere_deck/host/**` except `protocol.py`, `tests/test_host_*` | `PROTOCOL.md`, `PANEL.md` 4 | host worker |
| Shell, host window, build | `src/premiere_deck/shell/**`, `src/premiere_deck/ui/**` (except `vendor/`), `build/`, `scripts/`, `pyproject.toml`, `requirements*.txt`, `tests/test_shell_*` | `HOST_UI.md`, `BUILD.md` | host worker |
| Premiere facade | `src/premiere_deck/premiere/**` except `model.py`, `tests/test_premiere_*` | `PREMIERE_API.md` | panel worker |
| Panel | `src/premiere_deck/panel/**`, `tests/test_panel_*` | `PANEL.md` | panel worker |
| Files | `src/premiere_deck/modules/files/**`, `tests/test_modules_files*` | `FILES.md` | files worker |
| Quick actions | `src/premiere_deck/modules/quick_actions/**`, `tests/test_modules_quick*` | `QUICK_ACTIONS.md` | actions worker |
| AI search | `src/premiere_deck/modules/ai_search/**`, `tests/test_modules_ai*` | `AI_SEARCH.md` | search worker |
| Contracts | `docs/**`, `README.md`, `src/premiere_deck/__init__.py`, `host/protocol.py`, `modules/base.py`, `premiere/model.py` | this file | integrator |

## 5. Data flow

```
 PremiereDeck.exe (host)                                   Adobe Premiere Pro
 +-----------------------------------------+               +----------------------------------+
 | shell: window (Liquid UI) <-> bridge    |               | CEP extension (PANEL.md)         |
 | host.hub: HostServices                  |  WebSocket    |  headless "agent":               |
 |   registry -> modules (files, actions,  |<------------->|   evalScript, app.bind events,   |
 |              ai_search, ...)            |  127.0.0.1    |   project polling                |
 |   premiere.api -> jsx over the agent    |<------------->|  visible "display":              |
 |   server: discovery file, clients       |               |   renders the UI tree, sends     |
 | core: paths, settings, probe, jobs,     |               |   actions                        |
 |       tools (ffmpeg), selfupdate        |               +----------------------------------+
 +-----------------------------------------+
        |  watchdog                                        ExtendScript DOM: app.project, sequences,
        v                                                  sourceMonitor, encoder, QE
   project folder on disk (<root>\Footage, Audio\..., .premiere-deck\)
```

1. Start: the host starts the WebSocket server on the first free port of `47800..47809`, writes the discovery
   file `deck.json` into the installed panel folder (`PROTOCOL.md` 2), installs or updates the panel if its
   `VERSION` differs from the host's (`PANEL.md` 4), then shows the window.
2. Premiere up (launched by the host or by the user): the headless extension starts on `applicationActivate`,
   reads `deck.json`, connects as `agent`, sends `hello`; if no host answers within 5 s and `deck.json` allows
   it, the agent starts the host exe. The visible panel connects as `display` when opened; the host asks the
   agent to open it on the first connection of a session (`requestOpenExtension`).
3. Session: the host asks for a project snapshot, starts the modules' project hooks (Files: watcher on the
   project root), and pushes the UI tree to the display. Premiere events arrive from the agent, go through the
   registry to the modules; module actions from the display go to the module that owns the action id; the module
   answers with UI changes, toasts and jobs. Scripts run through `PremiereApi`, which serialises them: one
   `evalScript` at a time per agent, with a timeout, so Premiere never gets two scripts at once.
4. Shutdown: closing the host window keeps the host in the tray while Premiere runs (setting
   `keep_running_with_premiere`, default on); the host exits when Premiere exits, or at once on "Quit".

## 6. Runtime data (portable)

`AppPaths` (`core/paths.py`) resolves everything from one root:

- app dir = the folder of `PremiereDeck.exe` (frozen) or the project root (from source); never the PyInstaller
  extraction folder.
- root = `%PREMIERE_DECK_HOME%` if set (tests set it to a scratch folder), else `<app dir>\premiere-deck_data`.
- `settings.json`: host settings (`HOST_UI.md` 6).
- `bin/`: `ffmpeg.exe`, `ffprobe.exe` when not taken from PATH, `versions.json`.
- `models/`: AI model files and `models.json` (source URL, size, SHA-256).
- `panel/`: the signed panel folder staged for installation (copied from the bundle on first run and on update).
- `logs/host.log` (rotating 1 MB x 5), `logs/jobs/<id>.log`.
- `webview/`: WebView2 profile.
- `updates/`: the vendored updater's folder.

Two locations outside the data folder, both dictated by Adobe and recorded here as the project's deviation from
"Portable projects" (`_workspace\CONVENTIONS.md`):

- `%APPDATA%\Adobe\CEP\extensions\com.softoblivion.premieredeck\`: the installed panel (CEP loads extensions
  from this folder only; no admin rights needed). The host owns this folder entirely: it writes, replaces and
  removes it. Tests and fake runs replace the extensions folder through `PREMIERE_DECK_CEP_DIR` (the value stands
  for `...\CEP\extensions`; the bundle folder name is appended).
- `HKCU\Software\Adobe\CSXS.<n>\PlayerDebugMode`: written only as the fallback when the signed panel fails to load
  (`PANEL.md` 4.3), with the user's confirmation.

Per project, inside the user's project folder: `<root>\.premiere-deck\` (hidden) with `project.json` (layout id,
rules overrides, module settings of this project), `journal.jsonl` (Files operations) and `index\` (AI search
embeddings). It travels with the project when the folder is copied, which is the point.

## 7. Version

`src/premiere_deck/__version__` is the only version. The build writes it into `panel/VERSION` and into the
panel manifest (`ExtensionBundleVersion`); the host compares the installed panel's `VERSION` against its own and
reinstalls on any difference. A protocol version (`PROTOCOL.md` 3, integer) changes only when a message shape
changes incompatibly.
