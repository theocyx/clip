import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import db, gta6, worker
from app.models import Clip, Post, Source, Video
from app.pipeline import process, sources

TZ = ZoneInfo("Europe/Paris")


@pytest.fixture
def day_one(monkeypatch):
    monkeypatch.setattr(gta6, "mode", lambda: "on")
    gta6.viewers.clear()
    yield
    gta6.viewers.clear()


def test_window_and_override(monkeypatch):
    monkeypatch.setattr(worker.get_settings(), "gta6_mode", "auto")
    (worker.get_settings().data_dir / "gta6_mode").unlink(missing_ok=True)
    assert not gta6.active(datetime(2026, 11, 17, 12, tzinfo=TZ))
    assert gta6.active(datetime(2026, 11, 18, 20, tzinfo=TZ))  # veille au soir
    assert gta6.active(datetime(2026, 11, 19, 9, tzinfo=TZ))
    assert not gta6.active(datetime(2026, 11, 22, 1, tzinfo=TZ))
    gta6.set_mode("off")
    assert not gta6.active(datetime(2026, 11, 19, 9, tzinfo=TZ))
    gta6.set_mode("on")
    assert gta6.active(datetime(2026, 1, 1, tzinfo=TZ))
    gta6.set_mode("auto")
    with pytest.raises(ValueError):
        gta6.set_mode("bidon")


def test_detection():
    assert gta6.is_gta6("DAY ONE GTA 6 !!!")
    assert gta6.is_gta6("on découvre #GTAVI")
    assert gta6.is_gta6("chill", game="Grand Theft Auto VI")
    assert not gta6.is_gta6("GTA RP sur Flashback")
    assert not gta6.is_gta6("GTA 60 fps", game="Grand Theft Auto V")


def test_twitch_fast_lives(monkeypatch, day_one):
    srcs = [Source(id=1, name="Gros", platform="twitch", url="https://www.twitch.tv/gros"),
            Source(id=2, name="Petit", platform="twitch", url="https://www.twitch.tv/petit"),
            Source(id=3, name="YT", platform="youtube", url="https://www.youtube.com/@yt")]
    monkeypatch.setattr(gta6, "twitch_lives", lambda logins: {
        "gros": {"id": "99", "title": "chill", "game": "Grand Theft Auto VI", "viewers": 50000}})
    out = gta6.fast_lives(srcs)
    assert out[2] == [] and 3 not in out  # YouTube : vérification classique
    assert out[1][0].key == "twitch-live:99" and gta6.is_gta6(out[1][0].title)
    assert gta6.viewers[1] == 50000
    monkeypatch.setattr(gta6, "twitch_lives", lambda logins: None)
    assert gta6.fast_lives(srcs) == {}


def test_priority_and_queue(monkeypatch, tmp_path, day_one):
    with db.session() as s:
        a = Source(name="Petit", platform="twitch", url="https://www.twitch.tv/petit-gta")
        b = Source(name="Squeezie", platform="twitch", url="https://www.twitch.tv/squeezie")
        c = Source(name="Autre", platform="twitch", url="https://www.twitch.tv/autre-jeu")
        s.add_all([a, b, c])
        s.commit()
        vs = [Video(source_id=a.id, key="q:a", url="ua", title="GTA 6 day one", is_live=True),
              Video(source_id=b.id, key="q:b", url="ub", title="GTA 6 !!!", is_live=True),
              Video(source_id=c.id, key="q:c", url="uc", title="Minecraft", is_live=True)]
        s.add_all(vs)
        s.commit()
    monkeypatch.setattr(worker.get_settings(), "gta6_max_recordings", 2)
    monkeypatch.setattr(worker, "recorders", {})
    # catalogue : Squeezie avant le petit streamer inconnu, GTA 6 avant le reste
    assert [v.key for v in worker.lives_to_start(vs)] == ["q:b", "q:a"]
    gta6.viewers[a.id] = 90000  # plus de spectateurs : passe devant
    assert [v.key for v in worker.lives_to_start(vs)] == ["q:a", "q:b"]
    monkeypatch.setattr(worker, "recorders", {1: None, 2: None})
    assert worker.lives_to_start(vs) == []  # plus de place : en file
    monkeypatch.setattr(worker.get_settings(), "gta6_priority", "autre")
    monkeypatch.setattr(worker, "recorders", {})
    gta6.viewers.clear()
    assert worker.lives_to_start(vs)[0].key == "q:b"  # GTA 6 reste devant même un prioritaire hors GTA 6
    with db.session() as s:
        for v in s.exec(worker.select(Video).where(Video.key.startswith("q:"))).all():
            v.status = "done"
            s.add(v)
        s.commit()


