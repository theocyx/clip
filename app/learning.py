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

from . import db, notify
from .config import get_settings
from .models import Clip, ClipInfo, Post, PostStat, Source, Tuning, Video, now
from .pipeline import llm
from .pipeline.highlights import DEFAULT_WEIGHTS
from .publish import creds, tiktok, youtube

log = logging.getLogger(__name__)

SIGNALS = {"spike": "les pics de son (cris, rires)", "density": "le débit de parole", "excite": "les mots d'excitation"}
DURATION_BUCKETS = [(0, 25, 22), (25, 35, 30), (35, 45, 40), (45, 60, 52), (60, 90, 70),
                    (90, 1e9, 100)]  # (de, à, durée visée)
DUR_VERSION = 3  # durée apprise avant cette version (clips courts de 32 s) ignorée : on repart de CLIP_TARGET


# ---------- Réglages courants ----------

def defaults() -> dict:
    return {"weights": dict(DEFAULT_WEIGHTS), "llm_mix": 0.6, "clip_target": get_settings().clip_target,
            "rules": [], "top": [], "flop": [], "insights": [], "teaser": True, "dur_v": DUR_VERSION, "min_llm": 0}


def max_len() -> float:
    """Durée max d'un clip : CLIP_MAX, et jamais plus qu'une partie quand les parties sont désactivées."""
    cfg = get_settings()
    return cfg.clip_max if cfg.allow_parts else min(cfg.clip_max, cfg.part_max)


def latest() -> Tuning | None:
    with db.session() as s:
        return s.exec(select(Tuning).order_by(col(Tuning.id).desc())).first()


def current() -> dict:
    """Réglages à appliquer (les valeurs par défaut si l'apprentissage est coupé ou n'a encore rien appris)."""
    d = defaults()
    t = latest() if get_settings().learning else None
    if t:
        data = json.loads(t.data)
        if data.get("dur_v") != DUR_VERSION:
            data.pop("clip_target", None)
            data["dur_v"] = DUR_VERSION
        d.update(data)
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


def gaps(posts: list[Post]) -> dict[int, float | None]:
    """{post_id: minutes depuis la publication précédente sur la même plateforme} (None pour la première)."""
    out: dict[int, float | None] = {}
    last: dict[str, object] = {}
    for p in sorted((p for p in posts if p.published_at), key=lambda p: p.published_at):
        prev = last.get(p.platform)
        out[p.id] = (p.published_at - prev).total_seconds() / 60 if prev else None
        last[p.platform] = p.published_at
    return out


def is_burst(gap: float | None) -> bool:
    m = get_settings().burst_minutes
    return m > 0 and gap is not None and gap < m


def performances() -> dict[int, float]:
    """{clip_id: performance 0-1}, comparée aux autres clips de la même plateforme."""
    cfg = get_settings()
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.status == "published", Post.published_at != None)).all()  # noqa: E711
        stats = s.exec(select(PostStat).where(col(PostStat.post_id).in_([p.id for p in posts]))).all()
    by_post: dict[int, list[PostStat]] = {}
    for st in stats:
        by_post.setdefault(st.post_id, []).append(st)
    burst = {pid for pid, g in gaps(posts).items() if is_burst(g)}
    per_clip: dict[int, list[float]] = {}
    for platform in FETCHERS:
        rows = []
        for p in posts:
            if p.id in burst:  # noyé dans une rafale : ses vues ne disent rien du clip lui-même
                continue
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
        if np.isfinite(ret).sum() >= 3:  # la rétention décide de la diffusion des Shorts : elle compte autant
            score = 0.45 * _z(np.log1p(views)) + 0.2 * _z(eng) + 0.35 * np.nan_to_num(
                _z(np.where(np.isfinite(ret), ret, np.nanmean(ret))))
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
        if info.get("kind") == "news":  # les vidéos d'actu n'apprennent rien sur le montage des clips
            continue
        v = videos.get(c.video_id)
        rows.append({**info, "clip_id": cid, "perf": p, "title": info.get("title") or c.title,
                     "duration": c.end - c.start, "source": sources.get(v.source_id, "") if v else ""})
    return rows


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def learn(force: bool = False) -> dict:
    """Met à jour les réglages si de nouveaux clips ont été mesurés. Renvoie un résumé."""
    cfg = get_settings()
    perf = performances()
    rows = _clip_rows(perf)
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
        goal = float(np.clip(center, cfg.clip_min, max_len()))
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

    # 5. Ouverture choc (meilleur passage montré en premier) : gardée seulement si elle aide
    out["teaser"] = prev.get("teaser", True)
    with_t = [r["perf"] for r in rows if r.get("teaser") is True]
    without = [r["perf"] for r in rows if r.get("teaser") is False]
    if len(with_t) >= 3 and len(without) >= 3 and abs(np.mean(with_t) - np.mean(without)) > 0.1:
        out["teaser"] = bool(np.mean(with_t) > np.mean(without))
        insights.append("Les clips qui ouvrent sur leur meilleur passage marchent mieux : ouverture choc gardée."
                        if out["teaser"] else
                        "Les clips sans ouverture choc marchent mieux : elle devient l'exception.")

    # 6. Streamers (à titre d'info)
    by_src: dict[str, list[float]] = {}
    for r in rows:
        if r["source"]:
            by_src.setdefault(r["source"], []).append(r["perf"])
    ranked = sorted(((np.mean(v), k) for k, v in by_src.items() if len(v) >= 3), reverse=True)
    if len(ranked) >= 2:
        insights.append(f"Meilleure source : {ranked[0][1]} ; la moins bonne : {ranked[-1][1]}.")

    # 7. Note IA minimum pour publier : relevée si les clips mieux notés font nettement mieux
    out["min_llm"] = prev.get("min_llm", 0)
    if len(rated) >= cfg.learn_min_clips:
        for t in (9, 8, 7):
            hi = [r["perf"] for r in rated if r["llm"] >= t]
            lo = [r["perf"] for r in rated if r["llm"] < t]
            if len(hi) >= 3 and len(lo) >= 3 and np.mean(hi) - np.mean(lo) > 0.15:
                out["min_llm"] = t
                insights.append(f"Les clips notés {t}/10 ou plus par l'IA font nettement mieux : "
                                f"seuls ceux-là partent désormais.")
                break

    # 8. Publications en rafale
    effect = burst_effect(reviews())
    if effect:
        insights.append(effect + " Les rafales sont écartées de l'apprentissage.")

    # 9. Versions du montage essayées (voix, débit, zoom, sous-titres…) qui ont fait mieux que le défaut
    try:
        from . import experiments

        insights += experiments.insights(perf)
    except Exception:
        log.exception("Bilan des essais de montage impossible")

    # 10. Exemples + règles écrites par l'IA locale
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


