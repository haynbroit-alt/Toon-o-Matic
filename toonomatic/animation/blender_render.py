"""Lance Blender en headless (une instance par scène, N en parallèle)."""
from __future__ import annotations

import glob
import json
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import ffmpeg
from . import plates

log = logging.getLogger(__name__)
SCRIPT = Path(__file__).with_name("blender_scene.py")


class BlenderError(RuntimeError):
    pass


def render_scene(shotlist_path: Path, out: Path, bcfg: dict, fps: int, scale: int = 100) -> Path:
    exe = bcfg.get("executable", "blender")
    if not shutil.which(exe) and not Path(exe).exists():
        raise BlenderError(f"Blender introuvable ({exe}). Installez Blender ≥ 4.2 ou utilisez renderer: preview")
    on_twos = bool(bcfg.get("animate_on_twos"))
    frames = out.with_name(out.stem + "_frames")
    cmd = [exe, "-b", "--factory-startup", "--python", str(SCRIPT), "--",
           "--shotlist", str(shotlist_path), "--out", str(frames),
           "--engine", bcfg.get("engine", "BLENDER_EEVEE_NEXT"),
           "--samples", str(bcfg.get("samples", 16)), "--scale", str(scale)]
    if on_twos:
        cmd += ["--fps", str(fps // 2)]
    log.info("blender %s", shotlist_path.name)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    produced = sorted(glob.glob(str(frames / "frame_*.png")))
    if proc.returncode != 0 or "TOONOMATIC_RENDER_OK" not in proc.stdout or not produced:
        raise BlenderError(f"rendu échoué pour {shotlist_path.name}:\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    # Normalise (fps final, pixel format) — et double les images si animation "en 2".
    sl = json.loads(Path(shotlist_path).read_text(encoding="utf-8"))
    base_fps = int(sl.get("fps") or fps)  # la shot list fait foi : c'est la cadence rendue par Blender
    src_fps = base_fps // 2 if on_twos else base_fps
    if sl.get("background") == "plate":  # personnages (RGBA) incrustés sur la plaque 2D recadrée par plan
        plates.composite(frames / "frame_%04d.png", src_fps, sl, out, fps)
    else:
        ffmpeg.run(["-framerate", src_fps, "-i", frames / "frame_%04d.png", "-vf", f"fps={fps},format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", out])
    shutil.rmtree(frames, ignore_errors=True)
    return out


def run_still(shot: dict, out_dir: Path, bcfg: dict, frame: int = 1, samples: int = 8,
              engine: str | None = None) -> Path:
    """Rend UNE image d'une shot list (fond transparent si background=plate). Utilise Blender,
    ou à défaut le module Python `bpy` (pip install bpy) — pratique sur un serveur sans Blender."""
    import importlib.util
    import sys
    out_dir.mkdir(parents=True, exist_ok=True)
    sl = out_dir / "still.json"
    sl.write_text(json.dumps(shot), encoding="utf-8")
    frames = out_dir / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    exe = bcfg.get("executable", "blender")
    args = ["--shotlist", str(sl), "--out", str(frames), "--frame", str(frame), "--samples", str(samples),
            "--engine", engine or bcfg.get("engine", "BLENDER_EEVEE_NEXT")]
    if shutil.which(exe) or Path(exe).exists():
        cmd = [exe, "-b", "--factory-startup", "--python", str(SCRIPT), "--", *args]
    elif importlib.util.find_spec("bpy"):
        cmd = [sys.executable, str(SCRIPT), "--", *args]
    else:
        raise BlenderError("ni Blender ni le module bpy : impossible de rendre les personnages")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    got = sorted(frames.glob("frame_*.png"))
    if "TOONOMATIC_RENDER_OK" not in proc.stdout or not got:
        raise BlenderError(f"rendu fixe échoué :\n{proc.stdout[-1200:]}\n{proc.stderr[-1200:]}")
    return got[-1]


def render_many(jobs: list[tuple[Path, Path]], bcfg: dict, fps: int) -> list[Path]:
    with ThreadPoolExecutor(max_workers=max(1, int(bcfg.get("parallel_jobs", 1)))) as pool:
        return list(pool.map(lambda j: render_scene(j[0], j[1], bcfg, fps), jobs))
