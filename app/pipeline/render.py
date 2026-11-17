"""Rendu vertical 1080x1920 : fond flouté + vidéo centrée + sous-titres incrustés."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)
_gpu_broken = False  # passe à True si l'encodage GPU échoue : on reste ensuite sur le CPU


def build_cmd(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str | None = None,
              overlay: str = "", overlay_seconds: float = 0) -> list[str]:
    """overlay : image PNG 1080 px de large (accroche avec emojis) posée en haut, sur `overlay_seconds` s (0 = tout)."""
    s = get_settings()
    codec = codec or s.video_codec
    vf = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.08[bg];"
        "[b]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30"
    )
    if overlay:
        enable = f":enable='lte(t,{overlay_seconds:.2f})'" if overlay_seconds > 0 else ""
        vf += f"[base];[base][1:v]overlay=0:0:format=auto{enable}"
    vf += f",ass={ass_name}"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
        vf += ",format=nv12,hwupload"
    cmd += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src)]
    if overlay:
        cmd += ["-i", overlay]
    cmd += ["-filter_complex", vf + "[v]",
            "-map", "[v]", "-map", "0:a:0?"]
    if codec.endswith("_vaapi"):
        cmd += ["-c:v", codec, "-qp", str(s.crf + 2)]
    else:
        cmd += ["-c:v", codec, "-preset", "veryfast", "-crf", str(s.crf), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-movflags", "+faststart", str(out)]
    return cmd


def render(src: Path, start: float, end: float, ass_text: str, out: Path, overlay: str = "",
           overlay_seconds: float = 0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out.stem + ".ass"
    (out.parent / ass_name).write_text(ass_text, encoding="utf-8")
    global _gpu_broken
    codec = get_settings().video_codec
    if codec.endswith("_vaapi") and _gpu_broken:
        codec = "libx264"
    ov = (overlay, overlay_seconds)
    proc = _run(src, start, end, ass_name, out, codec, *ov)
    if proc.returncode != 0 and codec.endswith("_vaapi"):
        log.warning("Encodage GPU impossible, passage au CPU (libx264) : %s", proc.stderr.strip()[-300:])
        _gpu_broken = True
        proc = _run(src, start, end, ass_name, out, "libx264", *ov)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr[-800:]}")
    return out


def _run(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str, overlay: str = "",
         overlay_seconds: float = 0):
    cmd = build_cmd(src.resolve(), start, end, ass_name, out.resolve(), codec, overlay, overlay_seconds)
    return subprocess.run(cmd, cwd=out.parent,
                          capture_output=True, text=True)
