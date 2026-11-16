"""Transcription locale avec faster-whisper (CPU, int8), horodatage mot à mot."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)
_model = None


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


def transcribe(wav: Path, out_json: Path) -> list[dict]:
    """Retourne [{start, end, text, words: [{start, end, word}]}] et l'écrit dans out_json."""
    if out_json.exists():
        return json.loads(out_json.read_text())
    s = get_settings()
    segments, _ = _get_model().transcribe(
        str(wav), language=s.whisper_language or None, word_timestamps=True, vad_filter=True, beam_size=1,
    )
    result = []
    for seg in segments:
        words = [{"start": w.start, "end": w.end, "word": w.word.strip()} for w in (seg.words or []) if w.word.strip()]
        result.append({"start": seg.start, "end": seg.end, "text": seg.text.strip(), "words": words})
    out_json.write_text(json.dumps(result, ensure_ascii=False))
    return result
