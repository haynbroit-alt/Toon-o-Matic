"""Kit de publication par langue : tout ce qu'il faut pour poster sans retoucher à la main.

runs/<id>/package/<lang>/
  youtube.json      titre, description (avec CHAPITRES horodatés), tags, langue, mention IA
  thumbnail.jpg     miniature 1280x720 (image de la meilleure scène + titre incrusté)
  tiktok.json       légende ≤ 2200 caractères, hashtags, mention « contenu généré par IA »
  tiktok_cover.jpg  couverture verticale de l'extrait
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import ffmpeg
from .config import Config
from .models import Script
from .writing.llm import LLM

META_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["youtube_title", "youtube_description", "tags", "tiktok_caption", "hashtags",
                 "thumbnail_text", "chapters"],
    "properties": {
        "youtube_title": {"type": "string", "description": "≤ 70 caractères, accrocheur, sans clickbait mensonger"},
        "youtube_description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "tiktok_caption": {"type": "string", "description": "≤ 150 caractères, ton de la série"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {"type": "string", "description": "3 à 4 mots maximum"},
        "chapters": {"type": "array", "items": {"type": "string"}, "description": "un titre court par scène, dans l'ordre"},
    },
}


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def chapters(script: Script, labels: list[str], min_len: float = 10.0) -> list[tuple[float, str]]:
    """Règles YouTube : premier chapitre à 0:00, au moins 3 chapitres, chacun ≥ 10 s."""
    out: list[tuple[float, str]] = []
    for i, sc in enumerate(script.scenes):
        label = (labels[i] if i < len(labels) and labels[i].strip() else sc.summary)[:60]
        if out and sc.start - out[-1][0] < min_len:
            continue
        out.append((0.0 if not out else round(sc.start, 1), label))
    return out if len(out) >= 3 else []


def _font() -> str | None:
    if not shutil.which("fc-match"):
        return None
    r = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"], capture_output=True, text=True)
    return r.stdout.strip() or None


def thumbnail(video: Path, at: float, text: str, out: Path, size=(1280, 720)) -> Path:
    w, h = size
    font = _font()
    fnt = f"fontfile='{font}':" if font else ""
    text = " ".join(text.split()[:4]).upper()   # une miniature se lit en une seconde : 4 mots max
    txt = ffmpeg.escape_drawtext(text)
    fs = int(min(h * 0.13, 0.9 * w / (0.68 * max(1, len(text)))))  # la police s'adapte à la largeur
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},eq=saturation=1.25:contrast=1.08,"
          f"drawtext={fnt}text='{txt}':fontsize={fs}:fontcolor=white:borderw={int(h * 0.012)}:"
          f"bordercolor=black:x=(w-tw)/2:y=h-th-{int(h * 0.08)}")
    ffmpeg.run(["-ss", f"{max(0.0, at):.2f}", "-i", video, "-frames:v", 1, "-vf", vf, "-q:v", 2, out])
    return out


def build_package(cfg: Config, llm: LLM, episode_id: str, lang: str, script: Script,
                  clean_video: Path, short_video: Path | None, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ser = cfg.series or {}
    series_title = (ser.get("title") or {}).get(lang, "") if isinstance(ser.get("title"), dict) else ""
    system = ("Tu es responsable de la diffusion d'une web-série animée. Tu écris les métadonnées de "
              f"publication en {lang}, dans le ton de la série, honnêtes (pas de promesse que la vidéo ne "
              "tient pas), optimisées pour la découverte. Les hashtags commencent par #.")
    scenes = "\n".join(f"{i + 1}. {s.summary}" for i, s in enumerate(script.scenes))
    user = (f"SÉRIE : {series_title}\nTITRE DE L'ÉPISODE : {script.title}\nSCÈNES :\n{scenes}\n"
            "<context>" + json.dumps({"lang": lang, "title": script.title,
                                      "logline": scenes.splitlines()[0] if scenes else ""}, ensure_ascii=False)
            + "</context>")
    meta = llm.generate_json("metadata", system, user, META_SCHEMA)
    chaps = chapters(script, meta.get("chapters", []))
    desc = meta["youtube_description"].strip()
    if chaps:
        desc += "\n\n" + "\n".join(f"{_fmt(t)} {label}" for t, label in chaps)
    disclosure = {"fr": "Série d'animation produite avec l'aide d'outils d'IA (scénario, voix, animation).",
                  "en": "Animated series produced with the help of AI tools (writing, voices, animation)."}
    desc += "\n\n" + disclosure.get(lang, disclosure["en"])
    title = meta["youtube_title"].strip()
    if series_title and series_title.lower() not in title.lower():
        title = f"{title} | {series_title}"
    yt = {"title": title[:100], "description": desc[:5000], "tags": meta["tags"][:30],
          "defaultLanguage": lang, "defaultAudioLanguage": lang, "categoryId": "23",
          "containsSyntheticMedia": True, "madeForKids": False, "chapters": [[t, c] for t, c in chaps],
          "video": str(clean_video).replace("_clean.mp4", ".mp4"),
          "subtitles": sorted(str(p) for p in clean_video.parent.glob("subs.*.srt"))}
    (out_dir / "youtube.json").write_text(json.dumps(yt, ensure_ascii=False, indent=2), encoding="utf-8")
    hl = next((s for s in script.scenes if s.highlight), script.scenes[0])
    thumbnail(clean_video, (hl.start + hl.end) / 2, meta["thumbnail_text"], out_dir / "thumbnail.jpg")

    tags = " ".join(h if h.startswith("#") else f"#{h}" for h in meta["hashtags"][:8])
    tk = {"caption": f"{meta['tiktok_caption'].strip()} {tags}"[:2200], "ai_generated": True,
          "video": str(short_video) if short_video else None, "language": lang}
    if short_video and short_video.exists():
        thumbnail(short_video, 1.0, meta["thumbnail_text"], out_dir / "tiktok_cover.jpg", size=(1080, 1920))
        tk["cover"] = str(out_dir / "tiktok_cover.jpg")
    (out_dir / "tiktok.json").write_text(json.dumps(tk, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"youtube": yt, "tiktok": tk, "dir": str(out_dir)}