# ---------- Analyse de chaque Short publié ----------

VERDICTS = {"top": "🔥 Top", "ok": "👍 Correct", "flop": "👎 Flop", "early": "⏳ Trop tôt"}


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def reviews() -> dict[int, dict]:
    """{post_id: analyse} pour chaque publication suivie : verdict par rapport aux autres Shorts de la plateforme
    (vues, rétention, likes, commentaires) et explications en clair."""
    cfg = get_settings()
    since = now() - timedelta(days=cfg.stats_days)
    with db.session() as s:
        posts = s.exec(select(Post).where(Post.status == "published", Post.published_at >= since)).all()
        stats = s.exec(select(PostStat).where(col(PostStat.post_id).in_([p.id for p in posts]))
                       .order_by(col(PostStat.fetched_at))).all()
        infos = {i.clip_id: i.data for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([p.clip_id for p in posts]))).all()}
        clips = {c.id: c for c in s.exec(select(Clip).where(col(Clip.id).in_([p.clip_id for p in posts]))).all()}
    last: dict[int, PostStat] = {}
    for st in stats:
        last[st.post_id] = st
    gap = gaps(posts)
    age = lambda p: (now() - p.published_at).total_seconds() / 3600  # noqa: E731
    mature = [p for p in posts if p.id in last and age(p) >= cfg.review_age_hours]
    med: dict[str, dict[str, float | None]] = {}
    for platform in FETCHERS:
        mine = [last[p.id] for p in mature if p.platform == platform and not is_burst(gap.get(p.id))] or \
               [last[p.id] for p in mature if p.platform == platform]
        med[platform] = {
            "views": _median([st.views for st in mine]),
            "ret": _median([st.avg_view_pct for st in mine]),
            "likes": _median([st.likes / st.views for st in mine if st.views]),
            "comments": _median([st.comments / st.views for st in mine if st.views]),
        }
    out = {}
    for p in posts:
        st = last.get(p.id)
        g = gap.get(p.id)
        try:
            info = json.loads(infos.get(p.clip_id) or "{}")
        except ValueError:
            info = {}
        clip = clips.get(p.clip_id)
        reasons = []
        if is_burst(g):
            reasons.append(f"Publié {g:.0f} min après le précédent : en rafale, la plateforme le pousse très peu.")
        if not st or age(p) < cfg.review_age_hours:
            out[p.id] = {"verdict": "early", "label": VERDICTS["early"], "reasons": reasons, "burst": is_burst(g)}
            continue
        m = med[p.platform]
        mv = m["views"] or 0
        if st.views >= max(1.5 * mv, 50):
            verdict = "top"
        elif st.views < 10 or st.views <= 0.5 * mv:
            verdict = "flop"
        else:
            verdict = "ok"
        vs = f" (médiane {mv:.0f})" if mv else ""
        if st.views == 0:
            reasons.append(f"0 vue après {age(p):.0f} h : pas montré du tout (rafale, doublon, vidéo privée ou bloquée ?).")
        elif verdict == "top":
            reasons.append(f"{st.views} vues{vs} : bien au-dessus des autres.")
        elif verdict == "flop":
            reasons.append(f"{st.views} vues{vs} : bien en dessous des autres.")
        if st.avg_view_pct is not None:
            r, mr = st.avg_view_pct, m["ret"]
            if r >= 90:
                reasons.append(f"Regardé à {r:.0f} % : revu en boucle, excellent pour la diffusion.")
            elif mr and r < 0.8 * mr:
                reasons.append(f"Rétention {r:.0f} % (médiane {mr:.0f} %) : on décroche tôt, début ou accroche à revoir.")
            elif mr and r > 1.15 * mr:
                reasons.append(f"Rétention {r:.0f} % (médiane {mr:.0f} %) : on le regarde jusqu'au bout.")
        if st.views >= 30:
            lr, ml = st.likes / st.views, m["likes"]
            if ml and lr >= 1.5 * ml:
                reasons.append(f"{lr * 100:.1f} % de likes (médiane {ml * 100:.1f} %) : le moment plaît.")
            elif ml and lr < 0.5 * ml:
                reasons.append(f"{lr * 100:.1f} % de likes (médiane {ml * 100:.1f} %) : vu mais peu aimé.")
            mc = m["comments"]
            if st.comments >= 2 and (not mc or st.comments / st.views >= 1.5 * mc):
                reasons.append(f"{st.comments} commentaires : la question fait réagir.")
            elif st.comments == 0 and st.views >= 200:
                reasons.append("Aucun commentaire malgré les vues : question de fin à revoir.")
        if clip:
            dur = clip.end - clip.start
            bits = [f"{dur:.0f} s"]
            if info.get("llm") is not None:
                bits.append(f"note IA {float(info['llm']):.0f}/10")
            if info.get("polish_score") is not None:
                bits.append(f"relecture {float(info['polish_score']):.0f}/10")
            if info.get("teaser") is not None:
                bits.append("ouverture choc" if info.get("teaser") else "sans ouverture choc")
            reasons.append("Clip : " + ", ".join(bits) + ".")
        out[p.id] = {"verdict": verdict, "label": VERDICTS[verdict], "reasons": reasons, "burst": is_burst(g),
                     "views": st.views, "title": clip.title if clip else "", "platform": p.platform,
                     "age_h": round(age(p))}
    return out


