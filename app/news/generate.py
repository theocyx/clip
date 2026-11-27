"""Chef d'orchestre des vidéos d'actu : choix du sujet, script, voix, illustrations, montage,
puis rangement dans Clips (et dans la file d'envoi) comme un clip normal."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from .. import db, platforms
from ..config import get_settings
from ..models import Clip, Post, Video, now
from ..pipeline import engage, hook as hooks
from . import feeds, montage, script, tts, visuals

log = logging.getLogger(__name__)
PREFIX = "news:"
ON_KEY, NOW_KEY = "news_on", "news_now"
STORY_ON_KEY, STORY_NOW_KEY = "story_on", "story_now"
STORY_PREFIX = "news:story-"
_busy = threading.Lock()


# ---------- réglages de l'interface ----------

def enabled() -> bool:
    v = platforms._get(ON_KEY)
    return get_settings().news if v is None else v == "1"


def set_enabled(on: bool) -> None:
    platforms._put(ON_KEY, "1" if on else "0")


def request_now() -> None:
    """Bouton « Générer maintenant » : le worker s'en charge à son prochain passage (≤ 30 s)."""
    platforms._put(NOW_KEY, "1")


def take_request(key: str = NOW_KEY) -> bool:
    if platforms._get(key) == "1":
        platforms._put(key, None)
        return True
    return False


def stories_enabled() -> bool:
    v = platforms._get(STORY_ON_KEY)
    return get_settings().stories if v is None else v == "1"


def set_stories(on: bool) -> None:
    platforms._put(STORY_ON_KEY, "1" if on else "0")


def request_story() -> None:
    """Bouton « Histoire maintenant »."""
    platforms._put(STORY_NOW_KEY, "1")


def is_story(video: Video | None) -> bool:
    return bool(video and (video.key or "").startswith(STORY_PREFIX))


def is_news(video: Video | None) -> bool:
    return bool(video and (video.key or "").startswith(PREFIX))


# ---------- planning ----------

def _local_now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def day_start_utc() -> datetime:
    local = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def made_today(kind: str = "news") -> int:
    """Actus (kind=news) ou histoires (kind=story) terminées aujourd'hui (heure de Paris)."""
    with db.session() as s:
        keys = s.exec(select(Video.key).where(col(Video.key).startswith(PREFIX), Video.status == "done",
                                              Video.created_at >= day_start_utc())).all()
    return sum(1 for k in keys if (k or "").startswith(STORY_PREFIX) == (kind == "story"))


def in_hours(at: datetime | None = None) -> bool:
    from ..worker import _in_ranges, parse_hours

    ranges = parse_hours(get_settings().news_hours)
    return not ranges or _in_ranges(ranges, at or _local_now())


def made_since(hours: float, kind: str = "news") -> int:
    with db.session() as s:
        keys = s.exec(select(Video.key).where(col(Video.key).startswith(PREFIX), Video.status == "done",
                                              Video.created_at >= now() - timedelta(hours=hours))).all()
    return sum(1 for k in keys if (k or "").startswith(STORY_PREFIX) == (kind == "story"))


def quota(kind: str = "news") -> int:
    """Vidéos à faire aujourd'hui : NEWS_PER_DAY / STORY_PER_DAY, ajusté par les stats (growth.mix). Un format
    qui ne marche pas tombe à 0, avec un essai tous les MIX_TEST_DAYS jours pour voir si ça change."""
    from .. import growth

    s = get_settings()
    if (s.news_per_day if kind == "news" else s.story_per_day) <= 0:
        return 0
    n = growth.per_day(kind)
    if n > 0:
        return n
    return 1 if made_since(24 * max(1.0, get_settings().mix_test_days), kind) == 0 else 0


def due() -> bool:
    return enabled() and in_hours() and made_today() < quota("news")


def story_due() -> bool:
    return stories_enabled() and in_hours() and made_today("story") < quota("story")


# ---------- choix du sujet ----------

def recent_titles(days: float = 3) -> list[str]:
    with db.session() as s:
        rows = s.exec(select(Video.title).where(col(Video.key).startswith(PREFIX),
                                                Video.created_at >= now() - timedelta(days=days))).all()
    return [t for t in rows if t]


def known_keys(keys: list[str]) -> set[str]:
    if not keys:
        return set()
    with db.session() as s:
        return set(s.exec(select(Video.key).where(col(Video.key).in_(keys))).all())


