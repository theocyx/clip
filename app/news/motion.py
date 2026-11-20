"""Habillage animé des vidéos d'actu (motion design), calculé image par image avec numpy + OpenCV :

- photo / vidéo libre dans un panneau sombre sur fond quadrillé, avec un lent zoom ;
- mots forts qui basculent en 3D un par un dans le panneau ;
- chiffre clé qui défile de 0 à sa valeur, avec une barre qui se remplit ;
- quiz final « ET TOI ? » avec réponses A / B ;
- survol « vu d'en haut » : zoom depuis l'espace jusqu'au lieu de l'actu sur des images satellite libres,
  la caméra s'incline, la zone est surlignée, épingle + étiquette, boussole, coordonnées et échelle.

Chaque plan est envoyé à ffmpeg en images brutes (pas de fichier intermédiaire par image)."""
from __future__ import annotations

import functools
import logging
import math
import re
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)
W, H = 1080, 1920
FPS = 30
FOCAL = 1800.0


# ---------- outils ----------

def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def ease_out_cubic(p: float) -> float:
    p = clamp(p)
    return 1 - (1 - p) ** 3


def ease_in_out(p: float) -> float:
    p = clamp(p)
    return 3 * p * p - 2 * p * p * p


def ease_out_back(p: float, k: float = 1.70158) -> float:
    p = clamp(p) - 1
    return 1 + (k + 1) * p ** 3 + k * p ** 2


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#FFD400").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return 255, 212, 0


def project(w: float, h: float, cx: float, cy: float, scale: float = 1.0, rx: float = 0.0, ry: float = 0.0,
            rz: float = 0.0, ox: float = 0.0, oy: float = 0.0, oz: float = 0.0) -> np.ndarray:
    """Coins à l'écran d'un rectangle w x h tourné en 3D (degrés) autour du pivot (cx, cy).
    (ox, oy, oz) : position du rectangle par rapport au pivot (avant rotation)."""
    pts = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0], [-w / 2, h / 2, 0]], float)
    pts = (pts + [ox, oy, oz]) * scale
    a, b, c = (math.radians(v) for v in (rx, ry, rz))
    rot_x = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    rot_y = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    rot_z = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    p = pts @ (rot_z @ rot_y @ rot_x).T
    k = FOCAL / np.maximum(1.0, FOCAL + p[:, 2])
    return np.float32(np.stack([cx + p[:, 0] * k, cy + p[:, 1] * k], 1))


