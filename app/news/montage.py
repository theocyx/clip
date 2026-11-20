"""Montage vertical 1080x1920 d'une vidéo d'actu :
un plan par phrase (coupé en deux si la phrase est longue) avec mouvement de caméra lent sur les images,
pastille + titre en haut, sous-titres mot à mot au centre, source en bas, voix off + musique de fond baissée
automatiquement quand la voix parle, son à -14 LUFS."""
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
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
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


def pick_music() -> Path | None:
    d = get_settings().data_dir / "music"
    tracks = sorted(p for p in d.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".flac"))
    return random.choice(tracks) if tracks else None


def final_cmd(video: Path, voice: Path, header: str, ass_name: str, out: Path, total: float, codec: str,
              music: Path | None = None) -> list[str]:
    s = get_settings()
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
    cmd += ["-i", str(video), "-i", str(voice)]
    n = 2
    vf = "[0:v]"
    if header:
        cmd += ["-i", header]
        vf += f"[{n}:v]overlay=(W-w)/2:{HEADER_Y}:format=auto,"
        n += 1
    vf += f"ass={ass_name}"
    if codec.endswith("_vaapi"):
        vf += ",format=nv12,hwupload"
    vf += "[v]"
    af = f"[1:a]aresample=44100,aformat=channel_layouts=stereo,apad=whole_dur={total:.2f}"
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        fade = max(0.0, total - 1.5)
        af += ",asplit=2[vo][sc];"
        af += (f"[{n}:a]aresample=44100,aformat=channel_layouts=stereo,volume={s.news_music_volume:.3f},"
               f"atrim=0:{total:.2f},afade=t=out:st={fade:.2f}:d=1.5[mu];"
               "[mu][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[md];"
               "[vo][md]amix=inputs=2:duration=first:normalize=0,")
        n += 1
    else:
        af += ","
    af += render.AUDIO + "[a]"
    cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}"]
    if codec.endswith("_vaapi"):
        cmd += ["-c:v", codec, "-qp", str(s.crf + 2)]
    else:
        cmd += ["-c:v", codec, "-preset", "veryfast", "-crf", str(s.crf), "-pix_fmt", "yuv420p"]
    cmd += ["-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(out)]
    return cmd


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr.strip()[-600:]}")


def build(shots: list, spoken: list[Spoken], voice: Path, hook: str, source: str, out: Path, work: Path,
          on_progress=None) -> float:
    """shots : une illustration (visuals.Shot) par plan de `plan()`. Renvoie la durée de la vidéo."""
    s = get_settings()
    total = round(spoken[-1].end + TAIL, 3)
    cuts = plan(spoken, total)
    if len(shots) != len(cuts):
        raise ValueError("un plan = une illustration")
    parts = []
    for j, ((_, a, b), shot) in enumerate(zip(cuts, shots)):
        seg = work / f"shot_{j:02d}.mp4"
        _run(shot_cmd(shot.path, shot.kind, b - a, seg, j, offset=0.3 if shot.kind == "video" else 0))
        parts.append(seg)
        if on_progress:
            on_progress(80 * (j + 1) / len(cuts))
    listing = work / "shots.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    video = work / "shots.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy",
          video.name], cwd=work)
    header, _ = header_png(hook, s.news_badge, work / "header.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out.stem + ".ass"
    (out.parent / ass_name).write_text(ass_text(spoken, total, source), encoding="utf-8")
    music = pick_music()
    codec = render.resolve_codec()
    args = (video.resolve(), voice.resolve(), str(Path(header).resolve()) if header else "", ass_name,
            out.resolve(), total)
    try:
        _run(final_cmd(*args, codec=codec, music=music), cwd=out.parent)
    except RuntimeError as e:
        if not codec.endswith("_vaapi"):
            raise
        log.warning("Encodage GPU impossible pour l'actu, passage au CPU : %s", str(e)[-200:])
        _run(final_cmd(*args, codec="libx264", music=music), cwd=out.parent)
    if on_progress:
        on_progress(100)
    return total
