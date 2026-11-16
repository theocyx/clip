"""Processus de fond : surveille les sources, enregistre les lives, traite les vidéos, publie les clips."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import Clip, Post, Source, Video, now
from .pipeline import process, sources
from .publish import creds, tiktok, youtube

log = logging.getLogger("worker")
stop = threading.Event()
recorders: dict[int, subprocess.Popen] = {}


# ---------- Surveillance des sources ----------

def check_source(src: Source) -> int:
    found = sources.discover(src.platform, src.url, src.record_lives, src.include_videos)
    added = 0
    with db.session() as s:
        for f in found:
            if s.exec(select(Video).where(Video.key == f.key)).first():
                continue
            # au premier scan, l'historique existant est ignoré (seuls les lives en cours sont pris)
            status = "new" if (src.initialized or f.is_live) else "skipped"
            s.add(Video(source_id=src.id, key=f.key, url=f.url, title=f.title, is_live=f.is_live, status=status))
            added += status == "new"
        src = s.get(Source, src.id)
        src.initialized = True
        src.last_checked = now()
        s.add(src)
        s.commit()
    return added


def poll_loop():
    s = get_settings()
    while not stop.is_set():
        with db.session() as ss:
            srcs = ss.exec(select(Source).where(Source.enabled == True)).all()  # noqa: E712
        for src in srcs:
            try:
                n = check_source(src)
                if n:
                    log.info("%s : %d nouvelle(s) vidéo(s)", src.name, n)
            except Exception:
                log.exception("Vérification de %s échouée", src.name)
        stop.wait(s.poll_interval)


# ---------- Enregistrement des lives ----------

def record_loop():
    while not stop.is_set():
        with db.session() as s:
            lives = s.exec(select(Video).where(Video.is_live == True, Video.status == "new")).all()  # noqa: E712
        for v in lives:
            out = process.video_dir(v.id)
            out.mkdir(parents=True, exist_ok=True)
            log.info("Enregistrement du live %s", v.url)
            recorders[v.id] = subprocess.Popen(sources.download_cmd(v.url, out, live=True),
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.set_status(v.id, "recording")
        for vid, p in list(recorders.items()):
            if p.poll() is None:
                continue
            del recorders[vid]
            path = sources.find_output(process.video_dir(vid))
            if path and path.stat().st_size > 1_000_000:
                log.info("Live %d terminé", vid)
                process.set_status(vid, "downloaded", path=str(path))
            else:
                process.set_status(vid, "failed", error="Enregistrement du live vide ou échoué")
        stop.wait(15)


# ---------- Traitement ----------

def next_video() -> Video | None:
    with db.session() as s:
        return s.exec(
            select(Video).where(
                ((Video.status == "new") & (Video.is_live == False)) | (Video.status == "downloaded")  # noqa: E712
            ).order_by(col(Video.created_at))
        ).first()


def process_loop():
    while not stop.is_set():
        v = next_video()
        if not v:
            stop.wait(10)
            continue
        try:
            if v.status == "new":
                process.set_status(v.id, "downloading")
                path = sources.download(v.url, process.video_dir(v.id))
                process.set_status(v.id, "downloaded", path=str(path))
            log.info("Traitement de « %s »", v.title)
            n = process.process(v.id)
            log.info("« %s » : %d clip(s) créé(s)", v.title, n)
        except Exception as e:
            log.exception("Traitement de la vidéo %d échoué", v.id)
            process.set_status(v.id, "failed", error=str(e)[:1000])


# ---------- Publication ----------

PUBLISHERS = {
    "youtube": lambda clip, desc, tags: youtube.upload(clip.path, clip.title, desc, tags),
    "tiktok": lambda clip, desc, tags: tiktok.upload(clip.path, f"{clip.title} {' '.join('#' + t for t in tags)}"),
}


def description(clip: Clip, video: Video) -> tuple[str, list[str]]:
    s = get_settings()
    tags = [t.lstrip("#") for t in s.hashtags.split() if t.strip("#")]
    desc = f"{clip.title}\n\nExtrait de : {video.title}\n{video.url}\n\n{' '.join('#' + t for t in tags)}"
    return desc, tags


def can_post(s, platform: str) -> bool:
    cfg = get_settings()
    last = s.exec(select(Post).where(Post.platform == platform, Post.status == "published")
                  .order_by(col(Post.published_at).desc())).first()
    if last and last.published_at and now() - last.published_at < timedelta(minutes=cfg.min_minutes_between_posts):
        return False
    day = s.exec(select(Post).where(Post.platform == platform, Post.status == "published",
                                    Post.published_at >= now() - timedelta(days=1))).all()
    return len(day) < cfg.max_posts_per_day


def next_post(s, platform: str) -> Post | None:
    """Prochain envoi : les parties suivantes d'un clip passent en priorité, dans l'ordre."""
    pending = s.exec(select(Post, Clip).join(Clip, Clip.id == Post.clip_id)
                     .where(Post.platform == platform, Post.status == "pending", Clip.status == "ready")
                     .order_by(col(Clip.part).desc(), col(Clip.score).desc())).all()
    for post, clip in pending:
        if clip.part > 1:
            prev = s.exec(select(Post).join(Clip, Clip.id == Post.clip_id).where(
                Clip.group == clip.group, Clip.part == clip.part - 1, Post.platform == platform)).first()
            if prev and prev.status != "published":
                continue
        return post
    return None


def connected(platform: str) -> bool:
    if platform == "youtube":
        return youtube.configured() and creds.load("youtube") is not None
    return tiktok.configured() and creds.load("tiktok") is not None


def publish_once(platform: str) -> bool:
    if not connected(platform):
        return False
    with db.session() as s:
        if not can_post(s, platform):
            return False
        post = next_post(s, platform)
        if not post:
            return False
        clip = s.get(Clip, post.clip_id)
        video = s.get(Video, clip.video_id)
        post.status, post.attempts = "publishing", post.attempts + 1
        s.add(post)
        s.commit()
    desc, tags = description(clip, video)
    try:
        if not Path(clip.path).exists():
            raise RuntimeError("Fichier du clip introuvable")
        remote = PUBLISHERS[platform](clip, desc, tags)
        status, err = "published", ""
        log.info("Publié sur %s : %s", platform, clip.title)
    except Exception as e:
        log.exception("Publication %s échouée", platform)
        remote, err = "", str(e)[:1000]
        status = "failed" if post.attempts >= 3 else "pending"
    with db.session() as s:
        post = s.get(Post, post.id)
        post.status, post.remote_id, post.error = status, remote, err
        if status == "published":
            post.published_at = now()
        s.add(post)
        s.commit()
    return status == "published"


def publish_loop():
    while not stop.is_set():
        for platform in ("youtube", "tiktok"):
            try:
                publish_once(platform)
            except Exception:
                log.exception("Erreur de publication")
        stop.wait(60)


def reset_interrupted():
    """Remet en file les tâches coupées par un redémarrage."""
    with db.session() as s:
        for v in s.exec(select(Video).where(col(Video.status).in_(
                ["downloading", "transcribing", "analyzing", "rendering", "recording"]))).all():
            if v.status == "recording":
                path = sources.find_output(process.video_dir(v.id))
                v.status, v.path = ("downloaded", str(path)) if path else ("failed", "")
            else:
                v.status = "downloaded" if v.path and Path(v.path).exists() else "new"
            s.add(v)
        for p in s.exec(select(Post).where(Post.status == "publishing")).all():
            p.status = "pending"
            s.add(p)
        s.commit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.get_engine()
    reset_interrupted()
    threads = [threading.Thread(target=f, daemon=True, name=f.__name__)
               for f in (poll_loop, record_loop, process_loop, publish_loop)]
    for t in threads:
        t.start()
    log.info("Worker démarré")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
