"""Croissance de la chaîne YouTube : abonnés, heures de visionnage et « on double ce qui marche ».

- `snapshot` : relève abonnés / vues de la chaîne à chaque relevé de stats, historique par jour (courbe, rythme).
- `by_kind` : par format (clips GTA, actus, histoires, compilations) : vues, rétention, abonnés gagnés.
- `mix` : le format qui fait le plus de vues et d'abonnés passe devant dans la file et est produit plus souvent
  (histoires / actus par jour), sans jamais tomber à 0 pour continuer à tester.
- `progress` : où en est la chaîne par rapport à la monétisation (1000 abonnés + 4000 h ou 10 M vues Shorts).
- `subscribe_line` : lien « abonne-toi » (sub_confirmation) ajouté aux descriptions YouTube.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from sqlmodel import col, select

from . import db, platforms
from .config import get_settings
from .models import ClipInfo, Post, PostStat, now

log = logging.getLogger(__name__)

LONG = "youtube_long"  # plateforme des vidéos longues (compilations) : à part des Shorts (quota, apprentissage)
HIST_KEY, CHANNEL_KEY, MIX_KEY = "channel_hist", "yt_channel", "growth_mix"
KINDS = {"clip": "Clips GTA", "news": "Actus", "story": "Histoires", "compil": "Compilations"}
SUB_GOAL, HOURS_GOAL, SHORTS_GOAL = 1000, 4000, 10_000_000
WEAK_BOOST = 0.75  # format nettement sous les autres : ses Shorts ne partent qu'après (sauf essai)


def _load(key: str, default):
    try:
        raw = platforms._get(key)
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default


def kind_of(info: dict) -> str:
    k = info.get("kind")
    if k == "compil":
        return "compil"
    if k == "news":
        return "story" if info.get("story") else "news"
    return "clip"


def kind_of_key(key: str) -> str:
    """Type d'une vidéo d'après sa clé (sans lire ClipInfo) : sert au classement de la file."""
    key = key or ""
    if key.startswith("compil:"):
        return "compil"
    if key.startswith("news:story-"):
        return "story"
    if key.startswith("news:"):
        return "news"
    return "clip"


# ---------- chaîne ----------

def snapshot() -> dict:
    """Relève abonnés, vues et nombre de vidéos de la chaîne ; garde une valeur par jour (heure de Paris)."""
    from .publish import youtube

    info = youtube.channel()
    if not info:
        return {}
    platforms._put(CHANNEL_KEY, json.dumps({"id": info["id"], "title": info.get("title", "")}, ensure_ascii=False))
    hist = history()
    day = datetime.now(ZoneInfo(get_settings().timezone)).date().isoformat()
    hist[day] = {"subs": info["subs"], "views": info["views"], "videos": info["videos"]}
    keep = dict(sorted(hist.items())[-400:])
    platforms._put(HIST_KEY, json.dumps(keep))
    return info


def history() -> dict[str, dict]:
    return _load(HIST_KEY, {})


def channel_id() -> str:
    return (_load(CHANNEL_KEY, {}) or {}).get("id", "")


def subscribe_url() -> str:
    cid = channel_id()
    return f"https://www.youtube.com/channel/{cid}?sub_confirmation=1" if cid else ""


SUB_TEXT = {"clip": "🔔 Abonne-toi pour les meilleurs moments GTA 6 chaque jour",
            "news": "🔔 Abonne-toi pour l'actu en 1 minute chaque jour",
            "story": "🔔 Abonne-toi pour une nouvelle histoire vraie chaque jour",
            "compil": "🔔 Abonne-toi pour ne rien rater"}


def subscribe_line(kind: str = "clip") -> str:
    """Ligne d'abonnement en un clic (fenêtre « S'abonner » ouverte directement) ; "" si chaîne inconnue."""
    if not get_settings().subscribe_link:
        return ""
    url = subscribe_url()
    return f"{SUB_TEXT.get(kind, SUB_TEXT['compil'])} 👉 {url}" if url else ""


# ---------- performances par format ----------

def _latest_stats(post_ids: list[int]) -> dict[int, PostStat]:
    last: dict[int, PostStat] = {}
    if not post_ids:
        return last
    with db.session() as s:
        for st in s.exec(select(PostStat).where(col(PostStat.post_id).in_(post_ids))
                         .order_by(col(PostStat.fetched_at))).all():
            last[st.post_id] = st
    return last


def _posts(days: float, platforms_: tuple[str, ...] = ("youtube", LONG)) -> list[tuple[Post, str]]:
    """[(publication, type)] publiées depuis `days` jours sur ces plateformes."""
    since = now() - timedelta(days=days)
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.status == "published", col(Post.platform).in_(platforms_),
                                          Post.published_at >= since)).all()
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([p.clip_id for p in posts]))).all()}
    out = []
    for p in posts:
        try:
            info = json.loads(infos.get(p.clip_id) or "{}")
        except ValueError:
            info = {}
        out.append((p, kind_of(info)))
    return out


def by_kind(days: float | None = None, min_age_h: float | None = None) -> dict[str, dict]:
    """{type: {n, median, total, ret, subs, subs_1k, minutes}} des vidéos YouTube publiées depuis `days` jours
    et âgées d'au moins `min_age_h` h (comparaison équitable)."""
    cfg = get_settings()
    days = cfg.mix_days if days is None else days
    min_age_h = cfg.review_age_hours if min_age_h is None else min_age_h
    rows = [(p, k) for p, k in _posts(days) if (now() - p.published_at).total_seconds() / 3600 >= min_age_h]
    last = _latest_stats([p.id for p, _ in rows])
    out: dict[str, dict] = {}
    for kind in KINDS:
        sts = [last[p.id] for p, k in rows if k == kind and p.id in last]
        if not sts:
            continue
        views = [st.views for st in sts]
        rets = [st.avg_view_pct for st in sts if st.avg_view_pct is not None]
        subs = [st.subs for st in sts if st.subs is not None]
        total = int(sum(views))
        out[kind] = {"label": KINDS[kind], "n": len(sts), "median": float(np.median(views)), "total": total,
                     "views": views,
                     "ret": float(np.mean(rets)) if rets else None,
                     "subs": int(sum(subs)) if subs else None,
                     "subs_1k": (1000 * sum(subs) / total) if subs and total else None,
                     "minutes": float(sum(st.minutes or 0 for st in sts)),
                     "likes": int(sum(st.likes for st in sts)), "comments": int(sum(st.comments for st in sts)),
                     "shares": int(sum(st.shares for st in sts)),
                     "eng_1k": (1000 * sum(st.likes + st.comments + st.shares for st in sts) / total) if total else None}
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_mix(stats: dict[str, dict] | None = None) -> dict:
    """Combien d'actus / d'histoires produire par jour et quel poids donner à chaque format dans la file,
    d'après les vues médianes (et les abonnés gagnés) de chaque format comparées à la médiane de la chaîne."""
    cfg = get_settings()
    prev = _load(MIX_KEY, None) if stats is None else None
    if stats is None:
        stats = by_kind()
        longer = by_kind(days=max(cfg.mix_days, cfg.stats_days))
        for k, v in longer.items():  # format peu produit (coupé) : on regarde plus loin en arrière
            if stats.get(k, {}).get("n", 0) < cfg.mix_min:
                stats[k] = v
    base = {"news": cfg.news_per_day, "story": cfg.story_per_day}
    caps = {"news": max(cfg.news_per_day, cfg.news_max_per_day), "story": max(cfg.story_per_day,
                                                                             cfg.story_max_per_day)}
    out = {"per_day": dict(base), "boost": {}, "reasons": [], "at": now().isoformat(timespec="minutes")}
    if not cfg.growth_mix:
        return out
    shorts = {k: v for k, v in stats.items() if k != "compil"}
    measured = {k: v for k, v in shorts.items() if v["n"] >= max(1, cfg.mix_min)}
    if len(measured) < 2:
        out["reasons"].append(f"Pas encore assez de Shorts mesurés par format (il en faut {cfg.mix_min} par format) "
                              "pour comparer.")
        _stick(out, base, measured, prev)
        return out
    test = cfg.mix_test_days
    for kind, v in sorted(measured.items()):
        others = {k: o for k, o in measured.items() if k != kind}
        # référence : la médiane de tous les autres Shorts mesurés (chaque Short compte, pas chaque format)
        ref = float(np.median([x for o in others.values() for x in o.get("views") or [o["median"]]]))
        ratio = (v["median"] + 5) / (ref + 5)  # +5 : pas d'emballement sur des petits nombres
        subs_rates = [o["subs_1k"] for o in others.values() if o.get("subs_1k") is not None]
        if subs_rates and v.get("subs_1k") is not None:  # abonnés gagnés : ce qui fait grandir la chaîne
            ratio *= _clamp((v["subs_1k"] + 1) / (float(np.median(subs_rates)) + 1), 0.8, 1.5)
        out["boost"][kind] = round(_clamp(ratio, 0.3, 2.0), 2)
        if kind in base and base[kind] > 0:
            raw = base[kind] * ratio
            n = 0 if raw < 0.5 else int(round(_clamp(raw, 1, caps[kind])))
            out["per_day"][kind] = n
            if n == base[kind]:
                continue
            what = (f"plus qu'un essai tous les {test:g} jours (on coupe ce qui ne marche pas)" if n == 0 else
                    f"{n} par jour au lieu de {base[kind]}" + (" (on double ce qui marche)" if n > base[kind] else ""))
            out["reasons"].append(f"{KINDS[kind]} : {v['median']:.0f} vues en médiane contre {ref:.0f} pour "
                                  f"les autres Shorts → {what}.")
    _stick(out, base, measured, prev)
    best = max(out["boost"], key=out["boost"].get) if out["boost"] else ""
    if best:
        out["reasons"].append(f"Format le plus porteur : {KINDS[best]} (passe devant dans la file d'envoi).")
    return out


