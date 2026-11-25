"""Envoi sur YouTube (Shorts) via l'API YouTube Data v3."""
from __future__ import annotations

import logging
import os
from datetime import date

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import get_settings
from . import creds

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]  # rétention (statistiques détaillées)
ANALYTICS_SCOPE = SCOPES[2]
# Google peut accorder moins de droits que demandés (cases décochées) : ne pas faire échouer la connexion.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
TOKEN_URI = "https://oauth2.googleapis.com/token"


def redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/auth/youtube/callback"


def configured() -> bool:
    s = get_settings()
    return bool(s.youtube_client_id and s.youtube_client_secret)


def _flow(state: str | None = None) -> Flow:
    s = get_settings()
    cfg = {"web": {"client_id": s.youtube_client_id, "client_secret": s.youtube_client_secret,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": TOKEN_URI}}
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=redirect_uri(), state=state)


def auth_url(state: str) -> tuple[str, str | None]:
    """Renvoie (url, code_verifier) ; le verifier doit être rendu à finish_auth."""
    flow = _flow(state)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url, getattr(flow, "code_verifier", None)


def finish_auth(code: str, state: str, code_verifier: str | None = None) -> None:
    flow = _flow(state)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    c = flow.credentials
    name = ""
    try:
        yt = build("youtube", "v3", credentials=c, cache_discovery=False)
        items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
        name = items[0]["snippet"]["title"] if items else ""
    except Exception:
        pass
    scopes = list(getattr(c, "granted_scopes", None) or c.scopes or SCOPES)
    creds.save("youtube", {"token": c.token, "refresh_token": c.refresh_token, "scopes": scopes}, account=name)


class AuthExpired(RuntimeError):
    """Google a révoqué ou laissé expirer l'accès (7 jours si l'appli Google est « en test ») :
    il faut reconnecter la chaîne dans Comptes. Les clips restent en file en attendant."""


def expired() -> bool:
    """Accès YouTube expiré : envois en pause jusqu'à la reconnexion."""
    return bool((creds.load("youtube") or {}).get("expired"))


def _is_revoked(e: Exception) -> bool:
    return isinstance(e, RefreshError) and "invalid_grant" in str(e)


def _mark_expired(e: Exception) -> AuthExpired:
    data = creds.load("youtube")
    if data:
        creds.save("youtube", {**data, "expired": True})
    return AuthExpired(f"Accès YouTube expiré ou révoqué : reconnecte la chaîne dans Comptes ({e})")


def _credentials() -> Credentials:
    s = get_settings()
    data = creds.load("youtube")
    if not data:
        raise RuntimeError("Compte YouTube non connecté")
    if data.get("expired"):
        raise AuthExpired("Accès YouTube expiré : reconnecte la chaîne dans Comptes")
    # scopes réellement accordés (absent pour une connexion faite avant l'ajout des statistiques)
    c = Credentials(data.get("token"), refresh_token=data.get("refresh_token"), token_uri=TOKEN_URI,
                    client_id=s.youtube_client_id, client_secret=s.youtube_client_secret, scopes=data.get("scopes"))
    if not c.valid:
        try:
            c.refresh(Request())
        except RefreshError as e:
            if _is_revoked(e):
                raise _mark_expired(e) from e
            raise
        creds.save("youtube", {**data, "token": c.token, "refresh_token": c.refresh_token})
    return c


def has_analytics() -> bool:
    return ANALYTICS_SCOPE in ((creds.load("youtube") or {}).get("scopes") or [])


def stats(video_ids: list[str]) -> dict[str, dict]:
    """Vues, likes, commentaires (+ rétention si l'accès « statistiques » a été accordé)."""
    if not video_ids:
        return {}
    c = _credentials()
    yt = build("youtube", "v3", credentials=c, cache_discovery=False)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        for item in yt.videos().list(part="statistics", id=",".join(batch)).execute().get("items", []):
            st = item.get("statistics", {})
            out[item["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                               "comments": int(st.get("commentCount", 0))}
    if has_analytics():
        try:
            retention(c, list(out), out)
            _analytics_error("")
        except Exception as e:  # API Analytics pas activée dans la console Google, etc.
            logging.getLogger(__name__).warning("Statistiques YouTube Analytics indisponibles : %s", e)
            _analytics_error(str(e)[:300])
    return out


def retention(c, video_ids: list[str], out: dict[str, dict]) -> None:
    """Rétention (% regardé, durée moyenne) et partages via YouTube Analytics, ajoutés à `out`.

    Le rapport par vidéo exige un tri et maxResults ; les données arrivent avec 1 à 2 jours de retard."""
    ya = build("youtubeAnalytics", "v2", credentials=c, cache_discovery=False)
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = ya.reports().query(
            ids="channel==MINE", startDate="2020-01-01", endDate=date.today().isoformat(),
            metrics="views,averageViewDuration,averageViewPercentage,shares", dimensions="video",
            filters="video==" + ",".join(batch), sort="-views", maxResults=len(batch)).execute()
        cols = [h.get("name") for h in resp.get("columnHeaders", [])] or [
            "video", "views", "averageViewDuration", "averageViewPercentage", "shares"]
        for row in resp.get("rows", []):
            r = dict(zip(cols, row))
            vid = r.get("video")
            if vid in out:
                out[vid].update(avg_view_s=float(r.get("averageViewDuration") or 0),
                                avg_view_pct=float(r.get("averageViewPercentage") or 0),
                                shares=int(r.get("shares") or 0))


def _analytics_error(msg: str) -> None:
    """Dernière erreur YouTube Analytics, affichée dans Statistiques ("" = tout va bien)."""
    try:
        from .. import platforms
        platforms._put("analytics_error", msg or None)
    except Exception:
        pass


def analytics_error() -> str:
    try:
        from .. import platforms
        return platforms._get("analytics_error") or ""
    except Exception:
        return ""


class QuotaExceeded(RuntimeError):
    """YouTube refuse l'envoi : quota de l'API ou limite d'envois de la chaîne atteint pour la journée."""


QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded")


def is_quota_error(e: Exception) -> bool:
    if not isinstance(e, HttpError) or getattr(e.resp, "status", 0) not in (403, 429):
        return False
    text = str(getattr(e, "content", b"") or b"", "utf-8", "ignore") + str(e)
    return any(r in text for r in QUOTA_REASONS)


def upload(path: str, title: str, description: str, tags: list[str]) -> str:
    try:
        return _upload(path, title, description, tags)
    except HttpError as e:
        if is_quota_error(e):
            raise QuotaExceeded(str(e)[:500]) from e
        raise
    except RefreshError as e:  # jeton rafraîchi pendant l'envoi
        if _is_revoked(e):
            raise _mark_expired(e) from e
        raise


def requeue_auth_failures() -> int:
    """Après reconnexion : les envois abandonnés à cause de l'accès expiré repartent en file."""
    from sqlmodel import select

    from .. import db
    from ..models import Post

    n = 0
    with db.session() as s:
        for p in s.exec(select(Post).where(Post.platform == "youtube", Post.status == "failed")).all():
            if "invalid_grant" in (p.error or "") or "expiré" in (p.error or ""):
                p.status, p.attempts, p.error = "pending", 0, ""
                s.add(p)
                n += 1
        s.commit()
    return n


def _upload(path: str, title: str, description: str, tags: list[str]) -> str:
    s = get_settings()
    if "#shorts" not in (title + description).lower():
        description = description + "\n#Shorts"
    yt = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:15],
                    "categoryId": s.youtube_category, "defaultLanguage": "fr"},
        "status": {"privacyStatus": s.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
