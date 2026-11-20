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
Voici la transcription d'un extrait d'un live (sujet du live : « {video_title} »){who} :

\"\"\"{text}\"\"\"

Évalue son potentiel viral (drôle, surprenant, émouvant, instructif, autonome et compréhensible sans contexte).
Sois exigeant : la plupart des extraits valent 3 à 6. 7 et plus seulement si un inconnu qui ne connaît pas
le streamer regarderait jusqu'au bout et le partagerait. Bavardage, lecture du chat, attente, menu, chargement :
3 au maximum. Notre chaîne parle de GTA : un extrait qui ne parle pas du jeu (ou du RP) vaut 4 au maximum.
Si l'extrait est une publicité, un passage sponsorisé, un placement de produit, un partenariat, un code promo,
un appel à s'abonner ou à cliquer sur un lien : mets "pub": true et "score": 1.
Règles pour être vu au maximum sur TikTok et YouTube Shorts (style des clips GTA 6 qui font le plus de vues) :
- Titre : 30 à 60 caractères, comme un pote qui raconte la scène à l'oral : le streamer (s'il est connu) + ce
  qu'il fait ou sa réaction + « GTA 6 » (ou le jeu / serveur RP). Modèles : « X est choqué par … de GTA 6 »,
  « X devient … dans GTA 6 », « X c'est un fou sur GTA 6 », « WTF … dans GTA 6, X n'en revient pas ». Raconte CE
  QUI SE PASSE DANS CET EXTRAIT, pas tout en majuscules, sans guillemets, jamais vulgaire ni trompeur (rien qui
  n'est pas dans l'extrait). NE RECOPIE PAS le sujet du live
  (pas de « live », « stream », nombre d'heures ni de hashtag) : chaque clip doit avoir un titre unique.{recent}
- Accroche (affichée en gros sur la vidéo pendant tout le clip) : 7 à 10 mots qui décrivent la situation au présent,
  ton oral et drôle, avec le streamer s'il est connu et « GTA 6 » si ça colle (« X refait sa maison pour séduire
  Lucia dans GTA 6 », « X se fait rentrer dedans et règle ça à sa façon »), jamais un résumé plat ni une promesse
  fausse.
- Question : une question courte (8 mots max) sur CE moment, à laquelle tout le monde a envie de répondre en
  commentaire (avis, choix, pronostic : « Il aurait dû fuir ou se rendre ? », « Tu lui pardonnes ? »).
Réponds UNIQUEMENT en JSON : {{"pub": <true|false>, "score": <entier 1-10>, "title": "<titre en français, 60 caractères max, sans hashtag>", "hook": "<accroche affichée sur la vidéo, 7 à 10 mots, terminée par 1 emoji en lien avec le contenu (ex. 😭 💀 😂 🤯 😱), sans hashtag>", "question": "<question courte pour les commentaires>"}}"""


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


def parse(content: str) -> tuple[float, str, str, str] | None:
    """(score, titre, accroche, question). Score 0 si l'IA y voit une pub / un passage sponsorisé."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    clean = lambda k, n: str(data.get(k, "")).strip().strip('"')[:n]  # noqa: E731
    pub = data.get("pub") in (True, "true", "oui", 1)
    return 0.0 if pub else float(data.get("score", 0)), clean("title", 90), clean("hook", 60), clean("question", 90)


def _ask_ollama(prompt: str, timeout: float = 600, max_tokens: int | None = None, num_ctx: int = 4096) -> str:
    s = get_settings()
    options = {"temperature": 0.5, "num_ctx": num_ctx}
    if max_tokens:
        options["num_predict"] = max_tokens
    r = httpx.post(
        f"{s.ollama_url}/api/generate",
        json={"model": s.ollama_model, "format": "json", "stream": False, "prompt": prompt, "options": options},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["response"]


def rate(text: str, video_title: str, which: str | None = None, guidance: str = "",
         recent: list[str] | None = None, streamer: str = "") -> tuple[float, str, str, str] | None:
    """guidance : leçons tirées des clips déjà publiés (voir app/learning.py), ajoutées au prompt.

    recent : titres déjà utilisés, que l'IA ne doit pas reprendre. streamer : nom à citer dans le titre."""
    which = which or provider()
    avoid = ""
    if recent:
        avoid = "\n  Titres déjà utilisés (interdit de les reprendre ou de les imiter) :\n" + "\n".join(
            f"  - {t}" for t in recent[:12])
    who = f", streamer : {streamer}" if streamer else ""
    prompt = PROMPT.format(video_title=video_title, text=text[:4000], recent=avoid, who=who)
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


POLISH = """Tu es le rédacteur en chef d'une chaîne YouTube Shorts / TikTok francophone sur GTA.
On ne publie que les meilleurs extraits : sois très exigeant. Voici un extrait prêt à partir.

Transcription : \"\"\"{text}\"\"\"
Titre actuel : « {title} »
Accroche affichée en haut de la vidéo : « {hook} »

1. Note de 1 à 10 sa chance de faire beaucoup de vues auprès d'inconnus (7+ seulement si on le regarde
   jusqu'au bout et qu'on a envie de le partager ou de commenter).
2. Améliore le titre : 30 à 60 caractères, ce qui se passe vraiment, à l'oral comme un pote qui raconte
   (le streamer + ce qu'il fait ou sa réaction + « GTA 6 »), curiosité sans dévoiler la fin, sans hashtag, sans « live » ni « stream », sans guillemets, jamais vulgaire
   ni trompeur (ne promets rien qui n'est pas dans l'extrait). Garde le titre actuel s'il est déjà excellent.{recent}
3. Une question courte (8 mots max) sur CE moment, qui donne envie de répondre en commentaire.
Réponds UNIQUEMENT en JSON : {{"score": <entier 1-10>, "title": "<titre>", "question": "<question>"}}"""


def polish(text: str, title: str, hook: str = "", guidance: str = "",
           recent: list[str] | None = None) -> tuple[float, str, str] | None:
    """Deuxième lecture avant l'envoi, avec les leçons tirées des stats : (note, titre, question) ou None."""
    if not text.strip() or provider() != "ollama":
        return None
    avoid = ""
    if recent:
        avoid = "\n   Titres déjà publiés (interdit de les reprendre) :\n" + "\n".join(f"   - {t}" for t in recent[:12])
    prompt = POLISH.format(text=text[:4000], title=title, hook=hook, recent=avoid)
    if guidance:
        prompt = guidance.strip() + "\n\n" + prompt
    try:
        m = re.search(r"\{.*\}", _ask_ollama(prompt) or "", re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        return (float(data.get("score", 0)), str(data.get("title", "")).strip().strip('"')[:90],
                str(data.get("question", "")).strip().strip('"')[:90])
    except Exception as e:
        log.warning("Relecture IA échouée : %s", e)
        return None
