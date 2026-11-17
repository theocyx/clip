"""Chaîne complète pour une vidéo téléchargée : transcription → moments → rendu des clips."""
from __future__ import annotations

import json
import logging
import random
import threading
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from sqlmodel import select

from .. import db, learning
from ..config import get_settings
from ..models import Clip, Post, Video, now
from . import dedupe, facecam, highlights, hook, llm, media, render, subtitles, transcribe

log = logging.getLogger(__name__)
# Mis à vrai par le worker quand il s'arrête (mise à jour…) : le traitement s'interrompt proprement
# entre deux étapes et reprendra au redémarrage, sans refaire ce qui est déjà fait.
stopping = threading.Event()
Interrupted = transcribe.Interrupted


def stop_if_stopping() -> None:
    if stopping.is_set():
        raise Interrupted()


def set_status(video_id: int, status: str, **fields) -> Video:
    with db.session() as s:
        v = s.get(Video, video_id)
        v.status = status
        v.progress = 0.0
        v.updated_at = now()
        for k, val in fields.items():
            setattr(v, k, val)
        s.add(v)
        s.commit()
        return v


def set_progress(video_id: int, pct: float) -> None:
    """Enregistre l'avancement (0-100) de l'étape en cours, sans changer le statut."""
    with db.session() as s:
        v = s.get(Video, video_id)
        if v is None:
            return
        v.progress = round(max(0.0, min(100.0, pct)), 1)
        s.add(v)
        s.commit()


class Progress:
    """Rappel d'avancement qui n'écrit en base qu'au plus toutes les `every` secondes (ou à 100 %)."""

    def __init__(self, video_id: int, every: float = 3.0):
        self.video_id, self.every = video_id, every
        self.last_t, self.last_pct = -1e9, -1.0

    def __call__(self, pct: float) -> None:
        t = time.monotonic()
        if pct >= 100 or (t - self.last_t >= self.every and abs(pct - self.last_pct) >= 0.5):
            self.last_t, self.last_pct = t, pct
            try:
                set_progress(self.video_id, pct)
            except Exception as e:  # l'affichage de l'avancement ne doit jamais bloquer le traitement
                log.debug("Avancement non enregistré : %s", e)


def stop_if_skipped(video_id: int) -> None:
    """Arrête le traitement si la vidéo a été ignorée entre-temps."""
    from .sources import Cancelled
    with db.session() as s:
        v = s.get(Video, video_id)
        if v is None or v.status == "skipped":
            raise Cancelled()


def video_dir(video_id: int) -> Path:
    return get_settings().videos_dir / str(video_id)


def clip_count(duration: float) -> int:
    s = get_settings()
    return max(1, min(s.clips_max_per_video, round(s.clips_per_video * duration / 3600)))


def fallback_title(text: str) -> str:
    text = text.strip()
    return (text[:67] + "…") if len(text) > 70 else text


def render_hook_png(text: str, out: Path) -> str:
    """Accroche avec emojis en image ; "" si impossible (repli sur l'accroche ASS sans emoji)."""
    png = out.parent / f"{out.stem}_hook.png"
    try:
        return str(png.resolve()) if hook.render_png(text, get_settings().font_name, png) else ""
    except Exception as e:
        log.warning("Accroche en image impossible (%s), repli sans emoji.", e)
        return ""


def pick_layout(src: Path, start: float, end: float):
    """Zone de la facecam pour le format split, ou None pour le format classique."""
    s = get_settings()
    mode = (s.layout or "auto").lower()
    if mode in ("classic", "classique", "blur"):
        return None
    aspect = 1080 / s.split_cam_height
    cam = facecam.detect(src, start, end, aspect)
    if cam is None and mode == "split":
        log.info("Pas de facecam trouvée : format classique.")
    return cam


