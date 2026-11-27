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

from . import compile as compil, curate, db, discovery, growth, gta6, health, learning, maintenance, notify, platforms
from .config import get_settings
from .models import Clip, ClipInfo, Post, Source, Video, now
from .news import generate as news
from .pipeline import chat, engage, process, sources, sponsor
from .pipeline import tags as hashtags
from .publish import creds, tiktok, youtube

log = logging.getLogger("worker")
stop = threading.Event()
recorders: dict[int, subprocess.Popen] = {}
rec_started: dict[int, float] = {}  # video.id → début de l'enregistrement (time.monotonic)


# ---------- Surveillance des sources ----------

OFF_CATEGORY = "Catégorie Twitch :"
paused_lives: set[str] = set()  # lives coupés parce que le streamer a changé de jeu (clé de base)


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
            gta_live = day_one and f.is_live and gta6.live_matches(f.title, f.game)
            old = s.exec(select(Video).where(Video.key == f.key)).first()
            if old:
                if gta_live and old.status == "skipped" and (old.error or "").startswith(OFF_CATEGORY):
                    old.status, old.error = "new", ""  # le streamer est passé sur GTA 6 : on enregistre
                    paused_lives.discard(f.key)
                    s.add(old)
                    added += 1
                elif gta_live and f.key in paused_lives:
                    paused_lives.discard(f.key)  # retour sur GTA 6 après un autre jeu : la suite est enregistrée
                    n = 2
                    while s.exec(select(Video).where(Video.key == f"{f.key}#p{n}")).first():
                        n += 1
                    s.add(Video(source_id=src.id, key=f"{f.key}#p{n}", url=f.url, title=f.title, is_live=True,
                                status="new"))
                    added += 1
                continue
            # au premier scan, l'historique existant est ignoré (seuls les lives en cours sont pris)
            status = "new" if (src.initialized or f.is_live) else "skipped"
            error = ""
            if day_one and f.is_live and f.game and not gta_live:
                status, error = "skipped", f"{OFF_CATEGORY} {f.game} (pas GTA 6)"
            elif not sources.matches_keywords(f.title, src.keywords) and not gta_live:
                status = "skipped"  # hors sujet (ex. pas de GTA dans le titre)
            s.add(Video(source_id=src.id, key=f.key, url=f.url, title=f.title, is_live=f.is_live, status=status,
                        error=error))
            added += status == "new"
            if status == "new" and gta_live:
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
    if get_settings().chat_signal:
        try:
            chat.start(v.id, v.url, out)
        except Exception as e:  # le chat est un bonus : l'enregistrement continue sans
            log.warning("Chat du live %d non enregistré : %s", v.id, e)
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


def stop_off_category() -> int:
    """Mode GTA 6 : arrête l'enregistrement d'un live dont la catégorie Twitch passe à un autre jeu.
    Ce qui est déjà enregistré part au traitement ; si le streamer revient sur GTA 6, la suite est reprise."""
    n = 0
    for vid, p in list(recorders.items()):
        v = get_video(vid)
        game = gta6.off_category(v.source_id) if v else ""
        if not game or p.poll() is not None:
            continue
        log.info("Live %d : le streamer joue maintenant à %s, enregistrement arrêté", vid, game)
        paused_lives.add(gta6.base_key(v.key))
        stop_recording(p)
        n += 1
    return n


def lives_to_start(lives: list[Video]) -> list[Video]:
    """Hors mode GTA 6 : tous. Pendant : les plus prioritaires, dans la limite des places libres."""
    if not gta6.active():
        return lives
    with db.session() as s:
        srcs = {x.id: x for x in s.exec(select(Source)).all()}
    keep = []
    for v in lives:
        game = gta6.off_category(v.source_id)
        if game:  # en file, mais le streamer joue maintenant à autre chose
            process.set_status(v.id, "skipped", error=f"{OFF_CATEGORY} {game} (pas GTA 6)")
            paused_lives.add(gta6.base_key(v.key))
        else:
            keep.append(v)
    lives = sorted(keep, key=lambda v: gta6.live_priority(v, srcs.get(v.source_id)))
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
        chat.stop(vid)
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


