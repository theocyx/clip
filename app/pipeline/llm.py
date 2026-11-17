"""IA 100 % locale (Ollama) : note les moments candidats et écrit un titre accrocheur.

Sans Ollama disponible, le classement repose uniquement sur l'analyse audio/texte.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

PROMPT = """Tu es un monteur expert en clips TikTok / YouTube Shorts francophones.
Voici la transcription d'un extrait de la vidéo « {video_title} » :

\"\"\"{text}\"\"\"

Évalue son potentiel viral (drôle, surprenant, émouvant, instructif, autonome et compréhensible sans contexte).
Si l'extrait est une publicité, un passage sponsorisé, un placement de produit, un partenariat, un code promo,
un appel à s'abonner ou à cliquer sur un lien : mets "pub": true et "score": 1.
Règles pour être vu au maximum sur TikTok et YouTube Shorts :
- Titre : 40 à 60 caractères, commence par le mot-clé que les gens cherchent (le jeu, le serveur RP ou le
  streamer s'ils sont connus), crée de la curiosité SANS dévoiler la fin, langage parlé, pas tout en majuscules.
- Accroche : 3 à 6 mots lisibles en 1 seconde, qui posent une tension ou une question (« Il n'aurait jamais dû… »,
  « Personne ne s'attendait à ça »), jamais un résumé plat.
Réponds UNIQUEMENT en JSON : {{"pub": <true|false>, "score": <entier 1-10>, "title": "<titre en français, 60 caractères max, sans hashtag>", "hook": "<accroche très courte affichée en haut de la vidéo, 3 à 6 mots, terminée par 1 ou 2 emojis en lien avec le contenu (ex. 😂 🤯 😱 🔥 💀 💰), sans hashtag>"}}"""


def _ollama_ok() -> bool:
    s = get_settings()
    if not s.ollama_url:
        return False
    try:
        r = httpx.get(f"{s.ollama_url}/api/tags", timeout=5)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(n.split(":")[0] == s.ollama_model.split(":")[0] for n in names):
            log.info("Téléchargement du modèle %s dans Ollama…", s.ollama_model)
            httpx.post(f"{s.ollama_url}/api/pull", json={"model": s.ollama_model, "stream": False}, timeout=3600)
        return True
    except Exception as e:
        log.warning("Ollama indisponible (%s).", e)
        return False


def provider() -> str:
    """Fournisseur actif : ollama ou none."""
    return "ollama" if _ollama_ok() else "none"


def available() -> bool:
    return provider() != "none"


def parse(content: str) -> tuple[float, str, str] | None:
    """(score, titre, accroche). Score 0 si l'IA y voit une pub / un passage sponsorisé."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    clean = lambda k, n: str(data.get(k, "")).strip().strip('"')[:n]  # noqa: E731
    pub = data.get("pub") in (True, "true", "oui", 1)
    return 0.0 if pub else float(data.get("score", 0)), clean("title", 90), clean("hook", 60)


def _ask_ollama(prompt: str) -> str:
    s = get_settings()
    r = httpx.post(
        f"{s.ollama_url}/api/generate",
        json={"model": s.ollama_model, "format": "json", "stream": False, "prompt": prompt,
              "options": {"temperature": 0.3, "num_ctx": 4096}},
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["response"]


def rate(text: str, video_title: str, which: str | None = None, guidance: str = "") -> tuple[float, str, str] | None:
    """guidance : leçons tirées des clips déjà publiés (voir app/learning.py), ajoutées au prompt."""
    which = which or provider()
    prompt = PROMPT.format(video_title=video_title, text=text[:4000])
    if guidance:
        prompt = guidance.strip() + "\n\n" + prompt
    try:
        if which == "ollama":
            return parse(_ask_ollama(prompt))
    except Exception as e:
        log.warning("Notation IA échouée (%s) : %s", which, e)
    return None


ANALYZE = """Tu es un expert des clips TikTok / YouTube Shorts francophones.
Voici les clips de notre chaîne qui ont LE MIEUX marché (titre, accroche, début de la transcription) :
{top}

Et ceux qui ont LE MOINS marché :
{flop}

Compare-les. Qu'est-ce qui fait la différence (type de moment, humour, tension, accroche, titre, longueur) ?
Réponds UNIQUEMENT en JSON : {{"rules": ["<règle courte et concrète>", "..."]}} avec 3 à 5 règles en français."""


def analyze(top: list[dict], flop: list[dict]) -> list[str]:
    """Demande à l'IA locale ce qui distingue les clips qui marchent. Liste vide si indisponible."""
    if provider() != "ollama":
        return []
    fmt = lambda items: "\n".join(  # noqa: E731
        f"- « {c.get('title', '')} » / accroche « {c.get('hook', '')} » : {c.get('text', '')[:300]}" for c in items)
    try:
        content = _ask_ollama(ANALYZE.format(top=fmt(top), flop=fmt(flop)))
        m = re.search(r"\{.*\}", content or "", re.S)
        rules = json.loads(m.group(0)).get("rules", []) if m else []
        return [str(r).strip()[:200] for r in rules if str(r).strip()][:5]
    except Exception as e:
        log.warning("Analyse IA des performances échouée : %s", e)
        return []