def check_duplicate(video: Video, m) -> bool:
    """Vrai si la scène a déjà un meilleur clip (autre streamer) : on ne crée pas celui-ci.

    Si ce clip est meilleur et que l'autre n'est pas encore publié, l'autre est retiré de la file."""
    text = m.features.get("dedupe_text", "")
    dups = dedupe.find(video.id, text, m.features.get("abs_start"), m.features.get("abs_end"))
    if not dups:
        return False
    better = next((d for d in dups if d.posted or d.score >= m.score), None)
    if better:
        log.info("Doublon ignoré (%.0f %% en commun avec la vidéo %d) : %s", better.similarity * 100,
                 better.video_id, m.title)
        return True
    for d in dups:
        n = dedupe.replace(d.group, m.title)
        log.info("Doublon : « %s » remplace un clip moins bon de la vidéo %d (%d envoi(s) annulé(s))",
                 m.title, d.video_id, n)
    return False


def process(video_id: int) -> int:
    """Traite une vidéo dont le fichier est présent. Renvoie le nombre de clips créés."""
    s = get_settings()
    with db.session() as ss:
        video = ss.get(Video, video_id)
    src = Path(video.path)
    work = video_dir(video_id)
    dur = media.duration(src)

    set_status(video_id, "transcribing", duration=dur)
    wav = work / "audio.wav"
    if not wav.exists():
        media.extract_audio(src, wav)
    segments = transcribe.transcribe(wav, work / "transcript.json", dur, Progress(video_id), stopping.is_set)

    stop_if_skipped(video_id)
    stop_if_stopping()
    saved = work / "analysis.json"
    work.mkdir(parents=True, exist_ok=True)
    if saved.exists():  # analyse déjà faite avant un redémarrage : on ne la refait pas
        moments = [highlights.Moment(**d) for d in json.loads(saved.read_text())]
        log.info("Analyse reprise depuis la sauvegarde (%d moment(s))", len(moments))
    else:
        moments = analyze(video, wav, segments, dur)
        tmp = saved.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(m) for m in moments], ensure_ascii=False, default=float))
        tmp.replace(saved)
    return render_moments(video, src, segments, moments)


def analyze(video: Video, wav: Path, segments: list[dict], dur: float) -> list:
    """Choisit et note les moments à découper."""
    s = get_settings()
    video_id = video.id
    set_status(video_id, "analyzing")
    progress = Progress(video_id)
    loud = media.loudness_per_second(wav)
    progress(10)
    n = clip_count(dur)
    which = llm.provider()
    use_llm = which != "none"
    # réglages appris des clips déjà publiés, avec une petite variation pour continuer à tester
    tun = learning.current()
    target = tun["clip_target"] * random.uniform(1 - s.explore, 1 + s.explore)
    target = min(max(target, s.clip_min), s.clip_max)
    guidance = learning.guidance(tun) if use_llm else ""
    moments = highlights.find_moments(
        loud, segments, count=n, target=target, min_len=s.clip_min, max_len=s.clip_max,
        pool_factor=3 if use_llm else 1, weights=tun["weights"],
    )
    if moments:
        hmax = max(m.score for m in moments)
        hmin = min(m.score for m in moments)
    progress(20)
    for k, m in enumerate(moments):
        stop_if_stopping()
        progress(20 + 80 * k / len(moments))
        text = highlights.text_between(segments, m.start, m.end)
        heur = (m.score - hmin) / (hmax - hmin) if hmax > hmin else 0.5
        rated = llm.rate(text, video.title, which, guidance) if use_llm else None
        if rated:
            mix = tun["llm_mix"]
            m.score = (1 - mix) * heur + mix * rated[0] / 10
            m.title, m.hook = rated[1], rated[2]
        else:
            m.score = heur
        m.title = m.title or fallback_title(text) or video.title
        m.features.update(heur=round(heur, 3), llm=rated[0] if rated else None, target=round(target, 1),
                          tuning_id=tun.get("tuning_id"), text=text[:400],
                          dedupe_text=text[:dedupe.TEXT_CHARS],
                          abs_start=dedupe.abs_time(video.created_at, video.is_live, m.start),
                          abs_end=dedupe.abs_time(video.created_at, video.is_live, m.end))
    moments = sorted(moments, key=lambda m: -m.score)[:n]
    moments = [m for m in moments if m.score >= s.min_score]
    for m in moments:
        m.features["group"] = uuid.uuid4().hex[:10]
    return moments


