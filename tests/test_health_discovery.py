import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db, discovery, health, notify
from app.config import get_settings
from app.models import DiscoverIgnore, Source, Video
from app.web import main as web


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda text: out.append(text) or True)
    return out


@pytest.fixture
def clean_sources():
    with db.session() as s:
        for v in s.exec(select(Video)).all():
            v.source_id = None
            s.add(v)
        for x in s.exec(select(Source)).all() + s.exec(select(DiscoverIgnore)).all():
            s.delete(x)
        s.commit()
    yield


# ---------- Santé ----------

def test_cpu_temp_and_memory(tmp_path):
    hw = tmp_path / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("k10temp\n")
    (hw / "temp1_input").write_text("71250\n")
    other = tmp_path / "hwmon" / "hwmon1"
    other.mkdir()
    (other / "name").write_text("nvme\n")
    (other / "temp1_input").write_text("99000\n")  # pas le CPU
    assert health.cpu_temp(tmp_path) == 71.2
    z = tmp_path / "x" / "thermal" / "thermal_zone0"
    z.mkdir(parents=True)
    (z / "temp").write_text("55000")
    assert health.cpu_temp(tmp_path / "x") == 55.0
    assert health.cpu_temp(tmp_path / "vide") is None

    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal:       16000000 kB\nMemFree: 100 kB\nMemAvailable:    4000000 kB\n")
    assert health.memory(mem) == {"pct": 75.0, "free_gb": 4.0, "total_gb": 16.0}
    assert health.memory(tmp_path / "absent") is None


def test_problems_and_alerts(monkeypatch, sent):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "health_web_url", "")
    monkeypatch.setattr(health, "disk", lambda: {"pct": 95.0, "free_gb": 3.0, "total_gb": 60.0})
    monkeypatch.setattr(health, "cpu_temp", lambda: 93.0)
    monkeypatch.setattr(health, "memory", lambda: {"pct": 50.0, "free_gb": 8.0, "total_gb": 16.0})
    probs = health.worker_problems()
    assert set(probs) == {"disk", "temp"}

    (cfg.data_dir / "health" / "alerts-t.json").unlink(missing_ok=True)
    a = health.Alerter("t")
    t0 = time.time()
    assert len(a.update(probs, t0)) == 2
    assert a.update(probs, t0 + 60) == []  # pas de spam
    again = a.update(probs, t0 + cfg.health_repeat_hours * 3600 + 1)
    assert len(again) == 2 and again[0].startswith("⏰")
    done = a.update({"temp": probs["temp"]}, t0 + cfg.health_repeat_hours * 3600 + 60)
    assert done == ["✅ Réglé : 💾 Disque presque plein"]
    assert len(sent) == 5


def test_service_down_needs_two_checks(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "health_web_url", "http://web:8000/healthz")
    monkeypatch.setattr(health, "disk", lambda: {"pct": 10.0, "free_gb": 50.0, "total_gb": 60.0})
    monkeypatch.setattr(health, "cpu_temp", lambda: None)
    monkeypatch.setattr(health, "memory", lambda: None)
    monkeypatch.setattr(health, "ping", lambda url, timeout=5: False)
    health._fails.clear()
    assert health.worker_problems() == {}  # un service qui redémarre : pas d'alerte tout de suite
    assert set(health.worker_problems()) == {"web"}

    class Dead:
        name = "process_loop"

        def is_alive(self):
            return False
    monkeypatch.setattr(health, "ping", lambda url, timeout=5: True)
    assert set(health.worker_problems([Dead()])) == {"threads"}


def test_crash_detection_and_worker_watchdog(monkeypatch):
    d = get_settings().data_dir / "health"
    for f in ("w.running", "w.crashes"):
        (d / f).unlink(missing_ok=True) if d.exists() else None
    assert health.started("w") == 0
    health.stopped("w")
    assert health.started("w") == 0  # arrêt normal
    assert health.started("w") == 1  # pas d'arrêt normal : plantage
    assert health.started("w") == 2
    assert health.crashes("w") == 2
    assert "redémarrer" in health.crash_message("worker", 1)
    assert "en boucle" in health.crash_message("worker", 3)

    health.beat("worker")
    assert health.web_problems() == {}
    (d / "worker.beat").write_text(str(time.time() - 3600))
    assert "worker" in health.web_problems()
    health.beat("worker")


def test_dashboard_and_healthz(monkeypatch):
    web.app.dependency_overrides[web.auth] = lambda: None
    try:
        with TestClient(web.app) as c:
            assert c.get("/healthz").text == "ok"
            r = c.get("/")
            assert r.status_code == 200 and "Santé du serveur" in r.text
            r = c.get("/sources")
            assert r.status_code == 200 and "Découverte automatique" in r.text
    finally:
        web.app.dependency_overrides.clear()


# ---------- Découverte ----------

def test_looks_french():
    assert discovery.looks_french("DAY ONE GTA 6 on découvre la ville")
    assert discovery.looks_french("GTA RP [FR] chill")
    assert not discovery.looks_french("GTA 6 FIRST DAY GAMEPLAY LIVE")


