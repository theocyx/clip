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
- Pour chaque phrase aussi : "title" = 2 à 4 mots-clés affichés en gros à l'écran (« 25 MILLIONS VENDUS »),
  "chapter" = 1 ou 2 mots qui résument l'étape (« LES FAITS », « CONTEXTE », « BILAN », « ET APRÈS ? »),
  "place" = le lieu précis dont parle la phrase s'il y en a un (« Lyon, France »), sinon "".
- "place" : le lieu principal de l'actu s'il y en a un clairement (« Marseille, France », « Ukraine »),
  sinon "".
- "topic" : 1 à 3 mots, le sujet (« GRÈVE SNCF », « ÉLECTIONS USA »).
- "answers" : 2 réponses courtes (2 à 5 mots) possibles à la question finale.
- "hashtags" : 3 à 5 hashtags en minuscules sans espace, du plus précis au plus large.
Réponds UNIQUEMENT en JSON : {{"score": <1-10>, "title": "...", "hook": "...", "sentences": [{{"text": "...", "visual": "...", "title": "...", "chapter": "...", "place": ""}}], "question": "...", "answers": ["...", "..."], "topic": "...", "place": "...", "hashtags": ["..."]}}"""

STORY_PROMPT = """Tu écris le script d'une vidéo TikTok / YouTube Shorts d'environ {seconds} secondes qui raconte
une HISTOIRE VRAIE envoûtante (secret, mystère, affaire étrange), en français. Style conteur qui captive :
tension, suspense, révélations au fil du récit, mots simples et imagés, phrases courtes, ton posé et intrigant.
Aucune intro du genre « salut » ou « aujourd'hui je vais vous raconter ».

Sujet : « {title} » ({source})
Faits disponibles :
\"\"\"{facts}\"\"\"

Règles :
- Phrase 1 = accroche qui intrigue tout de suite (le détail le plus troublant, ou une scène : « En 2003, un
  technicien découvre une pièce où personne n'a le droit d'entrer. »).
- Puis raconte dans l'ordre, une idée par phrase (8 à 20 mots), avec des relances de suspense
  (« Mais ce qu'il trouve à l'intérieur… », « Et c'est là que tout bascule. »).
- Fin : la révélation, ou ce qui reste inexpliqué aujourd'hui, puis une question au spectateur.
- {n_min} à {n_max} phrases, {w_min} à {w_max} mots au total (environ {seconds} secondes lues).
- UNIQUEMENT des faits présents ci-dessus. N'invente aucun chiffre, date, nom, lieu ni citation. Une théorie
  ou une rumeur est présentée comme telle (« certains pensent que… ») ; jamais comme un fait. Reformule avec
  tes mots, ne recopie pas le texte. Les nombres en chiffres (« 1959 », « 9 randonneurs »).
- Respect des victimes : pas de détails macabres gratuits, aucun humour sur un drame.
- Pour chaque phrase : "visual" = 2 à 4 mots-clés EN ANGLAIS pour une image d'illustration générique libre
  (ex. "dark corridor", "old telephone exchange", "snowy mountain night") ; jamais une personne célèbre,
  un logo ou une œuvre protégée ; "title" = 2 à 4 mots-clés affichés en gros (« PIÈCE SECRÈTE ») ;
  "chapter" = 1 ou 2 mots pour l'étape du récit (« LA DÉCOUVERTE », « LE SECRET », « L'ENQUÊTE »,
  « LE MYSTÈRE ») ; "place" = le lieu précis de la phrase s'il y en a un (« San Francisco, USA »), sinon "".
- "title" : titre de 40 à 70 caractères, intrigant, jamais trompeur, sans hashtag.
- "hook" : 4 à 8 mots affichés en haut de la vidéo, terminés par 1 emoji (🕵️, 😨, 🤫, 👁️…).
- "score" : 1 à 10, à quel point l'histoire va captiver le grand public.
- "place" : lieu principal de l'histoire ; "topic" : 1 à 3 mots (« ROOM 641A ») ;
  "answers" : 2 réponses courtes possibles à la question finale ; "hashtags" : 3 à 5, du plus précis au plus large.
