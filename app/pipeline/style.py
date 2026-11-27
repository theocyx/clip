"""Habillage « magazine » des Shorts (CLIP_STYLE=magazine), relevé le 27/11 image par image sur une chaîne FR
de Shorts GTA 6 qui fait 20 k à 400 k vues par Short :

- jeu en haut (62 %), facecam du streamer en bas sur toute la largeur, sous-titres à la jonction ;
- titre d'ouverture 4 s : petite étiquette de couleur (« GTA 6 ») puis 2-3 lignes blanches très grasses,
  mots clés en jaune, sans boîte ;
- petite ligne de source en haut tout le reste du clip : « titre du live » (stream Untel) ;
- carte « message du chat » au moment fort (vrai message du chat Twitch du live) ;
- carte de crédit en fin de clip : photo ronde du streamer, /pseudo, pastille « SUIS-LE SUR TWITCH » ;
- miniature dessinée (Shorts) : étiquette, titre blanc + fin du titre dans un bloc de couleur, facecam dans un
  cadre arrondi à bord coloré par-dessus l'image du jeu.

Tout est dessiné ici avec Pillow (PNG transparents posés par ffmpeg). Aucune marque, logo ni contenu repris :
seulement des codes de mise en forme."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

from .hook import _font_file, strip_emojis
from .subtitles import GLUE, W

log = logging.getLogger(__name__)

YELLOW = (255, 212, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
# Couleurs des blocs de titre des miniatures (une par clip, tirée du titre)
ACCENTS = [(255, 212, 0), (64, 222, 96), (255, 92, 170), (255, 64, 64), (255, 150, 30), (80, 200, 255)]
PURPLE = (145, 70, 255)
PINK = (236, 72, 153)

TITLE_Y = 150  # haut du titre d'ouverture (zone de jeu / fond flou)
TITLE_SIZE = 82
TITLE_MAX_W = 960
SLIDE = 0.2  # s : les cartes arrivent en glissant de quelques pixels vers le haut


# ---------- Polices et texte ----------

def font(size: int, weight: str = "black", name: str = "Montserrat"):
    """Montserrat Black (ou la graisse la plus proche), sinon DejaVu gras."""
    from PIL import ImageFont

    order = {"black": ("black", "extrabold", "bold"), "bold": ("bold", "semibold"), "regular": ("regular", "medium")}
    for w in order.get(weight, ("bold",)):
        path = _font_file(f"{name}:{w}")
        if path and name.split()[0].lower() in Path(path).name.lower():
            return ImageFont.truetype(path, size)
    path = _font_file("DejaVu Sans:bold")
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


_PUNCT = re.compile(r"^[?!.…,;:]+$")


def tail_start(raw: list[str], n: int = 2) -> int:
    """Début de la fin du titre : les `n` derniers mots qui comptent (+ « ?! » détaché), sans petit mot."""
    i, words = len(raw) - 1, 0
    while i >= 0 and _PUNCT.match(raw[i]):
        i -= 1
    while i >= 0 and words < n:
        if raw[i].lower().strip(".,;:!?") in GLUE:
            break
        words += 1
        i -= 1
    start = i + 1
    return start if start > 0 or len(raw) == 1 else 1


def key_words(text: str) -> list[tuple[str, bool]]:
    """Mots du titre en MAJUSCULES, avec ceux à mettre en couleur : les mots que l'IA a écrits en majuscules
    (« TROP réaliste »), sinon la fin du titre (« ANYME JUGE [GTA 6] », « MICHOU A LEAK LE [GP EXPLORER 4 ?!] »)."""
    raw = strip_emojis(text).replace("«", "").replace("»", "").replace('"', "").split()
    if not raw:
        return []
    marked = [len(re.sub(r"\W", "", w)) >= 3 and re.sub(r"\W", "", w).isupper() and not re.sub(r"\W", "", w).isdigit()
              and re.sub(r"\W", "", w) not in ("GTA", "RP", "FR") for w in raw]
    if any(marked) and not all(marked):
        return [(w.upper(), m) for w, m in zip(raw, marked)]
    start = tail_start(raw)
    return [(w.upper(), k >= start) for k, w in enumerate(raw)]


def wrap(words: list[tuple[str, bool]], fnt, max_w: int, max_lines: int = 3) -> list[list[tuple[str, bool]]]:
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lines, cur = [], []
    for w in words:
        cand = cur + [w]
        if cur and d.textlength(" ".join(x for x, _ in cand), font=fnt) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur = cand
    if cur:
        lines.append(cur)
    # « ?! » jamais seul en début de ligne
    for k in range(1, len(lines)):
        while lines[k] and _PUNCT.match(lines[k][0][0]):
            lines[k - 1].append(lines[k].pop(0))
    lines = [ln for ln in lines if ln]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        lines[-1] = last[:-1] + [(last[-1][0].rstrip(",;:") + "…", last[-1][1])]
    return lines


def _shadow_text(canvas, xy, text, fnt, fill, shadow=12, offset=(0, 6), stroke=3):
    """Texte avec ombre portée floue et fin contour sombre (lisible sur n'importe quelle image de jeu)."""
    from PIL import Image, ImageDraw, ImageFilter

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0] + offset[0], xy[1] + offset[1]), text, font=fnt, fill=(0, 0, 0, 200),
                               stroke_width=stroke + 2, stroke_fill=(0, 0, 0, 200))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(shadow / 2)))
    ImageDraw.Draw(canvas).text(xy, text, font=fnt, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))


