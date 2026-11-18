"""Transcription locale avec faster-whisper (CPU, int8), horodatage mot à mot.

L'audio est transcrit par morceaux et chaque phrase est sauvegardée au fur et à mesure dans
`transcript.partial.jsonl` : après un redémarrage (mise à jour du serveur…), la transcription
reprend là où elle s'était arrêtée au lieu de repartir de zéro."""
from __future__ import annotations

import json
import logging
import os
import time
import wave
from pathlib import Path

import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)
_model = None

CHUNK = 600.0  # secondes d'audio transcrites d'un coup (limite aussi la mémoire utilisée)
SEARCH = 15.0  # le découpage se fait sur le passage le plus calme des SEARCH dernières secondes


class Interrupted(Exception):
    """Transcription arrêtée (arrêt du worker) : elle reprendra au prochain démarrage."""


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        s = get_settings()
        _model = WhisperModel(
            s.whisper_model, device="cpu", compute_type="int8", cpu_threads=s.whisper_threads,
            download_root=str(s.data_dir / "models"),
        )
    return _model


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def partial_path(out_json: Path) -> Path:
    return out_json.with_name(out_json.stem + ".partial.jsonl")


def load_partial(path: Path) -> tuple[list[dict], float]:
    """Phrases déjà transcrites et position (s) d'où reprendre."""
    segs, done = [], 0.0
    if not path.exists():
        return segs, done
    for line in path.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:  # dernière ligne coupée par un arrêt brutal
            continue
        if "seg" in item:
            segs.append(item["seg"])
            done = max(done, item["seg"]["end"])
        elif "done" in item:
            done = max(done, item["done"])
    return segs, done


def _read(wav: Path, start: float, end: float) -> np.ndarray:
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        w.setpos(min(w.getnframes(), int(start * sr)))
        raw = w.readframes(max(0, int((end - start) * sr)))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def wav_duration(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def _cut_point(wav: Path, start: float, end: float) -> float:
    """Fin du morceau placée sur le passage le plus calme juste avant `end` (évite de couper un mot)."""
    a = max(start + 1, end - SEARCH)
    audio = _read(wav, a, end)
    win = 1600  # 0,1 s à 16 kHz
    n = len(audio) // win
    if n < 2:
        return end
    rms = np.sqrt((audio[: n * win].reshape(n, win) ** 2).mean(axis=1))
    return a + (int(rms.argmin()) + 0.5) * (end - a) / n


def transcribe(wav: Path, out_json: Path, total: float | None = None, on_progress=None,
               should_stop=None) -> list[dict]:
    """Retourne [{start, end, text, words: [{start, end, word}]}] et l'écrit dans out_json.

    `on_progress(pct)` reçoit l'avancement (0-100) ; si `should_stop()` devient vrai, lève
    `Interrupted` (le travail déjà fait est gardé et sera repris)."""
    if out_json.exists():
        return json.loads(out_json.read_text())
    s = get_settings()
    part = partial_path(out_json)
    result, pos = load_partial(part)
    length = wav_duration(wav)
    total = total or length
    if pos > 0:
        log.info("Reprise de la transcription à %s / %s", _hms(pos), _hms(total))
    t0 = last = time.monotonic()
    with open(part, "a") as f:
        def save(item: dict) -> None:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

        while pos < length - 0.5:
            if should_stop and should_stop():
                raise Interrupted()
            end = length if length - pos <= CHUNK + SEARCH else _cut_point(wav, pos, pos + CHUNK)
            segments, _ = _get_model().transcribe(
                _read(wav, pos, end), language=s.whisper_language or None, word_timestamps=True,
                vad_filter=True, beam_size=1, initial_prompt=s.whisper_prompt or None,
            )
            for seg in segments:
                a, b = pos + seg.start, pos + seg.end
                if on_progress:
                    on_progress(min(99.0, b * 100 / total))
                if time.monotonic() - last >= 30:
                    last = time.monotonic()
                    log.info("Transcription : %s / %s (%d %%), depuis %d min", _hms(b), _hms(total),
                             min(99, b * 100 / total), (last - t0) // 60)
                words = [{"start": pos + w.start, "end": pos + w.end, "word": w.word.strip()}
                         for w in (seg.words or []) if w.word.strip()]
                item = {"start": a, "end": b, "text": seg.text.strip(), "words": words}
                result.append(item)
                save({"seg": item})
                if should_stop and should_stop():
                    raise Interrupted()
            save({"done": end})
            pos = end
    tmp = out_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(out_json)
    part.unlink(missing_ok=True)
    if on_progress:
        on_progress(100)
    return result