def render_moments(video: Video, src: Path, segments: list[dict], moments: list) -> int:
    s = get_settings()
    video_id = video.id
    stop_if_skipped(video_id)
    set_status(video_id, "rendering")
    progress = Progress(video_id, every=0)
    created = 0
    todo = sorted(moments, key=lambda m: m.start)
    for k, m in enumerate(todo):
        stop_if_stopping()
        progress(100 * k / len(todo))
        group = m.features.get("group") or uuid.uuid4().hex[:10]
        with db.session() as ss:  # parties déjà rendues avant un redémarrage
            done_parts = {c.part for c in ss.exec(select(Clip).where(Clip.video_id == video_id,
                                                                     Clip.group == group)).all()}
        if not done_parts and check_duplicate(video, m):
            continue
        parts = highlights.split_parts(m.start, m.end, segments, s.part_max)
        cam = pick_layout(src, m.start, m.end)
        m.features["layout"] = "split" if cam else "classic"
        top = render.split_top(s.split_cam_height) if cam else 0
        for i, (a, b) in enumerate(parts, 1):
            if i in done_parts:
                created += 1
                continue
            stop_if_stopping()
            label = f"Partie {i}/{len(parts)}" if len(parts) > 1 else ""
            out = s.clips_dir / str(video_id) / f"{group}_p{i}.mp4"
            try:
                hook_text = (m.hook or m.title) if s.hook_title else ""
                png = ""
                if hook_text and s.hook_emojis:
                    hook_text = hook.add_emojis(hook_text, m.title)
                    png = render_hook_png(hook_text, out)
                if not png:
                    hook_text = hook.strip_emojis(hook_text)
                ass = subtitles.build_ass(segments, a, b, s.font_name, label, hook_text, s.hook_seconds,
                                          hook_in_ass=not png, top=top)
                render.render(src, a, b, ass, out, png, s.hook_seconds, cam)
            except Exception as e:
                log.exception("Rendu échoué")
                status, err = "failed", str(e)
            else:
                status, err = "ready", ""
            title = m.title + (f" ({label.lower()})" if label else "")
            with db.session() as ss:
                clip = Clip(video_id=video_id, group=group, part=i, part_count=len(parts), start=a, end=b,
                            score=m.score, title=title, path=str(out), status=status,
                            description=err)
                ss.add(clip)
                ss.commit()
                learning.save_clip_info(clip.id, {**m.features, "title": m.title, "hook": m.hook,
                                                  "part": i, "part_count": len(parts)})
                if status == "ready":
                    created += 1
                    if s.auto_publish:
                        queue_posts(ss, clip)
            progress(100 * (k + i / len(parts)) / len(todo))
    stop_if_skipped(video_id)
    set_status(video_id, "done", error="")
    cleanup(video_id)
    return created


def queue_posts(ss, clip: Clip) -> None:
    s = get_settings()
    for platform, enabled in (("youtube", s.publish_youtube), ("tiktok", s.publish_tiktok)):
        exists = ss.exec(select(Post).where(Post.clip_id == clip.id, Post.platform == platform)).first()
        if enabled and not exists:
            ss.add(Post(clip_id=clip.id, platform=platform))
    ss.commit()


def cleanup(video_id: int) -> None:
    work = video_dir(video_id)
    (work / "audio.wav").unlink(missing_ok=True)
    (work / "analysis.json").unlink(missing_ok=True)
    if not get_settings().keep_source_videos:
        for p in work.glob("source.*"):
            p.unlink(missing_ok=True)


def delete_video_files(video_id: int) -> None:
    shutil.rmtree(video_dir(video_id), ignore_errors=True)