# ---------- Titre d'ouverture ----------

def title_png(text: str, tag: str, out: Path, font_name: str = "Montserrat") -> int:
    """Titre d'ouverture : étiquette de couleur + titre blanc, mots clés en jaune. PNG 1080 px de large.

    Renvoie la hauteur de l'image (0 si rien à afficher)."""
    from PIL import Image, ImageDraw

    words = key_words(text)
    if not words:
        return 0
    size = TITLE_SIZE
    fnt = font(size, "black", font_name)
    lines = wrap(words, fnt, TITLE_MAX_W)
    while len(lines) > 2 and size > 62:  # 2 lignes de préférence, comme les titres relevés
        size -= 6
        fnt = font(size, "black", font_name)
        lines = wrap(words, fnt, TITLE_MAX_W)
    tag_f = font(40, "black", font_name)
    line_h = round(size * 1.12)
    tag_h = 58 if tag else 0
    h = 20 + tag_h + line_h * len(lines) + 30
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    y = 14
    if tag:
        tw = d.textlength(tag.upper(), font=tag_f)
        _shadow_text(canvas, ((W - tw) / 2, y), tag.upper(), tag_f, YELLOW, shadow=8, offset=(0, 4), stroke=2)
        y += tag_h
    space = d.textlength(" ", font=fnt)
    for line in lines:
        widths = [d.textlength(w, font=fnt) for w, _ in line]
        x = (W - sum(widths) - space * (len(line) - 1)) / 2
        for (w, hot), ww in zip(line, widths):
            _shadow_text(canvas, (x, y), w, fnt, YELLOW if hot else WHITE)
            x += ww + space
        y += line_h
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return h


# ---------- Cartes ----------

def _circle(img, size: int):
    from PIL import Image, ImageDraw, ImageOps

    img = ImageOps.fit(img.convert("RGB"), (size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def _gradient(size: tuple[int, int], a: tuple, b: tuple, horizontal: bool = True):
    from PIL import Image

    w, h = size
    g = Image.new("RGBA", size)
    px = g.load()
    n = max(1, (w if horizontal else h) - 1)
    for i in range(w if horizontal else h):
        k = i / n
        c = tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3)) + (255,)
        for j in range(h if horizontal else w):
            px[(i, j) if horizontal else (j, i)] = c
    return g


def initials_avatar(name: str, size: int = 260):
    from PIL import Image, ImageDraw

    img = _gradient((size, size), PURPLE, PINK).convert("RGB")
    d = ImageDraw.Draw(img)
    letter = (re.sub(r"\W", "", name or "?")[:1] or "?").upper()
    f = font(round(size * 0.5), "black")
    d.text((size / 2, size / 2), letter, font=f, fill=WHITE, anchor="mm")
    return img


