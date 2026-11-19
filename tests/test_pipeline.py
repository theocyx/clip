import numpy as np

from app.pipeline import highlights, subtitles
from app.publish.tiktok import MB, chunk_plan


def make_segments(total=600, every=4.0):
    segs, t, i = [], 0.0, 0
    while t + every <= total:
        words = [{"start": t + k * 0.8, "end": t + k * 0.8 + 0.6, "word": f"mot{i}_{k}"} for k in range(4)]
        text = " ".join(w["word"] for w in words) + ("." if i % 3 == 2 else "")
        segs.append({"start": t, "end": t + 3.4, "text": text, "words": words})
        t += every
        i += 1
    return segs


def test_finds_loud_exciting_moment():
    total = 600
    loud = np.full(total, -30.0, dtype=np.float32)
    loud[400:420] = -8.0  # gros pic sonore
    segs = make_segments(total)
    for s in segs:
        if 400 <= s["start"] < 420:
            s["text"] = "Oh non c'est incroyable !"
            for w in s["words"]:
                w["word"] = "incroyable"
    moments = highlights.find_moments(loud, segs, count=1, target=60, min_len=35, max_len=180)
    assert len(moments) == 1
    m = moments[0]
    assert m.start <= 405 and m.end >= 415
    assert 35 <= m.duration <= 180


def test_snap_extends_to_sentence_end():
    segs = make_segments(600)
    b, e = highlights.snap(100, segs, 60, 35, 180, 600)
    last = [s for s in segs if s["end"] <= e + 0.01][-1]
    assert last["text"].endswith(".")
    assert e - b >= 60


def test_split_parts():
    segs = make_segments(600)
    parts = highlights.split_parts(0, 170, segs, 90)
    assert len(parts) == 2
    assert parts[0][0] == 0 and parts[-1][1] == 170
    assert all(b - a <= 100 for a, b in parts)
    assert highlights.split_parts(0, 60, segs, 90) == [(0, 60)]


def test_subtitles_ass():
    segs = make_segments(100)
    ass = subtitles.build_ass(segs, 10, 30, "Montserrat", "Partie 1/2")
    assert "PARTIE 1/2" in ass
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert lines and all("\\c&H00E5FF&" in l for l in lines)
    assert subtitles.ts(3661.5) == "1:01:01.50"


def test_group_words():
    words = [{"start": i, "end": i + 0.5, "word": "a"} for i in range(7)]
    groups = subtitles.group_words(words, max_pause=2)
    assert [len(g) for g in groups] == [3, 3, 1]


def test_tiktok_chunks():
    assert chunk_plan(30 * MB) == (30 * MB, 1)
    size, n = chunk_plan(150 * MB)
    assert size == 10 * MB and n == 15
    last = 150 * MB - (n - 1) * size
    assert 5 * MB <= last <= 128 * MB


def test_hook_title_at_top():
    from app.pipeline import llm
    segs = [{"start": 10, "end": 12, "text": "salut", "words": [{"start": 10, "end": 12, "word": "salut"}]}]
    ass = subtitles.build_ass(segs, 10, 30, "Montserrat", "Partie 1/2", "Il a vraiment fait ça en plein live devant tout le monde", 4)
    hook_line = [l for l in ass.splitlines() if ",Hook," in l][0]
    assert hook_line.startswith("Dialogue: 2,0:00:00.00,0:00:04.00,Hook")
    assert hook_line.endswith("…") and r"IL A VRAIMENT FAIT\NÇA" in hook_line
    assert "60,60,440,1" in ass  # « Partie » descend sous l'accroche
    assert llm.parse('{"score": 8, "title": "Titre", "hook": "Il a osé !", "question": "Tu valides ?"}') == (8.0, "Titre", "Il a osé !", "Tu valides ?")


def test_hook_emojis(tmp_path):
    from app.pipeline import hook
    assert hook.add_emojis("Il a osé 😱🔥💀") == "Il a osé 😱"
    assert hook.add_emojis("Il a osé 😱🔥💀", max_emojis=2) == "Il a osé 😱🔥"
    assert hook.add_emojis("Il perd tout son argent") == "Il perd tout son argent 💰"
    assert hook.add_emojis("Regardez bien", "un moment calme") == "Regardez bien 👀"
    assert hook.strip_emojis("Trop fort 🔥 non ❤️") == "Trop fort non"
    ass = subtitles.build_ass([], 0, 10, "Montserrat", "", "Salut 😂", 0, hook_in_ass=False)
    assert ",Hook," not in ass.split("[Events]")[1]
    n = hook.render_png("Il a osé faire ça en plein live 😱🔥", "Montserrat", tmp_path / "h.png")
    assert n == 2 and (tmp_path / "h.png").stat().st_size > 1000


def test_render_overlay_cmd(tmp_path):
    from app.pipeline import render
    cmd = render.build_cmd(tmp_path / "s.mp4", 0, 10, "c.ass", tmp_path / "o.mp4", "libx264", "/x/h.png", 4)
    assert cmd[cmd.index("-filter_complex") - 1] == "/x/h.png"
    vf = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:v]overlay=0:632-h" in vf and "lte(t,4.00)" in vf and vf.index("overlay=0:") < vf.index("ass=")
