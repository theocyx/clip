"""Habillage animé des vidéos d'actu (motion design), calculé image par image avec numpy + OpenCV :

- photo en carte 3D qui pivote lentement devant un fond flou qui bouge en sens inverse (parallaxe) ;
- mots forts qui basculent en 3D un par un sur un fond animé (dégradé, halos, particules) ;
- chiffre clé qui défile de 0 à sa valeur dans un anneau qui se remplit ;
- survol « vu d'en haut » : zoom depuis l'espace jusqu'au lieu de l'actu sur des images satellite libres,
  la caméra s'incline en 3D, puis une épingle tombe sur le lieu avec son nom.

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


# ---------- fonds animés ----------

def animated_bg(seed: int, accent: tuple[int, int, int], margin: float = 1.3) -> Callable[[int], np.ndarray]:
    """Fond sombre en dégradé avec halos de couleur qui dérivent et particules lumineuses qui montent."""
    import cv2

    rng = np.random.default_rng(seed)
    bw, bh = int(W * margin), int(H * margin)
    y = np.linspace(0, 1, bh, dtype=np.float32)[:, None, None]
    top, bot = np.array([8, 10, 22], np.float32), np.array(accent, np.float32) * 0.22 + 6
    big = np.repeat(top * (1 - y) + bot * y, bw, axis=1)
    q = 8
    glow = np.zeros((bh // q, bw // q, 3), np.float32)
    for k in range(4):
        col = np.array(accent if k % 2 == 0 else (60, 120, 255), np.float32)
        cx, cy = rng.uniform(0.15, 0.85) * bw / q, rng.uniform(0.15, 0.85) * bh / q
        cv2.circle(glow, (int(cx), int(cy)), int(rng.uniform(0.18, 0.3) * bw / q), col.tolist(), -1)
    glow = cv2.GaussianBlur(glow, (0, 0), bw / q * 0.12)
    big = np.clip(big + cv2.resize(glow, (bw, bh)) * 0.55, 0, 255).astype(np.uint8)
    parts = rng.uniform(0, 1, (46, 4))  # x, y, vitesse, taille
    phase = rng.uniform(0, 2 * math.pi)

    def frame(i: int) -> np.ndarray:
        t = i / FPS
        dx = (bw - W) / 2 * (1 + 0.8 * math.sin(phase + t * 0.6))
        dy = (bh - H) / 2 * (1 + 0.8 * math.cos(phase + t * 0.45))
        f = big[int(dy):int(dy) + H, int(dx):int(dx) + W].copy()
        for px, py, sp, sz in parts:
            yy = int(((py - t * (0.04 + 0.08 * sp)) % 1.0) * H)
            xx = int((px + 0.01 * math.sin(t * 2 + py * 9)) * W)
            r = int(2 + 5 * sz)
            cv2.circle(f, (xx, yy), r, (255, 255, 255) if sz < 0.7 else accent, -1, lineType=cv2.LINE_AA)
        return f

    return frame


# ---------- plans ----------

def parallax(path: Path, n: int, idx: int) -> Callable[[int], np.ndarray]:
    """Photo en carte 3D qui arrive du fond, pivote lentement et flotte ; fond flou de la même photo en sens
    inverse (effet de profondeur)."""
    import cv2

    img = load_rgb(path)
    bw, bh = int(W * 1.18), int(H * 1.18)
    small = cover(img, bw // 6, bh // 6)
    bg = cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (bw, bh), interpolation=cv2.INTER_LINEAR)
    bg = (bg * 0.45).astype(np.uint8)
    ih, iw = img.shape[:2]
    cw = 940
    ch = min(1000, round(ih * cw / iw))
    if ch < 520:  # image très large : un peu plus grande, quitte à rogner les côtés
        ch = 560
    card = rounded_card(cover(img, cw, ch), radius=42, border=6)
    side = 1 if idx % 2 == 0 else -1
    cx, cy = W / 2, H * 0.43

    def frame(i: int) -> np.ndarray:
        t, p = i / FPS, i / max(1, n - 1)
        e = ease_out_back(t / 0.45)
        ry = side * (-10 + 18 * ease_in_out(p)) + (1 - ease_out_cubic(t / 0.45)) * 35 * side
        rx = 6 - 9 * p
        scale = 0.6 + 0.4 * e + 0.05 * p
        fy = cy + 14 * math.sin(t * 2.2)
        sx = (bw - W) / 2 * (1 - side * 0.8 * (p - 0.5) * 2)
        sy = (bh - H) / 2 * (1 + 0.5 * (p - 0.5))
        f = bg[int(sy):int(sy) + H, int(sx):int(sx) + W].copy()
        poly = project(cw, ch, cx, fy, scale, rx=rx, ry=ry)
        drop_shadow(f, poly)
        warp_onto(f, card, poly, opacity=clamp(t / 0.15))
        return f

    return frame


def split_words(text: str, per_line: int = 11, max_lines: int = 3) -> list[list[str]]:
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
    """Mots forts qui basculent en 3D un par un, puis le bloc flotte en pivotant légèrement."""
    bg = animated_bg(idx * 7 + 3, accent)
    lines = split_words(text) or [["INFO"]]
    size = 150
    while size > 80 and max(sum(text_sprite(w, size).shape[1] for w in ln) + 30 * (len(ln) - 1)
                            for ln in lines) > W - 120:
        size -= 14
    sprites, k = [], 0
    line_h = size * 1.25
    y0 = -line_h * (len(lines) - 1) / 2
    for li, ln in enumerate(lines):
        sp = [text_sprite(w, size, accent if (li + j) % 2 == 0 else (255, 255, 255)) for j, w in enumerate(ln)]
        total = sum(s.shape[1] for s in sp) + 30 * (len(sp) - 1)
        x = -total / 2
        for s in sp:
            sprites.append((s, x + s.shape[1] / 2, y0 + li * line_h, k))
            x += s.shape[1] + 30
            k += 1
    cx, cy = W / 2, H * 0.42
    phase = idx * 1.3

    def frame(i: int) -> np.ndarray:
        f = bg(i)
        t = i / FPS
        ry, rz = 9 * math.sin(phase + t * 1.4), 2 * math.sin(phase + t * 0.9)
        for s, ox, oy, order in sprites:
            p = (t - 0.08 * order) / 0.32
            if p <= 0:
                continue
            e = ease_out_back(p)
            poly = project(s.shape[1], s.shape[0], cx, cy, 0.7 + 0.3 * e, rx=88 * (1 - ease_out_cubic(p)),
                           ry=ry, rz=rz, ox=ox, oy=oy)
            warp_onto(f, s, poly, opacity=clamp(p * 2))
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
    """Chiffre clé qui défile de 0 à sa valeur dans un anneau qui se remplit, l'unité arrive en 3D dessous."""
    import cv2

    parsed = parse_number(value_text)
    val, dec = (parsed[0], parsed[1]) if parsed else (0.0, 0)
    bg = animated_bg(idx * 11 + 5, accent)
    cx, cy, radius = W // 2, int(H * 0.40), 280
    unit_sp = text_sprite(unit.upper()[:22], 84, (0, 0, 0), box=accent) if unit else None
    final = fmt_number(val, dec)
    size = 190
    while size > 90 and text_sprite(final, size).shape[1] > 2 * radius - 70:
        size -= 12
    cache: dict[str, np.ndarray] = {}

    def frame(i: int) -> np.ndarray:
        f = bg(i)
        t = i / FPS
        p = ease_out_cubic(t / 1.0)
        cv2.circle(f, (cx, cy), radius, (255, 255, 255), 6, lineType=cv2.LINE_AA)
        cv2.ellipse(f, (cx, cy), (radius, radius), -90, 0, 360 * p, accent, 22, lineType=cv2.LINE_AA)
        txt = fmt_number(val * p, dec) if p < 1 else final
        sp = cache.get(txt)
        if sp is None:
            sp = cache[txt] = text_sprite(txt, size)
        pop = 1 + 0.08 * math.sin(math.pi * clamp((t - 1.0) / 0.25))  # petit rebond à l'arrivée
        warp_onto(f, sp, project(sp.shape[1], sp.shape[0], cx, cy, pop, ry=6 * math.sin(t * 1.3)))
        if unit_sp is not None:
            q = (t - 0.5) / 0.35
            if q > 0:
                poly = project(unit_sp.shape[1], unit_sp.shape[0], cx, cy + radius + 105, 0.8 + 0.2 * ease_out_back(q),
                               rx=80 * (1 - ease_out_cubic(q)), ry=-5 * math.sin(t * 1.3))
                warp_onto(f, unit_sp, poly, opacity=clamp(q * 2))
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


