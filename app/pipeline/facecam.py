"""Détection de la facecam du streamer (petit cadre avec son visage, en général dans un coin).

Léger pour le CPU : on extrait ~8 images basse définition du passage, on cherche les visages
(OpenCV, détecteur Haar), et on garde un visage qui revient au même endroit sur la plupart des images.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_W = 640  # largeur des images analysées


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


def grab_frames(path: Path, start: float, end: float, n: int = 8) -> list[np.ndarray]:
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
    found = _detector().detectMultiScale(frame, scaleFactor=1.15, minNeighbors=6,
                                         minSize=(max(16, w // 40), max(16, w // 40)))
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


def cam_region(face: Box, frame_w: int, frame_h: int, aspect: float) -> Box:
    """Cadre autour du visage (épaules comprises) au rapport largeur/hauteur `aspect` (en pixels)."""
    fw, fh = face.w * frame_w, face.h * frame_h
    h = min(frame_h, fh * 2.6)
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
        return cam_region(face, w, h, aspect)
    except Exception as e:  # OpenCV absent, vidéo illisible… : on garde le rendu classique
        log.warning("Détection de la facecam impossible : %s", e)
        return None
