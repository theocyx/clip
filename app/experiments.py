"""Amélioration continue du montage, réglage par réglage.

Chaque nouvelle vidéo utilise la meilleure version connue de chaque réglage (voix, débit, zoom, sous-titres…).
Une part des vidéos (EXPERIMENT_RATE, 25 % par défaut) essaie une autre version d'UN SEUL réglage : on sait
ainsi à quoi attribuer la différence. Quand les stats arrivent (performance 0-1 de `learning.performances`,
même âge, même plateforme), on compare les versions entre vidéos du même type (clip, actu, histoire) ; une
version qui fait nettement mieux sur au moins EXPERIMENT_MIN vidéos devient la version par défaut, et les essais
continuent pour suivre les goûts du public.

Les meilleures et les pires vidéos d'actu / d'histoire servent aussi d'exemples à l'IA qui écrit les suivantes
(`examples`)."""
from __future__ import annotations

import json
import logging
import random

from sqlmodel import col, select

from . import db
from .config import get_settings
from .models import ClipInfo

log = logging.getLogger(__name__)

KINDS = {"clip": "Clips", "news": "Actus", "story": "Histoires"}
PACE = {"posee": 0.94, "normale": 1.0, "rapide": 1.07}  # multiplicateur du débit de la voix
LABELS = {"on": "avec", "off": "sans", "posee": "posée", "normale": "normale", "rapide": "rapide"}
MARGIN = 0.05  # écart de performance (0-1) nécessaire pour détrôner la version par défaut


def _voices(raw: str, default: str) -> list[str]:
    out = [v.strip() for v in (raw or "").split(",") if v.strip()]
    return list(dict.fromkeys(([default] if default else []) + out))


def factors(kind: str) -> dict[str, dict]:
    """{réglage: {"label", "variants", "default"}} pour un type de vidéo. La version par défaut vient du .env."""
    s = get_settings()
    onoff = lambda b: "on" if b else "off"  # noqa: E731
    if kind == "clip":
        return {
            "zoom": {"label": "Zoom sur la réaction", "variants": ["off", "on"], "default": onoff(s.punch_zoom)},
            "keywords": {"label": "Mots forts en couleur (sous-titres)", "variants": ["off", "on"],
                         "default": onoff(s.sub_keywords)},
            "pop": {"label": "Sous-titres qui rebondissent", "variants": ["on", "off"], "default": onoff(s.sub_pop)},
            "cta": {"label": "« Abonne-toi » à l'écran en fin de clip", "variants": ["off", "on"],
                    "default": onoff(s.clip_cta)},
        }
    if (s.news_tts or "").lower() != "edge":  # voix Piper locale : une seule voix, seul le débit varie
        return {"pace": {"label": "Débit de la voix", "variants": list(PACE), "default": "normale"}}
    story = kind == "story"
    default = s.story_edge_voice if story else s.news_edge_voice
    return {
        "voice": {"label": "Voix", "variants": _voices(s.story_voices if story else s.news_voices, default),
                  "default": default},
        "pace": {"label": "Débit de la voix", "variants": list(PACE), "default": "normale"},
    }


def label(variant: str) -> str:
    if variant.startswith("fr-") and variant.endswith("Neural"):  # fr-FR-RemyMultilingualNeural → Remy
        return variant.split("-")[-1].replace("MultilingualNeural", "").replace("Neural", "")
    return LABELS.get(variant, variant)


def kind_of(info: dict) -> str:
    if info.get("kind") == "compil":  # vidéo longue : jamais comparée aux Shorts
        return "compil"
    if info.get("kind") == "news":
        return "story" if info.get("story") else "news"
    return "clip"


def measured(kind: str, perf: dict[int, float] | None = None) -> list[tuple[float, dict]]:
    """[(performance, infos du clip)] des vidéos mesurées de ce type."""
    if perf is None:
        from . import learning

        perf = learning.performances()
    if not perf:
        return []
    with db.session() as s:
        rows = s.exec(select(ClipInfo).where(col(ClipInfo.clip_id).in_(list(perf)))).all()
    out = []
    for r in rows:
        try:
            info = json.loads(r.data)
        except ValueError:
            continue
        if kind_of(info) == kind:
            out.append((perf[r.clip_id], info))
    return out


def results(kind: str, perf: dict[int, float] | None = None) -> dict[str, dict[str, list[float]]]:
    """{réglage: {version: [performances]}} des vidéos qui ont noté leurs versions."""
    table: dict[str, dict[str, list[float]]] = {}
    for p, info in measured(kind, perf):
        for f, v in (info.get("exp") or {}).items():
            table.setdefault(f, {}).setdefault(str(v), []).append(p)
    return table


