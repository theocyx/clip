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
AUTH_ERROR_KEY = "youtube_auth_error"


class AuthExpired(RuntimeError):
    """Jeton YouTube expiré ou révoqué : il faut reconnecter la chaîne dans l'interface (Comptes).

    Cas typique : appli Google Cloud restée en mode « Test », dont les jetons expirent au bout de 7 jours."""


def auth_error() -> str:
    """Raison de la dernière connexion refusée par Google ("" = connexion valide)."""
    try:
        from .. import platforms
        return platforms._get(AUTH_ERROR_KEY) or ""
    except Exception:
        return ""


def _set_auth_error(msg: str | None) -> None:
    from .. import platforms
    platforms._put(AUTH_ERROR_KEY, msg)


def _expired(e: Exception) -> AuthExpired:
    msg = str(e)[:300]
    _set_auth_error(msg or "jeton refusé")
    return AuthExpired(msg)


def is_auth_error(e: Exception) -> bool:
    """Google refuse définitivement le jeton (invalid_grant) : inutile de réessayer avant une reconnexion."""
    return isinstance(e, RefreshError) and "invalid_grant" in str(e)


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
    _set_auth_error(None)


def _credentials() -> Credentials:
    s = get_settings()
    data = creds.load("youtube")
    if not data:
        raise RuntimeError("Compte YouTube non connecté")
    # scopes réellement accordés (absent pour une connexion faite avant l'ajout des statistiques)
    c = Credentials(data.get("token"), refresh_token=data.get("refresh_token"), token_uri=TOKEN_URI,
                    client_id=s.youtube_client_id, client_secret=s.youtube_client_secret, scopes=data.get("scopes"))
    if not c.valid:
        try:
            c.refresh(Request())
        except RefreshError as e:
            if is_auth_error(e):
                raise _expired(e) from e
            raise
        creds.save("youtube", {**data, "token": c.token, "refresh_token": c.refresh_token})
    return c


def has_analytics() -> bool:
    return ANALYTICS_SCOPE in ((creds.load("youtube") or {}).get("scopes") or [])


def stats(video_ids: list[str]) -> dict[str, dict]:
    """Vues, likes, commentaires (+ rétention si l'accès « statistiques » a été accordé)."""
    if not video_ids:
        return {}
    if auth_error():
        raise AuthExpired("Connexion YouTube expirée : reconnecte la chaîne")
    c = _credentials()
    yt = build("youtube", "v3", credentials=c, cache_discovery=False)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            items = yt.videos().list(part="statistics", id=",".join(batch)).execute().get("items", [])
        except RefreshError as e:
            if is_auth_error(e):
                raise _expired(e) from e
            raise
        for item in items:
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


BASE_METRICS = "views,averageViewDuration,averageViewPercentage,shares"
# abonnés gagnés par vidéo (ce qui fait grandir la chaîne) et minutes regardées (heures de visionnage)
GROWTH_METRICS = BASE_METRICS + ",subscribersGained,estimatedMinutesWatched"


def retention(c, video_ids: list[str], out: dict[str, dict]) -> None:
    """Rétention (% regardé, durée moyenne), partages, abonnés gagnés et minutes regardées via YouTube Analytics,
    ajoutés à `out`.

    Le rapport par vidéo exige un tri et maxResults ; les données arrivent avec 1 à 2 jours de retard."""
    ya = build("youtubeAnalytics", "v2", credentials=c, cache_discovery=False)
    metrics = GROWTH_METRICS
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]

        def query(m: str):
            return ya.reports().query(
                ids="channel==MINE", startDate="2020-01-01", endDate=date.today().isoformat(),
                metrics=m, dimensions="video", filters="video==" + ",".join(batch), sort="-views",
                maxResults=len(batch)).execute()
        try:
            resp = query(metrics)
        except HttpError:
            if metrics == BASE_METRICS:
                raise
            metrics = BASE_METRICS  # métriques en plus refusées : on garde au moins la rétention
            resp = query(metrics)
        cols = [h.get("name") for h in resp.get("columnHeaders", [])] or ["video"] + metrics.split(",")
        for row in resp.get("rows", []):
            r = dict(zip(cols, row))
            vid = r.get("video")
            if vid in out:
                out[vid].update(avg_view_s=float(r.get("averageViewDuration") or 0),
                                avg_view_pct=float(r.get("averageViewPercentage") or 0),
                                shares=int(r.get("shares") or 0))
                if "subscribersGained" in r:
                    out[vid]["subs"] = int(r.get("subscribersGained") or 0)
                if "estimatedMinutesWatched" in r:
                    out[vid]["minutes"] = float(r.get("estimatedMinutesWatched") or 0)


def channel() -> dict:
    """Chaîne connectée : id, nom, abonnés, vues et nombre de vidéos ({} si indisponible)."""
    if auth_error() or not configured() or not creds.load("youtube"):
        return {}
    try:
        yt = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
        items = yt.channels().list(part="id,snippet,statistics", mine=True).execute().get("items", [])
    except Exception as e:
        logging.getLogger(__name__).warning("Infos de la chaîne YouTube indisponibles : %s", e)
        return {}
    if not items:
        return {}
    st = items[0].get("statistics", {})
    return {"id": items[0]["id"], "title": items[0].get("snippet", {}).get("title", ""),
            "subs": int(st.get("subscriberCount", 0) or 0), "views": int(st.get("viewCount", 0) or 0),
            "videos": int(st.get("videoCount", 0) or 0), "hidden": bool(st.get("hiddenSubscriberCount"))}


def set_thumbnail(video_id: str, path: str) -> bool:
    """Miniature personnalisée (vidéos longues). Refusée tant que la chaîne n'est pas validée par téléphone."""
    try:
        yt = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
        yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(path, mimetype="image/jpeg")).execute()
        return True
    except Exception as e:
        logging.getLogger(__name__).warning("Miniature refusée par YouTube (chaîne pas validée ?) : %s", e)
        return False


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


def upload(path: str, title: str, description: str, tags: list[str], shorts: bool = True,
           category: str = "") -> str:
    """Envoie la vidéo. shorts=False : vidéo longue (compilation), sans #Shorts."""
    try:
        return _upload(path, title, description, tags, shorts, category)
    except HttpError as e:
        if is_quota_error(e):
            raise QuotaExceeded(str(e)[:500]) from e
        raise
    except RefreshError as e:
        if is_auth_error(e):
            raise _expired(e) from e
        raise


def _upload(path: str, title: str, description: str, tags: list[str], shorts: bool = True,
            category: str = "") -> str:
    s = get_settings()
    if shorts and "#shorts" not in (title + description).lower():
        description = description + "\n#Shorts"
    yt = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:15],
                    "categoryId": category or s.youtube_category, "defaultLanguage": "fr",
                    "defaultAudioLanguage": "fr"},
        "status": {"privacyStatus": s.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
