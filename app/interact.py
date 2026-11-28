"""Interactions : faire réagir sous chaque vidéo (constat du 27/11 : 0 commentaire sur 189 Shorts, 0,9 % de likes).

- `run` : quelques minutes après l'envoi, la chaîne publie en 1er commentaire la question du clip (un choix qu'on
  tranche en un mot). Un fil déjà lancé, et visible en haut des commentaires, fait répondre bien plus qu'une
  question cachée dans la description. Demande l'autorisation « gérer les commentaires » (reconnecter YouTube).
- `share_line` : ligne « 📤 Envoie-le à un pote… » dans les descriptions (les partages sont le signal le plus fort).
- Les essais « question à l'écran » et « appel à l'écran » (app/experiments.py) sont jugés sur le taux
  d'interaction (`learning.engagement`).
- `community_loop` (toutes les COMMUNITY_EVERY min) : la chaîne répond aux commentaires des spectateurs (IA locale,
  une phrase qui relance la discussion : un spectateur qui reçoit une réponse revient et s'abonne bien plus
  souvent) et range chaque vidéo dans une playlist par streamer (« Squeezie GTA 6 : les meilleurs moments »),
  qui se regarde à la suite et ressort dans la recherche. Même autorisation que le 1er commentaire."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import col, select

from . import db, growth
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .pipeline import engage

log = logging.getLogger(__name__)
TRIES = 3
PER_RUN = 3
NEWS_QUESTIONS = ["Tu en penses quoi ?", "Bonne ou mauvaise nouvelle selon toi ?", "Tu étais au courant ?"]
STORY_QUESTIONS = ["Tu connaissais cette histoire ?", "Tu as une explication ?", "Tu y crois ou pas ?"]

SHARE = {"clip": "📤 Envoie-le à un pote qui joue à GTA", "news": "📤 Partage pour que tes proches soient au courant",
         "story": "📤 Envoie cette histoire à quelqu'un qui adore les mystères",
         "compil": "📤 Envoie la vidéo à un pote fan de GTA 6"}


def share_line(kind: str = "clip") -> str:
    return SHARE.get(kind, "") if get_settings().share_line else ""


def comment_text(info: dict, title: str = "") -> str:
    """Texte du 1er commentaire : la question du clip, sinon une question de repli selon le type de vidéo."""
    kind = growth.kind_of(info)
    if kind == "compil":
        return "C'est lequel ton moment préféré ? Donne juste le numéro 👇"
    q = engage.clean_question(info.get("question", ""))
    if not q:
        pool = {"news": NEWS_QUESTIONS, "story": STORY_QUESTIONS}.get(kind)
        seed = sum(map(ord, title or "")) if title else 0
        q = pool[seed % len(pool)] if pool else engage.clean_question(
            engage.fallback_question(info.get("text", ""), title))
    return f"{q} 👇" if q else ""


def due_posts() -> list[tuple[Post, Clip, dict]]:
    """Vidéos YouTube publiées il y a plus de FIRST_COMMENT_DELAY min (et moins de 2 jours) sans 1er commentaire."""
    cfg = get_settings()
    since, until = now() - timedelta(days=2), now() - timedelta(minutes=cfg.first_comment_delay)
    with db.session() as s:
        rows = s.exec(select(Post, Clip).join(Clip, Clip.id == Post.clip_id).where(
            col(Post.platform).in_(["youtube", growth.LONG]), Post.status == "published", Post.remote_id != "",
            Post.published_at >= since, Post.published_at <= until)).all()
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([c.id for _, c in rows]))).all()}
    out = []
    for post, clip in sorted(rows, key=lambda r: r[0].published_at, reverse=True):
        try:
            info = json.loads(infos.get(clip.id) or "{}")
        except ValueError:
            info = {}
        if info.get("comment_id") or info.get("comment_tries", 0) >= TRIES:
            continue
        out.append((post, clip, info))
    return out


def _save(clip_id: int, info: dict) -> None:
    with db.session() as s:
        s.merge(ClipInfo(clip_id=clip_id, data=json.dumps(info, ensure_ascii=False)))
        s.commit()


def run() -> int:
    """Pose le 1er commentaire sous les vidéos récentes qui n'en ont pas. Renvoie le nombre de commentaires."""
    from .publish import youtube

    cfg = get_settings()
    if not cfg.first_comment or not youtube.configured() or youtube.auth_error() or not youtube.can_comment():
        return 0
    done = 0
    for post, clip, info in due_posts()[:PER_RUN]:
        text = comment_text(info, clip.title)
        if not text:
            _save(clip.id, {**info, "comment_tries": TRIES})
            continue
        try:
            cid = youtube.comment(post.remote_id, text)
        except youtube.AuthExpired:
            return done
        except Exception as e:
            tries = info.get("comment_tries", 0) + 1
            log.warning("1er commentaire impossible (%s, essai %d/%d) : %s", clip.title, tries, TRIES, e)
            _save(clip.id, {**info, "comment_tries": tries})
            continue
        _save(clip.id, {**info, "comment_id": cid, "comment": text})
        log.info("1er commentaire posté sous « %s » : %s", clip.title, text)
        done += 1
    return done


