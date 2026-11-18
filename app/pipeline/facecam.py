"""Détection de la facecam du streamer (petit cadre avec son visage, en général dans un coin).

Léger pour le CPU : on extrait ~10 images du passage, on cherche les visages (OpenCV, détecteur Haar),
et on garde un visage qui revient au même endroit sur la plupart des images. On retrouve ensuite le
vrai cadre de la facecam : ses bords restent au même endroit d'une image à l'autre, contrairement au jeu.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_W = 960  # largeur des images analysées (assez pour un visage de facecam dans un coin)


@dataclass
class Box:
    """Rectangle en fractions de l'image (0..1)."""

    x: float
    y: float
    w: float
    h: float

    def as_dict(self) -> dict:
        return {k: round(v, 4) for k, v in self.__dict__.items()}


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    return int(st["width"]), int(st["height"])


def grab_frames(path: Path, start: float, end: float, n: int = 10) -> list[np.ndarray]:
    """Images en niveaux de gris, SAMPLE_W px de large, réparties sur [start, end]."""
    w, h = video_size(path)
    sh = max(2, round(SAMPLE_W * h / w / 2) * 2)
    frames = []
    for i in range(n):
        t = start + (end - start) * (i + 0.5) / n
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={SAMPLE_W}:{sh},format=gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=60).stdout
        if len(raw) == SAMPLE_W * sh:
            frames.append(np.frombuffer(raw, dtype=np.uint8).reshape(sh, SAMPLE_W))
    return frames


_cascade = None


def _detector():
    global _cascade
    if _cascade is None:
        import cv2
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _cascade


