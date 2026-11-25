"""Seulement les meilleurs Shorts : seuil strict, relecture par l'IA avant l'envoi, analyse de chaque Short publié."""
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import delete

from app import curate, db, learning, notify
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, PostStat, Tuning, Video, now
from app.pipeline import llm


def _cleanup():
    with db.session() as s:
        for m in (PostStat, Post, ClipInfo, Clip, Tuning):
            s.exec(delete(m))
        s.commit()


def _video(s, key):
    v = Video(key=key, url="u", title="GTA 6 RP", status="done")
    s.add(v)
    s.commit()
    return v


def test_only_top_clips_and_polish(monkeypatch):
    _cleanup()
    cfg = get_settings()
    for k, v in {"publish_top_pct": 20, "quality_min_ref": 10, "publish_min_llm": 7, "polish": True,
                 "polish_per_run": 4}.items():
        monkeypatch.setattr(cfg, k, v)
    with db.session() as s:
        v = _video(s, "bestonly:1")
        ids = []
        for i in range(10):  # scores 0.0 … 0.9 : seuls 0.8 et 0.9 sont dans les 20 % meilleurs
            c = Clip(video_id=v.id, group=f"b{i}", start=0, end=30, score=i / 10, title=f"GTA 6 : moment {i}",
                     path=__file__)
            s.add(c)
            s.commit()
            s.add(ClipInfo(clip_id=c.id, data=json.dumps({"llm": 8, "text": f"il se passe un truc fou numéro {i}",
                                                          "hook": "Personne ne s'y attendait"})))
            p = Post(clip_id=c.id, platform="youtube")
            s.add(p)
            s.commit()
            ids.append((c.id, p.id))
    assert curate.score_bar() == 0.8
    asked = []

    def fake_polish(text, title, hook="", guidance="", recent=None):
        asked.append(title)
        return (4, "x", "") if "9" in title else (8, "GTA 6 : il braque la banque en hélico", "Tu l'aurais fait ?")

    monkeypatch.setattr(llm, "polish", fake_polish)
    out = curate.run()
    assert out["polished"] == 2 and len(asked) == 2  # seuls les clips qui passent le seuil sont relus
    with db.session() as s:
        st = {i: s.get(Post, p).status for i, (_, p) in enumerate(ids)}
        assert [i for i, x in st.items() if x == "pending"] == [8]
        assert "meilleurs" in s.get(Post, ids[3][1]).error
        assert "relecture" in s.get(Post, ids[9][1]).error  # recalé au 2e avis
        assert s.get(Clip, ids[8][0]).title == "GTA 6 : il braque la banque en hélico"
        info = json.loads(s.get(ClipInfo, ids[8][0]).data)
        assert info["polished"] and info["polish_score"] == 8 and info["question"] == "Tu l'aurais fait ?"
    assert curate.run()["polished"] == 0  # jamais relu deux fois
    _cleanup()


def test_learned_min_llm(monkeypatch):
    _cleanup()
    monkeypatch.setattr(get_settings(), "publish_min_llm", 7)
    with db.session() as s:
        s.add(Tuning(samples=10, data=json.dumps({"min_llm": 8})))
        s.commit()
    assert curate.min_llm() == 8
    monkeypatch.setattr(get_settings(), "publish_min_llm", 0)
    assert curate.min_llm() == 0  # minimum coupé : l'apprentissage ne le rallume pas
    _cleanup()