def record_once() -> None:
    if gta6.active():
        stop_off_category()
        cut_chunks()
    finish_recordings()
    with db.session() as s:
        lives = s.exec(select(Video).where(Video.is_live == True, Video.status == "new")  # noqa: E712
                       .order_by(col(Video.id))).all()
    for v in lives_to_start(lives):
        start_recording(v)


def record_loop():
    while not stop.is_set():
        try:
            record_once()
        except Exception:  # ex. base momentanément verrouillée : on réessaie au tour suivant
            log.exception("Tour d'enregistrement des lives en erreur")
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


def drop_stale() -> int:
    """Abandonne les morceaux de live téléchargés qui attendent depuis trop longtemps (plus d'actualité)."""
    h = get_settings().stale_chunk_hours
    if h <= 0:
        return 0
    with db.session() as s:
        old = s.exec(select(Video).where(Video.status == "downloaded", Video.is_live == True,  # noqa: E712
                                         Video.created_at < now() - timedelta(hours=h))).all()
        for v in old:
            v.status, v.error = "skipped", f"Morceau en attente depuis plus de {h:g} h : abandonné"
            s.add(v)
        s.commit()
    if old:
        log.info("%d morceau(x) de live trop ancien(s) abandonné(s)", len(old))
    return len(old)


def next_video() -> Video | None:
    """Prochaine vidéo téléchargée à traiter : la plus récente d'abord (en mode GTA 6, l'ordre de priorité)."""
    drop_stale()
    with db.session() as s:
        q = select(Video).where(Video.status == "downloaded").order_by(col(Video.created_at).desc())
        if not gta6.active():
            if not get_settings().source_priority:
                return s.exec(q).first()
            # les streamers dont les clips marchent le mieux d'abord, puis le plus récent
            todo = s.exec(q).all()
            return min(todo, key=lambda v: (-growth.source_tier(v.source_id), -v.created_at.timestamp()),
                       default=None)
        # mode GTA 6 : les lives GTA 6 des plus gros streamers passent devant
        todo = s.exec(q).all()
        srcs = {x.id: x for x in s.exec(select(Source)).all()}
        return min(todo, key=lambda v: gta6.process_priority(v, srcs.get(v.source_id)), default=None)


def next_download() -> Video | None:
    """Prochaine VOD à télécharger (la plus ancienne d'abord)."""
    with db.session() as s:
        return s.exec(select(Video).where(Video.status == "new", Video.is_live == False)  # noqa: E712
                      .order_by(col(Video.created_at))).first()


def download_loop():
    """Téléchargements à part : le montage n'attend plus la fin d'un téléchargement."""
    while not stop.is_set():
        if process.stopping.is_set():
            return
        v = next_download()
        if not v:
            stop.wait(10)
            continue
        try:
            process.set_status(v.id, "downloading")
            path = sources.download(v.url, process.video_dir(v.id),
                                    lambda: process.stopping.is_set() or is_skipped(v.id),
                                    process.Progress(v.id))
            if is_skipped(v.id):
                raise sources.Cancelled()
            process.set_status(v.id, "downloaded", path=str(path))
        except sources.Cancelled:
            if process.stopping.is_set() and not is_skipped(v.id):
                log.info("« %s » : arrêt du worker, le téléchargement reprendra au redémarrage", v.title)
                return
            log.info("« %s » ignorée : téléchargement arrêté", v.title)
            process.delete_video_files(v.id)
        except Exception as e:
            if is_skipped(v.id):
                continue
            if sources.subscriber_only(str(e)):  # VOD réservée aux abonnés : impossible sans compte, pas une panne
                log.info("« %s » ignorée : vidéo réservée aux abonnés", v.title)
                process.set_status(v.id, "skipped", error="Vidéo réservée aux abonnés")
                process.delete_video_files(v.id)
                continue
            log.exception("Téléchargement de la vidéo %d échoué", v.id)
            process.set_status(v.id, "failed", error=str(e)[:1000])
            notify.error(f"Téléchargement échoué : {v.title}", str(e))


