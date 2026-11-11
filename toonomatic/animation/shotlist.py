"""Traduit un script minuté en "shot list" : la partition que suit le moteur 3D.

Pour chaque scène : décor, acteurs (rig + position), performances (action + émotion +
enveloppe de lip-sync par réplique) et découpage caméra automatique (plan large /
gros plan sur le locuteur / retour au large), selon des règles de montage simples.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..models import Scene, Script
from .plates import plate_path

STRONG = {"angry", "surprised", "scared", "sad"}


def camera_plan(scene: Scene, min_shot: float = 1.6) -> list[dict]:
    shots: list[dict] = []
    since_wide = 0
    for i, ln in enumerate(scene.lines):
        start, end = ln.start - scene.start, ln.end - scene.start
        dur = end - start
        if i == 0 or since_wide >= 4 or ln.action in ("walk_in", "walk_out", "drive"):
            cam, since_wide = "wide", 0
        elif dur > 2.5 or ln.emotion in STRONG or ln.action in ("facepalm", "shocked", "laugh"):
            cam = f"close:{ln.character}"
        else:
            cam = "two_shot"
        since_wide += 1
        if shots and (shots[-1]["camera"] == cam or end - shots[-1]["start"] < min_shot):
            shots[-1]["end"] = end  # fusionne les plans trop courts / identiques
        else:
            if shots:
                shots[-1]["end"] = start
            shots.append({"camera": cam, "start": start, "end": end, "speaker": ln.character})
    if shots:
        shots[0]["start"] = 0.0
        shots[-1]["end"] = scene.end - scene.start
    return shots


def build_shotlist(cfg: Config, script: Script, scene: Scene) -> dict:
    cast, anim = cfg.cast, cfg["animation"]
    lib = cfg.path(anim["blender"]["asset_library"])
    set_info = cast["sets"][scene.set]
    return {
        "episode_lang": script.lang,
        "scene_id": scene.id,
        "duration": round(scene.end - scene.start, 3),
        "fps": anim["fps"],
        "resolution": anim["resolution"],
        "set": {"id": scene.set, "blend": str(lib / "sets" / set_info.get("blend", "")),
                "plate": str(plate_path(cfg, set_info)) if set_info.get("plate") else "",
                "color": set_info["color"], "label": set_info.get(f"label_{script.lang}", scene.set)},
        "characters": [{"id": c, "slot": i, "display": cast["characters"][c]["display"],
                        "rig": str(lib / "characters" / cast["characters"][c]["rig"]),
                        "color": cast["characters"][c]["color"],
                        "signature": cast["characters"][c].get("signature", {})}
                       for i, c in enumerate(scene.characters)],
        "performances": [{"character": ln.character, "start": round(ln.start - scene.start, 3),
                          "end": round(ln.end - scene.start, 3), "action": ln.action,
                          "emotion": ln.emotion, "envelope": ln.envelope, "visemes": ln.visemes}
                         for ln in scene.lines],
        "style": cfg.style,
        "background": background_mode(cfg, set_info, lib),
        "shots": camera_plan(scene),
    }


def background_mode(cfg: Config, set_info: dict, lib: Path) -> str:
    """"plate" (image 2D + personnages sur fond transparent) ou "set" (décor 3D / placeholder)."""
    mode = (cfg.style or {}).get("backgrounds", "sets")
    has_plate = bool(set_info.get("plate")) and plate_path(cfg, set_info).exists()
    has_blend = bool(set_info.get("blend")) and (lib / "sets" / set_info["blend"]).exists()
    if mode == "plates" and has_plate:
        return "plate"
    if mode == "auto" and has_plate and not has_blend:
        return "plate"
    return "set"


def scene_paths(workdir: Path, lang: str, scene_id: str) -> tuple[Path, Path]:
    d = workdir / lang / "render"
    return d / f"{scene_id}.json", d / f"{scene_id}.mp4"