def _stick(out: dict, base: dict, measured: dict, prev: dict | None) -> None:
    """Format pas assez mesuré en ce moment (coupé, donc peu produit) : la dernière décision tient, sinon il
    repartirait à son rythme du .env dès que ses anciennes vidéos sortent de la fenêtre (va-et-vient)."""
    if not prev:
        return
    for kind in set(base) - set(measured):
        if kind in (prev.get("per_day") or {}) and kind in (prev.get("boost") or {}):
            out["per_day"][kind] = prev["per_day"][kind]
            out["boost"][kind] = prev["boost"][kind]


def update_mix() -> dict:
    m = compute_mix()
    platforms._put(MIX_KEY, json.dumps(m, ensure_ascii=False))
    for r in m["reasons"]:
        log.info("Croissance : %s", r)
    return m


def mix() -> dict:
    m = _load(MIX_KEY, None)
    return m if (m and get_settings().growth_mix) else compute_mix({})


def per_day(kind: str) -> int:
    """Actus (news) ou histoires (story) à produire aujourd'hui : réglage du .env, ajusté par les stats."""
    cfg = get_settings()
    base = cfg.news_per_day if kind == "news" else cfg.story_per_day
    if not cfg.growth_mix:
        return base
    return int((mix().get("per_day") or {}).get(kind, base))


