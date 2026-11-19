"""Mode « GTA 6 day one » : le jour de la sortie, détection rapide des lives GTA 6, enregistrement
prioritaire des plus gros streamers (les autres attendent leur tour), live découpé en morceaux pour
sortir les premiers clips pendant que le streamer joue encore, et publication sans attendre."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import get_settings
from .pipeline.sources import Found

log = logging.getLogger(__name__)

MODES = ("auto", "on", "off")
# Client public du site Twitch (le même que yt-dlp) : une seule requête pour savoir qui est en live, sur quel jeu
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GAME_RE = re.compile(r"grand theft auto (vi|6)\b|\bgta ?(vi|6)\b", re.I)

viewers: dict[int, int] = {}  # source.id → spectateurs du live en cours (relevé Twitch)
games: dict[int, str] = {}  # source.id → catégorie Twitch du live en cours ("" = inconnue)


# ---------- Activation ----------

def _override_file() -> Path:
    return get_settings().data_dir / "gta6_mode"


def mode() -> str:
    """Choix fait dans l'interface (auto / on / off), sinon GTA6_MODE du .env."""
    try:
        m = _override_file().read_text().strip()
        if m in MODES:
            return m
    except OSError:
        pass
    m = get_settings().gta6_mode.strip().lower()
    return m if m in MODES else "auto"


def set_mode(m: str) -> None:
    if m not in MODES:
        raise ValueError(m)
    _override_file().write_text(m)


def window() -> tuple[datetime, datetime]:
    """Période où le mode automatique est actif (heure de Paris) : du jour de sortie à minuit + N jours."""
    cfg = get_settings()
    tz = ZoneInfo(cfg.timezone)
    d = date.fromisoformat(cfg.gta6_date)
    start = datetime(d.year, d.month, d.day, tzinfo=tz) - timedelta(hours=cfg.gta6_hours_before)
    end = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=max(1, cfg.gta6_days))
    return start, end


def active(at: datetime | None = None) -> bool:
    m = mode()
    if m != "auto":
        return m == "on"
    try:
        start, end = window()
    except ValueError:
        return False
    t = at or datetime.now(ZoneInfo(get_settings().timezone))
    return start <= t < end


# ---------- Détection ----------

def is_gta6(title: str, game: str = "") -> bool:
    if game and GAME_RE.search(game):
        return True
    t = (title or "").lower()
    words = [k.strip().lower() for k in get_settings().gta6_keywords.split(",") if k.strip()]
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words)


def live_matches(title: str, game: str = "") -> bool:
    """Un live est GTA 6 d'après sa catégorie Twitch ; le titre ne sert qu'en secours si la catégorie est inconnue
    (« TFT SET 19 | Minuit : GTA VI » en catégorie TFT n'est pas un live GTA 6)."""
    if game:
        return bool(GAME_RE.search(game))
    return is_gta6(title)


def off_category(source_id) -> str:
    """Catégorie Twitch actuelle de la source si elle est connue et n'est pas GTA 6, sinon ""."""
    g = games.get(source_id, "")
    return g if g and not GAME_RE.search(g) else ""


