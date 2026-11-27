"""Reprise du travail après un redémarrage du worker (mise à jour du serveur)."""
import subprocess

import numpy as np
from sqlmodel import select

from app import db, worker
from app.config import get_settings
from app.models import Clip, Video
from app.pipeline import llm, media, process, render, transcribe
from tests.test_pipeline import make_segments


def setup(monkeypatch, tmp_path, key):
    with db.session() as s:
        v = Video(key=key, url="u", title="t", status="downloaded", path=str(tmp_path / "source.mp4"))
        s.add(v)
        s.commit()
    monkeypatch.setattr(media, "duration", lambda p: 600.0)
    monkeypatch.setattr(media, "extract_audio", lambda a, b: None)
    monkeypatch.setattr(media, "loudness_per_second", lambda w: np.random.default_rng(1).normal(-30, 3, 600))
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: make_segments(600))
    monkeypatch.setattr(llm, "provider", lambda: "none")
    monkeypatch.setattr(get_settings(), "auto_publish", False)
    monkeypatch.setattr(get_settings(), "min_score", 0)
    monkeypatch.setattr(get_settings(), "clips_per_video", 30)
    monkeypatch.setattr(get_settings(), "clips_max_per_video", 5)
    return v.id


def clips(vid):
    with db.session() as s:
        return s.exec(select(Clip).where(Clip.video_id == vid)).all()


def test_render_resumes_without_duplicates(monkeypatch, tmp_path):
    vid = setup(monkeypatch, tmp_path, "resume:render")
    calls = []

    def stop_after_first(*a, **k):
        calls.append(a)
        process.stopping.set()  # arrêt demandé pendant le rendu du 1er clip

    monkeypatch.setattr(render, "render", stop_after_first)
    try:
        try:
            process.process(vid)
            raise AssertionError("aurait dû s'interrompre")
        except process.Interrupted:
            pass
    finally:
        process.stopping.clear()
    first = clips(vid)
    assert len(first) == 1
    assert (process.video_dir(vid) / "analysis.json").exists()

    # au redémarrage : l'analyse n'est pas refaite et le clip déjà rendu n'est pas recréé
    monkeypatch.setattr(media, "loudness_per_second", lambda w: (_ for _ in ()).throw(AssertionError("analyse refaite")))
    monkeypatch.setattr(render, "render", lambda *a, **k: calls.append(a))
    n = process.process(vid)
    all_clips = clips(vid)
    assert n == len(all_clips) > 1
    assert len({(c.group, c.part) for c in all_clips}) == len(all_clips)
    assert first[0].id in {c.id for c in all_clips}
    assert len(calls) == len(all_clips)
    assert not (process.video_dir(vid) / "analysis.json").exists()


def test_interrupted_recording_restarts_and_is_joined(tmp_path, monkeypatch):
    with db.session() as s:
        v = Video(key="resume:live", url="u", title="live", is_live=True, status="recording")
        s.add(v)
        s.commit()
        vid = v.id
    work = process.video_dir(vid)
    work.mkdir(parents=True, exist_ok=True)

    def ts(path, freq):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
                        "-c:a", "mp2", "-f", "mpegts", str(path)], check=True)

    ts(work / "source.mp4", 440)
    worker.reset_interrupted()
    with db.session() as s:
        assert s.get(Video, vid).status == "new"  # l'enregistrement est relancé
    assert (work / "part01.mp4").exists() and not (work / "source.mp4").exists()

    ts(work / "source.mp4", 880)  # 2e morceau enregistré après le redémarrage
    out = worker.finish_recording(vid)
    assert out.name == "source.ts" and not list(work.glob("part*"))
    assert 1.8 < media.duration(out) < 2.3


def test_shutdown_stops_processing(monkeypatch):
    monkeypatch.setattr(worker, "stop", worker.threading.Event())
    monkeypatch.setattr(process, "stopping", process.threading.Event())
    worker.shutdown()
    assert worker.stop.is_set() and process.stopping.is_set()
    worker.process_loop()  # rend la main tout de suite
