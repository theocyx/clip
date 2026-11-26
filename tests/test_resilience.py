"""Pannes vues sur le serveur le 25/11 : jeton YouTube expiré, base verrouillée qui tuait l'enregistrement des
lives, titres en anglais, images IA refusées (402), VOD réservées aux abonnés, actus de 55-59 s écartées."""
import json
import sqlite3
import threading

from google.auth.exceptions import RefreshError
from sqlmodel import delete

from app import curate, db, notify, platforms, worker
from app.config import get_settings
from app.models import Clip, ClipInfo, Post, PostStat, Tuning, Video
from app.news import script, visuals
from app.pipeline import engage, llm, sources
from app.publish import creds, youtube

EXPIRED = "('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant'})"


def _cleanup():
    with db.session() as s:
        for m in (PostStat, Post, ClipInfo, Clip, Tuning):
            s.exec(delete(m))
        s.commit()


def test_youtube_expired_token_pauses_publishing(monkeypatch):
    _cleanup()
    with db.session() as s:
        v = Video(key="res:yt", url="u", title="t", status="done")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group="ryt", start=0, end=30, score=0.9, title="GTA 6 : il vole un avion",
                 path=__file__)
        s.add(c)
        s.commit()
        p = Post(clip_id=c.id, platform="youtube")
        old = Post(clip_id=c.id, platform="youtube", status="failed", attempts=3, error=EXPIRED)
        s.add_all([p, old])
        s.commit()
        pid, old_id = p.id, old.id
    sent, calls = [], []
    monkeypatch.setattr(youtube, "configured", lambda: True)
    monkeypatch.setattr(notify, "send", lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(worker.get_settings(), "min_minutes_between_posts", 0)
    monkeypatch.setattr(worker.get_settings(), "publish_hours", "")

    def refused(*a, **k):
        calls.append(1)
        raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})

    monkeypatch.setattr(youtube, "_upload", refused)
    platforms.set_enabled("youtube", True)
    creds.save("youtube", {"token": "x", "refresh_token": "y"}, account="Chaîne")
    try:
        assert worker.connected("youtube")
        assert not worker.publish_once("youtube")
        with db.session() as s:
            post = s.get(Post, pid)
            assert post.status == "pending" and post.attempts == 0  # rien de perdu, pas compté comme un échec
        assert youtube.auth_error() and not worker.connected("youtube")
        assert len(sent) == 1 and "Reconnecte" in sent[0]
        # plus aucun essai toutes les minutes tant que la chaîne n'est pas reconnectée
        assert not worker.publish_once("youtube") and len(calls) == 1
        # les statistiques ne retentent pas non plus
        try:
            youtube.stats(["abc"])
            raise AssertionError("AuthExpired attendu")
        except youtube.AuthExpired:
            pass
        # reconnexion : l'erreur est effacée et les envois abandonnés pour ce motif repartent
        youtube._set_auth_error(None)
        assert worker.requeue_auth_failures() == 1
        with db.session() as s:
            assert s.get(Post, old_id).status == "pending" and s.get(Post, old_id).attempts == 0
        assert worker.connected("youtube")
    finally:
        youtube._set_auth_error(None)
        creds.delete("youtube")
        _cleanup()


def test_web_shows_expired_youtube(monkeypatch):
    from app.web import main
    monkeypatch.setattr(youtube, "configured", lambda: True)
    creds.save("youtube", {"token": "x", "refresh_token": "y"}, account="Chaîne")
    youtube._set_auth_error(EXPIRED)
    try:
        st = main.accounts_status()["youtube"]
        assert st["connected"] and st["expired"]
    finally:
        youtube._set_auth_error(None)
        creds.delete("youtube")


