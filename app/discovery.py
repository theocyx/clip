"""Découverte automatique des streamers français qui font du GTA (Twitch par catégorie, YouTube en live).

Les chaînes trouvées sont ajoutées dans « Sources » (marquées « auto »), avec le filtre de titre GTA
et sans les rediffusions, pour ménager le CPU. Le nombre de chaînes auto est plafonné ; celles qui ne
font plus de GTA depuis DISCOVER_EXPIRE_DAYS jours sont retirées ; celles que tu supprimes ne sont
plus jamais rajoutées."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from sqlmodel import col, select

from . import db, gta6, notify
from .config import get_settings
from .models import DiscoverIgnore, Source, Video, now
from .pipeline import sources

log = logging.getLogger(__name__)

# Lives triés par spectateurs, langue du streamer = français
TWITCH_QUERY = ("query($g:String!,$n:Int,$l:[Language!]){game(name:$g){name streams(first:$n,"
                "options:{broadcasterLanguages:$l,sort:VIEWER_COUNT}){edges{node{id title viewersCount "
                "broadcaster{login displayName}}}}}}")
YT_LIVE_FILTER = "EgJAAQ%3D%3D"  # filtre « En direct » de la recherche YouTube
FR_WORDS = {"le", "la", "les", "des", "du", "un", "une", "et", "avec", "sur", "pour", "en", "on", "je", "tu",
            "nous", "vous", "ce", "cette", "mon", "ma", "mes", "ton", "pas", "est", "c'est", "jour", "nuit",
            "ville", "premier", "première", "découverte", "soirée", "ça", "qui", "quoi", "fr", "français"}
ACTIVE = ["new", "recording", "downloading", "downloaded", "transcribing", "analyzing", "rendering"]


def _file(name: str) -> Path:
    return get_settings().data_dir / name


def enabled() -> bool:
    """Choix fait dans l'interface (fichier data/discover_off), sinon DISCOVER du .env."""
    return get_settings().discover and not _file("discover_off").exists()


def set_enabled(on: bool) -> None:
    if on:
        _file("discover_off").unlink(missing_ok=True)
    else:
        _file("discover_off").write_text("off")


def looks_french(title: str) -> bool:
    t = (title or "").lower()
    if re.search(r"[éèêàçùûôî]", t) or re.search(r"\[fr\]|#fr\b|\bfr\b", t):
        return True
    words = set(re.findall(r"[a-zç'’]+", t.replace("’", "'")))
    return len(words & FR_WORDS) >= 2


def key_of(url: str) -> str:
    """Clé de comparaison d'une chaîne (pour savoir si elle est déjà suivie ou ignorée)."""
    try:
        return sources.normalize_channel_url(url).lower()
    except ValueError:
        return url.strip().rstrip("/").lower()


# ---------- Recherche ----------

def twitch_candidates(game: str, first: int = 30) -> list[dict] | None:
    """Lives FR d'une catégorie Twitch, triés par spectateurs. None si Twitch ne répond pas."""
    try:
        with httpx.Client(timeout=15, headers={"Client-Id": gta6.TWITCH_CLIENT_ID}) as http:
            r = http.post(gta6.TWITCH_GQL, json={"query": TWITCH_QUERY,
                                                 "variables": {"g": game, "n": first, "l": ["FR"]}})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Découverte Twitch (%s) impossible : %s", game, e)
        return None
    if data.get("errors") or "data" not in data:
        log.warning("Découverte Twitch (%s) refusée : %s", game, str(data.get("errors"))[:300])
        return None
    out = []
    for edge in (((data["data"].get("game") or {}).get("streams") or {}).get("edges") or []):
        n = (edge or {}).get("node") or {}
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        out.append({"name": b.get("displayName") or b["login"], "platform": "twitch",
                    "url": f"https://www.twitch.tv/{b['login'].lower()}", "title": n.get("title") or "",
                    "viewers": int(n.get("viewersCount") or 0), "game": game})
    return out


