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


def test_no_limit_by_default_and_ui_limit(monkeypatch):
    _clear()
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    monkeypatch.setattr(worker, "in_publish_window", lambda at=None: True)
    assert worker.get_settings().max_posts_per_day == 100
    monkeypatch.setattr(worker.get_settings(), "max_posts_per_day", 0)
    platforms.set_daily_limit("youtube", None)
    assert worker.post_limit("youtube") == 0
    with db.session() as s:
        for i in range(10):
            s.add(Post(clip_id=1, platform="youtube", status="published",
                       published_at=worker.now() - worker.timedelta(hours=3 + i)))
        s.commit()
        assert worker.can_post(s, "youtube")  # 10 envois sur 24 h, pas de plafond
        platforms.set_daily_limit("youtube", 5)
        assert worker.post_limit("youtube") == 5 and not worker.can_post(s, "youtube")
        assert worker.slots_left(s, "youtube") == 0
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    client.post("/platforms/youtube/limit", data={"limit": ""})
    assert platforms.daily_limit("youtube") is None
    client.post("/platforms/youtube/limit", data={"limit": "0"})
    assert platforms.daily_limit("youtube") == 0
    platforms.set_daily_limit("youtube", None)
    _clear()


def test_quota_exceeded_pauses_until_tomorrow(monkeypatch):
    from googleapiclient.errors import HttpError
    from app.publish import youtube

    _clear()
    platforms.set_enabled("youtube", True)
    monkeypatch.setattr(worker, "connected", lambda p: True)
    monkeypatch.setattr(worker, "in_publish_window", lambda at=None: True)
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    sent = []
    monkeypatch.setattr(worker.notify, "send", lambda m: sent.append(m))

    class Resp(dict):
        status = 403
        reason = "Forbidden"
    err = HttpError(Resp(), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
    assert youtube.is_quota_error(err)

    def boom(*a):
        raise youtube.QuotaExceeded("quota")
    monkeypatch.setitem(worker.PUBLISHERS, "youtube", boom)
    ids = _clips("quota", [0.9, 0.8])
    assert not worker.publish_once("youtube")
    with db.session() as s:
        posts = s.exec(worker.select(Post).where(worker.col(Post.clip_id).in_(ids))).all()
        assert all(p.status == "pending" and p.attempts == 0 for p in posts)
        assert not worker.can_post(s, "youtube") and worker.slots_left(s, "youtube") == 0
        pl = worker.plan(s)["youtube"]
        assert pl["blocked"] and len(pl["manual"]) == 2
    assert platforms.blocked_until("youtube") > worker.now()
    assert sent and "Quota" in sent[0]

    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    assert "reprise le" in client.get("/clips").text
    client.post("/platforms/youtube/limit", data={"resume": "1"})
    assert platforms.blocked_until("youtube") is None
    _clear()


def test_schedule_from_panel(monkeypatch):
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    assert platforms.valid_hours("12-14,18-22:30") and platforms.valid_hours("")
    assert not platforms.valid_hours("12h-14h") and not platforms.valid_hours("25-26")
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    r = client.post("/schedule", data={"hours": "9-11, 20-23", "gap": "45"}, follow_redirects=False)
    assert r.headers["location"] == "/clips?sched=ok"
    assert platforms.publish_hours() == "9-11,20-23" and platforms.min_gap() == 45
    tz = ZoneInfo(worker.get_settings().timezone)
    assert worker.in_publish_window(datetime(2026, 11, 17, 10, 0, tzinfo=tz))
    assert not worker.in_publish_window(datetime(2026, 11, 17, 13, 0, tzinfo=tz))
    assert worker.post_gap() == 45
    r = client.post("/schedule", data={"hours": "midi", "next": "/accounts"}, follow_redirects=False)
    assert r.headers["location"] == "/accounts?sched=ko" and platforms.publish_hours() == "9-11,20-23"
    assert "9-11,20-23" in client.get("/clips").text and "9-11,20-23" in client.get("/accounts").text
    client.post("/schedule", data={"reset": "1"})
    assert platforms.publish_hours() == worker.get_settings().publish_hours
    assert platforms.min_gap() == worker.get_settings().min_minutes_between_posts


def test_youtube_expired_token_waits_for_reconnect(monkeypatch):
    from google.auth.exceptions import RefreshError

    from app.config import get_settings
    from app.publish import creds, youtube

    _clear()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "youtube_client_id", "id")
    monkeypatch.setattr(cfg, "youtube_client_secret", "secret")
    platforms.set_enabled("youtube", True)
    monkeypatch.setattr(worker, "in_publish_window", lambda at=None: True)
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    sent = []
    monkeypatch.setattr(worker.notify, "send", lambda m: sent.append(m))
    creds.save("youtube", {"token": None, "refresh_token": "r", "scopes": youtube.SCOPES}, account="Ma chaîne")

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.",
                           {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    monkeypatch.setattr(youtube.Credentials, "refresh", refresh)
    monkeypatch.setitem(worker.PUBLISHERS, "youtube", lambda clip, d, t: youtube.upload(clip.path, clip.title, d, t))
    ids = _clips("expired", [0.9, 0.8])
    # une ancienne publication abandonnée pour la même raison (avant ce correctif)
    with db.session() as s:
        old = s.exec(worker.select(Post).where(Post.clip_id == ids[1])).first()
        old.status, old.attempts, old.error = "failed", 3, "('invalid_grant: Token has been expired or revoked.', {})"
        s.add(old)
        s.commit()
    assert not worker.publish_once("youtube")
    with db.session() as s:
        post = s.exec(worker.select(Post).where(Post.clip_id == ids[0])).first()
        assert post.status == "pending" and post.attempts == 0
    assert youtube.expired() and not worker.connected("youtube")
    assert len(sent) == 1 and "Reconnecte" in sent[0]
    assert not worker.publish_once("youtube") and len(sent) == 1  # en pause, pas de nouveau message

    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    assert "Reconnecter ma chaîne YouTube" in client.get("/accounts").text
    # reconnexion : l'accès repart et l'envoi abandonné revient en file
    creds.save("youtube", {"token": "new", "refresh_token": "r2", "scopes": youtube.SCOPES}, account="Ma chaîne")
    assert youtube.requeue_auth_failures() == 1 and not youtube.expired() and worker.connected("youtube")
    with db.session() as s:
        assert s.get(Post, old.id).status == "pending"
    creds.delete("youtube")
    _clear()
