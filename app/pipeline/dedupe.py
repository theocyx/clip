"""Anti-doublons : plusieurs streamers sur le même serveur RP vivent la même scène.

Leurs micros captent les mêmes répliques (chat de proximité du jeu) : on compare les phrases des
clips (groupes de 3 mots) avec ceux des autres vidéos des dernières heures. Pour deux lives, on
regarde aussi si les passages ont eu lieu au même moment.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import select

from .. import db
from ..config import get_settings
from ..models import Clip, ClipInfo, Post, now

TEXT_CHARS = 3000  # texte gardé par clip pour la comparaison
TIME_SLACK = 300  # s : décalage toléré entre deux lives (délai du stream, début d'enregistrement)


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", t)


def shingles(text: str, n: int = 3) -> set[str]:
    w = words(text)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def similarity(a: str, b: str) -> float:
    """Part des groupes de 3 mots du plus court des deux textes qu'on retrouve dans l'autre (0..1)."""
    sa, sb = shingles(a), shingles(b)
    if len(sa) < 5 or len(sb) < 5:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def overlap(a0: float | None, a1: float | None, b0: float | None, b1: float | None) -> bool | None:
    """Vrai/faux si les deux passages (heures réelles, en secondes) se chevauchent, None si inconnu."""
    if None in (a0, a1, b0, b1):
        return None
    return a0 < b1 + TIME_SLACK and b0 < a1 + TIME_SLACK


def is_duplicate(sim: float, same_time: bool | None, threshold: float) -> bool:
    if same_time is True:
        return sim >= threshold / 2
    if same_time is False:
        return sim >= min(0.9, threshold * 2)
    return sim >= threshold


@dataclass
class Match:
    group: str
    video_id: int
    score: float
    similarity: float
    posted: bool  # déjà publié (ou en cours d'envoi) sur au moins une plateforme


def abs_time(video_created: datetime | None, is_live: bool, t: float) -> float | None:
    """Heure réelle d'un instant d'un live enregistré (l'enregistrement démarre à sa détection)."""
    if not is_live or video_created is None:
        return None
    return video_created.timestamp() + t


def find(video_id: int, text: str, t0: float | None, t1: float | None) -> list[Match]:
    """Clips récents d'autres vidéos qui montrent la même scène (le plus ressemblant en premier)."""
    s = get_settings()
    if not s.dedupe or not text:
        return []
    since = now() - timedelta(hours=s.dedupe_hours)
    found: list[Match] = []
    with db.session() as ss:
        rows = ss.exec(select(Clip, ClipInfo).join(ClipInfo, ClipInfo.clip_id == Clip.id).where(
            Clip.video_id != video_id, Clip.part == 1, Clip.created_at >= since,
            Clip.status == "ready")).all()
        for clip, info in rows:
            try:
                data = json.loads(info.data)
            except ValueError:
                continue
            other = data.get("dedupe_text")
            if not other:
                continue
            sim = similarity(text, other)
            same = overlap(t0, t1, data.get("abs_start"), data.get("abs_end"))
            if not is_duplicate(sim, same, s.dedupe_similarity):
                continue
            posts = ss.exec(select(Post).join(Clip, Clip.id == Post.clip_id).where(Clip.group == clip.group)).all()
            posted = any(p.status in ("published", "publishing") for p in posts)
            found.append(Match(clip.group, clip.video_id, clip.score, sim, posted))
    return sorted(found, key=lambda m: -m.similarity)


def replace(group: str, by_title: str) -> int:
    """Annule les envois en attente d'un clip remplacé par un meilleur clip de la même scène."""
    n = 0
    with db.session() as ss:
        for clip in ss.exec(select(Clip).where(Clip.group == group)).all():
            for p in ss.exec(select(Post).where(Post.clip_id == clip.id, Post.status == "pending")).all():
                p.status, p.error = "cancelled", f"Doublon : même scène que « {by_title} »"
                ss.add(p)
                n += 1
        ss.commit()
    return n
