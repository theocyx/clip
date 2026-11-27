"""Croissance : abonnés gagnés par vidéo, format qui marche mis en avant, réserve de clips, compilations longues."""
import json
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httplib2
from googleapiclient.errors import HttpError
from sqlmodel import delete, select

from app import compile as compil, curate, db, growth, learning, platforms, worker
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, PostStat, Setting, Source, Tuning, Video, now
from app.news import generate as news
from app.pipeline import subtitles
from app.publish import youtube


def _cleanup():
    with db.session() as s:
        for m in (PostStat, Post, ClipInfo, Clip, Tuning):
            s.exec(delete(m))
        for v in s.exec(select(Video)).all():
            s.delete(v)
        for k in (growth.MIX_KEY, growth.CHANNEL_KEY, growth.HIST_KEY, growth.SOURCES_KEY, compil.NOW_KEY,
                  compil.ON_KEY):
            row = s.get(Setting, k)
            if row:
                s.delete(row)
        for row in s.exec(select(Setting)).all():
            if row.key.startswith(("compil_skip:", "compil_tries:")):
                s.delete(row)
        s.commit()


def _video(s, key, title="GTA 6 RP", source_id=None):
    v = Video(key=key, url="https://twitch.tv/videos/1", title=title, status="done", source_id=source_id)
    s.add(v)
    s.commit()
    return v


# ---------- YouTube : abonnés gagnés, minutes regardées ----------

class _Q:
    def __init__(self, calls, fail_first):
        self.calls, self.fail_first = calls, fail_first

    def query(self, **kw):
        self.calls.append(kw["metrics"])
        metrics = kw["metrics"]

        class R:
            def execute(_):
                if self.fail_first and len(self.calls) == 1:
                    raise HttpError(httplib2.Response({"status": 400}), b"unknown metric")
                cols = ["video"] + metrics.split(",")
                row = {"video": "abc", "views": 100, "averageViewDuration": 20, "averageViewPercentage": 55.5,
                       "shares": 2, "subscribersGained": 3, "estimatedMinutesWatched": 40}
                return {"columnHeaders": [{"name": c} for c in cols], "rows": [[row[c] for c in cols]]}
        return R()


class _YA:
    def __init__(self, calls, fail_first=False):
        self.q = _Q(calls, fail_first)

    def reports(self):
        return self.q


def test_retention_reads_subscribers_and_watch_time(monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, "build", lambda *a, **k: _YA(calls))
    out = {"abc": {"views": 100}}
    youtube.retention(None, ["abc"], out)
    assert out["abc"]["subs"] == 3 and out["abc"]["minutes"] == 40.0 and out["abc"]["avg_view_pct"] == 55.5
    assert "subscribersGained" in calls[0]
    # métriques en plus refusées : on garde au moins la rétention
    calls.clear()
    monkeypatch.setattr(youtube, "build", lambda *a, **k: _YA(calls, fail_first=True))
    out = {"abc": {"views": 100}}
    youtube.retention(None, ["abc"], out)
    assert calls[1] == youtube.BASE_METRICS and out["abc"]["avg_view_pct"] == 55.5 and "subs" not in out["abc"]


# ---------- on double ce qui marche ----------

def _kind(n, med):
    return {"n": n, "median": med, "views": [med] * n, "total": n * med, "subs_1k": None, "label": ""}


def test_mix_cuts_weak_formats_and_boosts_gta(monkeypatch):
    cfg = get_settings()
    for k, v in {"growth_mix": True, "mix_min": 4, "news_per_day": 3, "story_per_day": 1}.items():
        monkeypatch.setattr(cfg, k, v)
    # les vrais chiffres de la chaîne (27/11) : clips GTA ~300 vues, actus 12, histoires 1
    m = growth.compute_mix({"clip": _kind(20, 300), "news": _kind(17, 12), "story": _kind(7, 1)})
    assert m["per_day"] == {"news": 0, "story": 0}
    assert m["boost"]["clip"] == 2.0 and m["boost"]["news"] < worker.WEAK_BOOST
    assert any("Histoires" in r and "essai" in r for r in m["reasons"])
    # si les actus devenaient le format qui marche : on en fait plus (dans la limite)
    m = growth.compute_mix({"clip": _kind(20, 100), "news": _kind(10, 600)})
    assert m["per_day"]["news"] == cfg.news_max_per_day and m["boost"]["news"] == 2.0
    # pas assez de mesures : rien ne change
    m = growth.compute_mix({"clip": _kind(20, 300), "news": _kind(2, 1)})
    assert m["per_day"] == {"news": 3, "story": 1} and m["boost"] == {}
    monkeypatch.setattr(cfg, "growth_mix", False)
    assert growth.compute_mix({"clip": _kind(20, 300), "news": _kind(17, 12)})["per_day"]["news"] == 3


