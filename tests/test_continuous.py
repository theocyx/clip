"""Amélioration continue (essais de versions du montage) et qualité de la voix / des sous-titres."""
import random
import wave

import numpy as np
import pytest

from app import db, experiments, learning
from app.config import get_settings
from app.models import Clip, Video
from app.news import montage, motion, overlay, script, tts
from app.pipeline import subtitles


def _synth(text, sr=16000):
    n = int(sr * len(text.split()) * 0.25)
    audio = np.concatenate([np.zeros(800), 0.4 * np.sin(np.arange(n) / 5), np.zeros(800)]).astype(np.float32)
    return audio, sr


# ---------- voix ----------

def test_prosody_like_a_presenter():
    sents = ["Un incendie géant ravage le sud de la France ce matin.", "Mais personne ne s'y attendait.",
             "Les pompiers sont mobilisés depuis plusieurs heures sur place.", "Et toi, tu en penses quoi ?"]
    news = tts.prosody(sents, "news", ["LES FAITS", "LES FAITS", "BILAN", "ET TOI ?"])
    assert news[0]["rate"] > 1 and news[0]["pitch"] > 0  # accroche énergique
    assert news[0]["pause"] > 1.4  # respiration avant « Mais… »
    assert news[3]["pitch"] > 0  # la question monte
    assert news[2]["pause"] >= 1.6  # blanc avant la question au spectateur (et changement de partie)
    story = tts.prosody(sents, "story")
    assert story[0]["rate"] < 1 and story[0]["pitch"] < 0  # histoire : ouverture grave et posée
    assert story[1]["rate"] < 1  # phrase courte appuyée
    plain = [{"rate": round(1 + (0.02 if i % 2 else -0.01), 3)} for i in range(2)]
    flat = tts.prosody(["Une phrase normale avec assez de mots ici.", "Une autre phrase normale avec des mots."])
    assert [x["rate"] for x in flat][1:] == [plain[1]["rate"]]  # petites variations : pas de ton robotique
    assert all(x["pause"] <= 3 for x in news + story)
    assert tts.add_hz("-2Hz", 3) == "+1Hz" and tts.add_hz("", -1) == "-1Hz"


def test_speak_passes_tone_and_varies_pauses(tmp_path):
    calls = []

    def synth(text, rate=1.0, pitch=0):
        calls.append((rate, pitch))
        return _synth(text)

    synth.expressive = True
    synth.engine = "fr-FR-HenriNeural"
    sents = ["Phrase d'accroche assez longue pour la vidéo.", "Une phrase du milieu avec des mots.",
             "Et toi, tu en penses quoi ?"]
    info = {}
    spoken = tts.speak(sents, tmp_path / "v.wav", synth=synth, gap=0.2, info=info)
    assert len(calls) == 3 and calls[0][0] > 1 and calls[2][1] > 0
    gaps = [b.start - a.end for a, b in zip(spoken, spoken[1:])]
    assert gaps[0] > 0.2 * 1.3 and gaps[1] >= 0.2 * 1.6 - 0.01
    assert info["engine"] == "fr-FR-HenriNeural"
    with wave.open(str(tmp_path / "v.wav")) as wf:
        assert wf.getnframes() > 0


def test_edge_text_reads_abbreviations_and_keeps_word_timing(monkeypatch):
    said, counts = tts.edge_text("M. Dupont a gagné 10 M€ & GTA VI à 130 km/h.")
    assert said == "Monsieur Dupont a gagné 10 millions d'euros et GTA 6 à 130 kilomètres heure."
    assert counts == [1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 2]
    assert tts.edge_text("Il a vu 3 M. de vues")[0] == "Il a vu 3 M. de vues"  # pas suivi d'un nom : inchangé
    # mots lus (« millions d'euros ») regroupés sous le mot affiché (« M€ »)
    bounds = [(0.0, 0.2, "Gain"), (0.3, 0.2, "de"), (0.6, 0.1, "10"), (0.8, 0.3, "millions"), (1.2, 0.3, "d'euros")]
    w = tts.boundary_times("Gain de 10 M€.", bounds, 5.0, 7.0, counts=[1, 1, 1, 2])
    assert [x["word"] for x in w] == ["Gain", "de", "10", "M€."]
    assert w[3]["start"] == 5.8 and w[3]["end"] == 6.5
    # voix Edge : texte lu en toutes lettres, intonation transmise, ancienne interface conservée
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_tts", "edge")
    seen = {}

    def fake_edge(text, voice, speed=1.0, pitch="+0Hz"):
        seen.update(text=text, voice=voice, speed=speed, pitch=pitch)
        a, sr = _synth(text)
        return a, sr, [(0.05 + 0.2 * i, 0.15, t) for i, t in enumerate(text.split())]

    monkeypatch.setattr(tts, "edge_synth", fake_edge)
    synth = tts.default_synth("story", 1.0, voice="fr-FR-HenriNeural")
    res = synth("Mme Durand arrive.", rate=0.9, pitch=-1)
    assert seen == {"text": "Madame Durand arrive.", "voice": "fr-FR-HenriNeural", "speed": 0.9,
                    "pitch": tts.add_hz(cfg.story_edge_pitch, -1)}
    assert res[3] == [1, 1, 1] and synth.engine == "fr-FR-HenriNeural"


