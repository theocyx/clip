"""Apprentissage 100 % local : relève les stats des clips publiés et ajuste le montage d'après ce qui marche.

1. `fetch_stats` : vues, likes, commentaires, partages (+ rétention YouTube) de chaque publication récente.
2. `performances` : note chaque clip de 0 à 1 en le comparant aux autres clips de la même plateforme,
   tous mesurés au même âge (72 h par défaut) pour que ce soit équitable.
3. `learn` : regarde quelles caractéristiques ont les clips qui marchent (sonie, parole, excitation,
   durée, note de l'IA, accroche…) et déplace doucement les réglages dans cette direction.
   L'IA locale (Ollama) compare aussi les meilleurs et les pires clips et en tire des règles,
   réutilisées ensuite pour choisir les moments et écrire titres et accroches.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import timedelta

import numpy as np
from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import Clip, ClipInfo, Post, PostStat, Source, Tuning, Video, now
from .pipeline import llm
from .pipeline.highlights import DEFAULT_WEIGHTS
from .publish import creds, tiktok, youtube

log = logging.getLogger(__name__)

SIGNALS = {"spike": "les pics de son (cris, rires)", "density": "le débit de parole", "excite": "les mots d'excitation"}
DURATION_BUCKETS = [(0, 45, 40), (45, 60, 52), (60, 75, 67), (75, 90, 82), (90, 1e9, 100)]  # (de, à, durée visée)


# ---------- Réglages courants ----------

def defaults() -> dict:
    return {"weights": dict(DEFAULT_WEIGHTS), "llm_mix": 0.6, "clip_target": get_settings().clip_target,
            "rules": [], "top": [], "flop": [], "insights": []}


def latest() -> Tuning | None:
    with db.session() as s:
        return s.exec(select(Tuning).order_by(col(Tuning.id).desc())).first()


def current() -> dict:
    """Réglages à appliquer (les valeurs par défaut si l'apprentissage est coupé ou n'a encore rien appris)."""
    d = defaults()
    t = latest() if get_settings().learning else None
    if t:
        d.update(json.loads(t.data))
        d["tuning_id"] = t.id
    return d


def guidance(t: dict) -> str:
    """Texte ajouté au prompt de l'IA quand elle note un moment et écrit titre + accroche."""
    lines = []
    if t.get("rules"):
        lines.append("Ce qui marche sur notre chaîne (appris de nos statistiques) :")
        lines += [f"- {r}" for r in t["rules"]]
    if t.get("top"):
        lines.append("Titres / accroches de nos clips qui ont le plus marché :")
        lines += [f"- « {c['title']} » / « {c.get('hook', '')} »" for c in t["top"][:5]]
    if t.get("flop"):
        lines.append("Et de ceux qui ont le moins marché (à ne pas imiter) :")
        lines += [f"- « {c['title']} » / « {c.get('hook', '')} »" for c in t["flop"][:3]]
    return "\n".join(lines)


def save_clip_info(clip_id: int, data: dict) -> None:
    with db.session() as s:
        s.merge(ClipInfo(clip_id=clip_id, data=json.dumps(data, ensure_ascii=False)))
        s.commit()


# ---------- Relevé des statistiques ----------

FETCHERS = {"youtube": (youtube.configured, youtube.stats), "tiktok": (tiktok.configured, tiktok.stats)}


def fetch_stats() -> int:
    """Enregistre un relevé pour chaque publication des `stats_days` derniers jours. Renvoie le nombre de relevés."""
    cfg = get_settings()
    since = now() - timedelta(days=cfg.stats_days)
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.status == "published", Post.remote_id != "",
                                          Post.published_at >= since)).all()
    added = 0
    for platform, (configured, fetch) in FETCHERS.items():
        mine = {p.remote_id: p for p in posts if p.platform == platform}
        if not mine or not configured() or creds.load(platform) is None:
            continue
        try:
            found = fetch(list(mine))
        except Exception as e:
            log.warning("Statistiques %s indisponibles : %s", platform, e)
            continue
        with db.session() as s:
            for rid, st in found.items():
                if rid in mine:
                    s.add(PostStat(post_id=mine[rid].id, **{k: v for k, v in st.items() if k in PostStat.model_fields}))
                    added += 1
            s.commit()
    log.info("Statistiques : %d relevé(s)", added)
    return added


# ---------- Performance de chaque clip ----------

def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def _rank01(x: np.ndarray) -> np.ndarray:
    if len(x) < 2:
        return np.full(len(x), 0.5)
    r = np.argsort(np.argsort(x, kind="stable"), kind="stable").astype(float)
    return r / (len(x) - 1)