# ---------- Réponses aux commentaires des spectateurs ----------

REPLIED_KEY = "replied_comments"  # ids des commentaires auxquels la chaîne a répondu (les 2000 derniers)
REPLY_DAY_KEY = "replies_today"  # {"day": "AAAA-MM-JJ", "n": réponses du jour}
REPLY_MAX_AGE_H = 48
_LINK = re.compile(r"https?://|www\.|\.(com|fr|gg|ly|io)\b|@\w+\.\w+", re.I)
_SELF_PROMO = re.compile(r"\b(abonne|abonnez|sub|follow|cha[iî]ne|lien|clique|promo|code)\w*", re.I)

REPLY_PROMPT = """Tu gères une chaîne YouTube Shorts francophone de clips GTA 6 (moments forts de streamers).
Sous le Short « {title} », un spectateur ({author}) a écrit :
« {comment} »
{context}
Écris la réponse de la chaîne : UNE phrase courte (15 mots max), en français oral, sympa et complice,
tutoiement, 0 ou 1 emoji. Rebondis sur ce qu'il dit (accord, blague légère, avis sur la scène) et, si ça colle,
termine par une petite question pour continuer la discussion. Jamais de lien, jamais « abonne-toi », jamais
vulgaire, moqueur ou méchant, n'invente rien sur le streamer. Si le commentaire est du spam, une insulte, de la
haine, une arnaque, hors sujet ou incompréhensible : "skip": true.
Réponds UNIQUEMENT en JSON : {{"skip": <true|false>, "reply": "<réponse>"}}"""


def _load(key: str, default):
    from . import platforms

    try:
        return json.loads(platforms._get(key) or "null") or default
    except ValueError:
        return default


def _put(key: str, value) -> None:
    from . import platforms

    platforms._put(key, json.dumps(value, ensure_ascii=False))


def _age_h(published: str) -> float:
    try:
        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


def worth_reply(text: str) -> bool:
    """Commentaire auquel la chaîne peut répondre : ni lien, ni grossièreté, ni simple emoji ou « first »."""
    t = (text or "").strip()
    if not 2 <= len(t) <= 400 or _LINK.search(t) or engage.bad_title(t) == "vulgaire":
        return False
    letters = re.sub(r"[^a-zA-Zàâäçéèêëîïôöùûüœ]", "", t)
    return len(letters) >= 2 and t.lower() not in {"first", "premier", "1er", "1"}


def clean_reply(text: str) -> str:
    """Réponse de l'IA utilisable telle quelle, sinon "" (lien, auto-promo, grossièreté, anglais, trop longue)."""
    r = re.sub(r"\s+", " ", (text or "").strip().strip('"«» '))
    if not r or len(r) > 160 or _LINK.search(r) or _SELF_PROMO.search(r) or engage.bad_title(r) == "vulgaire" \
            or engage.looks_english(r):
        return ""
    return r


def write_reply(comment: str, title: str, author: str = "", context: str = "") -> str:
    """Réponse de la chaîne écrite par l'IA locale ("" si l'IA passe son tour ou n'est pas disponible)."""
    from .pipeline import llm

    if llm.provider() != "ollama":
        return ""
    prompt = REPLY_PROMPT.format(title=title or "clip GTA 6", author=author or "un spectateur",
                                 comment=comment[:400], context=context)
    try:
        m = re.search(r"\{.*\}", llm._ask_ollama(prompt, timeout=120, max_tokens=120, temperature=0.7) or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.warning("Réponse au commentaire impossible : %s", e)
        return ""
    if data.get("skip") in (True, "true", "oui", 1):
        return ""
    return clean_reply(str(data.get("reply", "")))


def _titles(video_ids: list[str]) -> dict[str, tuple[str, dict]]:
    """{id YouTube: (titre du clip, infos du clip)}"""
    with db.session() as s:
        rows = s.exec(select(Post, Clip).join(Clip, Clip.id == Post.clip_id).where(
            col(Post.remote_id).in_(video_ids))).all()
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([c.id for _, c in rows]))).all()}
    out = {}
    for p, c in rows:
        try:
            out[p.remote_id] = (c.title, json.loads(infos.get(c.id) or "{}"))
        except ValueError:
            out[p.remote_id] = (c.title, {})
    return out