def test_voice_studio_chain_in_final_mix(tmp_path, monkeypatch):
    cfg = get_settings()
    args = (tmp_path / "v.mp4", tmp_path / "voice.wav", "", "", tmp_path / "out.mp4", 10.0)
    cmd = " ".join(map(str, montage.final_cmd(*args, codec="libx264")))
    assert "deesser" in cmd and "acompressor" in cmd and "highpass=f=75" in cmd
    monkeypatch.setattr(cfg, "news_voice_fx", False)
    assert "deesser" not in " ".join(map(str, montage.final_cmd(*args, codec="libx264")))


def test_impact_hits_in_sfx_track(tmp_path):
    out = motion.whoosh_track([2.0], 3.0, tmp_path / "w.wav", sr=8000, hits=[0.05])
    with wave.open(str(out)) as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), np.int16)
    assert np.abs(pcm[:2000]).max() > 3000  # « boum » d'ouverture audible
    boom = motion.hit(8000)
    assert boom.dtype == np.float32 and abs(float(np.abs(boom).max()) - 1) < 1e-5


# ---------- sous-titres ----------

def _words(text, step=0.3):
    return [{"start": i * step, "end": i * step + 0.25, "word": w} for i, w in enumerate(text.split())]


def test_subtitle_groups_never_end_on_small_word():
    groups = subtitles.group_words(_words("il regarde vraiment le monstre arriver vite"))
    assert all(not subtitles.is_glue(g[-1]["word"]) for g in groups[:-1])
    assert any([w["word"] for w in g][:2] == ["le", "monstre"] for g in groups)
    news = overlay.sub_groups(_words("la police a retrouvé les deux suspects dans la forêt ce matin"), max_words=4)
    assert all(not subtitles.is_glue(g[-1]["word"]) for g in news[:-1])


def test_subtitles_pop_in():
    seg = [{"start": 0, "end": 2, "text": "", "words": _words("salut tout le monde ici")}]
    ass = subtitles.build_ass(seg, 0, 2, "Montserrat", pop=True)
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0")]
    assert "\\t(0,110," in lines[0] and "\\fscx80" in lines[0]  # 1re image du groupe : rebond
    assert "\\t(" not in lines[1]  # mots suivants du même groupe : sans rebond
    assert all("\\t(" not in ln for ln in subtitles.build_ass(seg, 0, 2, "Montserrat").splitlines()
               if ln.startswith("Dialogue: 0"))


def test_news_subtitle_pop_in_scales_up():
    frame = np.zeros((overlay.H, overlay.W, 3), np.uint8)
    groups = [_words("bonjour à tous")]
    overlay.draw_subs(frame, groups, 0.0)
    early = int((frame.sum(axis=2) > 0).sum())
    frame2 = np.zeros_like(frame)
    overlay.draw_subs(frame2, groups, 0.25)
    assert 0 < early < int((frame2.sum(axis=2) > 0).sum())


# ---------- essais de versions ----------

def _seed(kind_info: dict, perfs: list[tuple[dict, float]], key: str) -> dict[int, float]:
    out = {}
    with db.session() as s:
        v = Video(key=key, url="u", title="t", status="done")
        s.add(v)
        s.commit()
        for i, (exp, p) in enumerate(perfs):
            c = Clip(video_id=v.id, group=f"{key}{i}", start=0, end=30, title=f"vidéo {i}", path="")
            s.add(c)
            s.commit()
            learning.save_clip_info(c.id, {**kind_info, "exp": exp, "title": f"Titre {i}", "hook": f"Accroche {i}",
                                           "text": f"Première phrase {i}. Suite."})
            out[c.id] = p
    return out


def test_choose_defaults_and_one_trial_at_a_time(monkeypatch):
    cfg = get_settings()
    specs = experiments.factors("clip")
    monkeypatch.setattr(cfg, "experiments", False)
    assert experiments.choose("clip", perf={}) == {f: s["default"] for f, s in specs.items()}
    monkeypatch.setattr(cfg, "experiments", True)
    monkeypatch.setattr(cfg, "experiment_rate", 1.0)
    for seed in range(10):
        exp = experiments.choose("clip", rng=random.Random(seed), perf={})
        assert sum(exp[f] != s["default"] for f, s in specs.items()) == 1  # un seul réglage essayé
    monkeypatch.setattr(cfg, "experiment_rate", 0.0)
    assert experiments.choose("clip", perf={}) == {f: s["default"] for f, s in specs.items()}
    monkeypatch.setattr(cfg, "news_tts", "edge")
    news = experiments.factors("news")
    assert news["voice"]["default"] == cfg.news_edge_voice and len(news["voice"]["variants"]) >= 2
    assert experiments.label("fr-FR-RemyMultilingualNeural") == "Remy" and experiments.label("posee") == "posée"
    assert experiments.pace({"pace": "rapide"}) > 1 > experiments.pace({"pace": "posee"})