def process_loop():
    while not stop.is_set():
        if process.stopping.is_set():
            return
        v = next_video()
        if not v:
            stop.wait(10)
            continue
        try:
            log.info("Traitement de « %s »", v.title)
            n = process.process(v.id)
            log.info("« %s » : %d clip(s) créé(s)", v.title, n)
        except process.Interrupted:
            log.info("« %s » : arrêt du worker, le traitement reprendra au redémarrage", v.title)
            return
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
    "youtube": lambda clip, desc, tags: youtube.upload(clip.path, youtube_title(clip.title, tags), desc, tags),
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


def clip_data(clip: Clip) -> dict:
    if clip.id is None:
        return {}
    with db.session() as s:
        info = s.get(ClipInfo, clip.id)
    try:
        return json.loads(info.data) if info else {}
    except ValueError:
        return {}


def clip_text(clip: Clip) -> str:
    return clip_data(clip).get("text", "")


def clip_question(clip: Clip) -> str:
    """Question qui lance les commentaires (seulement sur la dernière partie : les autres renvoient à la suite)."""
    if not get_settings().end_question or clip.part < clip.part_count:
        return ""
    return engage.caption_question(clip_data(clip).get("question", ""))


def description(clip: Clip, video: Video) -> tuple[str, list[str]]:
    s = get_settings()
    if news.is_news(video):  # vidéo d'actu : source et crédits des images au lieu du streamer
        return news.description(clip, clip_data(clip), hashtags.split_tags(s.hashtags), clip_question(clip))
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
    ask = clip_question(clip)
    if ask:
        lines += [ask, ""]
    sub = growth.subscribe_line("clip")
    if sub:
        lines += [sub, ""]
    if name:
        lines += [f"🎮 Streamer : {name}", f"👉 Sa chaîne : {channel}", ""]
    lines += [f"Extrait de : {video.title}", video.url, "", " ".join("#" + t for t in tags_)]
    return "\n".join(lines), tags_


def youtube_title(title: str, tags: list[str]) -> str:
    """Titre + les 1-2 hashtags les plus ciblés à la fin (cliquables et indexés par la recherche Shorts)."""
    n = get_settings().title_hashtags
    out = title.strip()
    for t in [t for t in tags if t.lower() != "shorts"][:max(0, n)]:
        if "#" + t.lower() in out.lower():
            continue
        cand = f"{out} #{t}"
        if len(cand) > 100:
            break
        out = cand
    return out


GENERIC_TAGS = {"shorts", "short", "clip", "clips", "fr", "clipsfr", "viral", "fyp", "pourtoi"}
TIKTOK_FIRST = ["gta6", "gta", "gtarp"]


def tiktok_tags(tags: list[str], limit: int) -> list[str]:
    """Hashtags TikTok en minuscules, #gta6 #gta d'abord (comme les TikTok GTA 6 de la chaîne qui marchent),
    sans les tags génériques (#shorts, #clip, #fr)."""
    seen, picked = set(), []
    for t in tags:
        low = t.lower().lstrip("#")
        if low and low not in seen and low not in GENERIC_TAGS:
            seen.add(low)
            picked.append(low)
    picked.sort(key=lambda t: TIKTOK_FIRST.index(t) if t in TIKTOK_FIRST else len(TIKTOK_FIRST))
    return picked[:limit] if limit > 0 else picked


