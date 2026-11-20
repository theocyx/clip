"""Script de la vidéo d'actu, écrit par l'IA locale (Ollama) à partir des faits de l'article.

Garde-fous : le texte est réécrit (jamais recopié), et un chiffre absent de l'article fait rejeter le script
(le petit modèle local invente parfois des nombres)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ..config import get_settings
from ..pipeline import engage, hook as hooks, llm

log = logging.getLogger(__name__)

PROMPT = """Tu écris le script d'une vidéo TikTok / YouTube Shorts d'actualité en français, pour une chaîne
sur le thème « {topic} ». Style des comptes d'actu qui cartonnent : voix off rapide et naturelle, phrases
courtes, ton direct comme un pote bien informé, aucune intro du genre « salut » ou « aujourd'hui on parle de ».

Article ({source}) : « {title} »
Faits disponibles :
\"\"\"{facts}\"\"\"

Règles :
- Phrase 1 = accroche choc qui donne envie de rester (fait le plus surprenant, chiffre fort ou question).
- Puis le contexte et les faits importants, dans l'ordre logique, une idée par phrase (8 à 18 mots par phrase).
- Dernière phrase : une question au spectateur pour qu'il réponde en commentaire.
- {n_min} à {n_max} phrases, {w_min} à {w_max} mots au total (environ {seconds} secondes lues).
- UNIQUEMENT des faits présents ci-dessus. N'invente aucun chiffre, date, nom ni citation. Ne recopie pas
  l'article mot pour mot : reformule avec tes mots. Les nombres en chiffres (« 2026 », « 70 euros »).
- Pour chaque phrase, "visual" = 2 à 4 mots-clés EN ANGLAIS décrivant une image d'illustration générique
  trouvable dans une banque d'images libres (ex. "video game controller", "city night aerial",
  "stock market screen") ; jamais un logo, une personne célèbre ou une œuvre protégée.
- Ton neutre et factuel : aucune opinion politique, et sur un drame (décès, accident, attentat, guerre)
  aucun humour ni emoji rieur.
- "title" : titre de 40 à 70 caractères, oral et intrigant, sans hashtag, jamais trompeur.
- "hook" : 4 à 8 mots affichés en gros en haut de la vidéo, terminés par 1 emoji.
- "score" : note de 1 à 10 de l'intérêt du sujet pour le grand public de la chaîne (7+ seulement si les gens
  vont le partager ou en débattre ; promo, bon plan, test produit ou sujet trop pointu : 3 maximum).
- "hashtags" : 3 à 5 hashtags en minuscules sans espace, du plus précis au plus large.
Réponds UNIQUEMENT en JSON : {{"score": <1-10>, "title": "...", "hook": "...", "sentences": [{{"text": "...", "visual": "..."}}], "question": "...", "hashtags": ["..."]}}"""

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Script:
    title: str
    hook: str
    sentences: list[str]
    visuals: list[str]
    question: str = ""
    hashtags: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(self.sentences)


def _numbers(text: str) -> set[str]:
    text = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}\b)", "", text)  # « 25 000 » → « 25000 »
    return {n.replace(",", ".").lstrip("0") or "0" for n in _NUM.findall(text)}


def invented_numbers(script_text: str, facts: str) -> list[str]:
    """Chiffres du script absents des faits (les petits nombres écrits en lettres dans l'article passent)."""
    known = _numbers(facts)
    return [n for n in _numbers(script_text) if n not in known and not (n.isdigit() and int(n) <= 10)]


def clean_tag(t: str) -> str:
    return re.sub(r"[^\w]", "", str(t).lower().lstrip("#"), flags=re.UNICODE)[:30]


def parse(content: str) -> Script | None:
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    sentences, visuals = [], []
    for s in data.get("sentences") or []:
        if isinstance(s, dict):
            text, vis = str(s.get("text", "")).strip(), str(s.get("visual", "")).strip()
        else:
            text, vis = str(s).strip(), ""
        text = hooks.strip_emojis(text).strip(' "')
        if text:
            sentences.append(text)
            visuals.append(vis[:60])
    if not sentences:
        return None
    try:
        score = float(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    tags = [clean_tag(t) for t in (data.get("hashtags") or []) if clean_tag(t)]
    return Script(title=engage.tidy_title(str(data.get("title", "")).strip().strip('"'))[:90],
                  hook=str(data.get("hook", "")).strip().strip('"')[:70], sentences=sentences, visuals=visuals,
                  question=engage.clean_question(str(data.get("question", ""))) if data.get("question") else "",
                  hashtags=list(dict.fromkeys(tags))[:5], score=score)


def problems(sc: Script, facts: str) -> list[str]:
    """Pourquoi ce script n'est pas publiable (liste vide = OK)."""
    s = get_settings()
    out = []
    n_words = len(sc.text.split())
    target = s.news_target_seconds * 2.6 * s.news_voice_speed  # ~2,6 mots/s à vitesse normale
    if n_words < target * 0.55:
        out.append(f"script trop court ({n_words} mots)")
    if n_words > target * 1.6:
        out.append(f"script trop long ({n_words} mots)")
    if len(sc.sentences) < 4:
        out.append("moins de 4 phrases")
    bad = invented_numbers(sc.text + " " + sc.title + " " + sc.hook, facts)
    if bad:
        out.append("chiffres absents de l'article : " + ", ".join(bad[:5]))
    if not sc.title:
        out.append("pas de titre")
    return out


def write(title: str, facts: str, source: str) -> Script | None:
    """Script vérifié, ou None (IA indisponible, ou deux essais ratés)."""
    s = get_settings()
    if llm.provider() != "ollama":
        log.info("Actu : IA locale indisponible, pas de script.")
        return None
    words = s.news_target_seconds * 2.6 * s.news_voice_speed
    prompt = PROMPT.format(topic=s.news_topic, source=source or "média", title=title, facts=facts[:3500],
                           n_min=6, n_max=10, w_min=int(words * 0.8), w_max=int(words * 1.15),
                           seconds=int(s.news_target_seconds))
    last = []
    for attempt in range(2):
        try:
            sc = parse(llm._ask_ollama(prompt if not last else prompt + "\n\nATTENTION, essai précédent refusé : "
                                       + "; ".join(last) + ". Corrige ça."))
        except Exception as e:
            log.warning("Script d'actu : IA en erreur (%s)", e)
            return None
        if sc is None:
            last = ["réponse pas en JSON"]
            continue
        last = problems(sc, facts + " " + title)
        if not last:
            if not sc.question:
                sc.question = engage.clean_question(sc.sentences[-1]) if sc.sentences[-1].endswith("?") else ""
            return sc
        log.info("Script d'actu refusé (essai %d) : %s", attempt + 1, "; ".join(last))
    return None
