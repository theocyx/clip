"""Images et vidéos d'illustration, uniquement libres de droits :
1. Pexels (vidéos puis photos, licence Pexels : usage libre, sans obligation de crédit), si PEXELS_API_KEY ;
2. Wikimedia Commons (licences libres CC0 / domaine public / CC BY / CC BY-SA, auteur crédité) ;
3. sinon une carte générée (dégradé + mots forts de la phrase).
Aucune image d'article de presse n'est utilisée."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import get_settings
from .feeds import strip_html

log = logging.getLogger(__name__)
W, H = 1080, 1920
UA = "ClipNews/1.0 (self-hosted video tool; python-httpx)"
FREE = re.compile(r"^(cc0|public domain|pd\b|pdm|no restrictions|cc[ -]by(-sa)?([ -]\d(\.\d)?)?\b)", re.I)


@dataclass
class Shot:
    path: Path | None
    kind: str  # video / image (photo en carte 3D) / kinetic (mots animés) / counter (chiffre) / map (survol)
    credit: str = ""  # « Photo : X (licence) — lien » quand la licence demande de citer l'auteur
    ident: str = ""
    text: str = ""  # mots affichés (kinetic), chiffre (counter), nom du lieu (map)
    meta: dict = field(default_factory=dict)  # unit (counter) ; lon, lat, zoom (map)


def _download(url: str, dest: Path, headers: dict | None = None, max_mb: int = 60) -> Path | None:
    try:
        with httpx.stream("GET", url, headers={"User-Agent": UA, **(headers or {})}, follow_redirects=True,
                          timeout=60) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > max_mb << 20:
                        raise RuntimeError("fichier trop gros")
                    f.write(chunk)
        return dest
    except Exception as e:
        log.info("Téléchargement d'illustration raté (%s) : %s", url, e)
        dest.unlink(missing_ok=True)
        return None


# ---------- Pexels ----------

def best_video_file(files: list[dict]) -> dict | None:
    """Fichier MP4 le plus proche de 1080x1920 (portrait d'abord), sans dépasser ~2K."""
    ok = [f for f in files if (f.get("file_type") or "").endswith("mp4") and f.get("width") and f.get("height")
          and f["height"] <= 2600 and f["width"] <= 2600]
    if not ok:
        return None
    return min(ok, key=lambda f: (f["height"] < f["width"], abs(f["height"] - 1920) + abs(f["width"] - 1080)))


def pexels(query: str, work: Path, used: set[str], want_video: bool = True) -> Shot | None:
    key = get_settings().pexels_api_key
    if not key or not query:
        return None
    hdr = {"Authorization": key}
    try:
        if want_video:
            r = httpx.get("https://api.pexels.com/videos/search", headers=hdr, timeout=20,
                          params={"query": query, "orientation": "portrait", "per_page": 12, "size": "medium"})
            r.raise_for_status()
            for v in r.json().get("videos", []):
                ident = f"pexels-v{v.get('id')}"
                f = best_video_file(v.get("video_files") or [])
                if ident in used or not f or (v.get("duration") or 0) < 3:
                    continue
                p = _download(f["link"], work / f"{ident}.mp4")
                if p:
                    used.add(ident)
                    return Shot(p, "video", "", ident)
        r = httpx.get("https://api.pexels.com/v1/search", headers=hdr, timeout=20,
                      params={"query": query, "orientation": "portrait", "per_page": 12})
        r.raise_for_status()
        for ph in r.json().get("photos", []):
            ident = f"pexels-p{ph.get('id')}"
            src = (ph.get("src") or {})
            url = src.get("large2x") or src.get("portrait") or src.get("original")
            if ident in used or not url:
                continue
            p = _download(url, work / f"{ident}.jpg")
            if p:
                used.add(ident)
                return Shot(p, "image", "", ident)
    except Exception as e:
        log.info("Pexels indisponible pour « %s » : %s", query, e)
    return None


# ---------- Wikimedia Commons ----------

def commons_pick(pages: dict, used: set[str]) -> dict | None:
    """Première image sous licence libre, assez grande, pas déjà utilisée."""
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
        ident = f"commons-{page.get('pageid')}"
        if ident in used or not FREE.match(lic) or re.search(r"\b(nc|nd)\b", lic, re.I):
            continue
        if (info.get("width") or 0) < 800 or not (info.get("mime") or "").startswith("image/"):
            continue
        if (info.get("mime") or "") in ("image/svg+xml", "image/gif"):
            continue
        artist = strip_html((meta.get("Artist") or {}).get("value", ""))[:80] or "auteur inconnu"
        return {"id": ident, "url": info.get("thumburl") or info.get("url"), "license": lic, "artist": artist,
                "page": info.get("descriptionurl", "")}
    return None


def commons(query: str, work: Path, used: set[str]) -> Shot | None:
    if not get_settings().news_wikimedia or not query:
        return None
    try:
        r = httpx.get("https://commons.wikimedia.org/w/api.php", headers={"User-Agent": UA}, timeout=20, params={
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 15,
            "gsrsearch": f"filetype:bitmap {query}", "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": 1600})
        r.raise_for_status()
        pick = commons_pick((r.json().get("query") or {}).get("pages") or {}, used)
    except Exception as e:
        log.info("Wikimedia Commons indisponible pour « %s » : %s", query, e)
        return None
    if not pick or not pick["url"]:
        return None
    ext = Path(pick["url"].split("?")[0]).suffix.lower() or ".jpg"
    p = _download(pick["url"], work / f"{pick['id']}{ext}")
    if not p:
        return None
    used.add(pick["id"])
    return Shot(p, "image", f"Image : {pick['artist']} ({pick['license']}), Wikimedia Commons — {pick['page']}",
                pick["id"])


# ---------- cartes générées ----------

def key_words(sentence: str, n: int = 2) -> str:
    """Mots forts de la phrase pour une carte : le chiffre et le mot qui le suit (« 25 MILLIONS »),
    sinon les mots les plus longs, dans l'ordre de la phrase."""
    toks = [t.strip(".,;:!?«»\"'()") for t in sentence.split()]
    toks = [t for t in toks if t]
    if sentence.strip().endswith("?"):  # question finale : on appelle aux commentaires
        return "TON AVIS ?"
    for i, t in enumerate(toks):
        if any(c.isdigit() for c in t) and not (i and toks[i - 1].upper() == "GTA"):
            nxt = toks[i + 1] if i + 1 < len(toks) and len(toks[i + 1]) > 2 else ""
            return f"{t} {nxt}".strip()
    longest = sorted(range(len(toks)), key=lambda i: -len(toks[i]))[:n]
    return " ".join(toks[i] for i in sorted(longest))


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#FFD400").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return 255, 212, 0


def card(text: str, out: Path) -> Shot:
    """Carte 1080x1920 : fond sombre en dégradé avec halo de couleur, mots forts en très gros."""
    from PIL import Image, ImageDraw, ImageFilter

    from ..pipeline.hook import _text_font

    s = get_settings()
    accent = _rgb(s.news_accent)
    img = Image.new("RGB", (W, H), (12, 14, 22))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-300, 500, W + 300, 1500), fill=tuple(int(c * 0.45) for c in accent))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(180)), 0.8)
    d = ImageDraw.Draw(img)
    words = text.upper().split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > 12:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    lines = lines[:4]
    size = 130
    font = _text_font(s.font_name, size)
    while size > 70 and max((d.textlength(x, font=font) for x in lines), default=0) > W - 140:
        size -= 8
        font = _text_font(s.font_name, size)
    y = H // 2 - len(lines) * size * 1.15 / 2 - 80
    for i, line in enumerate(lines):
        x = (W - d.textlength(line, font=font)) / 2
        d.text((x, y + i * size * 1.15), line, font=font, fill=accent if i % 2 == 0 else (255, 255, 255),
               stroke_width=6, stroke_fill=(0, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return Shot(out, "image", "", f"card-{out.stem}")


def frame_image(src: Path, out: Path) -> Path:
    """Image mise au format 1080x1920 : remplie si elle est déjà verticale, sinon posée au centre sur un fond
    flouté de la même image (rien d'important n'est coupé)."""
    from PIL import Image, ImageFilter, ImageOps

    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    if im.height / im.width >= 1.45:
        canvas = ImageOps.fit(im, (W, H), Image.LANCZOS)
    else:
        canvas = ImageOps.fit(im, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
        canvas = Image.eval(canvas, lambda v: int(v * 0.6))
        fg = im.copy()
        fg.thumbnail((W, int(H * 0.62)), Image.LANCZOS)
        if fg.width < W:  # petite image : agrandie à la largeur
            fg = fg.resize((W, round(fg.height * W / fg.width)), Image.LANCZOS)
        canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2 - 60))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=92)
    return out