def cand(name, viewers, platform="twitch", title="GTA 6 day one"):
    url = f"https://www.twitch.tv/{name.lower()}" if platform == "twitch" else f"https://www.youtube.com/@{name}"
    return {"name": name, "platform": platform, "url": url, "title": title, "viewers": viewers,
            "game": "Grand Theft Auto VI"}


def test_run_adds_with_limits(monkeypatch, sent, clean_sources):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "discover_per_run", 2)
    monkeypatch.setattr(cfg, "discover_max_sources", 3)
    monkeypatch.setattr(cfg, "discover_min_viewers", 100)
    with db.session() as s:
        s.add(Source(name="Deja", platform="twitch", url="https://www.twitch.tv/deja"))
        s.add(DiscoverIgnore(url="https://www.twitch.tv/banni"))
        s.commit()
    found = [cand("Deja", 9000), cand("banni", 8000), cand("Grand", 5000), cand("moyen", 900),
             cand("petit", 50), cand("autre", 400)]
    st = discovery.run(found)
    assert st["added"] == ["Grand", "moyen"]
    with db.session() as s:
        auto = s.exec(select(Source).where(Source.auto == True)).all()  # noqa: E712
    assert {a.url for a in auto} == {"https://www.twitch.tv/grand", "https://www.twitch.tv/moyen"}
    assert all(a.keywords == "GTA" and a.record_lives and not a.include_videos for a in auto)
    assert len(sent) == 2 and "Grand" in sent[0]
    # plafond : une seule place restante
    st = discovery.run(found + [cand("nouveau1", 300), cand("nouveau2", 300)])
    assert st["added"] == ["autre"]
    assert discovery.run(found)["added"] == []
    assert "plafond" in discovery.last_status()["message"]


def test_prune_and_manual_delete(monkeypatch, clean_sources):
    from datetime import timedelta
    from app.models import now
    monkeypatch.setattr(get_settings(), "discover_expire_days", 7)
    with db.session() as s:
        old = Source(name="vieux", platform="twitch", url="https://www.twitch.tv/vieux", auto=True,
                     last_seen=now() - timedelta(days=8))
        busy = Source(name="occupe", platform="twitch", url="https://www.twitch.tv/occupe", auto=True,
                      last_seen=now() - timedelta(days=8))
        fresh = Source(name="frais", platform="twitch", url="https://www.twitch.tv/frais", auto=True,
                       last_seen=now() - timedelta(days=8))
        s.add_all([old, busy, fresh])
        s.commit()
        s.add(Video(source_id=busy.id, key="k-busy", url="u", status="rendering"))
        s.commit()
    st = discovery.run([cand("frais", 10)])  # vue en live : reste
    assert st["removed"] == ["vieux"] and st["seen"] == 1

    web.app.dependency_overrides[web.auth] = lambda: None
    try:
        with TestClient(web.app) as c:
            c.post(f"/sources/{fresh.id}/delete")
    finally:
        web.app.dependency_overrides.clear()
    with db.session() as s:
        assert s.get(DiscoverIgnore, "https://www.twitch.tv/frais")
    assert discovery.run([cand("frais", 5000)])["added"] == []  # retirée à la main : jamais rajoutée


def test_twitch_and_youtube_parsing(monkeypatch):
    class R:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, json):
            assert json["variables"] == {"g": "Grand Theft Auto VI", "n": 30, "l": ["FR"]}
            return R({"data": {"game": {"streams": {"edges": [
                {"node": {"id": "1", "title": "DAY ONE", "viewersCount": 1234,
                          "broadcaster": {"login": "Zerator", "displayName": "ZeratoR"}}}]}}}})

    monkeypatch.setattr(discovery.httpx, "Client", Client)
    out = discovery.twitch_candidates("Grand Theft Auto VI")
    assert out == [{"name": "ZeratoR", "platform": "twitch", "url": "https://www.twitch.tv/zerator",
                    "title": "DAY ONE", "viewers": 1234, "game": "Grand Theft Auto VI"}]

    monkeypatch.setattr(discovery.sources, "_list_entries", lambda url, limit: [
        {"title": "GTA 6 : on découvre la map", "channel_url": "https://www.youtube.com/channel/UC1",
         "channel": "Chaîne FR", "concurrent_view_count": 800, "live_status": "is_live"},
        {"title": "GTA 6 FIRST LOOK", "channel_url": "https://www.youtube.com/channel/UC2"},
        {"title": "Minecraft avec les potes", "channel_url": "https://www.youtube.com/channel/UC3"},
    ])
    yt = discovery.youtube_candidates("gta 6 fr")
    assert [c["name"] for c in yt] == ["Chaîne FR"] and yt[0]["viewers"] == 800


def test_toggle():
    discovery.set_enabled(False)
    assert not discovery.enabled()
    discovery.set_enabled(True)
    assert discovery.enabled()
