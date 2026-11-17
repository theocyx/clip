"""Interrupteurs de publication par plateforme (réglés dans l'interface, stockés en base).

Une plateforme coupée ne perd rien : les clips restent en attente pour elle et partent
quand on la rallume (ou peuvent être retirés de la file d'un clic).
"""
from sqlmodel import col, func, select

from . import db
from .models import Post, Setting, now

PLATFORMS = ("youtube", "tiktok")
LABELS = {"youtube": "YouTube Shorts", "tiktok": "TikTok"}
# TikTok coupé tant que l'app n'est pas validée par TikTok
DEFAULTS = {"youtube": True, "tiktok": False}


def _key(platform: str) -> str:
    return f"publish_{platform}"


def enabled(platform: str) -> bool:
    with db.session() as s:
        row = s.get(Setting, _key(platform))
    if row is None:
        return DEFAULTS.get(platform, False)
    return row.value == "1"


def set_enabled(platform: str, on: bool) -> None:
    if platform not in PLATFORMS:
        raise ValueError(platform)
    with db.session() as s:
        row = s.get(Setting, _key(platform)) or Setting(key=_key(platform))
        row.value, row.updated_at = ("1" if on else "0"), now()
        s.add(row)
        s.commit()


def drop_pending(platform: str) -> int:
    """Retire de la file les clips en attente pour cette plateforme (republiables depuis « Clips »)."""
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.platform == platform, Post.status == "pending")).all()
        for p in posts:
            p.status, p.error = "cancelled", "Retiré de la file (plateforme en pause)"
            s.add(p)
        s.commit()
    return len(posts)


def status() -> list[dict]:
    with db.session() as s:
        pending = dict(s.exec(select(Post.platform, func.count()).where(Post.status == "pending")
                              .group_by(Post.platform)).all())
        last = {p: s.exec(select(Post.published_at).where(Post.platform == p, Post.status == "published")
                          .order_by(col(Post.published_at).desc())).first() for p in PLATFORMS}
    return [{"name": p, "label": LABELS[p], "on": enabled(p), "pending": pending.get(p, 0), "last": last[p]}
            for p in PLATFORMS]
