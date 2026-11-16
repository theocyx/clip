"""Interface web d'administration."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlmodel import col, func, select

from .. import db
from ..config import get_settings
from ..models import Clip, Post, Source, Video
from ..pipeline import process, sources
from ..publish import creds, tiktok, youtube

security = HTTPBasic()


def auth(c: HTTPBasicCredentials = Depends(security)):
    s = get_settings()
    ok = secrets.compare_digest(c.username.encode(), s.admin_user.encode()) and secrets.compare_digest(
        c.password.encode(), s.admin_password.encode())
    if not ok:
        raise HTTPException(401, "Identifiants invalides", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Clip", dependencies=[Depends(auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_oauth_states: dict[str, dict] = {}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@app.on_event("startup")
def _startup():
    db.get_engine()


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
