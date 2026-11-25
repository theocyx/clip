"""Rendu vertical 1080x1920 + sous-titres incrustés.

Deux mises en page :
- classique : fond flouté + vidéo centrée ;
- split : facecam du streamer en haut, jeu recadré en bas (si une facecam a été détectée)."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)
W = 1080
_gpu_broken = False  # passe à True si l'encodage GPU échoue : on reste ensuite sur le CPU


def split_top(cam_h: int) -> int:
    """Décalage de l'accroche en format split : juste sous la facecam."""
    return cam_h - 150


def hook_y(cam, cam_h: int) -> str:
    """Position (expression ffmpeg, h = hauteur de l'image) de l'accroche : à cheval sur la jonction
    facecam / jeu en format split, juste au-dessus de la vidéo centrée en format classique."""
    if cam is not None:
        return f"{cam_h}-h/2"
    return f"{(1920 - 608) // 2 - 24}-h"


def _enable(windows) -> str:
    return "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in windows)


def _punch(label_in: str, label_out: str, w: int, h: int, windows, zoom: float = 1.15) -> str:
    """Zoom rapide (« punch-in ») sur `windows` : copie recadrée au centre puis agrandie, posée par-dessus."""
    return (f"{label_in}split=2[{label_out}a][{label_out}b];"
            f"[{label_out}b]crop=trunc(iw/{zoom}/2)*2:trunc(ih/{zoom}/2)*2,scale={w}:{h}:flags=lanczos,setsar=1"
            f"[{label_out}z];"
            f"[{label_out}a][{label_out}z]overlay=0:0:enable='{_enable(windows)}'")


def sharpen(amount: float | None = None) -> str:
    """Filtre de netteté après un agrandissement (facecam, jeu recadré) : l'image agrandie paraît floue sinon.
    "" si coupé (SHARPEN=0). Seule la luminance est accentuée (pas de bruit coloré)."""
    a = get_settings().sharpen if amount is None else amount
    return f",unsharp=5:5:{a:.2f}:5:5:0" if a > 0 else ""


def layout_filter(cam=None, cam_h: int = 0, src: str = "[0:v]", punch=None) -> str:
    """punch : fenêtres (début, fin) en temps du clip final où l'on zoome sur la réaction
    (sur la facecam en format split, sur toute l'image sinon).
    Agrandissements en lanczos (plus net que le bicubique par défaut), fond flou en bilinéaire rapide."""
    if cam is not None:
        cam = _inset(cam)
    if cam is None:
        f = (
            f"{src}split=2[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=1080:1920,"
            "boxblur=25:2,eq=brightness=-0.08[bg];"
            "[b]scale=1080:-2:flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30"
        )
        if punch:
            f += "[lay];" + _punch("[lay]", "pz", W, 1920, punch)
        return f
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
    top = (
        f"{src}split=2[a][b];"
        f"[a]crop=iw*{cam.w:.4f}:ih*{cam.h:.4f}:iw*{cam.x:.4f}:ih*{cam.y:.4f},"
        f"scale={W}:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{cam_h},setsar=1"
        + sharpen()
    )
    if punch:  # le zoom se fait après fps=30 pour que t soit bien celui du clip final
        top += ",fps=30[top0];" + _punch("[top0]", "pz", W, cam_h, punch)
    return (
        top + "[top];"
        f"[b]scale={W}:{game_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{game_h}:{gx}:0,setsar=1"
        + sharpen(get_settings().sharpen * 0.6) + "[bot];"
        "[top][bot]vstack=inputs=2,fps=30"
    )


def _inset(cam, margin: float = 0.006):
    """Facecam rognée de quelques pixels : le cadre / la bordure de couleur de l'overlay du streamer disparaît
    et la caméra remplit toute la largeur, sans bord."""
    from dataclasses import replace

    mx = min(margin, cam.w * 0.04)
    my = min(margin * 16 / 9, cam.h * 0.04)
    try:
        return replace(cam, x=cam.x + mx, y=cam.y + my, w=max(0.01, cam.w - 2 * mx), h=max(0.01, cam.h - 2 * my))
    except TypeError:  # pas une dataclass (tests) : zone gardée telle quelle
        return cam


