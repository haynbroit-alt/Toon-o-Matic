"""PRESENCE ENGINE : une scène devient plusieurs occasions de rencontrer les personnages.

    une scène ──► scène originale 16:9 (≈ 45 s)
              ├─► réaction 9:16 (≤ 10 s, centrée sur le pic d'émotion)
              ├─► short 9:16 (30–59 s, coupé sur la chute)
              ├─► stickers (expressions de la scène)       [niveau final, moteur 3D requis]
              ├─► en FR et en EN
              └─► mémoire : la scène enrichit l'histoire de chaque personnage présent

À la demande, pas en masse : le moteur établit un PLAN chiffré pour toutes les scènes (gratuit),
mais ne fabrique que les `presence.scenes_per_episode` meilleures (ou celles qu'on lui demande).
Chaque objet reçoit un identifiant stable `<épisode>.<scène>.<type>.<langue>` qui sert à relier
plus tard les mesures d'audience à ses personnages, répliques et expressions (mémoire sociale).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import ffmpeg
from .config import Config
from .editing.assemble import make_cutdown
from .editing.subtitles import make_cues, write_ass
from .models import Script, load_json, save_json
from .stickers import score as line_score
from .voice.tts import strip_tags

log = logging.getLogger(__name__)
KINDS = ("scene", "reaction", "short")


def scene_score(scene, weights: dict | None = None) -> float:
    weights = weights or {}
    if not scene.lines:
        return 0.0
    peaks = sorted((line_score(ln, scene.highlight) + weights.get((ln.character, ln.emotion, ln.action), 0)
                    for ln in scene.lines), reverse=True)
    return round(sum(peaks[:3]) / 3 + (2 if scene.highlight else 0) + 0.5 * len(set(ln.character for ln in scene.lines)), 2)


def _peak_line(scene):
    return max(scene.lines, key=lambda ln: line_score(ln, scene.highlight))


def windows(scene, max_reaction: float = 10.0, max_short: float = 59.0) -> dict[str, tuple[float, float]]:
    """Fenêtres temporelles (secondes, sur la timeline de l'épisode) de chaque format."""
    peak = _peak_line(scene)
    r_start = max(scene.start, peak.start - 2.0)          # un temps d'installation avant le pic
    r_end = min(scene.end, max(peak.end + 0.8, r_start + 6.0))
    if r_end - r_start > max_reaction:
        r_start = r_end - max_reaction
    s_end = scene.start + max_short
    inside = [ln.end for ln in scene.lines if ln.end <= s_end]
    s_end = min(scene.end, (inside[-1] + 0.5) if inside else s_end)  # coupe propre, sur une fin de réplique
    return {"scene": (scene.start, scene.end), "reaction": (r_start, r_end), "short": (scene.start, s_end)}


def plan(cfg: Config, episode_dir: Path) -> list[dict]:
    """Plan chiffré pour TOUTES les scènes (aucun rendu) : ce qu'on pourrait fabriquer et pourquoi."""
    from .social import expression_weights
    w = expression_weights(cfg)
    lang0 = cfg.languages[0]
    script = Script.from_dict(load_json(episode_dir / lang0 / "script_timed.json"))
    out = []
    for sc in script.scenes:
        peak = _peak_line(sc)
        win = windows(sc)
        out.append({"scene": sc.id, "score": scene_score(sc, w), "summary": sc.summary,
                    "characters": sorted({ln.character for ln in sc.lines}),
                    "peak": {"character": peak.character, "emotion": peak.emotion, "action": peak.action,
                             "text": strip_tags(peak.text)},
                    "durations": {k: round(b - a, 1) for k, (a, b) in win.items()}})
    return sorted(out, key=lambda x: -x["score"])


def vertical_clip(cfg: Config, episode_dir: Path, lang: str, scene, a: float, b: float, out: Path,
                  size: tuple[int, int], ass: Path | None) -> Path:
    """Extrait vertical. Avec le théâtre de marionnettes, il est RE-TOURNÉ en 9:16, caméra sur celui
    qui parle (un simple recadrage du 16:9 couperait les personnages des côtés) ; sinon, recadrage."""
    clean = next((episode_dir / lang).glob("*_clean.mp4"))
    sl = episode_dir / lang / "render" / f"{scene.id}.json"
    if cfg.get("animation.renderer") != "puppet" or not sl.exists():
        return make_cutdown(clean, a, b, out, size=size, ass=ass)
    from .animation import puppet
    pic = out.with_name(out.stem + "_pic.mp4")
    puppet.render_scene(load_json(sl), pic, size=size, window=(a - scene.start, b - scene.start), follow_speaker=True)
    vf = f"subtitles='{ffmpeg.escape_filter_path(ass)}'" if ass else "null"
    ffmpeg.run(["-i", pic, "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", clean, "-map", "0:v", "-map", "1:a",
                "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out])
    pic.unlink(missing_ok=True)
    return out


def _clip(clean: Path, start: float, end: float, out: Path, ass: Path | None) -> Path:
    vf = f"subtitles='{ffmpeg.escape_filter_path(ass)}'" if ass else "null"
    ffmpeg.run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", clean, "-vf", vf, "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", out])
    return out


def _caption(cfg: Config, lang: str, scene, kind: str) -> str:
    peak = _peak_line(scene)
    ser = cfg.series or {}
    title = (ser.get("title") or {}).get(lang, "") if isinstance(ser.get("title"), dict) else ""
    name = cfg.cast["characters"][peak.character]["display"]
    tags = {"fr": "#animation #humour", "en": "#animation #comedy"}.get(lang, "#animation")
    text = strip_tags(peak.text)
    return f"{name} : « {text[:90]} » — {title} {tags}".strip()


def build(cfg: Config, llm, episode_dir: Path, episode_id: str, scenes: list[str] | None = None,
          with_stickers: bool = True) -> dict:
    pc = cfg.get("presence") or {}
    chosen = scenes or [p["scene"] for p in plan(cfg, episode_dir)[:int(pc.get("scenes_per_episode", 2))]]
    st = cfg["editing"]["subtitle_style"]
    colors = {k: v["color"] for k, v in cfg.cast["characters"].items()}
    res16 = tuple(cfg["animation"]["resolution"])
    vert = tuple((cfg["editing"].get("cutdown") or {}).get("resolution", [1080, 1920]))
    out_dir = episode_dir / "presence"
    assets = []
    for lang in cfg.languages:
        script = Script.from_dict(load_json(episode_dir / lang / "script_timed.json"))
        clean = episode_dir / lang / f"{episode_id}_{lang}_clean.mp4"
        if not clean.exists():
            raise FileNotFoundError(f"vidéo propre absente : {clean}")
        for sc in script.scenes:
            if sc.id not in chosen:
                continue
            win = windows(sc, float(pc.get("reaction_max_s", 10)), float(pc.get("short_max_s", 59)))
            d = out_dir / lang / sc.id
            d.mkdir(parents=True, exist_ok=True)
            for kind in KINDS:
                a, b = win[kind]
                lines = [ln for ln in sc.lines if ln.end > a and ln.start < b]
                vertical = kind != "scene"
                ass = write_ass(make_cues(lines, max_chars=24 if vertical else st["max_chars_per_line"]),
                                d / f"{kind}.ass", st, vert if vertical else res16, offset=a, speaker_colors=colors)
                mp4 = d / f"{kind}.mp4"
                if vertical:
                    vertical_clip(cfg, episode_dir, lang, sc, a, b, mp4, vert, ass)
                else:
                    _clip(clean, a, b, mp4, ass)
                peak = _peak_line(sc)
                assets.append({
                    "id": f"{episode_id}.{sc.id}.{kind}.{lang}", "episode": episode_id, "scene": sc.id,
                    "kind": kind, "lang": lang, "video": str(mp4), "start": round(a, 2), "end": round(b, 2),
                    "duration": round(b - a, 2), "summary": sc.summary,
                    "characters": sorted({ln.character for ln in lines}),
                    "expressions": [[ln.character, ln.emotion, ln.action] for ln in
                                    ([peak] if kind == "reaction" else lines)],
                    "lines": [{"character": ln.character, "text": strip_tags(ln.text)} for ln in lines],
                    "caption": _caption(cfg, lang, sc, kind)})
    manifest = {"episode": episode_id, "built": time.strftime("%Y-%m-%d %H:%M"), "scenes": chosen,
                "assets": assets, "stickers": [], "committed": False}
    save_json(manifest, out_dir / "manifest.json")
    if with_stickers:
        commit(cfg, llm, episode_dir, episode_id)
    return manifest


def commit(cfg: Config, llm, episode_dir: Path, episode_id: str) -> dict:
    """Épisode VALIDÉ : ses objets entrent au catalogue, ses scènes dans l'histoire des personnages,
    ses expressions dans les packs de stickers. Rien de tout cela pour un épisode rejeté."""
    mpath = episode_dir / "presence" / "manifest.json"
    if not mpath.exists():
        return {}
    manifest = load_json(mpath)
    if manifest.get("committed"):
        return manifest
    _remember(cfg, episode_id, episode_dir, manifest["scenes"], manifest["assets"])
    if cfg.get("stickers.enabled", True):
        from . import stickers
        from .animation.blender_render import BlenderError
        pc = cfg.get("presence") or {}
        try:
            new = stickers.extract(cfg, llm, episode_dir, episode_id,
                                   int(pc.get("stickers_per_scene", 2)) * len(manifest["scenes"]),
                                   scenes=set(manifest["scenes"]))
            manifest["stickers"] = [s["id"] for s in new]
        except BlenderError as e:
            log.warning("[%s] stickers ignorés (pas de moteur 3D) : %s", episode_id, e)
    manifest["committed"] = True
    save_json(manifest, mpath)
    return manifest


def _remember(cfg: Config, episode_id: str, episode_dir: Path, chosen: list[str], assets: list[dict]) -> None:
    """Catalogue des objets (pour la mémoire sociale) + histoire vécue de chaque personnage."""
    mem = (cfg.series_dir or cfg.root) / "memory"
    (mem / "characters").mkdir(parents=True, exist_ok=True)
    known = set()
    cat = mem / "presence_assets.jsonl"
    if cat.exists():
        known = {json.loads(x)["id"] for x in cat.read_text(encoding="utf-8").splitlines() if x.strip()}
    with cat.open("a", encoding="utf-8") as fh:
        for a in assets:
            if a["id"] not in known:
                fh.write(json.dumps({k: v for k, v in a.items() if k != "video"}, ensure_ascii=False) + "\n")
    lang0 = cfg.languages[0]
    script = Script.from_dict(load_json(episode_dir / lang0 / "script_timed.json"))
    for sc in script.scenes:
        if sc.id not in chosen:
            continue
        for cid in sorted({ln.character for ln in sc.lines}):
            p = mem / "characters" / f"{cid}.jsonl"
            if p.exists() and f'"scene_key": "{episode_id}.{sc.id}"' in p.read_text(encoding="utf-8"):
                continue
            entry = {"scene_key": f"{episode_id}.{sc.id}", "episode": episode_id, "scene": sc.id,
                     "summary": sc.summary, "with": sorted({ln.character for ln in sc.lines} - {cid}),
                     "lines": [strip_tags(ln.text) for ln in sc.lines if ln.character == cid][:5],
                     "assets": [a["id"] for a in assets if a["scene"] == sc.id and a["lang"] == lang0],
                     "date": time.strftime("%Y-%m-%d")}
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
