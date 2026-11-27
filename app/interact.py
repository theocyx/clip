"""Interactions : faire réagir sous chaque vidéo (constat du 27/11 : 0 commentaire sur 189 Shorts, 0,9 % de likes).

- `run` : quelques minutes après l'envoi, la chaîne publie en 1er commentaire la question du clip (un choix qu'on
  tranche en un mot). Un fil déjà lancé, et visible en haut des commentaires, fait répondre bien plus qu'une
  question cachée dans la description. Demande l'autorisation « gérer les commentaires » (reconnecter YouTube).
- `share_line` : ligne « 📤 Envoie-le à un pote… » dans les descriptions (les partages sont le signal le plus fort).
- Les essais « question à l'écran » et « appel à l'écran » (app/experiments.py) sont jugés sur le taux
  d'interaction (`learning.engagement`)."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlmodel import col, select

from . import db, growth
from .config import get_settings
from .models import Clip, ClipInfo, Post, now
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
