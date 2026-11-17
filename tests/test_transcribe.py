import json
import logging
import wave
from types import SimpleNamespace

import pytest

from app.pipeline import transcribe


def make_wav(path, seconds: float):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(seconds * 16000))
    return path


def test_progress_is_logged(monkeypatch, tmp_path, caplog):
    segs = [SimpleNamespace(start=i * 60, end=i * 60 + 60, text="salut", words=[]) for i in range(3)]
    monkeypatch.setattr(transcribe, "_get_model", lambda: SimpleNamespace(transcribe=lambda *a, **k: (iter(segs), None)))
    clock = iter(range(0, 1000, 40))
    monkeypatch.setattr(transcribe.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO):
        out = transcribe.transcribe(make_wav(tmp_path / "a.wav", 1), tmp_path / "t.json", total=600)
    assert len(out) == 3
    assert "0:01:00 / 0:10:00 (10 %)" in caplog.text


class FakeModel:
    """Une phrase toutes les 10 s de l'audio reçu ; garde la trace des débuts demandés."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **k):
        dur = len(audio) / 16000
        self.calls.append(dur)
        n = int(dur // 10)
        return iter(SimpleNamespace(start=i * 10.0, end=i * 10.0 + 9, text=f"p{i}",
                                    words=[SimpleNamespace(start=i * 10.0, end=i * 10.0 + 1, word=" mot")])
                    for i in range(n)), None


def test_resumes_after_interruption(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "CHUNK", 100.0)
    model = FakeModel()
    monkeypatch.setattr(transcribe, "_get_model", lambda: model)
    wav = make_wav(tmp_path / "a.wav", 330)
    out = tmp_path / "t.json"

    seen = {"n": 0}

    def stop_after_15():
        seen["n"] += 1
        return seen["n"] > 15

    with pytest.raises(transcribe.Interrupted):
        transcribe.transcribe(wav, out, None, None, stop_after_15)
    assert not out.exists()
    done, pos = transcribe.load_partial(transcribe.partial_path(out))
    assert done and pos > 100  # le premier morceau est sauvegardé

    model.calls.clear()
    segs = transcribe.transcribe(wav, out)
    assert out.exists() and not transcribe.partial_path(out).exists()
    # la reprise ne retranscrit pas le début
    assert sum(model.calls) < 330 - 100
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert segs[-1]["end"] > 300
    assert segs[-1]["words"][0]["start"] == segs[-1]["start"]
    assert json.loads(out.read_text()) == segs


def test_partial_file_with_cut_line(tmp_path):
    p = tmp_path / "t.partial.jsonl"
    p.write_text('{"seg": {"start": 0, "end": 5, "text": "a", "words": []}}\n{"done": 12}\n{"seg": {"sta')
    segs, pos = transcribe.load_partial(p)
    assert len(segs) == 1 and pos == 12
