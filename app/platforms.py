"""Interrupteurs de publication par plateforme (réglés dans l'interface, stockés en base).

Une plateforme coupée ne perd rien : les clips restent en attente pour elle et partent
quand on la rallume (ou peuvent être retirés de la file d'un clic).
"""
import re
from datetime import datetime

from sqlmodel import col, func, select

from . import db
from .config import get_settings
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


def _get(key: str) -> str | None:
    with db.session() as s:
        row = s.get(Setting, key)
    return row.value if row else None


def _put(key: str, value: str | None) -> None:
    with db.session() as s:
        row = s.get(Setting, key)
        if value is None:
            if row:
                s.delete(row)
        else:
            row = row or Setting(key=key)
            row.value, row.updated_at = value, now()
            s.add(row)
        s.commit()


def daily_limit(platform: str) -> int | None:
    """Limite d'envois/jour réglée dans l'interface (0 = aucune), None = celle du .env."""
    v = _get(f"limit_{platform}")
    return int(v) if v not in (None, "") and v.isdigit() else None


def set_daily_limit(platform: str, value: int | None) -> None:
    if platform not in PLATFORMS:
        raise ValueError(platform)
    _put(f"limit_{platform}", None if value is None else str(max(0, value)))


def blocked_until(platform: str) -> datetime | None:
    """Envois en pause jusqu'à cette date (UTC) après un refus « quota atteint »."""
    v = _get(f"quota_until_{platform}")
    if not v:
        return None
    until = datetime.fromisoformat(v)
    return until if until > now() else None


def block_until(platform: str, until: datetime | None) -> None:
    _put(f"quota_until_{platform}", until.isoformat() if until else None)


HOURS_RE = re.compile(r"^\s*(\d{1,2}(:\d{2})?\s*-\s*\d{1,2}(:\d{2})?)(\s*,\s*\d{1,2}(:\d{2})?\s*-\s*\d{1,2}(:\d{2})?)*\s*$")


def valid_hours(spec: str) -> bool:
    """« 12-14,18-22:30 » ; vide = à toute heure."""
    if not spec.strip():
        return True
    if not HOURS_RE.match(spec):
        return False
    for t in re.findall(r"\d{1,2}(?::\d{2})?", spec):
        h, _, m = t.partition(":")
        if int(h) > 24 or int(m or 0) > 59 or (int(h) == 24 and int(m or 0)):
            return False
    return True


def publish_hours() -> str:
    """Heures d'envoi réglées dans l'interface, sinon PUBLISH_HOURS du .env."""
    v = _get("publish_hours")
    return get_settings().publish_hours if v is None else v


def min_gap() -> int:
    """Minutes minimum entre deux envois (interface, sinon MIN_MINUTES_BETWEEN_POSTS)."""
    v = _get("min_gap")
    return int(v) if v and v.isdigit() else get_settings().min_minutes_between_posts


def set_schedule(hours: str | None, gap: int | None) -> None:
    """None = revenir à la valeur du .env."""
    if hours is not None and not valid_hours(hours):
        raise ValueError(hours)
    _put("publish_hours", None if hours is None else re.sub(r"\s+", "", hours))
    _put("min_gap", None if gap is None else str(max(0, gap)))


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