def warp_onto(canvas: np.ndarray, sprite: np.ndarray, dst: np.ndarray, opacity: float = 1.0) -> None:
    """Pose une image RGBA déformée (4 coins `dst`) sur le canevas RGB, seulement dans sa zone (rapide)."""
    import cv2

    hh, ww = sprite.shape[:2]
    x0, y0 = int(max(0, math.floor(dst[:, 0].min()))), int(max(0, math.floor(dst[:, 1].min())))
    x1 = int(min(canvas.shape[1], math.ceil(dst[:, 0].max())))
    y1 = int(min(canvas.shape[0], math.ceil(dst[:, 1].max())))
    if x1 - x0 < 2 or y1 - y0 < 2 or opacity <= 0:
        return
    src = np.float32([[0, 0], [ww, 0], [ww, hh], [0, hh]])
    m = cv2.getPerspectiveTransform(src, np.float32(dst - [x0, y0]))
    out = cv2.warpPerspective(sprite, m, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    a = out[..., 3:4].astype(np.float32) * (opacity / 255.0)
    region = canvas[y0:y1, x0:x1]
    region[:] = (out[..., :3] * a + region * (1 - a)).astype(np.uint8)


def drop_shadow(canvas: np.ndarray, poly: np.ndarray, dy: float = 40, strength: float = 0.6) -> None:
    """Ombre douce sous une carte (calculée en quart de résolution)."""
    import cv2

    q = 4
    mask = np.zeros((canvas.shape[0] // q, canvas.shape[1] // q), np.float32)
    cv2.fillConvexPoly(mask, np.int32((poly + [0, dy]) / q), 1.0, lineType=cv2.LINE_AA)
    mask = cv2.GaussianBlur(mask, (0, 0), 9)
    mask = cv2.resize(mask, (canvas.shape[1], canvas.shape[0]), interpolation=cv2.INTER_LINEAR)
    canvas[:] = (canvas * (1 - strength * mask[..., None])).astype(np.uint8)


@functools.lru_cache(maxsize=16)
def _font(size: int):
    from ..pipeline.hook import _text_font

    return _text_font(get_settings().font_name, size)


def text_sprite(text: str, size: int, color=(255, 255, 255), stroke: int = 8, box: tuple | None = None,
                pad: int = 26) -> np.ndarray:
    """Texte en RGBA (contour noir, ou dans une boîte de couleur si `box`)."""
    from PIL import Image, ImageDraw

    font = _font(size)
    d = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    l, t, r, b = d.textbbox((0, 0), text, font=font, stroke_width=0 if box else stroke)
    w, h = r - l + (2 * pad if box else 2 * stroke), b - t + (2 * pad if box else 2 * stroke)
    img = Image.new("RGBA", (max(2, w), max(2, h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    if box:
        dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(28, h // 3), fill=tuple(box) + (255,))
        dr.text((pad - l, pad - t), text, font=font, fill=tuple(color) + (255,))
    else:
        dr.text((stroke - l, stroke - t), text, font=font, fill=tuple(color) + (255,), stroke_width=stroke,
                stroke_fill=(0, 0, 0, 255))
    return np.asarray(img).copy()


def rounded_card(img: np.ndarray, radius: int = 40, border: int = 0) -> np.ndarray:
    """Image RGB → RGBA aux coins arrondis (bordure blanche optionnelle)."""
    from PIL import Image, ImageDraw

    h, w = img.shape[:2]
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    out = np.dstack([img, np.asarray(mask)])
    if border:
        frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, outline=(255, 255, 255, 255),
                                                width=border)
        fr = np.asarray(frame).astype(np.float32)
        a = fr[..., 3:4] / 255
        out[..., :3] = (fr[..., :3] * a + out[..., :3] * (1 - a)).astype(np.uint8)
    return out


def write_frames(frame: Callable[[int], np.ndarray], n: int, out: Path) -> Path:
    """Encode `n` images (fonction n° → tableau H x W x 3 RGB) en MP4 sans son."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            proc.stdin.write(np.ascontiguousarray(frame(i), dtype=np.uint8).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg (animation) : {err[-400:]}")
    return out


def load_rgb(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps

    return np.asarray(ImageOps.exif_transpose(Image.open(path)).convert("RGB"))


def cover(img: np.ndarray, w: int, h: int) -> np.ndarray:
    import cv2

    ih, iw = img.shape[:2]
    k = max(w / iw, h / ih)
    r = cv2.resize(img, (max(w, round(iw * k)), max(h, round(ih * k))), interpolation=cv2.INTER_AREA)
    y, x = (r.shape[0] - h) // 2, (r.shape[1] - w) // 2
    return r[y:y + h, x:x + w]


# ---------- fond quadrillé et panneaux ----------

PANEL = (50, 520, 1030, 1080)  # x0, y0, x1, y1 du panneau central (le titre est au-dessus, les sous-titres dessous)


@functools.lru_cache(maxsize=4)
def grid_base(seed: int = 0) -> np.ndarray:
    """Fond presque noir quadrillé, avec une lueur rouge sur les bords."""
    import cv2

    f = np.full((H, W, 3), 11, np.uint8)
    f[:, :, 2] = 13
    step = 54
    f[::step, :] = (26, 26, 30)
    f[:, ::step] = (26, 26, 30)
    q = 8
    glow = np.zeros((H // q, W // q), np.float32)
    cv2.circle(glow, (0, H // q), W // q // 2, 1.0, -1)
    cv2.circle(glow, (W // q, 0), W // q // 3, 0.7, -1)
    glow = cv2.resize(cv2.GaussianBlur(glow, (0, 0), 30), (W, H))[..., None]
    red = np.array([150, 18, 22], np.float32)
    return np.clip(f * (1 - 0.6 * glow) + red * 0.6 * glow, 0, 255).astype(np.uint8)


@functools.lru_cache(maxsize=4)
def panel_sprite(w: int, h: int) -> np.ndarray:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=(15, 15, 19, 250),
                                          outline=(58, 58, 66, 255), width=2)
    return np.asarray(img).copy()


def paste_rgba(frame: np.ndarray, sprite: np.ndarray, x: int, y: int, opacity: float = 1.0) -> None:
    from .overlay import paste

    paste(frame, sprite, x, y, opacity)


def panel_frame(t: float) -> tuple[np.ndarray, float]:
    """Fond + panneau vide qui apparaît (léger zoom) ; renvoie aussi l'opacité d'entrée du contenu."""
    import cv2

    f = grid_base().copy()
    x0, y0, x1, y1 = PANEL
    sp = panel_sprite(x1 - x0, y1 - y0)
    e = ease_out_cubic(t / 0.25)
    if e < 1:
        s = 0.94 + 0.06 * e
        sp = cv2.resize(sp, (int(sp.shape[1] * s), int(sp.shape[0] * s)))
    paste_rgba(f, sp, (W - sp.shape[1]) // 2, (y0 + y1) // 2 - sp.shape[0] // 2, e)
    return f, e


def caption(text: str) -> np.ndarray:
    return text_sprite(text.upper(), 18, (150, 150, 158), stroke=0)


def panel_photo(path: Path, n: int, idx: int, label: str = "") -> Callable[[int], np.ndarray]:
    """Photo dans le panneau, avec un lent zoom / glissement (« Ken Burns »)."""
    import cv2

    x0, y0, x1, y1 = PANEL
    iw, ih = x1 - x0 - 28, y1 - y0 - 28
    big = cover(load_rgb(path), int(iw * 1.12), int(ih * 1.12))
    mask = rounded_card(np.zeros((ih, iw, 3), np.uint8), radius=12)[..., 3:4].astype(np.float32) / 255
    cap = caption(label) if label else None
    side = 1 if idx % 2 == 0 else -1

    def frame(i: int) -> np.ndarray:
        t, p = i / FPS, i / max(1, n - 1)
        f, e = panel_frame(t)
        z = 1.0 + 0.1 * p
        cw, ch = int(big.shape[1] / z * 0.9), int(big.shape[0] / z * 0.9)
        cx = (big.shape[1] - cw) / 2 * (1 + side * 0.6 * (p - 0.5))
        cy = (big.shape[0] - ch) / 2
        crop = cv2.resize(big[int(cy):int(cy) + ch, int(cx):int(cx) + cw], (iw, ih), interpolation=cv2.INTER_LINEAR)
        region = f[y0 + 14:y0 + 14 + ih, x0 + 14:x0 + 14 + iw]
        a = mask * e
        region[:] = (crop * a + region * (1 - a)).astype(np.uint8)
        if cap is not None:
            paste_rgba(f, cap, x1 - 24 - cap.shape[1], y1 + 10, e)
        return f

    return frame


def panel_video(path: Path, n: int, idx: int, label: str = "", offset: float = 0.3) -> Callable[[int], np.ndarray]:
    """Vidéo libre de droits jouée dans le panneau (décodée par ffmpeg à la taille du panneau)."""
    x0, y0, x1, y1 = PANEL
    iw, ih = x1 - x0 - 28, y1 - y0 - 28
    proc = subprocess.run(["ffmpeg", "-v", "error", "-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(path),
                           "-vf", f"scale={iw}:{ih}:force_original_aspect_ratio=increase,crop={iw}:{ih},fps={FPS}",
                           "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=300)
    size = iw * ih * 3
    count = len(proc.stdout) // size
    if count == 0:
        raise RuntimeError("vidéo illisible : " + proc.stderr.decode(errors="ignore")[-200:])
    clip = np.frombuffer(proc.stdout[:count * size], np.uint8).reshape(count, ih, iw, 3)
    mask = rounded_card(np.zeros((ih, iw, 3), np.uint8), radius=12)[..., 3:4].astype(np.float32) / 255
    cap = caption(label) if label else None

    def frame(i: int) -> np.ndarray:
        f, e = panel_frame(i / FPS)
        region = f[y0 + 14:y0 + 14 + ih, x0 + 14:x0 + 14 + iw]
        a = mask * e
        region[:] = (clip[min(i, count - 1)] * a + region * (1 - a)).astype(np.uint8)
        if cap is not None:
            paste_rgba(f, cap, x1 - 24 - cap.shape[1], y1 + 10, e)
        return f

    return frame


def split_words(text: str, per_line: int = 12, max_lines: int = 3) -> list[list[str]]:
    words, lines, cur = text.upper().split(), [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > per_line:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def kinetic(text: str, n: int, idx: int, accent: tuple[int, int, int]) -> Callable[[int], np.ndarray]:
    """Mots forts dans le panneau, qui basculent en 3D un par un puis flottent légèrement."""
    lines = split_words(text) or [["INFO"]]
    size = 130
    while size > 70 and max(sum(text_sprite(w, size, stroke=0).shape[1] for w in ln) + 26 * (len(ln) - 1)
                            for ln in lines) > PANEL[2] - PANEL[0] - 90:
        size -= 10
    sprites, k = [], 0
    line_h = size * 1.2
    y0 = -line_h * (len(lines) - 1) / 2
    for li, ln in enumerate(lines):
        sp = [text_sprite(w, size, (230, 32, 38) if (li + j) % 2 else (255, 255, 255), stroke=0)
              for j, w in enumerate(ln)]
        total = sum(s.shape[1] for s in sp) + 26 * (len(sp) - 1)
        x = -total / 2
        for s in sp:
            sprites.append((s, x + s.shape[1] / 2, y0 + li * line_h, k))
            x += s.shape[1] + 26
            k += 1
    cx, cy = W / 2, (PANEL[1] + PANEL[3]) / 2
    phase = idx * 1.3

    def frame(i: int) -> np.ndarray:
        t = i / FPS
        f, _ = panel_frame(t)
        ry = 6 * math.sin(phase + t * 1.4)
        for s, ox, oy, order in sprites:
            p = (t - 0.15 - 0.08 * order) / 0.3
            if p <= 0:
                continue
            e = ease_out_back(p)
            poly = project(s.shape[1], s.shape[0], cx, cy, 0.75 + 0.25 * e, rx=85 * (1 - ease_out_cubic(p)),
                           ry=ry, ox=ox, oy=oy)
            warp_onto(f, s, poly, opacity=clamp(p * 2))
        return f

    return frame


def quiz(question: str, answers: list[str], n: int, accent: tuple[int, int, int]) -> Callable[[int], np.ndarray]:
    """Fin interactive : « ET TOI ? », la question dans une boîte rouge, puis les réponses A / B."""
    from PIL import Image, ImageDraw

    from .overlay import box_text, font

    head = np.asarray(box_text("ET TOI ?", "title", 78, (255, 255, 255), None, pad=(0, 0)).convert("RGBA")).copy()
    import re as _re

    question = _re.sub(r"^\s*(et\s+)?(toi|vous)\s*,\s*", "", question.strip(), flags=_re.I)
    lines, cur = [], ""
    for w in question.upper().replace(" ?", "\x00?").split():  # le « ? » reste collé au dernier mot
        w = w.replace("\x00", " ")
        if cur and len(cur) + 1 + len(w) > 22:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines = (lines + [cur])[:3]
    q_imgs = [box_text(ln, "title", 52, (255, 255, 255), (230, 32, 38), pad=(16, 8), radius=2) for ln in lines]
    q_list = [np.asarray((im if im.width <= W - 120 else im.resize((W - 120, round(im.height * (W - 120) / im.width))))
                         .convert("RGBA")).copy() for im in q_imgs]
    q = np.zeros((sum(x.shape[0] for x in q_list) + 6 * (len(q_list) - 1), max(x.shape[1] for x in q_list), 4),
                 np.uint8)
    yy = 0
    for x in q_list:
        q[yy:yy + x.shape[0], :x.shape[1]] = x
        yy += x.shape[0] + 6
    rows = []
    for letter, ans in zip("AB", (answers or ["Oui", "Non"])[:2]):
        img = Image.new("RGBA", (W - 120, 96), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.rectangle((0, 0, W - 121, 95), fill=(16, 16, 20, 245), outline=(70, 70, 78, 255), width=2)
        dr.rectangle((16, 16, 80, 80), fill=(255, 255, 255, 255))
        f = font("title", 40)
        l, t, r, b = dr.textbbox((0, 0), letter, font=f)
        dr.text((48 - (r - l) / 2 - l, 48 - (b - t) / 2 - t), letter, font=f, fill=(0, 0, 0, 255))
        fs = font("sub", 40)
        l, t, r, b = dr.textbbox((0, 0), ans[:34], font=fs)
        dr.text((104 - l, 48 - (b - t) / 2 - t), ans[:34], font=fs, fill=(255, 255, 255, 255))
        rows.append(np.asarray(img).copy())

    def frame(i: int) -> np.ndarray:
        t = i / FPS
        f = grid_base().copy()
        y = 520
        paste_rgba(f, head, 60, y, clamp(t / 0.2))
        y += head.shape[0] + 20
        paste_rgba(f, q, 60, y, clamp((t - 0.2) / 0.2))
        y += q.shape[0] + 30
        for k, r in enumerate(rows):
            p = clamp((t - 0.45 - 0.2 * k) / 0.2)
            paste_rgba(f, r, 60 + int((1 - ease_out_cubic(p)) * 80), y, p)
            y += r.shape[0] + 18
        return f

    return frame


_NUMBER = re.compile(r"(\d+(?:[  .]\d{3})*(?:,\d+)?)")


def parse_number(text: str) -> tuple[float, int, str] | None:
    """« 25,5 » → (25.5, 1 décimale, « 25,5 »)."""
    m = _NUMBER.search(text or "")
    if not m:
        return None
    raw = m.group(1)
    clean = re.sub(r"[  .]", "", raw).replace(",", ".")
    try:
        val = float(clean)
    except ValueError:
        return None
    dec = len(clean.split(".")[1]) if "." in clean else 0
    return val, dec, raw


def fmt_number(v: float, dec: int) -> str:
    s = f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")
    return s


def counter(value_text: str, unit: str, n: int, idx: int, accent: tuple[int, int, int]) -> Callable[[int], np.ndarray]:
    """Chiffre clé dans le panneau : il défile de 0 à sa valeur, une barre rouge se remplit, l'unité arrive."""
    import cv2

    parsed = parse_number(value_text)
    val, dec = (parsed[0], parsed[1]) if parsed else (0.0, 0)
    x0, y0, x1, y1 = PANEL
    cx, cy = W // 2, y0 + int((y1 - y0) * 0.4)
    unit_sp = text_sprite(unit.upper()[:24], 44, (230, 230, 236), stroke=0) if unit else None
    final = fmt_number(val, dec)
    size = 170
    while size > 80 and text_sprite(final, size, stroke=0).shape[1] > x1 - x0 - 120:
        size -= 10
    cache: dict[str, np.ndarray] = {}
    bar_y = y0 + int((y1 - y0) * 0.66)

    def frame(i: int) -> np.ndarray:
        t = i / FPS
        f, e = panel_frame(t)
        p = ease_out_cubic((t - 0.15) / 0.9)
        txt = fmt_number(val * p, dec) if p < 1 else final
        sp = cache.get(txt)
        if sp is None:
            sp = cache[txt] = text_sprite(txt, size, (255, 255, 255), stroke=0)
        pop = 1 + 0.07 * math.sin(math.pi * clamp((t - 1.05) / 0.25))
        warp_onto(f, sp, project(sp.shape[1], sp.shape[0], cx, cy, pop), opacity=e)
        cv2.rectangle(f, (x0 + 60, bar_y), (x1 - 60, bar_y + 26), (40, 40, 46), -1)
        cv2.rectangle(f, (x0 + 60, bar_y), (x0 + 60 + int((x1 - x0 - 120) * p), bar_y + 26), (38, 32, 230)[::-1], -1)
        if unit_sp is not None:
            q = (t - 0.5) / 0.3
            if q > 0:
                paste_rgba(f, unit_sp, cx - unit_sp.shape[1] // 2, bar_y + 50, clamp(q))
        return f

    return frame


# ---------- survol « vu d'en haut » ----------

def lonlat_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Position en pixels (tuiles 256 px, projection Web Mercator) au niveau de zoom z."""
    lat = clamp(lat, -85.0, 85.0)
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class MapView:
    """Assemble les tuiles satellite d'une zone (tuiles fournies par `fetch(z, x, y)` → RGB 256x256 ou None)."""

    def __init__(self, fetch: Callable[[int, int, int], np.ndarray | None]):
        self.fetch = fetch
        self.cache: dict[tuple[int, int, int], np.ndarray | None] = {}
        self.missing = 0
        self.asked = 0

    def tiles_for(self, z: int, cx: float, cy: float, vw: float, vh: float) -> list[tuple[int, int, int]]:
        n = 2 ** z
        out = []
        for ty in range(int(math.floor((cy - vh / 2) / 256)), int(math.floor((cy + vh / 2) / 256)) + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(int(math.floor((cx - vw / 2) / 256)), int(math.floor((cx + vw / 2) / 256)) + 1):
                out.append((z, tx % n, ty))
        return out

    def tile(self, key: tuple[int, int, int]) -> np.ndarray | None:
        if key not in self.cache:
            self.asked += 1
            try:
                self.cache[key] = self.fetch(*key)
            except Exception:
                self.cache[key] = None
            if self.cache[key] is None:
                self.missing += 1
        return self.cache[key]

    def region(self, z: int, cx: float, cy: float, vw: float, vh: float, out_w: int, out_h: int) -> np.ndarray:
        import cv2

        x0, y0 = cx - vw / 2, cy - vh / 2
        tx0, ty0 = int(math.floor(x0 / 256)), int(math.floor(y0 / 256))
        tx1, ty1 = int(math.floor((x0 + vw) / 256)), int(math.floor((y0 + vh) / 256))
        mosaic = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256, 3), np.uint8)
        n = 2 ** z
        for ty in range(ty0, ty1 + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(tx0, tx1 + 1):
                t = self.tile((z, tx % n, ty))
                if t is not None:
                    mosaic[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t[:256, :256]
        ox, oy = x0 - tx0 * 256, y0 - ty0 * 256
        m = np.float32([[out_w / vw, 0, -ox * out_w / vw], [0, out_h / vh, -oy * out_h / vh]])
        return cv2.warpAffine(mosaic, m, (out_w, out_h), flags=cv2.INTER_LINEAR)


RW, RH = 432, 768  # résolution de calcul de la carte (agrandie ensuite) : moins de tuiles, plus rapide
SPREAD = 1.8  # largeur calculée en plus pour pouvoir incliner la caméra


MAX_TILT = 25.0


def map_plan(lon: float, lat: float, n: int, z0: float = 3.0, z1: float = 13.0) -> list[tuple[float, float]]:
    """(zoom, inclinaison en degrés) de chaque image : descente depuis l'espace puis la caméra se penche un peu."""
    out = []
    for i in range(n):
        p = i / max(1, n - 1)
        z = z0 + (z1 - z0) * ease_in_out(p / 0.72) + 0.35 * clamp((p - 0.72) / 0.28)
        out.append((z, MAX_TILT * ease_in_out((p - 0.35) / 0.6)))
    return out


def map_tiles_needed(lon: float, lat: float, n: int, z1: float) -> set[tuple[int, int, int]]:
    view = MapView(lambda *_: None)
    need: set[tuple[int, int, int]] = set()
    for z, _ in map_plan(lon, lat, n, z1=z1):
        lvl = int(round(z))
        s = 2 ** (z - lvl)
        cx, cy = lonlat_px(lon, lat, lvl)
        need.update(view.tiles_for(lvl, cx, cy, RW * SPREAD / s, RH / s))
    return need


def dms(value: float, pos: str, neg: str) -> str:
    v = abs(value)
    d, m = int(v), int((v - int(v)) * 60)
    sec = int(round(((v - d) * 60 - m) * 60)) % 60
    return f"{d}°{m:02d}'{sec:02d}\"{pos if value >= 0 else neg}"


def nice_scale(m_per_px: float, max_px: int = 220) -> tuple[int, str]:
    """Longueur (px) et texte d'une barre d'échelle « ronde » (1, 2, 5 × 10^n)."""
    target = m_per_px * max_px
    best = 1.0
    for e in range(0, 8):
        for k in (1, 2, 5):
            if k * 10 ** e <= target:
                best = k * 10 ** e
    px = max(20, int(best / m_per_px))
    return px, (f"{best / 1000:g} km" if best >= 1000 else f"{best:g} m")


MAP_HUD_Y = 1050  # haut de la boussole / des coordonnées (sous la carte « utile », au-dessus des sous-titres)


def compass_sprite() -> np.ndarray:
    import cv2

    r = 40
    sp = np.zeros((2 * r + 8, 2 * r + 8, 4), np.uint8)
    c = (r + 4, r + 4)
    cv2.circle(sp, c, r, (12, 12, 16, 200), -1, lineType=cv2.LINE_AA)
    cv2.circle(sp, c, r, (255, 255, 255, 255), 2, lineType=cv2.LINE_AA)
    cv2.fillConvexPoly(sp, np.int32([[c[0], c[1] - r + 8], [c[0] - 10, c[1]], [c[0] + 10, c[1]]]), (230, 32, 38, 255),
                       lineType=cv2.LINE_AA)
    cv2.fillConvexPoly(sp, np.int32([[c[0], c[1] + r - 8], [c[0] - 10, c[1]], [c[0] + 10, c[1]]]),
                       (235, 235, 235, 255), lineType=cv2.LINE_AA)
    return sp


def map_fly(lon: float, lat: float, label: str, n: int, fetch, accent: tuple[int, int, int],
            z1: float = 13.0, attribution: str = "", sub: str = "",
            rings: list[list[tuple[float, float]]] | None = None) -> Callable[[int], np.ndarray]:
    """Survol satellite : descente depuis l'espace, caméra qui se penche, zone du lieu surlignée en bleu,
    épingle + étiquette, boussole, coordonnées, altitude et échelle (habillage « carte d'actu »)."""
    import cv2

    from .overlay import box_text, paste

    view = MapView(fetch)
    plan = map_plan(lon, lat, n, z1=z1)
    name_sp = np.asarray(box_text(label.upper()[:26], "title", 38, (0, 0, 0), (255, 255, 255), pad=(14, 8),
                                  radius=3).convert("RGBA")).copy() if label else None
    sub_sp = np.asarray(box_text(sub.upper()[:30], "mono", 20, (200, 200, 205), (12, 12, 16, 220), pad=(10, 5),
                                 radius=2).convert("RGBA")).copy() if sub else None
    attr_sp = np.asarray(box_text(attribution, "mono", 15, (200, 200, 200), None, pad=(0, 0)).convert("RGBA")).copy() \
        if attribution else None
    coords = f"{dms(lat, 'N', 'S')}  {dms(lon, 'E', 'O')}"
    coord_sp = np.asarray(box_text(coords, "mono", 24, (255, 255, 255), None, pad=(0, 0)).convert("RGBA")).copy()
    compass = compass_sprite()
    shapes = [np.array([lonlat_px(x, y, 0) for x, y in ring], np.float64) for ring in (rings or []) if len(ring) > 2]
    fw = int(RW * SPREAD)
    text_cache: dict[str, np.ndarray] = {}

    def mono(text: str, size: int = 22) -> np.ndarray:
        if text not in text_cache:
            text_cache[text] = np.asarray(box_text(text, "mono", size, (255, 255, 255), None, pad=(0, 0))
                                          .convert("RGBA")).copy()
        return text_cache[text]

    def frame(i: int) -> np.ndarray:
        z, tilt = plan[i]
        lvl = int(round(z))
        s = 2 ** (z - lvl)
        cx, cy = lonlat_px(lon, lat, lvl)
        shift = RH * 0.08 * tilt / MAX_TILT  # lieu un peu sous le centre une fois incliné
        flat = view.region(lvl, cx, cy - shift / s, fw / s, RH / s, fw, RH)
        k = math.tan(math.radians(tilt)) * 0.38
        top_w = RW * (1 + 2 * k)
        src = np.float32([[(fw - top_w) / 2, 0], [(fw + top_w) / 2, 0], [(fw + RW) / 2, RH], [(fw - RW) / 2, RH]])
        dst = np.float32([[0, 0], [RW, 0], [RW, RH], [0, RH]])
        mat = cv2.getPerspectiveTransform(src, dst)
        img = cv2.warpPerspective(flat, mat, (RW, RH), flags=cv2.INTER_LINEAR)
        # image un peu sombre et désaturée : l'habillage ressort mieux
        grey = img.mean(axis=2, keepdims=True)
        img = (img * 0.62 + grey * 0.18).astype(np.uint8)
        if tilt > 1:  # brume vers l'horizon
            fog = np.linspace(0.45 * tilt / MAX_TILT, 0, RH // 2, dtype=np.float32)[:, None, None]
            img[:RH // 2] = (img[:RH // 2] * (1 - fog) + np.array([60, 70, 90]) * fog).astype(np.uint8)
        f = cv2.resize(img, (W, H), interpolation=cv2.INTER_CUBIC)
        p = i / max(1, n - 1)
        up = W / RW

        def to_screen(pts_flat: np.ndarray) -> np.ndarray:
            pts_flat[:, 1] = np.clip(pts_flat[:, 1], -2 * RH, 3 * RH)
            return cv2.perspectiveTransform(pts_flat[None].astype(np.float32), mat)[0] * up

        # zone du lieu (pays, région, ville) surlignée : bleu translucide + contour blanc
        zone = clamp((p - 0.5) / 0.2)
        if shapes and zone > 0:
            x0, y0 = cx - fw / s / 2, cy - shift / s - RH / s / 2
            layer = f.copy()
            polys = []
            for ring in shapes:
                pts = np.stack([(ring[:, 0] * 2 ** lvl - x0) * s, (ring[:, 1] * 2 ** lvl - y0) * s], axis=1)
                polys.append(to_screen(pts).astype(np.int32))
            cv2.fillPoly(layer, polys, (40, 110, 235), lineType=cv2.LINE_AA)
            f[:] = cv2.addWeighted(layer, 0.35 * zone, f, 1 - 0.35 * zone, 0)
            cv2.polylines(f, polys, True, (255, 255, 255), 3, lineType=cv2.LINE_AA)
        here = to_screen(np.float32([[fw / 2, RH / 2 + shift]]))[0]
        px, py = float(here[0]), float(here[1])
        pop = (p - 0.6) / 0.12
        if pop > 0:
            ring_p = clamp((p - 0.6) / 0.3)
            cv2.circle(f, (int(px), int(py)), int(18 + 70 * ring_p), (255, 255, 255), max(1, int(4 * (1 - ring_p))),
                       lineType=cv2.LINE_AA)
            r = int(16 * ease_out_back(pop))
            if r > 0:
                cv2.circle(f, (int(px), int(py)), r + 6, (255, 255, 255), -1, lineType=cv2.LINE_AA)
                cv2.circle(f, (int(px), int(py)), r, (230, 32, 38), -1, lineType=cv2.LINE_AA)
            q = clamp((p - 0.68) / 0.12)
            if name_sp is not None and q > 0:
                lx = int(px + 34 - (1 - ease_out_cubic(q)) * 40)
                lx = min(lx, W - 30 - name_sp.shape[1])
                ly = int(py - name_sp.shape[0] - 6)
                cv2.line(f, (int(px) + 18, int(py) - 6), (lx, ly + name_sp.shape[0]), (255, 255, 255), 2,
                         lineType=cv2.LINE_AA)
                paste(f, name_sp, lx, ly, q)
                if sub_sp is not None:
                    paste(f, sub_sp, lx, ly + name_sp.shape[0] + 4, q)
        # boussole, coordonnées, altitude (à gauche) et échelle (à droite)
        hud = clamp(i / FPS / 0.4)
        y = MAP_HUD_Y
        paste(f, compass, 30, y, hud)
        paste(f, coord_sp, 30 + compass.shape[1] + 16, y + 12, hud)
        m_px = 156543.03 * math.cos(math.radians(lat)) / 2 ** z / up
        alt = W * m_px / 1000 * 1.1
        paste(f, mono(f"ALT {alt:,.0f} km".replace(",", " ") if alt >= 10 else f"ALT {alt:.1f} km"),
              30 + compass.shape[1] + 16, y + 48, hud)
        bar, txt = nice_scale(m_px)
        bx1, by = W - 40, y + 64
        cv2.line(f, (bx1 - bar, by), (bx1, by), (255, 255, 255), 3, lineType=cv2.LINE_AA)
        for xx in (bx1 - bar, bx1):
            cv2.line(f, (xx, by - 10), (xx, by + 2), (255, 255, 255), 3, lineType=cv2.LINE_AA)
        t_sp = mono(txt)
        paste(f, t_sp, bx1 - t_sp.shape[1], by - 40, hud)
        if attr_sp is not None:
            paste(f, attr_sp, 30, y + compass.shape[0] + 14, 0.8 * hud)
        return f

    frame.view = view  # type: ignore[attr-defined]
    return frame


# ---------- son ----------

def whoosh_track(times: list[float], total: float, out: Path, sr: int = 44100, gain: float = 0.5) -> Path:
    """Piste de « whoosh » (souffle filtré qui monte puis redescend) calée sur les transitions."""
    import wave

    rng = np.random.default_rng(7)
    track = np.zeros(int(total * sr) + sr, np.float32)
    dur = 0.42
    m = int(dur * sr)
    for t0 in times:
        noise = rng.standard_normal(m).astype(np.float32)
        env = np.sin(np.linspace(0, math.pi, m)) ** 2
        cutoff = 0.02 + 0.25 * np.sin(np.linspace(0, math.pi, m))  # filtre qui s'ouvre puis se referme
        y = np.zeros(m, np.float32)
        acc = 0.0
        for k in range(m):
            acc += cutoff[k] * (noise[k] - acc)
            y[k] = acc
        y *= env / max(1e-6, float(np.abs(y).max()))
        a = int(max(0.0, t0 - dur * 0.5) * sr)
        track[a:a + m] += y[:len(track) - a] * gain
    pcm = (np.clip(track[:int(total * sr)], -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return out
