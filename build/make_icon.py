"""Build build/icon.ico (multi-size) for the exe, the window and the tray.

    python build/make_icon.py

A rounded tile in the Nova theme (violet to ember) with the app mark: three stacked rounded cards, the front one
solid. Drawn at 1024 px and downscaled with LANCZOS.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_ICO = ROOT / "build" / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]
S = 1024

TOP_LEFT = (106, 61, 240)      # Nova hue #6a3df0
BOTTOM_RIGHT = (255, 138, 74)  # Nova accent #ff8a4a


def gradient(size: int, a: tuple, b: tuple) -> Image.Image:
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = min(1.0, max(0.0, (x * 0.35 + y * 0.65) / 63))
            t = t ** 1.6
            px[x, y] = tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
    return small.resize((size, size), Image.BICUBIC)


def rounded(box: tuple, radius: int) -> Image.Image:
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def draw() -> Image.Image:
    tile = rounded((40, 40, S - 40, S - 40), 230)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(gradient(S, TOP_LEFT, BOTTOM_RIGHT), (0, 0), tile)
    marks = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_ = ImageDraw.Draw(marks)
    white = (255, 255, 255)
    cards = [((330, 250, 790, 560), 90), ((280, 330, 740, 640), 140), ((230, 410, 690, 780), 255)]
    for (box, alpha) in cards:
        if alpha < 255:
            draw_.rounded_rectangle(box, radius=64, outline=white + (alpha,), width=34)
        else:
            draw_.rounded_rectangle(box, radius=64, fill=white + (alpha,))
    glow = marks.filter(ImageFilter.GaussianBlur(18))
    base = Image.alpha_composite(base, Image.eval(glow, lambda v: v // 3))
    base = Image.alpha_composite(base, marks)
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle((44, 44, S - 44, S - 44), radius=226, outline=(255, 255, 255, 70), width=6)
    return Image.alpha_composite(base, rim)


def main() -> int:
    image = draw()
    images = [image.resize((size, size), Image.LANCZOS) for size in SIZES]
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(OUT_ICO, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print(f"wrote {OUT_ICO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
