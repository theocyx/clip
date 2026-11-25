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

EXTEND_PROMPT = """Voici le script (en cours) d'une vidéo {what} en français, sujet « {title} » ({source}) :
\"\"\"{script}\"\"\"

Faits disponibles :
\"\"\"{facts}\"\"\"

Le script est trop court. Écris la SUITE : {n} nouvelles phrases (8 à 20 mots chacune) qui se placent avant la
question finale et apportent d'AUTRES faits de l'article pas encore racontés (contexte, détails, déroulé,
conséquences, réactions, ce qu'on ignore encore). Même ton, aucune répétition, UNIQUEMENT des faits présents
ci-dessus (aucun chiffre, nom ni date inventé). Tout en français (sauf "visual", en anglais).
Réponds UNIQUEMENT en JSON : {{"sentences": [{{"text": "...", "visual": "...", "title": "...", "chapter": "...", "place": ""}}]}}"""

# ---------- écriture en 3 étapes (meilleur récit qu'en une seule réponse JSON) ----------

OUTLINE_PROMPT = """Lis ces informations sur « {title} » ({source}) et liste les faits importants DANS L'ORDRE où ils
se sont passés, pour pouvoir les raconter à quelqu'un qui ne connaît rien au sujet.

Informations :
\"\"\"{facts}\"\"\"

Pour chaque fait : une phrase simple et complète en français (qui fait quoi, où, quand), avec les chiffres et
les noms exacts du texte. 8 à 16 faits. Présente chaque personne ou organisation par son rôle.
N'invente rien : seulement ce qui est écrit ci-dessus. Une théorie ou une rumeur est notée comme telle.
Réponds en JSON : {{"accroche": "<le fait le plus surprenant ou intrigant>", "qui": "...", "ou": "...", "quand": "...", "faits": ["...", "..."], "inconnu": "<ce qui reste incertain ou inexpliqué, sinon vide>"}}"""

NEWS_NARRATION = """Tu es la voix off d'une chaîne TikTok d'actualité française qui cartonne. Écris le texte lu à voix
haute, environ {words} mots (environ {seconds} secondes), sur : « {title} ».

Faits (dans l'ordre) :
{outline}

Structure obligatoire :
1. Accroche (1 ou 2 phrases) : le fait le plus fort, dit simplement, qui donne envie de rester.
2. Le contexte : qui, où, quand. Présente chaque personne ou organisation la première fois.
3. Les faits dans l'ordre, une idée par phrase, reliés par des mots de liaison (d'abord, ensuite, quelques
   heures plus tard, résultat, mais).
4. Pourquoi c'est important, ou ce qui va se passer maintenant.
5. Une question au spectateur, pour qu'il réponde en commentaire.

Règles : français oral et naturel, phrases courtes (8 à 18 mots) mais complètes, une seule idée par phrase,
aucun jargon sans explication, ton neutre (aucune opinion), pas de liste ni de titre ni d'emoji ni de guillemets,
pas d'intro du genre « salut » ou « aujourd'hui on parle de ». Uniquement les faits ci-dessus, nombres en chiffres.
Réponds avec le texte seul, en un paragraphe."""

STORY_NARRATION = """Tu es un conteur sur TikTok, spécialiste des histoires vraies qui donnent des frissons. Écris le texte
lu à voix haute, environ {words} mots (environ {seconds} secondes), sur : « {title} ».

Faits (dans l'ordre) :
{outline}
Ce qui reste incertain : {unknown}

Structure obligatoire :
1. Ouverture choc (1 ou 2 phrases au présent) : une scène ou un détail troublant qui intrigue tout de suite.
2. Retour au début : qui, où, quand. Le spectateur ne connaît rien au sujet : présente chaque personne et lieu.
3. Le déroulé dans l'ordre, la tension monte, chaque étape amène la suivante (« Mais… », « Et c'est là que… »,
   « Personne ne sait encore que… »).
4. Le rebondissement ou la révélation.
5. Ce qui reste inexpliqué aujourd'hui (une théorie est présentée comme une théorie).
6. Une question au spectateur.

Règles : texte en français (même si les faits sont en anglais), présent de narration, phrases courtes (6 à 18 mots) mais complètes, une idée par phrase, mots simples
et images fortes, suspense sans exagérer, aucun humour sur un drame, pas de liste ni de titre ni d'emoji ni de
guillemets, pas d'intro du genre « aujourd'hui je vais vous raconter ». Uniquement les faits ci-dessus, nombres
en chiffres. Réponds avec le texte seul, en un paragraphe."""

