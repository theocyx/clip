"""Chaîne complète pour une vidéo téléchargée : transcription → moments → rendu des clips."""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from sqlmodel import select

from .. import db
from ..config import get_settings
from ..models import Clip, Post, Video, now
from . import highlights, llm, media, render, subtitles, transcribe

log = logging.getLogger(__name__)


def set_status(video_id: int, status: str, **fields) -> Video:
    with db.session() as s:
        v = s.get(Video, video_id)
        v.status = status
        v.updated_at = now()
        for k, val in fields.items():
            setattr(v, k, val)
        s.add(v)
        s.commit()
        return v


def video_dir(video_id: int) -> Path:
    return get_settings().videos_dir / str(video_id)


def clip_count(duration: float) -> int:
    s = get_settings()
    return max(1, min(s.clips_max_per_video, round(s.clips_per_video * duration / 3600)))


def fallback_title(text: str) -> str:
    text = text.strip()
    return (text[:67] + "…") if len(text) > 70 else text


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
    segments = transcribe.transcribe(wav, work / "transcript.json")

    set_status(video_id, "analyzing")
    loud = media.loudness_per_second(wav)
    n = clip_count(dur)
    which = llm.provider()
    use_llm = which != "none"
    moments = highlights.find_moments(
        loud, segments, count=n, target=s.clip_target, min_len=s.clip_min, max_len=s.clip_max,
        pool_factor=3 if use_llm else 1,
    )
    if moments:
        hmax = max(m.score for m in moments)
        hmin = min(m.score for m in moments)
    for m in moments:
        text = highlights.text_between(segments, m.start, m.end)
        heur = (m.score - hmin) / (hmax - hmin) if hmax > hmin else 0.5
        rated = llm.rate(text, video.title, which) if use_llm else None
        if rated:
            m.score = 0.4 * heur + 0.6 * rated[0] / 10
            m.title = rated[1]
        else:
            m.score = heur
        m.title = m.title or fallback_title(text) or video.title
    moments = sorted(moments, key=lambda m: -m.score)[:n]
    moments = [m for m in moments if m.score >= s.min_score]

    set_status(video_id, "rendering")
    created = 0
    for m in sorted(moments, key=lambda m: m.start):
        parts = highlights.split_parts(m.start, m.end, segments, s.part_max)
        group = uuid.uuid4().hex[:10]
        for i, (a, b) in enumerate(parts, 1):
            label = f"Partie {i}/{len(parts)}" if len(parts) > 1 else ""
            out = s.clips_dir / str(video_id) / f"{group}_p{i}.mp4"
            try:
                ass = subtitles.build_ass(segments, a, b, s.font_name, label)
                render.render(src, a, b, ass, out)
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
                if status == "ready":
                    created += 1
                    if s.auto_publish:
                        queue_posts(ss, clip)
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
    if not get_settings().keep_source_videos:
        for p in work.glob("source.*"):
            p.unlink(missing_ok=True)


def delete_video_files(video_id: int) -> None:
    shutil.rmtree(video_dir(video_id), ignore_errors=True)