def credit_png(name: str, handle: str, platform: str, avatar, out: Path, font_name: str = "Montserrat") -> int:
    """Carte de fin : photo ronde (anneau dégradé), /pseudo en gros, pastille « SUIS-LE SUR TWITCH ».
    Renvoie la hauteur de l'image (0 si pas de nom)."""
    from PIL import Image, ImageDraw

    if not (name or handle):
        return 0
    av = 250
    ring = 12
    h = av + 2 * ring + 230
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    cx = W // 2
    disc = _circle(_gradient((av + 2 * ring, av + 2 * ring), PINK, PURPLE), av + 2 * ring)
    canvas.alpha_composite(disc, (cx - disc.width // 2, 0))
    canvas.alpha_composite(_circle(avatar or initials_avatar(name, av), av), (cx - av // 2, ring))
    d = ImageDraw.Draw(canvas)
    big = font(76, "black", font_name)
    label = "/" + (handle or re.sub(r"\s+", "", name)).lower()
    badge_txt = {"twitch": "TWITCH", "youtube": "YOUTUBE"}.get(platform, "")
    badge_f = font(30, "black", font_name)
    bw = d.textlength(badge_txt, font=badge_f) + 30 if badge_txt else 0
    tw = d.textlength(label, font=big)
    y = av + 2 * ring + 18
    x = cx - (tw + bw + (14 if bw else 0)) / 2
    if bw:
        col = PURPLE if platform == "twitch" else (230, 33, 23)
        d.rounded_rectangle((x, y + 22, x + bw, y + 70), radius=12, fill=col + (255,))
        d.text((x + bw / 2, y + 46), badge_txt, font=badge_f, fill=WHITE, anchor="mm")
        x += bw + 14
    _shadow_text(canvas, (x, y), label, big, WHITE, shadow=10, offset=(0, 5), stroke=3)
    pill = "SUIS-LE SUR TWITCH" if platform == "twitch" else "VA VOIR SA CHAÎNE"
    pf = font(36, "black", font_name)
    pw = d.textlength(pill, font=pf) + 64
    py = y + 104
    grad = _gradient((int(pw), 68), PURPLE, PINK)
    mask = Image.new("L", grad.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, grad.width - 1, grad.height - 1), radius=34, fill=255)
    canvas.paste(grad, (int(cx - pw / 2), py), mask)
    d.text((cx, py + 34), pill, font=pf, fill=WHITE, anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return h


def chat_card_png(user: str, message: str, out: Path, font_name: str = "Montserrat") -> int:
    """Carte « message du chat » : fond sombre arrondi à bord violet, étiquette jaune VIEWER, pseudo, message en
    majuscules. Renvoie la hauteur de l'image (0 si rien à afficher)."""
    from PIL import Image, ImageDraw

    message = strip_emojis(message).strip()
    if not message:
        return 0
    card_w, pad = 880, 34
    mf = font(48, "black", font_name)
    lines = wrap([(w.upper(), False) for w in message.split()], mf, card_w - 2 * pad, max_lines=2)
    line_h = 60
    card_h = 150 + line_h * len(lines) + 26
    h = card_h + 40
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    x0, y0 = (W - card_w) // 2, 28
    border = _gradient((card_w + 8, card_h + 8), PURPLE, PINK)
    mask = Image.new("L", border.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, border.width - 1, border.height - 1), radius=34, fill=255)
    canvas.paste(border, (x0 - 4, y0 - 4), mask)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), radius=30, fill=(28, 22, 40, 245))
    tf = font(28, "black", font_name)
    tw = d.textlength("VIEWER", font=tf) + 34
    d.rounded_rectangle((x0 + 30, y0 - 22, x0 + 30 + tw, y0 + 22), radius=12, fill=YELLOW + (255,))
    d.text((x0 + 30 + tw / 2, y0), "VIEWER", font=tf, fill=(30, 20, 0), anchor="mm")
    av = 70
    canvas.alpha_composite(_circle(initials_avatar(user, av), av), (x0 + pad, y0 + 42))
    uf, sf = font(36, "black", font_name), font(26, "bold", font_name)
    d.text((x0 + pad + av + 18, y0 + 44), (user or "viewer")[:22], font=uf, fill=(196, 160, 255))
    d.text((x0 + pad + av + 18, y0 + 88), "à l'instant · chat du live", font=sf, fill=(170, 165, 185))
    y = y0 + 140
    for line in lines:
        d.text((x0 + pad, y), " ".join(w for w, _ in line), font=mf, fill=WHITE)
        y += line_h
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return h


# ---------- Message du chat à montrer ----------

_URL = re.compile(r"https?://|www\.|\.(com|fr|tv|gg)\b", re.I)
# Messages jamais montrés à l'écran (insultes, haine, sexe, attaques) : mieux vaut pas de carte qu'un dérapage
_BAD = re.compile(
    r"\b(pd|pédé|pede|tapette|encul|nègre|negre|bougnoul|youpin|bicot|niqu|nik|ntm|fdp|tg\b|ta gueule|salope|"
    r"pute|connard|conasse|batard|bâtard|abruti|débile|attard|mongol|cancer|suicide|viol|nazi|hitler|porn|sexe|"
    r"bite|chatte|couilles?|branl|suce|sucer|nude|nudes|onlyfans|ban\b|unban|timeout|mod\b|modo|prime|sub|"
    r"bits?\b|follow|donation|dons?\b|€|\$)", re.I)
_WORD = re.compile(r"[a-zàâäéèêëïîôöùûüç']{2,}", re.I)


def good_message(text: str) -> bool:
    t = (text or "").strip()
    if not 8 <= len(t) <= 70 or _URL.search(t) or "@" in t or t.startswith("!") or _BAD.search(t):
        return False
    words = _WORD.findall(t)
    if len(words) < 3:
        return False
    # emotes Twitch (KEKW, LUL, OMEGALUL…) et spam : au moins 3 vrais mots en minuscules / mixtes
    real = [w for w in words if not (w.isupper() and len(w) > 2)]
    return len(real) >= 3 and len(set(w.lower() for w in words)) >= 3


def pick_message(messages: list[tuple[float, str, str]]) -> tuple[str, str] | None:
    """Message le plus représentatif parmi ceux du moment fort : le premier message propre qui ressemble aux
    autres (beaucoup de spectateurs réagissent pareil), sinon le premier propre. (pseudo, texte) ou None."""
    clean = [(u, t) for _, u, t in messages if good_message(t)]
    if not clean:
        return None
    vocab: dict[str, int] = {}
    for _, _, t in messages:
        for w in set(x.lower() for x in _WORD.findall(t)):
            vocab[w] = vocab.get(w, 0) + 1
    best = max(clean, key=lambda m: sum(vocab.get(w.lower(), 0) for w in set(_WORD.findall(m[1]))) / (
        1 + len(_WORD.findall(m[1])) * 0.15))
    return best


# ---------- Photo du streamer (Twitch) ----------

def twitch_avatar(login: str, cache_dir: Path, max_age_days: float = 7):
    """Photo de profil Twitch (même client public que la détection des lives), en cache. None si impossible."""
    from PIL import Image

    if not login or not re.fullmatch(r"[a-z0-9_]{2,30}", login):
        return None
    path = cache_dir / f"{login}.png"
    try:
        if path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400:
            return Image.open(path).convert("RGB")
        import httpx

        from .. import gta6

        with httpx.Client(timeout=10, headers={"Client-Id": gta6.TWITCH_CLIENT_ID}, follow_redirects=True) as http:
            r = http.post(gta6.TWITCH_GQL, json={"query": "query($l:String!){user(login:$l){profileImageURL(width:300)}}",
                                                 "variables": {"l": login}})
            r.raise_for_status()
            url = ((r.json().get("data") or {}).get("user") or {}).get("profileImageURL") or ""
            if not url.startswith("https://"):
                return None
            img = http.get(url)
            img.raise_for_status()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img.content)
        return Image.open(path).convert("RGB")
    except Exception as e:
        log.debug("Photo Twitch de %s indisponible : %s", login, e)
        try:
            return Image.open(path).convert("RGB") if path.exists() else None
        except Exception:
            return None