def twitch_login(url: str) -> str:
    m = re.search(r"twitch\.tv/([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def twitch_lives(logins: list[str]) -> dict[str, dict] | None:
    """{login: {id, title, game, viewers}} des chaînes en live (une requête par lot de 50).

    Les chaînes hors ligne sont absentes. None si Twitch ne répond pas (on repasse alors par yt-dlp)."""
    out: dict[str, dict] = {}
    try:
        with httpx.Client(timeout=15, headers={"Client-Id": TWITCH_CLIENT_ID}) as http:
            for i in range(0, len(logins), 50):
                batch = logins[i:i + 50]
                q = ("query($l:[String!]){users(logins:$l){login stream{id title viewersCount game{name}}}}")
                r = http.post(TWITCH_GQL, json={"query": q, "variables": {"l": batch}})
                r.raise_for_status()
                data = r.json()
                if "data" not in data or data.get("errors"):
                    raise RuntimeError(str(data.get("errors"))[:200])
                for u in data["data"].get("users") or []:
                    st = (u or {}).get("stream")
                    if st and st.get("id"):
                        out[u["login"].lower()] = {"id": st["id"], "title": st.get("title") or "",
                                                   "game": (st.get("game") or {}).get("name") or "",
                                                   "viewers": int(st.get("viewersCount") or 0)}
    except Exception as e:
        log.warning("Relevé des lives Twitch impossible (%s) : vérification classique", e)
        return None
    return out


def fast_lives(srcs) -> dict[int, list[Found]]:
    """Lives Twitch des sources, en une requête : {source.id: [Found]} (liste vide = hors ligne).

    Les sources absentes du résultat sont à vérifier normalement (YouTube, ou Twitch injoignable)."""
    tw = {s.id: twitch_login(s.url) for s in srcs if s.platform == "twitch" and s.record_lives}
    tw = {k: v for k, v in tw.items() if v}
    if not tw:
        return {}
    lives = twitch_lives(sorted(set(tw.values())))
    if lives is None:
        return {}
    out: dict[int, list[Found]] = {}
    for sid, login in tw.items():
        st = lives.get(login)
        if not st:
            viewers.pop(sid, None)
            games.pop(sid, None)
            out[sid] = []
            continue
        viewers[sid] = st["viewers"]
        games[sid] = st["game"]
        # le titre garde la catégorie pour que le filtre de mots-clés et les hashtags la voient
        title = st["title"]
        if is_gta6("", st["game"]) and not is_gta6(title):
            title = f"{title} [GTA 6]".strip()
        out[sid] = [Found(f"twitch-live:{st['id']}", f"https://www.twitch.tv/{login}", title, True, st["game"])]
    return out


# ---------- Priorités ----------

@lru_cache
def _catalog() -> dict[str, int]:
    """Rang de chaque chaîne dans le catalogue (rangé des plus suivis aux moins suivis)."""
    try:
        items = json.loads((Path(__file__).parent / "web" / "streamers_fr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {c["url"].lower().rstrip("/"): i for i, c in enumerate(items)}


def rank(src) -> int:
    """Plus petit = plus gros streamer. GTA6_PRIORITY passe en tête, puis l'ordre du catalogue."""
    if src is None:
        return 10_000
    pri = [p.strip().lower() for p in get_settings().gta6_priority.split(",") if p.strip()]
    keys = {src.name.lower(), src.url.lower().rstrip("/"), twitch_login(src.url)}
    for i, p in enumerate(pri):
        if p in keys or p.rstrip("/") in keys:
            return -1000 + i
    return _catalog().get(src.url.lower().rstrip("/"), 1000)


def live_priority(video, src) -> tuple:
    """Ordre d'enregistrement : GTA 6 d'abord, puis le plus de spectateurs, puis le plus gros streamer."""
    return (not is_gta6(video.title), -viewers.get(getattr(src, "id", None), 0), rank(src), video.id or 0)


def process_priority(video, src) -> tuple:
    """Ordre de traitement : GTA 6 d'abord, gros streamers d'abord, puis le plus récent (l'actualité d'abord)."""
    return (not is_gta6(video.title), rank(src), -(video.id or 0))


def base_key(key: str) -> str:
    return key.split("#", 1)[0]


def next_part_key(key: str) -> str:
    base, _, n = key.partition("#p")
    return f"{base}#p{int(n or 1) + 1}"


def status() -> dict:
    """Infos affichées dans l'interface."""
    cfg = get_settings()
    try:
        start, end = window()
        win = f"du {start:%d/%m %Hh} au {end:%d/%m %Hh}"
    except ValueError:
        win = "date GTA6_DATE invalide"
    return {"mode": mode(), "active": active(), "window": win, "max_rec": cfg.gta6_max_recordings,
            "chunk": cfg.gta6_chunk_minutes, "gap": cfg.gta6_min_minutes_between_posts,
            "anytime": cfg.gta6_publish_anytime}
