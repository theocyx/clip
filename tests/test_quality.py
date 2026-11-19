"""Qualité plutôt que quantité : tri de la file, titres jamais recopiés du live, envois espacés et plafonnés."""
import json
from datetime import datetime, timedelta

from app import curate, db, learning, platforms, worker
from app.models import Clip, ClipInfo, Post, Source, Tuning, Video
from app.pipeline import engage
from app.publish import youtube

LIVE = "#GTAVI Live Stream 137 Heures !wankul"


def test_tidy_title():
    assert engage.tidy_title("#GTAVI RP | Serveur Infiltration | !wankul") == "GTA 6 RP | Serveur Infiltration"
    assert engage.tidy_title("GTA6 : il vole un avion @bob") == "GTA 6 : il vole un avion"


def test_fix_title_never_copies_live_title():
    # l'IA recopie le live et la transcription ne donne rien d'exploitable : jamais le titre du live
    for text in ("", "euh", "Live stream 137 heures les gars"):
        t = engage.fix_title("#GTAVI Live Stream 137 Heures", LIVE, [], text, "")
        assert not engage.too_close(t, LIVE, []), t
        assert "137" not in t
    t = engage.fix_title("#GTAVI Live Stream : 137 Heures", LIVE, [], "", "Il saute de l'avion 😱")
    assert t.startswith("GTA 6") and "avion" in t and "😱" not in t
    # titre original gardé (juste nettoyé)
    assert engage.fix_title("#GTAVI Il vole un tigre au zoo", LIVE, [], "") == "GTA 6 Il vole un tigre au zoo"


def _clip(s, video, title, score=0.5, part=1, part_count=1, info=None, age_h=1.0):
    c = Clip(video_id=video.id, group=f"q{title}{part}", part=part, part_count=part_count, start=0, end=30,
             score=score, title=title, path=__file__, created_at=worker.now() - timedelta(hours=age_h))
    s.add(c)
    s.commit()
    s.add(ClipInfo(clip_id=c.id, data=json.dumps(info or {})))
    p = Post(clip_id=c.id, platform="youtube")
    s.add(p)
    s.commit()
    return c.id, p.id


def test_curate_queue(monkeypatch):
    cfg = worker.get_settings()
    for k, v in {"topic_only": True, "publish_min_llm": 6, "queue_max_hours": 36, "allow_parts": False}.items():
        monkeypatch.setattr(cfg, k, v)
    with db.session() as s:
        src = Source(name="Wankil", platform="youtube", url="https://youtube.com/@wk")
        s.add(src)
        s.commit()
        gta = Video(key="q:gta", url="u", title=LIVE, status="done")
        off = Video(key="q:off", url="u", title="Mega Best of Wankil", source_id=src.id, status="done")
        s.add_all([gta, off])
        s.commit()
        ok = _clip(s, gta, "#GTAVI Live Stream 137 Heures", 0.9,
                   info={"llm": 8, "text": "Il saute de l'avion sans parachute, c'est n'importe quoi !"})
        part = _clip(s, gta, "Avion mortel (partie 1/2)", part_count=2, info={"llm": 9})
        low = _clip(s, gta, "GTA 6 : il attend au menu", info={"llm": 4})
        offt = _clip(s, off, "Wankil : 20 ans de rires", info={"llm": 8, "text": "on rigole"})
        old = _clip(s, gta, "GTA 6 : course poursuite", info={"llm": 8}, age_h=48)
        forced = _clip(s, gta, "GTA 6 : à garder (partie 2/2)", part=2, part_count=2, info={"llm": 2})
        p = s.get(Post, forced[1])
        p.error = curate.FORCED
        s.add(p)
        s.commit()
    curate.run()
    with db.session() as s:
        st = lambda ids: s.get(Post, ids[1]).status  # noqa: E731
        assert st(ok) == "pending" and st(forced) == "pending"
        assert st(part) == st(low) == st(offt) == st(old) == "cancelled"
        assert "plusieurs parties" in s.get(Post, part[1]).error
        assert "Hors sujet" in s.get(Post, offt[1]).error
        title = s.get(Clip, ok[0]).title
        assert "137" not in title and "avion" in title.lower()
        for ids in (ok, part, low, offt, old, forced):  # ménage pour les autres tests
            s.get(Post, ids[1]).status = "cancelled"
        s.commit()
    assert curate.run()["retitled"] == 0  # titres stables d'un passage à l'autre


