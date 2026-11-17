from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import db, platforms, worker
from app.models import Clip, Post, Video
from app.web import main, mfa


def _clips(prefix, scores):
    ids = []
    with db.session() as s:
        for i, sc in enumerate(scores):
            v = Video(key=f"{prefix}:{i}", url="https://twitch.tv/x", title="GTA RP", status="done")
            s.add(v)
            s.commit()
            c = Clip(video_id=v.id, group=f"{prefix}{i}", start=0, end=60, score=sc, title=f"{prefix} {sc}",
                     path=__file__)
            s.add(c)
            s.commit()
            s.add(Post(clip_id=c.id, platform="youtube"))
            s.commit()
            ids.append(c.id)
    return ids


def _clear():
    with db.session() as s:
        for p in s.exec(worker.select(Post)).all():
            if p.status in ("pending", "published"):
                p.status = "cancelled"
                s.add(p)
        s.commit()


def test_quota_goes_to_best_clips(monkeypatch):
    _clear()
    monkeypatch.setattr(worker, "connected", lambda p: True)
    monkeypatch.setattr(worker, "slots_left", lambda s, p: 2)
    platforms.set_enabled("youtube", True)
    ids = _clips("best", [0.3, 0.9, 0.5, 0.7])
    with db.session() as s:
        pl = worker.plan(s)["youtube"]
        posts = {s.get(Post, pid).clip_id: r for pid, r in pl["today"].items()}
        assert posts == {ids[1]: 1, ids[3]: 2}
        manual = {s.get(Post, pid).clip_id for pid in pl["manual"]}
        assert manual == {ids[0], ids[2]}
        assert s.get(Clip, worker.next_post(s, "youtube").clip_id).id == ids[1]
    _clear()


def test_slots_left(monkeypatch):
    _clear()
    cfg = worker.get_settings()
    monkeypatch.setattr(cfg, "publish_hours", "12-14,18-22")
    monkeypatch.setattr(cfg, "min_minutes_between_posts", 90)
    monkeypatch.setattr(cfg, "max_posts_per_day", 6)
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    tz = ZoneInfo(cfg.timezone)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, 13, 0, tzinfo=tz)
    monkeypatch.setattr(worker, "datetime", FakeDT)
    with db.session() as s:
        # 13:00 puis 14:30 (hors fenêtre) → 18:00, 19:30, 21:00 : 4 envois, sous le quota de 6
        assert worker.slots_left(s, "youtube") == 4
    assert tz


def test_manual_mark_and_views(monkeypatch):
    _clear()
    monkeypatch.setattr(worker, "connected", lambda p: True)
    monkeypatch.setattr(worker, "slots_left", lambda s, p: 1)
    platforms.set_enabled("youtube", True)
    ids = _clips("man", [0.8, 0.4])
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))

    r = client.get("/clips?vue=manuel")
    assert r.status_code == 200 and "man 0.4" in r.text and "man 0.8" not in r.text
    assert "à poster à la main" in r.text and "Textes à copier" in r.text
    assert "man 0.8" in client.get("/clips?vue=auto").text

    d = client.get(f"/clips/{ids[1]}/download")
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]

    r = client.post(f"/clips/{ids[1]}/manual", data={"platform": "youtube"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clips?vue=manuel"
    with db.session() as s:
        p = s.exec(worker.select(Post).where(Post.clip_id == ids[1])).one()
        assert p.status == "manual" and p.published_at
        assert worker.used_today(s, "youtube") == 0  # ne compte pas dans le quota
        assert ids[1] not in [c.id for _, c in worker.queue(s, "youtube")]
    assert "man 0.4" in client.get("/clips?vue=publies").text
    assert "man 0.4" not in client.get("/clips?vue=manuel").text

    client.post(f"/clips/{ids[1]}/manual", data={"platform": "youtube", "undo": "1"})
    with db.session() as s:
        assert s.exec(worker.select(Post).where(Post.clip_id == ids[1])).one().status == "pending"

    # TikTok sans envoi prévu : la marque crée l'envoi « posté à la main »
    client.post(f"/clips/{ids[0]}/manual", data={"platform": "tiktok"})
    with db.session() as s:
        assert s.exec(worker.select(Post).where(Post.clip_id == ids[0], Post.platform == "tiktok")).one().status == "manual"
    assert client.post(f"/clips/{ids[0]}/manual", data={"platform": "x"}).status_code == 400
    assert client.get("/").status_code == 200
    _clear()