BAR_H = 10


AUDIO = "loudnorm=I=-14:TP=-1.5:LRA=11"
# Son du fichier envoyé : 48 kHz (fréquence recommandée par YouTube), AAC 192 kb/s
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
# Couleurs déclarées en BT.709 (HD) : sans ça certains lecteurs supposent BT.601 et les couleurs virent
COLOR_ARGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


FADE = 0.02  # s de fondu à chaque raccord : pas de « clic » quand un blanc est coupé au milieu d'une onde
SYNC = "aresample=async=1:first_pts=0"  # trous du son remplis de silence : son et image restent calés


def piece_audio(dur: float) -> str:
    """Traitement du son d'un morceau avant le recollage : calé sur l'image, fondus très courts aux deux bouts."""
    return f"{SYNC},afade=t=in:d={FADE},afade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE}"


def video_args(codec: str) -> list[str]:
    """Encodage du fichier envoyé. Les plateformes réencodent derrière : une image propre au départ
    (qp plus bas qu'avant sur le GPU, qui était moins net que le CPU à réglage égal) donne une meilleure image
    finale."""
    s = get_settings()
    if codec.endswith("_vaapi"):
        return ["-c:v", codec, "-qp", str(max(12, s.crf - 1))] + COLOR_ARGS
    return ["-c:v", codec, "-preset", "veryfast", "-crf", str(s.crf), "-pix_fmt", "yuv420p"] + COLOR_ARGS
_LOUD_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def resolve_codec(codec: str | None = None) -> str:
    """VIDEO_CODEC=auto : encodage par le GPU intégré (VAAPI) s'il est accessible, sinon CPU (libx264)."""
    s = get_settings()
    codec = (codec or s.video_codec or "auto").strip()
    if codec.lower() != "auto":
        return codec
    dev = s.vaapi_device or "/dev/dri/renderD128"
    return "h264_vaapi" if os.access(dev, os.R_OK | os.W_OK) else "libx264"


