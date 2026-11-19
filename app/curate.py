"""Tri de la file d'envoi : seuls les bons clips partent automatiquement, avec un titre qui leur est propre.

Constaté sur la chaîne : les Shorts publiés en rafale, en plusieurs parties, hors sujet (pas GTA) ou titrés
comme le live (« #GTAVI Live Stream 137 Heures ») font 0 à 1 vue, et entraînent les autres avec eux.
Les clips écartés restent dans « Clips » (statut annulé, raison indiquée) et peuvent être relancés à la main.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import timedelta

from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .pipeline import engage, llm

log = logging.getLogger(__name__)

FORCED = "Relancé à la main"  # erreur posée par le bouton « Publier » : le tri automatique ne le retire plus
_PART = re.compile(r"\s*\(partie \d+/\d+\)$", re.I)


def min_llm() -> float:
    """Note IA minimum : PUBLISH_MIN_LLM, relevée par l'apprentissage si les stats montrent mieux (0 = pas de minimum)."""
    from . import learning
    base = get_settings().publish_min_llm
    return max(base, float(learning.current().get("min_llm") or 0)) if base > 0 else 0.0


def score_bar() -> float | None:
    """Score à atteindre pour partir tout seul : celui des PUBLISH_TOP_PCT % meilleurs clips des derniers jours.
    None tant qu'il n'y a pas assez de clips pour comparer (ou seuil coupé)."""
    cfg = get_settings()
    if cfg.publish_top_pct <= 0:
        return None
    with db.session() as s:
        scores = s.exec(select(Clip.score).where(Clip.part <= 1, Clip.status != "failed",
                                                 Clip.created_at >= now() - timedelta(days=cfg.quality_days))).all()
    if len(scores) < max(1, cfg.quality_min_ref):
        return None
    ordered = sorted(scores, reverse=True)
    return float(ordered[max(0, math.ceil(len(ordered) * min(cfg.publish_top_pct, 100) / 100) - 1)])


def reason(clip: Clip, video: Video, info: dict, keywords: str = "", bar: float | None = None,
           floor: float | None = None) -> str:
    """Pourquoi ce clip ne doit pas partir automatiquement ("" = il peut partir)."""
    cfg = get_settings()
    if not cfg.allow_parts and clip.part_count > 1:
        return "Clip en plusieurs parties (la suite n'est presque jamais vue en Shorts)"
    floor = cfg.publish_min_llm if floor is None else floor
    note = info.get("llm")
    if floor > 0 and note is not None and float(note) < floor:
        return f"Note de l'IA trop basse ({float(note):.0f}/10, minimum {floor:g})"
    second = info.get("polish_score")
    if floor > 0 and second is not None and float(second) < floor:
        return f"Recalé à la relecture par l'IA ({float(second):.0f}/10, minimum {floor:g})"
    if bar is not None and clip.score < bar:
        return f"Pas dans les {cfg.publish_top_pct:g} % meilleurs clips de la semaine (seuls ceux-là partent)"
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


def polish(clip: Clip, info: dict, recent: list[str], guidance: str) -> dict | None:
    """Relecture par l'IA avant l'envoi : 2e note (avec les leçons des stats), titre et question revus.
    Renvoie les infos mises à jour, ou None si l'IA n'a pas répondu."""
    base = _PART.sub("", clip.title or "")
    if not info.get("text", "").strip():
        return {**info, "polished": True}  # rien à relire
    res = llm.polish(info.get("text", ""), base, info.get("hook", ""), guidance, recent)
    if not res:
        return None
    note, title, question = res
    out = {**info, "polished": True, "polish_score": note, "title_before": base}
    title = engage.tidy_title(title)
    if title and not engage.too_close(title, "", recent) and not engage.bad_title(title):
        out["polished_title"] = title
    q = engage.clean_question(question) if question else ""
    if q and not info.get("question"):  # la question déjà incrustée en fin de vidéo reste celle de la légende
        out["question"] = q
    return out


def run() -> dict:
    """Retire de la file les clips qui feraient baisser la chaîne et corrige les titres recopiés.
    Les clips qui passent le seuil sont d'abord relus par l'IA (2e note, meilleur titre).
    Renvoie {"cancelled": n, "retitled": n, "polished": n}."""
    out = {"cancelled": 0, "retitled": 0, "polished": 0}
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
    cfg = get_settings()
    bar, floor = score_bar(), min_llm()
    polish_left = cfg.polish_per_run if cfg.polish else 0
    guidance = ""
    if polish_left:
        from . import learning
        guidance = learning.guidance(learning.current())
    done_titles: dict[int, str] = {}
    with db.session() as s:
        for post, clip, video in sorted(rows, key=lambda r: -r[1].score):
            try:
                info = json.loads(infos[clip.id].data) if clip.id in infos else {}
            except ValueError:
                info = {}
            forced = post.error == FORCED
            kw = keywords.get(video.source_id, "")
            if not forced and clip.id not in done_titles and reason(clip, video, info, kw, bar, floor) == "" \
                    and polish_left > 0 and not info.get("polished"):
                polish_left -= 1
                better = polish(clip, info, recent, guidance)
                if better is None:
                    polish_left = 0  # IA indisponible : on n'insiste pas ce tour-ci
                else:
                    info = better
                    s.merge(ClipInfo(clip_id=clip.id, data=json.dumps(info, ensure_ascii=False)))
                    c = s.get(Clip, clip.id)
                    new_title = info.get("polished_title")
                    if new_title and new_title != _PART.sub("", c.title):
                        log.info("Titre amélioré : « %s » → « %s »", c.title, new_title)
                        c.title = (new_title + c.title[len(_PART.sub("", c.title)):])[:100]
                        s.add(c)
                    clip = c
                    out["polished"] += 1
            if not forced:
                why = reason(clip, video, info, kw, bar, floor)
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
