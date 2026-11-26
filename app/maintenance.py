"""Sauvegarde quotidienne de la base et ménage du disque (vieilles vidéos sources, clips déjà publiés)."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import Clip, Post, Video, now

log = logging.getLogger(__name__)

ACTIVE = ["new", "recording", "downloading", "downloaded", "transcribing", "analyzing", "rendering"]


def backup(today: str | None = None) -> Path | None:
    """Copie cohérente de la base dans data/backups/clip-AAAA-MM-JJ.db (une par jour), garde les N dernières."""
    s = get_settings()
    if s.backup_keep <= 0:
        return None
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    out = s.backups_dir / f"clip-{today or datetime.now().strftime('%Y-%m-%d')}.db"
    if not out.exists():
        tmp = out.with_suffix(".tmp")
        src, dst = sqlite3.connect(s.db_path), sqlite3.connect(tmp)
        try:
            src.backup(dst)  # fonctionne même si le worker écrit en même temps
        finally:
            dst.close()
            src.close()
        tmp.replace(out)
        log.info("Base sauvegardée : %s", out.name)
    for old in sorted(s.backups_dir.glob("clip-*.db"))[:-s.backup_keep]:
        old.unlink(missing_ok=True)
    return out


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def cleanup() -> int:
    """Supprime les fichiers devenus inutiles. Renvoie le nombre d'octets libérés."""
    s = get_settings()
    freed = 0
    with db.session() as ss:
        # dossiers de travail des vidéos terminées / échouées / ignorées depuis plus de N jours
        limit = now() - timedelta(days=s.source_keep_days)
        old = ss.exec(select(Video).where(col(Video.status).not_in(ACTIVE), Video.updated_at < limit)).all()
        for v in old:
            d = s.videos_dir / str(v.id)
            if d.exists():
                freed += _size(d)
                shutil.rmtree(d, ignore_errors=True)
            if v.path:
                v.path = ""
                ss.add(v)

        # fichiers des clips publiés depuis plus de CLIP_KEEP_DAYS jours, et des clips jamais publiés (retirés par
        # le tri) depuis plus de UNPUBLISHED_KEEP_DAYS jours : sinon ~6 Go de plus chaque jour (constaté le 26/11)
        days = [d for d in (s.clip_keep_days, s.unpublished_keep_days) if d > 0]
        if days:
            limit = now() - timedelta(days=min(days))
            for c in ss.exec(select(Clip).where(Clip.status != "deleted", Clip.created_at < limit)).all():
                p = Path(c.path) if c.path else None
                if not p or not p.exists():
                    continue
                posts = ss.exec(select(Post).where(Post.clip_id == c.id)).all()
                if any(x.status in ("pending", "publishing") for x in posts):
                    continue
                age = now() - c.created_at
                if any(x.status in ("published", "manual") for x in posts) or c.status != "ready":
                    if s.clip_keep_days <= 0 or age < timedelta(days=s.clip_keep_days):
                        continue
                elif s.unpublished_keep_days <= 0 or age < timedelta(days=s.unpublished_keep_days):
                    continue  # jamais publié, encore récent : on garde le fichier (publication manuelle possible)
                else:
                    c.status = "deleted"  # plus proposé à l'envoi manuel : le fichier n'existe plus
                    ss.add(c)
                for f in p.parent.glob(f"{p.stem}*"):  # le clip et son image d'accroche
                    freed += _size(f)
                    f.unlink(missing_ok=True)
        ss.commit()
    if freed:
        log.info("Ménage : %.1f Go libérés", freed / 1e9)
    return freed


def run() -> None:
    try:
        backup()
    except Exception:
        log.exception("Sauvegarde de la base échouée")
        from . import notify
        notify.error("Sauvegarde de la base échouée")
    try:
        cleanup()
    except Exception:
        log.exception("Ménage du disque échoué")