def burst_effect(revs: dict[int, dict]) -> str:
    """Comparaison vues en rafale / vues isolées, quand il y a de quoi comparer."""
    done = [r for r in revs.values() if r["verdict"] != "early"]
    b = [r["views"] for r in done if r["burst"]]
    i = [r["views"] for r in done if not r["burst"]]
    if len(b) >= 3 and len(i) >= 3:
        return (f"Shorts publiés en rafale : {np.median(b):.0f} vues en médiane ({len(b)} Shorts), "
                f"contre {np.median(i):.0f} pour ceux publiés isolément ({len(i)}).")
    return ""


def notify_reviews() -> int:
    """Un message groupé avec le bilan des Shorts qui viennent d'atteindre l'âge d'analyse (chacun une seule fois).
    Renvoie le nombre de Shorts signalés."""
    if not notify.configured():
        return 0
    revs = {pid: r for pid, r in reviews().items() if r["verdict"] != "early"}
    if not revs:
        return 0
    with db.session() as s:
        posts = {p.id: p for p in s.exec(select(Post).where(col(Post.id).in_(list(revs)))).all()}
        infos = {i.clip_id: i for i in s.exec(select(ClipInfo).where(
            col(ClipInfo.clip_id).in_([p.clip_id for p in posts.values()]))).all()}
    new = []
    for pid, r in revs.items():
        p = posts.get(pid)
        if not p:
            continue
        ci = infos.get(p.clip_id)
        try:
            data = json.loads(ci.data) if ci else {}
        except ValueError:
            data = {}
        if p.platform not in data.get("reviewed", []):
            new.append((p, r, data))
    if not new:
        return 0
    new.sort(key=lambda x: -x[1]["views"])
    lines = [f"📊 Bilan de {len(new)} Short(s) publié(s) :"]
    detailed = len(new) <= 5
    for p, r, _ in new[:15]:
        label = "YouTube" if p.platform == "youtube" else "TikTok"
        lines.append(f"{r['label']} · {label} · {r['views']} vues · « {r['title']} »")
        if detailed:
            lines += [f"   • {x}" for x in r["reasons"]]
    if len(new) > 15:
        lines.append(f"… et {len(new) - 15} autre(s). Détail dans Stats.")
    effect = burst_effect(revs)
    if effect:
        lines.append(effect)
    notify.send("\n".join(lines))
    for p, _, data in new:
        data["reviewed"] = data.get("reviewed", []) + [p.platform]
        save_clip_info(p.clip_id, data)
    return len(new)

def run(force: bool = False) -> dict:
    fetch_stats()
    res = learn(force)
    try:
        notify_reviews()
    except Exception:
        log.exception("Bilan des Shorts non envoyé")
    return res
