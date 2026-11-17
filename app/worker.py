"""Processus de fond : surveille les sources, enregistre les lives, traite les vidéos, publie les clips."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from . import db, discovery, gta6, health, learning, maintenance, notify
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .pipeline import process, sources
from .pipeline import tags as hashtags
from .publish import creds, tiktok, youtube

log = logging.getLogger("worker")
stop = threading.Event()
recorders: dict[int, subprocess.Popen] = {}
rec_started: dict[int, float] = {}  # video.id → début de l'enregistrement (time.monotonic)


# ---------- Surveillance des sources ----------

def check_source(src: Source, lives: list[sources.Found] | None = None, videos: bool = True) -> int:
    """`lives` : lives déjà relevés (mode GTA 6) ; `videos=False` : pas de recherche des vidéos publiées."""
    if lives is None:
        found = sources.discover(src.platform, src.url, src.record_lives, src.include_videos and videos)
    else:
        found = lives + (sources.discover(src.platform, src.url, False, True) if videos and src.include_videos else [])
    day_one = gta6.active()
    added = 0
    with db.session() as s:
        for f in found:
            if s.exec(select(Video).where(Video.key == f.key)).first():
                continue
            # au premier scan, l'historique existant est ignoré (seuls les lives en cours sont pris)
            status = "new" if (src.initialized or f.is_live) else "skipped"
            if not sources.matches_keywords(f.title, src.keywords) and not (day_one and gta6.is_gta6(f.title)):
                status = "skipped"  # hors sujet (ex. pas de GTA dans le titre)
            s.add(Video(source_id=src.id, key=f.key, url=f.url, title=f.title, is_live=f.is_live, status=status))
            added += status == "new"
            if day_one and status == "new" and f.is_live and gta6.is_gta6(f.title):
                n = gta6.viewers.get(src.id)
                notify.send(f"🔴 GTA 6 en live : {src.name}" + (f" ({n} spectateurs)" if n else "") + f"\n{f.url}")
        if day_one and src.record_lives:
            # lives en file dont le streamer a coupé avant qu'une place se libère
            live_keys = {f.key for f in found if f.is_live}
            for v in s.exec(select(Video).where(Video.source_id == src.id, Video.is_live == True,  # noqa: E712
                                                Video.status == "new")).all():
                if gta6.base_key(v.key) not in live_keys:
                    path = finish_recording(v.id)  # début enregistré avant un redémarrage
                    if path and path.stat().st_size > 1_000_000:
                        v.status, v.path = "downloaded", str(path)
                    else:
                        v.status, v.error = "skipped", "Live terminé avant d'être enregistré"
                    s.add(v)
        src = s.get(Source, src.id)
        src.initialized = True
        src.last_checked = now()
        s.add(src)
        s.commit()
    return added


def poll_loop():
    s = get_settings()
    last_full = 0.0
    while not stop.is_set():
        with db.session() as ss:
            srcs = ss.exec(select(Source).where(Source.enabled == True)).all()  # noqa: E712
        day_one = gta6.active()
        # mode GTA 6 : lives vérifiés souvent (Twitch en une requête), vidéos publiées au rythme normal
        full = not day_one or time.monotonic() - last_full >= s.poll_interval
        fast = gta6.fast_lives(srcs) if day_one else {}

        def one(src):
            if not full and not src.record_lives:
                return
            try:
                n = check_source(src, fast.get(src.id), videos=full)
                if n:
                    log.info("%s : %d nouvelle(s) vidéo(s)", src.name, n)
            except Exception:
                log.exception("Vérification de %s échouée", src.name)

        if day_one:
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(one, srcs))
        else:
            for src in srcs:
                one(src)
        if full:
            last_full = time.monotonic()
        stop.wait(s.gta6_poll_interval if day_one else s.poll_interval)


# ---------- Enregistrement des lives ----------

def start_recording(v: Video) -> None:
    out = process.video_dir(v.id)
    out.mkdir(parents=True, exist_ok=True)
    log.info("Enregistrement du live %s", v.url)
    recorders[v.id] = subprocess.Popen(sources.download_cmd(v.url, out, live=True),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec_started[v.id] = time.monotonic()
    process.set_status(v.id, "recording")


def stop_recording(p: subprocess.Popen) -> None:
    """Arrête yt-dlp proprement (Ctrl+C) pour que le fichier reste lisible."""
    p.send_signal(signal.SIGINT)
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def cut_chunks() -> None:
    """Mode GTA 6 : un live enregistré depuis GTA6_CHUNK_MINUTES est coupé, le morceau part au traitement
    et la suite est mise en file (elle reprend aussitôt, sauf si un plus gros streamer attend)."""
    chunk = get_settings().gta6_chunk_minutes * 60
    if chunk <= 0:
        return
    for vid, p in list(recorders.items()):
        if p.poll() is not None or time.monotonic() - rec_started.get(vid, time.monotonic()) < chunk:
            continue
        with db.session() as s:
            v = s.get(Video, vid)
            if not v or not gta6.is_gta6(v.title):
                continue
            nxt = Video(source_id=v.source_id, key=gta6.next_part_key(v.key), url=v.url, title=v.title,
                        is_live=True, status="new")
            if s.exec(select(Video).where(Video.key == nxt.key)).first():
                continue
            s.add(nxt)
            s.commit()
        log.info("Live %d : morceau de %d min envoyé au traitement", vid, chunk // 60)
        stop_recording(p)


def lives_to_start(lives: list[Video]) -> list[Video]:
    """Hors mode GTA 6 : tous. Pendant : les plus prioritaires, dans la limite des places libres."""
    if not gta6.active():
        return lives
    with db.session() as s:
        srcs = {x.id: x for x in s.exec(select(Source)).all()}
    lives = sorted(lives, key=lambda v: gta6.live_priority(v, srcs.get(v.source_id)))
    free = max(0, get_settings().gta6_max_recordings - len(recorders))
    return lives[:free]


def get_video(vid: int) -> Video | None:
    with db.session() as s:
        return s.get(Video, vid)


def finish_recordings() -> None:
    for vid, p in list(recorders.items()):
        if p.poll() is None:
            continue
        del recorders[vid]
        rec_started.pop(vid, None)
        if stop.is_set():  # arrêt du worker : l'enregistrement reprendra au redémarrage
            continue
        path = finish_recording(vid)
        if path and path.stat().st_size > 1_000_000:
            log.info("Live %d terminé", vid)
            process.set_status(vid, "downloaded", path=str(path))
        elif "#p" in getattr(get_video(vid), "key", ""):
            process.set_status(vid, "skipped", error="Live terminé entre deux morceaux")
        else:
            process.set_status(vid, "failed", error="Enregistrement du live vide ou échoué")
            notify.error(f"Enregistrement du live {vid} vide ou échoué")


def record_loop():
    while not stop.is_set():
        if gta6.active():
            cut_chunks()
        finish_recordings()
        with db.session() as s:
            lives = s.exec(select(Video).where(Video.is_live == True, Video.status == "new")  # noqa: E712
                           .order_by(col(Video.id))).all()
        for v in lives_to_start(lives):
            start_recording(v)
        stop.wait(15)


def stash_recording(video_id: int) -> bool:
    """Met de côté le morceau de live déjà enregistré (avant un redémarrage). Vrai s'il y en a un."""
    work = process.video_dir(video_id)
    path = sources.find_output(work)
    if path and path.stat().st_size > 0:
        n = len(list(work.glob("part*")))
        path.rename(work / f"part{n + 1:02d}{path.suffix}")
    return any(work.glob("part*"))


