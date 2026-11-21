"""Voix off : voix neuronale Microsoft Edge (gratuite, sans clé, très naturelle ; NEWS_TTS=edge) ou voix
100 % locale Piper (NEWS_TTS=piper, et secours automatique si Edge ne répond pas).

Chaque phrase est lue à part : on connaît ainsi sa durée exacte, ce qui donne le découpage des plans
et l'horodatage mot à mot des sous-titres (instants donnés par Edge, sinon répartis selon la longueur des mots)."""
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


def trim_bounds(audio: np.ndarray, sr: int, thresh: float = 0.01, pad: float = 0.03) -> tuple[int, int]:
    if audio.size == 0:
        return 0, 0
    loud = np.flatnonzero(np.abs(audio) > thresh * max(1e-6, float(np.abs(audio).max())))
    if loud.size == 0:
        return 0, 0
    return max(0, loud[0] - int(pad * sr)), min(audio.size, loud[-1] + int(pad * sr))


def trim(audio: np.ndarray, sr: int, thresh: float = 0.01, pad: float = 0.03) -> np.ndarray:
    """Retire le silence au début et à la fin d'une phrase (le rythme des sous-titres reste calé)."""
    a, b = trim_bounds(audio, sr, thresh, pad)
    return audio[a:b]


def _norm(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower())


def boundary_times(text: str, bounds: list[tuple[float, float, str]], start: float, end: float,
                   lead: float = 0.0) -> list[dict]:
    """Mots de la phrase calés sur les instants donnés par la voix (Edge), sinon répartis à la longueur.
    bounds : [(début s, durée s, mot lu)] depuis le début de l'audio de la phrase ; lead : silence retiré."""
    words = text.split()
    tokens = [(o - lead, d, t) for o, d, t in bounds if _norm(t)]
    if not words or len(tokens) != len([w for w in words if _norm(w)]):
        return word_times(text, start, end)
    out, k = [], 0
    for w in words:
        if not _norm(w):  # ponctuation seule (« – ») : collée au mot précédent
            if out:
                out[-1]["word"] += " " + w
            continue
        o, d, _ = tokens[k]
        k += 1
        a = min(max(start, start + o), end)
        out.append({"start": round(a, 3), "end": round(min(end, max(a + 0.05, start + o + d)), 3), "word": w})
    for i in range(len(out) - 1):  # pas de trou entre deux mots (le mot reste surligné jusqu'au suivant)
        out[i]["end"] = max(out[i]["end"], out[i + 1]["start"])
    return out


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


def edge_rate(speed: float) -> str:
    return f"{round((speed - 1) * 100):+d}%"


def edge_synth(text: str, voice: str, speed: float = 1.0, pitch: str = "+0Hz"):
    """Voix neuronale Edge : (audio float 24 kHz, sr, [(début s, durée s, mot)])."""
    import subprocess

    import edge_tts

    com = edge_tts.Communicate(text, voice, rate=edge_rate(speed), pitch=pitch, boundary="WordBoundary",
                               receive_timeout=60)
    mp3, bounds = bytearray(), []
    for chunk in com.stream_sync():
        if chunk["type"] == "audio":
            mp3 += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            bounds.append((chunk["offset"] / 1e7, chunk["duration"] / 1e7, chunk["text"]))
    if not mp3:
        raise RuntimeError("voix Edge : aucun son reçu")
    sr = 24000
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                          input=bytes(mp3), capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy(), sr, bounds


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


def default_synth(kind: str = "news", speed: float | None = None):
    """Fonction de lecture selon NEWS_TTS : Edge (avec secours Piper phrase par phrase) ou Piper."""
    s = get_settings()
    speed = speed or s.news_voice_speed
    piper_voice = []

    def piper(text: str):
        if not piper_voice:
            piper_voice.append(_voice(s.news_voice))
        return _synth(piper_voice[0], text, speed)

    if (s.news_tts or "").lower() != "edge":
        return piper
    voice = s.story_edge_voice if kind == "story" else s.news_edge_voice
    pitch = s.story_edge_pitch if kind == "story" else "+0Hz"
    failed = []

    def edge(text: str):
        if not failed:
            try:
                return edge_synth(text, voice, speed, pitch)
            except Exception as e:  # coupure réseau, service changé… : voix locale pour toute la vidéo
                log.warning("Voix Edge indisponible (%s), voix locale Piper à la place.", e)
                failed.append(e)
        return piper(text)

    edge.failed = failed  # type: ignore[attr-defined]
    return edge


def speak(sentences: list[str], out_wav: Path, synth=None, speed: float | None = None, kind: str = "news",
          gap: float | None = None) -> list[Spoken]:
    """Lit les phrases à la suite dans out_wav (mono 16 bits). synth(text) -> (audio float, sr[, instants des
    mots]) remplaçable (tests). speed : débit (défaut NEWS_VOICE_SPEED). Renvoie le minutage de chaque phrase
    et de chaque mot."""
    if synth is None:
        synth = default_synth(kind, speed)
    gap = GAP if gap is None else gap
    chunks, spoken, t, sr = [], [], 0.0, 22050
    for text in sentences:
        res = synth(text)
        audio, sr = res[0], res[1]
        bounds = res[2] if len(res) > 2 else None
        a, b = trim_bounds(audio, sr)
        audio = audio[a:b]
        if audio.size == 0:
            continue
        dur = audio.size / sr
        words = boundary_times(text, bounds, t, t + dur, lead=a / sr) if bounds else word_times(text, t, t + dur)
        spoken.append(Spoken(text, round(t, 3), round(t + dur, 3), words))
        chunks += [audio, np.zeros(int(gap * sr), dtype=np.float32)]
        t += dur + gap
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
