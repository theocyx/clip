"""Interactions : 1er commentaire de la chaîne, partage, essais jugés sur les likes / commentaires / partages,
clips retirés trop vite remis en réserve."""
import json
from datetime import timedelta

from sqlmodel import delete, select

from app import curate, db, experiments, growth, interact, learning, platforms, worker
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, PostStat, Setting, Tuning, Video, now
from app.publish import youtube


def _cleanup():
    with db.session() as s:
        for m in (PostStat, Post, ClipInfo, Clip, Tuning):
            s.exec(delete(m))
        for v in s.exec(select(Video)).all():
            s.delete(v)
        for k in (curate.RESCUE_KEY, growth.CHANNEL_KEY, growth.MIX_KEY):
            row = s.get(Setting, k)
            if row:
                s.delete(row)
        s.commit()


def _clip(s, key, info, title="Domingo GTA 6 : la poursuite", published_min=None, created=None):
    v = s.exec(select(Video).where(Video.key == key)).first()
    if not v:
        v = Video(key=key, url="https://twitch.tv/videos/1", title="GTA 6 RP", status="done")
        s.add(v)
        s.commit()
    c = Clip(video_id=v.id, group=f"g{title}{published_min}", start=0, end=40, score=0.5, title=title,
             path=__file__, created_at=created or now())
    s.add(c)
    s.commit()
    s.add(ClipInfo(clip_id=c.id, data=json.dumps(info)))
    p = Post(clip_id=c.id, platform="youtube")
    if published_min is not None:
        p.status, p.remote_id, p.published_at = "published", f"yt{c.id}", now() - timedelta(minutes=published_min)
    s.add(p)
    s.commit()
    return c, p


def test_comment_text():
    assert interact.comment_text({"question": "Il aurait dû fuir ou se rendre ?"}) == \
        "Il aurait dû fuir ou se rendre ? 👇"
    assert interact.comment_text({"kind": "news"}, "Grève SNCF") in [q + " 👇" for q in interact.NEWS_QUESTIONS]
    assert interact.comment_text({"kind": "news", "story": True}, "Dyatlov").endswith("? 👇")
    assert "numéro" in interact.comment_text({"kind": "compil"})
    assert interact.comment_text({"text": "la police le course"}, "x").endswith("? 👇")  # question de repli


def test_first_comment_posted_once(monkeypatch):
    _cleanup()
    with db.session() as s:
        old, _ = _clip(s, "fc:1", {"question": "Tu lui pardonnes ?"}, title="A", published_min=10)
        fresh, _ = _clip(s, "fc:1", {"question": "Team lui ou team l'autre ?"}, title="B", published_min=1)
    sent = []
    monkeypatch.setattr(youtube, "configured", lambda: True)
    monkeypatch.setattr(youtube, "auth_error", lambda: "")
    monkeypatch.setattr(youtube, "can_comment", lambda: False)
    monkeypatch.setattr(youtube, "comment", lambda vid, text: sent.append((vid, text)) or "c1")
    assert interact.run() == 0  # autorisation pas encore accordée : rien
    monkeypatch.setattr(youtube, "can_comment", lambda: True)
    assert interact.run() == 1 and sent == [(f"yt{old.id}", "Tu lui pardonnes ? 👇")]  # la récente attend 3 min
    assert interact.run() == 0  # jamais deux fois
    with db.session() as s:
        info = json.loads(s.get(ClipInfo, old.id).data)
    assert info["comment_id"] == "c1" and info["question"] == "Tu lui pardonnes ?"

    def boom(vid, text):
        raise RuntimeError("commentsDisabled")

    monkeypatch.setattr(get_settings(), "first_comment_delay", 0)
    monkeypatch.setattr(youtube, "comment", boom)
    for _ in range(interact.TRIES + 1):
        interact.run()
    with db.session() as s:
        assert json.loads(s.get(ClipInfo, fresh.id).data)["comment_tries"] == interact.TRIES  # abandonne après 3
    _cleanup()


def test_share_line_in_description():
    _cleanup()
    with db.session() as s:
        c, _ = _clip(s, "share:1", {})
        v = s.get(Video, c.video_id)
    desc = worker.description(c, v)[0]
    assert "📤 Envoie-le à un pote qui joue à GTA" in desc
    _cleanup()