META_PROMPT = """Voici les phrases numérotées d'une vidéo TikTok {what} sur « {title} » :
{numbered}

Pour CHAQUE phrase, donne : "visual" = 2 à 4 mots-clés EN ANGLAIS pour une image d'illustration générique
(ex. "dark corridor", "train station crowd"), jamais une personne célèbre ni un logo ; "title" = 2 à 4 mots
affichés en gros (« PIÈCE SECRÈTE ») ; "chapter" = 1 ou 2 mots pour l'étape (« LES FAITS », « CONTEXTE »,
« LE SECRET », « ET APRÈS ? ») ; "place" = le lieu précis cité dans la phrase (« Lyon, France »), sinon "".
Puis : "title" de la vidéo (40 à 70 caractères, intrigant, jamais trompeur), "hook" (4 à 8 mots affichés en
haut, terminés par 1 emoji), "topic" (1 à 3 mots), "place" (lieu principal), "answers" (2 réponses courtes à la
question finale), "hashtags" (3 à 5), "score" (1 à 10 : intérêt pour le grand public ; promo ou sujet trop
pointu : 3 maximum).
Tout en FRANÇAIS (titre, accroche, mots à l'écran, chapitres, sujet, réponses, hashtags), sauf "visual" en anglais.
Réponds UNIQUEMENT en JSON : {{"score": 7, "title": "...", "hook": "...", "topic": "...", "place": "...", "answers": ["...", "..."], "hashtags": ["..."], "sentences": [{{"n": 1, "visual": "...", "title": "...", "chapter": "...", "place": ""}}]}}"""

_LABEL = re.compile(r"^\s*(?:(?:\d+[.)]\s*|[-•*]\s*|(?:accroche|contexte|conclusion|question|ouverture|"
                    r"rebondissement|déroulé|partie \d+)\s*:\s*))+", re.I)


def split_sentences(text: str) -> list[str]:
    """Texte libre de l'IA → phrases (sans numéros ni étiquettes « Accroche : »), les miettes recollées."""
    text = hooks.strip_emojis(text or "").replace("\u2026", "...").strip().strip('"')
    lines = [_LABEL.sub("", ln).strip(' "«»') for ln in text.splitlines()]
    text = " ".join(ln for ln in lines if ln)
    parts = [p.strip(' "«»') for p in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9«\"])", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(p.split()) < 4 and not out[-1].endswith("?"):
            out[-1] = out[-1].rstrip(".") + ", " + p[0].lower() + p[1:]
        else:
            out.append(p)
    return [p if p[-1] in ".!?" else p + "." for p in out if len(p.split()) >= 2]


def outline(title: str, facts: str, source: str, kind: str, model: str = "") -> dict:
    s = get_settings()
    try:
        raw = llm._ask_ollama(OUTLINE_PROMPT.format(title=title, source=source or "média",
                                                    facts=facts[:5000 if kind == "story" else 3500]),
                              timeout=s.news_llm_timeout, max_tokens=1200, num_ctx=8192, model=model,
                              temperature=0.2)
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.info("Script : plan des faits impossible (%s)", e)
        return {}
    data["faits"] = [str(f).strip() for f in (data.get("faits") or []) if str(f).strip()][:20]
    return data if data["faits"] else {}


def narrate(title: str, plan: dict, facts: str, kind: str, words: float, model: str = "") -> list[str]:
    s = get_settings()
    lines = plan.get("faits") or []
    head = []
    for k, label in (("accroche", "Le plus intrigant"), ("qui", "Qui"), ("ou", "Où"), ("quand", "Quand")):
        if str(plan.get(k) or "").strip():
            head.append(f"{label} : {plan[k]}")
    listed = "\n".join(head + [f"- {f}" for f in lines]) if lines else facts[:3500]
    tpl = STORY_NARRATION if kind == "story" else NEWS_NARRATION
    seconds = s.story_target_seconds if kind == "story" else s.news_target_seconds
    try:
        text = llm._ask_ollama(tpl.format(words=int(words), seconds=int(seconds), title=title, outline=listed,
                                          unknown=str(plan.get("inconnu") or "rien de particulier")),
                               timeout=s.news_llm_timeout, max_tokens=int(words * 2.6) + 200, num_ctx=8192,
                               model=model, json_mode=False, temperature=0.7)
    except Exception as e:
        log.info("Script : récit impossible (%s)", e)
        return []
    return split_sentences(text)


def dress(sentences: list[str], title: str, kind: str) -> Script:
    """Mise en forme (mots-clés d'image, titres à l'écran, chapitres, titre, accroche…) par le modèle rapide."""
    s = get_settings()
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(sentences))
    data: dict = {}
    try:
        raw = llm._ask_ollama(META_PROMPT.format(what="« histoire vraie »" if kind == "story" else "d'actualité",
                                                 title=title, numbered=numbered),
                              timeout=s.news_llm_timeout, max_tokens=400 + 70 * len(sentences), num_ctx=8192)
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.info("Script : mise en forme incomplète (%s)", e)
    per = {}
    for k, row in enumerate(data.get("sentences") or []):
        if isinstance(row, dict):
            try:
                n = int(row.get("n", k + 1)) - 1
            except (TypeError, ValueError):
                n = k
            per.setdefault(n, row)
    rows = []
    for i, t in enumerate(sentences):
        r = per.get(i, {})
        rows.append({"text": t, "visual": str(r.get("visual") or ""), "title": r.get("title") or "",
                     "chapter": r.get("chapter") or "", "place": r.get("place") or ""})
    data["sentences"] = rows
    data.setdefault("title", title)
    if sentences[-1].endswith("?"):
        data["question"] = sentences[-1]
    sc = parse(json.dumps(data, ensure_ascii=False))
    if sc is None:
        sc = Script(title=title, hook="", sentences=list(sentences), visuals=[""] * len(sentences))
    return sc


