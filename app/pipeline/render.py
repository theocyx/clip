"""Rendu vertical 1080x1920 : fond flouté + vidéo centrée + sous-titres incrustés."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import get_settings


def build_cmd(src: Path, start: float, end: float, ass_name: str, out: Path) -> list[str]:
    s = get_settings()
    vf = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.08[bg];"
        "[b]scale=1080:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,ass={ass_name}"
    )
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if s.video_codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
        vf += ",format=nv12,hwupload"
    cmd += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src), "-filter_complex", vf + "[v]",
            "-map", "[v]", "-map", "0:a:0?"]
    if s.video_codec.endswith("_vaapi"):
        cmd += ["-c:v", s.video_codec, "-qp", str(s.crf + 2)]
    else:
        cmd += ["-c:v", s.video_codec, "-preset", "veryfast", "-crf", str(s.crf), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-movflags", "+faststart", str(out)]
    return cmd


def render(src: Path, start: float, end: float, ass_text: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out.stem + ".ass"
    (out.parent / ass_name).write_text(ass_text, encoding="utf-8")
    proc = subprocess.run(build_cmd(src.resolve(), start, end, ass_name, out.resolve()), cwd=out.parent,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr[-800:]}")
    return out
