"""Voix off 100 % locale avec Piper (rapide sur CPU, voix françaises naturelles).

Chaque phrase est lue à part : on connaît ainsi sa durée exacte, ce qui donne le découpage des plans
et l'horodatage mot à mot des sous-titres (réparti selon la longueur des mots, la voix étant régulière)."""
from __future__ import annotations

import logging
import re
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)
VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
GAP = 0.18  # silence entre deux phrases (s) : rythme serré des vidéos d'actu
_voices: dict[str, object] = {}
_lock = threading.Lock()


@dataclass
class Spoken:
    text: str
    start: float
    end: float
    words: list[dict]  # [{start, end, word}] en temps de la voix off complète


def voice_files(name: str) -> tuple[str, str]:
    """« fr_FR-tom-medium » → URLs du modèle et de sa config sur le dépôt des voix Piper."""
    m = re.fullmatch(r"([a-z]{2})_([A-Z]{2})-([\w]+)-(x_low|low|medium|high)", name)
    if not m:
        raise ValueError(f"Nom de voix Piper invalide : {name}")
    lang, region, speaker, quality = m.groups()
    base = f"{VOICES_URL}/{lang}/{lang}_{region}/{speaker}/{quality}/{name}.onnx"
    return base, base + ".json"


def ensure_voice(name: str) -> Path:
    d = get_settings().data_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    model = d / f"{name}.onnx"
    for url, path in zip(voice_files(name), (model, d / f"{name}.onnx.json")):
        if path.exists() and path.stat().st_size > 0:
            continue
        log.info("Téléchargement de la voix %s…", path.name)
        tmp = path.with_suffix(path.suffix + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        tmp.replace(path)
    return model


def _voice(name: str):
    with _lock:
        if name not in _voices:
            from piper import PiperVoice

            _voices[name] = PiperVoice.load(ensure_voice(name))
        return _voices[name]


def for_speech(text: str) -> str:
    """Texte prononçable : sigles et symboles lus correctement par la voix."""
    t = text.replace("GTA 6", "GTA six").replace("GTA VI", "GTA six").replace("€", " euros")
    t = re.sub(r"(\d)\s?%", r"\1 pour cent", t)
    t = re.sub(r"\bPS5\b", "PS cinq", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def trim(audio: np.ndarray, sr: int, thresh: float = 0.01, pad: float = 0.03) -> np.ndarray:
    """Retire le silence au début et à la fin d'une phrase (le rythme des sous-titres reste calé)."""
    if audio.size == 0:
        return audio
    loud = np.flatnonzero(np.abs(audio) > thresh * max(1e-6, float(np.abs(audio).max())))
    if loud.size == 0:
        return audio[:0]
    a = max(0, loud[0] - int(pad * sr))
    b = min(audio.size, loud[-1] + int(pad * sr))
    return audio[a:b]


def word_times(text: str, start: float, end: float) -> list[dict]:
    """Mots de la phrase répartis sur sa durée selon leur longueur (+ petite pause après la ponctuation)."""
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 2 + (3 if w.endswith((",", ";", ":")) else 0) for w in words]
    total = sum(weights)
    out, t = [], start
    for w, k in zip(words, weights):
        d = (end - start) * k / total
        out.append({"start": round(t, 3), "end": round(t + d, 3), "word": w})
        t += d
    return out


def _synth(voice, text: str, speed: float | None = None) -> tuple[np.ndarray, int]:
    import io

    from piper import SynthesisConfig

    s = get_settings()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(for_speech(text), wf,
                             syn_config=SynthesisConfig(length_scale=1 / max(0.5, speed or s.news_voice_speed)))
    buf.seek(0)
    with wave.open(buf, "rb") as wf:
        sr = wf.getframerate()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return audio, sr


def speak(sentences: list[str], out_wav: Path, synth=None, speed: float | None = None) -> list[Spoken]:
    """Lit les phrases à la suite dans out_wav (mono 16 bits). synth(text) -> (audio float, sr) remplaçable
    (tests). speed : débit (défaut NEWS_VOICE_SPEED). Renvoie le minutage de chaque phrase et de chaque mot."""
    if synth is None:
        voice = _voice(get_settings().news_voice)
        synth = lambda text: _synth(voice, text, speed)  # noqa: E731
    chunks, spoken, t, sr = [], [], 0.0, 22050
    for text in sentences:
        audio, sr = synth(text)
        audio = trim(audio, sr)
        if audio.size == 0:
            continue
        dur = audio.size / sr
        spoken.append(Spoken(text, round(t, 3), round(t + dur, 3), word_times(text, t, t + dur)))
        chunks += [audio, np.zeros(int(GAP * sr), dtype=np.float32)]
        t += dur + GAP
    if not chunks:
        raise RuntimeError("Voix off vide")
    pcm = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype(np.int16)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return spoken