def write_steps(title: str, facts: str, source: str, kind: str = "news") -> Script | None:
    """Plan des faits dans l'ordre → récit en texte libre (structure imposée) → mise en forme.
    Le récit est écrit par le modèle le plus fort (NEWS_WRITER_MODEL), le reste par le modèle rapide."""
    s = get_settings()
    model = s.news_writer_model if s.news_writer_model and llm.ensure_model(s.news_writer_model) else ""
    words = target_words(kind)
    plan = outline(title, facts, source, kind, model)
    log.info("Script : %d faits retenus, écriture du récit%s…", len(plan.get("faits") or []),
             f" ({model})" if model else "")
    sentences = narrate(title, plan, facts, kind, words, model)
    if len(sentences) < 3:
        return None
    return dress(sentences, title, kind)


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
    removed += frenchify(sc, fallback_title)
    return removed


# ---------- tout en français (le modèle local répond parfois en anglais, surtout si l'article est anglais) ----------

_EN_WORDS = set("""the of and to in is was were are be been who what that this these those with for from by it its
his her their they them has have had did does do not never nobody into about after before behind inside hidden
truth mystery mysterious unexplained unsolved strange story beast how why when where which will would could
should can just most world ever still you your we our my he she there here than then""".split())
_FR_WORDS = set("""le la les de des du un une et est sont en dans qui que pour sur au aux par pas ne il elle ils elles
son sa ses leur leurs ce cet cette ces où quand comment pourquoi jamais personne mystère histoire bête très plus
mais ou avec sans été était être fait nous vous je tu on se lui y à là ça""".split())


def looks_english(text: str) -> bool:
    """Vrai si le texte a l'air écrit en anglais (mots-outils anglais plus nombreux que les mots français)."""
    words = re.findall(r"[a-zà-öø-ÿ']+", (text or "").lower())
    en = sum(w in _EN_WORDS for w in words)
    fr = sum(w in _FR_WORDS for w in words)
    return en >= 2 and en > fr


TRANSLATE_PROMPT = """Traduis en français naturel ce {what} de vidéo TikTok, même sens et à peu près même longueur,
sans guillemets ni hashtag : « {text} »
Réponds UNIQUEMENT en JSON : {{"fr": "..."}}"""


def to_french(text: str, what: str = "titre") -> str:
    """Traduction par l'IA locale ; "" si elle échoue ou répond encore en anglais."""
    s = get_settings()
    try:
        raw = llm._ask_ollama(TRANSLATE_PROMPT.format(what=what, text=text), timeout=min(180, s.news_llm_timeout),
                              max_tokens=120, num_ctx=2048)
        m = re.search(r"\{.*\}", raw or "", re.S)
        fr = str((json.loads(m.group(0)) if m else {}).get("fr") or "").strip().strip('"«» ')
    except Exception as e:
        log.info("Traduction en français impossible (%s)", e)
        return ""
    return "" if not fr or looks_english(fr) else fr


def frenchify(sc: Script, fallback_title: str = "") -> list[str]:
    """Retire ou traduit ce qui est sorti en anglais : phrases lues, titre, accroche, mots à l'écran, chapitres,
    sujet, réponses du quiz. Renvoie ce qui a été corrigé (pour le journal)."""
    fixed = []
    rows = sc.per_sentence()
    keep = [r for r in rows if not looks_english(r[0])]
    if len(keep) < len(rows):
        fixed.append(f"{len(rows) - len(keep)} phrase(s) en anglais")
    sc.set_sentences([(r[0], r[1], "" if looks_english(r[2]) else r[2], "" if looks_english(r[3]) else r[3], r[4])
                      for r in keep])
    if sc.question and looks_english(sc.question):
        last = sc.sentences[-1] if sc.sentences else ""
        sc.question = engage.clean_question(last) if last.endswith("?") else ""
    if sc.title and looks_english(sc.title):
        fixed.append("titre en anglais")
        title = engage.tidy_title(to_french(sc.title))[:90]
        if not title and fallback_title and not looks_english(fallback_title):
            title = engage.tidy_title(fallback_title)[:90]
        if not title and sc.sentences:
            title = engage.tidy_title(" ".join(sc.sentences[0].split()[:10]))[:90]
        sc.title = title
    if sc.hook and looks_english(sc.hook):
        fixed.append("accroche en anglais")
        sc.hook = to_french(sc.hook, "texte d'accroche")[:70]
    if sc.topic and looks_english(sc.topic):
        sc.topic = ""
    sc.answers = [a for a in sc.answers if not looks_english(a)]
    return fixed