def test_curate_does_not_lock_db_during_ai_calls(monkeypatch):
    """La relecture par l'IA prend ~30 s par clip : la base ne doit pas rester verrouillée pendant ce temps
    (sinon « database is locked » ailleurs, ce qui avait arrêté l'enregistrement des lives)."""
    _cleanup()
    cfg = get_settings()
    for k, v in {"publish_top_pct": 0, "publish_min_llm": 0, "polish": True, "polish_per_run": 4}.items():
        monkeypatch.setattr(cfg, k, v)
    with db.session() as s:
        v = Video(key="res:lock", url="u", title="GTA 6 RP", status="done")
        s.add(v)
        s.commit()
        for i in range(3):
            c = Clip(video_id=v.id, group=f"rl{i}", start=0, end=30, score=0.9 - i / 10, title=f"GTA 6 : moment {i}",
                     path=__file__)
            s.add(c)
            s.commit()
            s.add(ClipInfo(clip_id=c.id, data=json.dumps({"llm": 8, "text": f"il se passe un truc fou numéro {i}"})))
            s.add(Post(clip_id=c.id, platform="youtube"))
            s.commit()
    locked = []

    def fake_polish(text, title, hook="", guidance="", recent=None):
        con = sqlite3.connect(str(cfg.db_path), timeout=0.2)
        try:
            con.execute("BEGIN IMMEDIATE")  # une écriture d'un autre fil passe-t-elle ?
            con.rollback()
        except sqlite3.OperationalError:
            locked.append(title)
        finally:
            con.close()
        return 8, f"GTA 6 : il refait {title[-1]} le casse du siècle", ""

    monkeypatch.setattr(llm, "polish", fake_polish)
    assert curate.run()["polished"] == 3
    assert locked == []
    _cleanup()


def test_supervised_task_restarts_after_crash(monkeypatch):
    monkeypatch.setattr(worker, "stop", threading.Event())
    runs = []

    def flaky():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("database is locked")

    worker.supervised(flaky, retry_s=0)()
    assert len(runs) == 2  # relancée une fois, puis fin normale (pas de boucle infinie)


def test_record_loop_survives_errors(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(worker, "stop", stop)
    seen = []

    def boom():
        seen.append(1)
        if len(seen) >= 2:
            stop.set()
        raise RuntimeError("database is locked")

    monkeypatch.setattr(worker, "record_once", boom)
    monkeypatch.setattr(stop, "wait", lambda t=None: stop.is_set())
    worker.record_loop()  # ne lève rien
    assert len(seen) == 2


def test_subscriber_only_vod_is_skipped_quietly(monkeypatch):
    from app.pipeline import process
    msg = ("Téléchargement échoué : ERROR: [twitch:vod] 2938524518: You must be logged into an account that has "
           "access to this subscriber-only content.")
    assert sources.subscriber_only(msg) and not sources.subscriber_only("HTTP Error 404")
    with db.session() as s:
        v = Video(key="res:sub", url="https://www.twitch.tv/videos/1", title="VOD abonnés", status="new")
        s.add(v)
        s.commit()
        vid = v.id
    stop = threading.Event()
    monkeypatch.setattr(worker, "stop", stop)
    monkeypatch.setattr(process.stopping, "is_set", lambda: False)
    queue = [vid]

    def nxt():
        if queue:
            with db.session() as s:
                return s.get(Video, queue.pop())
        stop.set()
        return None

    monkeypatch.setattr(worker, "next_download", nxt)
    monkeypatch.setattr(sources, "download", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(msg)))
    errors = []
    monkeypatch.setattr(notify, "error", lambda *a, **k: errors.append(a))
    worker.download_loop()
    with db.session() as s:
        v = s.get(Video, vid)
        assert v.status == "skipped" and "abonnés" in v.error
    assert errors == []


def test_looks_english():
    for t in ("The Unexplained Beast of Gévaudan", "The Enigmatic Prisoner", "Nvidia's Comeback: Under Trump Pressure",
              "Jensen Huang Back as Nvidia CEO 🌟"):
        assert engage.looks_english(t), t
    for t in ("Nvidia: Huang réintégré après démission", "Rennes-le-Château : L'énigme du Trésor",
              "Meghan Markle : Père Formellement Inculpé", "Livret A - La pire année depuis 2008",
              "GTA 6 : Squeezie fait face à une pression", "Breaking Bad dans GTA 6 ?", "Tu as vu ça ?"):
        assert not engage.looks_english(t), t


