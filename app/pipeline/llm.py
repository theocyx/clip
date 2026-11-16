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
Réponds UNIQUEMENT en JSON : {{"score": <entier 1-10>, "title": "<titre accrocheur en français, 70 caractères max, sans hashtag>"}}"""


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


def parse(content: str) -> tuple[float, str] | None:
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    return float(data.get("score", 0)), str(data.get("title", "")).strip().strip('"')[:90]


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


def rate(text: str, video_title: str, which: str | None = None) -> tuple[float, str] | None:
    which = which or provider()
    prompt = PROMPT.format(video_title=video_title, text=text[:4000])
    try:
        if which == "ollama":
            return parse(_ask_ollama(prompt))
    except Exception as e:
        log.warning("Notation IA échouée (%s) : %s", which, e)
    return None