def test_reviews_bursts_and_notification(monkeypatch):
    _cleanup()
    monkeypatch.setattr(get_settings(), "burst_minutes", 20)
    monkeypatch.setattr(get_settings(), "review_age_hours", 24)
    t0 = now() - timedelta(days=2)
    with db.session() as s:
        v = _video(s, "rev:1")
        plan = [(0, 886, 92, 40, 6), (120, 555, 70, 20, 0), (240, 401, 60, 10, 1), (360, 80, 30, 1, 0)]
        plan += [(362 + 2 * k, 0, None, 0, 0) for k in range(4)]  # rafale : toutes les 2 min, 0 vue
        for k, (mins, views, ret, likes, com) in enumerate(plan):
            c = Clip(video_id=v.id, group=f"r{k}", start=0, end=32, title=f"GTA 6 : clip {k}", path="")
            s.add(c)
            s.commit()
            s.add(ClipInfo(clip_id=c.id, data=json.dumps({"llm": 8})))
            p = Post(clip_id=c.id, platform="youtube", status="published", remote_id=f"r{k}",
                     published_at=t0 + timedelta(minutes=mins))
            s.add(p)
            s.commit()
            s.add(PostStat(post_id=p.id, fetched_at=now(), views=views, likes=likes, comments=com,
                           avg_view_pct=ret))
        recent = Post(clip_id=c.id, platform="tiktok", status="published", remote_id="t", published_at=now())
        s.add(recent)
        s.commit()
    revs = learning.reviews()
    by_views = {r.get("views"): r for r in revs.values()}
    assert by_views[886]["verdict"] == "top" and any("boucle" in x for x in by_views[886]["reasons"])
    assert by_views[80]["verdict"] == "flop"
    bursts = [r for r in revs.values() if r["burst"]]
    assert len(bursts) == 4 and all(r["verdict"] == "flop" for r in bursts)
    assert any("rafale" in x for x in bursts[0]["reasons"]) and any("0 vue" in x for x in bursts[0]["reasons"])
    assert revs[recent.id]["verdict"] == "early"
    assert "rafale" in learning.burst_effect(revs)
    # les rafales ne servent pas à apprendre
    perf_clips = set(learning.performances())
    with db.session() as s:
        burst_clips = {s.get(Post, pid).clip_id for pid, r in revs.items() if r["burst"]}
    assert not perf_clips & burst_clips

    sent = []
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    assert learning.notify_reviews() == 8
    assert len(sent) == 1 and "Bilan de 8 Short" in sent[0] and "886 vues" in sent[0]
    assert learning.notify_reviews() == 0  # une seule fois par Short

    from app.web import main, mfa
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    r = client.get("/stats")
    assert r.status_code == 200 and "Seuil de publication" in r.text and "🔥 Top" in r.text
    _cleanup()


def test_bad_source_gives_zero_clips(monkeypatch, tmp_path):
    import numpy as np

    from app.pipeline import media, process, render, transcribe
    from tests.test_pipeline import make_segments

    with db.session() as s:
        v = Video(key="bestonly:bad", url="u", title="t", status="downloaded", path=str(tmp_path / "source.mp4"))
        s.add(v)
        s.commit()
    monkeypatch.setattr(media, "duration", lambda p: 600.0)
    monkeypatch.setattr(media, "extract_audio", lambda a, b: None)
    monkeypatch.setattr(media, "loudness_per_second", lambda w: np.random.default_rng(1).normal(-30, 3, 600))
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: make_segments(600))
    monkeypatch.setattr(render, "render", lambda *a: None)
    monkeypatch.setattr(get_settings(), "auto_publish", False)
    monkeypatch.setattr(get_settings(), "clip_min_llm", 6)
    monkeypatch.setattr(llm, "provider", lambda: "ollama")
    monkeypatch.setattr(llm, "rate", lambda *a, **k: (4, "Bof", "Bof", ""))  # rien de bon dans cette vidéo
    assert process.process(v.id) == 0
    with db.session() as s:
        assert s.get(Video, v.id).status == "done"


def test_queue_drops_clip_with_placeholder_hook():
    from app import curate
    from app.models import Clip, Video
    v = Video(key="x:1", url="u", title="GTA 6 RP", status="done")
    c = Clip(video_id=1, group="g", start=0, end=30, score=0.9, title="Squeezie règle un conflit dans GTA 6")
    assert "« X »" in curate.reason(c, v, {"hook": "X règle ça à sa façon GTA 6 🤯", "llm": 9})
    assert "« X »" not in curate.reason(c, v, {"hook": "Squeezie règle un conflit 🤯", "llm": 9})