def map_plan(lon: float, lat: float, n: int, z0: float = 3.0, z1: float = 13.0) -> list[tuple[float, float]]:
    """(zoom, inclinaison en degrés) de chaque image : descente depuis l'espace puis la caméra se penche."""
    out = []
    for i in range(n):
        p = i / max(1, n - 1)
        z = z0 + (z1 - z0) * ease_in_out(p / 0.72) + 0.35 * clamp((p - 0.72) / 0.28)
        out.append((z, 42 * ease_in_out((p - 0.35) / 0.6)))
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


def map_fly(lon: float, lat: float, label: str, n: int, fetch, accent: tuple[int, int, int],
            z1: float = 13.0, attribution: str = "") -> Callable[[int], np.ndarray]:
    import cv2

    view = MapView(fetch)
    plan = map_plan(lon, lat, n, z1=z1)
    label_sp = text_sprite(label.upper()[:28], 66, (0, 0, 0), box=(255, 255, 255)) if label else None
    attr_sp = text_sprite(attribution, 22, (255, 255, 255), stroke=3) if attribution else None
    fw = int(RW * SPREAD)

    def frame(i: int) -> np.ndarray:
        z, tilt = plan[i]
        lvl = int(round(z))
        s = 2 ** (z - lvl)
        cx, cy = lonlat_px(lon, lat, lvl)
        # lieu un peu sous le centre une fois incliné (on voit l'horizon au-dessus)
        flat = view.region(lvl, cx, cy - RH * 0.08 * tilt / 42 / s, fw / s, RH / s, fw, RH)
        k = math.tan(math.radians(tilt)) * 0.38
        top_w = RW * (1 + 2 * k)
        src = np.float32([[(fw - top_w) / 2, 0], [(fw + top_w) / 2, 0], [(fw + RW) / 2, RH], [(fw - RW) / 2, RH]])
        dst = np.float32([[0, 0], [RW, 0], [RW, RH], [0, RH]])
        mat = cv2.getPerspectiveTransform(src, dst)
        img = cv2.warpPerspective(flat, mat, (RW, RH), flags=cv2.INTER_LINEAR)
        if tilt > 1:  # brume vers l'horizon
            fog = np.linspace(0.55 * tilt / 42, 0, RH // 2, dtype=np.float32)[:, None, None]
            img[:RH // 2] = (img[:RH // 2] * (1 - fog) + np.array([170, 190, 215]) * fog).astype(np.uint8)
        f = cv2.resize(img, (W, H), interpolation=cv2.INTER_CUBIC)
        p = i / max(1, n - 1)
        # le lieu à l'écran : son point sur la carte à plat, passé dans la même perspective
        here = cv2.perspectiveTransform(np.float32([[[fw / 2, RH / 2 + RH * 0.08 * tilt / 42]]]), mat)[0, 0]
        px, py = float(here[0]) * W / RW, float(here[1]) * H / RH
        drop = (p - 0.66) / 0.14
        if drop > 0:
            ring = clamp((p - 0.78) / 0.22)
            if ring > 0:
                cv2.ellipse(f, (int(px), int(py)), (int(30 + 120 * ring), int((30 + 120 * ring) * 0.45)), 0, 0, 360,
                            (255, 255, 255), max(1, int(6 * (1 - ring))), lineType=cv2.LINE_AA)
            fall = (1 - ease_out_back(drop)) * 500  # l'épingle entière tombe puis rebondit
            tip = py - fall
            hy = tip - 130
            cv2.ellipse(f, (int(px), int(py)), (26, 10), 0, 0, 360, (0, 0, 0), -1, lineType=cv2.LINE_AA)
            tri = np.int32([[px - 44, hy + 30], [px + 44, hy + 30], [px, tip]])
            cv2.fillConvexPoly(f, tri, (230, 30, 40), lineType=cv2.LINE_AA)
            cv2.circle(f, (int(px), int(hy)), 56, (230, 30, 40), -1, lineType=cv2.LINE_AA)
            cv2.circle(f, (int(px), int(hy)), 22, (255, 255, 255), -1, lineType=cv2.LINE_AA)
            if label_sp is not None:
                q = (p - 0.76) / 0.12
                if q > 0:
                    poly = project(label_sp.shape[1], label_sp.shape[0], px, py + 110, 0.75 + 0.25 * ease_out_back(q),
                                   rx=75 * (1 - ease_out_cubic(q)))
                    warp_onto(f, label_sp, poly, opacity=clamp(q * 2))
        if attr_sp is not None:
            warp_onto(f, attr_sp, project(attr_sp.shape[1], attr_sp.shape[0], 24 + attr_sp.shape[1] / 2,
                                          H - 150, 1.0), opacity=0.8)
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
