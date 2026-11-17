"""Hashtags tendance ajoutés selon le titre du live et ce qui se dit dans le clip."""
from __future__ import annotations

import re

from ..config import get_settings

# (expression régulière, hashtags) : cherchée dans le titre du live et le texte du clip
RULES: list[tuple[str, str]] = [
    (r"gta ?(6|vi|six)\b", "#GTA6 #GTAVI"),
    (r"gta ?rp|\brp\b|role ?play|\bwl\b|whitelist", "#GTARP #RP"),
    (r"gta ?(5|v|cinq)\b", "#GTA5"),
    (r"\bgta|grand theft auto", "#GTA #GTA6"),  # hype GTA 6 : sur tout le contenu GTA
]

# Serveurs GTA RP connus (FR) : nom (regex) → hashtag. Complétables avec RP_SERVERS dans .env.
RP_SERVERS: list[tuple[str, str]] = [
    (r"flash ?back", "#FlashBack"),
    (r"21 ?jump ?click", "#21JumpClick"),
    (r"faily ?v\b", "#FailyV"),
    (r"no ?pixel", "#NoPixel"),
]


def parse_rules(spec: str) -> list[tuple[str, str]]:
    """« mot1|mot2=#Tag1 #Tag2; Autre serveur=#Tag3 » → [(regex, tags), …]."""
    out = []
    for part in (spec or "").split(";"):
        if "=" in part:
            words, tags = part.split("=", 1)
            words = words.strip()
            if words and tags.strip():
                if not re.search(r"[|()\\\[\]?*+]", words):  # simple nom : espaces facultatifs
                    words = r"\s?".join(re.escape(w) for w in words.split())
                out.append((words, tags.strip()))
    return out


def split_tags(tags: str) -> list[str]:
    return [t.lstrip("#") for t in (tags or "").replace(",", " ").split() if t.strip("#")]


def trending(*texts: str) -> list[str]:
    """Hashtags (sans #) qui collent au contenu, serveurs RP en premier."""
    s = get_settings()
    if not s.trend_hashtags:
        return []
    hay = " ".join(t for t in texts if t).lower()
    out: list[str] = []
    for rules in (parse_rules(s.rp_servers) + RP_SERVERS, parse_rules(s.trend_rules) + RULES):
        for pattern, tags in rules:
            try:
                found = re.search(pattern, hay, flags=re.IGNORECASE)
            except re.error:
                continue
            if found:
                out += split_tags(tags)
    return out


def merge(*lists: list[str], limit: int = 0) -> list[str]:
    """Fusionne sans doublon (insensible à la casse), dans l'ordre, en gardant au plus `limit` tags."""
    seen, out = set(), []
    for lst in lists:
        for t in lst:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out[:limit] if limit > 0 else out