def score(vals: list[float]) -> float:
    """Moyenne tirée vers 0,5 quand il y a peu de vidéos (2 vidéos « moyennes » fictives)."""
    return (sum(vals) + 1.0) / (len(vals) + 2)


def winner(spec: dict, vals: dict[str, list[float]]) -> str:
    """Version à utiliser : la version par défaut, sauf si une autre fait nettement mieux sur assez de vidéos."""
    need = max(1, get_settings().experiment_min)
    best = spec["default"]
    ref = vals.get(best, [])
    if len(ref) < need:
        return best
    top = score(ref) + MARGIN
    for v in spec["variants"]:
        got = vals.get(v, [])
        if v != best and len(got) >= need and score(got) > top:
            best, top = v, score(got)
    return best


def choose(kind: str, rng: random.Random | None = None, perf: dict[int, float] | None = None) -> dict[str, str]:
    """Versions pour une nouvelle vidéo : les meilleures connues, sauf (EXPERIMENT_RATE) un réglage pris au hasard
    qui essaie une autre version (la moins testée d'abord)."""
    rng = rng or random
    s = get_settings()
    specs = factors(kind)
    if not s.experiments:
        return {f: spec["default"] for f, spec in specs.items()}
    try:
        table = results(kind, perf)
    except Exception as e:  # base pas prête… : versions par défaut, jamais de blocage du montage
        log.debug("Résultats des essais illisibles : %s", e)
        table = {}
    out = {f: winner(spec, table.get(f, {})) for f, spec in specs.items()}
    testable = [f for f, spec in specs.items() if len(spec["variants"]) > 1]
    if testable and rng.random() < s.experiment_rate:
        f = rng.choice(testable)
        others = [v for v in specs[f]["variants"] if v != out[f]]
        counts = {v: len(table.get(f, {}).get(v, [])) for v in others}
        least = min(counts.values())
        out[f] = rng.choice([v for v in others if counts[v] == least])
        log.info("Amélioration continue (%s) : essai « %s » = %s", KINDS.get(kind, kind), specs[f]["label"],
                 label(out[f]))
    return out


def on(exp: dict, factor: str) -> bool:
    return exp.get(factor) == "on"


def pace(exp: dict) -> float:
    return PACE.get(exp.get("pace", "normale"), 1.0)


def summary(perf: dict[int, float] | None = None) -> list[dict]:
    """Lignes du tableau « Amélioration continue » de la page Stats."""
    if perf is None:
        from . import learning

        perf = learning.performances()
    rows = []
    for kind, kind_label in KINDS.items():
        table = results(kind, perf)
        for f, spec in factors(kind).items():
            vals = table.get(f, {})
            win = winner(spec, vals)
            for v in spec["variants"]:
                got = vals.get(v, [])
                rows.append({"kind": kind_label, "factor": spec["label"], "variant": label(v), "n": len(got),
                             "perf": (sum(got) / len(got)) if got else None, "default": v == spec["default"],
                             "used": v == win})
    return rows


def insights(perf: dict[int, float] | None = None) -> list[str]:
    """Phrases pour « Ce que l'outil a appris » quand une version a détrôné le réglage par défaut."""
    out = []
    for kind, kind_label in KINDS.items():
        table = results(kind, perf)
        for f, spec in factors(kind).items():
            win = winner(spec, table.get(f, {}))
            if win != spec["default"]:
                got = table[f][win]
                out.append(f"{kind_label} : « {spec['label']} » {label(win)} fait mieux que "
                           f"{label(spec['default'])} ({len(got)} vidéos) : adopté par défaut.")
    return out


def examples(kind: str, perf: dict[int, float] | None = None, n_top: int = 3, n_flop: int = 2) -> dict:
    """Meilleures et pires vidéos mesurées de ce type (titre, accroche, 1re phrase), pour guider l'IA."""
    rows = sorted(measured(kind, perf), key=lambda x: -x[0])
    if len(rows) < max(4, n_top + n_flop):
        return {"top": [], "flop": []}
    pick = lambda info: {"title": info.get("title", ""), "hook": info.get("hook", ""),  # noqa: E731
                         "opening": (info.get("text") or "").split(". ")[0][:200]}
    return {"top": [pick(i) for _, i in rows[:n_top]], "flop": [pick(i) for _, i in rows[-n_flop:][::-1]]}
