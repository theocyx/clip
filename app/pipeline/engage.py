"""Engagement : titres variés tirés du contenu du clip, et question finale qui pousse à commenter.

Le petit modèle local a tendance à recopier le titre du live (« #GTAVI Live Stream 137 Heures ») :
on lui donne un titre de live nettoyé, la liste des titres récents à éviter, et on vérifie après coup.
"""
from __future__ import annotations

import difflib
import random
import re
from datetime import timedelta

STOP = {
    "le", "la", "les", "un", "une", "des", "de", "du", "d", "l", "et", "ou", "a", "à", "au", "aux", "en", "sur",
    "pour", "par", "avec", "dans", "ce", "ça", "sa", "son", "ses", "il", "elle", "on", "je", "tu", "est", "c",
    "qui", "que", "qu", "pas", "ne", "n", "y", "se", "s", "mon", "ma", "mes", "the", "of",
}
_WORD = re.compile(r"[\wàâäéèêëïîôöùûüç]+", re.I)
# bruit des titres de live : compteurs, « live », « stream », hashtags, jours…
_HOURS = re.compile(r"\b\d+\s*(h|heures?|hours?|jours?|days?|j)\b", re.I)
_NOISE = re.compile(
    r"\b(live|stream|streaming|en direct|direct|subathon|"
    r"jour|day|ep|épisode|episode|part|partie)\b\s*\d*|\[[^\]]*\]|\([^)]*\)|[|!•\-–—_]+",
    re.I,
)


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in STOP]


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()


def clean_video_title(title: str) -> str:
    """Titre du live sans le bruit (compteurs d'heures, « live stream », hashtags) donné à l'IA comme contexte."""
    out = _NOISE.sub(" ", _HOURS.sub(" ", re.sub(r"#(\w+)", r"\1", title or "")))
    out = re.sub(r"\s{2,}", " ", out).strip(" :,.")
    return out or (title or "").strip()


def topic(video_title: str, text: str = "") -> str:
    """Mot-clé recherché à mettre en tête du titre (GTA 6, GTA RP, serveur…), ou ""."""
    hay = f"{video_title} {text}".lower()
    for pattern, name in ((r"flash ?back", "FlashBack"), (r"21 ?jump ?click", "21JumpClick"),
                          (r"faily ?v\b", "FailyV"), (r"no ?pixel", "NoPixel")):
        if re.search(pattern, hay):
            return f"GTA RP {name}"
    if re.search(r"gta ?(6|vi|six)\b", hay):
        return "GTA 6"
    if re.search(r"gta ?rp|\brp\b|role ?play", hay):
        return "GTA RP"
    if re.search(r"\bgta\b|grand theft auto", hay):
        return "GTA"
    return ""


def too_close(title: str, video_title: str, recent: list[str]) -> bool:
    """Vrai si le titre recopie le titre du live ou ressemble trop à un titre déjà publié."""
    tw = words(title)
    if len(tw) < 2:
        return True
    vw = set(words(video_title))
    if vw and sum(w in vw for w in tw) / len(tw) >= 0.7:
        return True
    if similarity(title, video_title) >= 0.6:
        return True
    return any(similarity(title, r) >= 0.75 for r in recent)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", re.sub(r"\s+", " ", text or "").strip())
    return [p.strip(" ,;-") for p in parts if len(p.strip()) >= 12]


def content_title(text: str, video_title: str = "", recent: list[str] | None = None) -> str:
    """Titre construit à partir de la phrase la plus forte du clip, mot-clé en tête."""
    from .subtitles import is_keyword

    recent = recent or []
    best, best_score = "", -1e9
    for sent in _sentences(text):
        n = len(sent)
        score = sum(is_keyword(w) for w in sent.split()) * 2 + sent.count("!") + sent.count("?") * 1.5
        score -= abs(n - 45) / 15  # ni trop court ni trop long
        if any(similarity(sent, r) >= 0.75 for r in recent):
            score -= 10
        if score > best_score:
            best, best_score = sent, score
    if not best:
        best = (text or "").strip()
    if len(best) > 58:
        best = best[:57].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    best = best[:1].upper() + best[1:]
    key = topic(video_title, text)
    if key and key.lower() not in best.lower():
        best = f"{key} : {best}"
    return best.strip()


# ---------- Titres « citation » (style @cyprienlefute : « Je suis le meilleur » 😂) ----------

_QUOTE_WORD = re.compile(r"[\wàâäçéèêëîïôöùûüœ']+", re.I)


def _norm_words(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _QUOTE_WORD.findall((text or "").replace("’", "'"))]


