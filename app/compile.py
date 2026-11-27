"""Compilations longues hebdomadaires : « TOP 15 des meilleurs moments GTA 6 de la semaine ».

Pourquoi : les Shorts ne comptent pas dans les 4000 heures de visionnage exigées pour la monétisation, seules les
vidéos longues comptent. Chaque semaine (COMPIL_DAY / COMPIL_HOUR), les Shorts qui ont fait le plus de vues sont
remontés en une vidéo 16:9 de 5 à 15 min (clip vertical au centre sur un fond flouté, numéro et titre sur les
côtés, du moins bon au meilleur pour garder le public jusqu'au bout), avec chapitres, crédits des streamers et
miniature, puis publiés comme une vidéo normale (pas un Short).

Les vidéos longues sont rangées sous la plateforme « youtube_long » : elles ne prennent pas de place dans le quota
de Shorts du jour et ne faussent pas l'apprentissage des Shorts."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import textwrap
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from . import db, growth, notify, platforms
from .config import get_settings
from .models import Clip, ClipInfo, Post, PostStat, Source, Video, now
from .pipeline import media

log = logging.getLogger(__name__)

PREFIX = "compil:"
ON_KEY, NOW_KEY = "compil_on", "compil_now"
W, H = 1920, 1080
FG_W = 608  # clip vertical 1080x1920 ramené à 1080 px de haut
SIDE = (W - FG_W) // 2
TRIES = 3
_busy = threading.Lock()


# ---------- réglages ----------

def enabled() -> bool:
    v = platforms._get(ON_KEY)
    return get_settings().compil if v is None else v == "1"


def set_enabled(on: bool) -> None:
    platforms._put(ON_KEY, "1" if on else "0")


def kinds() -> list[str]:
    return [k.strip() for k in (get_settings().compil_kinds or "").split(",") if k.strip() in growth.KINDS
            and k.strip() != "compil"]


def request_now(kind: str = "") -> None:
    """Bouton « Compilation maintenant » : montée au prochain passage (≤ 5 min), même si ce n'est pas le jour."""
    platforms._put(NOW_KEY, kind or (kinds() or ["clip"])[0])


def _local(at: datetime | None = None) -> datetime:
    tz = ZoneInfo(get_settings().timezone)
    return at.astimezone(tz) if at else datetime.now(tz)


def week_key(kind: str, at: datetime | None = None) -> str:
    y, w, _ = _local(at).isocalendar()
    return f"{PREFIX}{kind}:{y}-W{w:02d}"


def exists(key: str) -> bool:
    with db.session() as s:
        return s.exec(select(Video.id).where(Video.key == key)).first() is not None


def due(kind: str, at: datetime | None = None) -> bool:
    cfg = get_settings()
    t = _local(at)
    if not enabled() or t.weekday() != cfg.compil_day % 7 or t.hour < cfg.compil_hour:
        return False
    key = week_key(kind, at)
    return not exists(key) and platforms._get("compil_skip:" + key) is None


# ---------- choix des Shorts ----------

def used_clips(kind: str) -> set[int]:
    """Shorts déjà mis dans une compilation de ce type (jamais deux fois)."""
    out: set[int] = set()
    with db.session() as s:
        for info in s.exec(select(ClipInfo).join(Clip, Clip.id == ClipInfo.clip_id).join(
                Video, Video.id == Clip.video_id).where(col(Video.key).startswith(PREFIX + kind + ":"))).all():
            try:
                out.update(json.loads(info.data).get("items") or [])
            except ValueError:
                pass
    return out


def candidates(kind: str, days: float) -> list[dict]:
    """Shorts YouTube de ce type publiés depuis `days` jours dont le fichier existe encore, les plus vus d'abord."""
    since = now() - timedelta(days=days)
    used = used_clips(kind)
    with db.session() as s:
        rows = s.exec(select(Post, Clip, Video).join(Clip, Clip.id == Post.clip_id).join(
            Video, Video.id == Clip.video_id).where(Post.platform == "youtube", Post.status == "published",
                                                    Post.published_at >= since, Clip.status == "ready")).all()
        ids = [c.id for _, c, _ in rows]
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(col(ClipInfo.clip_id).in_(ids))).all()}
        views: dict[int, int] = {}
        for st in s.exec(select(PostStat).where(col(PostStat.post_id).in_([p.id for p, _, _ in rows]))
                         .order_by(col(PostStat.fetched_at))).all():
            views[st.post_id] = st.views
        srcs = {x.id: x for x in s.exec(select(Source)).all()}
    out, seen = [], set()
    for post, clip, video in rows:
        try:
            info = json.loads(infos.get(clip.id) or "{}")
        except ValueError:
            info = {}
        if growth.kind_of(info) != kind or clip.id in used or clip.group in seen:
            continue
        if not clip.path or not Path(clip.path).is_file():
            continue
        seen.add(clip.group)
        src = srcs.get(video.source_id)
        out.append({"clip": clip, "video": video, "info": info, "views": views.get(post.id, 0),
                    "streamer": src.name if src else "", "channel": src.url if src else "",
                    "duration": max(0.0, clip.end - clip.start)})
    return sorted(out, key=lambda x: (-x["views"], -x["clip"].score))


