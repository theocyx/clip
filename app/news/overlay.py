"""Habillage dessiné par-dessus chaque image de la vidéo d'actu (style des comptes d'actu « carte + HUD ») :

- bandeau du haut : pastille de la marque + sujet, date, onglets des chapitres (celui en cours en blanc),
  ligne « SOURCES » qui défile ;
- titre de la phrase en cours dans une boîte colorée légèrement inclinée, qui apparaît d'un coup de zoom ;
- sous-titres dans une boîte sombre : mots déjà dits en blanc, mot prononcé sur fond rouge, la suite en gris.

Tout est dessiné avec Pillow une fois (images mises en cache) puis collé image par image avec numpy."""
from __future__ import annotations

import functools
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)
W, H = 1080, 1920
RED = (230, 32, 38)
BLUE = (40, 110, 235)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GREY = (150, 150, 150)
DAYS = ["LUN.", "MAR.", "MER.", "JEU.", "VEN.", "SAM.", "DIM."]
MONTHS = ["JANV.", "FÉVR.", "MARS", "AVR.", "MAI", "JUIN", "JUIL.", "AOÛT", "SEPT.", "OCT.", "NOV.", "DÉC."]

HUD_TOP = 70
TITLE_Y = 330  # centre du titre (sous le bandeau, au-dessus du panneau / de la carte)
SUB_Y = int(H * 0.685)  # centre des sous-titres


def date_fr(d: datetime | None = None) -> str:
    d = d or datetime.now()
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


@functools.lru_cache(maxsize=64)
def font(kind: str, size: int):
    """kind : title (très gras), sub (gras lisible), mono (bandeau)."""
    from PIL import ImageFont

    from ..pipeline.hook import _font_file, _text_font

    if kind == "title":
        return _text_font(get_settings().font_name, size)
    queries = {"sub": ["Inter:bold", "Inter:semibold", "Montserrat:bold", "DejaVu Sans:bold"],
               "mono": ["DejaVu Sans Mono:bold", "DejaVu Sans Mono", "monospace:bold"]}[kind]
    for q in queries:
        path = _font_file(q)
        if path and (q.split(":")[0].split()[0].lower() in path.lower() or q.startswith("monospace")):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _rgba(img) -> np.ndarray:
    return np.asarray(img.convert("RGBA")).copy()


