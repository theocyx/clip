"""Montage vertical 1080x1920 d'une vidéo d'actu :
un plan par phrase (coupé en deux si la phrase est longue) : survol satellite, panneaux animés (photo, vidéo,
chiffre, mots, quiz) ; habillage dessiné par-dessus (bandeau marque / sujet / date / chapitres / sources,
mots-clés en boîte inclinée, sous-titres en boîte mot à mot) ; voix off + musique de fond baissée
automatiquement quand la voix parle, son à -14 LUFS. Sans habillage animé (NEWS_MOTION=false) : images avec
mouvement de caméra, pastille + titre en haut, sous-titres ASS."""
from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path

from ..config import get_settings
from ..pipeline import hook as hooks, render, subtitles
from .tts import Spoken

log = logging.getLogger(__name__)
W, H = 1080, 1920
FPS = 30
HEADER_Y = 150
MAX_SHOT = 4.2  # au-delà, la phrase est illustrée par deux plans (rythme d'un compte d'actu : ~3 s par plan)
TAIL = 0.7  # secondes gardées après la dernière phrase


def plan(spoken: list[Spoken], total: float) -> list[tuple[int, float, float]]:
    """[(n° de phrase, début, fin)] qui couvrent toute la vidéo, sans trou."""
    cuts: list[tuple[int, float]] = []
    for i, sp in enumerate(spoken):
        start = 0.0 if i == 0 else sp.start
        end = spoken[i + 1].start if i + 1 < len(spoken) else total
        n = max(1, int((end - start) // MAX_SHOT) + (1 if (end - start) % MAX_SHOT > 1.2 else 0))
        for k in range(n):
            cuts.append((i, start + (end - start) * k / n))
    out = []
    for j, (i, a) in enumerate(cuts):
        b = cuts[j + 1][1] if j + 1 < len(cuts) else total
        out.append((i, round(a, 3), round(b, 3)))
    return out


def motion(idx: int, frames: int) -> str:
    """Mouvement lent sur une image : zoom avant, zoom arrière ou glissement latéral, en alternance."""
    n = max(1, frames)
    kind = idx % 3
    if kind == 0:
        z, x, y = f"1+0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif kind == 1:
        z, x, y = f"1.12-0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.1", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"
    return (f"scale={int(W * 1.5)}:{int(H * 1.5)},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={FPS},setsar=1")


def shot_cmd(path: Path, kind: str, dur: float, out: Path, idx: int, offset: float = 0.0) -> list[str]:
    frames = max(1, round(dur * FPS))
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if kind == "video":
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,fps={FPS},"
              # léger zoom continu : même une vidéo calme donne une impression de mouvement
              f"scale=iw*1.06:-2,crop={W}:{H}")
        cmd += ["-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(path)]
    else:
        vf = motion(idx, frames)
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(path)]
    cmd += ["-vf", vf, "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)]
    return cmd


def header_png(hook: str, badge: str, out: Path) -> tuple[str, int]:
    """Pastille rouge (« ACTU ») au-dessus du titre en boîte blanche. Renvoie (chemin, hauteur) ou ("", 0)."""
    from PIL import Image, ImageDraw

    from ..pipeline.hook import _text_font

    s = get_settings()
    box = out.with_name(out.stem + "_hook.png")
    try:
        n = hooks.render_png(hook, s.font_name, box) if hook else 0
    except Exception as e:
        log.warning("Titre en image impossible : %s", e)
        n = 0
    hook_img = Image.open(box).convert("RGBA") if n else None
    badge_img = None
    if badge:
        font = _text_font(s.font_name, 44)
        d = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        tw = round(d.textlength(badge.upper(), font=font))
        badge_img = Image.new("RGBA", (tw + 64, 72), (0, 0, 0, 0))
        bd = ImageDraw.Draw(badge_img)
        bd.rounded_rectangle((0, 0, tw + 63, 71), radius=16, fill=(230, 30, 40, 255))
        bd.ellipse((18, 28, 34, 44), fill=(255, 255, 255, 255))
        bd.text((44, 10), badge.upper(), font=font, fill=(255, 255, 255, 255))
    if hook_img is None and badge_img is None:
        return "", 0
    h = (hook_img.height if hook_img else 0) + (badge_img.height + 14 if badge_img else 0)
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    y = 0
    if badge_img:
        canvas.alpha_composite(badge_img, ((W - badge_img.width) // 2, 0))
        y = badge_img.height + 14
    if hook_img:
        canvas.alpha_composite(hook_img, (0, y))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return str(out), h


def ass_text(spoken: list[Spoken], total: float, source: str) -> str:
    """Sous-titres mot à mot (mot prononcé en jaune, mots forts en vert) + « Source : X » en bas."""
    s = get_settings()
    segments = [{"start": sp.start, "end": sp.end, "text": sp.text, "words": sp.words} for sp in spoken]
    ass = subtitles.build_ass(segments, 0, total, s.font_name, keywords=True)
    style = ("Style: Src,{font},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,0,2,"
             "60,60,70,1").format(font=s.font_name)
    ass = ass.replace("\n\n[Events]", "\n" + style + "\n\n[Events]", 1)
    if source:
        ass += f"Dialogue: 1,{subtitles.ts(0)},{subtitles.ts(total)},Src,,0,0,0,,SOURCE : {source.upper()}\n"
    return ass


def pick_music(story: bool = False) -> Path | None:
    """Musique de fond au hasard dans data/music (histoires : data/music/story s'il contient des morceaux)."""
    base = get_settings().data_dir / "music"

    def tracks(d: Path) -> list[Path]:
        return sorted(p for p in d.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".flac"))

    found = (tracks(base / "story") if story else []) or tracks(base)
    return random.choice(found) if found else None


TRANSITION = 0.28  # durée d'une transition (s)
TRANSITIONS = ["zoomin", "smoothup", "slideleft", "circleopen", "hblur", "smoothleft", "slideup", "radial"]
HEADER_IN = 0.45  # le titre glisse depuis le haut pendant ces secondes


def final_cmd(video: Path, voice: Path, header: str, ass_name: str, out: Path, total: float, codec: str,
              music: Path | None = None, sfx: Path | None = None, fx: bool = True, vignette: bool = True) -> list[str]:
    """fx : grain léger (+ vignette), et titre qui glisse depuis le haut au début. ass_name vide : pas de
    sous-titres ASS (habillage déjà incrusté image par image)."""
    s = get_settings()
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
    cmd += ["-i", str(video), "-i", str(voice)]
    n = 2
    vf = "[0:v]" + (("vignette=angle=PI/5," if vignette else "") + "noise=alls=4:allf=t," if fx else "")
    if header:
        cmd += ["-loop", "1", "-i", header]
        y = (f"if(lt(t\\,{HEADER_IN})\\,-h+({HEADER_Y}+h)*(1-pow(1-t/{HEADER_IN}\\,3))\\,{HEADER_Y})"
             if fx else str(HEADER_Y))
        vf += f"null[base];[base][{n}:v]overlay=x=(W-w)/2:y='{y}':shortest=1:format=auto,"
        n += 1
    vf += f"ass={ass_name}" if ass_name else "null"
    if codec.endswith("_vaapi"):
        vf += ",format=nv12,hwupload"
    vf += "[v]"
    af = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={total:.2f}"
    extra = []
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        fade = max(0.0, total - 1.5)
        af += ",asplit=2[vo][sc];"
        af += (f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume={s.news_music_volume:.3f},"
               f"atrim=0:{total:.2f},afade=t=out:st={fade:.2f}:d=1.5[mu];"
               "[mu][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[md];")
        extra.append("[md]")
        n += 1
        voice_label = "[vo]"
    else:
        af += "[vo];"
        voice_label = "[vo]"
    if sfx and s.news_sfx_volume > 0:
        cmd += ["-i", str(sfx)]
        af += (f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume={s.news_sfx_volume:.3f},"
               f"apad=whole_dur={total:.2f}[fx];")
        extra.append("[fx]")
        n += 1
    if extra:
        af += voice_label + "".join(extra) + f"amix=inputs={1 + len(extra)}:duration=first:normalize=0,"
    else:
        af += voice_label + "anull,"
    af += render.AUDIO + "[a]"
    cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}"]
    cmd += render.video_args(codec)
    cmd += ["-r", str(FPS)] + render.AUDIO_ARGS + ["-movflags", "+faststart", str(out)]
    return cmd


def join_cmd(parts: list[Path], durs: list[float], out: Path, transitions: bool = True) -> list[str]:
    """Plans bout à bout. Avec transitions, chaque plan (sauf le dernier) dure TRANSITION de plus et se fond
    dans le suivant (zoom, glissement, flou…) au moment exact du changement de phrase."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in parts:
        cmd += ["-i", str(p)]
    n = len(parts)
    if n == 1:
        return cmd + ["-c", "copy", str(out)]
    if transitions:
        f, prev, offset = "", "[0:v]", 0.0
        for k in range(1, n):
            offset += durs[k - 1]
            lab = f"[x{k}]"
            f += (f"{prev}[{k}:v]xfade=transition={TRANSITIONS[(k - 1) % len(TRANSITIONS)]}:"
                  f"duration={TRANSITION}:offset={offset:.3f}{lab};")
            prev = lab
        f = f.rstrip(";")
        last = prev
    else:
        f = "".join(f"[{k}:v]trim=0:{durs[k]:.3f},setpts=PTS-STARTPTS[t{k}];" for k in range(n))
        f += "".join(f"[t{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[j]"
        last = "[j]"
    return cmd + ["-filter_complex", f, "-map", last, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)]


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr.strip()[-600:]}")


def render_shot(shot, dur: float, seg: Path, idx: int, work: Path, fx: bool) -> Path:
    """Un plan en MP4 : vidéo libre de droits recadrée, ou animation calculée image par image."""
    from . import motion as anim, visuals

    s = get_settings()
    accent = anim.rgb(s.news_accent)
    frames = max(1, round(dur * FPS))
    if shot.kind == "video" and not fx:
        _run(shot_cmd(shot.path, "video", dur, seg, idx, offset=0.3))
        return seg
    if not fx:  # habillage animé coupé : image fixe avec mouvement de caméra simple
        if shot.kind == "image":
            path = visuals.frame_image(shot.path, work / f"frame_{idx}.jpg")
        else:
            path = visuals.card(shot.text or "INFO", work / f"card_{idx}.jpg").path
        _run(shot_cmd(path, "image", dur, seg, idx))
        return seg
    if shot.kind == "video":
        gen = anim.panel_video(shot.path, frames, idx, label=shot.meta.get("label", ""))
    elif shot.kind == "image":
        gen = anim.panel_photo(shot.path, frames, idx, label=shot.meta.get("label", ""))
    elif shot.kind == "quiz":
        gen = anim.quiz(shot.text, shot.meta.get("answers") or [], frames, accent)
    elif shot.kind == "counter":
        gen = anim.counter(shot.text, shot.meta.get("unit", ""), frames, idx, accent, year=shot.meta.get("year", False))
    elif shot.kind == "map":
        from . import geo
        gen = anim.map_fly(shot.meta["lon"], shot.meta["lat"], shot.text, frames, shot.meta["fetch"], accent,
                           z1=shot.meta.get("zoom", 12.5), attribution=geo.ATTRIBUTION,
                           sub=shot.meta.get("sub", ""), rings=shot.meta.get("rings"))
    else:
        gen = anim.kinetic(shot.text or "INFO", frames, idx, accent)
    return anim.write_frames(gen, frames, seg)


def make_overlay(spoken: list[Spoken], hook: str, look: dict | None):
    """Habillage « carte d'actu » : bandeau (marque, sujet, date, chapitres, sources), mots-clés en boîte,
    sous-titres en boîte avec le mot prononcé sur fond rouge."""
    from . import overlay as ov, visuals

    s = get_settings()
    look = look or {}
    n = len(spoken)
    titles = list(look.get("titles") or [])[:n] + [""] * max(0, n - len(look.get("titles") or []))
    titles = [(t or visuals.key_words(sp.text)).upper()[:40] for t, sp in zip(titles, spoken)]
    if hook:
        titles[0] = hooks.strip_emojis(hook).upper()[:40]
    labels = list(look.get("chapters") or [])
    labels = (labels + [""] * n)[:n]
    if not any(labels):  # pas de chapitres : début / milieu / fin
        labels = ["LES FAITS" if i < max(1, n // 3) else "CONTEXTE" if i < n - 1 else "ET TOI ?"
                  for i in range(n)]
    else:  # trous comblés par le chapitre précédent
        for i in range(n):
            labels[i] = labels[i] or (labels[i - 1] if i else "LES FAITS")
    tabs, chapter_of = ov.chapters(labels)
    hud = ov.Hud(brand=look.get("brand") or s.news_brand or s.news_badge or "ACTU",
                 topic=look.get("topic") or "", date=ov.date_fr(), tabs=tabs,
                 sources=look.get("sources") or "")
    words = [w for sp in spoken for w in sp.words]
    sentences = [(sp.start, sp.end) for sp in spoken]
    total = spoken[-1].end + TAIL if spoken else 0.0
    return ov.Overlay(hud, sentences, titles, chapter_of, words,
                      cta=ov.cta_windows(sentences, total) if s.news_cta else [])


def overlay_pass(video: Path, overlay, total: float, out: Path) -> Path:
    """Décode les plans, dessine l'habillage sur chaque image, réencode (sans fichier par image)."""
    import numpy as np

    n = max(1, round(total * FPS))
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                            "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                            "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    size = W * H * 3
    last = None
    try:
        for i in range(n):
            buf = dec.stdout.read(size) if dec.stdout else b""
            if len(buf) == size:
                last = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
            elif last is None:
                raise RuntimeError("plans illisibles")
            frame = overlay.apply(last.copy(), i / FPS)
            enc.stdin.write(frame.tobytes())
    finally:
        if enc.stdin:
            enc.stdin.close()
        dec.kill()
        dec.wait()
        err = enc.stderr.read().decode(errors="ignore") if enc.stderr else ""
        enc.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {err.strip()[-600:]}")
    return out


def build(shots: list, spoken: list[Spoken], voice: Path, hook: str, source: str, out: Path, work: Path,
          on_progress=None, look: dict | None = None) -> float:
    """shots : une illustration (visuals.Shot) par plan de `plan()`. Renvoie la durée de la vidéo.
    look : marque, sujet, chapitres, mots-clés et sources du bandeau (habillage animé uniquement)."""
    from . import motion as anim

    s = get_settings()
    fx = s.news_motion
    total = round(spoken[-1].end + TAIL, 3)
    cuts = plan(spoken, total)
    if len(shots) != len(cuts):
        raise ValueError("un plan = une illustration")
    parts, durs = [], []
    for j, ((_, a, b), shot) in enumerate(zip(cuts, shots)):
        dur = b - a
        extra = TRANSITION if fx and j + 1 < len(cuts) else 0.0
        seg = work / f"shot_{j:02d}.mp4"
        try:
            render_shot(shot, dur + extra, seg, j, work, fx)
        except Exception as e:  # un plan raté ne doit pas faire échouer la vidéo : mots animés à la place
            log.warning("Plan %d (%s) raté, remplacé : %s", j, shot.kind, str(e)[-200:])
            from .visuals import Shot
            render_shot(Shot(None, "kinetic", text=shot.text or "INFO"), dur + extra, seg, j, work, fx)
        parts.append(seg)
        durs.append(dur)
        if on_progress:
            on_progress((70 if fx else 80) * (j + 1) / len(cuts))
    video = work / "shots.mp4"
    try:
        _run(join_cmd(parts, durs, video, transitions=fx))
    except RuntimeError as e:
        if not fx:
            raise
        log.warning("Transitions impossibles, plans collés simplement : %s", str(e)[-200:])
        _run(join_cmd(parts, durs, video, transitions=False))
    sfx = None
    if fx and s.news_sfx_volume > 0 and len(cuts) > 1:
        cut_times = [c[1] + TRANSITION / 2 for c in cuts[1:]]
        sfx = anim.whoosh_track(cut_times, total, work / "whoosh.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    if fx:  # habillage complet dessiné image par image
        look = dict(look or {})
        look.setdefault("sources", source.upper() if source else "")
        ov = make_overlay(spoken, hook, look)
        if shots and shots[-1].kind == "quiz":  # le quiz a déjà son « ET TOI ? »
            ov.titles[-1] = ""
        video = overlay_pass(video, ov, total, work / "overlay.mp4")
        if on_progress:
            on_progress(92)
        header, ass_name = "", ""
    else:
        header, _ = header_png(hook, s.news_badge, work / "header.png")
        ass_name = out.stem + ".ass"
        (out.parent / ass_name).write_text(ass_text(spoken, total, source), encoding="utf-8")
    music = pick_music(bool((look or {}).get("story")))
    codec = render.resolve_codec()
    args = (video.resolve(), voice.resolve(), str(Path(header).resolve()) if header else "", ass_name,
            out.resolve(), total)
    try:
        _run(final_cmd(*args, codec=codec, music=music, sfx=sfx, fx=fx, vignette=not fx), cwd=out.parent)
    except RuntimeError as e:
        if not codec.endswith("_vaapi"):
            raise
        log.warning("Encodage GPU impossible pour l'actu, passage au CPU : %s", str(e)[-200:])
        _run(final_cmd(*args, codec="libx264", music=music, sfx=sfx, fx=fx, vignette=not fx), cwd=out.parent)
    if on_progress:
        on_progress(100)
    return total
