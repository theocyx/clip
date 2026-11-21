"""Vidéos d'actu générées : flux RSS, script vérifié, voix, plans, illustrations libres, montage, publication."""
import json
import subprocess
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlmodel import select

from app import curate, db, learning, worker
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, Video
from app.news import feeds, generate, montage, script, tts, visuals

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Millenium</title>
<item><title>GTA 6 : 25 millions de ventes en 24 heures</title><link>https://ex.fr/a</link>
<description>&lt;p&gt;Rockstar annonce 25 millions de copies vendues en 24 heures.&lt;/p&gt;</description>
<pubDate>{d1}</pubDate></item>
<item><title>Bon plan : la PS5 en promo</title><link>https://ex.fr/b</link><description>promo</description>
<pubDate>{d1}</pubDate></item>
<item><title>Vieux sujet</title><link>https://ex.fr/c</link><description>x</description>
<pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Gamekult</title>
<entry><title>GTA 6 : 25 millions de ventes dès le premier jour</title><link href="https://gk.fr/x"/>
<summary>Un record.</summary><updated>{iso}</updated></entry></feed>"""


def _now():
    return datetime.utcnow()


def test_parse_rss_atom_google_and_rank():
    d1 = _now().strftime("%a, %d %b %Y %H:%M:%S GMT")
    rss = feeds.parse(RSS.format(d1=d1), "https://ex.fr/rss")
    assert [i.title for i in rss][0] == "GTA 6 : 25 millions de ventes en 24 heures"
    assert rss[0].source == "Millenium" and "25 millions" in rss[0].summary and "<p>" not in rss[0].summary
    atom = feeds.parse(ATOM.format(iso=_now().isoformat() + "Z"), "https://gk.fr/feed")
    assert atom[0].link == "https://gk.fr/x" and atom[0].source == "Gamekult"
    g = feeds.parse("""<rss><channel><title>Google</title><item><title>GTA 6 cartonne - Le Monde</title>
        <link>https://news.google.com/rss/articles/abc</link></item></channel></rss>""",
                    "https://news.google.com/rss/search?q=x")
    assert g[0].title == "GTA 6 cartonne" and g[0].source == "Le Monde"
    items = [i for i in rss + atom if feeds.matches(i, [], ["promo", "bon plan"])]
    ranked = feeds.rank(items, 24)
    # les deux articles sur le même sujet sont regroupés : le sujet est « repris », le vieux sujet est écarté
    assert len(ranked) == 1 and ranked[0].buzz == 1 and ranked[0].related
    assert feeds.matches(rss[0], ["gta"], []) and not feeds.matches(rss[0], ["fortnite"], [])
    assert feeds.parse("pas du xml") == []


def test_article_text_keeps_facts_only():
    page = """<html><head><meta property="og:description" content="Rockstar annonce un record historique."></head>
    <body><nav><p>Menu menu menu menu menu menu menu menu menu menu menu menu menu menu</p></nav>
    <p>Le jeu s'est vendu à 25 millions d'exemplaires en une seule journée selon l'éditeur.</p>
    <p>Acceptez les cookies pour continuer votre lecture sur notre site internet ok ok ok.</p></body></html>"""
    text = feeds.article_text(page)
    assert text.startswith("Rockstar annonce") and "25 millions" in text
    assert "Menu" not in text and "cookies" not in text


def _llm_json(**over):
    data = {"score": 8, "title": "GTA 6 pulvérise tous les records de vente", "hook": "GTA 6 explose tout 🤯",
            "sentences": [{"text": "GTA 6 vient de battre un record historique en 24 heures.", "visual": "game"},
                          {"text": "Rockstar annonce 25 millions de copies vendues dès le premier jour.",
                           "visual": "stock chart"},
                          {"text": "Aucun jeu n'avait jamais fait mieux dans toute l'histoire.", "visual": "trophy"},
                          {"text": "Les serveurs en ligne ont même saturé pendant plusieurs heures.",
                           "visual": "server room"},
                          {"text": "Et toi, tu y joues déjà ou tu attends la version PC ?", "visual": "gamer"}],
            "question": "Tu y joues déjà ?", "hashtags": ["#GTA6", "gta", "Rockstar Games"]}
    data.update(over)
    return json.dumps(data)


def test_script_parse_and_checks(monkeypatch):
    monkeypatch.setattr(get_settings(), "news_target_seconds", 12.0)
    sc = script.parse("blabla " + _llm_json() + " fin")
    assert sc and len(sc.sentences) == 5 and sc.visuals[1] == "stock chart"
    assert sc.hashtags == ["gta6", "gta", "rockstargames"] and sc.score == 8
    facts = "Rockstar annonce 25 millions de copies vendues en 24 heures."
    assert script.problems(sc, facts) == []
    # chiffre inventé (40 millions) : la phrase est retirée, le reste du script est gardé
    sents = json.loads(_llm_json())["sentences"]
    bad = script.parse(_llm_json(title="GTA 6 : 40 millions de ventes", hook="40 millions 🤯",
                                 sentences=sents[:2] + [{"text": "Il s'est vendu 40 millions de fois, waouh.",
                                                         "visual": ""}] + sents[2:]))
    assert script.problems(bad, facts) == ["chiffres absents de l'article"]
    assert script.sanitize(bad, facts, "GTA 6 cartonne") == ["1 phrase(s)", "titre", "accroche"]
    assert len(bad.sentences) == 5 and len(bad.visuals) == 5 and bad.title == "GTA 6 cartonne" and bad.hook == ""
    assert script.problems(bad, facts) == []
    assert script.invented_numbers("En 2026, 3 jeux et 25,5 %", "en 2026 on a 25.5 %") == []
    assert script.parse("pas de json") is None


