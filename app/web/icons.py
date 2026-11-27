"""Génère les icônes de l'appli (écran d'accueil iPhone / Android) dans app/web/static.

    python -m app.web.icons
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).parent / "static"
BG_TOP, BG_BOTTOM, ACC, DARK = (30, 35, 46), (15, 17, 21), (255, 210, 63), (15, 17, 21)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SIZES = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}


def draw(size: int = 1024) -> Image.Image:
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):  # dégradé vertical sombre
        t = y / (size - 1)
        d.line([(0, y), (size, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    # une carte verticale 9:16 (un Short), dans la zone sûre des icônes « maskable » (cercle de 80 %)
    w, h = size * 0.41, size * 0.64
    x0, y0 = (size - w) / 2, (size - h) / 2
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=size * 0.07, fill=ACC)
    # ciseaux sombres au centre de la carte
    glyph = Image.new("L", (size, size), 0)
    font = ImageFont.truetype(FONT, int(size * 0.5))
    ImageDraw.Draw(glyph).text((size / 2, size / 2), "✂", font=font, anchor="mm", fill=255)
    glyph = glyph.rotate(-90, resample=Image.BICUBIC)
    img.paste(Image.new("RGB", (size, size), DARK), mask=glyph)
    return img


def main() -> None:
    STATIC.mkdir(exist_ok=True)
    big = draw()
    for name, px in SIZES.items():
        big.resize((px, px), Image.LANCZOS).save(STATIC / name, optimize=True)


if __name__ == "__main__":
    main()
