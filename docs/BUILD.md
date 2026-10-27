# Build, run, test, release

Toolchain, running from source, tests, the build, the release procedure and troubleshooting.

## 1. Toolchain

Python 3.14 (`py -3.14`), virtual environment `.venv` in the project root (`build\build.ps1 -SkipBuild` creates
and fills it). Versions of the first build (2026-10-21):

| Package | Version | Use |
|---|---|---|
| Python | 3.14.5 | runtime |
| pywebview | 6.2.1 (`edgechromium`, WebView2) | host window |
| pythonnet / clr_loader | 3.2.1 / 0.3.1 | WinForms backend of pywebview, tray icon (`NotifyIcon`) |
| websockets | 17.3 (asyncio server; sync client in tests and `--fake`) | host <-> panel transport |
| watchdog | 6.0.0 | Files module |
| numpy | 2.5.4 | audio samples for the YAMNet classifier (Files module) |
| onnxruntime | 1.31.0 (CPU provider only; the DirectML build of 1.x is gone) | runs `modules\files\models\yamnet.onnx` |
| Pillow | 13.0.0 (dev only, `requirements-dev.txt`) | `build\make_icon.py` (no runtime use in the host since 2.0) |
| PyInstaller / hooks-contrib | 6.22.3 / 2026.9 | one-file exe |
| ffmpeg / ffprobe | 9.0.2 (gyan.dev full build on PATH) | media probe; the app downloads its own copy into `<data>\bin` when none is found (`core\tools.py`, yt-dlp/FFmpeg-Builds, verified by SHA-256) |

`requirements.txt` holds the runtime packages, `requirements-dev.txt` adds PyInstaller. `pyproject.toml` carries the
same metadata (version from `premiere_deck.__version__`).

## 2. Run from source

- `scripts\run_dev.bat [--fake] [--debug]`: sets `PYTHONPATH=src`, `PREMIERE_DECK_HOME=<project>\premiere-deck_data`
  (unless already set) and runs `python -m premiere_deck`. Arguments: `--project <path>` (launch Premiere with it),
  `--from-premiere` (start in the tray), `--fake`, `--debug` (WebView2 dev tools, verbose log), `--ui-dir <folder>`.