def find(query: str, sentence: str, work: Path, used: set[str], idx: int, prefer_video: bool = True) -> Shot:
    """Illustration d'une phrase : Pexels, puis Wikimedia Commons, puis carte générée (jamais d'échec)."""
    shot = pexels(query, work, used, prefer_video) or commons(query, work, used)
    if shot is None and query and len(query.split()) > 1:  # requête plus large : premier mot-clé seul
        shot = pexels(query.split()[0], work, used, prefer_video)
    if shot is None:  # pas d'image libre : mots forts animés
        return Shot(None, "kinetic", text=key_words(sentence))
    if shot.kind == "image":
        try:
            from PIL import Image
            with Image.open(shot.path) as im:
                im.verify()
        except Exception as e:
            log.info("Image illisible (%s) : mots animés à la place", e)
            return Shot(None, "kinetic", text=key_words(sentence))
    return shot


def number_shot(sentence: str) -> Shot | None:
    """Plan « chiffre clé » si la phrase contient un nombre parlant (pas une année seule)."""
    from .motion import parse_number

    parsed = parse_number(sentence)
    if not parsed:
        return None
    val, dec, raw = parsed
    toks = [t.strip(".,;:!?«»\"'()") for t in sentence.split()]
    try:
        k = next(i for i, t in enumerate(toks) if raw.split()[0] in t)
    except StopIteration:
        return None
    stop = {"dans", "les", "la", "le", "à", "au", "aux", "en", "sur", "pour", "par", "avec", "ce", "cette", "et",
            "ou", "qui", "que", "selon", "depuis", "contre", "entre", "vers"}
    unit_words = []
    for t in toks[k + len(raw.split()):k + len(raw.split()) + 3]:
        if not t or t.lower() in stop:
            break
        unit_words.append(t)
    while unit_words and unit_words[-1].lower() in ("de", "d", "des", "du"):
        unit_words.pop()
    unit = " ".join(unit_words)[:22]
    if dec == 0 and 1900 <= val <= 2100 and not unit.lower().startswith(("euro", "€", "million", "milliard")):
        return None  # une année se lit mieux en mots
    if val < 2 and not unit:
        return None
    unit = " ".join(unit.split()[:3])
    return Shot(None, "counter", text=raw, meta={"unit": unit})