def test_queued_live_ended_is_skipped(monkeypatch, day_one):
    with db.session() as s:
        src = Source(name="Fini", platform="twitch", url="https://www.twitch.tv/fini", initialized=True)
        s.add(src)
        s.commit()
        s.add(Video(source_id=src.id, key="twitch-live:5", url="u", title="GTA 6", is_live=True))
        s.commit()
    sent = []
    monkeypatch.setattr(worker.notify, "send", sent.append)
    worker.check_source(src, lives=[sources.Found("twitch-live:6", "u", "GTA VI la suite", True)], videos=False)
    with db.session() as s:
        old = s.exec(worker.select(Video).where(Video.key == "twitch-live:5")).one()
        new = s.exec(worker.select(Video).where(Video.key == "twitch-live:6")).one()
        assert old.status == "skipped" and new.status == "new"
        new.status = "done"
        s.add(new)
        s.commit()
    assert sent and "GTA 6 en live : Fini" in sent[0]


def test_live_cut_into_chunks(monkeypatch, day_one):
    with db.session() as s:
        v = Video(key="twitch-live:77", url="u", title="GTA 6", is_live=True, status="recording")
        s.add(v)
        s.commit()
    p = __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(worker, "recorders", {v.id: p})
    monkeypatch.setattr(worker, "rec_started", {v.id: worker.time.monotonic() - 21 * 60})
    out = process.video_dir(v.id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "source.ts").write_bytes(b"0" * 2_000_000)
    worker.cut_chunks()
    worker.finish_recordings()
    with db.session() as s:
        assert s.get(Video, v.id).status == "downloaded"
        nxt = s.exec(worker.select(Video).where(Video.key == "twitch-live:77#p2")).one()
        assert nxt.is_live and nxt.status == "new"
        for x in (s.get(Video, v.id), nxt):
            x.status = "done"
            s.add(x)
        s.commit()
    assert gta6.next_part_key("k#p2") == "k#p3"


def test_fast_publishing(monkeypatch, day_one):
    cfg = worker.get_settings()
    monkeypatch.setattr(cfg, "publish_hours", "3-4")  # hors des heures de publication
    monkeypatch.setattr(worker, "in_publish_window", lambda: False)
    with db.session() as s:
        old = Video(key="pub:1", url="u", title="GTA 6 matin", status="done")
        other = Video(key="pub:2", url="u", title="Minecraft", status="done")
        s.add_all([old, other])
        s.commit()
        new = Video(key="pub:3", url="u", title="GTA 6 soir", status="done")
        s.add(new)
        s.commit()
        clips = [Clip(video_id=other.id, group="o", start=0, end=60, score=0.99, title="mc", path=__file__),
                 Clip(video_id=old.id, group="m", start=0, end=60, score=0.9, title="matin", path=__file__,
                      created_at=worker.now() - worker.timedelta(hours=6)),
                 Clip(video_id=new.id, group="n", start=0, end=60, score=0.5, title="soir", path=__file__)]
        s.add_all(clips)
        s.commit()
        for c in clips:
            s.add(Post(clip_id=c.id, platform="youtube"))
        s.commit()
        assert worker.can_post(s, "youtube")
        assert s.get(Clip, worker.next_post(s, "youtube").clip_id).title == "soir"
        for p in s.exec(worker.select(Post).where(Post.platform == "youtube")).all():
            p.status = "cancelled"
            s.add(p)
        s.commit()
    monkeypatch.setattr(gta6, "mode", lambda: "off")
    with db.session() as s:
        assert not worker.can_post(s, "youtube")


def test_dashboard_toggle(monkeypatch):
    from fastapi.testclient import TestClient
    from app.web import main
    main.app.dependency_overrides[main.auth] = lambda: None
    try:
        c = TestClient(main.app)
        r = c.post("/gta6", data={"mode": "on"}, follow_redirects=False)
        assert r.status_code == 303 and gta6.mode() == "on"
        page = c.get("/").text
        assert "Mode GTA 6 day one" in page and "actif" in page
        assert c.post("/gta6", data={"mode": "x"}).status_code == 400
        c.post("/gta6", data={"mode": "auto"})
    finally:
        main.app.dependency_overrides.clear()
