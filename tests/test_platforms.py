from app import db, platforms, worker
from app.models import Clip, Post, Video


def _clip(key):
    with db.session() as s:
        v = Video(key=key, url="u", title="GTA 6 day one", status="done")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group=key, start=0, end=60, score=0.5, title=key, path=__file__)
        s.add(c)
        s.commit()
        s.add(Post(clip_id=c.id, platform="tiktok"))
        s.add(Post(clip_id=c.id, platform="youtube"))
        s.commit()
        return c.id


def test_defaults_tiktok_off():
    assert platforms.DEFAULTS == {"youtube": True, "tiktok": False}


def test_paused_platform_keeps_clips_pending(monkeypatch):
    cid = _clip("plat:1")
    sent = []
    monkeypatch.setattr(worker, "connected", lambda p: True)
    monkeypatch.setattr(worker, "can_post", lambda s, p: True)
    monkeypatch.setitem(worker.PUBLISHERS, "tiktok", lambda clip, d, t: sent.append("tiktok") or "id")
    monkeypatch.setitem(worker.PUBLISHERS, "youtube", lambda clip, d, t: sent.append("youtube") or "id")

    platforms.set_enabled("tiktok", False)
    platforms.set_enabled("youtube", True)
    for p in ("youtube", "tiktok"):
        worker.publish_once(p)
    assert sent == ["youtube"]
    with db.session() as s:
        tt = s.exec(Post.__table__.select().where(Post.clip_id == cid, Post.platform == "tiktok")).first()
        assert tt.status == "pending" and tt.attempts == 0

    platforms.set_enabled("tiktok", True)
    worker.publish_once("tiktok")
    assert sent == ["youtube", "tiktok"]


def test_drop_pending_and_web(monkeypatch):
    from app.web import main

    _clip("plat:2")
    platforms.set_enabled("tiktok", False)
    assert platforms.drop_pending("tiktok") >= 1
    st = {p["name"]: p for p in platforms.status()}
    assert st["tiktok"]["pending"] == 0 and not st["tiktok"]["on"]

    html = main.templates.env.get_template("_platforms.html").render(platforms=platforms.status(), next="/")
    assert "TikTok" in html and "Activer" in html
    r = main.platform_toggle("tiktok", on="1", next="/accounts")
    assert r.status_code == 303 and r.headers["location"] == "/accounts"
    assert platforms.enabled("tiktok")
    assert main.platform_toggle("youtube", on="0", next="https://evil").headers["location"] == "/"
    assert not platforms.enabled("youtube")
    platforms.set_enabled("youtube", True)
