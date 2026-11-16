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

    for _ in range(4):
        worker.publish_once("tiktok")
    # le meilleur score d'abord, puis partie 1 avant partie 2
    assert sent == ["solo", "p1", "p2"]

    monkeypatch.setattr(worker.get_settings(), "min_minutes_between_posts", 90)
    with db.session() as s:
        assert not worker.can_post(s, "tiktok")
