# -*- mode: python ; coding: utf-8 -*-
# One-file, windowed PyInstaller build of Premiere Deck. Run through build/build.ps1 (docs/BUILD.md 4).
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
PKG = SRC / "premiere_deck"
UI_DIR = PKG / "ui"
ICON = ROOT / "build" / "icon.ico"
WORK = ROOT / "build" / "work"
# build.ps1 prepares the panel here (VERSION and manifest version generated, signed when a certificate is set).
PANEL_DIR = Path(os.environ.get("DECK_PANEL_BUNDLE", WORK / "panel_bundle"))

if not (UI_DIR / "index.html").is_file():
    raise SystemExit(f"index.html not found in {UI_DIR}: the window page is required for the build")
if not (PANEL_DIR / "CSXS" / "manifest.xml").is_file():
    raise SystemExit(f"prepared panel not found in {PANEL_DIR}: run build/build.ps1")

VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (PKG / "__init__.py").read_text("utf-8")).group(1)

# --- Windows version resource ------------------------------------------------------------------------------------
_parts = (tuple(int(p) for p in re.findall(r"\d+", VERSION)[:4]) + (0, 0, 0, 0))[:4]
WORK.mkdir(parents=True, exist_ok=True)
_version_file = WORK / "_version_info.txt"
_version_file.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_parts}, prodvers={_parts}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Premiere Deck'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'PremiereDeck'),
      StringStruct('OriginalFilename', 'PremiereDeck.exe'),
      StringStruct('ProductName', 'Premiere Deck'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

# --- bundle contents ---------------------------------------------------------------------------------------------
# shell/main.resolve_ui_dir and core/paths.bundled_panel_dir read premiere_deck/ui and premiere_deck/panel next to
# the package inside the extraction folder; main.resolve_icon reads icon.ico at the archive root.
SKIP_DIRS = {"__pycache__", "ui", "panel"}
SKIP_SUFFIXES = {".py", ".pyc", ".pyo"}
datas = [(str(UI_DIR), "premiere_deck/ui"), (str(PANEL_DIR), "premiere_deck/panel")]
for folder, dirs, files in os.walk(PKG):
    rel = Path(folder).relative_to(PKG)
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        dirs[:] = []
        continue
    dirs[:] = [d for d in dirs if d != "__pycache__" and not (not rel.parts and d in SKIP_DIRS)]
    for name in files:
        if Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        datas.append((str(Path(folder) / name), str(Path("premiere_deck") / rel)))  # jsx, layouts, presets, notes
if ICON.is_file():
    datas.append((str(ICON), "."))

# Every package module: modules are imported by name at run time (host/registry.py). collect_submodules imports
# the package, so src must be on sys.path while the spec runs; the third-party packages the modules import lazily
# (inside functions) are named explicitly.
import sys  # noqa: E402

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_own = collect_submodules("premiere_deck")
if len(_own) < 20:
    raise SystemExit(f"collect_submodules found only {len(_own)} premiere_deck modules: check SRC on sys.path")
hiddenimports = _own + [
    "numpy",
    "onnxruntime",
    "tokenizers",
    "watchdog.observers",
    "watchdog.events",
    "PIL.Image",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "websockets.asyncio.server",
    "websockets.sync.client",
]

excludes = [
    "tkinter", "pydoc", "doctest", "idlelib", "lib2to3",
    "matplotlib", "scipy", "pandas", "IPython", "pytest", "setuptools", "pip", "wheel", "sympy",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3", "torch",
]

a = Analysis(
    [str(ROOT / "build" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PremiereDeck",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed exes trigger antivirus false positives
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(_version_file),
)
