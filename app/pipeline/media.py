"""Outils ffmpeg : durée, extraction audio, sonie par seconde."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

SR = 16000


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def extract_audio(path: Path, wav: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    return wav


def loudness_per_second(wav: Path) -> np.ndarray:
    """Niveau RMS (dB) pour chaque seconde de l'audio."""
    data = np.memmap(wav, dtype=np.int16, mode="r", offset=44)
    n = len(data) // SR
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        chunk = data[i * SR:(i + 1) * SR].astype(np.float32) / 32768.0
        out[i] = 20 * np.log10(np.sqrt(np.mean(chunk ** 2)) + 1e-6)
    return out