WORDS_PER_SECOND = 2.6  # débit de la voix Piper à vitesse 1 (corrigé ensuite par la durée réelle mesurée)


def target_seconds(kind: str = "news") -> float:
    s = get_settings()
    return s.story_target_seconds if kind == "story" else s.news_target_seconds


def min_seconds(kind: str = "news") -> float:
    """Durée minimum d'une vidéo publiée (en dessous, le script est rallongé, sinon le sujet est écarté)."""
    s = get_settings()
    return s.story_min_seconds if kind == "story" else s.news_min_seconds


def target_words(kind: str = "news") -> float:
    """Mots à écrire pour la durée visée."""
    s = get_settings()
    speed = s.story_voice_speed if kind == "story" else s.news_voice_speed
    return target_seconds(kind) * WORDS_PER_SECOND * speed


def extend(sc: Script, title: str, facts: str, source: str, kind: str = "news", need_words: float = 60) -> int:
    """Rallonge le script (le petit modèle local écrit court) : demande des phrases de plus, placées avant la
    question finale. Renvoie le nombre de phrases ajoutées."""
    s = get_settings()
    n = max(3, min(12, round(need_words / 14)))
    prompt = EXTEND_PROMPT.format(what="« histoire vraie »" if kind == "story" else "d'actualité", title=title,
                                  source=source or "média", script=" ".join(sc.sentences)[:3000],
                                  facts=facts[:5000 if kind == "story" else 3000], n=n)
    try:
        more = parse(llm._ask_ollama(prompt, timeout=s.news_llm_timeout, max_tokens=300 + n * 110, num_ctx=8192)
                     .replace('"sentences"', '"score": 0, "sentences"', 1))
    except Exception as e:
        log.warning("Script : rallonge impossible (%s)", e)
        return 0
    if more is None:
        return 0
    rows = sc.per_sentence()
    tail = [rows.pop()] if rows and rows[-1][0].rstrip().endswith("?") else []
    added = 0
    for r in more.per_sentence():
        if _bad(r[0], facts) or r[0].rstrip().endswith("?") or looks_english(r[0]):
            continue
        if any(engage.similarity(r[0], old[0]) >= 0.6 for old in rows):
            continue
        rows.append((r[0], r[1], "" if _bad(r[2], facts) else r[2], r[3], r[4]))
        added += 1
    sc.set_sentences(rows + tail)
    return added


def grow(sc: Script, title: str, facts: str, source: str, kind: str = "news", goal_words: float | None = None,
         tries: int = 3) -> None:
    """Rallonge jusqu'à environ goal_words mots (3 demandes maximum, arrêt si l'IA n'a plus rien à ajouter)."""
    goal = goal_words or target_words(kind)
    for _ in range(tries):
        have = len(sc.text.split())
        if have >= goal * 0.95:
            return
        added = extend(sc, title, facts, source, kind, goal - have)
        log.info("Script : +%d phrase(s) pour atteindre la durée visée (%d → %d mots, objectif %d)", added, have,
                 len(sc.text.split()), goal)
        if not added:
            return


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
    if s.news_script_steps:  # récit en 3 étapes, plus clair et plus naturel ; sinon ancienne méthode
        sc = write_steps(title, facts, source, kind)
        if sc is not None:
            removed = sanitize(sc, facts_all, title)
            if removed:
                log.info("Script : retiré (chiffre absent de l'article) : %s", ", ".join(removed))
            issues = problems(sc, facts_all, kind)
            if not issues or all("trop court" in x for x in issues) and len(sc.sentences) >= 3:
                grow(sc, title, facts_all, source, kind)
                if not problems(sc, facts_all, kind):
                    return sc
            log.info("Script en 3 étapes refusé : %s ; essai à l'ancienne", "; ".join(problems(sc, facts_all, kind)))
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
            grow(sc, title, facts_all, source, kind)
            if not sc.question:
                sc.question = engage.clean_question(sc.sentences[-1]) if sc.sentences[-1].endswith("?") else ""
            return sc
        log.info("Script d'actu refusé (essai %d) : %s", attempt + 1, "; ".join(last))
    return None
