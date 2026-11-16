"""Découverte des nouvelles vidéos / lives et téléchargement (yt-dlp)."""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)

FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"


@dataclass
class Found:
    key: str
    url: str
    title: str
    is_live: bool = False


def detect_platform(url: str) -> str:
    if "twitch.tv" in url:
        return "twitch"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    raise ValueError("URL non reconnue (Twitch ou YouTube attendu)")


def normalize_channel_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if detect_platform(url) == "youtube":
        url = re.sub(r"/(videos|streams|live|shorts|featured)$", "", url)
    else:
        m = re.search(r"twitch\.tv/([^/?#]+)", url)
        if m:
            url = f"https://www.twitch.tv/{m.group(1)}"
    return url


def _ydl(extra: dict | None = None) -> yt_dlp.YoutubeDL:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _list_entries(url: str, limit: int) -> list[dict]:
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return [e for e in (info.get("entries") or []) if e]


def _live_info(url: str) -> dict | None:
    try:
        with _ydl() as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # pas en live
        return None
    if info and info.get("is_live"):
        return info
    return None


def discover(platform: str, channel_url: str, record_lives: bool, include_videos: bool, limit: int = 5) -> list[Found]:
    found: list[Found] = []
    if platform == "youtube":
        if record_lives:
            info = _live_info(channel_url + "/live")
            if info:
                found.append(Found(f"youtube:{info['id']}", f"https://www.youtube.com/watch?v={info['id']}", info.get("title", ""), True))
        if include_videos:
            for tab in ("videos", "streams"):
                for e in _list_entries(f"{channel_url}/{tab}", limit):
                    if e.get("live_status") in ("is_live", "is_upcoming"):
                        continue
                    found.append(Found(f"youtube:{e['id']}", f"https://www.youtube.com/watch?v={e['id']}", e.get("title", "")))
    else:
        if record_lives:
            info = _live_info(channel_url)
            if info:
                found.append(Found(f"twitch-live:{info.get('id')}", channel_url, info.get("description") or info.get("title", ""), True))
        if include_videos:
            for e in _list_entries(f"{channel_url}/videos?filter=archives&sort=time", limit):
                vid = str(e.get("id", "")).lstrip("v")
                found.append(Found(f"twitch:{vid}", f"https://www.twitch.tv/videos/{vid}", e.get("title", "")))
    # dédoublonnage en gardant l'ordre
    seen, out = set(), []
    for f in found:
        if f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return out


def probe_url(url: str) -> Found:
    """Infos d'une URL ajoutée à la main."""
    with _ydl({"ignoreerrors": False}) as ydl:
        info = ydl.extract_info(url, download=False)
    extractor = (info.get("extractor_key") or "").lower()
    plat = "twitch" if "twitch" in extractor else "youtube" if "youtube" in extractor else extractor
    live = bool(info.get("is_live"))
    key = f"{plat}{'-live' if live and plat == 'twitch' else ''}:{info['id']}"
    return Found(key, info.get("webpage_url") or url, info.get("title", ""), live)


def download_cmd(url: str, out_dir: Path, live: bool) -> list[str]:
    cmd = [
        "yt-dlp", "--no-progress", "--no-part", "-f", FORMAT,
        "-o", str(out_dir / "source.%(ext)s"), "--retries", "10", "--fragment-retries", "50",
    ]
    if live:
        cmd += ["--hls-use-mpegts", "--no-live-from-start"]
    else:
        cmd += ["--merge-output-format", "mp4"]
    return cmd + [url]


def find_output(out_dir: Path) -> Path | None:
    files = [p for p in out_dir.glob("source.*") if p.suffix not in (".part", ".ytdl", ".json")]
    return max(files, key=lambda p: p.stat().st_size) if files else None


class Cancelled(Exception):
    """Téléchargement arrêté parce que la vidéo a été ignorée."""


def download(url: str, out_dir: Path, should_stop=None) -> Path:
    """Télécharge une vidéo. `should_stop()` est vérifié toutes les 2 s pour pouvoir l'annuler."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "download.log"
    with open(log_file, "w") as err:
        proc = subprocess.Popen(download_cmd(url, out_dir, False), stdout=subprocess.DEVNULL, stderr=err)
        while proc.poll() is None:
            if should_stop and should_stop():
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled()
            time.sleep(2)
    path = find_output(out_dir)
    if proc.returncode != 0 or not path:
        tail = log_file.read_text(errors="replace")[-500:] if log_file.exists() else ""
        raise RuntimeError(f"Téléchargement échoué : {tail}")
    return path
