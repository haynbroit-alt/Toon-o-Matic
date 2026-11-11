"""Sous-titres SRT (fichier séparé) et ASS (stylisés, incrustés) à partir des timings réels."""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from ..voice.tts import strip_tags


@dataclass
class Cue:
    start: float
    end: float
    text: str
    speaker: str = ""


def _ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def make_cues(lines, texts: list[str] | None = None, max_chars: int = 42) -> list[Cue]:
    """Une réplique → 1..n cues de 2 lignes max, durée répartie au prorata des caractères."""
    cues: list[Cue] = []
    for i, ln in enumerate(lines):
        txt = strip_tags(texts[i] if texts else ln.text)
        if not txt:
            continue
        rows = textwrap.wrap(txt, max_chars) or [txt]
        blocks = ["\n".join(rows[j:j + 2]) for j in range(0, len(rows), 2)]
        total = sum(len(b) for b in blocks)
        t = ln.start
        for b in blocks:
            d = (ln.end - ln.start) * len(b) / total
            cues.append(Cue(round(t, 3), round(t + d, 3), b, ln.character))
            t += d
    return cues


def write_srt(cues: list[Cue], out: Path, offset: float = 0.0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{i}\n{_ts_srt(c.start - offset)} --> {_ts_srt(c.end - offset)}\n{c.text}\n"
                     for i, c in enumerate(cues, 1))
    out.write_text(body, encoding="utf-8")
    return out


def write_ass(cues: list[Cue], out: Path, style: dict, play_res=(1920, 1080), offset: float = 0.0,
              speaker_colors: dict[str, str] | None = None) -> Path:
    w, h = play_res
    size = int(style.get("size", 54) * h / 1080) if w > h else int(style.get("size", 54) * 1.4)
    margin_v = int(h * (0.06 if w > h else 0.22))
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style.get('font', 'DejaVu Sans')},{size},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,{style.get('outline', 3)},1,2,60,60,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    for c in cues:
        s, e = c.start - offset, c.end - offset
        if e <= 0:
            continue
        color = (speaker_colors or {}).get(c.speaker)
        prefix = ""
        if color:  # liseré coloré par personnage (ASS = &HBBGGRR)
            hx = color.lstrip("#").upper()
            prefix = "{\\3c&H%s%s%s&}" % (hx[4:6], hx[2:4], hx[0:2])
        txt = c.text.replace("\n", "\\N")
        ev.append(f"Dialogue: 0,{_ts_ass(max(0, s))},{_ts_ass(e)},Default,{c.speaker},0,0,0,,{prefix}{txt}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n".join(ev) + "\n", encoding="utf-8")
    return out
