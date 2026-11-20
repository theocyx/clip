"""Analyse de vidéos modèles (ex. TikTok d'un compte dont on veut le style), à lancer sur le serveur :

    docker compose exec worker python -m app.news.probe URL [URL…]

Pour chaque vidéo : téléchargement (yt-dlp), planche de 12 images réparties sur la vidéo, planche des
3 premières secondes, et mesures du montage (durée, nombre de plans, durée moyenne d'un plan).
Tout est rangé dans data/style/ (dossier ~/clip/data/style sur le serveur) : envoie les .jpg à Claude."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ..config import get_settings


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


def cuts(path: Path, threshold: float = 0.3) -> list[float]:
    """Instants des changements de plan détectés par ffmpeg."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf",
                           f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                          capture_output=True, text=True)
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", proc.stderr)]


def sheet(path: Path, times: list[float], out: Path, cols: int = 4, w: int = 270) -> Path:
    """Planche contact des images aux instants donnés (avec l'instant écrit dessus)."""
    from PIL import Image, ImageDraw

    tiles = []
    for t in times:
        frame = out.with_suffix(f".{len(tiles)}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={w}:-2", str(frame)], check=False)
        if frame.exists():
            im = Image.open(frame).convert("RGB")
            ImageDraw.Draw(im).text((8, 8), f"{t:.1f}s", fill=(255, 0, 0))
            tiles.append(im)
            frame.unlink()
    if not tiles:
        raise RuntimeError("aucune image extraite")
    h = max(t.height for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    board = Image.new("RGB", (cols * w, rows * h), (0, 0, 0))
    for k, im in enumerate(tiles):
        board.paste(im, ((k % cols) * w, (k // cols) * h))
    board.save(out, quality=88)
    return out


def probe(url: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    vid = re.sub(r"\W", "", url.rstrip("/").split("/")[-1].split("?")[0])[-24:] or "video"
    src = out_dir / f"{vid}.mp4"
    if not src.exists():
        subprocess.run(["yt-dlp", "-q", "-f", "mp4/best[height<=1280]/best", "-o", str(src), url], check=True)
    dur = _duration(src)
    cs = cuts(src)
    sheet(src, [dur * (k + 0.5) / 12 for k in range(12)], out_dir / f"{vid}_planche.jpg")
    sheet(src, [0.1 + 0.36 * k for k in range(8)], out_dir / f"{vid}_debut.jpg")
    info = {"url": url, "duree_s": round(dur, 1), "plans": len(cs) + 1,
            "duree_moyenne_plan_s": round(dur / (len(cs) + 1), 2) if dur else 0,
            "changements_de_plan_s": [round(c, 1) for c in cs][:80]}
    (out_dir / f"{vid}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1))
    return info


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    out = get_settings().data_dir / "style"
    for url in argv:
        try:
            info = probe(url, out)
            print(f"OK {url} : {info['duree_s']} s, {info['plans']} plans (~{info['duree_moyenne_plan_s']} s/plan)")
        except Exception as e:
            print(f"ÉCHEC {url} : {e}")
    print(f"Fichiers dans {out} (sur le serveur : ~/clip/data/style)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
