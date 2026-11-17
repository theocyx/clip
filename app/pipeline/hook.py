"""Accroche en haut du clip, rendue en image (PNG) pour afficher les emojis en couleur.

libass (sous-titres ASS de ffmpeg) ne sait pas dessiner les emojis couleur : l'accroche est donc
dessinée avec Pillow (texte + police Noto Color Emoji) puis superposée à la vidéo par ffmpeg.
"""
from __future__ import annotations

import functools
import logging
import re
import subprocess
from pathlib import Path

from .subtitles import W, hook_lines

log = logging.getLogger(__name__)

TOP = 170  # même position que l'ancien style ASS « Hook »
LINE_H = 115
PAD_X = 18
TEXT_SIZE = 64
EMOJI_FONT_SIZE = 109  # Noto Color Emoji n'existe qu'en 109 px, on redimensionne ensuite

# Plages Unicode des emojis (suffisant pour ceux qu'écrit le LLM)
_EMOJI = (r"\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿"
          r"⤴⤵〰〽㊗㊙")
EMOJI_RE = re.compile(f"[{_EMOJI}][️\U0001F3FB-\U0001F3FF]*")
_JOINERS = re.compile("[‍︎️\U0001F3FB-\U0001F3FF⃣]")

# Emojis de secours selon le contenu, si l'IA n'en a pas mis (ou si elle est désactivée)
KEYWORDS: list[tuple[str, str]] = [
    (r"mdr|ptdr|lol|rire|rigol|drôle|marrant|blague|hilarant", "😂"),
    (r"peur|flipp|horreur|terrifi|cri", "😱"),
    (r"incroyable|dingue|fou|ouf|choqu|hallucin|jamais vu|impossible", "🤯"),
    (r"énerv|rage|colère|insult|clash|embrouille|vénère", "😡"),
    (r"triste|pleur|larme|émouv|ému", "😢"),
    (r"argent|€|euros?|dollars?|riche|payé|salaire|cher", "💰"),
    (r"gagn|victoire|champion|record|top 1|win", "🏆"),
    (r"mort|tué|kill|crève|rip", "💀"),
    (r"jeu|game|gamer|partie|manette|fortnite|minecraft|gta|valorant|lol\b", "🎮"),
    (r"bouffe|manger|repas|cuisine|pizza|burger|faim", "🍔"),
    (r"amour|crush|bisou|couple|love|meuf|copine|copain", "❤️"),
    (r"foot|but|match|ballon", "⚽"),
    (r"voiture|vitesse|course|moto", "🏎️"),
    (r"secret|vérité|avoue|révél|confess", "🤫"),
    (r"chaud|feu|incendie|brûl|hype|énorme", "🔥"),
]
DEFAULT_EMOJI = "👀"


def emojis_in(text: str) -> list[str]:
    return EMOJI_RE.findall(text or "")


def strip_emojis(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _JOINERS.sub("", EMOJI_RE.sub("", text or ""))).strip()


def add_emojis(hook: str, context: str = "", max_emojis: int = 2) -> str:
    """Garde au plus `max_emojis` emojis (ceux de l'IA), sinon en choisit selon le contenu."""
    found = emojis_in(hook)[:max_emojis]
    text = strip_emojis(hook)
    if not text:
        return ""
    if not found:
        hay = f"{text} {context}".lower()
        for pattern, emoji in KEYWORDS:
            if re.search(pattern, hay):
                found = [emoji]
                break
        else:
            found = [DEFAULT_EMOJI]
    return f"{text} {''.join(found)}"


@functools.lru_cache
def _font_file(query: str) -> str | None:
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", query], capture_output=True, text=True, timeout=10)
        path = out.stdout.strip()
        return path if path and Path(path).is_file() else None
    except Exception:
        return None


def _fonts(font_name: str):
    from PIL import ImageFont

    text_path = _font_file(f"{font_name}:bold") or _font_file("DejaVu Sans:bold")
    text = ImageFont.truetype(text_path, TEXT_SIZE) if text_path else ImageFont.load_default(TEXT_SIZE)
    emoji_path = _font_file("Noto Color Emoji")
    emoji = None
    if emoji_path:
        try:
            emoji = ImageFont.truetype(emoji_path, EMOJI_FONT_SIZE)
        except OSError:
            log.warning("Police emoji illisible : %s", emoji_path)
    return text, emoji


def _runs(line: str) -> list[tuple[bool, str]]:
    """Découpe une ligne en morceaux texte / emoji."""
    runs, pos = [], 0
    for m in EMOJI_RE.finditer(line):
        if m.start() > pos:
            runs.append((False, line[pos:m.start()]))
        runs.append((True, m.group(0)))
        pos = m.end()
    if pos < len(line):
        runs.append((False, line[pos:]))
    return runs


def _emoji_img(emoji_font, char: str, size: int):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (EMOJI_FONT_SIZE * 2, EMOJI_FONT_SIZE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((0, 0), char, font=emoji_font, embedded_color=True)
    box = img.getbbox()
    if not box:
        return None
    img = img.crop(box)
    return img.resize((max(1, round(img.width * size / img.height)), size), Image.LANCZOS)


def render_png(hook: str, font_name: str, out: Path) -> int:
    """Dessine l'accroche (MAJUSCULES, bandeaux noirs) en PNG transparent 1080 px de large.

    Renvoie le nombre de lignes (0 si rien à afficher)."""
    from PIL import Image, ImageDraw

    text_font, emoji_font = _fonts(font_name)
    if emoji_font is None:
        hook = strip_emojis(hook)
    lines = hook_lines(hook)
    if not lines:
        return 0
    asc, desc = text_font.getmetrics()
    emoji_size = round(asc * 1.05)
    canvas = Image.new("RGBA", (W, TOP + LINE_H * len(lines) + 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for i, line in enumerate(lines):
        pieces = []  # (largeur, objet)
        for is_emoji, part in _runs(line.upper()):
            if is_emoji:
                img = _emoji_img(emoji_font, _JOINERS.sub("", part)[:1] or part, emoji_size)
                if img:
                    pieces.append((img.width + 6, img))
            elif part:
                pieces.append((round(draw.textlength(part, font=text_font)), part))
        width = sum(w for w, _ in pieces)
        x = (W - width) // 2
        y = TOP + i * LINE_H
        y0 = y - (LINE_H - asc - desc) // 2  # bandeaux collés d'une ligne à l'autre
        draw.rectangle((x - PAD_X, y0, x + width + PAD_X, y0 + LINE_H - 1), fill=(0, 0, 0, 255))
        for w, obj in pieces:
            if isinstance(obj, str):
                draw.text((x, y), obj, font=text_font, fill=(255, 255, 255, 255))
            else:
                canvas.alpha_composite(obj, (x + 3, y + (asc - obj.height) // 2 + desc // 2))
            x += w
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return len(lines)