def testing(boosts: dict[str, float]) -> set[str]:
    """Formats faibles sans Short publié depuis MIX_TEST_DAYS jours : leur prochain Short part normalement (essai),
    sinon il resterait toujours derrière les clips GTA et ne serait jamais mesuré."""
    from .models import Clip, Video

    weak = {k for k, b in boosts.items() if b < WEAK_BOOST}
    if not weak:
        return set()
    since = now() - timedelta(days=get_settings().mix_test_days)
    with db.session() as s:
        keys = s.exec(select(Video.key).join(Clip, Clip.video_id == Video.id).join(Post, Post.clip_id == Clip.id)
                      .where(Post.platform == "youtube", Post.status == "published",
                             Post.published_at >= since)).all()
    return weak - {kind_of_key(k) for k in keys}


def queue_boosts() -> dict[str, float]:
    """Poids de chaque format dans la file (1 pour un format faible dont c'est le tour d'être testé)."""
    if not get_settings().growth_mix:
        return {}
    b = dict(mix().get("boost") or {})
    for k in testing(b):
        b[k] = 1.0
    return b


def boost(kind: str) -> float:
    if not get_settings().growth_mix:
        return 1.0
    return float((mix().get("boost") or {}).get(kind, 1.0))


# ---------- streamers qui marchent ----------