def fresh_candidates() -> list[feeds.Item]:
    s = get_settings()
    items = feeds.candidates(feeds.split_list(s.news_feeds), feeds.split_list(s.news_keywords),
                             feeds.split_list(s.news_exclude), s.news_max_age_hours)
    seen = known_keys([i.key for i in items])
    recent = recent_titles()
    return [i for i in items if i.key not in seen
            and not any(engage.similarity(i.title, t) >= 0.55 for t in recent)]


# ---------- création ----------

def work_dir(video_id: int) -> Path:
    return get_settings().videos_dir / str(video_id)


def _set(video_id: int, **fields) -> None:
    with db.session() as s:
        v = s.get(Video, video_id)
        for k, val in fields.items():
            setattr(v, k, val)
        v.updated_at = now()
        s.add(v)
        s.commit()


def _new_video(item: feeds.Item) -> Video:
    with db.session() as s:
        v = Video(key=item.key, url=item.link, title=item.title[:300], status="generating")
        s.add(v)
        s.commit()
        s.refresh(v)
    d = work_dir(v.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "item.json").write_text(json.dumps({"title": item.title, "link": item.link, "summary": item.summary,
                                             "source": item.source, "related": item.related, "kind": item.kind,
                                             "topic": item.topic}, ensure_ascii=False))
    return v


def clip_score(llm_score: float) -> float:
    """Place dans la file d'envoi : un sujet noté 7/10 se range au niveau des bons clips de la semaine."""
    from .. import curate

    bar = curate.score_bar()
    return round((bar if bar else 1.0) * llm_score / 7, 4)


_GRAVE = re.compile(r"\b(mort|morts|décès|décédé|tué|tués|victimes?|meurtre|attentat|guerre|accident|drame|"
                    r"blessés?|viol|agression|incendie|séisme|otages?)\b", re.I)


_YEAR = re.compile(r"\b(1[5-9]\d\d|20[0-2]\d)\b")


def header_text(hook: str, title: str, text: str, emojis: bool = True) -> str:
    """Titre affiché en haut : avec un emoji, sauf sur un sujet grave (pas de 💀 ni de 😂 sur un drame)."""
    base = hook or title
    if not emojis or _GRAVE.search(f"{title} {text}"):
        return hooks.strip_emojis(base)
    return hooks.add_emojis(base, text)


def map_shot(place: str, n_frames: int = 120):
    """Survol « vu d'en haut » du lieu de l'actu, si on le trouve et que les tuiles satellite répondent."""
    from . import geo, motion

    s = get_settings()
    if not (s.news_map and s.news_motion and place):
        return None
    found = geo.geocode(place)
    if not found:
        return None
    zoom = geo.zoom_for(found.kind)
    tiles = geo.Tiles()
    need = motion.map_tiles_needed(found.lon, found.lat, n_frames, zoom)
    missing = tiles.prefetch(need)
    if missing > len(need) * 0.2:
        log.info("Actu : survol de « %s » abandonné (%d/%d tuiles satellite manquantes)", found.name, missing,
                 len(need))
        return None
    return visuals.Shot(None, "map", credit=geo.CREDIT, text=found.name,
                        meta={"lon": found.lon, "lat": found.lat, "zoom": zoom, "fetch": tiles.get,
                              "sub": found.sub, "rings": found.rings})


MAX_MAPS = 3


def pick_shots(sc, cuts, work: Path, kind: str = "news") -> tuple[list, list]:
    """Une illustration par plan : survol du lieu en ouverture (et des autres lieux cités, 3 cartes max),
    chiffres clés animés (2 max), quiz « Et toi ? » sur la question finale, sinon vidéo / photo libre de
    droits dans un panneau, sinon mots forts animés."""
    s = get_settings()
    used: set[str] = set()
    shots, counters, maps, seen_sentence, seen_places = [], 0, 0, set(), set()
    story, years, seen_years = kind == "story", 0, set()
    places = getattr(sc, "places", []) or []
    last = cuts[-1][0] if cuts else -1
    for j, (i, a, b) in enumerate(cuts):
        shot = None
        first_of_sentence = i not in seen_sentence
        seen_sentence.add(i)
        place = sc.place if j == 0 else (places[i] if first_of_sentence and i < len(places) else "")
        if place and maps < MAX_MAPS and place.lower() not in seen_places:
            seen_places.add(place.lower())
            try:
                shot = map_shot(place, max(30, round((b - a) * montage.FPS)))
            except Exception as e:
                log.info("Actu : survol impossible : %s", e)
            maps += shot is not None
        if shot is None and s.news_motion and i == last and j == len(cuts) - 1 and len(cuts) > 2 \
                and sc.sentences[i].rstrip().endswith("?"):
            shot = visuals.Shot(None, "quiz", text=sc.sentences[i], meta={"answers": getattr(sc, "answers", [])})
        # histoire : la date d'un moment clé (« En 1959, … ») remonte le temps à l'écran
        if shot is None and story and s.news_motion and years < 2 and first_of_sentence and j > 0:
            m = _YEAR.search(sc.sentences[i])
            if m and m.group(1) not in seen_years:
                seen_years.add(m.group(1))
                shot = visuals.Shot(None, "counter", text=m.group(1), meta={"unit": "", "year": True})
                years += 1
        # pas de compteur qui défile sur un bilan de victimes
        if shot is None and s.news_motion and counters < 2 and first_of_sentence and j > 0 \
                and not _GRAVE.search(sc.sentences[i]):
            shot = visuals.number_shot(sc.sentences[i])
            counters += shot is not None
        if shot is None:
            query = sc.visuals[i] if i < len(sc.visuals) else ""
            # histoire : 2 plans sur 3 en image IA (les banques d'images ont rarement la scène racontée)
            shot = visuals.find(query, sc.sentences[i], work / "media", used, j, prefer_video=j % 2 == 0,
                                ai_first=story and j % 3 != 0, kind=kind)
            if shot.kind in ("image", "video") and not (shot.meta or {}).get("label"):
                shot.meta = {**(shot.meta or {}), "label": "ILLUSTRATION"}
        shots.append(shot)
    return shots, []


