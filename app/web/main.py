"""Interface web d'administration."""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import col, func, select

from .. import db
from ..config import get_settings
from ..models import Clip, Post, Source, Video
from ..pipeline import process, sources
from ..publish import creds, tiktok, youtube
from . import mfa

# Pages accessibles sans connexion (exigées par TikTok / Google, et la connexion elle-même).
PUBLIC_PATHS = {"/conditions", "/confidentialite", "/login", "/mfa/setup"}
VERIFY_FILE = re.compile(r"^/[A-Za-z0-9_-]{1,100}\.txt$")


class LoginRequired(Exception):
    pass


def auth(request: Request):
    path = request.url.path
    if path in PUBLIC_PATHS or VERIFY_FILE.match(path):
        return
    if not mfa.valid_token("session", request.cookies.get(mfa.SESSION_COOKIE)):
        if request.method == "GET":
            raise LoginRequired()
        raise HTTPException(401, "Connexion requise")


app = FastAPI(title="Clip", dependencies=[Depends(auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.exception_handler(LoginRequired)
def _to_login(request: Request, _exc: LoginRequired):
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
_oauth_states: dict[str, dict] = {}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@app.on_event("startup")
def _startup():
    db.get_engine()


# ---------- Connexion (mot de passe + code MFA) ----------

def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


def _set_cookie(request: Request, resp, name: str, value: str, max_age: int):
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" \
        or get_settings().public_url.startswith("https")
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", secure=https)


def _login_page(request: Request, error: str = "", nxt: str = "/", status: int = 200):
    return templates.TemplateResponse(request, "login.html", {
        "error": error, "next": _safe_next(nxt), "need_code": get_settings().mfa and mfa.configured()},
        status_code=status)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    return _login_page(request, nxt=next)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), code: str = Form(""),
          next: str = Form("/")):
    if mfa.blocked():
        return _login_page(request, "Trop d'essais ratés. Réessaie dans 15 minutes.", next, 429)
    s = get_settings()
    ok = mfa.check_password(username, password)
    if ok and s.mfa and not mfa.configured():
        resp = back("/mfa/setup?next=" + quote(_safe_next(next)))
        _set_cookie(request, resp, mfa.SETUP_COOKIE, mfa.make_token("setup", mfa.SETUP_TTL), mfa.SETUP_TTL)
        return resp
    if ok and s.mfa:
        ok = mfa.verify_code(code)
    if not ok:
        mfa.record_failure()
        return _login_page(request, "Identifiant, mot de passe ou code incorrect.", next, 401)
    resp = back(_safe_next(next))
    _set_cookie(request, resp, mfa.SESSION_COOKIE, mfa.make_token("session", mfa.session_ttl()), mfa.session_ttl())
    return resp


def _setup_allowed(request: Request) -> bool:
    return get_settings().mfa and not mfa.configured() and mfa.valid_token(
        "setup", request.cookies.get(mfa.SETUP_COOKIE))


def _setup_page(request: Request, nxt: str, error: str = "", status: int = 200):
    secret = mfa.pending_secret()
    return templates.TemplateResponse(request, "mfa_setup.html", {
        "qr": mfa.qr_svg(mfa.provisioning_uri(secret)), "secret": secret, "error": error,
        "next": _safe_next(nxt)}, status_code=status)


@app.get("/mfa/setup")
def mfa_setup_form(request: Request, next: str = "/"):
    if not _setup_allowed(request):
        return back("/login")
    return _setup_page(request, next)


@app.post("/mfa/setup")
def mfa_setup(request: Request, code: str = Form(""), next: str = Form("/")):
    if not _setup_allowed(request):
        return back("/login")
    if mfa.blocked():
        return _setup_page(request, next, "Trop d'essais ratés. Réessaie dans 15 minutes.", 429)
    if not mfa.confirm_setup(code):
        mfa.record_failure()
        return _setup_page(request, next, "Code incorrect, vérifie l'heure du téléphone et réessaie.", 400)
    resp = back(_safe_next(next))
    resp.delete_cookie(mfa.SETUP_COOKIE)
    _set_cookie(request, resp, mfa.SESSION_COOKIE, mfa.make_token("session", mfa.session_ttl()), mfa.session_ttl())
    return resp


@app.post("/logout")
def logout():
    resp = back("/login")
    resp.delete_cookie(mfa.SESSION_COOKIE)
    return resp


# ---------- Tableau de bord ----------

@app.get("/")
def dashboard(request: Request):
    with db.session() as s:
        counts = dict(s.exec(select(Video.status, func.count()).group_by(Video.status)).all())
        posts = dict(s.exec(select(Post.status, func.count()).group_by(Post.status)).all())
        videos = s.exec(select(Video).where(Video.status != "skipped").order_by(col(Video.created_at).desc()).limit(10)).all()
        n_sources = s.exec(select(func.count()).select_from(Source)).one()
    return templates.TemplateResponse(request, "dashboard.html", {
        "counts": counts, "posts": posts, "videos": videos, "n_sources": n_sources, "accounts": accounts_status()})


def accounts_status() -> dict:
    return {
        "youtube": {"configured": youtube.configured(), "account": creds.account("youtube"),
                    "connected": creds.load("youtube") is not None},
        "tiktok": {"configured": tiktok.configured(), "account": creds.account("tiktok"),
                   "connected": creds.load("tiktok") is not None},
    }


# ---------- Sources ----------

@app.get("/sources")
def list_sources(request: Request):
    with db.session() as s:
        items = s.exec(select(Source).order_by(col(Source.created_at))).all()
    return templates.TemplateResponse(request, "sources.html", {"sources": items})


@app.post("/sources")
def add_source(url: str = Form(...), name: str = Form(""), record_lives: bool = Form(False),
               include_videos: bool = Form(False)):
    try:
        platform = sources.detect_platform(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    url = sources.normalize_channel_url(url)
    with db.session() as s:
        s.add(Source(name=name or url.rsplit("/", 1)[-1], platform=platform, url=url,
                     record_lives=record_lives, include_videos=include_videos))
        s.commit()
    return back("/sources")


@app.post("/sources/{sid}/toggle")
def toggle_source(sid: int):
    with db.session() as s:
        src = s.get(Source, sid)
        src.enabled = not src.enabled
        s.add(src)
        s.commit()
    return back("/sources")


@app.post("/sources/{sid}/delete")
def delete_source(sid: int):
    with db.session() as s:
        src = s.get(Source, sid)
        if src:
            for v in s.exec(select(Video).where(Video.source_id == sid)).all():
                v.source_id = None
                s.add(v)
            s.delete(src)
            s.commit()
    return back("/sources")


# ---------- Vidéos ----------

@app.get("/videos")
def list_videos(request: Request, all: bool = False):
    with db.session() as s:
        q = select(Video).order_by(col(Video.created_at).desc()).limit(200)
        if not all:
            q = q.where(Video.status != "skipped")
        items = s.exec(q).all()
    return templates.TemplateResponse(request, "videos.html", {"videos": items, "all": all})


@app.post("/videos")
def add_video(url: str = Form(...)):
    try:
        f = sources.probe_url(url.strip())
    except Exception as e:
        raise HTTPException(400, f"URL illisible : {e}")
    with db.session() as s:
        if not s.exec(select(Video).where(Video.key == f.key)).first():
            s.add(Video(key=f.key, url=f.url, title=f.title, is_live=f.is_live))
            s.commit()
    return back("/videos")


@app.post("/videos/{vid}/retry")
def retry_video(vid: int):
    with db.session() as s:
        v = s.get(Video, vid)
        v.status, v.error = ("downloaded" if v.path and Path(v.path).exists() else "new"), ""
        if v.status == "new":
            v.is_live = False  # on retélécharge la vidéo (rediffusion) au lieu d'attendre un live
        s.add(v)
        s.commit()
    return back("/videos")


@app.post("/videos/{vid}/delete")
def delete_video(vid: int):
    with db.session() as s:
        v = s.get(Video, vid)
        if v:
            v.status = "skipped"
            s.add(v)
            s.commit()
    process.delete_video_files(vid)
    return back("/videos")


# ---------- Clips ----------

@app.get("/clips")
def list_clips(request: Request):
    with db.session() as s:
        clips = s.exec(select(Clip).where(Clip.status != "deleted").order_by(col(Clip.created_at).desc()).limit(100)).all()
        posts = s.exec(select(Post).where(col(Post.clip_id).in_([c.id for c in clips]))).all()
    by_clip: dict[int, list[Post]] = {}
    for p in posts:
        by_clip.setdefault(p.clip_id, []).append(p)
    return templates.TemplateResponse(request, "clips.html", {"clips": clips, "posts": by_clip})


@app.get("/clips/{cid}/video")
def clip_video(cid: int):
    with db.session() as s:
        c = s.get(Clip, cid)
    if not c or not Path(c.path).exists():
        raise HTTPException(404)
    return FileResponse(c.path, media_type="video/mp4")


@app.post("/clips/{cid}/title")
def clip_title(cid: int, title: str = Form(...)):
    with db.session() as s:
        c = s.get(Clip, cid)
        c.title = title.strip()[:100]
        s.add(c)
        s.commit()
    return back("/clips")


@app.post("/clips/{cid}/publish")
def clip_publish(cid: int):
    with db.session() as s:
        c = s.get(Clip, cid)
        process.queue_posts(s, c)
        for p in s.exec(select(Post).where(Post.clip_id == cid, col(Post.status).in_(["failed", "cancelled"]))).all():
            p.status, p.attempts, p.error = "pending", 0, ""
            s.add(p)
        s.commit()
    return back("/clips")


@app.post("/clips/{cid}/delete")
def clip_delete(cid: int):
    with db.session() as s:
        c = s.get(Clip, cid)
        c.status = "deleted"
        s.add(c)
        for p in s.exec(select(Post).where(Post.clip_id == cid, Post.status == "pending")).all():
            p.status = "cancelled"
            s.add(p)
        s.commit()
    Path(c.path).unlink(missing_ok=True)
    return back("/clips")


# ---------- Comptes (OAuth) ----------

@app.get("/accounts")
def accounts(request: Request):
    return templates.TemplateResponse(request, "accounts.html", {
        "accounts": accounts_status(), "yt_redirect": youtube.redirect_uri(), "tt_redirect": tiktok.redirect_uri()})


@app.get("/auth/youtube")
def auth_youtube():
    if not youtube.configured():
        raise HTTPException(400, "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET manquants dans .env")
    state = secrets.token_urlsafe(16)
    url, verifier = youtube.auth_url(state)
    _oauth_states[state] = {"platform": "youtube", "verifier": verifier}
    return RedirectResponse(url)


@app.get("/auth/youtube/callback")
def auth_youtube_cb(code: str, state: str):
    st = _oauth_states.pop(state, None)
    if not st or st["platform"] != "youtube":
        raise HTTPException(400, "État OAuth invalide, recommence la connexion")
    youtube.finish_auth(code, state, st.get("verifier"))
    return back("/accounts")


@app.get("/auth/tiktok")
def auth_tiktok():
    if not tiktok.configured():
        raise HTTPException(400, "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = {"platform": "tiktok"}
    return RedirectResponse(tiktok.auth_url(state))


@app.get("/auth/tiktok/callback")
def auth_tiktok_cb(code: str, state: str):
    st = _oauth_states.pop(state, None)
    if not st or st["platform"] != "tiktok":
        raise HTTPException(400, "État OAuth invalide, recommence la connexion")
    tiktok.finish_auth(code)
    return back("/accounts")


@app.post("/accounts/{platform}/disconnect")
def disconnect(platform: str):
    creds.delete(platform)
    return back("/accounts")


# ---------- Pages publiques ----------

_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.6}}</style>
</head><body><h1>{title}</h1>{body}</body></html>"""

_TERMS = """<p>Clip est un outil personnel qui crée des extraits courts à partir de vidéos et les publie
sur les comptes YouTube et TikTok de son propriétaire, avec son autorisation.</p>
<p>Seul le propriétaire du serveur utilise ce service. Il est responsable des contenus publiés
et du respect des droits des créateurs d'origine, qui sont crédités dans chaque publication.</p>
<p>Le service est fourni tel quel, sans garantie. L'accès peut être retiré à tout moment
depuis les paramètres de YouTube ou de TikTok.</p>"""

_PRIVACY = """<p>Clip fonctionne sur un serveur privé. Les seules données traitées sont :</p>
<ul><li>les jetons de connexion YouTube et TikTok du propriétaire, stockés uniquement sur ce serveur ;</li>
<li>les informations de base du compte (nom, identifiant) renvoyées par YouTube et TikTok ;</li>
<li>les vidéos et clips créés.</li></ul>
<p>Aucune donnée n'est vendue, partagée ou envoyée à un tiers, en dehors des vidéos publiées
volontairement sur YouTube et TikTok. Pour supprimer les données, déconnecte les comptes
dans Clip ou retire l'accès depuis les paramètres de YouTube ou de TikTok.</p>"""


@app.get("/conditions", response_class=HTMLResponse)
def page_terms():
    return _PAGE.format(title="Conditions d'utilisation", body=_TERMS)


@app.get("/confidentialite", response_class=HTMLResponse)
def page_privacy():
    return _PAGE.format(title="Politique de confidentialité", body=_PRIVACY)


@app.get("/{name}.txt", response_class=PlainTextResponse)
def verify_file(name: str):
    """Fichiers de vérification de domaine (ex. TikTok), à déposer dans data/verify/."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
        raise HTTPException(404)
    f = get_settings().data_dir / "verify" / f"{name}.txt"
    if not f.is_file():
        raise HTTPException(404)
    return f.read_text()
