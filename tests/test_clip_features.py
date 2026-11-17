import json
import subprocess
from datetime import datetime

import pytest

from app import db, worker
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, Source, Video
from app.pipeline import dedupe, facecam, render, subtitles, tags
from app.pipeline.facecam import Box

# ---------- Hashtags tendance ----------


def test_trending_tags(monkeypatch):
    t = tags.trending("GTA RP sur FlashBack ce soir", "on attend gta 6 de fou")
    assert t[0] == "FlashBack"
    assert {"GTARP", "GTA6", "GTA"} <= set(t)
    assert tags.trending("Minecraft hardcore") == []
    monkeypatch.setattr(get_settings(), "rp_servers", "Lunar City=#LunarCity")
    assert "LunarCity" in tags.trending("gta rp lunarcity")
    monkeypatch.setattr(get_settings(), "trend_hashtags", False)
    assert tags.trending("GTA 6") == []


def test_description_has_trend_tags():
    with db.session() as s:
        src = Source(name="Kameto", platform="twitch", url="https://www.twitch.tv/kamet0", keywords="GTA")
        s.add(src)
        s.commit()
        v = Video(source_id=src.id, key="tt:1", url="https://twitch.tv/videos/9", title="Soirée RP sur 21 Jump Click")
        s.add(v)
        s.commit()
    clip = Clip(video_id=v.id, group="tt", start=0, end=60, title="Course-poursuite folle")
    desc, t = worker.description(clip, v)
    assert t[:2] == ["Kameto", "21JumpClick"]
    assert "GTARP" in t and "GTA6" in t and "shorts" in t
    assert len(t) <= get_settings().max_hashtags
    assert len({x.lower() for x in t}) == len(t)
    assert "#21JumpClick" in worker.tiktok_caption(clip, desc, t)


# ---------- Anti-doublons ----------

SCENE = ("les mains en l'air police nationale tu descends de la voiture tout de suite "
         "attends attends je suis juste venu chercher mon pote il a rien fait je te jure")


def test_similarity():
    a = "OK Kameto : " + SCENE + " mdr il est trop fort"
    b = "Bon alors " + SCENE.upper() + " euh quoi ?"
    assert dedupe.similarity(a, b) > 0.6
    assert dedupe.similarity(a, "rien à voir on parle de pizza et de minecraft ce soir avec les potes") < 0.1
    assert dedupe.is_duplicate(0.2, True, 0.3) and not dedupe.is_duplicate(0.2, None, 0.3)
    assert dedupe.overlap(0, 60, 400, 460) is False and dedupe.overlap(0, 60, 30, 90) is True
    assert dedupe.overlap(None, 60, 0, 1) is None


def _clip(video_key: str, score: float, text: str, post_status: str = "pending"):
    with db.session() as s:
        v = Video(key=video_key, url="u", title="GTA RP", is_live=True, created_at=datetime(2026, 11, 17, 20))
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group=video_key, start=0, end=60, score=score, title=video_key)
        s.add(c)
        s.commit()
        s.add(ClipInfo(clip_id=c.id, data=json.dumps({"dedupe_text": text, "abs_start": 1000.0, "abs_end": 1060.0})))
        s.add(Post(clip_id=c.id, platform="tiktok", status=post_status))
        s.commit()
        return v, c


def test_find_and_replace_duplicate():
    v1, c1 = _clip("dup:a", 0.5, SCENE)
    me = Video(id=99999, key="dup:me", url="u")
    found = dedupe.find(me.id, "et là " + SCENE, 1010.0, 1070.0)
    assert [m.group for m in found] == [c1.group] and not found[0].posted
    assert dedupe.find(me.id, "autre chose " * 30, 1010.0, 1070.0) == []
    assert dedupe.replace(c1.group, "meilleur") == 1
    with db.session() as s:
        p = s.exec(Post.__table__.select().where(Post.clip_id == c1.id)).first()
        assert p.status == "cancelled" and "Doublon" in p.error


def test_check_duplicate_keeps_best(monkeypatch):
    from types import SimpleNamespace

    from app.pipeline import process
    text = "scène unique " + SCENE + " fin de la scène deux"
    v1, c1 = _clip("dup:b", 0.9, text, "published")
    me = SimpleNamespace(id=88888)
    m = SimpleNamespace(score=0.95, title="moi", features={"dedupe_text": text, "abs_start": None, "abs_end": None})
    assert process.check_duplicate(me, m)  # déjà publié : on ne refait pas la scène
    monkeypatch.setattr(get_settings(), "dedupe", False)
    assert not process.check_duplicate(me, m)


# ---------- Format split ----------


def test_stable_face():
    cam = Box(0.8, 0.7, 0.08, 0.14)
    frames = [[cam], [Box(0.805, 0.7, 0.08, 0.14)], [], [cam, Box(0.4, 0.4, 0.1, 0.1)], [cam]]
    f = facecam.stable_face(frames)
    assert f and abs(f.x - 0.8) < 0.01
    assert facecam.stable_face([[Box(0.45, 0.4, 0.1, 0.15)]] * 5) is None  # visage au centre
    assert facecam.stable_face([[cam], [], [], [], []]) is None  # pas assez stable
    r = facecam.cam_region(f, 1920, 1080, 1080 / 640)
    assert 0 <= r.x and r.x + r.w <= 1.0001 and 0 <= r.y and r.y + r.h <= 1.0001
    assert abs(r.w * 1920 / (r.h * 1080) - 1080 / 640) < 0.01


def test_split_subtitles_offset():
    ass = subtitles.build_ass([], 0, 10, "Montserrat", "Partie 1/2", "Une accroche", top=490)
    assert ",90,90,660,1" in ass  # accroche sous la facecam


def test_detect_without_face(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=4",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    assert facecam.detect(src, 0, 4, 1080 / 640) is None


@pytest.mark.parametrize("cam", [None, Box(0.7, 0.65, 0.3, 0.35)])
def test_render_layouts(tmp_path, cam):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    out = tmp_path / "o" / "c.mp4"
    ass = subtitles.build_ass([], 0, 2, "DejaVu Sans", "", "Test", top=render.split_top(640) if cam else 0)
    render.render(src, 0, 2, ass, out, cam=cam)
    assert facecam.video_size(out) == (1080, 1920)