- `python -m premiere_deck --fake`: an in-process fake agent (`host\fake_agent.py`) connects over the real WebSocket
  and answers scripts with canned data (project "Sample Project" in `<data>\fake\`), so the window, the registry and
  the modules run without Premiere. A fake run installs the panel into `<data>\fake-cep\...` unless
  `PREMIERE_DECK_CEP_DIR` is set: it never writes Adobe's folder. Its single-instance mutex is separate, so a fake
  window can run beside a real one.
- Environment: `PREMIERE_DECK_HOME` (data folder), `PREMIERE_DECK_CEP_DIR` (replaces
  `%APPDATA%\Adobe\CEP\extensions`; the bundle folder `com.softoblivion.premieredeck` is appended),
  `PREMIERE_DECK_RELEASE_URL` (update source for the end-to-end update test).
- Panel development: `scripts\panel_dev.ps1` replaces the installed panel folder with a directory junction to
  `src\premiere_deck\panel` (an installed copy is moved to `<name>.dev-backup`) and writes `.debug` (DevTools at
  `http://localhost:8098` for the panel, `8099` for the agent; both stay off 8088, which other Premiere tools may hold); `-Remove` takes the junction away. The host never
  installs over a junction and then writes `deck.json` into the source folder (keep `deck.json` and `.debug` out of
  git). Premiere must be restarted after manifest changes.
- Host window alone: serve `src\premiere_deck\ui` with any static server and open `index.html?mock=1`
  (`ui\js\mock.js` answers the bridge calls with canned data; flags
  `premiere=not_running|starting|connected|no_panel`, `missing=1`).
- Logs: `<data>\logs\host.log` (rotating 1 MB x 5): loggers `premiere_deck.*`, `module.<id>`, `panel.agent`,
  `panel.display`.

## 3. Tests

`.venv\Scripts\python -m unittest discover -s tests -t .` with `PYTHONPATH=src` and `PREMIERE_DECK_HOME` set to a
scratch folder (`build\build.ps1` sets both, plus `PREMIERE_DECK_CEP_DIR`, to a temp folder). Panel JS tests:
`node tests\panel\run.js` (Node is a development dependency only, not shipped). No test touches the real Premiere,
the user's CEP folder, the registry or the network:

- host tests open real WebSocket connections on free ports (`tests\test_host_server.py`, `test_host_hub.py`);
- the panel installer works on scratch folders with a dictionary registry (`MemoryRegistry`);
- Premiere discovery reads fake registry entries and fake `Program Files` folders;
- the probe tests generate their media with ffmpeg (H.264 + AAC mp4, VP9 WebM with alpha, ProRes 4444 with alpha,
  RGBA PNG, WAV) and skip when ffmpeg is not on PATH.

The live checks in Premiere Pro are manual.

## 4. Build

`powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1 [-SkipTests]`:

1. `.venv` and the requirements; the unit tests (unless `-SkipTests`).
2. `build\make_icon.py` -> `build\icon.ico` (exe, window and tray icon).
3. Writes `src\premiere_deck\panel\VERSION` and the manifest versions (`ExtensionBundleVersion`, each
   `<Extension Version>`) from `__version__`, copies the panel to `build\work\panel_plain` without `deck.json` and
   `.debug`.
4. `build\sign_panel.ps1` signs it: `ZXPSignCmd -sign <panel> <zxp> <cert.p12> <password>`, `-verify`, then unpacks
   the zxp to `build\work\panel_bundle` so `META-INF\signatures.xml` is present. Certificate and password from
   `DECK_SIGN_P12` and `DECK_SIGN_PASSWORD` (create the self-signed certificate once, outside the repository:
   `ZXPSignCmd -selfSignedCert US CA softoblivion "Premiere Deck" <password> deck-cert.p12`); `ZXPSignCmd` from
   `DECK_ZXPSIGNCMD` or `PATH`. Without the variables the build prints a
   warning and bundles the unsigned panel (dev builds; the panel then loads only with `PlayerDebugMode`).
5. PyInstaller one-file, windowed, `build\PremiereDeck.spec` (reads the prepared panel from `DECK_PANEL_BUNDLE`):
   bundles `premiere_deck/ui` (with the vendored kit), `premiere_deck/panel`, every non-Python file of the package
   (jsx, layouts, the YAMNet model) and `icon.ico`; every package module as a hidden import (modules are imported by name).
6. Output `release\PremiereDeck.exe` and `release\SHA256SUMS` (`<sha256>  PremiereDeck.exe`, UTF-8, LF).

## 5. Release

Release notes `docs\releases\vX.Y.Z.md`, tests and build green, the pre-publish check clean (no tooling files,
private paths or e-mail addresses in the tracked files and the history), tag `vX.Y.Z`, GitHub Release with
`PremiereDeck.exe` and `SHA256SUMS`. The end-to-end self-update test (an older build updates itself from a test
release source, `PREMIERE_DECK_RELEASE_URL`) runs before every release that touches the update path or the panel
installer (the panel update rides on it).

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The panel does not appear in Window > Extensions | unsigned panel and `PlayerDebugMode` off, or a CEP version below 9 | sign the build (section 4); or Deck page / Settings > Panel > debug mode (writes `HKCU\Software\Adobe\CSXS.9..12\PlayerDebugMode=1` after a confirmation; Settings removes exactly the values it wrote); check `%APPDATA%\Adobe\CEP\extensions\com.softoblivion.premieredeck\VERSION` |
| The panel shows "Connecting to Premiere Deck…" | the host is not running, or `deck.json` is stale | start the host; it rewrites `deck.json` at every start (port and token change per run) |
| "Restart Premiere Pro to finish the Premiere Deck update" | the manifest changed with the update (`<data>\panel\install.json`) | restart Premiere; CEP reads manifests only at start |
| The host does not start: "no free port in 47800..47809" | other programs hold all ten ports | close them; the range is fixed |
| "The window could not be started" | Microsoft Edge WebView2 Runtime missing | install the Evergreen WebView2 Runtime |
| ffmpeg download fails | offline, proxy, or GitHub blocked | put `ffmpeg.exe` and `ffprobe.exe` on PATH or into `<data>\bin` |
| A second start shows nothing new | the first instance is in the tray (Premiere runs) | a second start shows the first instance's window (WM_COPYDATA); check the tray |
| Test scratch folders stay behind | a log handler or a thread still holds a file | `tests\test_host_helpers.remove_tree` retries; stop hubs and servers in `tearDown` first |