def test_mix_decision_sticks_when_a_cut_format_has_few_new_videos(monkeypatch):
    _cleanup()
    cfg = get_settings()
    for k, v in {"growth_mix": True, "mix_min": 4, "news_per_day": 3, "story_per_day": 1}.items():
        monkeypatch.setattr(cfg, k, v)
    platforms._put(growth.MIX_KEY, json.dumps({"per_day": {"news": 0, "story": 0},
                                               "boost": {"clip": 2.0, "news": 0.3, "story": 0.3}, "reasons": []}))
    monkeypatch.setattr(growth, "by_kind", lambda days=None, min_age_h=None: {"clip": _kind(20, 300),
                                                                             "news": _kind(1, 10)})
    m = growth.compute_mix()
    assert m["per_day"] == {"news": 0, "story": 0} and m["boost"]["news"] == 0.3  # pas de retour à 3/jour
    _cleanup()


def test_news_quota_follows_mix(monkeypatch):
    _cleanup()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "growth_mix", True)
    monkeypatch.setattr(cfg, "news_per_day", 3)
    platforms._put(growth.MIX_KEY, json.dumps({"per_day": {"news": 0, "story": 2}, "boost": {}, "reasons": []}))
    assert news.quota("story") == 2
    assert news.quota("news") == 1  # coupé, mais un essai tous les MIX_TEST_DAYS jours
    with db.session() as s:
        s.add(Video(key="news:test-1", url="u", title="actu", status="done"))
        s.commit()
    assert news.quota("news") == 0  # essai déjà fait il y a moins de 3 jours
    monkeypatch.setattr(cfg, "news_per_day", 0)
    assert news.quota("news") == 0  # coupé à la main : jamais d'essai
    _cleanup()


def test_reserve_keeps_near_threshold_gta_clips(monkeypatch):
    _cleanup()
    cfg = get_settings()
    for k, v in {"reserve": True, "publish_min_llm": 7, "reserve_max": 1}.items():
        monkeypatch.setattr(cfg, k, v)
    with db.session() as s:
        s.add(Tuning(samples=40, data=json.dumps({"min_llm": 8})))  # seuil relevé par l'apprentissage
        s.commit()
        v = _video(s, "reserve:1")
        posts = {}
        for name, note, score in (("top", 9, 0.5), ("near", 7.5, 0.9), ("near2", 7, 0.3), ("bad", 6, 0.99)):
            c = Clip(video_id=v.id, group=name, start=0, end=40, score=score, title=f"Domingo GTA 6 : {name}",
                     path=__file__)
            s.add(c)
            s.commit()
            s.add(ClipInfo(clip_id=c.id, data=json.dumps({"llm": note, "text": "gta 6 braquage"})))
            p = Post(clip_id=c.id, platform="youtube")
            s.add(p)
            s.commit()
            posts[name] = p.id
    out = curate.run()
    with db.session() as s:
        st = {k: s.get(Post, pid) for k, pid in posts.items()}
        assert st["top"].status == "pending" and st["top"].error == ""
        assert st["near"].status == "pending" and curate.is_reserve(st["near"])  # gardé en réserve
        assert st["near2"].status == "cancelled" and "réserve pleine" in st["near2"].error  # RESERVE_MAX=1
        assert st["bad"].status == "cancelled" and "trop basse" in st["bad"].error  # sous PUBLISH_MIN_LLM
        order = [p.id for p, _ in worker.queue(s, "youtube")]
    assert out["reserve"] == 1
    assert order == [posts["top"], posts["near"]]  # la réserve ne passe qu'après, malgré un meilleur score
    _cleanup()