SOURCES_KEY = "source_scores"


def compute_source_scores(perf: dict[int, float] | None = None) -> dict[str, dict]:
    """{source_id: {"name", "perf", "n"}} : performance moyenne (0-1) des clips mesurés de chaque streamer."""
    from . import learning
    from .models import Clip, Source, Video

    perf = learning.performances() if perf is None else perf
    if not perf:
        return {}
    with db.session() as s:
        rows = s.exec(select(Clip.id, Video.source_id).join(Video, Video.id == Clip.video_id)
                      .where(col(Clip.id).in_(list(perf)))).all()
        names = {x.id: x.name for x in s.exec(select(Source)).all()}
    by: dict[int, list[float]] = {}
    for cid, sid in rows:
        if sid:
            by.setdefault(sid, []).append(perf[cid])
    return {str(sid): {"name": names.get(sid, ""), "perf": round(float(np.mean(v)), 3), "n": len(v)}
            for sid, v in by.items()}


def source_tier(source_id: int | None) -> int:
    """2 = streamer dont les clips marchent (enregistré et traité en premier), 0 = ceux qui marchent le moins,
    1 = moyen ou pas encore assez mesuré (on continue à le tester)."""
    if not source_id or not get_settings().source_priority:
        return 1
    sc = _load(SOURCES_KEY, {}).get(str(source_id))
    if not sc or sc.get("n", 0) < 3:
        return 1
    return 2 if sc["perf"] >= 0.6 else 0 if sc["perf"] <= 0.35 else 1


# ---------- monétisation ----------

def progress() -> dict:
    """Avancement vers le Programme Partenaire YouTube (approximatif : YouTube ne compte que les vues « qualifiées »)."""
    hist = history()
    days = sorted(hist)
    subs = hist[days[-1]]["subs"] if days else None
    rate = None
    if len(days) >= 2:
        first = next((d for d in days if d >= (datetime.fromisoformat(days[-1]) - timedelta(days=7)).date()
                      .isoformat()), days[0])
        span = max(1, (datetime.fromisoformat(days[-1]) - datetime.fromisoformat(first)).days)
        rate = (hist[days[-1]]["subs"] - hist[first]["subs"]) / span
    shorts = _posts(90, ("youtube",))
    last = _latest_stats([p.id for p, _ in shorts])
    shorts_views = int(sum(last[p.id].views for p, _ in shorts if p.id in last))
    longs = _posts(365, (LONG,))
    last_l = _latest_stats([p.id for p, _ in longs])
    hours = sum((last_l[p.id].minutes or 0) for p, _ in longs if p.id in last_l) / 60
    eta = None
    if subs is not None and rate and rate > 0 and subs < SUB_GOAL:
        eta = int((SUB_GOAL - subs) / rate)
    return {"subs": subs, "rate": rate, "eta_days": eta, "shorts_views": shorts_views, "hours": hours,
            "sub_goal": SUB_GOAL, "hours_goal": HOURS_GOAL, "shorts_goal": SHORTS_GOAL,
            "sub_pct": min(100.0, 100 * (subs or 0) / SUB_GOAL), "hours_pct": min(100.0, 100 * hours / HOURS_GOAL),
            "shorts_pct": min(100.0, 100 * shorts_views / SHORTS_GOAL), "history": [(d, hist[d]) for d in days[-30:]]}


def _first_short() -> datetime | None:
    with db.session() as s:
        return s.exec(select(Post.published_at).where(Post.platform == "youtube", Post.status == "published",
                                                      Post.published_at != None)  # noqa: E711
                      .order_by(col(Post.published_at))).first()