def finish_recording(video_id: int) -> Path | None:
    """Recolle les morceaux d'un live coupé par des redémarrages en un seul fichier."""
    work = process.video_dir(video_id)
    parts = sorted(work.glob("part*"))
    last = sources.find_output(work)
    if last:
        parts.append(last)
    if len(parts) <= 1:
        return parts[0] if parts else None
    out = work / "joined.ts"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "concat:" + "|".join(str(p) for p in parts),
                        "-c", "copy", "-f", "mpegts", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        log.error("Recollage du live %d impossible : %s", video_id, r.stderr[-500:])
        out.unlink(missing_ok=True)
        return max(parts, key=lambda p: p.stat().st_size)
    for p in parts:
        p.unlink(missing_ok=True)
    return out.rename(work / "source.ts")


# ---------- Traitement ----------

def is_skipped(video_id: int) -> bool:
    with db.session() as s:
        v = s.get(Video, video_id)
        return v is None or v.status == "skipped"


def next_video() -> Video | None:
    with db.session() as s:
        q = select(Video).where(
            ((Video.status == "new") & (Video.is_live == False)) | (Video.status == "downloaded")  # noqa: E712
        ).order_by(col(Video.created_at))
        if not gta6.active():
            return s.exec(q).first()
        # mode GTA 6 : les lives GTA 6 des plus gros streamers passent devant
        todo = s.exec(q).all()
        srcs = {x.id: x for x in s.exec(select(Source)).all()}
        return min(todo, key=lambda v: gta6.process_priority(v, srcs.get(v.source_id)), default=None)


