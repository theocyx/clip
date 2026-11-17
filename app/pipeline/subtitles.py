"""Sous-titres dynamiques (style TikTok) au format ASS : 1 à 3 mots, mot prononcé surligné."""
from __future__ import annotations

W, H = 1080, 1920

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,480,1
Style: Part,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,8,60,60,{part_v},1
Style: Hook,{font},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{hook_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
HIGHLIGHT = r"{\c&H00E5FF&}"  # jaune
RESET = r"{\c&HFFFFFF&}"
NL = r"\N"  # retour à la ligne ASS


def ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").upper()


def words_in(segments: list[dict], a: float, b: float) -> list[dict]:
    out = []
    for seg in segments:
        if seg["end"] < a or seg["start"] > b:
            continue
        for w in seg.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            if a <= mid <= b:
                out.append({"start": w["start"] - a, "end": w["end"] - a, "word": w["word"]})
    return out


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 18, max_pause: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            text_len = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            pause = w["start"] - cur[-1]["end"]
            ends = cur[-1]["word"].endswith((".", "!", "?", ","))
            if len(cur) >= max_words or text_len > max_chars or pause > max_pause or ends:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def hook_lines(text: str, per_line: int = 18, max_lines: int = 2) -> list[str]:
    """Accroche courte découpée en lignes (le rendu ASS ne coupe pas tout seul)."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(",;:-") + "…"
    return lines


def build_ass(segments: list[dict], a: float, b: float, font: str, part_label: str = "",
              hook: str = "", hook_seconds: float = 0, hook_in_ass: bool = True, top: int = 0) -> str:
    """hook_in_ass=False : l'accroche est rendue à part en image (emojis couleur), on garde juste la place.

    top : décalage vers le bas de l'accroche et de « Partie x/y » (format split : sous la facecam)."""
    dur = b - a
    hl = hook_lines(hook) if hook else []
    part_v = (170 + 115 * len(hl) + 40 if hl else 180) + top
    lines = [HEADER.format(w=W, h=H, font=font, part_v=part_v, hook_v=170 + top)]
    if hl and hook_in_ass:
        end = min(hook_seconds, dur) if hook_seconds > 0 else dur
        lines.append(f"Dialogue: 2,{ts(0)},{ts(end)},Hook,,0,0,0,,{NL.join(_escape(x) for x in hl)}")
    if part_label:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(dur)},Part,,0,0,0,,{_escape(part_label)}")
    groups = group_words(words_in(segments, a, b))
    for gi, g in enumerate(groups):
        g_end = g[-1]["end"]
        if gi + 1 < len(groups):
            g_end = min(max(g_end, groups[gi + 1][0]["start"]), g_end + 0.5)
        for i, w in enumerate(g):
            start = w["start"] if i else g[0]["start"]
            end = g[i + 1]["start"] if i + 1 < len(g) else g_end
            if end <= start:
                end = start + 0.05
            text = " ".join(
                (HIGHLIGHT + _escape(x["word"]) + RESET) if j == i else _escape(x["word"]) for j, x in enumerate(g)
            )
            lines.append(f"Dialogue: 0,{ts(start)},{ts(min(end, dur))},Sub,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"