def test_weak_format_waits_behind_gta(monkeypatch):
    _cleanup()
    monkeypatch.setattr(get_settings(), "growth_mix", True)
    platforms._put(growth.MIX_KEY, json.dumps({"per_day": {}, "boost": {"clip": 2.0, "news": 0.3}, "reasons": []}))
    with db.session() as s:
        gta, actu = _video(s, "boost:gta"), _video(s, "news:boost-1", title="Actu")
        ids = []
        for v, score in ((actu, 5.0), (gta, 1.0)):
            c = Clip(video_id=v.id, group=f"g{v.id}", start=0, end=30, score=score, title="t", path=__file__)
            s.add(c)
            s.commit()
            p = Post(clip_id=c.id, platform="youtube")
            s.add(p)
            s.commit()
            ids.append(p.id)
        # aucune actu publiée depuis 3 jours : c'est son tour d'être testée, elle passe normalement
        assert [p.id for p, _ in worker.queue(s, "youtube")] == [ids[0], ids[1]]
        old = Clip(video_id=actu.id, group="old", start=0, end=30, score=1, title="t", path=__file__)
        s.add(old)
        s.commit()
        s.add(Post(clip_id=old.id, platform="youtube", status="published", published_at=now() - timedelta(days=1)))
        s.commit()
        order = [p.id for p, _ in worker.queue(s, "youtube")]
    assert order == [ids[1], ids[0]]  # essai fait hier : le clip GTA d'abord, l'actu (format faible) ensuite
    _cleanup()


def test_subscribe_link_in_descriptions():
    _cleanup()
    with db.session() as s:
        v = _video(s, "sub:1")
        c = Clip(video_id=v.id, group="s1", start=0, end=30, score=1, title="Domingo GTA 6 : la poursuite")
        s.add(c)
        s.commit()
    assert "sub_confirmation" not in worker.description(c, v)[0]  # chaîne pas encore relevée
    platforms._put(growth.CHANNEL_KEY, json.dumps({"id": "UCtest"}))
    desc = worker.description(c, v)[0]
    assert "https://www.youtube.com/channel/UCtest?sub_confirmation=1" in desc and "GTA 6" in desc
    _cleanup()


def test_snapshot_history_and_progress(monkeypatch):
    _cleanup()
    monkeypatch.setattr(youtube, "channel", lambda: {"id": "UCx", "title": "GTA VI Clips", "subs": 9, "views": 18956,
                                                      "videos": 189})
    growth.snapshot()
    hist = growth.history()
    old = (datetime.fromisoformat(max(hist)) - timedelta(days=7)).date().isoformat()
    hist[old] = {"subs": 2, "views": 1000, "videos": 100}
    platforms._put(growth.HIST_KEY, json.dumps(hist))
    p = growth.progress()
    assert p["subs"] == 9 and p["rate"] == 1.0 and p["eta_days"] == 991 and growth.channel_id() == "UCx"
    _cleanup()


def test_best_streamers_first(monkeypatch):
    _cleanup()
    platforms._put(growth.SOURCES_KEY, json.dumps({"5": {"perf": 0.8, "n": 4}, "6": {"perf": 0.2, "n": 5},
                                                   "7": {"perf": 0.9, "n": 1}}))
    assert [growth.source_tier(i) for i in (5, 6, 7, None)] == [2, 0, 1, 1]
    with db.session() as s:
        a = Video(key="srcprio:a", url="u", status="downloaded", source_id=6, created_at=now())
        b = Video(key="srcprio:b", url="u", status="downloaded", source_id=5, created_at=now() - timedelta(hours=1))
        s.add(a)
        s.add(b)
        s.commit()
    assert worker.next_video().key == "srcprio:b"  # le streamer qui marche passe devant le plus récent
    monkeypatch.setattr(get_settings(), "source_priority", False)
    assert worker.next_video().key == "srcprio:a"
    _cleanup()