def test_script_write_retries_then_accepts(monkeypatch):
    from app.pipeline import llm
    monkeypatch.setattr(get_settings(), "news_target_seconds", 12.0)
    monkeypatch.setattr(get_settings(), "news_script_steps", False)  # ancienne méthode (une seule réponse)
    short = json.dumps({"score": 8, "title": "t", "sentences": [{"text": "Trop court.", "visual": ""}]})
    answers = iter([short, _llm_json(title="GTA 6 : 99 millions !")])
    prompts = []
    monkeypatch.setattr(llm, "provider", lambda: "ollama")
    monkeypatch.setattr(llm, "_ask_ollama", lambda p, **kw: prompts.append(p) or next(answers))
    sc = script.write("GTA 6 record", "Rockstar : 25 millions de copies en 24 heures.", "Millenium")
    # 2e essai : le titre au chiffre inventé est remplacé par celui de l'article au lieu de tout jeter
    assert sc and sc.title == "GTA 6 record" and len(prompts) == 2
    assert "trop court" in prompts[1] and "refusé" in prompts[1]  # le 2e essai sait pourquoi le 1er a été refusé
    monkeypatch.setattr(llm, "provider", lambda: "none")
    assert script.write("x", "y", "z") is None


def _synth(text):
    sr = 16000
    n = int(sr * len(text.split()) * 0.3)
    audio = np.concatenate([np.zeros(1600), 0.4 * np.sin(np.arange(n) / 5), np.zeros(3200)]).astype(np.float32)
    return audio, sr


def test_speak_timings(tmp_path):
    sents = ["Phrase numéro un, assez courte.", "Et voici la deuxième phrase du script ?"]
    spoken = tts.speak(sents, tmp_path / "v.wav", synth=_synth)
    assert len(spoken) == 2 and spoken[0].start == 0
    assert spoken[1].start == pytest.approx(spoken[0].end + tts.GAP, abs=0.01)  # silences retirés + petit blanc
    w = spoken[1].words
    assert [x["word"] for x in w][-1] == "script ?" or w[-1]["word"] == "?" or w[-1]["word"].endswith("?")
    assert w[0]["start"] == spoken[1].start and w[-1]["end"] == pytest.approx(spoken[1].end, abs=0.01)
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(w, w[1:]))
    assert tts.for_speech("GTA 6 coûte 80 € (+10 %)") == "GTA six coûte 80 euros (+10 pour cent)"
    assert tts.voice_files("fr_FR-tom-medium")[0].endswith("/fr/fr_FR/tom/medium/fr_FR-tom-medium.onnx")
    with pytest.raises(ValueError):
        tts.voice_files("../../etc")


def test_plan_covers_video_without_gaps():
    sp = [tts.Spoken("a", 0.0, 2.5, []), tts.Spoken("b", 2.7, 11.0, []), tts.Spoken("c", 11.2, 13.0, [])]
    cuts = montage.plan(sp, 13.7)
    assert cuts[0][1] == 0 and cuts[-1][2] == 13.7
    assert all(a[2] == b[1] for a, b in zip(cuts, cuts[1:]))
    assert [c[0] for c in cuts].count(1) == 2  # phrase longue : deux plans
    assert max(b - a for _, a, b in cuts) <= 5.5