def tiktok_caption(clip: Clip, desc: str, tags: list[str]) -> str:
    """Légende TikTok courte et orale, comme les TikTok GTA 6 de la chaîne qui font le plus de vues :
    titre + 1 emoji + #gta6 #gta, puis le crédit du streamer sur une ligne courte."""
    from .pipeline import hook as hooks

    s = get_settings()
    info = clip_data(clip)
    if info.get("kind") == "news":
        return news.caption(clip, info, s.tiktok_max_hashtags)
    who = next((ln.split(": ", 1)[1] for ln in desc.splitlines() if ln.startswith("🎮 Streamer : ")), "")
    title = clip.title.strip()
    if not hooks.emojis_in(title):
        emo = hooks.emojis_in(clip_data(clip).get("hook", ""))[:1]
        title = f"{title} {emo[0]}" if emo else hooks.add_emojis(title)
    picked = tiktok_tags(tags, s.tiktok_max_hashtags)
    cap = title + "".join(f" #{t}" for t in picked)
    if who:
        cap += f"\n\nCrédit : {who}"
    return cap


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


def _in_ranges(ranges: list[tuple[int, int]], t: datetime) -> bool:
    m = t.hour * 60 + t.minute
    return any((a <= m < b) if a <= b else (m >= a or m < b) for a, b in ranges)


def in_publish_window(at: datetime | None = None) -> bool:
    cfg = get_settings()
    ranges = parse_hours(platforms.publish_hours())
    if not ranges:
        return True
    return _in_ranges(ranges, at or datetime.now(ZoneInfo(cfg.timezone)))


def in_quiet_hours(at: datetime | None = None) -> bool:
    """Heures creuses (QUIET_HOURS, la nuit) : aucun envoi, même en mode GTA 6 (personne pour regarder)."""
    cfg = get_settings()
    ranges = parse_hours(cfg.quiet_hours)
    return bool(ranges) and _in_ranges(ranges, at or datetime.now(ZoneInfo(cfg.timezone)))


def open_at(t: datetime) -> bool:
    """Un envoi est-il permis à cette heure (heures d'envoi ou mode GTA 6, hors heures creuses) ?"""
    return (anytime() or in_publish_window(t)) and not in_quiet_hours(t)


def post_limit(platform: str = "youtube") -> int:
    """Envois max par jour (depuis minuit), 0 = pas de limite. Réglage de l'interface, sinon le .env."""
    cfg = get_settings()
    if gta6.active() and cfg.gta6_max_posts_per_day > 0:
        return _capped(cfg.gta6_max_posts_per_day)
    ui = platforms.daily_limit(platform)
    limit = ui if ui is not None else max(0, cfg.max_posts_per_day)
    return _capped(limit)


def _capped(limit: int) -> int:
    """DAILY_CAP l'emporte : au-delà, chaque Short est moins poussé (la chaîne semble spammer)."""
    cap = max(0, get_settings().daily_cap)
    if not cap:
        return limit
    return min(limit, cap) if limit else cap


