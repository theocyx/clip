"""Rendu vertical 1080x1920 + sous-titres incrustés.

Deux mises en page :
- classique : fond flouté + vidéo centrée ;
- split : facecam du streamer en haut, jeu recadré en bas (si une facecam a été détectée)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)
W = 1080
_gpu_broken = False  # passe à True si l'encodage GPU échoue : on reste ensuite sur le CPU


def split_top(cam_h: int) -> int:
    """Décalage de l'accroche en format split : juste sous la facecam."""
    return cam_h - 150


def layout_filter(cam=None, cam_h: int = 0, src: str = "[0:v]") -> str:
    if cam is None:
        return (
            f"{src}split=2[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.08[bg];"
            "[b]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30"
        )
    game_h = 1920 - cam_h
    # le jeu est recadré au centre ; si la facecam déborde dans ce cadre (source 16:9), on le décale
    half = W / (game_h * 16 / 9) / 2
    shift = 0.0
    if cam.x + cam.w / 2 < 0.5:
        shift = max(0.0, cam.x + cam.w + 0.015 - (0.5 - half))  # + marge pour la bordure du cadre
    else:
        shift = -max(0.0, (0.5 + half) - (cam.x - 0.015))
    shift = max(-0.15, min(0.15, shift))
    gx = f"max(0\\,min(in_w-out_w\\,(in_w-out_w)/2+in_w*{shift:.4f}))"
    return (
        f"{src}split=2[a][b];"
        f"[a]crop=iw*{cam.w:.4f}:ih*{cam.h:.4f}:iw*{cam.x:.4f}:ih*{cam.y:.4f},"
        f"scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h},setsar=1[top];"
        f"[b]scale={W}:{game_h}:force_original_aspect_ratio=increase,crop={W}:{game_h}:{gx}:0,setsar=1[bot];"
        "[top][bot]vstack=inputs=2,fps=30"
    )


AUDIO = "loudnorm=I=-14:TP=-1.5:LRA=11"


def build_cmd(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str | None = None,
              overlay: str = "", overlay_seconds: float = 0, cam=None,
              teaser: tuple[float, float] | None = None) -> list[str]:
    """overlay : image PNG 1080 px de large (accroche avec emojis) posée en haut, sur `overlay_seconds` s (0 = tout).

    cam : zone de la facecam (facecam.Box) pour le format split, None = format classique.
    teaser : (début, fin) d'un court extrait montré en ouverture, avant le clip (« cold open »)."""
    s = get_settings()
    codec = codec or s.video_codec
    pieces = ([teaser] if teaser else []) + [(start, end)]
    vf = ""
    src_v = "[0:v]"
    if teaser:  # extrait + clip bout à bout, puis un court flash blanc pour marquer le retour au début
        vf = "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[cv][ca];"
        src_v = "[cv]"
    vf += layout_filter(cam, s.split_cam_height, src_v)
    if teaser:
        t0 = teaser[1] - teaser[0]
        vf += f",drawbox=c=white@0.7:t=fill:enable='between(t,{t0:.2f},{t0 + 0.12:.2f})'"
    if overlay:
        enable = f":enable='lte(t,{overlay_seconds:.2f})'" if overlay_seconds > 0 else ""
        y = split_top(s.split_cam_height) if cam is not None else 0
        vf += f"[base];[base][{len(pieces)}:v]overlay=0:{y}:format=auto{enable}"
    vf += f",ass={ass_name}"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
        vf += ",format=nv12,hwupload"
    for a, b in pieces:
        cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
    if overlay:
        cmd += ["-i", overlay]
    if teaser:
        cmd += ["-filter_complex", vf + f"[v];[ca]{AUDIO}[a]", "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", vf + "[v]",
                "-map", "[v]", "-map", "0:a:0?"]
    if codec.endswith("_vaapi"):
        cmd += ["-c:v", codec, "-qp", str(s.crf + 2)]
    else:
        cmd += ["-c:v", codec, "-preset", "veryfast", "-crf", str(s.crf), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100"] + ([] if teaser else ["-af", AUDIO])
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def render(src: Path, start: float, end: float, ass_text: str, out: Path, overlay: str = "",
           overlay_seconds: float = 0, cam=None, teaser: tuple[float, float] | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out.stem + ".ass"
    (out.parent / ass_name).write_text(ass_text, encoding="utf-8")
    global _gpu_broken
    codec = get_settings().video_codec
    if codec.endswith("_vaapi") and _gpu_broken:
        codec = "libx264"
    ov = (overlay, overlay_seconds, cam, teaser)
    proc = _run(src, start, end, ass_name, out, codec, *ov)
    if proc.returncode != 0 and codec.endswith("_vaapi"):
        log.warning("Encodage GPU impossible, passage au CPU (libx264) : %s", proc.stderr.strip()[-300:])
        _gpu_broken = True
        proc = _run(src, start, end, ass_name, out, "libx264", *ov)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr[-800:]}")
    return out


def _run(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str, overlay: str = "",
         overlay_seconds: float = 0, cam=None, teaser=None):
    cmd = build_cmd(src.resolve(), start, end, ass_name, out.resolve(), codec, overlay, overlay_seconds, cam, teaser)
    return subprocess.run(cmd, cwd=out.parent,
                          capture_output=True, text=True)