def look(sc, item: feeds.Item) -> dict:
    """Ce qu'affiche le bandeau : marque, sujet, chapitres, mots-clés, et les médias qui en parlent."""
    s = get_settings()
    sources = [item.source.upper()] if item.source else []
    for title in item.related[:3]:
        sources.append(title[:70])
    if item.kind == "story":
        sources = [f"{item.source.upper()} : {item.title}"] if item.source else []
    return {"brand": s.news_brand, "topic": sc.topic or (sc.place.split(",")[0] if sc.place else ""),
            "chapters": sc.chapters, "titles": sc.titles, "sources": "   ·   ".join(sources),
            "story": item.kind == "story"}


def make(video_id: int, item: feeds.Item) -> int | None:
    """Fabrique la vidéo d'un sujet. Renvoie l'id du clip, ou None si le sujet est écarté."""
    s = get_settings()
    work = work_dir(video_id)
    story = item.kind == "story"
    what = "Histoire" if story else "Actu"
    log.info("%s : sujet « %s » (%s), lecture de l'article…", what, item.title, item.source)
    facts = feeds.fetch_article(item)
    _set(video_id, progress=5)
    log.info("%s : écriture du script par l'IA (peut prendre plusieurs minutes)…", what)
    sc = script.write(item.title, facts, item.source, kind=item.kind)
    if sc is None:
        _set(video_id, status="skipped", error="Script refusé ou IA indisponible", progress=0)
        return None
    if not story and sc.score < s.news_min_score:  # une histoire est choisie dans une liste : pas de note
        _set(video_id, status="skipped", progress=0,
             error=f"Sujet jugé peu intéressant ({sc.score:.0f}/10, minimum {s.news_min_score:g})")
        return None
    (work / "script.json").write_text(json.dumps(sc.__dict__, ensure_ascii=False))
    _set(video_id, progress=15)
    log.info("%s : script prêt (%d phrases, note %g/10), voix off et images…", what, len(sc.sentences), sc.score)
    from .. import experiments

    exp = experiments.choose(item.kind if story else "news")  # voix et débit (amélioration continue)
    base = (s.story_voice_speed if story else s.news_voice_speed) * experiments.pace(exp)
    voice = exp.get("voice")
    engine: dict = {}

    def say(speed: float, gap: float | None = None):
        return tts.speak(sc.sentences, work / "voice.wav", speed=speed, kind=item.kind,
                         gap=gap if gap is not None else (0.34 if story else None), voice=voice,
                         chapters=sc.chapters, info=engine)

    speed = base
    spoken = say(speed)
    floor = script.min_seconds(item.kind)
    for _ in range(2):  # voix plus rapide que prévu : on rallonge le script d'après la durée réellement lue
        if spoken[-1].end >= floor:
            break
        rate = len(sc.text.split()) / max(1.0, spoken[-1].end)
        goal = rate * script.target_seconds(item.kind)
        log.info("%s : voix off de %.0f s (minimum %.0f s), on rallonge le script…", what, spoken[-1].end, floor)
        script.grow(sc, item.title, facts + " " + item.title, item.source, item.kind, goal_words=goal, tries=2)
        spoken = say(speed)
    if floor * 0.85 <= spoken[-1].end < floor:
        # presque assez long (55-59 s pour 60) : voix un peu plus posée plutôt que d'écarter le sujet
        slower = max(0.9, base * spoken[-1].end / floor * 0.97)
        log.info("%s : voix off de %.0f s, débit ralenti à %.2f pour atteindre %.0f s", what, spoken[-1].end, slower,
                 floor)
        spoken = say(slower, 0.34 if story else 0.26)
    if spoken[-1].end < floor:
        _set(video_id, status="skipped", progress=0,
             error=f"Trop court ({spoken[-1].end:.0f} s, minimum {floor:.0f} s) : pas assez de faits dans l'article")
        log.info("%s écartée : %.0f s de voix off seulement (minimum %.0f s)", what, spoken[-1].end, floor)
        return None
    (work / "script.json").write_text(json.dumps(sc.__dict__, ensure_ascii=False))
    total = round(spoken[-1].end + montage.TAIL, 3)
    cuts = montage.plan(spoken, total)
    shots, credits = pick_shots(sc, cuts, work, kind=item.kind)
    for shot in shots:
        if shot.credit and shot.credit not in credits:
            credits.append(shot.credit)
    _set(video_id, progress=35)
    log.info("%s : montage de %d plans…", what, len(shots))
    out = s.clips_dir / "news" / f"{video_id}.mp4"
    hook_text = header_text(sc.hook, sc.title, sc.text, s.hook_emojis)
    montage.build(shots, spoken, work / "voice.wav", hook_text, item.source, out, work,
                  on_progress=lambda p: _set(video_id, progress=35 + p * 0.6), look=look(sc, item))
    info = {"kind": "news", "story": story, "text": sc.text, "title": sc.title, "hook": hook_text, "question": sc.question,
            "llm": sc.score, "polished": True, "hashtags": sc.hashtags, "source": item.source,
            "source_url": item.link, "credits": credits, "duration": total, "shots": len(shots),
            "layout": "news", "voice": engine.get("engine", ""),
            # versions réellement utilisées (voix Edge tombée en panne → Piper : l'essai de voix ne compte pas)
            "exp": {k: v for k, v in exp.items() if k != "voice" or engine.get("engine") == v}}
    with db.session() as ss:
        clip = Clip(video_id=video_id, group=uuid.uuid4().hex[:10], start=0, end=total,
                    score=clip_score(sc.score), title=sc.title, path=str(out), status="ready")
        ss.add(clip)
        ss.commit()
        ss.refresh(clip)
        from .. import learning
        learning.save_clip_info(clip.id, info)
        if s.auto_publish and s.news_publish:
            from ..pipeline.process import queue_posts
            queue_posts(ss, clip)
    _set(video_id, status="done", error="", progress=100, duration=total, path="")
    shutil.rmtree(work / "media", ignore_errors=True)
    for p in work.glob("shot*.mp4"):
        p.unlink(missing_ok=True)
    log.info("%s créée : %s (%d s, %d plans)", "Histoire" if story else "Vidéo d'actu", sc.title, total,
             len(shots))
    return clip.id


