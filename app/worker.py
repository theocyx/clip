"""Processus de fond : surveille les sources, enregistre les lives, traite les vidéos, publie les clips."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from . import db, learning, maintenance, notify
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .pipeline import process, sources
from .pipeline import tags as hashtags
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
            if not sources.matches_keywords(f.title, src.keywords):
                status = "skipped"  # hors sujet (ex. pas de GTA dans le titre)
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
                notify.error(f"Enregistrement du live {vid} vide ou échoué")
        stop.wait(15)


# ---------- Traitement ----------

def is_skipped(video_id: int) -> bool:
    with db.session() as s:
        v = s.get(Video, video_id)
        return v is None or v.status == "skipped"


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
                path = sources.download(v.url, process.video_dir(v.id), lambda: is_skipped(v.id))
                if is_skipped(v.id):
                    raise sources.Cancelled()
                process.set_status(v.id, "downloaded", path=str(path))
            log.info("Traitement de « %s »", v.title)
            n = process.process(v.id)
            log.info("« %s » : %d clip(s) créé(s)", v.title, n)
        except sources.Cancelled:
            log.info("« %s » ignorée : traitement arrêté", v.title)
            process.delete_video_files(v.id)
        except Exception as e:
            if is_skipped(v.id):
                continue
            log.exception("Traitement de la vidéo %d échoué", v.id)
            process.set_status(v.id, "failed", error=str(e)[:1000])
            notify.error(f"Traitement échoué : {v.title}", str(e))


# ---------- Publication ----------

PUBLISHERS = {
    "youtube": lambda clip, desc, tags: youtube.upload(clip.path, clip.title, desc, tags),
    "tiktok": lambda clip, desc, tags: tiktok.upload(clip.path, tiktok_caption(clip, desc, tags)),
}


def streamer_tag(name: str) -> str:
    """« Le Bouseuh » → « LeBouseuh » (hashtag)."""
    return re.sub(r"[^\w]", "", name or "", flags=re.UNICODE)


def credit(video: Video) -> tuple[str, str]:
    """(nom du streamer, lien de sa chaîne) de la source de la vidéo, ou ("", "")."""
    if not get_settings().credit_streamer or not video.source_id:
        return "", ""
    with db.session() as s:
        src = s.get(Source, video.source_id)
    return (src.name, src.url) if src else ("", "")


def clip_text(clip: Clip) -> str:
    if clip.id is None:
        return ""
    with db.session() as s:
        info = s.get(ClipInfo, clip.id)
    try:
        return json.loads(info.data).get("text", "") if info else ""
    except ValueError:
        return ""


def description(clip: Clip, video: Video) -> tuple[str, list[str]]:
    s = get_settings()
    name, channel = credit(video)
    tag = streamer_tag(name)
    keywords = ""
    if video.source_id:
        with db.session() as ss:
            src = ss.get(Source, video.source_id)
        keywords = src.keywords if src else ""
    trend = hashtags.trending(video.title, keywords, clip.title, clip_text(clip))
    tags_ = hashtags.merge([tag] if tag else [], trend, hashtags.split_tags(s.hashtags), limit=s.max_hashtags)
    lines = [clip.title, ""]
    if name:
        lines += [f"🎮 Streamer : {name}", f"👉 Sa chaîne : {channel}", ""]
    lines += [f"Extrait de : {video.title}", video.url, "", " ".join("#" + t for t in tags_)]
    return "\n".join(lines), tags_


def tiktok_caption(clip: Clip, desc: str, tags: list[str]) -> str:
    who = next((ln.split(": ", 1)[1] for ln in desc.splitlines() if ln.startswith("🎮 Streamer : ")), "")
    return f"{clip.title}" + (f" 🎮 {who}" if who else "") + " " + " ".join("#" + t for t in tags)


def parse_hours(spec: str) -> list[tuple[int, int]]:
    """« 12-14,18-22:30 » → [(720, 840), (1080, 1350)] en minutes depuis minuit."""
    def minutes(x: str) -> int:
        h, _, m = x.strip().partition(":")
        return int(h) * 60 + int(m or 0)
    out = []
    for part in (spec or "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.append((minutes(a), minutes(b)))
    return out


def in_publish_window(at: datetime | None = None) -> bool:
    cfg = get_settings()
    ranges = parse_hours(cfg.publish_hours)
    if not ranges:
        return True
    t = at or datetime.now(ZoneInfo(cfg.timezone))
    m = t.hour * 60 + t.minute
    return any((a <= m < b) if a <= b else (m >= a or m < b) for a, b in ranges)


def can_post(s, platform: str) -> bool:
    cfg = get_settings()
    if not in_publish_window():
        return False
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
        if status == "failed":
            notify.error(f"Publication {platform} abandonnée après 3 essais : {clip.title}", err)
    with db.session() as s:
        post = s.get(Post, post.id)
        post.status, post.remote_id, post.error = status, remote, err
        if status == "published":
            post.published_at = now()
        s.add(post)
        s.commit()
    if status == "published":
        notify.published(platform, clip.title, remote)
    return status == "published"


def publish_loop():
    while not stop.is_set():
        for platform in ("youtube", "tiktok"):
            try:
                publish_once(platform)
            except Exception:
                log.exception("Erreur de publication")
        stop.wait(60)


# ---------- Statistiques et apprentissage ----------

def stats_loop():
    cfg = get_settings()
    stop.wait(300)  # laisse le reste démarrer
    while not stop.is_set():
        if cfg.learning:
            try:
                log.info("Apprentissage : %s", learning.run()["message"])
            except Exception:
                log.exception("Relevé des statistiques échoué")
        stop.wait(cfg.stats_interval_hours * 3600)


def maintenance_loop():
    stop.wait(120)
    while not stop.is_set():
        maintenance.run()
        stop.wait(6 * 3600)  # la sauvegarde n'est faite qu'une fois par jour


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
               for f in (poll_loop, record_loop, process_loop, publish_loop, stats_loop, maintenance_loop)]
    for t in threads:
        t.start()
    log.info("Worker démarré")
    notify.send("🟢 Clip a démarré")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
