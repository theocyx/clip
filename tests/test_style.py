"""Habillage « magazine » des Shorts (app/pipeline/style.py) : titre, cartes, miniature, mise en page."""
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.config import get_settings
from app.pipeline import chat, process, render, style, subtitles
from app.pipeline.facecam import Box


def hot(text):
    return [w for w, f in style.key_words(text) if f]


def test_title_key_words_end_of_title_or_caps():
    assert hot("Anyme juge GTA 6") == ["GTA", "6"]
    assert hot("Michou a leak le GP Explorer 4 ?!") == ["EXPLORER", "4", "?!"]
    assert hot("Il vole un CAMION en pleine ville ?! 😱") == ["CAMION"]  # mot écrit en majuscules par l'IA
    assert hot("Wankil n'est pas convaincu par GTA 6") == ["GTA", "6"]
    assert all(w.isupper() or not w.isalpha() for w, _ in style.key_words("il est choqué"))
    assert style.key_words("") == []


def test_title_and_cards_are_drawn(tmp_path):
    h = style.title_png("Il vole un camion en pleine ville ?!", "GTA 6", tmp_path / "t.png")
    assert h > 150
    im = Image.open(tmp_path / "t.png")
    assert im.size == (1080, h) and im.mode == "RGBA"
    assert style.credit_png("Tonton", "tonton_fr", "twitch", None, tmp_path / "c.png") > 300
    assert style.chat_card_png("lea_rp", "nan mais il a vraiment fait ça mdr", tmp_path / "m.png") > 150
    assert style.chat_card_png("x", "😂😂", tmp_path / "e.png") == 0
    assert style.title_png("", "GTA 6", tmp_path / "v.png") == 0


def test_thumbnail_has_title_block_and_facecam(tmp_path):
    game = Image.new("RGB", (1920, 1080), (40, 120, 60))
    cam = Image.new("RGB", (480, 270), (200, 150, 120))
    out = style.thumbnail(game, cam, "Il se croit encore dans le jeu ?!", "GTA 6", tmp_path / "th.jpg")
    assert out and out.exists() and out.stat().st_size < 2_000_000
    im = Image.open(out)
    assert im.size == (1080, 1920)
    accent = style.accent_for("Il se croit encore dans le jeu ?!")
    # bloc de couleur du titre présent dans le haut de l'image
    px = [im.getpixel((x, y)) for x in range(100, 980, 20) for y in range(200, 700, 10)]
    assert any(sum(abs(a - b) for a, b in zip(p, accent)) < 40 for p in px)
    assert style.thumbnail(None, cam, "x", "", tmp_path / "n.jpg") is None


def test_chat_messages_are_filtered():
    assert style.good_message("nan mais il a vraiment fait ça mdr")
    assert not style.good_message("KEKW KEKW KEKW")
    assert not style.good_message("regarde ça https://spam.com wow")
    assert not style.good_message("@pote t'as vu ça frère")
    assert not style.good_message("ferme ta gueule sale batard")
    assert not style.good_message("merci pour le sub frérot")
    msgs = [(1, "a", "MDRRRR"), (2, "b", "il a vraiment pas réfléchi"), (3, "c", "il a vraiment craqué là"),
            (4, "d", "omg")]
    assert style.pick_message(msgs)[0] in ("b", "c")
    assert style.pick_message([(1, "a", "KEKW")]) is None


def test_chat_log_keeps_nick_and_old_files_still_work(tmp_path):
    assert chat.nick_of(":lea_rp!lea_rp@lea_rp.tmi.twitch.tv PRIVMSG #x :salut") == "lea_rp"
    assert chat.nick_of("PING :tmi") == ""
    t0 = 1000.0
    (tmp_path / "rec_starts.txt").write_text(f"{t0}\n")
    lines = [f"{t0 + 10 + i * 0.1:.2f}\tmdr incroyable\tuser{i}" for i in range(25)] + [f"{t0 + 12:.2f}\tvieux format"]
    (tmp_path / "chat.tsv").write_text("\n".join(lines) + "\n")
    act = chat.activity(tmp_path, 30, delay=0)
    assert act is not None and act[10] > 0
    got = chat.messages(tmp_path, 9, 13)
    assert ("user0", "mdr incroyable") == got[0][1:]
    assert got[-1][1:] == ("", "vieux format")
    assert chat.messages(tmp_path, 50, 60) == []


