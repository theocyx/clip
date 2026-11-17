"""Détection des moments forts : sonie, densité de parole, mots d'excitation."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

EXCITE = {
    "mdr", "ptdr", "lol", "haha", "hahaha", "ahah", "wow", "ouah", "waouh", "oh", "ah", "putain", "incroyable",
    "quoi", "non", "nan", "ouais", "go", "gg", "dingue", "fou", "folie", "jamais", "sérieux", "attends", "regarde",
    "énorme", "trop", "mais", "pardon", "bordel", "chaud", "insane", "clip", "omg", "what",
}
_WORD = re.compile(r"[\wàâäéèêëïîôöùûüç']+", re.I)


@dataclass
class Moment:
    start: float
    end: float
    score: float
    title: str = ""
    hook: str = ""
    parts: list[tuple[float, float]] = field(default_factory=list)
    features: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _z(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    sd = x.std()
    return np.clip((x - x.mean()) / sd, -3, 4) if sd > 1e-6 else np.zeros_like(x)


def _smooth(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1 or len(x) == 0:
        return x
    return np.convolve(x, np.ones(w) / w, mode="same")


def _rolling_median(x: np.ndarray, w: int) -> np.ndarray:
    half = w // 2
    return np.array([np.median(x[max(0, i - half): i + half + 1]) for i in range(len(x))]) if len(x) else x


DEFAULT_WEIGHTS = {"spike": 0.45, "density": 0.25, "excite": 0.30}


def components(loudness: np.ndarray, segments: list[dict]) -> dict[str, np.ndarray]:
    """Signaux par seconde : pics de sonie, densité de parole, excitation (mots, ponctuation)."""
    n = len(loudness)
    if n == 0:
        return {k: np.zeros(0) for k in DEFAULT_WEIGHTS}
    # pics de sonie par rapport au niveau local (60 s), calculés sur un signal sous-échantillonné pour la vitesse
    step = 5 if n > 3600 else 1
    base = _rolling_median(loudness[::step], max(3, 60 // step))
    base = np.repeat(base, step)[:n]
    spike = _z(_smooth(loudness - base, 3))

    density = np.zeros(n)
    excite = np.zeros(n)
    for seg in segments:
        text = seg.get("text", "")
        punct = text.count("!") * 1.0 + text.count("?") * 0.5
        s0, s1 = int(seg["start"]), min(n - 1, int(seg["end"]))
        if s1 >= s0 and punct:
            excite[s0:s1 + 1] += punct / (s1 - s0 + 1)
        for w in seg.get("words", []):
            i = int(w["start"])
            if 0 <= i < n:
                density[i] += 1
                tokens = _WORD.findall(w["word"].lower())
                if any(t in EXCITE for t in tokens):
                    excite[i] += 1
    return {"spike": spike, "density": _z(_smooth(density, 5)), "excite": _z(_smooth(excite, 5))}


def combine(comps: dict[str, np.ndarray], weights: dict[str, float] | None = None) -> np.ndarray:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    return sum(w[k] * comps[k] for k in DEFAULT_WEIGHTS)


def second_scores(loudness: np.ndarray, segments: list[dict], weights: dict[str, float] | None = None) -> np.ndarray:
    """Score d'intérêt pour chaque seconde."""
    if len(loudness) == 0:
        return loudness
    return combine(components(loudness, segments), weights)


def candidate_windows(scores: np.ndarray, length: int, count: int, step: int = 5, gap: int = 10) -> list[tuple[int, float]]:
    """Meilleures fenêtres (début, score) sans chevauchement."""
    n = len(scores)
    if n == 0:
        return []
    length = min(length, n)
    peak = _smooth(scores, 10)
    cands = []
    for a in range(0, n - length + 1, step):
        win = scores[a:a + length]
        cands.append((a, float(win.mean() + 0.5 * peak[a:a + length].max())))
    cands.sort(key=lambda c: -c[1])
    chosen: list[tuple[int, float]] = []
    for a, sc in cands:
        if all(abs(a - b) >= length + gap for b, _ in chosen):
            chosen.append((a, sc))
            if len(chosen) >= count:
                break
    return chosen


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…"))


def snap(start: float, segments: list[dict], target: float, min_len: float, max_len: float, total: float) -> tuple[float, float]:
    """Cale le clip sur des débuts/fins de phrases ; prolonge si la phrase n'est pas finie."""
    if not segments:
        return start, min(total, start + target)
    # début : début du segment en cours (ou le suivant)
    s_idx = 0
    for i, seg in enumerate(segments):
        if seg["end"] > start:
            s_idx = i
            break
    else:
        s_idx = len(segments) - 1
    begin = max(0.0, segments[s_idx]["start"] - 0.3)
    # fin : premier segment qui dépasse la durée visée, puis jusqu'à une fin de phrase
    end = begin + target
    for j in range(s_idx, len(segments)):
        seg = segments[j]
        if seg["end"] - begin >= target:
            end = seg["end"]
            k = j
            while not _ends_sentence(segments[k]["text"]) and k + 1 < len(segments) and segments[k + 1]["end"] - begin <= max_len:
                k += 1
                end = segments[k]["end"]
            break
        end = seg["end"]
    end = max(end, begin + min_len)
    end = min(end + 0.4, begin + max_len, total)
    return begin, end


def split_parts(begin: float, end: float, segments: list[dict], part_max: float) -> list[tuple[float, float]]:
    """Découpe un clip trop long en parties égales, sur des limites de phrases."""
    dur = end - begin
    if dur <= part_max:
        return [(begin, end)]
    k = math.ceil(dur / part_max)
    bounds = [s["end"] for s in segments if begin < s["end"] < end]
    cuts = []
    for i in range(1, k):
        ideal = begin + dur * i / k
        best = min(bounds, key=lambda b: abs(b - ideal)) if bounds else ideal
        if abs(best - ideal) > dur / (2 * k):  # pas de limite de phrase proche
            best = ideal
        cuts.append(best)
    edges = [begin] + sorted(set(cuts)) + [end]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] - edges[i] > 5]


def text_between(segments: list[dict], a: float, b: float) -> str:
    return " ".join(s["text"] for s in segments if s["end"] > a and s["start"] < b).strip()


def find_moments(loudness: np.ndarray, segments: list[dict], *, count: int, target: float, min_len: float,
                 max_len: float, pool_factor: int = 1, weights: dict[str, float] | None = None,
                 exclude: list[tuple[float, float]] | None = None) -> list[Moment]:
    """exclude : passages interdits (pub, sponsor…) ; aucun clip ne les touche."""
    total = float(len(loudness))
    comps = components(loudness, segments)
    scores = combine(comps, weights) if len(loudness) else loudness
    exclude = exclude or []
    if exclude and len(scores):
        scores = scores.copy()
        for a, b in exclude:
            scores[max(0, int(a)):max(0, int(math.ceil(b)))] = -10.0
    wins = candidate_windows(scores, int(target), count * pool_factor)
    moments = []
    for a, sc in wins:
        b, e = snap(a, segments, target, min_len, max_len, total)
        if any(min(e, m.end) - max(b, m.start) > 5 for m in moments):
            continue
        if any(min(e, r1) - max(b, r0) > 2 for r0, r1 in exclude):
            continue
        i0, i1 = int(b), max(int(b) + 1, int(e))
        feats = {k: round(float(v[i0:i1].mean()), 3) if len(v[i0:i1]) else 0.0 for k, v in comps.items()}
        moments.append(Moment(b, e, sc, features=feats))
    return moments
