from app import db, worker
from app.models import Clip, Post, Video


def test_publication_order_and_limits(monkeypatch):
    with db.session() as s:
        v = Video(key="w:1", url="u", title="t", status="done")
        s.add(v)
        s.commit()
        c1 = Clip(video_id=v.id, group="g", part=1, part_count=2, start=0, end=60, score=0.5, title="p1", path=__file__)
        c2 = Clip(video_id=v.id, group="g", part=2, part_count=2, start=60, end=120, score=0.5, title="p2", path=__file__)
        c3 = Clip(video_id=v.id, group="h", part=1, part_count=1, start=200, end=260, score=0.9, title="solo", path=__file__)
        s.add_all([c1, c2, c3])
        s.commit()
        for c in (c1, c2, c3):
            s.add(Post(clip_id=c.id, platform="tiktok"))
        s.commit()

    sent = []
    monkeypatch.setattr(worker, "connected", lambda p: True)
    monkeypatch.setitem(worker.PUBLISHERS, "tiktok", lambda clip, d, t: sent.append(clip.title) or "id")
    monkeypatch.setattr(worker.get_settings(), "min_minutes_between_posts", 0)
    monkeypatch.setattr(worker.get_settings(), "publish_hours", "")

    for _ in range(4):
        worker.publish_once("tiktok")
    # le meilleur score d'abord, puis partie 1 avant partie 2
    assert sent == ["solo", "p1", "p2"]

    monkeypatch.setattr(worker.get_settings(), "min_minutes_between_posts", 90)
    with db.session() as s:
        assert not worker.can_post(s, "tiktok")


def test_ignored_video_stops_download(monkeypatch, tmp_path):
    import sys
    from app.pipeline import process, sources

    with db.session() as s:
        v = Video(key="w:cancel", url="u", title="longue", status="new")
        s.add(v)
        s.commit()
        vid = v.id

    # faux yt-dlp qui ne finit jamais
    monkeypatch.setattr(sources, "download_cmd", lambda url, out, live: [sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    import pytest
    with pytest.raises(sources.Cancelled):
        sources.download("u", tmp_path, stop)

    # le traitement s'arrête aussi si la vidéo est ignorée entre deux étapes
    with db.session() as s:
        v = s.get(Video, vid)
        v.status = "skipped"
        s.add(v)
        s.commit()
    with pytest.raises(sources.Cancelled):
        process.stop_if_skipped(vid)
    assert worker.is_skipped(vid)


def test_check_source_keyword_filter(monkeypatch):
    from sqlmodel import select

    from app import db, worker
    from app.models import Source, Video
    from app.pipeline import sources

    with db.session() as s:
        src = Source(name="t", platform="twitch", url="https://www.twitch.tv/t", keywords="GTA", initialized=True)
        s.add(src)
        s.commit()
        s.refresh(src)
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Found("twitch:kw1", "u1", "GTA 6 jour 1"), sources.Found("twitch:kw2", "u2", "Minecraft")])
    assert worker.check_source(src) == 1
    with db.session() as s:
        st = {v.key: v.status for v in s.exec(select(Video).where(Video.source_id == src.id))}
    assert st == {"twitch:kw1": "new", "twitch:kw2": "skipped"}