def test_gap_floor_cap_spread_and_quiet_hours(monkeypatch):
    cfg = worker.get_settings()
    monkeypatch.setattr(worker.gta6, "active", lambda: False)
    monkeypatch.setattr(cfg, "publish_hours", "")
    monkeypatch.setattr(platforms, "ui_gap", lambda: 2)  # écart de 2 min choisi dans l'interface
    monkeypatch.setattr(platforms, "daily_limit", lambda p: None)
    monkeypatch.setattr(cfg, "max_posts_per_day", 100)
    monkeypatch.setattr(cfg, "post_gap_floor", 40)
    monkeypatch.setattr(cfg, "daily_cap", 12)
    assert worker.post_gap() == 2 and worker.base_gap() == 40
    assert worker.post_limit("youtube") == 12
    monkeypatch.setattr(cfg, "max_posts_per_day", 0)
    assert worker.post_limit("youtube") == 12  # plafond même sans limite
    monkeypatch.setattr(cfg, "daily_cap", 0)
    assert worker.post_limit("youtube") == 0
    monkeypatch.setattr(cfg, "daily_cap", 12)

    # répartition : 12 envois sur la journée entière → au moins 1440/12/… min entre deux
    monkeypatch.setattr(cfg, "spread_posts", True)
    monkeypatch.setattr(cfg, "quiet_hours", "")
    monkeypatch.setattr(worker, "open_minutes", lambda a, b: 600)
    with db.session() as s:
        monkeypatch.setattr(worker, "used_today", lambda s, p: 2)
        assert worker.effective_gap(s, "youtube") == 60  # 600 min / 10 envois restants
        monkeypatch.setattr(worker, "open_minutes", lambda a, b: 100)
        assert worker.effective_gap(s, "youtube") == 40  # jamais sous le plancher
        monkeypatch.setattr(cfg, "spread_posts", False)
        monkeypatch.setattr(worker, "open_minutes", lambda a, b: 6000)
        assert worker.effective_gap(s, "youtube") == 40

    tz = worker.ZoneInfo(cfg.timezone)
    monkeypatch.setattr(cfg, "quiet_hours", "1-8")
    assert worker.in_quiet_hours(datetime(2026, 11, 19, 3, 0, tzinfo=tz))
    assert not worker.in_quiet_hours(datetime(2026, 11, 19, 12, 0, tzinfo=tz))
    monkeypatch.setattr(worker.gta6, "active", lambda: True)
    monkeypatch.setattr(cfg, "gta6_publish_anytime", True)
    assert not worker.open_at(datetime(2026, 11, 19, 3, 0, tzinfo=tz))  # la nuit, même le jour J
    assert worker.open_at(datetime(2026, 11, 19, 23, 0, tzinfo=tz))


def test_old_long_duration_forgotten_and_single_part(monkeypatch):
    with db.session() as s:
        s.add(Tuning(samples=10, data=json.dumps({"clip_target": 82, "llm_mix": 0.6})))
        s.commit()
    assert learning.current()["clip_target"] == worker.get_settings().clip_target
    monkeypatch.setattr(worker.get_settings(), "allow_parts", False)
    assert learning.max_len() <= 58
    with db.session() as s:
        for t in s.exec(worker.select(Tuning)).all():
            s.delete(t)
        s.commit()


def test_retention_parsed_from_analytics(monkeypatch):
    class Q:
        def __init__(self, kw):
            self.kw = kw

        def execute(self):
            return {"columnHeaders": [{"name": n} for n in
                                      ("video", "views", "averageViewDuration", "averageViewPercentage", "shares")],
                    "rows": [["a", 10, 21.5, 74.2, 3]]}

    class R:
        def query(self, **kw):
            assert kw["sort"] == "-views" and kw["maxResults"] == 2 and kw["filters"] == "video==a,b"
            return Q(kw)

    class YA:
        def reports(self):
            return R()

    monkeypatch.setattr(youtube, "build", lambda *a, **k: YA())
    out = {"a": {"views": 10}, "b": {"views": 0}}
    youtube.retention(None, ["a", "b"], out)
    assert out["a"]["avg_view_pct"] == 74.2 and out["a"]["shares"] == 3 and "avg_view_pct" not in out["b"]


def test_used_today_resets_at_midnight():
    start = worker.day_start()
    with db.session() as s:
        before = worker.used_today(s, "youtube")
        v = Video(key="q:midnight", url="u", title="t", status="done")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group="mid", start=0, end=30, title="GTA 6 : minuit", path=__file__)
        s.add(c)
        s.commit()
        for delta in (timedelta(minutes=-30), timedelta(hours=-3), timedelta(minutes=5)):
            s.add(Post(clip_id=c.id, platform="youtube", status="published", published_at=start + delta))
        s.commit()
        assert worker.used_today(s, "youtube") == before + 1
        for p in s.exec(worker.select(Post).where(Post.clip_id == c.id)).all():
            p.status = "cancelled"
            s.add(p)
        s.commit()
