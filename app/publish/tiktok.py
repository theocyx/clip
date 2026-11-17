"""Publication TikTok via la Content Posting API (v2)."""
from __future__ import annotations

import os
import time
import urllib.parse

import httpx

from ..config import get_settings
from . import creds

API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish,video.upload"
MB = 1024 * 1024


def redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/auth/tiktok/callback"


def configured() -> bool:
    s = get_settings()
    return bool(s.tiktok_client_key and s.tiktok_client_secret)


def scopes() -> str:
    return SCOPES + (",video.list" if get_settings().tiktok_stats else "")


def auth_url(state: str) -> str:
    q = {"client_key": get_settings().tiktok_client_key, "scope": scopes(), "response_type": "code",
         "redirect_uri": redirect_uri(), "state": state}
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)


def _token_request(data: dict) -> dict:
    s = get_settings()
    r = httpx.post(f"{API}/oauth/token/", data={"client_key": s.tiktok_client_key,
                                                "client_secret": s.tiktok_client_secret, **data},
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    tok = r.json()
    if "access_token" not in tok:
        raise RuntimeError(f"TikTok OAuth : {tok}")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 86400)) - 120
    return tok


def finish_auth(code: str) -> None:
    tok = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    name = ""
    try:
        r = httpx.get(f"{API}/user/info/", params={"fields": "display_name"},
                      headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
        name = r.json()["data"]["user"]["display_name"]
    except Exception:
        pass
    creds.save("tiktok", tok, account=name)


def _access_token() -> str:
    tok = creds.load("tiktok")
    if not tok:
        raise RuntimeError("Compte TikTok non connecté")
    if time.time() >= tok.get("expires_at", 0):
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        creds.save("tiktok", tok)
    return tok["access_token"]


def chunk_plan(size: int) -> tuple[int, int]:
    """(taille de morceau, nombre de morceaux) selon les règles TikTok (5-64 Mo, dernier ≤ 128 Mo)."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


def _check(r: httpx.Response) -> dict:
    data = r.json()
    err = data.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok : {err.get('code')} – {err.get('message')}")
    return data.get("data", {})


def upload(path: str, title: str) -> str:
    s = get_settings()
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"}
    size = os.path.getsize(path)
    chunk, count = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}

    with httpx.Client(timeout=120) as http:
        if s.tiktok_mode == "draft":
            init = _check(http.post(f"{API}/post/publish/inbox/video/init/", headers=headers, json={"source_info": source}))
        else:
            info = _check(http.post(f"{API}/post/publish/creator_info/query/", headers=headers))
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            privacy = s.tiktok_privacy if s.tiktok_privacy in options else "SELF_ONLY"
            body = {"post_info": {"title": title[:2200], "privacy_level": privacy, "disable_duet": False,
                                  "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                    "source_info": source}
            init = _check(http.post(f"{API}/post/publish/video/init/", headers=headers, json=body))
        publish_id, upload_url = init["publish_id"], init["upload_url"]

        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                length = size - start if i == count - 1 else chunk
                f.seek(start)
                data = f.read(length)
                r = http.put(upload_url, content=data, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}"}, timeout=600)
                if r.status_code not in (200, 201, 206):
                    raise RuntimeError(f"TikTok envoi morceau {i}: HTTP {r.status_code} {r.text[:200]}")

        for _ in range(60):  # jusqu'à ~10 min
            time.sleep(10)
            st = _check(http.post(f"{API}/post/publish/status/fetch/", headers=headers, json={"publish_id": publish_id}))
            status = st.get("status")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                return str(ids[0]) if ids else publish_id
            if status == "FAILED":
                raise RuntimeError(f"TikTok : échec ({st.get('fail_reason')})")
    return publish_id


def stats(video_ids: list[str]) -> dict[str, dict]:
    """Vues, likes, commentaires, partages (scope video.list). Seuls les vrais identifiants de vidéo comptent."""
    ids = [v for v in video_ids if v.isdigit()]
    if not ids or not get_settings().tiktok_stats:
        return {}
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"}
    out: dict[str, dict] = {}
    with httpx.Client(timeout=60) as http:
        for i in range(0, len(ids), 20):
            data = _check(http.post(f"{API}/video/query/", headers=headers,
                                    params={"fields": "id,view_count,like_count,comment_count,share_count"},
                                    json={"filters": {"video_ids": ids[i:i + 20]}}))
            for v in data.get("videos", []):
                out[str(v["id"])] = {"views": int(v.get("view_count", 0)), "likes": int(v.get("like_count", 0)),
                                     "comments": int(v.get("comment_count", 0)), "shares": int(v.get("share_count", 0))}
    return out