def pick(kind: str) -> list[dict]:
    """Les meilleurs Shorts de la semaine (la quinzaine si la semaine ne suffit pas), ou [] si trop peu."""
    cfg = get_settings()
    for days in (cfg.compil_days, 2 * cfg.compil_days):
        sel = candidates(kind, days)[:max(1, cfg.compil_max)]
        for x in sel:  # durée réelle du fichier (blancs coupés)
            try:
                x["duration"] = media.duration(Path(x["clip"].path)) or x["duration"]
            except Exception:
                pass
        if len(sel) >= cfg.compil_min and sum(x["duration"] for x in sel) >= cfg.compil_min_seconds:
            return sel
    return []


def order(kind: str, sel: list[dict]) -> list[dict]:
    """Clips GTA : compte à rebours, le meilleur à la fin (on reste jusqu'au bout). Sinon le meilleur d'abord."""
    best_first = sorted(sel, key=lambda x: (-x["views"], -x["clip"].score))
    return best_first[::-1] if kind == "clip" else best_first


# ---------- textes ----------

def stamp(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean_title(title: str) -> str:
    import re
    return re.sub(r"\s*#\w+", "", title or "").strip(" -–:")


def label(kind: str, i: int, n: int) -> str:
    return f"#{n - i}" if kind == "clip" else f"{i + 1}/{n}"


def texts(kind: str, items: list[dict], starts: list[float], at: datetime | None = None) -> tuple[str, str, list[str]]:
    """(titre, description avec chapitres, tags) de la compilation."""
    n = len(items)
    monday = (_local(at) - timedelta(days=_local(at).weekday())).strftime("%d/%m")
    best = sorted(items, key=lambda x: -x["views"])
    names = list(dict.fromkeys(x["streamer"] for x in best if x["streamer"]))
    if kind == "clip":
        title = f"TOP {n} des meilleurs moments GTA 6 de la semaine"
        if names:
            cand = f"{title} ({', '.join(names[:3])})"
            title = cand if len(cand) <= 100 else f"{title} ({names[0]}…)"
        intro = (f"Les {n} moments GTA 6 les plus forts de la semaine chez les streamers FR, du {n}e au 1er. "
                 f"Lequel est ton préféré ? Dis-le en commentaire 👇")
        tags = ["GTA6", "GTAVI", "GTA6FR", "GTA", "GTARP", "Twitch"]
    elif kind == "story":
        subj = clean_title(best[0]["clip"].title)[:50]
        title = f"{n} histoires vraies troublantes : {subj} et d'autres"
        intro = f"{n} histoires vraies à écouter d'une traite. Laquelle t'a le plus marqué ? Dis-le en commentaire 👇"
        tags = ["histoire", "histoirevraie", "mystère", "faitdivers"]
    else:
        title = f"L'actu de la semaine en {n} vidéos (semaine du {monday})"
        intro = f"Les {n} infos de la semaine du {monday}, résumées en 1 minute chacune."
        tags = ["actu", "actualité", "info", "news"]
    from . import interact

    lines = [intro, ""]
    extra = [x for x in (interact.share_line("compil" if kind == "clip" else kind), growth.subscribe_line(kind)) if x]
    if extra:
        lines += extra + [""]
    lines.append("Chapitres :")
    for i, (x, t0) in enumerate(zip(items, starts)):
        who = f"{x['streamer']} : " if kind == "clip" and x["streamer"] and x["streamer"].lower() not in \
            x["clip"].title.lower() else ""
        lines.append(f"{stamp(t0)} {x.get('label') or label(kind, i, n)} {who}{clean_title(x['clip'].title)}"[:120])
    if kind == "clip" and names:
        lines += ["", "🎮 Streamers (allez les soutenir) :"]
        for name in names:
            url = next((x["channel"] for x in items if x["streamer"] == name and x["channel"]), "")
            lines.append(f"• {name} : {url}" if url else f"• {name}")
    lines += ["", "Montage automatique des meilleurs Shorts de la chaîne.", "", " ".join("#" + t for t in tags[:5])]
    return title[:100], "\n".join(lines)[:4900], tags


# ---------- images ----------

def _font(size: int):
    from .pipeline import hook
    return hook._text_font(get_settings().font_name, size)


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if draw.textlength(cand, font=font) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = textwrap.shorten(lines[-1] + " …", width=max(8, len(lines[-1])), placeholder=" …")
    return lines


def panel(out: Path, header: str, big: str, title: str, sub: str) -> Path:
    """Habillage 1920x1080 transparent : en-tête et numéro à gauche, titre et streamer à droite du clip."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 56
    f_head, f_big, f_title, f_sub = _font(40), _font(220), _font(60), _font(42)
    for i, line in enumerate(_wrap(d, header.upper(), f_head, SIDE - 2 * pad, 3)):
        d.text((pad, 70 + i * 52), line, font=f_head, fill=(255, 212, 0, 255), stroke_width=3,
               stroke_fill=(0, 0, 0, 255))
    bw = d.textlength(big, font=f_big)
    d.text(((SIDE - bw) / 2, H / 2 - 140), big, font=f_big, fill=(255, 255, 255, 255), stroke_width=10,
           stroke_fill=(0, 0, 0, 255))
    x0 = SIDE + FG_W + pad
    size = 60
    while True:  # titre réduit jusqu'à tenir dans la colonne (mot long : « course-poursuite »)
        lines = _wrap(d, title, f_title, W - x0 - pad, 6)
        if size <= 38 or all(d.textlength(x, font=f_title) <= W - x0 - pad for x in lines):
            break
        size -= 4
        f_title = _font(size)
    step = int(size * 1.23)
    y = H / 2 - (len(lines) * step) / 2 - 30
    for line in lines:
        d.text((x0, y), line, font=f_title, fill=(255, 255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255))
        y += step
    if sub:
        d.text((x0, y + 24), sub, font=f_sub, fill=(255, 212, 0, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
    img.save(out)
    return out


def frame(src: Path, at: float, out: Path) -> Path | None:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                        str(out)], capture_output=True, timeout=120)
    return out if r.returncode == 0 and out.exists() else None


def thumbnail(src: Path, big: str, small: str, out: Path) -> Path | None:
    """Miniature 1280x720 : image forte du meilleur clip + gros texte jaune."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    try:
        shot = frame(src, max(0.5, media.duration(src) * 0.4), out.with_suffix(".frame.png"))
        if not shot:
            return None
        im = Image.open(shot).convert("RGB")
        bg = im.resize((1280, int(1280 * im.height / im.width))).crop((0, 0, 1280, 720))
        bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(18))).enhance(0.55)
        fg = im.resize((int(720 * im.width / im.height), 720))
        bg.paste(fg, (1280 - fg.width - 40, 0))
        d = ImageDraw.Draw(bg)
        y = 150
        for i, line in enumerate((big, small)):
            f = _font(150 if i == 0 else 84)
            for part in _wrap(d, line.upper(), f, 1280 - fg.width - 100, 2):
                d.text((50, y), part, font=f, fill=(255, 212, 0) if i == 0 else (255, 255, 255), stroke_width=9,
                       stroke_fill=(0, 0, 0))
                y += 160 if i == 0 else 96
        bg.save(out, "JPEG", quality=90)
        return out
    except Exception:
        log.exception("Miniature de la compilation impossible")
        return None