def test_magazine_layout_puts_game_on_top_and_cards(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "clip_style", "magazine")
    cam = Box(0.02, 0.75, 0.3, 0.22)
    png = tmp_path / "t.png"
    Image.new("RGBA", (1080, 200)).save(png)
    cmd = render.build_cmd(Path("in.mp4"), 0, 30, "c.ass", tmp_path / "o.mp4", "libx264", cam=cam,
                           extras=[(str(png), 150, 0.0, 4.0), (str(png), 300, 27.0, 30.0), ("", 0, 0, 1)])
    graph = cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""
    assert "[bot][top]vstack" in graph
    assert f"scale=1080:{1920 - get_settings().mag_cam_height}" in graph
    assert graph.count("overlay=x=(W-w)/2") == 2  # carte vide ignorée
    assert "between(t,27.00,30.00)" in graph
    assert cmd.count(str(png)) == 2
    monkeypatch.setattr(get_settings(), "clip_style", "tiktok")
    cmd = render.build_cmd(Path("in.mp4"), 0, 30, "c.ass", tmp_path / "o.mp4", "libx264", cam=cam)
    assert "[top][bot]vstack" in cmd[cmd.index("-filter_complex") + 1]


def test_ass_subtitles_at_junction_and_source_line():
    segs = [{"start": 0, "end": 10, "words": [{"start": 1, "end": 1.4, "word": "salut"}]}]
    ass = subtitles.build_ass(segs, 0, 10, "Montserrat", sub_v=754, source="« GTA 6 » (stream Tonton)",
                              source_from=4)
    assert ",70,70,754,1" in ass
    assert "Dialogue: 1,0:00:04.00,0:00:10.00,Src" in ass and "(stream Tonton)" in ass
    assert ",70,70,540,1" in subtitles.build_ass(segs, 0, 10, "Montserrat")


def test_source_line_is_clean():
    assert process.source_line("🔴 GTA 6 DAY ONE : ON DÉCOUVRE VICE CITY !! LIVE 12H", "Tonton") == \
        "« GTA 6 DAY ONE : ON DÉCOUVRE VICE CITY » (stream Tonton)"
    assert process.source_line("", "Tonton") == "(stream Tonton)"
    assert len(process.source_line("a" * 200, "")) < 60


def test_dressing_places_title_chat_and_credit(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "credit_streamer", True)
    monkeypatch.setattr(process, "streamer_name", lambda v: "Tonton")
    monkeypatch.setattr(process, "video_source", lambda v: SimpleNamespace(platform="twitch",
                                                                         url="https://twitch.tv/tonton_fr"))
    monkeypatch.setattr(style, "twitch_avatar", lambda *a, **k: None)
    work = process.video_dir(4242)
    work.mkdir(parents=True, exist_ok=True)
    (work / "rec_starts.txt").write_text("1000\n")
    (work / "chat.tsv").write_text("1023.00\til a vraiment fait ça mdr\tlea\n")
    video = SimpleNamespace(id=4242, title="GTA 6 day one", source_id=1)
    m = SimpleNamespace(title="Tonton GTA 6 : le camion", hook="", features={"peak": 20.0, "text": ""})
    out = tmp_path / "c_p1.mp4"
    d = process.dressing(video, Path("absent.mp4"), out, m, Box(0, 0.7, 0.3, 0.3), [(10.0, 40.0)], None,
                         "Il vole un camion")
    names = [Path(e[0]).name for e in d.extras]
    assert names == ["c_p1_title.png", "c_p1_credit.png", "c_p1_chat.png"]
    title, credit, card = d.extras
    assert title[2:] == (0.0, s.title_seconds)
    assert credit[2:] == (30.0 - s.credit_seconds, 30.0)
    assert card[2] == 10.0 and card[3] <= credit[2]
    assert d.title and d.sub_v == render.cam_height() + 24
    assert d.source == "« GTA 6 day one » (stream Tonton)" and d.source_from == s.title_seconds


def test_thumbnail_sent_after_youtube_upload(monkeypatch, tmp_path):
    from app import worker

    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(worker, "clip_data", lambda c: {"thumb": str(thumb)})
    monkeypatch.setattr(worker.youtube, "set_thumbnail", lambda vid, p: calls.append(vid) or False)
    monkeypatch.setattr(worker, "_thumb_refused_at", 0.0)
    clip = SimpleNamespace(id=1)
    assert worker.send_thumbnail(clip, "abc") is False and calls == ["abc"]
    assert worker.send_thumbnail(clip, "def") is False and calls == ["abc"]  # refusée : pas avant demain
    monkeypatch.setattr(worker, "_thumb_refused_at", 0.0)
    monkeypatch.setattr(worker, "clip_data", lambda c: {})
    assert worker.send_thumbnail(clip, "ghi") is False and calls == ["abc"]