# ---------- Miniature ----------

def accent_for(text: str) -> tuple[int, int, int]:
    return ACCENTS[int(hashlib.md5((text or "").encode()).hexdigest(), 16) % len(ACCENTS)]


def thumbnail(game, cam, text: str, tag: str, out: Path, font_name: str = "Montserrat") -> Path | None:
    """Miniature 1080x1920 : image du jeu, étiquette, titre blanc dont la fin est dans un bloc de couleur,
    facecam dans un cadre arrondi à bord de la même couleur, légèrement penchée."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    words = key_words(text)
    if game is None or not words:
        return None
    H = 1920
    bg = ImageOps.fit(game.convert("RGB"), (W, H), Image.LANCZOS, centering=(0.5, 0.45))
    bg = ImageEnhance.Contrast(ImageEnhance.Color(bg).enhance(1.25)).enhance(1.08).convert("RGBA")
    shade = Image.new("L", (1, H))
    for y in range(H):  # haut assombri pour le titre, bas un peu aussi
        k = max(0.0, 1 - y / 760) * 0.78 + max(0.0, (y - 1450) / 470) * 0.45
        shade.putpixel((0, y), round(255 * min(0.85, k)))
    bg.alpha_composite(Image.merge("RGBA", (*(Image.new("L", (W, H), 0),) * 3, shade.resize((W, H)))))
    accent = accent_for(text)
    dark = accent in (ACCENTS[3],)  # rouge : texte blanc dans le bloc
    d = ImageDraw.Draw(bg)
    y = 120
    if tag:
        tf = font(56, "black", font_name)
        tw = d.textlength(tag.upper(), font=tf)
        _shadow_text(bg, ((W - tw) / 2, y), tag.upper(), tf, accent, shadow=10, offset=(0, 5), stroke=3)
        y += 84
    size = 104
    fnt = font(size, "black", font_name)
    # fin du titre dans le bloc de couleur (« ... [DANS LE JEU ?!] »), le reste en blanc au-dessus
    hot_from = tail_start([w for w, _ in words])
    while True:
        top_lines = wrap(words[:hot_from], fnt, 980, max_lines=9) if hot_from else []
        box_lines = wrap(words[hot_from:], fnt, 900, max_lines=9)
        if len(top_lines) + len(box_lines) <= 3 or size <= 70:
            break
        size -= 8
        fnt = font(size, "black", font_name)
    top_lines = top_lines[:2]
    box_lines = box_lines[:2]
    line_h = round(size * 1.08)
    for line in top_lines:
        s = " ".join(w for w, _ in line)
        _shadow_text(bg, ((W - d.textlength(s, font=fnt)) / 2, y), s, fnt, WHITE, shadow=14, offset=(0, 7), stroke=4)
        y += line_h
    y += 10
    for line in box_lines:
        s = " ".join(w for w, _ in line)
        tw = d.textlength(s, font=fnt)
        x0 = (W - tw) / 2 - 26
        d.rounded_rectangle((x0, y - 4, x0 + tw + 52, y + line_h + 6), radius=18, fill=accent + (255,))
        d.text((x0 + 26, y + 2), s, font=fnt, fill=WHITE if dark else (14, 14, 14))
        y += line_h + 16
    if cam is not None:
        cw = 700
        ch = max(10, round(cw * cam.height / max(1, cam.width)))
        ch = min(ch, 620)
        face = ImageOps.fit(cam.convert("RGB"), (cw, ch), Image.LANCZOS)
        b = 12
        frame = Image.new("RGBA", (cw + 2 * b, ch + 2 * b), (0, 0, 0, 0))
        fd = ImageDraw.Draw(frame)
        fd.rounded_rectangle((0, 0, frame.width - 1, frame.height - 1), radius=44, fill=accent + (255,))
        mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=34, fill=255)
        frame.paste(face, (b, b), mask)
        angle = -4 if accent_for(text + "a") in ACCENTS[:3] else 4
        frame = frame.rotate(angle, resample=Image.BICUBIC, expand=True)
        shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        shadow.putalpha(frame.getchannel("A").point(lambda a: 170 if a else 0))
        shadow = shadow.filter(ImageFilter.GaussianBlur(18))
        py = max(y + 60, 1060)
        py = min(py, H - frame.height - 80)
        px = (W - frame.width) // 2
        bg.alpha_composite(shadow, (px, py + 16))
        bg.alpha_composite(frame, (px, py))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    if out.stat().st_size > 1_900_000:  # limite YouTube : 2 Mo
        bg.convert("RGB").save(out, "JPEG", quality=72, optimize=True)
    return out


def tag_for(title: str, text: str = "", streamer: str = "") -> str:
    """Étiquette au-dessus du titre : « GTA 6 » sur tout contenu GTA, sinon le nom du streamer."""
    if re.search(r"\bgta\b|gta ?(6|vi)\b|vice city|grand theft", f"{title} {text}", re.I):
        return "GTA 6"
    return streamer.upper()[:24]
