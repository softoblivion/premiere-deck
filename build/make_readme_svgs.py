"""Icon tiles of the README: section headings, feature cards and the Why cards.

Writes small standalone SVG files under docs/assets/:

  sections/<key>.svg   64 x 64 tile shown at 32 px, floated left inside a `##` heading of README.md
  icons/<key>.svg      96 x 96 tile shown at 48 px (feature and Why cards)

Every tile has the look of the hero banner (docs/assets/hero.svg): the Crystal Black background gradient, two
blurred blobs in the theme's hue and accent, a glass fill with a light rim, and one glyph of the application's own
icon sprite (src/premiere_deck/ui/assets/icons.svg, then src/premiere_deck/panel/icons.svg) drawn in white with a
soft accent glow. Colours come from src/premiere_deck/ui/themes.json (theme "black"). A tile carries no text, so
it renders the same on GitHub's light and dark pages and needs no font.

The output is deterministic (no dates, fixed number formatting): running the script twice gives the same bytes.

Usage (from the repository root, standard library only):
  py -3 build\\make_readme_svgs.py           write every file
  py -3 build\\make_readme_svgs.py --check   compare only; exit 1 when a file is missing or differs

To add a tile, add a line to SECTIONS or ICONS (output key, glyph id of the sprite) and run the script.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPRITES = [
    ROOT / "src" / "premiere_deck" / "ui" / "assets" / "icons.svg",
    ROOT / "src" / "premiere_deck" / "panel" / "icons.svg",
]
THEMES = ROOT / "src" / "premiere_deck" / "ui" / "themes.json"
ASSETS = ROOT / "docs" / "assets"

# README `##` headings: output key -> glyph id.
SECTIONS = {
    "why": "i-info",
    "quick-start": "i-launch",
    "features": "i-activity",
    "how-it-works": "i-consolidate",
    "documentation": "i-journal",
    "building": "i-settings",
    "disclaimer": "i-warn",
    "license": "i-check",
}

# Feature cards and Why cards: output key -> glyph id.
ICONS = {
    "files": "i-sort",
    "quick-actions": "i-actions",
    "pinned-row": "i-pin",
    "themes": "i-palette",
    "self-update": "i-update",
    "portable": "i-folder",
    "routine": "i-ease-fast",
    "safe": "i-undo",
    "open-source": "i-open",
}


def theme_colours() -> dict:
    data = json.loads(THEMES.read_text("utf-8"))
    black = next(t for t in data["themes"] if t["name"] == "black")
    d = black["derived"]
    return {
        "top": d["--bg-top"],
        "mid": d["--bg-mid"],
        "bottom": d["--bg-bottom"],
        "hue": black["hue"],
        "accent": black["accent"],
    }


def load_glyphs() -> dict:
    """Glyph id -> (viewBox, attributes of the symbol, inner markup); the first sprite wins."""
    glyphs: dict = {}
    for sprite in SPRITES:
        text = sprite.read_text("utf-8")
        for m in re.finditer(r'<symbol id="(i-[^"]+)"([^>]*)>(.*?)</symbol>', text, re.S):
            gid, attrs, inner = m.group(1), m.group(2), m.group(3)
            if gid in glyphs:
                continue
            vb = re.search(r'viewBox="([^"]+)"', attrs)
            attrs = re.sub(r'\s*viewBox="[^"]+"', "", attrs).strip()
            glyphs[gid] = (vb.group(1) if vb else "0 0 24 24", attrs, inner.strip())
    return glyphs


def tile(size: int, glyph: tuple, c: dict, label: str) -> str:
    """One square tile of `size` user units with the glyph centred at half the size."""
    vb, attrs, inner = glyph
    attrs = attrs.replace("currentColor", "#ffffff")
    inner = re.sub(r"\s+", " ", inner.replace("currentColor", "#ffffff"))
    s = size
    r = round(s * 0.24)
    g = s / 2  # glyph box
    off = (s - g) / 2
    blur = round(s * 0.16, 1)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 {s} {s}" role="img" aria-label="{label}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{c['top']}"/>
      <stop offset="0.55" stop-color="{c['mid']}"/>
      <stop offset="1" stop-color="{c['bottom']}"/>
    </linearGradient>
    <linearGradient id="glass" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.16"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0.03"/>
    </linearGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.7"/>
      <stop offset="0.45" stop-color="#ffffff" stop-opacity="0.1"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0.32"/>
    </linearGradient>
    <filter id="blur" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="{blur}"/>
    </filter>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="0" stdDeviation="0.7" flood-color="{c['accent']}" flood-opacity="0.9"/>
    </filter>
    <clipPath id="frame"><rect width="{s}" height="{s}" rx="{r}"/></clipPath>
  </defs>
  <g clip-path="url(#frame)">
    <rect width="{s}" height="{s}" fill="url(#bg)"/>
    <g filter="url(#blur)">
      <circle cx="{s * 0.86:.1f}" cy="{s * 0.08:.1f}" r="{s * 0.42:.1f}" fill="{c['hue']}" fill-opacity="0.85"/>
      <circle cx="{s * 0.12:.1f}" cy="{s * 0.98:.1f}" r="{s * 0.44:.1f}" fill="{c['accent']}" fill-opacity="0.8"/>
    </g>
    <rect width="{s}" height="{s}" fill="url(#glass)"/>
  </g>
  <rect x="0.75" y="0.75" width="{s - 1.5}" height="{s - 1.5}" rx="{r - 0.75}" fill="none" stroke="url(#rim)" stroke-width="1.5"/>
  <svg x="{off:.1f}" y="{off:.1f}" width="{g:.1f}" height="{g:.1f}" viewBox="{vb}" overflow="visible">
    <g {attrs} filter="url(#glow)">{inner}</g>
  </svg>
</svg>
'''


def build() -> dict:
    c = theme_colours()
    glyphs = load_glyphs()
    out: dict = {}
    for folder, table, size in (("sections", SECTIONS, 64), ("icons", ICONS, 96)):
        for key, gid in table.items():
            if gid not in glyphs:
                raise SystemExit(f"glyph {gid} not found in the sprites")
            out[ASSETS / folder / f"{key}.svg"] = tile(size, glyphs[gid], c, key.replace("-", " "))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare only; exit 1 on a missing or different file")
    a = ap.parse_args()
    files = build()
    stale = []
    for path, text in files.items():
        data = text.encode("utf-8")
        if a.check:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)
    rel = lambda p: p.relative_to(ROOT).as_posix()  # noqa: E731
    if a.check:
        for p in stale:
            print("differs:", rel(p))
        print("ok" if not stale else f"{len(stale)} of {len(files)} files differ")
        return 1 if stale else 0
    print(f"{len(files)} files in {rel(ASSETS / 'sections')} and {rel(ASSETS / 'icons')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