def paste(frame: np.ndarray, sprite: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    """Colle une image RGBA (coin haut-gauche en x, y), en ne touchant que sa zone."""
    h, w = sprite.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    if x1 <= x0 or y1 <= y0 or opacity <= 0:
        return
    sp = sprite[y0 - y:y1 - y, x0 - x:x1 - x]
    a = sp[..., 3:4].astype(np.float32) * (opacity / 255.0)
    region = frame[y0:y1, x0:x1]
    region[:] = (sp[..., :3] * a + region * (1 - a)).astype(np.uint8)


def box_text(text: str, kind: str, size: int, fg=WHITE, bg=RED, pad=(14, 6), radius: int = 4):
    from PIL import Image, ImageDraw

    f = font(kind, size)
    d = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    w, h = r - l + 2 * pad[0], b - t + 2 * pad[1]
    img = Image.new("RGBA", (max(2, w), max(2, h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    if bg is not None:
        dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=tuple(bg) + (255,) if len(bg) == 3 else bg)
    dr.text((pad[0] - l, pad[1] - t), text, font=f, fill=tuple(fg) + (255,))
    return img


# ---------- bandeau du haut ----------

@dataclass
class Hud:
    brand: str
    topic: str
    date: str
    tabs: list[str]
    sources: str
    cache: dict = field(default_factory=dict)

    def static(self):
        """Pastille + sujet + date (ne change pas)."""
        if "static" in self.cache:
            return self.cache["static"]
        from PIL import Image

        img = Image.new("RGBA", (W, 60), (0, 0, 0, 0))
        b = box_text(self.brand.upper(), "title", 28, WHITE, RED, pad=(12, 7))
        img.alpha_composite(b, (30, 8))
        x = 30 + b.width + 14
        if self.topic:
            t = box_text("• " + self.topic.upper()[:28], "title", 22, WHITE, None, pad=(0, 0))
            img.alpha_composite(t, (x, 8 + (b.height - t.height) // 2))
        d = box_text(self.date, "mono", 22, (220, 220, 220), (20, 20, 24, 230), pad=(12, 8))
        img.alpha_composite(d, (W - 30 - d.width, 8 + (b.height - d.height) // 2))
        self.cache["static"] = _rgba(img)
        return self.cache["static"]

    def tabs_img(self, active: int):
        key = ("tabs", active)
        if key in self.cache:
            return self.cache[key]
        from PIL import Image, ImageDraw

        n = max(1, len(self.tabs))
        img = Image.new("RGBA", (W, 40), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        gap, x0 = 6, 30
        tw = (W - 60 - gap * (n - 1)) / n
        f = font("mono", 19)
        for k, label in enumerate(self.tabs or [""]):
            x = x0 + k * (tw + gap)
            on = k == active
            dr.rectangle((x, 4, x + tw, 36), fill=(255, 255, 255, 255) if on else (18, 18, 22, 210),
                         outline=None if on else (60, 60, 66, 255))
            txt = label.upper()[:16]
            l, t, r, b = dr.textbbox((0, 0), txt, font=f)
            dr.text((x + (tw - (r - l)) / 2 - l, 20 - (b - t) / 2 - t), txt, font=f,
                    fill=(0, 0, 0, 255) if on else (120, 120, 128, 255))
        self.cache[key] = _rgba(img)
        return self.cache[key]

    def ticker(self):
        if "ticker" in self.cache:
            return self.cache["ticker"]
        text = (self.sources + "   ·   ") * 3
        t = box_text(text, "mono", 18, (190, 190, 196), None, pad=(0, 4))
        self.cache["ticker"] = _rgba(t)
        self.cache["label"] = _rgba(box_text("SOURCES", "mono", 17, BLACK, WHITE, pad=(8, 4), radius=2))
        return self.cache["ticker"]

    def draw(self, frame: np.ndarray, t: float, active: int, appear: float = 1.0) -> None:
        # assombrit le haut pour que le bandeau se lise sur n'importe quelle image
        top = frame[:300]
        grad = np.linspace(0.62, 0, 300, dtype=np.float32)[:, None, None]
        top[:] = (top * (1 - grad)).astype(np.uint8)
        paste(frame, self.static(), 0, HUD_TOP, appear)
        if t < 0.25:
            return
        paste(frame, self.tabs_img(active), 0, HUD_TOP + 58, clamp((t - 0.25) / 0.2))
        tick = self.ticker()
        label = self.cache["label"]
        y = HUD_TOP + 104
        x0 = 30 + label.shape[1] + 10
        width = W - 30 - x0
        span = max(1, tick.shape[1] // 3)
        off = int((t * 60) % span)
        strip = tick[:, off:off + width]
        o = clamp((t - 0.45) / 0.2)
        paste(frame, strip, x0, y + (label.shape[0] - strip.shape[0]) // 2, o)
        paste(frame, label, 30, y, o)


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def ease_out_back(p: float, k: float = 1.70158) -> float:
    p = clamp(p) - 1
    return 1 + (k + 1) * p ** 3 + k * p ** 2


# ---------- titres ----------

TITLE_STYLES = [(WHITE, RED), (BLACK, WHITE), (WHITE, BLUE)]


@functools.lru_cache(maxsize=64)
def title_sprite(text: str, style: int, angle: float) -> np.ndarray:
    """Titre en majuscules dans une boîte de couleur, légèrement incliné."""
    from PIL import Image

    fg, bg = TITLE_STYLES[style % len(TITLE_STYLES)]
    size = 58
    img = box_text(text.upper(), "title", size, fg, bg, pad=(18, 8), radius=3)
    while img.width > W - 70 and size > 34:
        size -= 4
        img = box_text(text.upper(), "title", size, fg, bg, pad=(18, 8), radius=3)
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    return _rgba(img)


def draw_title(frame: np.ndarray, text: str, style: int, angle: float, since: float) -> None:
    import cv2

    if not text or since < 0:
        return
    sp = title_sprite(text, style, angle)
    s = 0.6 + 0.4 * ease_out_back(since / 0.28)
    if s != 1.0:
        sp = cv2.resize(sp, (max(2, int(sp.shape[1] * s)), max(2, int(sp.shape[0] * s))),
                        interpolation=cv2.INTER_LINEAR)
    paste(frame, sp, (W - sp.shape[1]) // 2, TITLE_Y - sp.shape[0] // 2, clamp(since / 0.12))


# ---------- sous-titres ----------

def sub_groups(words: list[dict], max_chars: int = 26, max_words: int = 5) -> list[list[dict]]:
    """Groupes de mots affichés ensemble (coupés aux fins de phrase et aux pauses)."""
    groups, cur = [], []
    for w in words:
        if cur:
            length = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            if length > max_chars or len(cur) >= max_words or cur[-1]["word"].endswith((".", "!", "?")) \
                    or w["start"] - cur[-1]["end"] > 0.5:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


@functools.lru_cache(maxsize=512)
def sub_sprite(words: tuple[str, ...], active: int) -> np.ndarray:
    """Boîte sombre : mots déjà dits en blanc, mot prononcé en blanc sur fond rouge, la suite en gris."""
    from PIL import Image, ImageDraw

    f = font("sub", 50)
    d = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    space = d.textlength(" ", font=f) + 4
    sizes = [d.textbbox((0, 0), w, font=f) for w in words]
    widths = [b[2] - b[0] for b in sizes]
    asc, desc = f.getmetrics()
    line_h = asc + desc + 14
    # deux lignes au besoin
    lines, cur, cur_w = [], [], 0.0
    for k, w in enumerate(widths):
        if cur and cur_w + space + w > W - 160:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur.append(k)
        cur_w += (space if len(cur) > 1 else 0) + w
    lines.append(cur)
    line_w = [sum(widths[k] for k in ln) + space * (len(ln) - 1) for ln in lines]
    pad_x, pad_y = 26, 14
    bw, bh = int(max(line_w) + 2 * pad_x), int(line_h * len(lines) + 2 * pad_y)
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=10, fill=(12, 12, 14, 205))
    for li, ln in enumerate(lines):
        x = (bw - line_w[li]) / 2
        y = pad_y + li * line_h + 7
        for k in ln:
            l, t, r, b = sizes[k]
            if k == active:
                dr.rounded_rectangle((x - 8, y - 4, x + widths[k] + 8, y + asc + desc - 2), radius=6,
                                     fill=RED + (255,))
            color = WHITE if k <= active else GREY
            dr.text((x - l, y), words[k], font=f, fill=color + (255,))
            x += widths[k] + space
    return _rgba(img)


def draw_subs(frame: np.ndarray, groups: list[list[dict]], t: float) -> None:
    for gi, g in enumerate(groups):
        end = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else g[-1]["end"] + 0.6
        end = min(end, g[-1]["end"] + 0.6)
        if g[0]["start"] - 0.05 <= t < end:
            active = 0
            for k, w in enumerate(g):
                if t >= w["start"]:
                    active = k
            sp = sub_sprite(tuple(w["word"] for w in g), active)
            paste(frame, sp, (W - sp.shape[1]) // 2, SUB_Y - sp.shape[0] // 2)
            return


# ---------- tout l'habillage ----------

@dataclass
class Overlay:
    hud: Hud
    sentences: list[tuple[float, float]]  # (début, fin) de chaque phrase
    titles: list[str]
    chapter_of: list[int]  # n° d'onglet de chaque phrase
    words: list[dict]
    groups: list[list[dict]] = field(default_factory=list)

    def __post_init__(self):
        self.groups = sub_groups(self.words)

    def sentence_at(self, t: float) -> int:
        k = 0
        for i, (a, _) in enumerate(self.sentences):
            if t >= a - 0.05:
                k = i
        return k

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        i = self.sentence_at(t)
        self.hud.draw(frame, t, self.chapter_of[i] if i < len(self.chapter_of) else 0,
                      appear=clamp(t / 0.25))
        if i < len(self.titles):
            angle = (0.0, -2.0, 1.5, -1.5, 2.0)[i % 5] if i else 0.0
            draw_title(frame, self.titles[i], i % 3, angle, t - self.sentences[i][0])
        draw_subs(frame, self.groups, t)
        return frame


def chapters(labels: list[str], max_tabs: int = 5) -> tuple[list[str], list[int]]:
    """Onglets (dans l'ordre d'apparition, 5 max) et n° d'onglet de chaque phrase."""
    tabs: list[str] = []
    of = []
    for lab in labels:
        lab = (lab or "").strip().upper()[:16]
        if lab and lab not in tabs and len(tabs) < max_tabs:
            tabs.append(lab)
        of.append(tabs.index(lab) if lab in tabs else (len(tabs) - 1 if tabs else 0))
    return tabs, of