def faces_in(frame: np.ndarray) -> list[Box]:
    h, w = frame.shape
    import cv2
    size = (max(20, w // 50), max(20, w // 50))
    found = _detector().detectMultiScale(frame, scaleFactor=1.1, minNeighbors=5, minSize=size)
    if len(found) == 0:  # webcam sombre : on rehausse le contraste localement et on réessaie
        boosted = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(frame)
        found = _detector().detectMultiScale(boosted, scaleFactor=1.1, minNeighbors=5, minSize=size)
    return [Box(x / w, y / h, fw / w, fh / h) for x, y, fw, fh in found]


def stable_face(per_frame: list[list[Box]], min_ratio: float = 0.5) -> Box | None:
    """Visage présent au même endroit sur au moins `min_ratio` des images, hors du centre de l'écran."""
    if not per_frame:
        return None
    candidates = [b for boxes in per_frame for b in boxes if 0.03 <= b.w <= 0.3]
    best, best_hits = None, 0
    for c in candidates:
        cx, cy = c.x + c.w / 2, c.y + c.h / 2
        hits = []
        for boxes in per_frame:
            near = [b for b in boxes if abs(b.x + b.w / 2 - cx) < c.w * 0.5
                    and abs(b.y + b.h / 2 - cy) < c.h * 0.5 and 0.6 < b.w / c.w < 1.6]
            if near:
                hits.append(near[0])
        if len(hits) > best_hits:
            best_hits = len(hits)
            best = Box(*(float(np.median([getattr(b, k) for b in hits])) for k in ("x", "y", "w", "h")))
    if best is None or best_hits < max(2, round(len(per_frame) * min_ratio)):
        return None
    cx, cy = best.x + best.w / 2, best.y + best.h / 2
    if 0.3 < cx < 0.7 and 0.3 < cy < 0.7:  # visage au centre : pas une facecam (caméra plein écran, perso du jeu)
        return None
    return best


def persistent_edges(frames: list[np.ndarray], ratio: float = 0.6) -> np.ndarray:
    """Pixels qui sont un contour sur au moins `ratio` des images (bords fixes : cadre de la facecam)."""
    import cv2
    acc = np.zeros(frames[0].shape, dtype=np.float32)
    for f in frames:
        acc += cv2.Canny(f, 50, 150) > 0
    return acc / len(frames) >= ratio


def _peaks(strength: np.ndarray, lo: int, hi: int, thr: float, keep: int = 8) -> list[int]:
    """Positions (lignes ou colonnes) entre lo et hi où un bord fixe est assez continu, les plus nettes d'abord."""
    lo, hi = max(0, lo), min(len(strength), hi)
    idx = [i for i in range(lo, hi) if strength[i] >= thr
           and strength[i] >= strength[max(0, i - 1)] and strength[i] >= strength[min(len(strength) - 1, i + 1)]]
    idx.sort(key=lambda i: -strength[i])
    out: list[int] = []
    for i in idx:  # une seule position par bord épais
        if all(abs(i - o) > 3 for o in out):
            out.append(i)
        if len(out) == keep:
            break
    return out


def find_cam_box(edges: np.ndarray, face: Box, thr: float = 0.35, min_score: float = 0.75,
                 min_side: float = 0.6) -> Box | None:
    """Cadre de la facecam autour du visage, d'après les contours fixes. None si on ne le trouve pas.

    Le décor de la webcam (chambre, fond vert) a aussi des contours fixes : on ne prend donc pas la
    première ligne venue mais le plus grand rectangle dont les quatre côtés sont des bords continus."""
    h, w = edges.shape
    fx, fy, fw, fh = face.x * w, face.y * h, face.w * w, face.h * h
    cx, cy = fx + fw / 2, fy + fh / 2
    dil = edges.copy()  # tolérance d'un pixel
    dil[1:] |= edges[:-1]
    dil[:-1] |= edges[1:]
    dil[:, 1:] |= edges[:, :-1]
    dil[:, :-1] |= edges[:, 1:]
    d = dil.astype(np.float32)
    d[0, :] = d[-1, :] = d[:, 0] = d[:, -1] = 1  # le bord de l'image compte comme un bord de cadre
    col_cum = np.vstack([np.zeros((1, w), np.float32), np.cumsum(d, axis=0)])  # col_cum[y, x]
    row_cum = np.hstack([np.zeros((h, 1), np.float32), np.cumsum(d, axis=1)])  # row_cum[y, x]

    def vcov(x, t, b):
        return float(col_cum[b, x] - col_cum[t, x]) / max(1, b - t)

    def hcov(y, l, r):
        return float(row_cum[y, r] - row_cum[y, l]) / max(1, r - l)

    # repérage grossier des candidats : continuité sur une bande autour du visage
    r0, r1 = int(max(0, cy - 1.0 * fh)), int(min(h, cy + 1.0 * fh))
    c0, c1 = int(max(0, cx - 1.0 * fw)), int(min(w, cx + 1.0 * fw))
    if r1 - r0 < 4 or c1 - c0 < 4:
        return None
    cols = d[r0:r1].mean(axis=0)
    rows = d[:, c0:c1].mean(axis=1)
    lefts = _peaks(cols, int(fx - 4 * fw), int(fx - 0.2 * fw), thr)
    rights = _peaks(cols, int(fx + 1.2 * fw), int(fx + 5 * fw) + 1, thr)
    tops = _peaks(rows, int(fy - 3 * fh), int(fy - 0.3 * fh), thr)
    bottoms = _peaks(rows, int(fy + 1.05 * fh), int(fy + 5 * fh) + 1, thr)
    best, best_area = None, 0.0
    for left in lefts:
        for right in rights:
            bw = right - left
            if bw < 1.8 * fw or bw > 0.6 * w:
                continue
            for top in tops:
                for bottom in bottoms:
                    bh = bottom - top
                    if bh < 1.8 * fh or bh > 0.8 * h or bw * bh <= best_area:
                        continue
                    sides = (vcov(left, top, bottom + 1), vcov(right, top, bottom + 1),
                             hcov(top, left, right + 1), hcov(bottom, left, right + 1))
                    if min(sides) < min_side or sum(sides) / 4 < min_score:
                        continue
                    # côté posé sur le bord de l'image : les côtés voisins doivent aller jusqu'au bout,
                    # sinon la facecam s'arrête avant et on prendrait une bande de jeu
                    ex, ey = max(3, bw // 20), max(3, bh // 20)
                    ends = ([hcov(top, 0, ex), hcov(bottom, 0, ex)] if left == 0 else []) \
                        + ([hcov(top, right + 1 - ex, right + 1), hcov(bottom, right + 1 - ex, right + 1)]
                           if right == w - 1 else []) \
                        + ([vcov(left, 0, ey), vcov(right, 0, ey)] if top == 0 else []) \
                        + ([vcov(left, bottom + 1 - ey, bottom + 1), vcov(right, bottom + 1 - ey, bottom + 1)]
                           if bottom == h - 1 else [])
                    if all(c >= 0.7 for c in ends):
                        best, best_area = (left, top, right, bottom), bw * bh
    if best is None:
        return None
    left, top, right, bottom = best
    m = 2  # rester à l'intérieur du cadre (bordure décorative)
    left, top = (left + m if left > 0 else 0), (top + m if top > 0 else 0)
    right, bottom = (right - m + 1 if right < w - 1 else w), (bottom - m + 1 if bottom < h - 1 else h)
    return Box(left / w, top / h, (right - left) / w, (bottom - top) / h)


def fit_aspect(box: Box, face: Box, frame_w: int, frame_h: int, aspect: float) -> Box:
    """Plus grand cadre au rapport `aspect` dans `box`, centré au mieux sur le visage (tête non coupée)."""
    bx, by, bw, bh = box.x * frame_w, box.y * frame_h, box.w * frame_w, box.h * frame_h
    if bw / bh > aspect:
        w, h = bh * aspect, bh
    else:
        w, h = bw, bw / aspect
    cx = (face.x + face.w / 2) * frame_w
    cy = (face.y + face.h / 2) * frame_h + face.h * frame_h * 0.25
    x = min(max(cx - w / 2, bx), bx + bw - w)
    y = min(max(cy - h / 2, by), by + bh - h)
    return Box(x / frame_w, y / frame_h, w / frame_w, h / frame_h)


def cam_region(face: Box, frame_w: int, frame_h: int, aspect: float) -> Box:
    """Repli sans cadre trouvé : zone autour du visage (épaules comprises) au rapport `aspect` (en pixels)."""
    fw, fh = face.w * frame_w, face.h * frame_h
    h = min(frame_h, fh * 3.2)
    w = h * aspect
    if w > frame_w * 0.6:  # ne pas prendre la moitié du jeu
        w = frame_w * 0.6
        h = w / aspect
    cx = (face.x + face.w / 2) * frame_w
    cy = (face.y + face.h / 2) * frame_h + fh * 0.25  # un peu plus bas pour avoir les épaules
    x = min(max(cx - w / 2, 0), frame_w - w)
    y = min(max(cy - h / 2, 0), frame_h - h)
    return Box(x / frame_w, y / frame_h, w / frame_w, h / frame_h)


def detect(path: Path, start: float, end: float, aspect: float) -> Box | None:
    """Zone de la facecam (fractions de l'image) ou None si pas de facecam fiable."""
    try:
        frames = grab_frames(path, start, end)
        if len(frames) < 3:
            return None
        face = stable_face([faces_in(f) for f in frames])
        if face is None:
            return None
        w, h = video_size(path)
        box = find_cam_box(persistent_edges(frames), face)
        if box is None:
            log.info("Facecam détectée mais cadre non trouvé : recadrage autour du visage.")
            return cam_region(face, w, h, aspect)
        log.info("Facecam détectée : %s", box.as_dict())
        return fit_aspect(box, face, w, h, aspect)
    except Exception as e:  # OpenCV absent, vidéo illisible… : on garde le rendu classique
        log.warning("Détection de la facecam impossible : %s", e)
        return None