def said_in(quote: str, text: str) -> bool:
    """Vrai si la citation a bien été dite dans l'extrait (au moins 80 % de ses mots, dans la transcription)."""
    qw, tw = _norm_words(quote), set(_norm_words(text))
    return bool(qw) and sum(w in tw for w in qw) / len(qw) >= 0.8


def clean_quote(quote: str) -> str:
    q = re.sub(r"[«»\"“”„`*_#~<>{}\[\]]", " ", quote or "")
    q = re.sub(r"([!?])[!?]+", r"\1", re.sub(r"\s+", " ", q)).strip(" .,;:-–—'’")
    return q[:1].upper() + q[1:]


def pick_quote(llm_quote: str, text: str) -> str:
    """Phrase marquante dite dans l'extrait (2 à 9 mots, 48 caractères max) : celle de l'IA si elle a vraiment été
    dite, sinon la phrase courte la plus forte de la transcription (exclamations, mots forts). "" si aucune."""
    from .subtitles import is_keyword

    def ok(q: str) -> bool:
        n = len(_norm_words(q))
        return 2 <= n <= 9 and len(q) <= 48 and not bad_title(q) and not looks_english(q)

    q = clean_quote(llm_quote)
    if q and ok(q) and said_in(q, text):
        return q
    best, best_score = "", 0.5
    for sent in re.split(r"(?<=[.!?…])\s+", re.sub(r"\s+", " ", text or "").strip()):
        cand = clean_quote(sent)
        if not ok(cand):
            continue
        score = sum(is_keyword(w) for w in cand.split()) + 1.5 * sent.count("!") + sent.count("?")
        score -= abs(len(_norm_words(cand)) - 5) / 4
        if score > best_score:
            best, best_score = cand, score
    return best


def quote_title(quote: str, streamer: str = "", hook: str = "") -> str:
    """« Streamer : « citation » 😭 » (les hashtags #gta6… sont ajoutés à l'envoi). "" sans citation."""
    from .hook import DEFAULT_EMOJI, emojis_in

    q = clean_quote(quote)
    if not q:
        return ""
    emoji = (emojis_in(hook) or [DEFAULT_EMOJI])[0]
    title = f"« {q} » {emoji}"
    if streamer.strip():
        title = f"{streamer.strip()} : {title}"
    return title if len(title) <= 80 else ""


_GTA_NAME = re.compile(r"\b(gta ?vi|gta6|gta ?six)\b", re.I)
_JUNK = re.compile(r"[\"“”„`*_~\\<>{}\[\]^]")
# Vulgarité : un Short au titre grossier est moins recommandé (et donne une mauvaise image de la chaîne)
_VULGAR = re.compile(
    r"(?<![\w])(cul|culs|bite|bites|merde|putain|pute|putes|salope|salopes|connard|connasse|encul\w*|nique|niquer|"
    r"niqu[ée]\w*|fdp|ntm|bordel|chier|chiant\w*|couilles?|branl\w*|porno?|sexe|sexy|nichons?|baiser|baise|"
    r"teub|zizi|batard|bâtard|pd|tg|fuck\w*|shit|bitch\w*|nudes?)(?![\w])", re.I)
# Titres trompeurs ou qui ne disent rien du moment (« un streamer », « cette vidéo », promesses fausses)
_MISLEADING = re.compile(
    r"\b(un streamer|une vid[ée]o|cette vid[ée]o|la vid[ée]o|gratuit\w*|giveaway|leak\w*|fuite|"
    r"officiel\w*|annonce officielle|clique|abonne\w*|(?:100|cent) ?% (?:vrai|r[ée]el))\b", re.I)


def bad_title(title: str) -> str:
    """Pourquoi ce titre ne doit pas partir ("" = il est correct)."""
    if _VULGAR.search(title or ""):
        return "vulgaire"
    if _MISLEADING.search(title or ""):
        return "trompeur ou vide"
    if has_placeholder(title):
        return "« X » au lieu du nom"
    if copies_example(title):
        return "exemple des consignes recopié"
    return ""


# Le petit modèle recopie parfois un exemple des consignes tel quel, « X » compris
# (« X règle ça à sa façon GTA 6 » affiché sur une vidéo le 25/11).
_PLACEHOLDER = re.compile(r"(?<![\w'’.-])(?<!Mr )(?<!Mr\. )(?<!Malcolm )X(?![\w'’-])")
# exemples des anciennes consignes, que le modèle recopiait (les consignes n'en donnent plus)
PROMPT_EXAMPLES = ["refait sa maison pour séduire Lucia dans GTA 6", "se fait rentrer dedans et règle ça à sa façon"]