def youtube_candidates(query: str, limit: int = 20) -> list[dict]:
    """Lives YouTube trouvés par la recherche, dont le titre parle de GTA et semble en français."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={YT_LIVE_FILTER}"
    try:
        entries = sources._list_entries(url, limit)
    except Exception as e:
        log.warning("Découverte YouTube (%s) impossible : %s", query, e)
        return []
    out = []
    for e in entries:
        title = e.get("title") or ""
        chan = e.get("channel_url") or e.get("uploader_url") or (
            f"https://www.youtube.com/channel/{e['channel_id']}" if e.get("channel_id") else "")
        if not chan or not sources.matches_keywords(title, "GTA") or not looks_french(title):
            continue
        if e.get("live_status") not in (None, "is_live"):
            continue
        out.append({"name": e.get("channel") or e.get("uploader") or chan.rsplit("/", 1)[-1],
                    "platform": "youtube", "url": chan, "title": title,
                    "viewers": int(e.get("concurrent_view_count") or e.get("view_count") or 0), "game": "YouTube"})
    return out


def candidates() -> list[dict]:
    cfg = get_settings()
    found: list[dict] = []
    for game in [g.strip() for g in cfg.discover_games.split(",") if g.strip()]:
        found += twitch_candidates(game) or []
    if cfg.discover_youtube:
        for q in [q.strip() for q in cfg.discover_youtube_queries.split(",") if q.strip()]:
            found += youtube_candidates(q)
    best: dict[str, dict] = {}
    for c in found:  # une chaîne trouvée deux fois : on garde le plus de spectateurs
        k = key_of(c["url"])
        if k not in best or c["viewers"] > best[k]["viewers"]:
            best[k] = c
    # GTA 6 d'abord, puis le plus de spectateurs
    return sorted(best.values(), key=lambda c: (not gta6.is_gta6(c["title"], c["game"]), -c["viewers"]))


# ---------- Ajout, suivi, retrait ----------

def ignore(url: str) -> None:
    with db.session() as s:
        k = key_of(url)
        if not s.get(DiscoverIgnore, k):
            s.add(DiscoverIgnore(url=k))
            s.commit()


def prune(s) -> list[str]:
    """Retire les chaînes auto qui n'ont plus fait de GTA en live depuis N jours (sauf traitement en cours)."""
    days = get_settings().discover_expire_days
    if days <= 0:
        return []
    limit = now() - timedelta(days=days)
    removed = []
    for src in s.exec(select(Source).where(Source.auto == True)).all():  # noqa: E712
        if (src.last_seen or src.created_at) >= limit:
            continue
        busy = s.exec(select(Video).where(Video.source_id == src.id, col(Video.status).in_(ACTIVE))).first()
        if busy:
            continue
        for v in s.exec(select(Video).where(Video.source_id == src.id)).all():
            v.source_id = None
            s.add(v)
        s.delete(src)
        removed.append(src.name)
    return removed


def run(found: list[dict] | None = None) -> dict:
    """Une recherche : met à jour les chaînes auto déjà suivies, en ajoute de nouvelles dans la limite."""
    cfg = get_settings()
    found = candidates() if found is None else found
    added, seen = [], 0
    with db.session() as s:
        followed = {key_of(x.url): x for x in s.exec(select(Source)).all()}
        ignored = {x.url for x in s.exec(select(DiscoverIgnore)).all()}
        for c in found:
            src = followed.get(key_of(c["url"]))
            if src is not None and src.auto:
                src.last_seen = now()
                s.add(src)
                seen += 1
        removed = prune(s)
        n_auto = len(s.exec(select(Source).where(Source.auto == True)).all())  # noqa: E712
        room = min(cfg.discover_per_run, max(0, cfg.discover_max_sources - n_auto))
        for c in found:
            if len(added) >= room:
                break
            k = key_of(c["url"])
            if k in followed or k in ignored or c["viewers"] < cfg.discover_min_viewers:
                continue
            src = Source(name=c["name"][:80], platform=c["platform"], url=sources.normalize_channel_url(c["url"]),
                         record_lives=True, include_videos=False, keywords="GTA", auto=True, last_seen=now())
            s.add(src)
            followed[k] = src
            added.append(c)
        s.commit()
    for c in added:
        notify.send(f"🆕 Streamer découvert : {c['name']} ({c['viewers']} spectateurs, {c['game']}), "
                    f"ajouté automatiquement aux sources.\n{c['url']}")
    msg = (f"{len(found)} live(s) GTA FR trouvés, {len(added)} chaîne(s) ajoutée(s)"
           + (f" ({', '.join(c['name'] for c in added)})" if added else "")
           + (f", {len(removed)} retirée(s) car inactives ({', '.join(removed)})" if removed else "")
           + (" — plafond atteint" if room == 0 and cfg.discover_max_sources else ""))
    status = {"at": time.time(), "message": msg, "found": len(found), "added": [c["name"] for c in added],
              "removed": removed, "seen": seen}
    try:
        _file("discover_status.json").write_text(json.dumps(status))
    except OSError:
        pass
    log.info("Découverte : %s", msg)
    return status


def last_status() -> dict | None:
    try:
        return json.loads(_file("discover_status.json").read_text())
    except (OSError, ValueError):
        return None


def interval() -> float:
    cfg = get_settings()
    minutes = cfg.discover_interval_minutes
    if gta6.active():
        minutes = min(minutes, 10)  # le jour de la sortie, les nouveaux arrivent vite
    return max(5, minutes) * 60


def loop(stop) -> None:
    stop.wait(180)
    while not stop.is_set():
        if enabled():
            try:
                run()
            except Exception:
                log.exception("Découverte des streamers échouée")
        stop.wait(interval())
