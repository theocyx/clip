"""Tri de la file d'envoi : seuls les bons clips partent automatiquement, avec un titre qui leur est propre.

Constaté sur la chaîne : les Shorts publiés en rafale, en plusieurs parties, hors sujet (pas GTA) ou titrés
comme le live (« #GTAVI Live Stream 137 Heures ») font 0 à 1 vue, et entraînent les autres avec eux.
Les clips écartés restent dans « Clips » (statut annulé, raison indiquée) et peuvent être relancés à la main.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .pipeline import engage

log = logging.getLogger(__name__)

FORCED = "Relancé à la main"  # erreur posée par le bouton « Publier » : le tri automatique ne le retire plus
_PART = re.compile(r"\s*\(partie \d+/\d+\)$", re.I)


def reason(clip: Clip, video: Video, info: dict, keywords: str = "") -> str:
    """Pourquoi ce clip ne doit pas partir automatiquement ("" = il peut partir)."""
    cfg = get_settings()
    if not cfg.allow_parts and clip.part_count > 1:
        return "Clip en plusieurs parties (la suite n'est presque jamais vue en Shorts)"
    llm = info.get("llm")
    if cfg.publish_min_llm > 0 and llm is not None and float(llm) < cfg.publish_min_llm:
        return f"Note de l'IA trop basse ({float(llm):.0f}/10, minimum {cfg.publish_min_llm:g})"
    if cfg.topic_only and not engage.topic(f"{video.title} {keywords}", f"{clip.title} {info.get('text', '')}"):
        return "Hors sujet : pas de GTA (la chaîne reste dans sa niche)"
    if cfg.queue_max_hours > 0 and now() - clip.created_at > timedelta(hours=cfg.queue_max_hours):
        return f"En attente depuis plus de {cfg.queue_max_hours:g} h : plus d'actualité"
    return ""


def published_titles(days: int = 3) -> list[str]:
    with db.session() as s:
        rows = s.exec(select(Clip.title).join(Post, Post.clip_id == Clip.id).where(
            col(Post.status).in_(["published", "manual"]), Post.published_at >= now() - timedelta(days=days))).all()
    return [_PART.sub("", t or "") for t in rows if t]


def retitle(clip: Clip, video: Video, info: dict, recent: list[str]) -> str | None:
    """Nouveau titre si celui du clip recopie le live ou un titre déjà publié, sinon None."""
    base = _PART.sub("", clip.title or "")
    fixed = engage.fix_title(base, video.title, recent, info.get("text", ""), info.get("hook", ""))
    if fixed and fixed != base:
        suffix = clip.title[len(base):]
        return (fixed + suffix)[:100]
    return None


def run() -> dict:
    """Retire de la file les clips qui feraient baisser la chaîne et corrige les titres recopiés.
    Renvoie {"cancelled": n, "retitled": n}."""
    out = {"cancelled": 0, "retitled": 0}
    with db.session() as s:
        rows = s.exec(select(Post, Clip, Video).join(Clip, Clip.id == Post.clip_id)
                      .join(Video, Video.id == Clip.video_id)
                      .where(Post.status == "pending", Clip.status == "ready")).all()
        if not rows:
            return out
        infos = {i.clip_id: i for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([c.id for _, c, _ in rows]))).all()}
        keywords = {x.id: x.keywords or "" for x in s.exec(select(Source)).all()}
    recent = published_titles()
    done_titles: dict[int, str] = {}
    with db.session() as s:
        for post, clip, video in sorted(rows, key=lambda r: -r[1].score):
            try:
                info = json.loads(infos[clip.id].data) if clip.id in infos else {}
            except ValueError:
                info = {}
            if post.error != FORCED:
                why = reason(clip, video, info, keywords.get(video.source_id, ""))
                if why:
                    p = s.get(Post, post.id)
                    p.status, p.error = "cancelled", f"Retiré : {why}"
                    s.add(p)
                    out["cancelled"] += 1
                    continue
            if clip.id in done_titles:
                continue
            new = retitle(clip, video, info, recent)
            c = s.get(Clip, clip.id)
            if new:
                log.info("Titre corrigé : « %s » → « %s »", c.title, new)
                c.title = new
                s.add(c)
                out["retitled"] += 1
            done_titles[clip.id] = c.title
            recent.insert(0, _PART.sub("", c.title))
        s.commit()
    if out["cancelled"] or out["retitled"]:
        log.info("File d'envoi triée : %d clip(s) retiré(s), %d titre(s) corrigé(s)", out["cancelled"],
                 out["retitled"])
    return out