def test_better_version_is_adopted(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_tts", "edge")
    monkeypatch.setattr(cfg, "experiment_min", 3)
    monkeypatch.setattr(cfg, "experiment_rate", 0.0)
    default = cfg.news_edge_voice
    other = experiments.factors("news")["voice"]["variants"][1]
    rows = [({"voice": default, "pace": "normale"}, 0.3)] * 4 + [({"voice": other, "pace": "normale"}, 0.9)] * 3
    perf = _seed({"kind": "news"}, rows, "exp:news")
    table = experiments.results("news", perf)
    assert len(table["voice"][default]) == 4 and len(table["voice"][other]) == 3
    assert experiments.choose("news", perf=perf)["voice"] == other
    assert any("fait mieux" in x for x in experiments.insights(perf))
    summary = [r for r in experiments.summary(perf) if r["kind"] == "Actus" and r["factor"] == "Voix"]
    assert any(r["used"] and not r["default"] and r["n"] == 3 for r in summary)
    # trop peu de vidéos pour la version essayée : on garde la version par défaut
    monkeypatch.setattr(cfg, "experiment_min", 4)
    assert experiments.choose("news", perf=perf)["voice"] == default
    # les histoires et les clips ne se mélangent pas aux actus
    assert experiments.results("story", perf) == {} and experiments.results("clip", perf) == {}


def test_trial_picks_least_tested_version(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "news_tts", "piper")  # une seule voix : seul le débit varie
    monkeypatch.setattr(cfg, "experiment_rate", 1.0)
    perf = _seed({"kind": "news", "story": True}, [({"pace": "rapide"}, 0.5)] * 3, "exp:story")
    picks = {experiments.choose("story", rng=random.Random(s), perf=perf)["pace"] for s in range(8)}
    assert picks == {"posee"}


def test_examples_guide_the_writer():
    rows = [({}, p) for p in (0.9, 0.1, 0.5, 0.7, 0.2)]
    perf = _seed({"kind": "news"}, rows, "exp:examples")
    ex = experiments.examples("news", perf)
    assert ex["top"][0]["title"] == "Titre 0" and ex["flop"][0]["title"] == "Titre 1"
    assert ex["top"][0]["opening"] == "Première phrase 0"
    text = script.guide(ex, "opening")
    assert "« Première phrase 0 »" in text and "moins marché" in text
    assert "accroche « Accroche 0 »" in script.guide(ex, "title")
    assert script.guide({"top": [], "flop": []}, "title") == ""
    assert experiments.examples("story", perf) == {"top": [], "flop": []}


@pytest.mark.parametrize("kind", ["clip", "news", "story"])
def test_factors_have_default_in_variants(kind):
    for spec in experiments.factors(kind).values():
        assert spec["default"] in spec["variants"]


def test_citation_titles_ab_tested(monkeypatch):
    from app import curate
    from app.pipeline import engage, process

    cfg = get_settings()
    monkeypatch.setattr(cfg, "experiment_rate", 0.25)
    monkeypatch.setattr(cfg, "title_test_share", 1.0)
    exp = experiments.choose("clip", rng=random.Random(1), perf={})
    assert exp["title"] == "citation"  # pas encore mesuré : la moitié des clips (ici tous) essaient la citation
    monkeypatch.setattr(cfg, "experiment_rate", 0.0)
    assert experiments.choose("clip", rng=random.Random(1), perf={})["title"] == "action"  # par défaut

    text = "Bon on y va. Il m'a écrasé avec le camion ! Ok d'accord."
    assert engage.pick_quote("Il m'a écrasé avec le camion", text) == "Il m'a écrasé avec le camion"
    assert engage.pick_quote("Phrase jamais dite par personne ici", text) == "Il m'a écrasé avec le camion !"

    class M:
        title, hook = "Domingo se fait écraser GTA 6", "Il se fait écraser par un camion 😭"
        features = {"quote": "Il m'a écrasé avec le camion", "streamer": "Domingo", "text": text}

    m = M()
    video = type("V", (), {"title": "GTA RP live 12h", "source_id": None})()
    assert process.citation_title(video, m, {"title": "action"}) == "action" and m.title.startswith("Domingo se")
    assert process.citation_title(video, m, {"title": "citation"}) == "citation"
    assert m.title == "Domingo : « Il m'a écrasé avec le camion » 😭"
    assert m.features["title_action"] == "Domingo se fait écraser GTA 6"
    m2 = M()
    m2.features = {"quote": "", "text": "euh"}
    assert process.citation_title(video, m2, {"title": "citation"}) == "action"  # rien de citable

    # la relecture IA garde le titre citation (sinon l'essai ne mesurerait rien)
    monkeypatch.setattr(curate.llm, "polish", lambda *a, **k: (8, "Domingo se fait rouler dessus GTA 6", "Q ?"))
    clip = type("C", (), {"title": m.title})()
    out = curate.polish(clip, {"text": text, "exp": {"title": "citation"}}, [], "")
    assert "polished_title" not in out
    assert curate.polish(clip, {"text": text, "exp": {"title": "action"}}, [], "")["polished_title"]