# ---------- montage ----------

def has_audio(src: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=60)
    return bool(r.stdout.strip())


def segment_cmd(src: Path, png: Path, out: Path, dur: float, audio: bool = True) -> list[str]:
    """Un Short vertical posé au centre d'une image 16:9 (fond : le même clip flouté), habillage par-dessus."""
    vf = ("[0:v]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=12:2,"
          f"scale={W}:{H},eq=brightness=-0.12:saturation=1.15,setsar=1[bg];"
          f"[0:v]scale=-2:{H},setsar=1[fg];[bg][fg]overlay=(W-w)/2:0[b];"
          "[b][1:v]overlay=0:0,fps=30,format=yuv420p[v];")
    vf += ("[0:a]aresample=48000,aformat=channel_layouts=stereo[a]" if audio else
           f"[2:a]atrim=0:{dur:.3f}[a]")
    return ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(png), "-f", "lavfi", "-t", f"{dur:.3f}",
            "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-g", "60",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-video_track_timescale", "15360",
            "-movflags", "+faststart", str(out)]


def _nice(cmd: list[str]) -> list[str]:
    """Priorité basse : la compilation ne ralentit pas le traitement des lives (ni ne fait chauffer d'un coup)."""
    return (["nice", "-n", "15"] + cmd) if shutil.which("nice") else cmd


