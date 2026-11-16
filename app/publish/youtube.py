"""Envoi sur YouTube (Shorts) via l'API YouTube Data v3."""
from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from ..config import get_settings
from . import creds

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
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
    creds.save("youtube", {"token": c.token, "refresh_token": c.refresh_token}, account=name)


def _credentials() -> Credentials:
    s = get_settings()
    data = creds.load("youtube")
    if not data:
        raise RuntimeError("Compte YouTube non connecté")
    c = Credentials(data.get("token"), refresh_token=data.get("refresh_token"), token_uri=TOKEN_URI,
                    client_id=s.youtube_client_id, client_secret=s.youtube_client_secret, scopes=SCOPES)
    if not c.valid:
        c.refresh(Request())
        creds.save("youtube", {"token": c.token, "refresh_token": c.refresh_token})
    return c


def upload(path: str, title: str, description: str, tags: list[str]) -> str:
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
