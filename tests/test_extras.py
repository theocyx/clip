import os
from datetime import datetime, timedelta
from pathlib import Path

from app import db, maintenance, notify, worker
from app.config import get_settings
from app.models import Clip, Post, Source, Video, now


def test_publish_window(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "publish_hours", "12-14,18-22:30")
    at = lambda h, m=0: datetime(2026, 11, 17, h, m)  # noqa: E731
    assert worker.in_publish_window(at(12, 0))
    assert worker.in_publish_window(at(22, 15))
    assert not worker.in_publish_window(at(14, 0))
    assert not worker.in_publish_window(at(3))
    monkeypatch.setattr(cfg, "publish_hours", "22-2")  # plage qui passe minuit
    assert worker.in_publish_window(at(1)) and not worker.in_publish_window(at(12))
    monkeypatch.setattr(cfg, "publish_hours", "")
    assert worker.in_publish_window(at(3))


def test_no_post_outside_window(monkeypatch):
    monkeypatch.setattr(worker, "in_publish_window", lambda at=None: False)
    with db.session() as s:
        assert not worker.can_post(s, "youtube")


def test_streamer_credit():
    with db.session() as s:
        src = Source(name="Le Bouseuh", platform="twitch", url="https://www.twitch.tv/lebouseuh")
        s.add(src)
        s.commit()
        v = Video(source_id=src.id, key="cr:1", url="https://twitch.tv/videos/1", title="GTA 6 RP")
        s.add(v)
        s.commit()
    clip = Clip(video_id=v.id, group="g", start=0, end=60, title="Il rate tout")
    desc, tags = worker.description(clip, v)
    assert "Streamer : Le Bouseuh" in desc and "https://www.twitch.tv/lebouseuh" in desc
    assert tags[0] == "LeBouseuh" and "#LeBouseuh" in desc
    cap = worker.tiktok_caption(clip, desc, tags)
    assert cap.startswith("Il rate tout 🎮 Le Bouseuh #LeBouseuh")


def test_notify(monkeypatch):
    cfg = get_settings()
    sent = []

    class Resp:
        is_success, status_code, text = True, 200, ""

    class FakeClient:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json): sent.append((url, json)); return Resp()

    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    assert not notify.configured()
    monkeypatch.setattr(cfg, "discord_webhook_url", "https://discord.test/hook")
    monkeypatch.setattr(cfg, "telegram_bot_token", "T")
    monkeypatch.setattr(cfg, "telegram_chat_id", "42")
    notify.published("youtube", "Mon clip", "abc")
    assert sent[0] == ("https://discord.test/hook", {"content": "✅ Clip publié sur YouTube Shorts : Mon clip\nhttps://youtube.com/shorts/abc"})
    assert sent[1][0] == "https://api.telegram.org/botT/sendMessage" and sent[1][1]["chat_id"] == "42"
    monkeypatch.setattr(cfg, "notify_errors", False)
    notify.error("boom")
    assert len(sent) == 2


def test_backup_rotation(monkeypatch):
    cfg = get_settings()
    db.get_engine()
    monkeypatch.setattr(cfg, "backup_keep", 2)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        assert maintenance.backup(d).exists()
    names = sorted(p.name for p in cfg.backups_dir.glob("clip-*.db"))
    assert names == ["clip-2026-01-02.db", "clip-2026-01-03.db"]


def test_cleanup(monkeypatch):
    cfg = get_settings()
    old = now() - timedelta(days=30)
    with db.session() as s:
        done = Video(key="cl:1", url="u", status="done", updated_at=old)
        busy = Video(key="cl:2", url="u", status="downloading", updated_at=old)
        s.add_all([done, busy])
        s.commit()
        files = {}
        for name, v in (("done", done), ("busy", busy)):
            d = cfg.videos_dir / str(v.id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "source.mp4").write_bytes(b"x" * 10)
            files[name] = d
        cdir = cfg.clips_dir / str(done.id)
        cdir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for k in ("pub", "pending", "never"):
            p = cdir / f"{k}_p1.mp4"
            p.write_bytes(b"y" * 10)
            paths[k] = p
        (cdir / "pub_p1_hook.png").write_bytes(b"z")
        clips = {k: Clip(video_id=done.id, group=k, start=0, end=1, path=str(p), created_at=old) for k, p in paths.items()}
        s.add_all(clips.values())
        s.commit()
        s.add(Post(clip_id=clips["pub"].id, platform="youtube", status="published"))
        s.add(Post(clip_id=clips["pending"].id, platform="youtube", status="pending"))
        s.commit()

    assert maintenance.cleanup() > 0
    assert not files["done"].exists() and files["busy"].exists()
    assert not paths["pub"].exists() and not (cdir / "pub_p1_hook.png").exists()
    assert paths["pending"].exists() and paths["never"].exists()