def reply_candidates(threads: list[dict], channel_id: str, replied: set[str]) -> list[dict]:
    """Commentaires de spectateurs récents sans réponse de la chaîne : ceux en haut des vidéos, et les réponses
    données sous le 1er commentaire de la chaîne (réponse adressée avec @pseudo dans le même fil)."""
    out = []
    for th in threads:
        ours = th["author_id"] == channel_id
        if not ours:
            if th["id"] in replied or any(r["author_id"] == channel_id for r in th.get("replies", [])):
                continue
            out.append({**th, "parent": th["id"], "mention": ""})
            continue
        for r in th.get("replies", []):  # réponses à la question posée par la chaîne
            if r["author_id"] != channel_id and r["id"] not in replied:
                out.append({**r, "video_id": th["video_id"], "parent": th["id"], "mention": r["author"],
                            "question": th["text"]})
    return [c for c in out if _age_h(c["published"]) <= REPLY_MAX_AGE_H and worth_reply(c["text"])]


def reply_comments() -> int:
    """Répond à quelques commentaires récents (REPLY_PER_RUN par tour, REPLY_PER_DAY par jour)."""
    from .publish import youtube

    cfg = get_settings()
    if not cfg.comment_replies or not youtube.configured() or youtube.auth_error() or not youtube.can_comment():
        return 0
    cid = growth.channel_id()
    if not cid:
        return 0
    day = datetime.now(timezone.utc).date().isoformat()
    count = _load(REPLY_DAY_KEY, {})
    used = count.get("n", 0) if count.get("day") == day else 0
    if used >= cfg.reply_per_day:
        return 0
    replied_list = _load(REPLIED_KEY, [])
    replied = set(replied_list)
    try:
        threads = youtube.recent_comments(cid, 50)
    except youtube.AuthExpired:
        return 0
    todo = reply_candidates(threads, cid, replied)[:min(cfg.reply_per_run, cfg.reply_per_day - used)]
    if not todo:
        return 0
    titles = _titles(list({c["video_id"] for c in todo}))
    done = 0
    for c in todo:
        title, info = titles.get(c["video_id"], ("", {}))
        context = f"La chaîne avait demandé : « {c['question']} »" if c.get("question") else ""
        text = write_reply(c["text"], title or info.get("title", ""), c["author"], context)
        replied_list.append(c["id"])  # une seule tentative par commentaire, réponse ou pas
        if not text:
            continue
        if c["mention"]:
            text = f"@{c['mention'].lstrip('@')} {text}"
        try:
            youtube.reply(c["parent"], text)
        except youtube.AuthExpired:
            break
        except Exception as e:
            log.warning("Réponse au commentaire refusée par YouTube : %s", e)
            continue
        log.info("Réponse de la chaîne à « %s » : %s", c["text"][:60], text)
        done += 1
    _put(REPLIED_KEY, replied_list[-2000:])
    _put(REPLY_DAY_KEY, {"day": day, "n": used + done})
    return done


# ---------- Playlists par streamer ----------

PLAYLIST_KEY = "playlists"  # {titre: id} des playlists créées ou trouvées sur la chaîne
PLAYLIST_NEW_KEY = "playlists_new"  # {"day", "n"} playlists créées aujourd'hui, {"until"} pause après un refus
PLAYLIST_TRIES = 3  # échecs (hors limite de YouTube) avant d'abandonner une vidéo
PLAYLIST_NEW_PER_DAY = 2  # YouTube refuse vite les créations en rafale (« 429 Resource has been exhausted »)
PLAYLIST_MIN_VIDEOS = 3  # pas de playlist pour un streamer tant qu'il n'a pas 3 vidéos à y mettre
PLAYLIST_PAUSE_H = 12
PLAYLIST_TITLES = {"news": "Actus du jour en 1 minute", "story": "Histoires vraies incroyables",
                   "compil": "Compilations GTA 6 : les meilleurs moments de la semaine"}


def _rate_limited(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("429", "exhausted", "ratelimit", "rate limit", "quota", "toomanyrequests"))


def playlist_title(kind: str, streamer: str = "") -> str:
    if kind == "clip":
        name = streamer.strip()
        return f"{name} GTA 6 : les meilleurs moments" if name else "GTA 6 : les meilleurs moments"
    return PLAYLIST_TITLES.get(kind, "")


def playlist_description(title: str) -> str:
    return f"{title}. Les moments les plus fous, drôles et incroyables, en Shorts. Nouvelle vidéo chaque jour."