def test_subscribe_cta_on_screen():
    segs = [{"start": 0.0, "end": 20.0, "text": "salut", "words": [{"start": 0.1, "end": 0.5, "word": "salut"}]}]
    ass = subtitles.build_ass(segs, 0, 20, "Montserrat", cta="ABONNE-TOI POUR PLUS DE GTA 6")
    assert "Cta,,0,0,0,," in ass and "ABONNE-TOI" in ass and "0:00:17.00" in ass
    assert "Cta,,0" not in subtitles.build_ass(segs, 0, 6, "Montserrat", cta="ABONNE-TOI")  # clip trop court


# ---------- compilations longues ----------

def _mp4(path, seconds=2, audio=True):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=270x480:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def test_weekly_compilation(monkeypatch, tmp_path):
    _cleanup()
    cfg = get_settings()
    for k, v in {"compil": True, "compil_min": 3, "compil_min_seconds": 3, "compil_max": 5, "compil_days": 7,
                 "compil_day": 6, "compil_hour": 18}.items():
        monkeypatch.setattr(cfg, k, v)
    sunday = datetime(2026, 11, 29, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))
    with db.session() as s:
        src = Source(name="Domingo", platform="twitch", url="https://twitch.tv/domingo")
        s.add(src)
        s.commit()
        v = _video(s, "compil-test:1", source_id=src.id)
        ids = {}
        for i, views in enumerate((10, 500, 50, 200)):
            c = Clip(video_id=v.id, group=f"cp{i}", start=0, end=2, score=1, title=f"Moment fou {i} #GTA6",
                     path=str(_mp4(tmp_path / f"c{i}.mp4", audio=i != 2)))
            s.add(c)
            s.commit()
            s.add(ClipInfo(clip_id=c.id, data="{}"))
            p = Post(clip_id=c.id, platform="youtube", status="published", remote_id=f"r{i}",
                     published_at=now() - timedelta(days=1))
            s.add(p)
            s.commit()
            s.add(PostStat(post_id=p.id, views=views))
            s.commit()
            ids[views] = c.id
    assert compil.due("clip", sunday)
    assert not compil.due("clip", sunday - timedelta(days=1)) and not compil.due("clip", sunday.replace(hour=10))
    vid = compil.make("clip", sunday)
    assert vid and not compil.due("clip", sunday) and compil.make("clip", sunday) is None  # une par semaine
    with db.session() as s:
        video = s.get(Video, vid)
        clip = s.exec(select(Clip).where(Clip.video_id == vid)).first()
        info = json.loads(s.get(ClipInfo, clip.id).data)
    assert video.status == "compil_ready" and 7 < video.duration < 9.5
    assert info["kind"] == "compil" and info["items"] == [ids[10], ids[50], ids[200], ids[500]]  # meilleur à la fin
    assert clip.title.startswith("TOP 4 des meilleurs moments GTA 6 de la semaine (Domingo)")
    assert "0:00 #4 Domingo : Moment fou 0" in clip.description and "#1 Domingo : Moment fou 1" in clip.description
    assert "https://twitch.tv/domingo" in clip.description and "#GTA6" in clip.description
    assert compil.used_clips("clip") == set(ids.values())  # jamais deux fois dans une compilation
    sent = {}

    def fake_upload(path, title, desc, tags, shorts=True, category=""):
        sent.update(path=path, shorts=shorts, category=category)
        return "long123"

    monkeypatch.setattr(youtube, "upload", fake_upload)
    monkeypatch.setattr(youtube, "configured", lambda: True)
    monkeypatch.setattr(youtube, "set_thumbnail", lambda *a: True)
    assert compil.publish(vid)
    assert sent["shorts"] is False and sent["category"] == "20"
    with db.session() as s:
        post = s.exec(select(Post).where(Post.platform == growth.LONG)).one()
        assert post.remote_id == "long123" and post.status == "published"
        assert s.get(Video, vid).status == "done" and worker.used_today(s, "youtube") == 0  # hors quota des Shorts
        s.add(PostStat(post_id=post.id, views=50, minutes=120.0))
        s.commit()
    assert learning.performances() is not None and post.clip_id not in learning.performances()
    assert growth.progress()["hours"] == 2.0
    assert compil.pending() == []
    _cleanup()