def has_placeholder(text: str) -> bool:
    """« X » tout seul (nom de l'exemple) au lieu du vrai nom du streamer."""
    return bool(_PLACEHOLDER.search(text or ""))


def copies_example(text: str) -> bool:
    """Titre ou accroche qui reprend un bout d'exemple des consignes (2 mots forts d'affilée ou plus)."""
    generic = {"gta", "6", "vi"}
    wt = [w for w in words(text) if w not in generic]
    for ex in PROMPT_EXAMPLES:
        we = [w for w in words(ex) if w not in generic]
        m = difflib.SequenceMatcher(None, wt, we, autojunk=False).find_longest_match(0, len(wt), 0, len(we))
        if m.size >= 2:
            return True
    return False


def fill_name(text: str, streamer: str = "") -> str:
    """Titre ou accroche de l'IA sans « X » : remplacé par le nom du streamer s'il est connu.
    "" si le texte est inutilisable (nom inconnu, ou exemple recopié) : on prend alors le repli habituel."""
    t = (text or "").strip()
    if has_placeholder(t):
        if not streamer.strip():
            return ""
        t = _PLACEHOLDER.sub(streamer.strip(), t)
    return "" if copies_example(t) else t


# Petits mots qui trahissent une langue (le modèle local répond parfois en anglais malgré la consigne)
_EN_WORDS = {"the", "of", "and", "in", "under", "with", "for", "to", "is", "are", "was", "were", "his",
             "her", "its", "their", "how", "why", "what", "who", "when", "from", "behind", "after", "this", "that",
             "back", "new", "unexplained", "mystery", "mysterious", "enigmatic", "beast", "prisoner",
             "comeback", "story", "truth", "shocking", "breaking", "amazing", "first", "never", "ever", "again",
             "man", "woman", "men", "women", "people", "world", "life", "death", "dark", "hidden", "lost", "killer",
             "murder", "inside", "you", "your", "my", "we", "he", "she", "they", "has", "have", "had", "will",
             "can", "not", "masked", "mask", "king", "queen", "legend", "ancient", "curse", "cursed", "treasure",
             "ghost", "haunted"}
_FR_WORDS = {"le", "la", "les", "un", "une", "des", "du", "de", "et", "à", "au", "aux", "en", "sur", "sous",
             "pour", "est", "sont", "qui", "que", "dans", "avec", "par", "son", "sa", "ses", "après", "ce", "cette",
             "il", "elle", "ils", "on", "pas", "plus", "mais", "ou", "où", "quand", "comment", "pourquoi", "mystère",
             "secret", "énigme", "vérité", "histoire", "nouveau", "nouvelle", "jamais", "enfin", "encore"}


def looks_english(text: str) -> bool:
    """Vrai si le texte est plutôt en anglais (titre, accroche) : il doit alors être réécrit en français."""
    t = (text or "").lower().replace("’", "'")
    if re.search(r"\b[a-z]+'s\b", t):  # « Nvidia's comeback »
        return True
    words = re.findall(r"[a-zàâäçéèêëîïôöùûüœ']+", t)
    words = [w.split("'")[-1] if w.startswith(("l'", "d'", "qu'", "n'", "s'", "c'", "j'")) else w for w in words]
    fr_hint = any(w.startswith(("l'", "d'", "qu'")) for w in re.findall(r"[a-zàâäçéèêëîïôöùûüœ']+", t))
    en = sum(w in _EN_WORDS and w not in _FR_WORDS for w in words)
    fr = sum(w in _FR_WORDS and w not in _EN_WORDS for w in words) + fr_hint
    if re.search(r"[àâçéèêëîïôûùœ]", t):
        fr += 1
    return en >= 1 and en > fr


def tidy_title(title: str) -> str:
    """Titre propre : sans guillemets ni caractères parasites, commandes de chat (!drop), mentions, hashtags ni
    barres en trop, « GTA 6 » écrit comme on le cherche."""
    t = (title or "").replace("#", " ")
    t = _JUNK.sub(" ", t)
    if t.count("«") != t.count("»"):  # guillemet français orphelin
        t = t.replace("«", " ").replace("»", " ")
    t = re.sub(r"(^|\s)[!@]\w+", " ", t)
    t = _GTA_NAME.sub("GTA 6", t)
    t = re.sub(r"\s*\|\s*(\|\s*)*", " | ", t)
    t = re.sub(r"\s+([,.…])", r"\1", t)
    t = re.sub(r"\s+:", " :", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" |-–—:,'’")
    return t


