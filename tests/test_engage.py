import subprocess

import pytest

from app import db, learning, worker
from app.models import Clip, Video
from app.pipeline import engage, facecam, highlights, process, render, subtitles
from app.pipeline.facecam import Box


# ---------- Titres variés ----------

def test_clean_video_title_removes_live_noise():
    assert engage.clean_video_title("#GTAVI Live Stream 137 Heures") == "GTAVI"
    assert engage.clean_video_title("SOIRÉE RP sur FlashBack | Jour 12 !") == "SOIRÉE RP sur FlashBack"


def test_title_copying_live_title_is_rebuilt_from_content():
    live = "#GTAVI Live Stream 137 Heures"
    text = "Bon on va à la banque. Attends attends, il a braqué la banque avec 3 millions ! Mais non c'est pas vrai."
    t = engage.fix_title("GTAVI Live Stream 137 heures", live, [], text)
    assert "137" not in t and "Live" not in t
    assert "banque" in t.lower()
    # un titre original de l'IA est gardé
    assert engage.fix_title("GTA 6 : il braque la banque en plein live", live, [], text).startswith("GTA 6 : il braque")


def test_title_too_close_to_recent_is_replaced():
    recent = ["GTA RP : il se fait arrêter par la police"]
    assert engage.too_close("GTA RP : il se fait arrêter par la police !", "Soirée RP", recent)
    t = engage.fix_title("GTA RP : il se fait arrêter par la police !", "Soirée RP", recent,
                         "Le policier sort de la voiture. Franchement je lui ai mis 500 euros, c'est incroyable !")
    assert t != recent[0] and "500" in t


def test_topic_keyword():
    assert engage.topic("Soirée RP sur 21 Jump Click") == "GTA RP 21JumpClick"
    assert engage.topic("#GTAVI Live") == "GTA 6"
    assert engage.topic("Minecraft hardcore") == ""


# ---------- Question / fin de clip ----------

def test_end_card_and_question():
    assert engage.end_card("Tu valides ?", 1, 2).startswith("La suite en partie 2")
    assert engage.end_card("Il aurait dû fuir ?", 2, 2) == "Il aurait dû fuir ? Dis-le en commentaire"
    assert engage.end_card("", 1, 1) == ""
    assert engage.clean_question("Tu lui pardonnes 😂") == "Tu lui pardonnes ?"
    assert engage.fallback_question("la police arrive") in dict(engage.QUESTIONS)[
        r"police|flic|arrêt|prison|course|poursuite|braquage|banque"]


def test_end_text_in_ass():
    ass = subtitles.build_ass([], 0, 20, "Montserrat", end_text="Tu valides ? Dis-le en commentaire")
    line = next(ln for ln in ass.splitlines() if ",Ask," in ln)
    assert line.startswith("Dialogue: 3,0:00:16.50,0:00:20.00") and "TU VALIDES" in line
    # clip trop court : pas de carte de fin
    assert ",Ask," not in subtitles.build_ass([], 0, 4, "Montserrat", end_text="Tu valides ?").split("[Events]")[1]


def test_description_and_caption_have_question():
    with db.session() as s:
        v = Video(source_id=None, key="eng:1", url="https://youtu.be/x", title="Live GTA")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group="eng", start=0, end=30, title="Il braque la banque")
        s.add(c)
        s.commit()
        s.refresh(c)
    learning.save_clip_info(c.id, {"question": "Il aurait dû fuir ?"})
    desc, tags = worker.description(c, v)
    assert "💬 Il aurait dû fuir ? Réponds en commentaire 👇" in desc
    assert "fuir" not in worker.tiktok_caption(c, desc, tags)  # légende TikTok courte, sans question
    c.part, c.part_count = 1, 2  # partie 1/2 : pas de question, la suite arrive
    assert "fuir" not in worker.description(c, v)[0]


# ---------- Rythme : blancs coupés, zoom ----------

def _w(t, word="mot"):
    return {"start": t, "end": t + 0.4, "word": " " + word}


def test_cut_silences():
    segs = [{"start": 0, "end": 30, "text": "x", "words": [_w(1), _w(1.5), _w(6), _w(6.5), _w(7.2), _w(12)]}]
    pieces = highlights.cut_silences(segs, 0, 14, min_gap=1.2, keep=0.3, max_frac=1)
    # blancs coupés : 2,2-5,7 et 7,9-11,7 (on garde 0,3 s autour des mots)
    assert pieces == [(0, 2.2), (5.7, 7.9), (11.7, 14)]
    assert highlights.out_time(pieces, 6.0) == pytest.approx(2.2 + 0.3)
    # long blanc au tout début : coupé aussi
    assert highlights.cut_silences(segs, 0, 14, min_gap=0.5, keep=0.3, max_frac=1)[0][0] == 0.7
    assert highlights.out_time(pieces, 4.0) is None
    assert highlights.cut_silences([], 0, 10) == [(0, 10)]