def render(kind: str, items: list[dict], work: Path, header: str) -> tuple[Path, list[float], list[dict]]:
    """Monte la compilation ; renvoie (fichier, instants de début de chaque Short, Shorts gardés)."""
    work.mkdir(parents=True, exist_ok=True)
    parts, starts, kept, t = [], [], [], 0.0
    n = len(items)
    for i, x in enumerate(items):
        src = Path(x["clip"].path)
        dur = media.duration(src) or x["duration"]
        sub = f"Streamer : {x['streamer']}" if x["streamer"] else ""
        x["label"] = label(kind, i, n)
        png = panel(work / f"panel_{i}.png", header, x["label"], clean_title(x["clip"].title), sub)
        out = work / f"seg_{i:02d}.mp4"
        r = subprocess.run(_nice(segment_cmd(src, png, out, dur, has_audio(src))), capture_output=True, text=True,
                           timeout=1800)
        if r.returncode != 0:
            log.warning("Compilation : Short ignoré (%s) : %s", x["clip"].title, r.stderr[-300:])
            continue
        parts.append(out)
        starts.append(t)
        kept.append(x)
        t += media.duration(out) or dur
    if not parts:
        raise RuntimeError("aucun Short n'a pu être monté")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    final = work / "compilation.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-movflags", "+faststart", str(final)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"assemblage impossible : {r.stderr[-300:]}")
    for p in parts:
        p.unlink(missing_ok=True)
    return final, starts, kept


HEADERS = {"clip": "Top GTA 6 de la semaine", "story": "Histoires vraies", "news": "L'actu de la semaine"}


def make(kind: str, at: datetime | None = None) -> int | None:
    """Monte la compilation de la semaine pour ce type. Renvoie l'id de la vidéo, ou None (pas assez de Shorts)."""
    key = week_key(kind, at)
    if exists(key):
        return None
    sel = pick(kind)
    if not sel:
        cfg = get_settings()
        log.info("Compilation %s : pas assez de Shorts encore disponibles (il en faut %d, %d s au total)",
                 growth.KINDS[kind], cfg.compil_min, int(cfg.compil_min_seconds))
        platforms._put("compil_skip:" + key, now().isoformat())
        return None
    items = order(kind, sel)
    with db.session() as s:
        v = Video(key=key, url="", title=f"Compilation {growth.KINDS[kind]}", status="compiling")
        s.add(v)
        s.commit()
        vid = v.id
    work = get_settings().clips_dir / f"compil_{vid}"
    try:
        path, starts, kept = render(kind, items, work, HEADERS.get(kind, ""))
        title, desc, tags = texts(kind, kept, starts, at)
        dur = media.duration(path)
        best = max(kept, key=lambda x: x["views"])
        thumbnail(Path(best["clip"].path), f"TOP {len(kept)}" if kind == "clip" else f"{len(kept)} histoires",
                  "GTA 6 de la semaine" if kind == "clip" else growth.KINDS[kind], work / "thumb.jpg")
    except Exception as e:
        log.exception("Compilation %s impossible", growth.KINDS[kind])
        with db.session() as s:
            v = s.get(Video, vid)
            v.status, v.error = "failed", str(e)[:500]
            s.add(v)
            s.commit()
        return None
    with db.session() as s:
        v = s.get(Video, vid)
        v.status, v.title, v.path, v.duration, v.error = "compil_ready", title, str(path), dur, ""
        s.add(v)
        clip = Clip(video_id=vid, group=f"compil-{uuid.uuid4().hex[:8]}", start=0, end=dur, score=0.0, title=title,
                    description=desc, path=str(path), status="ready")
        s.add(clip)
        s.commit()
        s.merge(ClipInfo(clip_id=clip.id, data=json.dumps({
            "kind": "compil", "of": kind, "title": title, "items": [x["clip"].id for x in kept],
            "tags": tags, "duration": dur, "starts": starts}, ensure_ascii=False)))
        s.commit()
    log.info("Compilation %s montée : %s (%d Shorts, %s)", growth.KINDS[kind], title, len(kept), stamp(dur))
    return vid


