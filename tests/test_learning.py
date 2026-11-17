from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import delete

from app import db, learning
from app.config import get_settings
from app.models import Clip, Post, PostStat, Tuning, Video, now
from app.pipeline import llm


def seed(n=12, key="learn:1"):
    """n clips YouTube : plus le pic de son est fort, plus il y a de vues ; les clips de ~65 s font mieux."""
    t0 = now() - timedelta(days=5)
    with db.session() as s:
        s.exec(delete(Tuning))
        v = Video(key=key, url="u", title="t", status="done")
        s.add(v)
        s.commit()
        for i in range(n):
            dur = 65 if i % 2 else 40
            c = Clip(video_id=v.id, group=f"l{i}", start=0, end=dur, title=f"clip {i}", path="")
            s.add(c)
            s.commit()
            learning.save_clip_info(c.id, {"spike": i / n, "density": 0.3, "excite": ((i * 7) % n) / n,
                                           "heur": 0.5, "llm": None, "hook": f"accroche {i}", "text": "bla"})
            p = Post(clip_id=c.id, platform="youtube", status="published", remote_id=f"yt{i}", published_at=t0)
            s.add(p)
            s.commit()
            views = 100 * (i + 1) + (5000 if dur == 65 else 0)
            s.add(PostStat(post_id=p.id, fetched_at=t0 + timedelta(hours=72), views=views, likes=views // 20))
            s.add(PostStat(post_id=p.id, fetched_at=t0 + timedelta(hours=6), views=1, likes=0))
        s.commit()


def test_spearman():
    assert learning.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert learning.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert learning.spearman([1, 1, 1], [1, 2, 3]) == 0.0


def test_learns_from_stats(monkeypatch):
    seed()
    monkeypatch.setattr(llm, "analyze", lambda top, flop: ["Miser sur les cris"])
    res = learning.learn()
    assert res["learned"], res
    t = learning.current()
    # le pic de son est le signal qui prédit le mieux : son poids augmente
    assert t["weights"]["spike"] > 0.45
    assert abs(sum(t["weights"].values()) - 1) < 0.01
    # les clips de 60-75 s marchent mieux : la durée visée s'en rapproche
    assert get_settings().clip_target < t["clip_target"] <= 67
    assert t["rules"] == ["Miser sur les cris"]
    assert t["top"][0]["title"] == "clip 11"
    g = learning.guidance(t)
    assert "Miser sur les cris" in g and "clip 11" in g
    # rien de neuf : pas de nouvelle analyse
    assert not learning.learn()["learned"]

    client = TestClient(__import__("app.web.main", fromlist=["app"]).app)
    s = get_settings()
    r = client.get("/stats", auth=(s.admin_user, s.admin_password))
    assert r.status_code == 200 and "Miser sur les cris" in r.text


def test_not_enough_data_keeps_defaults():
    with db.session() as s:
        s.exec(delete(Tuning))
        s.commit()
    assert learning.current()["weights"] == learning.defaults()["weights"]


def test_fetch_stats(monkeypatch):
    seed(3, "learn:2")
    monkeypatch.setitem(learning.FETCHERS, "youtube", (lambda: True, lambda ids: {i: {"views": 7} for i in ids}))
    monkeypatch.setitem(learning.FETCHERS, "tiktok", (lambda: False, lambda ids: {}))
    monkeypatch.setattr(learning.creds, "load", lambda p: {"token": "x"})
    with db.session() as s:
        s.exec(delete(PostStat))
        s.commit()
    # publications d'il y a 5 jours (dans la fenêtre de 30 jours)
    assert learning.fetch_stats() >= 3


def test_process_uses_tuning_and_saves_info(monkeypatch, tmp_path):
    import json

    import numpy as np

    from app.models import ClipInfo
    from app.pipeline import media, process, render, transcribe
    from tests.test_pipeline import make_segments

    with db.session() as s:
        s.exec(delete(Tuning))
        s.add(Tuning(samples=10, data=json.dumps({"weights": {"spike": 0.6, "density": 0.2, "excite": 0.2},
                                                  "llm_mix": 0.7, "clip_target": 50, "rules": ["r"]})))
        v = Video(key="learn:proc", url="u", title="t", status="downloaded", path=str(tmp_path / "source.mp4"))
        s.add(v)
        s.commit()
    monkeypatch.setattr(media, "duration", lambda p: 600.0)
    monkeypatch.setattr(media, "extract_audio", lambda a, b: None)
    monkeypatch.setattr(media, "loudness_per_second", lambda w: np.random.default_rng(1).normal(-30, 3, 600))
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: make_segments(600))
    monkeypatch.setattr(render, "render", lambda *a: None)
    monkeypatch.setattr(get_settings(), "auto_publish", False)
    seen = {}
    monkeypatch.setattr(llm, "provider", lambda: "ollama")
    monkeypatch.setattr(llm, "rate", lambda text, title, which, guidance="": seen.setdefault("g", guidance) and (8, "Titre", "Accroche"))
    assert process.process(v.id) >= 1
    assert "r" in seen["g"]
    with db.session() as s:
        clip = s.exec(__import__("sqlmodel").select(Clip).where(Clip.video_id == v.id)).first()
        info = json.loads(s.get(ClipInfo, clip.id).data)
    assert info["llm"] == 8 and info["hook"] == "Accroche" and "spike" in info
    assert 50 * 0.85 - 1 <= info["target"] <= 50 * 1.15 + 1
