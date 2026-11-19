import subprocess

import numpy as np

from app import learning, worker
from app.config import get_settings
from app.models import Clip
from app.pipeline import facecam, highlights, process, render, subtitles


def _words(start, n, step=0.5):
    return [{"start": start + k * step, "end": start + k * step + 0.4, "word": f" mot{k}"} for k in range(n)]


def test_teaser_around_peak():
    segs = [{"start": 0, "end": 60, "text": "x", "words": _words(0, 120)}]
    a, b = highlights.teaser(segs, 40, 0, 60, 2.5)
    assert 38.5 <= a <= 40 and 1.0 <= b - a <= 3.2
    # pic déjà au début : pas d'ouverture choc
    assert highlights.teaser(segs, 3, 0, 60, 2.5) is None
    assert highlights.teaser(segs, None, 0, 60, 2.5) is None


def test_find_moments_records_peak():
    loud = np.zeros(300)
    loud[150] = 40
    segs = [{"start": 100, "end": 200, "text": "ok", "words": _words(100, 100, 1)}]
    ms = highlights.find_moments(loud, segs, count=1, target=60, min_len=30, max_len=90)
    assert ms and ms[0].start <= ms[0].features["peak"] <= ms[0].end


def test_subtitles_pieces_and_keywords():
    segs = [{"start": 0, "end": 30, "text": "x", "words": [
        {"start": 1.0, "end": 1.4, "word": " début"},
        {"start": 20.0, "end": 20.4, "word": " incroyable"},
        {"start": 20.5, "end": 20.9, "word": " 500€"},
    ]}]
    ass = subtitles.build_ass(segs, 0, 30, "Montserrat", pieces=[(19.5, 22), (0, 30)])
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0,")]
    # l'extrait d'ouverture passe en premier, puis le clip décalé de 2,5 s
    assert lines[0].startswith("Dialogue: 0,0:00:00.50") and "INCROYABLE" in lines[0]
    assert any(ln.startswith("Dialogue: 0,0:00:03.50") and "DÉBUT" in ln for ln in lines)
    assert subtitles.KEYWORD in ass and r"\fscx115" in ass
    assert subtitles.is_keyword("500€") and subtitles.is_keyword("DINGUE!") and not subtitles.is_keyword("table")


def test_render_with_teaser(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=duration=6", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    out = tmp_path / "o" / "c.mp4"
    ass = subtitles.build_ass([], 0, 4, "DejaVu Sans", "", "Test", pieces=[(4.5, 5.5), (0, 4)])
    render.render(src, 0, 4, ass, out, teaser=(4.5, 5.5))
    assert facecam.video_size(out) == (1080, 1920)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert 4.7 < dur < 5.4


def test_teaser_cmd_overlay_index(tmp_path):
    cmd = render.build_cmd(tmp_path / "s.mp4", 0, 10, "c.ass", tmp_path / "o.mp4", "libx264", "/x/h.png", 0,
                           teaser=(7, 9))
    vf = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2" in vf and "[2:v]overlay" in vf and cmd.count("-i") == 3


def test_use_teaser_follows_learning(monkeypatch):
    monkeypatch.setattr(get_settings(), "explore", 0.0)
    monkeypatch.setattr(get_settings(), "teaser", True)
    assert process.use_teaser({"teaser": True})
    assert not process.use_teaser({"teaser": False})
    monkeypatch.setattr(get_settings(), "teaser", False)
    assert not process.use_teaser({"teaser": True})


def test_youtube_title_hashtags():
    assert worker.youtube_title("Il rate tout", ["shorts", "GTA6", "GTARP", "FlashBack"]) == "Il rate tout #GTA6 #GTARP"
    assert worker.youtube_title("x" * 95, ["GTA6", "GTARP"]) == "x" * 95
    assert worker.youtube_title("Enfin #GTA6", ["GTA6"]) == "Enfin #GTA6"


def test_tiktok_caption_limits_tags():
    clip = Clip(video_id=1, group="g", start=0, end=60, title="Il rate tout")
    desc = "Il rate tout\n\n🎮 Streamer : Bob\n👉 Sa chaîne : u\n\nExtrait de : GTA RP soirée\nurl\n\n#a"
    cap = worker.tiktok_caption(clip, desc, ["Bob", "GTA6", "GTARP", "RP", "GTA", "clip", "fr", "shorts"])
    assert cap == "Il rate tout 👀 #gta6 #gta\n\nCrédit : Bob"
    assert worker.tiktok_tags(["Bob", "GTARP", "GTA6", "clip", "shorts"], 3) == ["gta6", "gtarp", "bob"]


def test_learning_default_keeps_teaser():
    assert learning.defaults()["teaser"] is True


# ---------- Style des TikTok GTA 6 qui marchent ----------

def test_hook_white_box_png(tmp_path):
    from PIL import Image

    from app.pipeline import hook
    n = hook.render_png("Il refait sa maison pour séduire Lucia dans GTA 6 😭", "Montserrat", tmp_path / "h.png")
    assert n in (2, 3)
    img = Image.open(tmp_path / "h.png").convert("RGBA")
    assert img.width == 1080 and img.height < 400  # image serrée : posée à cheval sur la jonction facecam / jeu
    assert img.getpixel((540, 8))[:3] == (255, 255, 255)  # boîte blanche


def test_hook_position_and_plain_subtitles():
    from types import SimpleNamespace
    assert render.hook_y(SimpleNamespace(x=0, y=0, w=0.2, h=0.2), 614) == "614-h/2"
    segs = [{"start": 0, "end": 2, "text": "incroyable", "words": [
        {"start": 0, "end": 1, "word": "incroyable"}, {"start": 1, "end": 2, "word": "gta"}]}]
    ass = subtitles.build_ass(segs, 0, 2, "Montserrat", keywords=False)
    assert subtitles.KEYWORD not in ass and r"\fscx115" in ass


def test_style_defaults():
    from app.config import Settings
    d = Settings(_env_file=None)
    assert not d.teaser and not d.punch_zoom and not d.progress_bar and not d.end_card
    assert 60 <= d.clip_target <= 68 and d.tiktok_max_hashtags == 2


def test_rate_prompt_names_streamer(monkeypatch):
    from app.pipeline import llm
    seen = {}
    monkeypatch.setattr(llm, "_ask_ollama", lambda p: seen.setdefault("p", p) and '{"score": 8, "title": "t", "hook": "h"}')
    llm.rate("bla", "GTA 6", "ollama", streamer="Squeezie")
    assert "streamer : Squeezie" in seen["p"] and "7 à 10 mots" in seen["p"]