def run_once(force: bool = False, story: bool = False) -> int | None:
    """Une vidéo si c'est le moment (ou si force) : l'histoire du jour d'abord, sinon une actu.
    story=True : une histoire tout de suite (bouton). Renvoie l'id du clip créé, ou None."""
    if not _busy.acquire(blocking=False):
        return None
    try:
        retry = _retry_queue()
        if retry:
            return _run(*retry)
        want_story = story or (not force and story_due())
        if not (force or want_story or due()):
            return None
        from ..pipeline import llm
        if llm.provider() != "ollama":  # sans IA pas de script : on ne « grille » aucun sujet
            log.info("Actu : IA locale indisponible, sujet cherché au prochain tour.")
            return None
        if want_story:
            from . import stories
            for item in stories.candidates(2):
                v = _new_video(item)
                cid = _run(v, item)
                if cid:
                    return cid
            log.info("Histoire : aucune histoire disponible (liste épuisée ou Wikipédia injoignable).")
            if story or not (force or due()):
                return None
        for item in fresh_candidates()[:4]:
            v = _new_video(item)
            cid = _run(v, item)
            if cid:
                return cid
        log.info("Actu : aucun sujet retenu ce tour-ci.")
        return None
    finally:
        _busy.release()


def _run(video: Video, item: feeds.Item) -> int | None:
    try:
        return make(video.id, item)
    except Exception as e:
        log.exception("Vidéo d'actu échouée : %s", item.title)
        _set(video.id, status="failed", error=str(e)[:1000], progress=0)
        from .. import notify
        notify.error(f"Vidéo d'actu échouée : {item.title}", str(e))
        return None