def test_long_video_stats_use_youtube_account(monkeypatch):
    _cleanup()
    with db.session() as s:
        v = _video(s, "compil:clip:2026-W01")
        c = Clip(video_id=v.id, group="L", start=0, end=600, score=0, title="TOP")
        s.add(c)
        s.commit()
        s.add(Post(clip_id=c.id, platform=growth.LONG, status="published", remote_id="L1", published_at=now()))
        s.commit()
    monkeypatch.setattr(learning.creds, "load", lambda p: {"token": "x"} if p == "youtube" else None)
    monkeypatch.setitem(learning.FETCHERS, growth.LONG, (lambda: True, lambda ids: {
        i: {"views": 42, "subs": 3, "minutes": 90.0} for i in ids}))
    monkeypatch.setitem(learning.FETCHERS, "youtube", (lambda: True, lambda ids: {}))
    assert learning.fetch_stats() == 1
    with db.session() as s:
        st = s.exec(select(PostStat)).one()
    assert st.subs == 3 and st.minutes == 90.0
    _cleanup()


def test_stats_page_shows_growth(monkeypatch):
    _cleanup()
    from fastapi.testclient import TestClient

    from app.web import main, mfa
    platforms._put(growth.HIST_KEY, json.dumps({"2026-11-20": {"subs": 2, "views": 1, "videos": 1},
                                                "2026-11-27": {"subs": 9, "views": 2, "videos": 2}}))
    platforms._put(growth.CHANNEL_KEY, json.dumps({"id": "UCtest"}))
    platforms._put(growth.MIX_KEY, json.dumps({"per_day": {"news": 0, "story": 0}, "boost": {"clip": 2.0},
                                               "reasons": ["Actus : 12 vues en médiane contre 300"]}))
    monkeypatch.setattr(get_settings(), "growth_mix", True)
    client = TestClient(main.app)
    client.cookies.set(mfa.SESSION_COOKIE, mfa.make_token("session", 3600))
    r = client.get("/stats")
    assert r.status_code == 200 and "Croissance de la chaîne" in r.text and "<b>9</b> abonnés" in r.text
    assert "sub_confirmation=1" in r.text and "Actus : 12 vues" in r.text and "Compilations longues" in r.text
    assert "quand la chaîne peut gagner de l'argent" in r.text and "€ par mois" in r.text
    r = client.post("/compil", data={"action": "now"}, follow_redirects=False)
    assert r.status_code in (302, 303) and platforms._get(compil.NOW_KEY) == "clip"
    _cleanup()


def test_monetization_forecast(monkeypatch):
    _cleanup()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "rpm_shorts", 0.04)
    monkeypatch.setattr(cfg, "rpm_long", 1.5)
    with db.session() as s:
        v = _video(s, "fc:shorts")
        c = Clip(video_id=v.id, group="g", start=0, end=30, score=0.5, title="A", path=__file__)
        s.add(c)
        s.commit()
        s.add(Post(clip_id=c.id, platform="youtube", status="published", remote_id="yt1",
                   published_at=now() - timedelta(days=30)))
        s.commit()
    # 7 jours : +7 abonnés, +14 000 vues => 1 abonné et 2000 vues par jour
    platforms._put(growth.HIST_KEY, json.dumps({"2026-11-20": {"subs": 2, "views": 5000, "videos": 150},
                                                "2026-11-27": {"subs": 9, "views": 19000, "videos": 189}}))
    f = growth.forecast()
    assert f["sub_rate"] == 1.0 and f["sub_eta"] == 992 and f["views_day"] == 2000
    assert round(f["shorts_factor"]) == 56 and not f["shorts_ok"]
    assert f["eta"] is None and f["date"] is None  # ni 10 M vues Shorts ni heures : pas estimable
    assert round(f["month_now"], 1) == 2.4  # 60 000 vues/mois × 0,04 €
    assert round(f["month_goal"]) == 133  # 3,3 M vues Shorts/mois au seuil
    # assez de vues Shorts : la date dépend des abonnés
    platforms._put(growth.HIST_KEY, json.dumps({"2026-11-20": {"subs": 900, "views": 0, "videos": 150},
                                                "2026-11-27": {"subs": 970, "views": 1_050_000, "videos": 189}}))
    f = growth.forecast()
    assert f["shorts_ok"] and f["sub_eta"] == 4 and f["eta"] == 90 and f["date"]
    _cleanup()
