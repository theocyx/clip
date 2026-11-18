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


def chat_component(chat: np.ndarray | None, n: int) -> np.ndarray | None:
    """Rafales du chat par rapport à son activité habituelle (z-score lissé), None sans chat."""
    if chat is None or n == 0 or len(chat) == 0:
        return None
    c = np.zeros(n)
    c[:min(n, len(chat))] = chat[:n]
    return _z(_smooth(c, 7))


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
                 exclude: list[tuple[float, float]] | None = None, chat: np.ndarray | None = None,
                 chat_weight: float = 0.5) -> list[Moment]:
    """exclude : passages interdits (pub, sponsor…) ; aucun clip ne les touche.
    chat : activité du chat Twitch par seconde (lives enregistrés), ajoutée avec le poids `chat_weight`."""
    total = float(len(loudness))
    comps = components(loudness, segments)
    scores = combine(comps, weights) if len(loudness) else loudness
    cc = chat_component(chat, len(loudness))
    if cc is not None and chat_weight > 0:
        scores = scores + chat_weight * cc
        comps["chat"] = cc
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
        if i1 > i0 and len(scores[i0:i1]):
            feats["peak"] = float(i0 + int(np.argmax(_smooth(scores, 3)[i0:i1])))  # seconde la plus forte
        moments.append(Moment(b, e, sc, features=feats))
    return moments


def teaser(segments: list[dict], peak: float | None, begin: float, end: float,
           length: float = 2.5) -> tuple[float, float] | None:
    """Extrait de ~`length` s autour du moment le plus fort, montré en ouverture du clip (« cold open »).

    Les 2 premières secondes décident si on reste : on montre tout de suite le meilleur passage,
    puis le clip reprend depuis le début. None si le pic est déjà au début (inutile)."""
    if peak is None or length <= 0 or not (begin <= peak <= end) or peak - begin < length + 4:
        return None
    words = [w for s in segments for w in s.get("words", []) if peak - 1.5 <= w["start"] <= peak + length]
    start = next((w["start"] for w in words if w["start"] >= peak - 1.2), peak - 1.0)
    start = max(begin, start - 0.1)
    ends = [w["end"] for w in words if start + 1.2 <= w["end"] <= start + length + 0.5]
    stop = (max(ends) + 0.15) if ends else start + length
    stop = min(stop, end)
    return (round(start, 2), round(stop, 2)) if stop - start >= 1.0 else None


def cut_silences(segments: list[dict], a: float, b: float, min_gap: float = 1.2, keep: float = 0.3,
                 max_cuts: int = 12) -> list[tuple[float, float]]:
    """Morceaux de [a, b] à garder, sans les blancs de plus de `min_gap` s entre deux mots (« jump cuts »).

    On garde `keep` s de part et d'autre de chaque mot pour ne pas couper sec ; au plus `max_cuts` blancs,
    les plus longs. Sans transcription mot à mot, renvoie [(a, b)]."""
    words = sorted((w["start"], w["end"]) for s in segments for w in s.get("words", [])
                   if a <= (w["start"] + w["end"]) / 2 <= b)
    gaps = []
    for (_, e1), (s2, _) in zip(words, words[1:]):
        if s2 - e1 >= min_gap:
            gaps.append((e1 + keep, s2 - keep))
    if words and words[0][0] - a >= min_gap:  # blanc au tout début
        gaps.append((a, words[0][0] - keep))
    gaps = sorted(sorted(gaps, key=lambda g: g[0] - g[1])[:max_cuts])
    pieces, cur = [], a
    for g0, g1 in gaps:
        if g1 - g0 < 0.3:
            continue
        if g0 > cur:
            pieces.append((round(cur, 2), round(g0, 2)))
        cur = max(cur, g1)
    pieces.append((round(cur, 2), round(b, 2)))
    return [p for p in pieces if p[1] - p[0] >= 0.2] or [(a, b)]


def out_time(pieces: list[tuple[float, float]], t: float) -> float | None:
    """Instant `t` de la source, une fois les morceaux montés bout à bout (None s'il a été coupé)."""
    offset = 0.0
    for x, y in pieces:
        if x <= t <= y:
            return offset + t - x
        offset += y - x
    return None