def publish(video_id: int) -> bool:
    """Envoie une compilation montée. Connexion expirée / quota : on réessaiera au prochain passage."""
    from .publish import youtube

    cfg = get_settings()
    with db.session() as s:
        v = s.get(Video, video_id)
        clip = s.exec(select(Clip).where(Clip.video_id == video_id)).first()
        info = s.get(ClipInfo, clip.id) if clip else None
    if not v or not clip or not Path(clip.path).is_file():
        return False
    if not platforms.enabled("youtube") or not youtube.configured() or youtube.auth_error():
        return False
    data = json.loads(info.data) if info else {}
    category = "24" if data.get("of") == "story" and cfg.compil_category == "20" else cfg.compil_category
    tries_key = f"compil_tries:{video_id}"
    try:
        remote = youtube.upload(clip.path, clip.title, clip.description, data.get("tags") or [], shorts=False,
                                category=category)
    except (youtube.AuthExpired, youtube.QuotaExceeded) as e:
        log.warning("Compilation en attente (YouTube) : %s", e)
        return False
    except Exception as e:
        tries = int(platforms._get(tries_key) or 0) + 1
        platforms._put(tries_key, str(tries))
        log.exception("Envoi de la compilation échoué (essai %d/%d)", tries, TRIES)
        if tries >= TRIES:
            with db.session() as s:
                v = s.get(Video, video_id)
                v.status, v.error = "failed", f"Envoi YouTube : {str(e)[:400]}"
                s.add(v)
                s.commit()
            notify.error(f"Compilation pas envoyée après {TRIES} essais : {clip.title}", str(e)[:500])
        return False
    platforms._put(tries_key, None)
    thumb = Path(clip.path).with_name("thumb.jpg")
    if thumb.is_file():
        youtube.set_thumbnail(remote, str(thumb))
    with db.session() as s:
        s.add(Post(clip_id=clip.id, platform=growth.LONG, status="published", remote_id=remote, attempts=1,
                   published_at=now()))
        v = s.get(Video, video_id)
        v.status, v.url = "done", f"https://youtu.be/{remote}"
        s.add(v)
        s.commit()
    log.info("Compilation publiée : %s (https://youtu.be/%s)", clip.title, remote)
    if cfg.notify_published:
        notify.send(f"🎬 Compilation publiée (vidéo longue, heures de visionnage) : {clip.title}\n"
                    f"https://youtu.be/{remote}")
    return True


def pending() -> list[int]:
    with db.session() as s:
        return list(s.exec(select(Video.id).where(col(Video.key).startswith(PREFIX),
                                                  Video.status == "compil_ready")).all())


def run_once(at: datetime | None = None) -> None:
    if not _busy.acquire(blocking=False):
        return
    try:
        forced = platforms._get(NOW_KEY)
        if forced:
            platforms._put(NOW_KEY, None)
            if forced in growth.KINDS:
                platforms._put("compil_skip:" + week_key(forced, at), None)
                make(forced, at)
        for kind in kinds():
            if due(kind, at):
                make(kind, at)
        for vid in pending():
            publish(vid)
    finally:
        _busy.release()


def reset_interrupted() -> None:
    """Compilation coupée par un redémarrage : on efface le brouillon, elle sera remontée."""
    with db.session() as s:
        for v in s.exec(select(Video).where(col(Video.key).startswith(PREFIX), Video.status == "compiling")).all():
            shutil.rmtree(get_settings().clips_dir / f"compil_{v.id}", ignore_errors=True)
            s.delete(v)
        s.commit()


def status() -> dict:
    cfg = get_settings()
    with db.session() as s:
        last = s.exec(select(Video).where(col(Video.key).startswith(PREFIX))
                      .order_by(col(Video.created_at).desc()).limit(4)).all()
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    return {"on": enabled(), "when": f"{days[cfg.compil_day % 7]} à partir de {cfg.compil_hour} h",
            "kinds": [growth.KINDS[k] for k in kinds()], "last": last}


def loop(stop: threading.Event) -> None:
    stop.wait(120)  # laisse le reste démarrer
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            log.exception("Compilations en erreur")
        stop.wait(300)
