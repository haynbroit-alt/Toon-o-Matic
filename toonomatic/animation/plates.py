"""Décors en PLAQUES 2D : personnages 3D rendus sur fond transparent, incrustés sur une image.

Comme dans les références visuelles (rue pavillonnaire peinte, garage quasi photo), le décor
est une image ; chaque plan en recadre une partie :
  - plan large  : image entière ;
  - deux-plans  : léger zoom centré ;
  - gros plan   : zoom fort centré sur le personnage (le fond « grossit » avec le cadrage).

Génération des plaques manquantes : `mock` (dégradé ciel/sol, pour tester) ou `http`
(n'importe quel serveur d'images : API Automatic1111/Forge par défaut, ou une passerelle
ComfyUI / FLUX / SDXL auto-hébergée).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import requests

from .. import ffmpeg
from ..config import Config

log = logging.getLogger(__name__)

ZOOM = {"wide": 1.0, "two_shot": 1.22, "close": 1.75}


def screen_x(slot: int, n: int, lens: float, depth: float = 11.0, spread: float = 2.4) -> float:
    """Position horizontale (0..1) d'un acteur vu par la caméra large (capteur 36 mm)."""
    x = (slot - (n - 1) / 2) * spread
    half_width = depth * 18.0 / lens
    return min(0.95, max(0.05, 0.5 + x / (2 * half_width)))


def shot_framing(shot: dict, sl: dict) -> tuple[float, float, float]:
    """(zoom, centre_x, centre_y) du recadrage de la plaque pour un plan."""
    cam = shot["camera"]
    if cam.startswith("close:"):
        cid = cam.split(":", 1)[1]
        chars = sl["characters"]
        c = next((c for c in chars if c["id"] == cid), None)
        lens = float((sl.get("style") or {}).get("camera", {}).get("lens_wide", 30))
        cx = screen_x(c["slot"], len(chars), lens) if c else 0.5
        return ZOOM["close"], cx, 0.42
    return ZOOM.get(cam, 1.0), 0.5, 0.5


def background_video(sl: dict, out: Path, fps: int) -> Path:
    """Fond de la scène : un segment recadré par plan, concaténés."""
    w, h = sl["resolution"]
    plate = Path(sl["set"]["plate"])
    segs: list[Path] = []
    for i, shot in enumerate(sl["shots"] or [{"camera": "wide", "start": 0, "end": sl["duration"]}]):
        dur = max(1.0 / fps, float(shot["end"]) - float(shot["start"]))
        z, cx, cy = shot_framing(shot, sl)
        zw, zh = int(round(w * z / 2) * 2), int(round(h * z / 2) * 2)
        vf = (f"scale={zw}:{zh}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h}:'min(max(0,{cx:.4f}*iw-{w}/2),iw-{w})':'min(max(0,{cy:.4f}*ih-{h}/2),ih-{h})',"
              f"fps={fps},format=yuv420p")
        seg = out.with_name(f"{out.stem}_bg{i:03d}.mp4")
        ffmpeg.run(["-loop", 1, "-t", f"{dur:.3f}", "-i", plate, "-vf", vf, "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "18", "-an", seg])
        segs.append(seg)
    ffmpeg.concat(segs, out)
    for s in segs:
        s.unlink(missing_ok=True)
    return out


def composite(frames_pattern: Path, frames_fps: int, sl: dict, out: Path, fps: int) -> Path:
    bg = background_video(sl, out.with_name(out.stem + "_bg.mp4"), fps)
    ffmpeg.run(["-i", bg, "-framerate", frames_fps, "-i", frames_pattern,
                "-filter_complex", f"[1:v]fps={fps}[fg];[0:v][fg]overlay=format=auto:shortest=1,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", out])
    bg.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- génération
def _mock_plate(color: str, label: str, out: Path, size=(1920, 1080)) -> Path:
    w, h = size
    sky_h = int(h * 0.62)
    hexc = "0x" + color.lstrip("#")
    vf = (f"drawbox=x=0:y={sky_h}:w={w}:h={h - sky_h}:color={hexc}:t=fill,"
          f"drawbox=x=0:y={sky_h - 6}:w={w}:h=12:color=0x2e7d4f:t=fill,"
          f"drawtext=text='{ffmpeg.escape_drawtext(label)}':x=40:y={sky_h + 30}:fontsize=40:fontcolor=white@0.5")
    ffmpeg.run(["-f", "lavfi", "-i", f"gradients=s={w}x{h}:c0=0x55c8f0:c1=0xb8ecff:x0=0:y0=0:x1=0:y1={sky_h}:d=1",
                "-frames:v", 1, "-vf", vf, out])
    return out


def _http_plate(cfg: dict, prompt: str, negative: str, out: Path, size=(1920, 1080)) -> Path:
    c = cfg.get("http", {})
    url = c.get("url") or "http://localhost:7860/sdapi/v1/txt2img"
    body = {k: (v.format(prompt=prompt, negative=negative) if isinstance(v, str) else v)
            for k, v in (c.get("json") or {"prompt": "{prompt}", "negative_prompt": "{negative}",
                                             "width": 1344, "height": 768, "steps": 28}).items()}
    r = requests.post(url, json=body, timeout=int(c.get("timeout_s", 600)))
    r.raise_for_status()
    field = c.get("response_field", "images")
    data = r.json()[field] if "json" in r.headers.get("content-type", "") else r.content
    raw = base64.b64decode(data[0] if isinstance(data, list) else data) if not isinstance(data, bytes) else data
    tmp = out.with_suffix(".download")
    tmp.write_bytes(raw)
    w, h = size  # mise au format 16:9 du rendu final
    ffmpeg.run(["-i", tmp, "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}",
                "-frames:v", 1, out])
    tmp.unlink(missing_ok=True)
    return out


def ensure_plates(cfg: Config, set_ids: list[str] | None = None, force: bool = False) -> list[Path]:
    """Génère les plaques manquantes (ou toutes avec force=True). Les plaques fournies à la main sont gardées."""
    pc = cfg.get("plates", {}) or {}
    provider = pc.get("provider", "mock")
    style_prefix = (cfg.style or {}).get("plate_style", "")
    negative = pc.get("negative_prompt", "people, characters, text, watermark, logo, blurry")
    made = []
    for sid, info in cfg.cast["sets"].items():
        if set_ids and sid not in set_ids:
            continue
        if not info.get("plate"):
            continue
        out = plate_path(cfg, info)
        if out.exists() and not force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        prompt = ", ".join(x for x in (style_prefix, info.get("plate_prompt", sid)) if x)
        if provider == "http":
            _http_plate(pc, prompt, negative, out)
        elif provider == "mock":
            _mock_plate(info["color"], info.get("label_fr", sid), out)
        else:
            log.warning("plaque %s absente et plates.provider=%s : décor placeholder", sid, provider)
            continue
        log.info("plaque générée : %s", out)
        made.append(out)
    return made


def plate_path(cfg: Config, info: dict) -> Path:
    p = Path(info["plate"])
    return p if p.is_absolute() else (cfg.series_dir or cfg.root) / p
