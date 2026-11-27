"""Sous-titres dynamiques (style TikTok) au format ASS : 1 à 3 mots en majuscules blanches à contour noir,
vers 70 % de la hauteur (dans le jeu), mot prononcé en jaune."""
from __future__ import annotations

W, H = 1080, 1920

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,{sub_v},1
Style: Part,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,8,60,60,{part_v},1
Style: Hook,{font},64,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{hook_v},1
Style: Ask,{font},70,&H00000000,&H00000000,&H0000D7FF,&H0000D7FF,-1,0,0,0,100,100,0,0,3,20,0,5,90,90,0,1
Style: Cta,{font},56,&H00FFFFFF,&H00FFFFFF,&H002A2AE6,&H002A2AE6,-1,0,0,0,100,100,0,0,3,16,0,2,90,90,300,1
Style: Src,{font},30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,2,1,8,50,50,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
import re

HIGHLIGHT = r"{\c&H00E5FF&\fscx115\fscy115}"  # mot prononcé : jaune et un peu plus gros (« pop »)
RESET = r"{\c&HFFFFFF&\fscx100\fscy100}"
KEYWORD = r"{\c&H3CFF5A&}"  # mots forts en vert, pour accrocher l'œil même hors du mot prononcé
KEYWORD_RESET = r"{\c&HFFFFFF&}"
# Mots qui portent l'émotion ou l'enjeu (nombres et sommes d'argent aussi)
EMPHASIS = {
    "mdr", "ptdr", "wow", "waouh", "putain", "incroyable", "dingue", "fou", "folle", "folie", "jamais", "sérieux",
    "énorme", "bordel", "insane", "omg", "gg", "impossible", "mort", "morts", "tué", "police", "flic", "flics",
    "argent", "million", "millions", "milliard", "euros", "dollars", "gagné", "perdu", "record", "première",
    "premier", "dernier", "dernière", "secret", "braquage", "banque", "prison", "arrêté", "explose", "explosé",
    "catastrophe", "terrible", "magnifique", "parfait", "légendaire", "choqué", "hallucine", "gta", "gta6",
}
_TOKEN = re.compile(r"[\wàâäéèêëïîôöùûüç']+", re.I)
# Petits mots qui ne doivent pas finir un groupe à l'écran (« LE » seul en fin de ligne se lit mal) :
# ils passent au groupe suivant, avec le mot qu'ils annoncent
GLUE = {"le", "la", "les", "l'", "un", "une", "des", "de", "du", "d'", "à", "au", "aux", "en", "et", "ou", "mais",
        "que", "qu'", "qui", "ce", "cet", "cette", "ces", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses",
        "notre", "votre", "leur", "leurs", "je", "j'", "tu", "il", "elle", "on", "ils", "elles", "nous", "vous",
        "pour", "par", "sur", "dans", "avec", "sans", "chez", "vers", "ne", "n'", "se", "s'", "c'", "très", "plus"}


def is_glue(word: str) -> bool:
    return word.strip(".,;:!?«»\"").lower() in GLUE
NL = r"\N"  # retour à la ligne ASS


def ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").upper()


def words_in(segments: list[dict], a: float, b: float) -> list[dict]:
    out = []
    for seg in segments:
        if seg["end"] < a or seg["start"] > b:
            continue
        for w in seg.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            if a <= mid <= b:
                out.append({"start": w["start"] - a, "end": w["end"] - a, "word": w["word"]})
    return out


def is_keyword(word: str) -> bool:
    w = word.strip().lower()
    if any(c.isdigit() for c in w) or "€" in w or "$" in w:
        return True
    return any(tok in EMPHASIS for tok in _TOKEN.findall(w))