def playlist_due() -> list[tuple[Post, Clip, dict, str]]:
    """Vidéos YouTube publiées depuis moins de 7 jours, pas encore rangées dans leur playlist."""
    since = now() - timedelta(days=7)
    with db.session() as s:
        rows = s.exec(select(Post, Clip, Video).join(Clip, Clip.id == Post.clip_id)
                      .join(Video, Video.id == Clip.video_id).where(
            col(Post.platform).in_(["youtube", growth.LONG]), Post.status == "published", Post.remote_id != "",
            Post.published_at >= since)).all()
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([c.id for _, c, _ in rows]))).all()}
        names = {x.id: (x.name or "").strip() for x in s.exec(select(Source)).all()}
    out = []
    for post, clip, video in sorted(rows, key=lambda r: r[0].published_at):
        try:
            info = json.loads(infos.get(clip.id) or "{}")
        except ValueError:
            info = {}
        if info.get("playlist_id") or info.get("playlist_fails", 0) >= PLAYLIST_TRIES:
            continue
        streamer = info.get("streamer") or names.get(video.source_id, "")
        out.append((post, clip, info, playlist_title(growth.kind_of(info), streamer)))
    return [x for x in out if x[3]]


def _creation_allowed(state: dict, day: str) -> bool:
    if state.get("until", "") > now().isoformat():
        return False
    return (state.get("n", 0) if state.get("day") == day else 0) < PLAYLIST_NEW_PER_DAY


def add_to_playlists(limit: int = 5) -> int:
    """Range les vidéos récentes dans leur playlist. Les nouvelles playlists sont créées au compte-gouttes
    (PLAYLIST_NEW_PER_DAY, et seulement à partir de PLAYLIST_MIN_VIDEOS vidéos) ; un refus « trop de requêtes »
    de YouTube met en pause sans pénaliser les vidéos, qui seront rangées plus tard."""
    from .publish import youtube

    cfg = get_settings()
    if not cfg.playlists or not youtube.configured() or youtube.auth_error() or not youtube.can_comment():
        return 0
    due = playlist_due()
    if not due:
        return 0
    known = _load(PLAYLIST_KEY, {})
    state = _load(PLAYLIST_NEW_KEY, {})
    if state.get("hold", "") > now().isoformat():
        return 0  # YouTube a refusé un ajout : on laisse passer l'heure
    day = datetime.now(timezone.utc).date().isoformat()
    waiting: dict[str, int] = {}
    for *_, title in due:
        waiting[title] = waiting.get(title, 0) + 1
    refreshed, done = False, 0
    for post, clip, info, title in due:
        if done >= limit:
            break
        try:
            pid = known.get(title)
            if not pid and not refreshed:
                known.update(youtube.playlists())  # playlist déjà créée à la main ou avant une remise à zéro
                refreshed = True
                _put(PLAYLIST_KEY, known)
                pid = known.get(title)
            if not pid:
                if waiting[title] < PLAYLIST_MIN_VIDEOS or not _creation_allowed(state, day):
                    continue  # la vidéo attend : playlist créée plus tard
                pid = youtube.create_playlist(title, playlist_description(title))
                known[title] = pid
                _put(PLAYLIST_KEY, known)
                state = {"day": day, "n": (state.get("n", 0) if state.get("day") == day else 0) + 1}
                _put(PLAYLIST_NEW_KEY, state)
                log.info("Playlist créée : « %s »", title)
            youtube.add_to_playlist(pid, post.remote_id)
        except youtube.AuthExpired:
            break
        except Exception as e:
            if _rate_limited(e):
                adding = bool(known.get(title))  # refus sur l'ajout (sinon sur la création)
                hours = 1 if adding else PLAYLIST_PAUSE_H
                log.warning("Playlists : YouTube demande de ralentir (%s), pause de %d h", str(e)[:120], hours)
                state = {**state, ("hold" if adding else "until"): (now() + timedelta(hours=hours)).isoformat()}
                _put(PLAYLIST_NEW_KEY, state)
                if adding:
                    break  # tout attend
                continue
            log.warning("Playlist « %s » : ajout impossible (%s)", title, e)
            _save(clip.id, {**info, "playlist_fails": info.get("playlist_fails", 0) + 1})
            continue
        _save(clip.id, {**info, "playlist_id": pid, "playlist": title})
        log.info("« %s » rangé dans la playlist « %s »", clip.title, title)
        done += 1
    return done


def status() -> dict:
    """Pour la page Stats : réponses du jour et playlists de la chaîne."""
    cfg = get_settings()
    count = _load(REPLY_DAY_KEY, {})
    today = datetime.now(timezone.utc).date().isoformat()
    return {"replies_on": cfg.comment_replies, "replies_today": count.get("n", 0) if count.get("day") == today else 0,
            "reply_max": cfg.reply_per_day, "playlists": sorted(_load(PLAYLIST_KEY, {}))}


def community_loop(stop: threading.Event) -> None:
    stop.wait(180)  # laisse le reste démarrer
    while not stop.is_set():
        for job, what in ((add_to_playlists, "Playlists"), (reply_comments, "Réponses aux commentaires")):
            try:
                job()
            except Exception:
                log.exception("%s en erreur", what)
        stop.wait(max(60.0, get_settings().community_every * 60))