def spearman(x, y) -> float:
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 3 or x.std() < 1e-9 or y.std() < 1e-9:
        return 0.0
    rx, ry = _rank01(x), _rank01(y)
    c = np.corrcoef(rx, ry)[0, 1]
    return 0.0 if math.isnan(c) else float(c)


def snapshot_at_age(post: Post, stats: list[PostStat], age_h: float) -> PostStat | None:
    """Relevé le plus proche de l'âge visé (sans le précéder de plus de 12 h)."""
    ok = [st for st in stats if (st.fetched_at - post.published_at).total_seconds() / 3600 >= age_h - 12]
    if not ok:
        return None
    return min(ok, key=lambda st: abs((st.fetched_at - post.published_at).total_seconds() / 3600 - age_h))


def performances() -> dict[int, float]:
    """{clip_id: performance 0-1}, comparée aux autres clips de la même plateforme."""
    cfg = get_settings()
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.status == "published", Post.published_at != None)).all()  # noqa: E711
        stats = s.exec(select(PostStat).where(col(PostStat.post_id).in_([p.id for p in posts]))).all()
    by_post: dict[int, list[PostStat]] = {}
    for st in stats:
        by_post.setdefault(st.post_id, []).append(st)
    per_clip: dict[int, list[float]] = {}
    for platform in FETCHERS:
        rows = []
        for p in posts:
            if p.platform == platform and (snap := snapshot_at_age(p, by_post.get(p.id, []), cfg.learn_age_hours)):
                rows.append((p.clip_id, snap))
        if len(rows) < 3:
            continue
        views = np.array([st.views for _, st in rows], float)
        if views.std() < 1e-9:  # toutes privées ou à 0 vue : rien à apprendre
            continue
        eng = np.array([(st.likes + st.comments + st.shares) / max(st.views, 1) for _, st in rows])
        ret = np.array([st.avg_view_pct if st.avg_view_pct is not None else np.nan for _, st in rows])
        score = 0.6 * _z(np.log1p(views)) + 0.25 * _z(eng)
        if np.isfinite(ret).sum() >= 3:
            score = score + 0.15 * np.nan_to_num(_z(np.where(np.isfinite(ret), ret, np.nanmean(ret))))
        for (cid, _), r in zip(rows, _rank01(score)):
            per_clip.setdefault(cid, []).append(float(r))
    return {cid: sum(v) / len(v) for cid, v in per_clip.items()}


# ---------- Apprentissage ----------

