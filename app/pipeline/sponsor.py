"""Passages sponsorisés à ne jamais clipper : placement de produit, pub, code promo, autopromo.

Trois sources, de la plus fiable à la plus coûteuse :
1. SponsorBlock (API publique et gratuite, vidéos YouTube) : passages signalés par les spectateurs.
2. La transcription : mots typiques d'une pub (« sponsorisé », « code promo », « lien en description »…).
3. Le texte à l'écran (« sponsorisé », « partenariat »…) lu par OCR local (tesseract) sur quelques images.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

SPONSORBLOCK_URL = "https://sponsor.ajay.app/api/skipSegments"
CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro"]

# Expressions d'une pub ou d'un placement de produit (comparées sans accents, en minuscules).
PATTERNS = [
    r"sponsoris", r"\b(?:le|notre|mon|du) sponso\b", r"\bsponsors? (?:de|du|d'aujourd'hui)",
    r"(?:notre|mon|nos|nouveau|grand) partenaire(?! twitch)", r"partenariat", r"en collaboration avec",
    r"placement de produit", r"communication commerciale", r"contenu (?:sponsorise|promotionnel)",
    r"code promo", r"code de reduction", r"avec mon code",
    r"lien (?:est )?en (?:description|bio)", r"lien (?:dans|juste en dessous|en dessous|sous) (?:la|de la)? ?(?:description|video)",
    r"merci (?:a|aux?) .{1,40} (?:pour (?:avoir )?(?:sponsorise|soutenu|financ)|de sponsoriser|qui sponsorise)",
    r"qui (?:sponsorise|finance) (?:cette|la) video", r"cette video est (?:sponsorisee|offerte|financee)",
    r"(?:-|moins) ?\d{1,2} ?(?:%|pour ?cent) (?:de reduction|sur)", r"offre (?:exclusive|speciale)",
    r"telecharge[rz]? (?:l'appli|l'application|gratuitement)", r"inscri(?:s|vez)[- ]vous (?:gratuitement|via)",
    r"nordvpn", r"surfshark", r"raid shadow legends", r"hellofresh", r"\bbetclic\b", r"\bwinamax\b",
]
_RE = re.compile("|".join(f"(?:{p})" for p in PATTERNS))
# Texte à l'écran : on ne garde que les mentions sans ambiguïté
SCREEN_PATTERNS = re.compile(r"sponsoris|partenariat|placement de produit|communication commerciale|code promo|"
                             r"publicite|paid promotion|\bsponsored\b")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("’", "'")


def is_sponsor_text(text: str) -> bool:
    return bool(_RE.search(_norm(text)))


def merge(ranges: list[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def from_transcript(segments: list[dict], before: float | None = None, after: float | None = None) -> list[tuple[float, float]]:
    """Passages autour des phrases qui ressemblent à une pub (une pub dure souvent 30 s à 2 min)."""
    s = get_settings()
    before = s.sponsor_pad_before if before is None else before
    after = s.sponsor_pad_after if after is None else after
    hits = [(max(0.0, seg["start"] - before), seg["end"] + after)
            for seg in segments if is_sponsor_text(seg.get("text", ""))]
    # plusieurs mentions rapprochées = un seul bloc de pub
    return merge(hits, gap=60)


def youtube_id(key: str) -> str:
    return key.split(":", 1)[1] if key.startswith("youtube:") else ""


def from_sponsorblock(video_key: str, cache: Path | None = None) -> list[tuple[float, float]]:
    """Passages signalés sur SponsorBlock (vide si la vidéo n'est pas sur YouTube ou sans signalement)."""
    vid = youtube_id(video_key)
    if not vid or not get_settings().sponsorblock:
        return []
    if cache and cache.exists():
        try:
            return [tuple(r) for r in json.loads(cache.read_text())]
        except ValueError:
            pass
    try:
        r = httpx.get(SPONSORBLOCK_URL, params={"videoID": vid, "categories": json.dumps(CATEGORIES)}, timeout=15)
        if r.status_code == 404:  # aucun signalement
            ranges = []
        else:
            r.raise_for_status()
            ranges = [tuple(map(float, d["segment"])) for d in r.json() if d.get("actionType", "skip") != "full"]
    except Exception as e:
        log.warning("SponsorBlock indisponible (%s) : on continue sans.", e)
        return []
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(ranges))
        except OSError:
            pass
    if ranges:
        log.info("SponsorBlock : %d passage(s) exclu(s)", len(ranges))
    return ranges