def fix_title(title: str, video_title: str, recent: list[str], text: str, hook: str = "") -> str:
    """Garde le titre de l'IA s'il est original, sinon le reconstruit à partir de ce qui se dit dans le clip.

    Ne renvoie jamais une copie du titre du live (« Live Stream 137 Heures ») : en dernier recours,
    accroche ou début de la transcription précédés du mot-clé."""
    title = tidy_title(title)
    if title and not too_close(title, video_title, recent) and not bad_title(title):
        return title
    key = topic(video_title, text) or "GTA 6"
    kw = set(words(key))
    meaningful = lambda t: len([w for w in words(t) if w not in kw]) >= 2  # noqa: E731
    alt = tidy_title(content_title(text, video_title, recent)) if (text or "").strip() else ""
    if alt and meaningful(alt) and not too_close(alt, video_title, []) and not bad_title(alt):
        return alt
    from .hook import strip_emojis

    for cand in (strip_emojis(hook or ""), " ".join((text or "").split()[:9])):
        cand = tidy_title(cand)
        if meaningful(cand) and not bad_title(cand):
            cand = cand if key.lower() in cand.lower() else f"{key} : {cand[:1].upper()}{cand[1:]}"
            if not too_close(cand, video_title, []):
                return cand[:70]
    return f"{key} : la scène que personne n'a vu venir"


# Questions de repli (sans IA) selon le contenu : elles invitent à donner son avis en commentaire
QUESTIONS: list[tuple[str, list[str]]] = [
    (r"police|flic|arrêt|prison|course|poursuite|braquage|banque",
     ["Il aurait dû se rendre ou continuer ?", "T'aurais fait quoi à sa place ?"]),
    (r"mdr|ptdr|rire|drôle|blague|lol",
     ["Note ce moment sur 10 😂", "Qui a déjà vécu ça ?"]),
    (r"argent|€|euros?|dollars?|million|payé|riche",
     ["Tu aurais accepté ?", "Ça vaut vraiment ce prix ?"]),
    (r"mort|tué|kill|perdu|rate|raté|fail",
     ["C'est de sa faute ou pas ?", "T'aurais survécu toi ?"]),
    (r"gta ?6|gta ?vi|trailer|sortie",
     ["Tu vas acheter GTA 6 day one ?", "GTA 6 : hype ou déçu ?"]),
]
GENERIC = ["T'aurais fait pareil, oui ou non ?", "Tu valides ou pas ?", "Team lui ou team l'autre ?",
           "Génie ou complètement fou ?"]  # un choix qu'on tranche en un mot : on répond bien plus qu'à « ton avis ? »


def fallback_question(text: str, title: str = "") -> str:
    hay = f"{title} {text}".lower()
    for pattern, options in QUESTIONS:
        if re.search(pattern, hay):
            return random.choice(options)
    return random.choice(GENERIC)


def clean_question(q: str) -> str:
    from .hook import strip_emojis

    q = re.sub(r"\s{2,}", " ", strip_emojis(q or "").replace("#", " ")).strip(" \"'")
    if not q or len(q) > 70 or len(words(q)) < 2:
        return ""
    return q if q.endswith(("?", "!", "…")) else q + " ?"


def end_card(question: str, part: int = 1, part_count: int = 1) -> str:
    """Texte affiché à la fin : la suite pour les parties, sinon la question qui pousse à commenter."""
    if part < part_count:
        return f"La suite en partie {part + 1} : abonne-toi pour ne pas la rater"
    q = clean_question(question)
    if not q:
        return ""
    return q if "comment" in q.lower() else f"{q} Dis-le en commentaire"


def caption_question(question: str) -> str:
    """Ligne ajoutée en tête de description / légende pour lancer les commentaires."""
    q = clean_question(question)
    return f"💬 {q} Réponds en commentaire 👇" if q else ""


def recent_titles(days: int = 3, limit: int = 40) -> list[str]:
    """Titres des clips récents (sans « (partie x/y) »), pour ne pas les répéter."""
    from sqlmodel import select

    from .. import db
    from ..models import Clip, now

    try:
        with db.session() as s:
            rows = s.exec(select(Clip.title).where(Clip.created_at >= now() - timedelta(days=days))
                          .order_by(Clip.id.desc()).limit(limit)).all()
    except Exception:
        return []
    out = []
    for t in rows:
        t = re.sub(r"\s*\(partie \d+/\d+\)$", "", t or "").strip()
        if t and t not in out:
            out.append(t)
    return out