def quota_reset() -> datetime:
    """Prochain minuit, heure du Pacifique (remise à zéro du quota YouTube), en UTC sans fuseau."""
    pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    nxt = (pt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return nxt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def post_gap() -> int:
    """Écart entre deux envois : celui réglé dans l'interface prime, sinon GTA6_MIN_MINUTES_BETWEEN_POSTS
    en mode GTA 6, sinon MIN_MINUTES_BETWEEN_POSTS."""
    ui = platforms.ui_gap()
    if ui is not None:
        return ui
    return get_settings().gta6_min_minutes_between_posts if gta6.active() else platforms.min_gap()


def base_gap() -> int:
    """Écart choisi (interface / .env), relevé à POST_GAP_FLOOR : publier toutes les 2 min fait chuter
    les vues de chaque Short (testés auprès des mêmes abonnés, ils se cannibalisent)."""
    return max(post_gap(), max(0, get_settings().post_gap_floor))


def open_minutes(start: datetime, end: datetime) -> int:
    """Minutes où l'envoi est permis entre start et end (pas d'une minute, au plus 24 h)."""
    n, t = 0, start
    while t < end and n < 1440:
        if open_at(t):
            n += 1
        t += timedelta(minutes=1)
    return n


def effective_gap(s, platform: str) -> int:
    """Écart réellement appliqué : base_gap, et si SPREAD_POSTS les envois restants du jour sont répartis
    sur les heures d'envoi qui restent d'ici minuit (au lieu de tout publier d'un coup en début de journée)."""
    gap = base_gap()
    if not get_settings().spread_posts:
        return gap
    limit = post_limit(platform)
    if not limit:
        return gap
    left = limit - used_today(s, platform)
    if left <= 0:
        return gap
    tz = ZoneInfo(get_settings().timezone)
    t = datetime.now(tz).replace(second=0, microsecond=0)
    midnight = (t + timedelta(days=1)).replace(hour=0, minute=0)
    mins = open_minutes(t, midnight)
    return max(gap, min(180, mins // left)) if mins else gap


def anytime() -> bool:
    return gta6.active() and get_settings().gta6_publish_anytime  # la hype est courte : on publie à toute heure


def last_published(s, platform: str) -> datetime | None:
    return s.exec(select(Post.published_at).where(Post.platform == platform, Post.status == "published")
                  .order_by(col(Post.published_at).desc())).first()


def day_start() -> datetime:
    """Minuit du jour (heure locale), en UTC sans fuseau comme published_at."""
    tz = ZoneInfo(get_settings().timezone)
    local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def used_today(s, platform: str) -> int:
    """Envois automatiques depuis minuit (heure locale) : une rafale de la veille ne bloque plus la journée suivante."""
    return len(s.exec(select(Post.id).where(Post.platform == platform, Post.status == "published",
                                            Post.published_at >= day_start())).all())


def can_post(s, platform: str) -> bool:
    if platforms.blocked_until(platform):
        return False
    if (not anytime() and not in_publish_window()) or in_quiet_hours():
        return False
    last = last_published(s, platform)
    if last and now() - last < timedelta(minutes=effective_gap(s, platform)):
        return False
    limit = post_limit(platform)
    return not limit or used_today(s, platform) < limit


def slots_left(s, platform: str) -> int:
    """Envois encore possibles d'ici minuit (quota, écart minimum entre deux envois, heures de publication)."""
    if platforms.blocked_until(platform):
        return 0
    limit = post_limit(platform)
    left = limit - used_today(s, platform) if limit else 10_000
    if left <= 0:
        return 0
    tz = ZoneInfo(get_settings().timezone)
    t = datetime.now(tz).replace(second=0, microsecond=0)
    midnight = (t + timedelta(days=1)).replace(hour=0, minute=0)
    last = last_published(s, platform)
    if last:  # published_at est en UTC sans fuseau
        nxt = last.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz) + timedelta(minutes=base_gap())
        t = max(t, nxt)
    n, gap = 0, timedelta(minutes=max(1, base_gap()))
    while t < midnight and n < left:
        if open_at(t):
            n += 1
            t += gap
        else:
            t += timedelta(minutes=1)
    return n


def rank(clip: Clip, video: Video, at: datetime | None = None, boosts: dict | None = None) -> float:
    """Valeur d'un clip pour la file : son score × le poids de son format (celui qui fait le plus de vues passe
    devant), et en mode GTA 6 la fraîcheur compte (la hype ne dure pas)."""
    kind = growth.kind_of_key(video.key)
    boost = (boosts.get(kind, 1.0) if boosts is not None else growth.boost(kind))
    if not gta6.active():
        return clip.score * boost
    age_h = max(0.0, ((at or now()) - clip.created_at).total_seconds() / 3600)
    half = get_settings().gta6_fresh_hours
    fresh = 0.5 ** (age_h / half) if half > 0 else 1.0
    return clip.score * boost * fresh * (1.0 if gta6.is_gta6(video.title) else 0.5)


WEAK_BOOST = growth.WEAK_BOOST


def tier(post: Post, video: Video, boosts: dict) -> int:
    """0 = part en priorité ; 1 = réserve (clip juste sous le seuil) ou format qui ne marche pas : seulement quand
    il ne reste rien de mieux."""
    if curate.is_reserve(post):
        return 1
    return 1 if boosts.get(growth.kind_of_key(video.key), 1.0) < WEAK_BOOST else 0


def queue(s, platform: str) -> list[tuple[Post, Clip]]:
    """Clips en attente dans l'ordre d'envoi : les suites d'un clip d'abord, puis les mieux classés
    (la réserve et les formats qui ne marchent pas en dernier)."""
    rows = s.exec(select(Post, Clip, Video).join(Clip, Clip.id == Post.clip_id).join(Video, Video.id == Clip.video_id)
                  .where(Post.platform == platform, Post.status == "pending", Clip.status == "ready")).all()
    at = now()
    boosts = growth.queue_boosts()
    rows = sorted(rows, key=lambda r: (r[1].part <= 1, tier(r[0], r[2], boosts), -rank(r[1], r[2], at, boosts),
                                       -r[1].id))
    return [(post, clip) for post, clip, _ in rows]


def next_post(s, platform: str) -> Post | None:
    """Prochain envoi : les parties suivantes d'un clip passent en priorité, dans l'ordre."""
    for post, clip in queue(s, platform):
        if clip.part > 1:
            prev = s.exec(select(Post).join(Clip, Clip.id == Post.clip_id).where(
                Clip.group == clip.group, Clip.part == clip.part - 1, Post.platform == platform)).first()
            if prev and prev.status not in ("published", "manual"):
                continue
        return post
    return None


def plan(s) -> dict[str, dict]:
    """Par plateforme : envois utilisés / restants aujourd'hui, clips qui partiront aujourd'hui (rang)
    et clips en trop, à poster à la main."""
    out = {}
    for platform in ("youtube", "tiktok"):
        q = queue(s, platform)
        on = platforms.enabled(platform) and connected(platform)
        left = slots_left(s, platform) if on else 0
        until = platforms.blocked_until(platform)
        out[platform] = {"on": on, "used": used_today(s, platform), "limit": post_limit(platform), "left": left,
                         "ui_limit": platforms.daily_limit(platform),
                         "blocked": until.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                             ZoneInfo(get_settings().timezone)).strftime("%d/%m %H:%M") if until else "",
                         "today": {p.id: i + 1 for i, (p, _) in enumerate(q[:left])},
                         "manual": [p.id for p, _ in q[left:]], "pending": len(q)}
    return out


def connected(platform: str) -> bool:
    if platform == "youtube":
        return youtube.configured() and creds.load("youtube") is not None and not youtube.auth_error()
    return tiktok.configured() and creds.load("tiktok") is not None


AUTH_EXPIRED_MSG = ("🔑 YouTube déconnecté : Google a refusé le jeton (expiré ou révoqué). Les envois sont en pause, "
                    "rien n'est perdu. Reconnecte la chaîne dans Comptes → « Connecter ma chaîne YouTube ». "
                    "Pour que ça ne se reproduise pas tous les 7 jours : Google Cloud → Écran de consentement OAuth "
                    "→ « Publier l'application » (mode Production).")


def requeue_auth_failures() -> int:
    """Après une reconnexion YouTube : les envois abandonnés à cause du jeton expiré repartent en file
    (le tri de la file retire ensuite ceux qui sont trop vieux)."""
    n = 0
    with db.session() as s:
        for p in s.exec(select(Post).where(Post.platform == "youtube", Post.status == "failed")).all():
            if "invalid_grant" in (p.error or ""):
                p.status, p.error, p.attempts = "pending", "", 0
                s.add(p)
                n += 1
        s.commit()
    return n


def publish_once(platform: str) -> bool:
    if not platforms.enabled(platform):  # coupée dans l'interface : les clips restent en attente
        return False
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
    quota_hit = False
    try:
        if not Path(clip.path).exists():
            raise RuntimeError("Fichier du clip introuvable")
        remote = PUBLISHERS[platform](clip, desc, tags)
        status, err = "published", ""
        log.info("Publié sur %s : %s", platform, clip.title)
    except youtube.QuotaExceeded as e:
        # quota du jour atteint : rien n'est perdu, les clips restent en file jusqu'à la remise à zéro
        until = quota_reset()
        platforms.block_until(platform, until)
        remote, err, status, quota_hit = "", "", "pending", True
        with db.session() as s:
            waiting = len(queue(s, platform))
        local = until.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(get_settings().timezone))
        log.warning("Quota %s atteint, reprise le %s UTC : %s", platform, until, e)
        notify.send(f"⏸ Quota {platforms.LABELS[platform]} atteint : envois en pause jusqu'au "
                    f"{local:%d/%m à %H:%M}. {waiting + 1} clip(s) en file, à poster à la main si besoin.")
    except youtube.AuthExpired as e:
        # rien n'est perdu : le clip reste en file et repartira dès que la chaîne sera reconnectée
        remote, err, status, quota_hit = "", "", "pending", True
        log.error("Publication YouTube en pause : connexion expirée ou révoquée (%s)", e)
        notify.send(AUTH_EXPIRED_MSG)
    except Exception as e:
        log.exception("Publication %s échouée", platform)
        remote, err = "", str(e)[:1000]
        status = "failed" if post.attempts >= 3 else "pending"
        if status == "failed":
            notify.error(f"Publication {platform} abandonnée après 3 essais : {clip.title}", err)
    with db.session() as s:
        post = s.get(Post, post.id)
        post.status, post.remote_id, post.error = status, remote, err
        if quota_hit:
            post.attempts = max(0, post.attempts - 1)  # ne compte pas comme un essai raté
        if status == "published":
            post.published_at = now()
        s.add(post)
        s.commit()
    if status == "published":
        notify.published(platform, clip.title, remote)
    return status == "published"


