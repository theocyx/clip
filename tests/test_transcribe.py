import logging
from types import SimpleNamespace

from app.pipeline import transcribe


def test_progress_is_logged(monkeypatch, tmp_path, caplog):
    segs = [SimpleNamespace(start=i * 60, end=i * 60 + 60, text="salut", words=[]) for i in range(3)]
    monkeypatch.setattr(transcribe, "_get_model", lambda: SimpleNamespace(transcribe=lambda *a, **k: (iter(segs), None)))
    clock = iter(range(0, 1000, 40))
    monkeypatch.setattr(transcribe.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO):
        out = transcribe.transcribe(tmp_path / "a.wav", tmp_path / "t.json", total=600)
    assert len(out) == 3
    assert "0:01:00 / 0:10:00 (10 %)" in caplog.text