def _retry_queue() -> tuple[Video, feeds.Item] | None:
    """Vidéo d'actu relancée à la main (bouton « Relancer ») : refaite à partir du même article."""
    with db.session() as s:
        v = s.exec(select(Video).where(col(Video.key).startswith(PREFIX), Video.status == "news_retry")).first()
    if not v:
        return None
    try:
        data = json.loads((work_dir(v.id) / "item.json").read_text())
    except (OSError, ValueError):
        data = {"title": v.title, "link": v.url}
    item = feeds.Item(title=data.get("title") or v.title, link=data.get("link") or v.url,
                      summary=data.get("summary", ""), source=data.get("source", ""), related=data.get("related", []),
                      kind=data.get("kind", "news"), topic=data.get("topic", ""))
    _set(v.id, status="generating", error="")
    return v, item


def reset_interrupted() -> None:
    """Vidéo d'actu coupée par un redémarrage : relancée depuis le même article."""
    with db.session() as s:
        for v in s.exec(select(Video).where(col(Video.key).startswith(PREFIX), Video.status == "generating")).all():
            v.status, v.error = "news_retry", ""
            s.add(v)
        s.commit()


def loop(stop: threading.Event) -> None:
    s = get_settings()
    stop.wait(90)  # laisse le reste démarrer
    last = 0.0
    import time
    while not stop.is_set():
        try:
            forced, story = take_request(), take_request(STORY_NOW_KEY)
            if story:
                run_once(story=True)
            if forced or time.monotonic() - last >= max(5, s.news_interval_minutes) * 60:
                last = time.monotonic()
                run_once(force=forced)
            elif _retry_pending():
                run_once()
        except Exception:
            log.exception("Boucle des vidéos d'actu en erreur")
        stop.wait(30)


def _retry_pending() -> bool:
    with db.session() as s:
        return s.exec(select(Video.id).where(col(Video.key).startswith(PREFIX),
                                             Video.status == "news_retry")).first() is not None


# ---------- textes de publication ----------

def description(clip: Clip, info: dict, hashtags_default: list[str], question_line: str) -> tuple[str, list[str]]:
    tags = list(dict.fromkeys([t for t in (info.get("hashtags") or []) if t] + hashtags_default))[:8]
    lines = [clip.title, ""]
    if question_line:
        lines += [question_line, ""]
    from .. import growth, interact
    kind = "story" if info.get("story") else "news"
    extra = [x for x in (interact.share_line(kind), growth.subscribe_line(kind)) if x]
    if extra:
        lines += extra + [""]
    if info.get("source"):
        lines += [f"📰 Source : {info['source']}", info.get("source_url", ""), ""]
    for c in info.get("credits") or []:
        lines.append(f"🖼️ {c}")
    if info.get("credits"):
        lines.append("")
    lines += ["Voix et montage générés automatiquement.", "", " ".join("#" + t for t in tags)]
    return "\n".join(lines), tags


def caption(clip: Clip, info: dict, max_tags: int) -> str:
    title = clip.title.strip()
    if not hooks.emojis_in(title) and hooks.emojis_in(info.get("hook", "")):
        emo = hooks.emojis_in(info.get("hook", ""))[:1]
        title = f"{title} {emo[0]}" if emo else hooks.add_emojis(title)
    tags = [t for t in (info.get("hashtags") or []) if t][:max(1, max_tags + 1)]
    cap = title + "".join(f" #{t}" for t in tags)
    if info.get("source"):
        cap += f"\n\nSource : {info['source']}"
    return cap


def status() -> dict:
    s = get_settings()
    with db.session() as ss:
        last = ss.exec(select(Video).where(col(Video.key).startswith(PREFIX))
                       .order_by(col(Video.created_at).desc()).limit(6)).all()
        pending = ss.exec(select(Post.id).join(Clip, Clip.id == Post.clip_id).join(Video, Video.id == Clip.video_id)
                          .where(col(Video.key).startswith(PREFIX), Post.status == "pending")).all()
    from . import stories
    return {"on": enabled(), "today": made_today(), "per_day": quota("news"), "topic": s.news_topic,
            "stories_on": stories_enabled(), "stories_today": made_today("story"), "story_per_day": quota("story"),
            "stories_left": stories.left(), "ai_images": s.news_ai_images,
            "hours": s.news_hours, "pexels": bool(s.pexels_api_key), "publish": s.news_publish and s.auto_publish,
            "last": last, "pending": len(pending),
            "music": len([p for p in (s.data_dir / "music").glob("*") if p.is_file()])
            if (s.data_dir / "music").exists() else 0}