def excluded(video_key: str, segments: list[dict], cache: Path | None = None) -> list[tuple[float, float]]:
    """Tous les passages à exclure (SponsorBlock + transcription)."""
    if not get_settings().exclude_sponsored:
        return []
    return merge(from_sponsorblock(video_key, cache) + from_transcript(segments))


def overlaps(a: float, b: float, ranges: list[tuple[float, float]], tolerance: float = 2.0) -> bool:
    return any(min(b, r1) - max(a, r0) > tolerance for r0, r1 in ranges)


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None and shutil.which("ffmpeg") is not None


def on_screen(src: Path, start: float, end: float, every: float | None = None) -> bool:
    """Vrai si « sponsorisé », « partenariat »… est écrit à l'écran pendant le passage (OCR local, léger)."""
    s = get_settings()
    if not s.sponsor_ocr or not ocr_available():
        return False
    every = every or s.sponsor_ocr_every
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                 "-i", str(src), "-vf", f"fps=1/{every},scale=1280:-2,format=gray", f"{tmp}/f%03d.png"],
                check=True, timeout=300)
        except Exception as e:
            log.debug("Images pour l'OCR impossibles : %s", e)
            return False
        for img in sorted(Path(tmp).glob("f*.png")):
            try:
                out = subprocess.run(["tesseract", str(img), "stdout", "-l", "fra+eng", "--psm", "11"],
                                     capture_output=True, text=True, timeout=60).stdout
            except Exception as e:
                log.debug("OCR échoué : %s", e)
                return False
            if SCREEN_PATTERNS.search(_norm(out)):
                log.info("Texte sponsorisé à l'écran vers %.0f s : passage ignoré", start)
                return True
    return False


def _segments(video_id: int) -> list[dict]:
    p = get_settings().videos_dir / str(video_id) / "transcript.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    return data.get("segments", data) if isinstance(data, dict) else data


def purge_queue(ocr: bool = True, should_stop=None) -> int:
    """Retire de la file les clips pas encore publiés qui tombent dans un passage sponsorisé.

    Chaque clip n'est vérifié qu'une fois (marque « sponsor_checked »). Renvoie le nombre d'envois annulés."""
    from sqlmodel import select

    from .. import db
    from ..models import Clip, ClipInfo, Post, Video

    if not get_settings().exclude_sponsored:
        return 0
    with db.session() as ss:
        rows = ss.exec(select(Clip, Video).join(Video, Video.id == Clip.video_id).join(
            Post, Post.clip_id == Clip.id).where(Post.status == "pending", Clip.status == "ready")).all()
        todo = {c.id: (c, v) for c, v in rows}
    cache: dict[int, list[tuple[float, float]]] = {}
    bad_groups: dict[str, str] = {}
    for clip, video in todo.values():
        if should_stop and should_stop():
            break
        with db.session() as ss:
            info = ss.get(ClipInfo, clip.id)
            data = json.loads(info.data) if info and info.data else {}
        if data.get("sponsor_checked") or clip.group in bad_groups:
            continue
        if video.id not in cache:
            cache[video.id] = excluded(video.key, _segments(video.id),
                                       get_settings().videos_dir / str(video.id) / "sponsor.json")
        reason = ""
        if overlaps(clip.start, clip.end, cache[video.id]):
            reason = "passage sponsorisé (SponsorBlock / transcription)"
        elif is_sponsor_text(data.get("text", "")):
            reason = "pub dans la transcription"
        elif ocr and Path(clip.path).exists() and on_screen(Path(clip.path), 0, clip.end - clip.start):
            reason = "« sponsorisé » écrit à l'écran"
        if reason:
            bad_groups[clip.group] = reason
            continue
        with db.session() as ss:
            info = ss.get(ClipInfo, clip.id) or ClipInfo(clip_id=clip.id)
            info.data = json.dumps({**data, "sponsor_checked": True}, ensure_ascii=False)
            ss.add(info)
            ss.commit()
    n = 0
    with db.session() as ss:
        for group, reason in bad_groups.items():
            for c in ss.exec(select(Clip).where(Clip.group == group)).all():
                for p in ss.exec(select(Post).where(Post.clip_id == c.id, Post.status == "pending")).all():
                    p.status, p.error = "cancelled", f"Retiré : {reason}"
                    ss.add(p)
                    n += 1
            log.info("Clip retiré de la file (%s) : groupe %s", reason, group)
        ss.commit()
    return n
