"""Histoires vraies envoûtantes (secrets, mystères, affaires étranges) racontées en vidéo de 60-90 s.

Les sujets viennent d'une liste (intégrée + STORY_TOPICS + fichier data/stories.txt, un sujet par ligne) et
les faits de Wikipédia (français, sinon anglais) : l'IA ne raconte que ce qui est dans l'article, et la
source est citée dans la description. Une histoire déjà faite n'est jamais refaite."""
from __future__ import annotations

import hashlib
import logging
import re

import httpx

from ..config import get_settings
from . import feeds

log = logging.getLogger(__name__)
UA = "ClipNews/1.0 (self-hosted video tool)"

# Mystères et secrets documentés, connus du grand public (pas de fait divers récent visant des particuliers).
SEEDS = [
    "Room 641A", "Affaire du col Dyatlov", "Mary Celeste", "Signal Wow!", "Affaire Tamam Shud", "D. B. Cooper",
    "Opération Mincemeat", "Projet MK-Ultra", "Station de nombres", "Manuscrit de Voynich",
    "Incident Max Headroom", "Événement de la Toungouska", "Meurtres de Hinterkaifeck", "Cicada 3301",
    "Opération Northwoods", "Bête du Gévaudan", "Homme au masque de fer", "Rennes-le-Château", "Vol 19",
    "Fantôme de Heilbronn", "Opération Paperclip", "Projet Azorian", "Kryptos", "Île du Chêne",
    "Colonie de Roanoke", "Île de Sentinelle du Nord", "Forage superprofond de Kola", "Duga (radar)",
    "UVB-76", "Opération Gladio", "Affaire de Roswell", "Echelon", "Stuxnet", "Bloop",
    "Machine d'Anticythère", "Expédition Franklin", "Georgia Guidestones", "Projet Greek Island",
    "Codex Seraphinianus", "Inscription de Shugborough", "Trésor des Templiers", "Opération Ivy Bells",
    "Opération Acoustic Kitty", "Projet Stargate", "Bunker de Raven Rock", "Île de Poveglia",
    "Le Taos Hum", "Disparition de Jim Thompson", "Lettre du Zodiaque", "Mine de Centralia",
]

_END = re.compile(r"^\s*(notes et références|références|voir aussi|liens externes|bibliographie|notes|"
                  r"see also|references|external links|further reading|annexes)\s*$", re.I | re.M)


def key(topic: str) -> str:
    return "news:story-" + hashlib.sha1(topic.strip().lower().encode()).hexdigest()[:14]


def topics() -> list[str]:
    """Sujets dans l'ordre : ceux de l'utilisateur (fichier puis réglage) d'abord, puis la liste intégrée."""
    s = get_settings()
    out: list[str] = []
    f = s.data_dir / "stories.txt"
    if f.exists():
        out += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    out += feeds.split_list(s.story_topics)
    if s.story_builtin:
        out += SEEDS
    return list(dict.fromkeys(out))


def clean_extract(text: str, max_chars: int = 6000) -> str:
    """Texte de l'article sans les annexes (références, liens…), sans lignes vides en trop."""
    m = _END.search(text or "")
    if m:
        text = text[:m.start()]
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:max_chars]


def wiki(topic: str, lang: str = "fr") -> dict | None:
    """{title, url, text, lat, lon} de l'article Wikipédia le plus proche du sujet, ou None."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        r = httpx.get(api, headers={"User-Agent": UA}, timeout=20, params={
            "action": "query", "format": "json", "list": "search", "srsearch": topic, "srlimit": 1})
        r.raise_for_status()
        hits = (r.json().get("query") or {}).get("search") or []
        if not hits:
            return None
        r = httpx.get(api, headers={"User-Agent": UA}, timeout=20, params={
            "action": "query", "format": "json", "prop": "extracts|coordinates|info", "explaintext": 1,
            "exsectionformat": "plain", "inprop": "url", "redirects": 1, "titles": hits[0]["title"]})
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as e:
        log.info("Wikipédia (%s) indisponible pour « %s » : %s", lang, topic, e)
        return None
    for page in pages.values():
        text = clean_extract(page.get("extract") or "")
        if not text:
            continue
        coords = (page.get("coordinates") or [{}])[0]
        return {"title": page.get("title") or topic, "url": page.get("fullurl") or "", "text": text,
                "lat": coords.get("lat"), "lon": coords.get("lon"), "lang": lang}
    return None


def fetch(topic: str) -> feeds.Item | None:
    """Faits de l'histoire : article français, ou anglais s'il est bien plus fourni (l'IA traduit). Le titre reste
celui de l'article français, sinon le sujet tel qu'écrit dans la liste (en français)."""
    fr = wiki(topic, "fr")
    title = fr["title"] if fr else topic  # titre toujours en français (le sujet), même si les faits sont anglais
    if fr is None or len(fr["text"]) < 2500:
        en = wiki(topic, "en")
        if en and len(en["text"]) > (len(fr["text"]) if fr else 0) * 1.5:
            fr = en
    if fr is None or len(fr["text"]) < 800:
        log.info("Histoire : pas assez de faits sur « %s »", topic)
        return None
    source = "Wikipédia" if fr["lang"] == "fr" else "Wikipedia (en)"
    return feeds.Item(title=title, link=fr["url"], summary=fr["text"], source=source, kind="story",
                      topic=topic)


def done_keys(keys: list[str]) -> set[str]:
    from sqlmodel import col, select

    from .. import db
    from ..models import Video

    if not keys:
        return set()
    with db.session() as s:
        return set(s.exec(select(Video.key).where(col(Video.key).in_(keys))).all())


def left() -> int:
    """Sujets pas encore racontés."""
    todo = topics()
    seen = done_keys([key(t) for t in todo])
    return sum(1 for t in todo if key(t) not in seen)


def candidates(limit: int = 3) -> list[feeds.Item]:
    """Prochaines histoires pas encore faites (on ne cherche les faits que pour les premières)."""
    todo = topics()
    seen = done_keys([key(t) for t in todo])
    out = []
    for t in todo:
        if key(t) in seen:
            continue
        item = fetch(t)
        if item:
            out.append(item)
            if len(out) >= limit:
                break
    return out