def words_in_pieces(segments: list[dict], pieces: list[tuple[float, float]]) -> list[list[dict]]:
    """Mots de chaque morceau monté bout à bout, en temps de la vidéo finale."""
    out, offset = [], 0.0
    for a, b in pieces:
        ws = words_in(segments, a, b)
        for w in ws:
            w["start"] = max(0.0, w["start"]) + offset
            w["end"] = min(w["end"], b - a) + offset
        out.append(ws)
        offset += b - a
    return out


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 18, max_pause: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            text_len = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            pause = w["start"] - cur[-1]["end"]
            ends = cur[-1]["word"].endswith((".", "!", "?", ","))
            if ends or pause > max_pause:
                groups.append(cur)
                cur = []
            elif len(cur) >= max_words or text_len > max_chars:
                carry = [cur.pop()] if len(cur) > 1 and is_glue(cur[-1]["word"]) else []
                groups.append(cur)
                cur = carry
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def hook_lines(text: str, per_line: int = 18, max_lines: int = 2) -> list[str]:
    """Accroche courte découpée en lignes (le rendu ASS ne coupe pas tout seul)."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(",;:-") + "…"
    return lines


POP_MS = 110  # durée du rebond quand un groupe de mots apparaît


def _word_text(word: str, active: bool, keywords: bool = True, pop: bool = False) -> str:
    """pop : 1re image du groupe, chaque mot grandit de 80 % à sa taille en POP_MS (effet « rebond »)."""
    if pop:
        grow = rf"\t(0,{POP_MS},\fscx{{to}}\fscy{{to}})"
        if active:
            return (r"{\c&H00E5FF&\fscx90\fscy90" + grow.format(to=115) + "}" + _escape(word)
                    + r"{\c&HFFFFFF&}")
        color = r"\c&H3CFF5A&" if keywords and is_keyword(word) else r"\c&HFFFFFF&"
        return "{" + color + r"\fscx80\fscy80" + grow.format(to=100) + "}" + _escape(word) + r"{\c&HFFFFFF&}"
    if active:
        return HIGHLIGHT + _escape(word) + RESET
    if keywords and is_keyword(word):
        return KEYWORD + _escape(word) + KEYWORD_RESET
    return _escape(word)


def build_ass(segments: list[dict], a: float, b: float, font: str, part_label: str = "",
              hook: str = "", hook_seconds: float = 0, hook_in_ass: bool = True, top: int = 0,
              pieces: list[tuple[float, float]] | None = None, end_text: str = "",
              end_seconds: float = 3.5, keywords: bool = True, pop: bool = False, cta: str = "",
              cta_seconds: float = 3.0, sub_v: int = 540, source: str = "", source_from: float = 0) -> str:
    """hook_in_ass=False : l'accroche est rendue à part en image (emojis couleur), on garde juste la place.

    top : décalage vers le bas de l'accroche et de « Partie x/y » (format split : sous la facecam).
    pieces : morceaux de la source montés bout à bout (extrait d'ouverture + clip) ; par défaut [(a, b)].
    end_text : question / appel à commenter affiché au milieu de l'écran pendant les `end_seconds` dernières s.
    keywords : mots forts en vert (sinon seul le mot prononcé change de couleur).
    pop : chaque groupe de mots apparaît avec un petit rebond.
    cta : appel à s'abonner (pastille rouge sous les sous-titres) pendant les `cta_seconds` dernières secondes.
    sub_v : marge du bas des sous-titres (540 : vers 70 % de la hauteur ; style magazine : jonction jeu / facecam).
    source : petite ligne en haut (« titre du live » (stream Untel)) à partir de `source_from` s."""
    pieces = pieces or [(a, b)]
    dur = sum(y - x for x, y in pieces)
    hl = hook_lines(hook) if hook else []
    part_v = (170 + 115 * len(hl) + 40 if hl else 180) + top
    lines = [HEADER.format(w=W, h=H, font=font, part_v=part_v, hook_v=170 + top, sub_v=int(sub_v))]
    if source and dur > source_from + 1:
        lines.append(f"Dialogue: 1,{ts(source_from)},{ts(dur)},Src,,0,0,0,,{{\\fad(200,0)}}"
                     + source.replace("\\", "").replace("{", "(").replace("}", ")"))
    if hl and hook_in_ass:
        end = min(hook_seconds, dur) if hook_seconds > 0 else dur
        lines.append(f"Dialogue: 2,{ts(0)},{ts(end)},Hook,,0,0,0,,{NL.join(_escape(x) for x in hl)}")
    if part_label:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(dur)},Part,,0,0,0,,{_escape(part_label)}")
    if end_text and dur > end_seconds + 2:
        ask = NL.join(_escape(x) for x in hook_lines(end_text, per_line=20, max_lines=3))
        lines.append(f"Dialogue: 3,{ts(dur - end_seconds)},{ts(dur)},Ask,,0,0,0,,"
                     rf"{{\fad(150,0)\t(0,180,\fscx108\fscy108)\t(180,320,\fscx100\fscy100)}}{ask}")
    if cta and dur > cta_seconds + 5:
        txt = NL.join(_escape(x) for x in hook_lines(cta, per_line=20, max_lines=2))
        lines.append(f"Dialogue: 4,{ts(dur - cta_seconds)},{ts(dur)},Cta,,0,0,0,,"
                     rf"{{\fad(120,0)\t(0,160,\fscx112\fscy112)\t(160,300,\fscx100\fscy100)}}{txt}")
    groups = [g for ws in words_in_pieces(segments, pieces) for g in group_words(ws)]
    for gi, g in enumerate(groups):
        g_end = g[-1]["end"]
        if gi + 1 < len(groups):
            g_end = min(max(g_end, groups[gi + 1][0]["start"]), g_end + 0.5)
        for i, w in enumerate(g):
            start = w["start"] if i else g[0]["start"]
            end = g[i + 1]["start"] if i + 1 < len(g) else g_end
            if end <= start:
                end = start + 0.05
            text = " ".join(_word_text(x["word"], j == i, keywords, pop=pop and i == 0) for j, x in enumerate(g))
            lines.append(f"Dialogue: 0,{ts(start)},{ts(min(end, dur))},Sub,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"