def test_interaction_tests_judged_on_engagement(monkeypatch):
    _cleanup()
    with db.session() as s:
        a, _ = _clip(s, "eng:1", {"exp": {"ask": "on", "zoom": "on", "cta": "on"}}, title="A")
        b, _ = _clip(s, "eng:1", {"exp": {"ask": "off", "zoom": "off", "cta": "partage"}}, title="B")
    perf = {a.id: 0.2, b.id: 0.9}
    table = experiments.results("clip", perf, eng={a.id: 0.95, b.id: 0.1})
    assert table["zoom"] == {"on": [0.2], "off": [0.9]}  # montage : jugé sur la performance générale
    assert table["ask"] == {"on": [0.95], "off": [0.1]}  # question à l'écran : jugée sur les interactions
    assert table["cta"] == {"abonne": [0.95], "partage": [0.1]}  # ancienne valeur « on » = « abonne »
    assert "ask" not in experiments.results("clip", perf, eng={})  # trop peu de vues pour juger : pas compté
    _cleanup()


def test_engagement_ranks_comments_and_shares_higher():
    _cleanup()
    with db.session() as s:
        ids = []
        for i, (likes, comments, shares) in enumerate(((10, 0, 0), (2, 3, 0), (0, 0, 4), (1, 0, 0))):
            c, p = _clip(s, "engrank:1", {}, title=f"E{i}", published_min=60 * 80 + i * 60)
            s.add(PostStat(post_id=p.id, views=500, likes=likes, comments=comments, shares=shares,
                           fetched_at=p.published_at + timedelta(hours=72)))
            s.commit()
            ids.append(c.id)
    eng = learning.engagement()
    assert eng[ids[2]] > eng[ids[1]] > eng[ids[3]]  # 4 partages > 2 likes + 3 commentaires > 1 like
    assert learning.interaction_rate(PostStat(post_id=0, views=100, likes=1, comments=1, shares=1)) == 0.06
    _cleanup()


def test_recently_discarded_clips_come_back_once(monkeypatch):
    _cleanup()
    cfg = get_settings()
    for k, v in {"reserve": True, "publish_min_llm": 7, "reserve_max": 20, "queue_max_hours": 36}.items():
        monkeypatch.setattr(cfg, k, v)
    with db.session() as s:
        s.add(Tuning(samples=40, data=json.dumps({"min_llm": 8})))
        s.commit()
        near, p1 = _clip(s, "rescue:1", {"llm": 7, "text": "gta 6"}, title="near")
        weak, p2 = _clip(s, "rescue:1", {"llm": 5, "text": "gta 6"}, title="weak")
        old, p3 = _clip(s, "rescue:1", {"llm": 7, "text": "gta 6"}, title="old",
                        created=now() - timedelta(hours=50))
        for p in (p1, p2, p3):
            p = s.get(Post, p.id)
            p.status, p.error = "cancelled", "Retiré : Note de l'IA trop basse (7/10, minimum 8)"
            s.add(p)
        s.commit()
    out = curate.run()
    assert out["rescued"] == 2  # le trop vieux reste retiré
    with db.session() as s:
        assert curate.is_reserve(s.get(Post, p1.id))  # 7/10 : en réserve, prêt à partir si la file est vide
        assert s.get(Post, p2.id).status == "cancelled"  # 5/10 : retiré de nouveau (sous PUBLISH_MIN_LLM)
        assert s.get(Post, p3.id).status == "cancelled"
        p = s.get(Post, p1.id)
        p.status, p.error = "cancelled", "Retiré : Note de l'IA trop basse (7/10, minimum 8)"
        s.add(p)
        s.commit()
    assert curate.run()["rescued"] == 0  # une seule fois
    _cleanup()


def test_accounts_page_asks_to_reconnect_for_comments(monkeypatch):
    from fastapi.testclient import TestClient

    from app.web import main, mfa
    monkeypatch.setattr(main.creds, "load", lambda p: {"token": "x"} if p == "youtube" else None)
    monkeypatch.setattr(youtube, "auth_error", lambda: "")
    monkeypatch.setattr(youtube, "can_comment", lambda: False)
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    r = client.get("/accounts")
    assert r.status_code == 200 and "1er commentaire" in r.text
    monkeypatch.setattr(youtube, "can_comment", lambda: True)
    assert "1er commentaire" not in client.get("/accounts").text