def test_cut_silences_keeps_gameplay():
    # 75 s de jeu avec peu de paroles (VAD) : avant, ~46 s coupées et un clip de 29 s
    words = [_w(t) for t in (0.5, 1, 9, 9.5, 20, 31, 31.5, 44, 56, 57, 70)]
    segs = [{"start": 0, "end": 75, "text": "x", "words": words}]
    kept = lambda p: sum(y - x for x, y in p)  # noqa: E731
    pieces = highlights.cut_silences(segs, 0, 75)
    assert kept(pieces) >= 75 * 0.85 - 0.01  # au plus 15 % coupés
    assert len(pieces) > 1  # les plus longs blancs sont quand même coupés
    pieces = highlights.cut_silences(segs, 0, 40, max_frac=1, min_len=35)
    assert kept(pieces) >= 35 - 0.01  # jamais sous CLIP_MIN
    # blancs de moins de 3 s (défaut) : rien n'est coupé
    short = [{"start": 0, "end": 10, "text": "x", "words": [_w(t) for t in (0, 2, 4, 6, 8)]}]
    assert highlights.cut_silences(short, 0, 10) == [(0, 10)]


def test_punch_windows():
    body = [(0, 10), (12, 20)]
    assert process.punch_windows(14, body) == [(11.8, 13.4)]
    assert process.punch_windows(14, body, teaser=(13, 15.5)) == [(14.3, 15.9)]
    assert process.punch_windows(11, body) is None and process.punch_windows(None, body) is None


@pytest.mark.parametrize("cam", [None, Box(0.7, 0.65, 0.3, 0.35)])
def test_render_cuts_punch_bar(tmp_path, cam):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=duration=8", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    out = tmp_path / "o" / "c.mp4"
    body = [(0, 2), (4, 7)]
    pieces = [(5, 6)] + body
    ass = subtitles.build_ass([], 0, 7, "DejaVu Sans", "", "Test", top=render.split_top(640) if cam else 0,
                              pieces=pieces, end_text="Tu valides ?", end_seconds=1.5)
    render.render(src, 0, 7, ass, out, cam=cam, teaser=(5, 6), cuts=body, punch=[(2.0, 3.0)], bar=True)
    assert facecam.video_size(out) == (1080, 1920)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert 5.7 < dur < 6.4


def test_cmd_with_cuts_inputs(tmp_path):
    cmd = render.build_cmd(tmp_path / "s.mp4", 0, 10, "c.ass", tmp_path / "o.mp4", "libx264", "/x/h.png", 0,
                           cuts=[(0, 4), (5, 10)], bar=True)
    vf = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2" in vf and "[2:v]overlay" in vf and "W*t/9.00" in vf and cmd.count("-i") == 3


def test_placeholder_x_replaced_by_streamer_name():
    """« X règle ça à sa façon GTA 6 » affiché sur une vidéo : le « X » des exemples recopié par l'IA."""
    from app.pipeline import engage, llm
    assert "X " not in llm.PROMPT and "règle ça à sa façon" not in llm.PROMPT
    assert engage.has_placeholder("X règle ça à sa façon GTA 6 🤯")
    assert not engage.has_placeholder("Xbox : GTA 6 tourne enfin") and not engage.has_placeholder("Mr X débarque")
    assert engage.fill_name("X vole un avion dans GTA 6 😱", "Squeezie") == "Squeezie vole un avion dans GTA 6 😱"
    assert engage.fill_name("X vole un avion dans GTA 6", "") == ""  # nom inconnu : repli habituel
    assert engage.fill_name("Squeezie règle ça à sa façon GTA 6", "Squeezie") == ""  # exemple recopié
    assert engage.fill_name("Inoxtag se fait arrêter par la police", "Inoxtag") == "Inoxtag se fait arrêter par la police"
    assert engage.bad_title("X règle ça à sa façon GTA 6")
    assert not engage.fix_title("X règle ça à sa façon GTA 6", "Live GTA 6", [], "il vole une voiture de police").startswith("X ")