def process_loop():
    while not stop.is_set():
        if process.stopping.is_set():
            return
        v = next_video()
        if not v:
            stop.wait(10)
            continue
        try:
            if v.status == "new":
                process.set_status(v.id, "downloading")
                path = sources.download(v.url, process.video_dir(v.id),
                                        lambda: process.stopping.is_set() or is_skipped(v.id),
                                        process.Progress(v.id))
                if is_skipped(v.id):
                    raise sources.Cancelled()
                process.set_status(v.id, "downloaded", path=str(path))
            log.info("Traitement de « %s »", v.title)
            n = process.process(v.id)
            log.info("« %s » : %d clip(s) créé(s)", v.title, n)
        except process.Interrupted:
            log.info("« %s » : arrêt du worker, le traitement reprendra au redémarrage", v.title)
            return
        except sources.Cancelled:
            if process.stopping.is_set() and not is_skipped(v.id):
                log.info("« %s » : arrêt du worker, le téléchargement reprendra au redémarrage", v.title)
                return
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
    day_one = gta6.active()  # la hype est courte : on publie plus vite, à toute heure
    if not (day_one and cfg.gta6_publish_anytime) and not in_publish_window():
        return False
    gap = cfg.gta6_min_minutes_between_posts if day_one else cfg.min_minutes_between_posts
    last = s.exec(select(Post).where(Post.platform == platform, Post.status == "published")
                  .order_by(col(Post.published_at).desc())).first()
    if last and last.published_at and now() - last.published_at < timedelta(minutes=gap):
        return False
    day = s.exec(select(Post).where(Post.platform == platform, Post.status == "published",
                                    Post.published_at >= now() - timedelta(days=1))).all()
    limit = cfg.gta6_max_posts_per_day if day_one and cfg.gta6_max_posts_per_day > 0 else cfg.max_posts_per_day
    return len(day) < limit


def next_post(s, platform: str) -> Post | None:
    """Prochain envoi : les parties suivantes d'un clip passent en priorité, dans l'ordre."""
    rows = s.exec(select(Post, Clip, Video).join(Clip, Clip.id == Post.clip_id).join(Video, Video.id == Clip.video_id)
                  .where(Post.platform == platform, Post.status == "pending", Clip.status == "ready")
                  .order_by(col(Clip.part).desc(), col(Clip.score).desc())).all()
    if gta6.active():
        # mode GTA 6 : les suites d'abord, puis les clips GTA 6 les plus récents (la hype ne dure pas)
        rows = sorted(rows, key=lambda r: (r[1].part <= 1, not gta6.is_gta6(r[2].title), -r[2].id))
    pending = [(post, clip) for post, clip, _ in rows]
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


def discover_loop():
    discovery.loop(stop)


def reset_interrupted():
    """Remet en file les tâches coupées par un redémarrage."""
    with db.session() as s:
        for v in s.exec(select(Video).where(col(Video.status).in_(
                ["downloading", "transcribing", "analyzing", "rendering", "recording"]))).all():
            if v.status == "recording":
                # le live continue peut-être : on relance l'enregistrement, les morceaux seront recollés
                stash_recording(v.id)
                v.status = "new"
            else:
                v.status = "downloaded" if v.path and Path(v.path).exists() else "new"
            s.add(v)
        for p in s.exec(select(Post).where(Post.status == "publishing")).all():
            p.status = "pending"
            s.add(p)
        s.commit()


def shutdown(*_):
    """Arrêt demandé (docker compose up --build, restart…) : on termine proprement."""
    if stop.is_set():
        return
    log.info("Arrêt demandé : sauvegarde du travail en cours…")
    stop.set()
    process.stopping.set()
    for p in recorders.values():  # yt-dlp finalise le fichier du live sur Ctrl+C
        if p.poll() is None:
            p.send_signal(signal.SIGINT)


def wait_for(threads: list[threading.Thread], recs: list[subprocess.Popen], timeout: float) -> None:
    end = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    for p in recs:
        try:
            p.wait(max(0.1, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.get_engine()
    reset_interrupted()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    crashed = health.started("worker")
    threads = [threading.Thread(target=f, daemon=True, name=f.__name__)
               for f in (poll_loop, record_loop, process_loop, publish_loop, stats_loop, maintenance_loop,
                         discover_loop)]
    for t in threads:
        t.start()
    # la surveillance démarre après : elle vérifie que les autres tâches tournent toujours
    threading.Thread(target=health.loop, args=("worker", stop, threads), daemon=True, name="health").start()
    log.info("Worker démarré")
    notify.send(health.crash_message("worker", crashed) if crashed else "🟢 Clip a démarré")
    while not stop.wait(3600):
        pass
    recs = list(recorders.values())
    # le traitement s'arrête à la fin de la phrase en cours ; on lui laisse jusqu'à 90 s
    wait_for([t for t in threads if t.name == "process_loop"], recs, 90)
    health.stopped("worker")
    log.info("Worker arrêté proprement")


if __name__ == "__main__":
    main()
