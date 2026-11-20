"""Sujets d'actu : lecture des flux RSS / Atom, filtrage, et classement par « buzz »
(un sujet repris par plusieurs médias en même temps passe devant)."""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from ..pipeline import engage

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Item:
    title: str
    link: str
    summary: str = ""
    source: str = ""
    published: datetime | None = None  # UTC, sans fuseau
    buzz: int = 0  # nombre d'autres articles sur le même sujet
    related: list[str] = field(default_factory=list)
    kind: str = "news"  # news (actu du jour) ou story (histoire vraie, voir stories.py)
    topic: str = ""  # sujet demandé pour une histoire

    @property
    def key(self) -> str:
        if self.kind == "story":
            from .stories import key
            return key(self.topic or self.title)
        return "news:" + hashlib.sha1(self.link.encode()).hexdigest()[:16]


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", html.unescape(text or "")))).strip()


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, *names: str) -> str:
    for n in names:
        c = _child(el, n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def site_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host.split(".")[0].capitalize() if host else ""


def parse(xml: str | bytes, feed_url: str = "") -> list[Item]:
    """Articles d'un flux RSS 2.0 ou Atom."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("Flux illisible %s : %s", feed_url, e)
        return []
    channel = _child(root, "channel")
    feed_title = strip_html(_text(channel if channel is not None else root, "title"))
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        title = strip_html(_text(e, "title"))
        link = _text(e, "link")
        if not link:
            ln = _child(e, "link")
            link = ln.get("href", "") if ln is not None else ""
        if not title or not link:
            continue
        summary = strip_html(_text(e, "description", "summary", "content", "encoded"))
        src_el = _child(e, "source")
        source = strip_html(src_el.text) if src_el is not None and src_el.text else ""
        # Google News : « Titre - Média »
        if "news.google." in feed_url and " - " in title:
            title, _, source = title.rpartition(" - ")
            if summary.startswith(title):
                summary = ""
        out.append(Item(title=title.strip(), link=link.strip(), summary=summary[:1500],
                        source=source or feed_title or site_name(link),
                        published=_date(_text(e, "pubdate", "published", "updated", "date"))))
    return out


def fetch(url: str) -> list[Item]:
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        return parse(r.content, url)
    except Exception as e:
        log.warning("Flux %s inaccessible : %s", url, e)
        return []


def split_list(spec: str) -> list[str]:
    return [x.strip() for x in (spec or "").split(",") if x.strip()]


def matches(item: Item, keywords: list[str], exclude: list[str]) -> bool:
    hay = f"{item.title} {item.summary}".lower()
    if any(x.lower() in hay for x in exclude):
        return False
    return not keywords or any(k.lower() in hay for k in keywords)


def rank(items: list[Item], max_age_hours: float, at: datetime | None = None) -> list[Item]:
    """Articles récents, regroupés par sujet (le plus récent de chaque sujet gardé), du plus repris au moins repris."""
    at = at or datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = [i for i in items if i.published is None or at - i.published <= timedelta(hours=max_age_hours)]
    fresh.sort(key=lambda i: i.published or at - timedelta(hours=max_age_hours), reverse=True)
    groups: list[list[Item]] = []
    for it in fresh:
        for g in groups:
            if engage.similarity(it.title, g[0].title) >= 0.55:
                g.append(it)
                break
        else:
            groups.append([it])
    out = []
    for g in groups:
        best = max(g, key=lambda i: len(i.summary))  # l'article le plus détaillé sert de base
        best.buzz = len({i.source for i in g}) - 1
        best.related = [i.title for i in g if i is not best][:4]
        out.append(best)

    def score(i: Item) -> float:
        age = (at - i.published).total_seconds() / 3600 if i.published else max_age_hours / 2
        return i.buzz * 2 + max(0.0, 1 - age / max(1.0, max_age_hours)) * 3 + min(len(i.summary), 400) / 400

    return sorted(out, key=score, reverse=True)


def candidates(feeds: list[str], keywords: list[str], exclude: list[str], max_age_hours: float) -> list[Item]:
    items = [i for url in feeds for i in fetch(url)]
    return rank([i for i in items if matches(i, keywords, exclude)], max_age_hours)


# ---------- texte de l'article ----------

_META = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*>', re.I)
_CONTENT = re.compile(r'content=["\']([^"\']+)["\']', re.I)
_P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_DROP = re.compile(r"<(script|style|nav|footer|header|aside|form)[^>]*>.*?</\1>", re.I | re.S)


def article_text(page: str, limit: int = 3500) -> str:
    """Description + paragraphs principaux d'une page d'article (sert uniquement de base factuelle à l'IA)."""
    page = _DROP.sub(" ", page or "")
    parts = []
    m = _META.search(page)
    if m:
        c = _CONTENT.search(m.group(0))
        if c:
            parts.append(strip_html(c.group(1)))
    for p in _P.findall(page):
        t = strip_html(p)
        if len(t) >= 60 and not re.search(r"cookie|abonnez|newsletter|©|tous droits", t, re.I):
            parts.append(t)
    out, seen = [], set()
    for t in parts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return "\n".join(out)[:limit]


def fetch_article(item: Item) -> str:
    """Faits de l'article : sa page si elle est lisible, sinon le résumé du flux (histoire : texte Wikipédia)."""
    if item.kind == "story":
        return item.summary.strip()
    text = ""
    if "news.google." not in item.link:
        try:
            r = httpx.get(item.link, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
            if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                text = article_text(r.text)
        except Exception as e:
            log.info("Article illisible (%s) : %s", item.link, e)
    if len(text) < len(item.summary):
        text = item.summary
    if item.related:
        text += "\nAutres titres sur le sujet : " + " / ".join(item.related)
    return text.strip()