def publish_loop():
    last_curate = 0.0
    while not stop.is_set():
        if time.monotonic() - last_curate >= 600:  # tri de la file : mauvais clips retirés, titres corrigés
            last_curate = time.monotonic()
            try:
                curate.run()
            except Exception:
                log.exception("Tri de la file d'envoi échoué")
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


def sponsor_purge():
    """Une fois au démarrage : retire de la file les clips déjà rendus tombés dans une pub."""
    stop.wait(30)
    try:
        n = sponsor.purge_queue(should_stop=stop.is_set)
        if n:
            log.info("%d envoi(s) annulé(s) : passages sponsorisés", n)
    except Exception:
        log.exception("Vérification des passages sponsorisés échouée")


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
    news.reset_interrupted()
    compil.reset_interrupted()


def news_loop():
    news.loop(stop)


def compil_loop():
    compil.loop(stop)


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


def supervised(f, retry_s: float = 30):
    """Relance la tâche si elle s'arrête sur une erreur imprévue (une fin normale, à l'arrêt, n'est pas relancée)."""
    def run():
        while not stop.is_set():
            try:
                f()
                return
            except Exception:
                log.exception("Tâche %s plantée, relance dans %d s", f.__name__, retry_s)
                stop.wait(retry_s)
    run.__name__ = f.__name__
    return run


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import logs
    logs.setup("worker")
    db.get_engine()
    reset_interrupted()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    crashed = health.started("worker")
    threads = [threading.Thread(target=supervised(f), daemon=True, name=f.__name__)
               for f in (poll_loop, record_loop, download_loop, process_loop, publish_loop, stats_loop, maintenance_loop,
                         discover_loop, news_loop, compil_loop)]
    for t in threads:
        t.start()
    threading.Thread(target=sponsor_purge, daemon=True, name="sponsor_purge").start()
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