def test_english_title_rewritten_in_french(monkeypatch):
    facts = "En 1669, un prisonnier masqué est enfermé à Pignerol, puis à la Bastille où il meurt en 1703."
    sc = script.Script(title="The Enigmatic Prisoner", hook="Un Masked Man? 🎭",
                       sentences=["En 1669, un prisonnier masqué arrive à Pignerol.", "Personne ne voit son visage.",
                                  "Il meurt à la Bastille en 1703."],
                       visuals=["prison cell", "iron mask", "bastille"], answers=["Un frère du roi", "The king's brother"])
    sc.titles = ["PRISONNIER MASQUÉ", "The Mask", ""]
    monkeypatch.setattr(llm, "_ask_ollama", lambda *a, **k: "Titre : Le prisonnier au masque de fer, l'énigme royale")
    script.sanitize(sc, facts, "Homme au masque de fer")
    assert sc.title == "Le prisonnier au masque de fer, l'énigme royale"
    assert sc.hook == "" and sc.titles == ["PRISONNIER MASQUÉ", "", ""] and sc.answers == ["Un frère du roi"]

    # IA muette : titre du sujet (en français) à la place
    sc = script.Script(title="The Unexplained Beast of Gévaudan", hook=":eyes: #Gévaudan", sentences=sc.sentences,
                       visuals=sc.visuals)
    monkeypatch.setattr(llm, "_ask_ollama", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timed out")))
    script.sanitize(sc, facts, "Bête du Gévaudan")
    assert sc.title == "Bête du Gévaudan" and sc.hook == ""


def test_pollinations_spaced_and_retried(monkeypatch, tmp_path):
    answers = [None, tmp_path / "ok.jpg"]
    asked, slept = [], []

    def fake_download(url, dest, headers=None, max_mb=60):
        asked.append(url)
        return answers.pop(0)

    monkeypatch.setattr(visuals, "_download", fake_download)
    monkeypatch.setattr(visuals, "_poll_last", [visuals.time.monotonic()])
    assert visuals._pollinations("https://image.pollinations.ai/prompt/x", tmp_path / "x.jpg", gap=16,
                                 sleep=slept.append) == tmp_path / "ok.jpg"
    assert len(asked) == 2 and len(slept) == 2
    assert 15 < slept[0] <= 16 and slept[1] > 16  # une image toutes les ~16 s, plus d'attente après un refus


def test_story_from_english_article_keeps_french_title_and_sentences(monkeypatch):
    from app.news import stories
    pages = {"fr": None, "en": {"title": "Beast of Gévaudan", "url": "https://en.wikipedia.org/wiki/Beast",
                                "text": "long " * 900, "lat": None, "lon": None, "lang": "en"}}
    monkeypatch.setattr(stories, "wiki", lambda topic, lang="fr": pages[lang])
    assert stories.fetch("Bête du Gévaudan").title == "Bête du Gévaudan"
    # une phrase lue sortie en anglais est retirée (la voix française la lirait mal)
    facts = "En 1764, une bête attaque des villageois dans le Gévaudan."
    sc = script.Script(title="La bête du Gévaudan, le mystère jamais résolu", hook="",
                       sentences=["En 1764, une bête attaque des villageois.", "The beast was never found.",
                                  "Personne ne sait ce que c'était.", "Et toi, tu y crois ?"],
                       visuals=["forest", "wolf", "village", "night"], topic="THE BEAST")
    sc.chapters = ["LES FAITS", "THE HUNT", "LE MYSTÈRE", "ET TOI ?"]
    script.sanitize(sc, facts, "Bête du Gévaudan")
    assert len(sc.sentences) == 3 and "beast" not in sc.text.lower()
    assert sc.chapters == ["LES FAITS", "LE MYSTÈRE", "ET TOI ?"] and sc.topic == ""


def test_only_publishable_moments_are_rendered(monkeypatch):
    """Monter des clips qui ne partiront jamais coûte du temps et de la chaleur : seuil de montage relevé
    à 1 point sous le seuil de publication (appris ou réglé)."""
    from app.pipeline import process
    cfg = get_settings()
    monkeypatch.setattr(cfg, "clip_min_llm", 6)
    monkeypatch.setattr(cfg, "publish_min_llm", 7)
    assert process.render_floor({"min_llm": 8}) == 7  # seuil appris 8 : notes 7+ montées
    assert process.render_floor({}) == 6
    monkeypatch.setattr(cfg, "clip_min_llm", 0)
    assert process.render_floor({"min_llm": 8}) == 0  # coupé


def test_logs_kept_in_data_folder(tmp_path, monkeypatch):
    import logging

    from app import logs
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logs.setup("worker")
        logs.setup("worker")  # une seule fois
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        logging.getLogger("app.test").warning("trace gardée")
        added[0].flush()
        assert "trace gardée" in (tmp_path / "logs" / "worker.log").read_text(encoding="utf-8")
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_not_live_is_not_an_error(caplog):
    import logging
    lg = sources._YdlLog()
    with caplog.at_level(logging.DEBUG, logger="app.pipeline.sources"):
        lg.error("ERROR: [youtube] UC123: The channel is not currently live")
        lg.error("ERROR: [twitch:vod] 1: Unable to download JSON metadata")
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["yt-dlp : ERROR: [youtube] UC123: The channel is not currently live"] == logging.DEBUG
    assert levels["yt-dlp : ERROR: [twitch:vod] 1: Unable to download JSON metadata"] == logging.WARNING