def _clip_rows(perf: dict[int, float]) -> list[dict]:
    with db.session() as s:
        infos = {i.clip_id: json.loads(i.data) for i in s.exec(
            select(ClipInfo).where(col(ClipInfo.clip_id).in_(list(perf)))).all()}
        clips = {c.id: c for c in s.exec(select(Clip).where(col(Clip.id).in_(list(perf)))).all()}
        videos = {v.id: v for v in s.exec(select(Video).where(
            col(Video.id).in_([c.video_id for c in clips.values()]))).all()}
        sources = {x.id: x.name for x in s.exec(select(Source)).all()}
    rows = []
    for cid, p in perf.items():
        c = clips.get(cid)
        if not c:
            continue
        info = infos.get(cid, {})
        v = videos.get(c.video_id)
        rows.append({**info, "clip_id": cid, "perf": p, "title": info.get("title") or c.title,
                     "duration": c.end - c.start, "source": sources.get(v.source_id, "") if v else ""})
    return rows


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def learn(force: bool = False) -> dict:
    """Met à jour les réglages si de nouveaux clips ont été mesurés. Renvoie un résumé."""
    cfg = get_settings()
    rows = _clip_rows(performances())
    n = len(rows)
    prev_t = latest()
    if n < cfg.learn_min_clips:
        return {"samples": n, "learned": False,
                "message": f"{n} clip(s) mesuré(s) sur {cfg.learn_min_clips} nécessaires avant d'ajuster quoi que ce soit."}
    if prev_t and prev_t.samples == n and not force:
        return {"samples": n, "learned": False, "message": "Pas de nouveau clip mesuré depuis la dernière analyse."}

    prev = current()
    out = defaults()
    insights = []

    # 1. Poids des signaux qui repèrent les moments forts
    feat = [r for r in rows if all(k in r for k in DEFAULT_WEIGHTS)]
    if len(feat) >= cfg.learn_min_clips:
        yf = np.array([r["perf"] for r in feat])
        target = {k: DEFAULT_WEIGHTS[k] * (1 + float(np.clip(spearman([r[k] for r in feat], yf), -0.6, 0.6)))
                  for k in DEFAULT_WEIGHTS}
        tot = sum(target.values())
        w = {k: 0.6 * prev["weights"].get(k, DEFAULT_WEIGHTS[k]) + 0.4 * target[k] / tot for k in DEFAULT_WEIGHTS}
        tot = sum(w.values())
        out["weights"] = {k: round(v / tot, 3) for k, v in w.items()}
        best = max(out["weights"], key=lambda k: out["weights"][k] - DEFAULT_WEIGHTS[k])
        if out["weights"][best] - DEFAULT_WEIGHTS[best] > 0.02:
            insights.append(f"Les clips qui marchent ont surtout {SIGNALS[best]} : poids "
                            f"{_pct(DEFAULT_WEIGHTS[best])} → {_pct(out['weights'][best])}.")
    else:
        out["weights"] = prev["weights"]

    # 2. Confiance dans la note de l'IA par rapport à l'analyse audio
    rated = [r for r in rows if r.get("llm") is not None and r.get("heur") is not None]
    if len(rated) >= cfg.learn_min_clips:
        yr = np.array([r["perf"] for r in rated])
        c_llm = spearman([r["llm"] for r in rated], yr)
        c_heur = spearman([r["heur"] for r in rated], yr)
        goal = float(np.clip(0.6 + 0.5 * (c_llm - c_heur), 0.3, 0.85))
        out["llm_mix"] = round(0.6 * prev["llm_mix"] + 0.4 * goal, 3)
        who = "l'IA" if c_llm > c_heur else "l'analyse du son et de la parole"
        insights.append(f"C'est {who} qui prédit le mieux les bons clips : part de l'IA dans la note "
                        f"{_pct(out['llm_mix'])}.")
    else:
        out["llm_mix"] = prev["llm_mix"]

    # 3. Durée idéale
    buckets = []
    for lo, hi, center in DURATION_BUCKETS:
        vals = [r["perf"] for r in rows if lo <= r["duration"] < hi]
        if len(vals) >= 3:
            buckets.append((float(np.mean(vals)), center, lo, hi, len(vals)))
    out["clip_target"] = prev["clip_target"]
    if len(buckets) >= 2:
        score, center, lo, hi, cnt = max(buckets)
        goal = float(np.clip(center, cfg.clip_min, cfg.part_max))
        out["clip_target"] = round(prev["clip_target"] + 0.5 * (goal - prev["clip_target"]), 1)
        span = f"plus de {lo} s" if hi > 1e8 else f"{lo}-{hi} s"
        insights.append(f"Les clips de {span} marchent le mieux ({cnt} clips) : durée visée {out['clip_target']:.0f} s.")

    # 4. Accroches en question
    hooks = [r for r in rows if r.get("hook")]
    q = [r["perf"] for r in hooks if r["hook"].rstrip().endswith("?")]
    nq = [r["perf"] for r in hooks if not r["hook"].rstrip().endswith("?")]
    if len(q) >= 3 and len(nq) >= 3 and abs(np.mean(q) - np.mean(nq)) > 0.1:
        better = np.mean(q) > np.mean(nq)
        insights.append("Les accroches en forme de question marchent mieux." if better
                        else "Les accroches affirmatives marchent mieux que les questions.")

    # 5. Streamers (à titre d'info)
    by_src: dict[str, list[float]] = {}
    for r in rows:
        if r["source"]:
            by_src.setdefault(r["source"], []).append(r["perf"])
    ranked = sorted(((np.mean(v), k) for k, v in by_src.items() if len(v) >= 3), reverse=True)
    if len(ranked) >= 2:
        insights.append(f"Meilleure source : {ranked[0][1]} ; la moins bonne : {ranked[-1][1]}.")

    # 6. Exemples + règles écrites par l'IA locale
    ordered = sorted(rows, key=lambda r: -r["perf"])
    pick = lambda r: {"title": r["title"], "hook": r.get("hook", ""), "text": r.get("text", ""),  # noqa: E731
                      "perf": round(r["perf"], 2)}
    out["top"] = [pick(r) for r in ordered[:5]]
    out["flop"] = [pick(r) for r in ordered[-3:][::-1]] if n >= 8 else []
    out["rules"] = llm.analyze(out["top"], out["flop"]) or prev.get("rules", [])
    out["insights"] = insights

    with db.session() as s:
        s.add(Tuning(samples=n, data=json.dumps(out, ensure_ascii=False)))
        s.commit()
    log.info("Apprentissage : %d clips mesurés, réglages mis à jour (%s)", n, out["weights"])
    return {"samples": n, "learned": True, "message": f"Réglages mis à jour à partir de {n} clips."}


def run(force: bool = False) -> dict:
    fetch_stats()
    return learn(force)
