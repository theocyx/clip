"""Vidéos d'actu générées : flux RSS, script vérifié, voix, plans, illustrations libres, montage, publication."""
import json
import subprocess
from datetime import datetime, timedelta

import numpy as np
import pytest

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


def test_find_falls_back_to_card(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "news_wikimedia", False)
    shot = visuals.find("game controller", "Le jeu fait 25 millions de ventes", tmp_path, set(), 0)
    assert shot.kind == "image" and shot.path.exists() and shot.credit == ""
    from PIL import Image
    assert Image.open(shot.path).size == (1080, 1920)


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
    ass = (out.parent / "news.ass").read_text()
    assert "SOURCE : MILLENIUM" in ass and "RECORD" in ass
    (music / "m.mp3").unlink()


def _item(title="GTA 6 : 25 millions en 24 heures", link="https://ex.fr/gta"):
    return feeds.Item(title=title, link=link, summary="Rockstar annonce 25 millions de copies en 24 heures.",
                      source="Millenium", published=_now())


def test_make_creates_clip_and_queues(monkeypatch, tmp_path):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_target_seconds", 12.0)
    monkeypatch.setattr(feeds, "fetch_article", lambda item: item.summary)
    monkeypatch.setattr(script, "write", lambda *a: script.parse(_llm_json()))
    monkeypatch.setattr(tts, "speak", lambda sents, out: _real_speak(sents, out, synth=_synth))
    built = {}

    def fake_build(shots, spoken, voice, hook, source, out, work, on_progress=None):
        built.update(shots=shots, hook=hook, source=source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return spoken[-1].end + montage.TAIL

    monkeypatch.setattr(montage, "build", fake_build)
    monkeypatch.setattr(visuals, "find", lambda q, s, w, u, j, prefer_video=True:
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
    monkeypatch.setattr(script, "write", lambda *a: script.parse(_llm_json(score=3)))
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