def audio_filter(measured: dict | None = None) -> str:
    """Son à -14 LUFS (niveau des Shorts / TikTok). Avec une mesure préalable (2 passes), le gain est
    appliqué d'un bloc (linear) : pas d'effet de « pompage » sur les cris et les silences."""
    if not measured:
        return AUDIO
    m = {k: measured[k] for k in _LOUD_KEYS}
    return (AUDIO + f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")


def measure_loudness(src: Path, pieces: list[tuple[float, float]]) -> dict | None:
    """1re passe de loudnorm sur l'audio seul des morceaux du clip (rapide). None si impossible."""
    try:
        cmd = ["ffmpeg", "-hide_banner", "-nostats"]
        for a, b in pieces:
            cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
        n = len(pieces)
        chain = ("".join(f"[{i}:a]{SYNC}[m{i}];" for i in range(n)) + "".join(f"[m{i}]" for i in range(n))
                 + f"concat=n={n}:v=0:a=1," if n > 1 else f"[0:a]{SYNC},")
        cmd += ["-filter_complex", chain + AUDIO + ":print_format=json[a]", "-map", "[a]", "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        found = re.findall(r"\{[^{}]*\}", proc.stderr or "")
        data = json.loads(found[-1]) if found else {}
        if not all(k in data for k in _LOUD_KEYS):
            return None
        if float(data["input_i"]) < -60:  # quasi silencieux : la normalisation simple suffit
            return None
        return data
    except Exception as e:  # la mesure ne doit jamais empêcher le rendu
        log.debug("Mesure du volume impossible : %s", e)
        return None


def build_cmd(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str | None = None,
              overlay: str = "", overlay_seconds: float = 0, cam=None,
              teaser: tuple[float, float] | None = None, cuts: list[tuple[float, float]] | None = None,
              punch: list[tuple[float, float]] | None = None, bar: bool = False,
              loud: dict | None = None) -> list[str]:
    """overlay : image PNG 1080 px de large (accroche avec emojis) posée en haut, sur `overlay_seconds` s (0 = tout).

    cam : zone de la facecam (facecam.Box) pour le format split, None = format classique.
    teaser : (début, fin) d'un court extrait montré en ouverture, avant le clip (« cold open »).
    cuts : morceaux du clip gardés (blancs coupés), par défaut [(start, end)].
    punch : fenêtres de zoom (temps du clip final). bar : barre de progression en haut.
    loud : mesure du volume (measure_loudness) pour une normalisation en 2 passes."""
    s = get_settings()
    codec = resolve_codec(codec)
    afilter = audio_filter(loud)
    body = cuts or [(start, end)]
    pieces = ([teaser] if teaser else []) + list(body)
    total = sum(b - a for a, b in pieces)
    n = len(pieces)
    vf = ""
    src_v = "[0:v]"
    if n > 1:  # morceaux bout à bout (extrait d'ouverture, clip sans les blancs), son calé et raccords sans clic
        vf = "".join(f"[{i}:a]{piece_audio(b - a)}[pa{i}];" for i, (a, b) in enumerate(pieces))
        vf += "".join(f"[{i}:v][pa{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca];"
        src_v = "[cv]"
    vf += layout_filter(cam, s.split_cam_height, src_v, punch)
    if teaser:  # court flash blanc pour marquer le retour au début
        t0 = teaser[1] - teaser[0]
        vf += f",drawbox=c=white@0.7:t=fill:enable='between(t,{t0:.2f},{t0 + 0.12:.2f})'"
    if overlay:
        enable = f":enable='lte(t,{overlay_seconds:.2f})'" if overlay_seconds > 0 else ""
        vf += f"[base];[base][{n}:v]overlay=0:{hook_y(cam, s.split_cam_height)}:format=auto{enable}"
    if bar and total > 0:
        vf += (f"[pb];color=c=0x00E5FF:s={W}x{BAR_H}:r=30[bar];"
               f"[pb][bar]overlay=x='-W+W*t/{total:.2f}':y=0:shortest=1")
    vf += f",ass={ass_name}"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if codec.endswith("_vaapi"):
        cmd += ["-vaapi_device", s.vaapi_device or "/dev/dri/renderD128"]
        vf += ",format=nv12,hwupload"
    for a, b in pieces:
        cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
    if overlay:
        cmd += ["-i", overlay]
    if n > 1:
        cmd += ["-filter_complex", vf + f"[v];[ca]{afilter}[a]", "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", vf + "[v]",
                "-map", "[v]", "-map", "0:a:0?"]
    cmd += video_args(codec)
    cmd += AUDIO_ARGS + ([] if n > 1 else ["-af", piece_audio(total) + "," + afilter])
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def render(src: Path, start: float, end: float, ass_text: str, out: Path, overlay: str = "",
           overlay_seconds: float = 0, cam=None, teaser: tuple[float, float] | None = None,
           cuts=None, punch=None, bar: bool = False) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out.stem + ".ass"
    (out.parent / ass_name).write_text(ass_text, encoding="utf-8")
    global _gpu_broken
    codec = resolve_codec()
    if codec.endswith("_vaapi") and _gpu_broken:
        codec = "libx264"
    pieces = ([teaser] if teaser else []) + list(cuts or [(start, end)])
    loud = measure_loudness(src.resolve(), pieces)
    ov = (overlay, overlay_seconds, cam, teaser, cuts, punch, bar, loud)
    proc = _run(src, start, end, ass_name, out, codec, *ov)
    if proc.returncode != 0 and codec.endswith("_vaapi"):
        log.warning("Encodage GPU impossible, passage au CPU (libx264) : %s", proc.stderr.strip()[-300:])
        _gpu_broken = True
        proc = _run(src, start, end, ass_name, out, "libx264", *ov)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg : {proc.stderr[-800:]}")
    return out


def _run(src: Path, start: float, end: float, ass_name: str, out: Path, codec: str, overlay: str = "",
         overlay_seconds: float = 0, cam=None, teaser=None, cuts=None, punch=None, bar=False, loud=None):
    cmd = build_cmd(src.resolve(), start, end, ass_name, out.resolve(), codec, overlay, overlay_seconds, cam, teaser,
                    cuts, punch, bar, loud)
    return subprocess.run(cmd, cwd=out.parent,
                          capture_output=True, text=True)