def test_free_licenses_only():
    pages = {
        "1": {"index": 1, "pageid": 1, "imageinfo": [{"width": 2000, "mime": "image/jpeg", "url": "u1",
                                                      "extmetadata": {"LicenseShortName": {"value": "CC BY-NC 2.0"}}}]},
        "2": {"index": 2, "pageid": 2, "imageinfo": [{"width": 400, "mime": "image/jpeg", "url": "u2",
                                                      "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]},
        "3": {"index": 3, "pageid": 3, "imageinfo": [{"width": 3000, "mime": "image/jpeg", "url": "u3",
                                                      "thumburl": "t3", "descriptionurl": "d3",
                                                      "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                                                                      "Artist": {"value": "<a>Jean</a>"}}}]},
    }
    pick = visuals.commons_pick(pages, set())
    assert pick["id"] == "commons-3" and pick["url"] == "t3" and pick["artist"] == "Jean"
    assert visuals.commons_pick(pages, {"commons-3"}) is None
    files = [{"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "land"},
             {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "hd"},
             {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "4k"}]
    assert visuals.best_video_file(files)["link"] == "hd"
    assert visuals.key_words("Rockstar annonce 25 millions de copies.") == "25 millions"


def test_find_falls_back_to_animated_words(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "news_wikimedia", False)
    shot = visuals.find("game controller", "Le jeu fait 25 millions de ventes", tmp_path, set(), 0)
    assert shot.kind == "kinetic" and shot.text == "25 millions" and shot.credit == ""
    card = visuals.card("25 millions", tmp_path / "c.jpg")  # carte fixe gardée quand NEWS_MOTION=false
    from PIL import Image
    assert Image.open(card.path).size == (1080, 1920)


def test_number_shots():
    s = visuals.number_shot("Selon les organisateurs, ils étaient 25 000 dans les rues.")
    assert s.kind == "counter" and s.text == "25 000" and s.meta["unit"] == ""
    s = visuals.number_shot("Rockstar annonce 25 millions de copies vendues.")
    assert s.text == "25" and s.meta["unit"] == "millions de copies"
    assert visuals.number_shot("En 2026, le prix monte.") is None  # une année seule
    assert visuals.number_shot("Aucun chiffre ici.") is None
    from app.news import motion
    assert motion.parse_number("1 250,5 euros")[:2] == (1250.5, 1)
    assert motion.fmt_number(1250.5, 1) == "1 250,5" and motion.fmt_number(25000, 0) == "25 000"


def test_motion_frames_and_map(tmp_path):
    from app.news import motion
    acc = (255, 212, 0)
    poly = motion.project(100, 50, 540, 960, ry=30)
    assert poly.shape == (4, 2) and poly[1, 0] - poly[0, 0] < 100  # pivoté : plus étroit
    canvas = np.zeros((motion.H, motion.W, 3), np.uint8)
    motion.warp_onto(canvas, np.full((50, 100, 4), 255, np.uint8), poly)
    assert canvas[960, 540].tolist() == [255, 255, 255] and canvas[0, 0].tolist() == [0, 0, 0]
    calls = []

    def fetch(z, x, y):
        calls.append((z, x, y))
        return np.full((256, 256, 3), 60 + z * 10, np.uint8)

    need = motion.map_tiles_needed(2.35, 48.85, 40, 12.5)
    assert 20 < len(need) < 600 and all(3 <= z <= 13 for z, _, _ in need)
    ring = [(2.30, 48.87), (2.40, 48.87), (2.40, 48.83), (2.30, 48.83)]
    fly = motion.map_fly(2.35, 48.85, "Paris", 40, fetch, acc, z1=12.5, attribution="EOX", sub="France",
                         rings=[ring])
    first, last = fly(0), fly(39)
    assert first.shape == (motion.H, motion.W, 3) and not np.array_equal(first, last)
    red = (last[..., 0] > 200) & (last[..., 1] < 80)
    assert red.sum() > 300  # l'épingle rouge est posée
    assert (last[..., 2].astype(int) - last[..., 0] > 40).sum() > 10000  # zone surlignée en bleu
    assert motion.dms(48.8566, "N", "S") == "48°51'24\"N" and motion.nice_scale(10.0) == (200, "2 km")
    assert set(calls) <= need  # aucune tuile en dehors de celles téléchargées à l'avance
    from PIL import Image
    Image.new("RGB", (400, 300), (0, 200, 0)).save(tmp_path / "p.jpg")
    for gen in (motion.kinetic("Record historique", 20, 0, acc), motion.counter("25 000", "personnes", 20, 1, acc),
                motion.panel_photo(tmp_path / "p.jpg", 20, 0, "ILLUSTRATION"),
                motion.quiz("Et toi, tu y vas ?", ["Oui", "Non"], 20, acc)):
        assert gen(0).shape == (motion.H, motion.W, 3) and gen(19).any()
    out = motion.write_frames(motion.kinetic("Test", 6, 0, acc), 6, tmp_path / "k.mp4")
    assert out.stat().st_size > 0
    wav = motion.whoosh_track([1.0, 2.5], 3.0, tmp_path / "w.wav")
    import wave
    with wave.open(str(wav)) as w:
        assert abs(w.getnframes() / w.getframerate() - 3.0) < 0.01


def test_probe_sheets(tmp_path):
    from app.news import probe
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=180x320:d=2", "-f", "lavfi",
                    "-i", "color=c=blue:s=180x320:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                    "-map", "[v]", str(src)], check=True)
    assert len(probe.cuts(src)) == 1
    out = probe.sheet(src, [0.5, 1.5, 2.5, 3.5], tmp_path / "s.jpg")
    from PIL import Image
    assert Image.open(out).size == (4 * 270, 480)


def test_render_news_video(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_codec", "libx264")
    sents = ["GTA 6 bat un record en 24 heures.", "Rockstar annonce 25 millions de copies vendues."]
    spoken = tts.speak(sents, tmp_path / "voice.wav", synth=_synth)
    total = round(spoken[-1].end + montage.TAIL, 3)
    cuts = montage.plan(spoken, total)
    shots = [visuals.card(visuals.key_words(sents[i]), tmp_path / f"c{j}.jpg") for j, (i, _, _) in enumerate(cuts)]
    music = get_settings().data_dir / "music"
    music.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=3", str(music / "m.mp3")],
                   check=True)
    out = tmp_path / "out" / "news.mp4"
    dur = montage.build(shots, spoken, tmp_path / "voice.wav", "GTA 6 explose tout 🤯", "Millenium", out, tmp_path)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-show_entries", "format=duration", "-of", "json", str(out)],
                           capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(dur, abs=0.3)
    if get_settings().news_motion:  # habillage incrusté : pastille rouge de la marque en haut à gauche
        assert not (out.parent / "news.ass").exists()
        png = tmp_path / "f.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.0", "-i", str(out), "-frames:v", "1", str(png)],
                       check=True)
        from PIL import Image
        px = np.asarray(Image.open(png).convert("RGB"))[95, 60]
        assert px[0] > 180 and px[1] < 90
    else:
        ass = (out.parent / "news.ass").read_text()
        assert "SOURCE : MILLENIUM" in ass and "RECORD" in ass
    (music / "m.mp3").unlink()


def _item(title="GTA 6 : 25 millions en 24 heures", link="https://ex.fr/gta"):
    return feeds.Item(title=title, link=link, summary="Rockstar annonce 25 millions de copies en 24 heures.",
                      source="Millenium", published=_now())


def test_make_creates_clip_and_queues(monkeypatch, tmp_path):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_target_seconds", 12.0)
    monkeypatch.setattr(cfg, "news_min_seconds", 5.0)
    monkeypatch.setattr(feeds, "fetch_article", lambda item: item.summary)
    monkeypatch.setattr(script, "write", lambda *a, **k: script.parse(_llm_json()))
    monkeypatch.setattr(tts, "speak", lambda sents, out, speed=None, **k: _real_speak(sents, out, synth=_synth))
    built = {}

    def fake_build(shots, spoken, voice, hook, source, out, work, on_progress=None, look=None):
        built.update(shots=shots, hook=hook, source=source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return spoken[-1].end + montage.TAIL

    monkeypatch.setattr(montage, "build", fake_build)
    monkeypatch.setattr(visuals, "find", lambda q, s, w, u, j, prefer_video=True, **k:
                        visuals.Shot(tmp_path / "x.jpg", "image", "Image : Jean (CC BY 4.0)" if j == 0 else ""))
    item = _item()
    v = generate._new_video(item)
    cid = generate.make(v.id, item)
    with db.session() as s:
        clip, video = s.get(Clip, cid), s.get(Video, v.id)
        info = json.loads(s.get(ClipInfo, cid).data)
        posts = s.exec(__import__("sqlmodel").select(Post).where(Post.clip_id == cid)).all()
    assert video.status == "done" and clip.status == "ready" and clip.title.startswith("GTA 6 pulvérise")
    assert info["kind"] == "news" and info["source"] == "Millenium" and info["credits"] == ["Image : Jean (CC BY 4.0)"]
    assert built["source"] == "Millenium" and len(built["shots"]) >= 5
    assert {p.platform for p in posts} == {"youtube", "tiktok"}
    # textes de publication : source + crédits, hashtags du sujet
    desc, tags = worker.description(clip, video)
    assert "📰 Source : Millenium" in desc and "Image : Jean" in desc and tags[0] == "gta6"
    cap = worker.tiktok_caption(clip, desc, tags)
    assert cap.startswith("GTA 6 pulvérise") and "#gta6" in cap and "Source : Millenium" in cap
    # tri de la file : pas recalé comme « hors GTA » ni « pas dans les meilleurs », ni retitré
    assert curate.reason(clip, video, info, bar=999) == ""
    assert curate.retitle(clip, video, info, [clip.title]) is None
    assert curate.reason(clip, video, {**info, "llm": 3}) .startswith("Sujet d'actu trop faible")
    # pas utilisé pour l'apprentissage du montage des clips
    assert learning._clip_rows({cid: 1.0}) == []
    # même article : plus jamais proposé
    monkeypatch.setattr(feeds, "candidates", lambda *a: [item, _item("Autre sujet totalement différent", "https://ex.fr/o")])
    assert [i.link for i in generate.fresh_candidates()] == ["https://ex.fr/o"]


_real_speak = tts.speak


def test_low_score_topic_is_skipped(monkeypatch):
    monkeypatch.setattr(feeds, "fetch_article", lambda item: item.summary)
    monkeypatch.setattr(script, "write", lambda *a, **k: script.parse(_llm_json(score=3)))
    item = _item(link="https://ex.fr/faible")
    v = generate._new_video(item)
    assert generate.make(v.id, item) is None
    with db.session() as s:
        assert s.get(Video, v.id).status == "skipped"


def test_schedule_toggle_and_retry(monkeypatch):
    cfg = get_settings()
    generate.set_enabled(False)
    assert not generate.due()
    generate.set_enabled(True)
    monkeypatch.setattr(cfg, "news_hours", "")
    monkeypatch.setattr(cfg, "news_per_day", 99)
    assert generate.due()
    monkeypatch.setattr(cfg, "news_per_day", 0)
    assert not generate.due()
    generate.request_now()
    assert generate.take_request() and not generate.take_request()
    # interrompu par un redémarrage → refait depuis le même article
    item = _item(link="https://ex.fr/coupe")
    v = generate._new_video(item)
    generate.reset_interrupted()
    calls = []
    monkeypatch.setattr(generate, "make", lambda vid, it: calls.append((vid, it.link)) or 42)
    assert generate.run_once() == 42 and calls == [(v.id, "https://ex.fr/coupe")]


def test_dashboard_news_card(monkeypatch):
    from fastapi.testclient import TestClient

    from app.web import main as web
    web.app.dependency_overrides[web.auth] = lambda: None
    try:
        c = TestClient(web.app)
        r = c.get("/")
        assert r.status_code == 200 and "Vidéos d'actu auto" in r.text
        assert c.post("/news", data={"action": "off"}, follow_redirects=False).status_code == 303
        assert not generate.enabled()
        c.post("/news", data={"action": "on"}, follow_redirects=False)
        assert generate.enabled()
        c.post("/news", data={"action": "now"}, follow_redirects=False)
        assert generate.take_request()
        assert c.post("/news", data={"action": "x"}).status_code == 400
        # carte aussi sur la page Vidéos, avec l'étape en cours d'une actu
        v = generate._new_video(_item(link="https://ex.fr/etapes"))
        generate._set(v.id, progress=20)
        r = c.get("/videos")
        assert "Vidéos d'actu auto" in r.text and "voix + images (20 %)" in r.text and "actu en cours" in r.text
        assert "Voix + images" in r.text  # étapes de l'actu dans le tableau des vidéos
        p = c.get(f"/videos/progress?ids={v.id}").json()[str(v.id)]
        assert p["active"] and "Script IA" in p["html"]
    finally:
        web.app.dependency_overrides.clear()


def test_no_emoji_on_grave_topics():
    assert generate.header_text("Incendie meurtrier à Lyon 💀", "", "Trois morts dans un incendie.") == \
        "Incendie meurtrier à Lyon"
    assert generate.header_text("GTA 6 bat tous les records 🤯", "", "Record de ventes.").endswith("🤯")
    assert generate.header_text("Le prix du pain augmente", "", "Le pain coûte plus cher.", emojis=False) == \
        "Le prix du pain augmente"


def test_pick_shots_map_counter_and_photos(monkeypatch, tmp_path):
    from app.news import geo
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_map", True)
    monkeypatch.setattr(geo, "geocode", lambda place: geo.Place(2.35, 48.85, "Paris", "city", "France",
                                                                 [[(2.2, 48.9), (2.5, 48.9), (2.4, 48.8)]])
                        if place else None)

    class FakeTiles:
        def __init__(self):
            pass

        def prefetch(self, keys):
            return 0

        def get(self, z, x, y):
            return np.zeros((256, 256, 3), np.uint8)

    monkeypatch.setattr(geo, "Tiles", FakeTiles)
    monkeypatch.setattr(visuals, "find", lambda q, s, w, u, j, prefer_video=True, **k: visuals.Shot(tmp_path / "p.jpg",
                                                                                                "image"))
    sc = script.parse(_llm_json(place="Paris, France"))
    cuts = [(0, 0, 3), (1, 3, 6), (2, 6, 9), (2, 9, 12), (3, 12, 15), (4, 15, 18)]
    shots, _ = generate.pick_shots(sc, cuts, tmp_path)
    kinds = [s.kind for s in shots]
    assert kinds[0] == "map" and shots[0].credit.startswith("Carte : Sentinel-2")
    assert kinds.count("counter") == 1 and kinds[1] == "counter"  # « 25 millions de copies »
    assert kinds[2:] == ["image"] * 3 + ["quiz"]  # question finale : quiz « ET TOI ? »
    assert shots[-1].text.endswith("?")
    monkeypatch.setattr(cfg, "news_map", False)
    assert generate.pick_shots(sc, cuts, tmp_path)[0][0].kind != "map"


def test_style_page(tmp_path):
    from fastapi.testclient import TestClient
    from PIL import Image

    from app.web import main as web
    d = get_settings().data_dir / "style"
    d.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40)).save(d / "123_planche.jpg")
    (d / "123.json").write_text(json.dumps({"url": "https://x/123", "duree_s": 64.6, "plans": 5,
                                            "duree_moyenne_plan_s": 12.9}))
    web.app.dependency_overrides[web.auth] = lambda: None
    try:
        c = TestClient(web.app)
        r = c.get("/style")
        assert "123_planche" in r.text and "64.6 s, 5 plans" in r.text
        assert c.get("/style/file/123_planche.jpg").status_code == 200
        assert c.get("/style/file/..%2Fclip.db").status_code == 404
        assert c.get("/style/file/clip.db").status_code == 404
    finally:
        web.app.dependency_overrides.clear()


def test_overlay_titles_chapters_and_script_fields():
    from app.news import overlay
    sp = [tts.Spoken("GTA 6 sort demain.", 0.0, 1.5, tts.word_times("GTA 6 sort demain.", 0.0, 1.5)),
          tts.Spoken("Rockstar vise 25 millions de ventes.", 1.7, 3.5,
                     tts.word_times("Rockstar vise 25 millions de ventes.", 1.7, 3.5)),
          tts.Spoken("Et toi, tu l'achètes ?", 3.7, 5.0, tts.word_times("Et toi, tu l'achètes ?", 3.7, 5.0))]
    ov = montage.make_overlay(sp, "GTA 6 arrive 🔥", {"chapters": ["LES FAITS", "", "ET TOI ?"], "topic": "GTA 6"})
    assert ov.titles[0] == "GTA 6 ARRIVE" and ov.titles[1] == "25 MILLIONS" and ov.titles[2] == "TON AVIS ?"
    assert ov.hud.tabs == ["LES FAITS", "ET TOI ?"] and ov.chapter_of == [0, 0, 1]
    frame = ov.apply(np.zeros((overlay.H, overlay.W, 3), np.uint8), 2.0)
    assert frame[overlay.SUB_Y].any() and frame[overlay.TITLE_Y].any()
    assert overlay.chapters(["A", "B", "A", ""]) == (["A", "B"], [0, 1, 0, 1])
    sc = script.parse(json.dumps({"score": 7, "title": "t", "hook": "h", "topic": "Grève SNCF",
                                  "answers": ["Oui", "Non", "Bof"], "sentences": [
                                      {"text": "Grève à Lyon.", "visual": "train", "title": "GRÈVE",
                                       "chapter": "LES FAITS", "place": "Lyon, France"},
                                      "Deuxième phrase sans détails."]}))
    assert sc.topic == "Grève SNCF" and sc.answers == ["Oui", "Non"]
    assert sc.titles == ["GRÈVE", ""] and sc.places == ["Lyon, France", ""] and sc.chapters[0] == "LES FAITS"
    sc.sentences[1] = "Il y a 999 trains supprimés."
    script.sanitize(sc, "Grève à Lyon.")
    assert sc.sentences == ["Grève à Lyon."] and sc.places == ["Lyon, France"] and sc.titles == ["GRÈVE"]


def test_openverse_and_pixabay_sources(tmp_path, monkeypatch):
    res = [{"id": "a", "url": "https://x/a.jpg", "license": "by-nc", "width": 2000},
           {"id": "b", "url": "https://x/b.jpg", "license": "by-nd", "width": 2000},
           {"id": "c", "url": "https://x/c.jpg", "license": "by", "width": 500},
           {"id": "d", "url": "https://x/d.jpg", "license": "by-sa", "license_version": "4.0", "width": 1600,
            "creator": "Jean", "foreign_landing_url": "https://flickr.com/d"},
           {"id": "e", "url": "https://x/e.jpg", "license": "cc0", "width": 1600}]
    pick = visuals.openverse_pick(res, set())
    assert pick["id"] == "openverse-d" and pick["license"] == "CC BY-SA 4.0" and pick["artist"] == "Jean"
    assert visuals.openverse_pick(res, {"openverse-d"})["license"] == "CC0"
    assert visuals.pixabay_video_file({"large": {"url": "l", "width": 3840}, "medium": {"url": "m", "width": 1920},
                                       "small": {"url": "s", "width": 1280}})["url"] == "s"
    calls = []
    monkeypatch.setattr(visuals, "pexels", lambda q, w, u, v=True: calls.append(("pexels", q)))
    monkeypatch.setattr(visuals, "pixabay", lambda q, w, u, v=True: calls.append(("pixabay", q)))
    monkeypatch.setattr(visuals, "commons", lambda q, w, u: calls.append(("commons", q)))
    from PIL import Image
    Image.new("RGB", (900, 900), (10, 20, 30)).save(tmp_path / "o.jpg")

    def fake_openverse(q, w, u):
        calls.append(("openverse", q))
        return visuals.Shot(tmp_path / "o.jpg", "image", "Image : Jean (CC0), via Openverse", "openverse-x") \
            if q == "train station" else None

    monkeypatch.setattr(visuals, "openverse", fake_openverse)
    shot = visuals.find("train station night", "Grève à la gare.", tmp_path, set(), 0)
    assert shot.ident == "openverse-x" and shot.credit.startswith("Image : Jean")
    assert [c[0] for c in calls[:4]] == ["pexels", "pixabay", "openverse", "commons"]
    assert ("openverse", "train station") in calls and ("openverse", "train") not in calls[:4]


def test_stories_topics_and_facts(monkeypatch, tmp_path):
    from app.news import stories
    cfg = get_settings()
    (cfg.data_dir / "stories.txt").write_text("# mes sujets\nRoom 641A\nAffaire du vol 19\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "story_topics", "Mary Celeste, Kryptos")
    t = stories.topics()
    assert t[:4] == ["Room 641A", "Affaire du vol 19", "Mary Celeste", "Kryptos"] and len(t) == len(set(t))
    assert stories.key("Room 641A") == stories.key(" room 641a ") and stories.key("x").startswith("news:story-")
    text = "Room 641A est une pièce secrète.\n\n\nElle est située à San Francisco.\nNotes et références\n[1] x"
    assert stories.clean_extract(text) == "Room 641A est une pièce secrète.\nElle est située à San Francisco."
    pages = {"fr": {"title": "Room 641A", "url": "https://fr.wikipedia.org/wiki/Room_641A", "text": "court " * 50,
                    "lat": 37.78, "lon": -122.39, "lang": "fr"},
             "en": {"title": "Room 641A", "url": "https://en.wikipedia.org/wiki/Room_641A", "text": "long " * 900,
                    "lat": None, "lon": None, "lang": "en"}}
    monkeypatch.setattr(stories, "wiki", lambda topic, lang="fr": pages[lang])
    item = stories.fetch("Room 641A")
    assert item.kind == "story" and item.source == "Wikipedia (en)" and item.key == stories.key("Room 641A")
    assert feeds.fetch_article(item) == item.summary.strip()
    (cfg.data_dir / "stories.txt").unlink()


def test_story_run_once_and_prompt(monkeypatch, tmp_path):
    from app.news import stories
    from app.pipeline import llm
    cfg = get_settings()
    monkeypatch.setattr(llm, "provider", lambda: "ollama")
    asked = {}

    def fake_ask(prompt, timeout=600, max_tokens=None, num_ctx=4096):
        asked.update(prompt=prompt, max_tokens=max_tokens, num_ctx=num_ctx)
        return _llm_json()

    monkeypatch.setattr(llm, "_ask_ollama", fake_ask)
    facts = "GTA 6 record 24 heures 25 millions de copies. " * 3
    monkeypatch.setattr(cfg, "story_target_seconds", 12.0)
    monkeypatch.setattr(cfg, "news_script_steps", False)
    sc = script.write("Room 641A", facts, "Wikipédia", kind="story")
    assert sc is not None and "HISTOIRE VRAIE" in asked["prompt"] and asked["num_ctx"] == 8192
    assert script.target_words("story") < script.target_words("news")
    item = feeds.Item(title="Room 641A", link="https://fr.wikipedia.org/wiki/Room_641A", summary=facts,
                      source="Wikipédia", kind="story", topic="Room 641A")
    monkeypatch.setattr(stories, "candidates", lambda n=3: [item])
    seen = []
    monkeypatch.setattr(generate, "make", lambda vid, it: seen.append(it.kind) or 7)
    assert generate.run_once(story=True) == 7 and seen == ["story"]
    with db.session() as s:
        v = s.exec(select(Video).where(Video.key == stories.key("Room 641A"))).first()
    assert generate.is_story(v) and generate.is_news(v)
    data = json.loads((generate.work_dir(v.id) / "item.json").read_text())
    assert data["kind"] == "story" and data["topic"] == "Room 641A"
    generate.request_story()
    assert generate.take_request(generate.STORY_NOW_KEY) and not generate.take_request(generate.STORY_NOW_KEY)
    look = generate.look(script.parse(_llm_json(topic="ROOM 641A")), item)
    assert look["story"] and look["topic"] == "ROOM 641A" and "WIKIPÉDIA : Room 641A" in look["sources"]


def test_ai_images_story_shots_and_cta(monkeypatch, tmp_path):
    from PIL import Image

    from app.news import motion, overlay
    cfg = get_settings()
    urls = []

    def fake_download(url, dest, headers=None, max_mb=60):
        urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (768, 1152), (90, 30, 30)).save(dest)
        return dest

    monkeypatch.setattr(visuals, "_download", fake_download)
    shot = visuals.ai_image("secret room telephone", tmp_path, set(), "story", seed=3)
    assert shot.kind == "image" and shot.credit == visuals.AI_CREDIT and shot.meta["label"] == "IA · ILLUSTRATION"
    assert urls[0].startswith("https://image.pollinations.ai/prompt/secret%20room") and "seed=3" in urls[0]
    monkeypatch.setattr(cfg, "news_ai_images", "off")
    assert visuals.ai_image("secret room", tmp_path, set()) is None
    monkeypatch.setattr(cfg, "news_ai_images", "pollinations")
    # pas de photo libre : image IA avant les mots animés
    for name in ("pexels", "pixabay"):
        monkeypatch.setattr(visuals, name, lambda *a, **k: None)
    monkeypatch.setattr(visuals, "openverse", lambda *a, **k: None)
    monkeypatch.setattr(visuals, "commons", lambda *a, **k: None)
    assert visuals.find("dark corridor", "Un couloir.", tmp_path, set(), 1).ident.startswith("ai-")
    assert visuals.relevant("dark corridor", "Long corridor in a castle") and not visuals.relevant("corridor", "cat")
    sc = script.parse(json.dumps({"score": 7, "title": "t", "hook": "h", "sentences": [
        {"text": "Tout commence à San Francisco.", "visual": "city fog"},
        {"text": "En 2003, un technicien découvre une pièce fermée.", "visual": "locked door"},
        {"text": "Personne n'a le droit d'y entrer.", "visual": "security badge"},
        {"text": "Et toi, tu y crois ?", "visual": "question"}]}))
    cuts = [(0, 0, 3), (1, 3, 6), (2, 6, 9), (3, 9, 12)]
    shots, _ = generate.pick_shots(sc, cuts, tmp_path, kind="story")
    assert shots[1].kind == "counter" and shots[1].meta["year"] and shots[1].text == "2003"
    assert shots[2].ident.startswith("ai-") and shots[-1].kind == "quiz"
    gen = motion.counter("2003", "", 30, 0, (255, 0, 0), year=True)
    assert gen(0).shape == (motion.H, motion.W, 3)
    win = overlay.cta_windows([(0, 3), (3, 7), (7, 12), (12, 20)], 21.0)
    assert win[0][0] == 12 and win[-1] == (17.5, 3.5)
    frame = np.zeros((overlay.H, overlay.W, 3), np.uint8)
    overlay.draw_cta(frame, 2.0, 3.0)
    band = frame[overlay.CTA_Y - 30:overlay.CTA_Y + 30]
    assert ((band[..., 0] > 200) & (band[..., 1] < 80)).sum() > 200  # cœur rouge / bouton


def test_short_scripts_are_extended_to_min_duration(monkeypatch, tmp_path):
    from app.pipeline import llm
    cfg = get_settings()
    facts = ("GTA 6 bat un record. Rockstar annonce 25 millions de copies. Les serveurs ont saturé. "
             "Les joueurs attendent la version PC. Les critiques sont excellentes. Le jeu se passe à Vice City.")
    extra = [{"text": t, "visual": "game"} for t in (
        "Les files d'attente se sont formées devant les magasins dès minuit.",
        "Certains fans avaient posé des congés pour pouvoir jouer toute la journée.",
        "La carte de Vice City serait la plus grande jamais créée par le studio.",
        "Les critiques saluent surtout la mise en scène des missions principales.",
        "Sur les réseaux sociaux, les premières vidéos cumulent déjà des millions de vues.",
        "Plusieurs streamers ont joué en direct pendant toute la nuit sans s'arrêter.",
        "Le studio prépare déjà des mises à jour pour le mode en ligne.",
        "Les joueurs sur ordinateur devront encore patienter plusieurs mois.")]
    extra.append({"text": "Le jeu coûte 999 euros selon une rumeur.", "visual": "money"})  # chiffre inventé
    calls = []

    def fake_ask(prompt, timeout=600, max_tokens=None, num_ctx=4096):
        calls.append(prompt)
        return json.dumps({"sentences": extra[:4] if len(calls) == 1 else extra[4:]})

    monkeypatch.setattr(llm, "_ask_ollama", fake_ask)
    sc = script.parse(_llm_json())
    n0, words0 = len(sc.sentences), len(sc.text.split())
    added = script.extend(sc, "GTA 6", facts, "Millenium", need_words=50)
    assert added == 4 and len(sc.sentences) == n0 + 4 and sc.sentences[-1].endswith("?")  # question à la fin
    assert "trop court" in calls[0] and len(sc.titles) == len(sc.sentences) == len(sc.places)
    script.grow(sc, "GTA 6", facts, "Millenium", goal_words=words0 + 200, tries=3)
    assert not any("999" in x for x in sc.sentences) and len(sc.sentences) == n0 + 8
    # voix trop courte : rallongée d'après la durée lue, sinon sujet écarté
    monkeypatch.setattr(cfg, "news_target_seconds", 12.0)
    monkeypatch.setattr(cfg, "news_min_seconds", 25.0)
    monkeypatch.setattr(cfg, "news_publish", False)
    monkeypatch.setattr(feeds, "fetch_article", lambda item: facts)
    monkeypatch.setattr(script, "write", lambda *a, **k: script.parse(_llm_json()))
    monkeypatch.setattr(tts, "speak", lambda sents, out, speed=None, **k: _real_speak(sents, out, synth=_synth))
    grown = []

    def fake_grow(sc, title, facts, source, kind="news", goal_words=None, tries=3):
        grown.append(goal_words)
        sc.set_sentences(sc.per_sentence()[:-1] + [(f"Phrase de plus numéro {k} sur ce lancement.", "", "", "", "")
                                                   for k in "abcdefgh"] + sc.per_sentence()[-1:])

    monkeypatch.setattr(script, "grow", fake_grow)
    monkeypatch.setattr(montage, "build", lambda shots, spoken, *a, **k: spoken[-1].end + montage.TAIL)
    monkeypatch.setattr(generate, "pick_shots", lambda sc, cuts, work, kind="news": ([visuals.Shot(None, "kinetic")] * len(cuts), []))
    v = generate._new_video(_item(link="https://ex.fr/court"))
    assert generate.make(v.id, _item(link="https://ex.fr/court")) is not None and len(grown) == 1
    monkeypatch.setattr(cfg, "news_min_seconds", 500.0)
    v2 = generate._new_video(_item(link="https://ex.fr/trop-court"))
    assert generate.make(v2.id, _item(link="https://ex.fr/trop-court")) is None
    with db.session() as s:
        assert s.get(Video, v2.id).status == "skipped" and "Trop court" in s.get(Video, v2.id).error


def test_script_in_three_steps_and_edge_word_timings(monkeypatch, tmp_path):
    from app.pipeline import llm
    cfg = get_settings()
    monkeypatch.setattr(cfg, "story_target_seconds", 20.0)
    monkeypatch.setattr(llm, "provider", lambda: "ollama")
    monkeypatch.setattr(llm, "ensure_model", lambda name: True)
    facts = ("En 2003, Mark Klein, technicien chez AT&T à San Francisco, découvre la Room 641A. "
             "La pièce copie le trafic internet pour la NSA. Il révèle tout en 2006.")
    calls = []

    def fake_ask(prompt, timeout=600, max_tokens=None, num_ctx=4096, model="", json_mode=True, temperature=0.5):
        calls.append((prompt, model, json_mode))
        if "DANS L'ORDRE" in prompt:
            return json.dumps({"accroche": "Une pièce secrète copie internet", "qui": "Mark Klein, technicien",
                               "faits": ["En 2003, Mark Klein découvre la Room 641A à San Francisco.",
                                         "La pièce copie le trafic internet pour la NSA.",
                                         "En 2006, il révèle tout."], "inconnu": ""})
        if not json_mode:
            return ("1. Ouverture : Une porte fermée à clé cache un secret. Personne n'a le droit d'entrer. "
                    "En 2003, Mark Klein, un technicien d'AT&T, travaille à San Francisco. Il découvre la Room "
                    "641A. Cette pièce copie tout le trafic internet. Et c'est pour la NSA. En 2006, il décide de "
                    "tout révéler au grand jour. Mais la pièce existe-t-elle encore aujourd'hui ? "
                    "Et toi, tu penses qu'on est surveillés ?")
        return json.dumps({"score": 8, "title": "La pièce secrète qui copiait internet", "hook": "Une pièce "
                           "interdite 🤫", "topic": "Room 641A", "answers": ["Oui", "Non"], "hashtags": ["nsa"],
                           "sentences": [{"n": 1, "visual": "locked door", "title": "PORTE FERMÉE",
                                          "chapter": "LE SECRET"},
                                         {"n": 4, "visual": "server room", "title": "ROOM 641A",
                                          "chapter": "LA DÉCOUVERTE", "place": "San Francisco, USA"}]})

    monkeypatch.setattr(llm, "_ask_ollama", fake_ask)
    sc = script.write("Room 641A", facts, "Wikipédia", kind="story")
    assert sc is not None and sc.sentences[0] == "Une porte fermée à clé cache un secret."
    assert sc.sentences[-1].endswith("?") and sc.question == sc.sentences[-1]
    assert calls[1][1] == cfg.news_writer_model and calls[1][2] is False  # récit en texte libre, gros modèle
    assert calls[2][1] == "" and calls[2][2] is True  # mise en forme par le modèle rapide
    assert sc.visuals[0] == "locked door" and sc.places[3] == "San Francisco, USA" and sc.topic == "Room 641A"
    assert "STORY_NARRATION" not in calls[1][0] and "frissons" in calls[1][0]
    # mots calés sur les instants donnés par la voix Edge (silence de début retiré)
    w = tts.boundary_times("Il entre, enfin !", [(0.30, 0.2, "Il"), (0.55, 0.3, "entre"), (1.0, 0.4, "enfin")],
                           10.0, 11.5, lead=0.25)
    assert [x["word"] for x in w] == ["Il", "entre,", "enfin !"] and w[0]["start"] == 10.05 and w[1]["start"] == 10.3
    assert tts.boundary_times("Deux mots", [(0, 0.2, "Deux")], 0, 1) == tts.word_times("Deux mots", 0, 1)
    assert tts.edge_rate(1.12) == "+12%" and tts.edge_rate(1.0) == "+0%"
    monkeypatch.setattr(cfg, "news_tts", "edge")
    from app.news import tts as tts_mod
    monkeypatch.setattr(tts_mod, "edge_synth", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hors ligne")))
    monkeypatch.setattr(tts_mod, "_voice", lambda name: "piper")
    monkeypatch.setattr(tts_mod, "_synth", lambda voice, text, speed=None: _synth(text))
    spoken = tts.speak(["Bonjour à tous.", "Deuxième phrase."], tmp_path / "v.wav", kind="story", gap=0.3)
    assert len(spoken) == 2 and spoken[1].start - spoken[0].end == pytest.approx(0.3, abs=0.01)