Réponds UNIQUEMENT en JSON : {{"score": <1-10>, "title": "...", "hook": "...", "sentences": [{{"text": "...", "visual": "...", "title": "...", "chapter": "...", "place": ""}}], "question": "...", "answers": ["...", "..."], "topic": "...", "place": "...", "hashtags": ["..."]}}"""

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
    place: str = ""
    titles: list[str] = field(default_factory=list)  # mots-clés à l'écran, un par phrase
    chapters: list[str] = field(default_factory=list)  # onglet du bandeau, un par phrase
    places: list[str] = field(default_factory=list)  # lieu cité par la phrase (carte), un par phrase
    topic: str = ""
    answers: list[str] = field(default_factory=list)  # réponses A / B du quiz final

    def per_sentence(self) -> list[tuple]:
        n = len(self.sentences)
        pad = lambda xs: (list(xs) + [""] * n)[:n]  # noqa: E731
        return list(zip(self.sentences, pad(self.visuals), pad(self.titles), pad(self.chapters), pad(self.places)))

    def set_sentences(self, rows: list[tuple]) -> None:
        self.sentences, self.visuals, self.titles, self.chapters, self.places = (
            [list(r) for r in zip(*rows)] if rows else ([], [], [], [], []))

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
    rows = []
    for s in data.get("sentences") or []:
        if isinstance(s, dict):
            text, vis = str(s.get("text", "")).strip(), str(s.get("visual", "")).strip()
            extra = [_short(s.get("title"), 40), _short(s.get("chapter"), 16), _short(s.get("place"), 80)]
        else:
            text, vis, extra = str(s).strip(), "", ["", "", ""]
        text = hooks.strip_emojis(text).strip(' "')
        if text:
            rows.append((text, vis[:60], *extra))
    sentences = [r[0] for r in rows]
    if not sentences:
        return None
    try:
        score = float(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    tags = [clean_tag(t) for t in (data.get("hashtags") or []) if clean_tag(t)]
    return Script(title=engage.tidy_title(str(data.get("title", "")).strip().strip('"'))[:90],
                  hook=str(data.get("hook", "")).strip().strip('"')[:70], sentences=sentences,
                  question=engage.clean_question(str(data.get("question", ""))) if data.get("question") else "",
                  hashtags=list(dict.fromkeys(tags))[:5], score=score,
                  place=_short(data.get("place"), 80), visuals=[r[1] for r in rows],
                  titles=[r[2] for r in rows], chapters=[r[3] for r in rows], places=[r[4] for r in rows],
                  topic=_short(data.get("topic"), 28),
                  answers=[_short(x, 40) for x in (data.get("answers") or []) if _short(x, 40)][:2])


def _short(value, n: int) -> str:
    if not isinstance(value, str):
        return ""
    return hooks.strip_emojis(value).strip(' ."«»').strip()[:n]


def _bad(text: str, facts: str) -> bool:
    return bool(invented_numbers(text, facts))


def sanitize(sc: Script, facts: str, fallback_title: str = "") -> list[str]:
    """Retire du script ce qui contient un chiffre absent de l'article (le petit modèle en invente parfois)
    au lieu de jeter tout le script. Renvoie ce qui a été retiré (pour le journal)."""
    removed = []
    rows = sc.per_sentence()
    keep = [r for r in rows if not _bad(r[0], facts)]
    if len(keep) < len(rows):
        removed.append(f"{len(rows) - len(keep)} phrase(s)")
    # un mot-clé à l'écran avec un chiffre inventé est simplement effacé (remplacé plus tard)
    sc.set_sentences([(r[0], r[1], "" if _bad(r[2], facts) else r[2], r[3], r[4]) for r in keep])
    if sc.title and _bad(sc.title, facts):
        removed.append("titre")
        sc.title = engage.tidy_title(fallback_title)[:90] if fallback_title and not _bad(fallback_title, facts) else ""
    if not sc.title and sc.sentences:  # pas de titre utilisable : début de la 1re phrase
        sc.title = engage.tidy_title(" ".join(sc.sentences[0].split()[:10]))[:90]
    if sc.hook and _bad(sc.hook, facts):
        removed.append("accroche")
        sc.hook = ""
    return removed


def target_words(kind: str = "news") -> float:
    """Mots à écrire pour la durée visée (~2,6 mots/s à vitesse normale)."""
    s = get_settings()
    if kind == "story":
        return s.story_target_seconds * 2.6 * s.story_voice_speed
    return s.news_target_seconds * 2.6 * s.news_voice_speed


def problems(sc: Script, facts: str, kind: str = "news") -> list[str]:
    """Pourquoi ce script n'est pas publiable (liste vide = OK). Appelé après sanitize()."""
    out = []
    n_words = len(sc.text.split())
    target = target_words(kind)
    if n_words < max(35, target * 0.4):
        out.append(f"script trop court ({n_words} mots)")
    if n_words > target * 1.9:
        out.append(f"script trop long ({n_words} mots)")
    if len(sc.sentences) < 3:
        out.append("moins de 3 phrases")
    if invented_numbers(sc.text + " " + sc.title + " " + sc.hook, facts):
        out.append("chiffres absents de l'article")
    if not sc.title:
        out.append("pas de titre")
    return out


def write(title: str, facts: str, source: str, kind: str = "news") -> Script | None:
    """Script vérifié, ou None (IA indisponible, ou deux essais ratés). kind : news (actu) ou story (histoire).
    Un 2e essai n'est demandé que si le 1er est inutilisable (pas du JSON, ou trop court une fois nettoyé)."""
    s = get_settings()
    if llm.provider() != "ollama":
        log.info("Actu : IA locale indisponible, pas de script.")
        return None
    words = target_words(kind)
    story = kind == "story"
    seconds = s.story_target_seconds if story else s.news_target_seconds
    n_min, n_max = max(5, int(words / 18)), max(8, int(words / 10))
    prompt = (STORY_PROMPT if story else PROMPT).format(
        topic=s.news_topic, source=source or "média", title=title, facts=facts[:5000 if story else 3000],
        n_min=n_min, n_max=n_max, w_min=int(words * 0.85), w_max=int(words * 1.2), seconds=int(seconds))
    max_tokens = 600 + n_max * 110
    facts_all = facts + " " + title
    last: list[str] = []
    for attempt in range(2):
        try:
            sc = parse(llm._ask_ollama(prompt if not last else prompt + "\n\nATTENTION, essai précédent refusé : "
                                       + "; ".join(last) + ". Corrige ça.", timeout=s.news_llm_timeout,
                                       max_tokens=max_tokens, num_ctx=8192))
        except Exception as e:
            log.warning("Script d'actu : IA en erreur (%s)", e)
            return None
        if sc is None:
            last = ["réponse pas en JSON"]
            continue
        removed = sanitize(sc, facts_all, title)
        if removed:
            log.info("Script d'actu : retiré (chiffre absent de l'article) : %s", ", ".join(removed))
        last = problems(sc, facts_all, kind)
        if not last:
            if not sc.question:
                sc.question = engage.clean_question(sc.sentences[-1]) if sc.sentences[-1].endswith("?") else ""
            return sc
        log.info("Script d'actu refusé (essai %d) : %s", attempt + 1, "; ".join(last))
    return None