def _views_per_day(hist: dict[str, dict], age_days: float) -> float | None:
    """Vues par jour : progression du compteur de la chaîne sur 3 à 14 jours, sinon moyenne depuis le 1er Short."""
    days = sorted(hist)
    if len(days) >= 2:
        last = datetime.fromisoformat(days[-1])
        old = [d for d in days if 3 <= (last - datetime.fromisoformat(d)).days <= 14]
        if old:
            span = (last - datetime.fromisoformat(old[0])).days
            return max(0.0, (hist[days[-1]]["views"] - hist[old[0]]["views"]) / span)
    with db.session() as s:
        posts = s.exec(select(Post.id).where(Post.platform == "youtube", Post.status == "published")).all()
    last_st = _latest_stats(list(posts))
    total = sum(st.views for st in last_st.values())
    return total / age_days if total and age_days > 0 else None


def forecast(p: dict | None = None) -> dict:
    """Date estimée d'éligibilité à la monétisation et revenus estimés, au rythme actuel.

    Hypothèses (affichées) : rythme constant ; RPM_SHORTS / RPM_LONG € pour 1000 vues ; YouTube ne compte que les
    vues « qualifiées » et peut refuser une chaîne de contenu réutilisé : c'est un ordre de grandeur, pas une promesse."""
    cfg = get_settings()
    p = progress() if p is None else p
    first = _first_short()
    age = max(1.0, (now() - first).total_seconds() / 86400) if first else 0.0
    subs = p.get("subs")
    sub_rate = p.get("rate")
    if sub_rate is None and subs is not None and age:
        sub_rate = subs / age  # une seule mesure : moyenne depuis le 1er Short
    views_day = _views_per_day(history(), age) if age else None
    since = now() - timedelta(days=28)
    longs = [(x, k) for x, k in _posts(28, (LONG,))]
    last_l = _latest_stats([x.id for x, _ in longs])
    long_days = max(1.0, min(28.0, (now() - min((x.published_at for x, _ in longs), default=since))
                             .total_seconds() / 86400))
    hours_day = sum((last_l[x.id].minutes or 0) for x, _ in longs if x.id in last_l) / 60 / long_days
    long_views_day = sum(last_l[x.id].views for x, _ in longs if x.id in last_l) / long_days

    sub_eta = 0 if subs is not None and subs >= SUB_GOAL else (
        int((SUB_GOAL - subs) / sub_rate) + 1 if subs is not None and sub_rate and sub_rate > 0 else None)
    shorts_need = SHORTS_GOAL / 90
    shorts_ok = bool(views_day and views_day >= shorts_need)
    hours_eta = 0 if p.get("hours", 0) >= HOURS_GOAL else (
        int((HOURS_GOAL - p.get("hours", 0)) / hours_day) + 1 if hours_day > 0 else None)
    path = [x for x in ((90 if shorts_ok else None), hours_eta) if x is not None]
    path_eta = min(path) if path else None
    eta = max(sub_eta, path_eta) if sub_eta is not None and path_eta is not None else None
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    month_now = ((views_day or 0) * 30 / 1000 * cfg.rpm_shorts) + long_views_day * 30 / 1000 * cfg.rpm_long
    month_goal = SHORTS_GOAL / 3 / 1000 * cfg.rpm_shorts  # au seuil : 10 M vues Shorts sur 90 jours
    return {"sub_rate": sub_rate, "sub_eta": sub_eta,
            "sub_date": (today + timedelta(days=sub_eta)).strftime("%d/%m/%Y") if sub_eta is not None else None,
            "views_day": views_day, "views_90": (views_day or 0) * 90, "shorts_need": shorts_need,
            "shorts_factor": (shorts_need / views_day) if views_day else None, "shorts_ok": shorts_ok,
            "hours_week": hours_day * 7, "hours_eta": hours_eta,
            "eta": eta, "date": (today + timedelta(days=eta)).strftime("%d/%m/%Y") if eta is not None else None,
            "month_now": month_now, "month_goal": month_goal, "rpm_shorts": cfg.rpm_shorts, "rpm_long": cfg.rpm_long}


def run() -> dict:
    """Appelé à chaque relevé de stats : chaîne relevée, puis mix des formats recalculé."""
    try:
        snapshot()
    except Exception:
        log.exception("Relevé de la chaîne YouTube échoué")
    try:
        platforms._put(SOURCES_KEY, json.dumps(compute_source_scores(), ensure_ascii=False))
    except Exception:
        log.exception("Classement des streamers échoué")
    return update_mix()
