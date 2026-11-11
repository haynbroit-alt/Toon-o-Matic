"""CONTRÔLE DE MARQUE : un personnage-marque se reconnaît en moins d'une demi-seconde, même en
miniature sur un fil d'actualité muet.

Deux tests automatiques, rejoués à chaque changement de design :
  1. SILHOUETTES : chaque personnage est rendu en ombre chinoise, réduit à 64 px, et comparé aux
     autres (recouvrement IoU). Deux silhouettes trop proches = deux personnages confondus.
  2. COULEURS SIGNATURES : écart perceptuel (ΔE, espace Lab) entre les couleurs des personnages,
     et contre le décor de la série (un personnage ne doit pas se fondre dans le ciel).

Sortie : series/<slug>/brand/report.json + planche visuelle silhouettes.png.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from itertools import combinations
from pathlib import Path

from . import ffmpeg
from .config import Config

log = logging.getLogger(__name__)
SIZE = 64


# --------------------------------------------------------------------------- couleurs
def hex_to_lab(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047
    y = (0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])
    z = (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """ΔE CIE76 : ~2 = à peine visible, > 25 = couleurs clairement différentes."""
    la, lb = hex_to_lab(a), hex_to_lab(b)
    return round(sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5, 1)


# --------------------------------------------------------------------------- silhouettes
def alpha_mask(png: Path) -> tuple[list[list[bool]], int, int]:
    """Masque binaire (opaque = personnage) lu via FFmpeg, sans dépendance d'image."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(png)], capture_output=True, text=True,
                         check=True).stdout.strip().split(",")
    w, h = int(out[0]), int(out[1])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-vf", "alphaextract", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return [[raw[y * w + x] > 127 for x in range(w)] for y in range(h)], w, h


def normalize(mask: list[list[bool]], size: int = SIZE) -> tuple[list[list[bool]], dict]:
    """Recadre sur la silhouette et la ramène à `size` px de haut (proportions conservées)."""
    ys = [y for y, row in enumerate(mask) if any(row)]
    xs = [x for row in mask for x, v in enumerate(row) if v]
    if not ys:
        return [[False] * size for _ in range(size)], {"aspect": 0, "fill": 0, "height_px": 0}
    y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
    bh, bw = y1 - y0 + 1, x1 - x0 + 1
    scale = size / max(bh, bw)
    nw, nh = max(1, round(bw * scale)), max(1, round(bh * scale))
    ox, oy = (size - nw) // 2, size - nh  # posé au sol, centré
    out = [[False] * size for _ in range(size)]
    for j in range(nh):
        sy = y0 + min(bh - 1, int(j / scale))
        for i in range(nw):
            out[oy + j][ox + i] = mask[sy][x0 + min(bw - 1, int(i / scale))]
    fill = sum(map(sum, out)) / (nw * nh)
    return out, {"aspect": round(bw / bh, 3), "fill": round(fill, 3), "height_px": bh}


def iou(a: list[list[bool]], b: list[list[bool]]) -> float:
    inter = sum(x and y for ra, rb in zip(a, b) for x, y in zip(ra, rb))
    union = sum(x or y for ra, rb in zip(a, b) for x, y in zip(ra, rb))
    return round(inter / union, 3) if union else 1.0


def render_silhouettes(cfg: Config, out_dir: Path) -> dict[str, Path]:
    """Une image fond transparent par personnage, de face, plan large, pose neutre."""
    from .animation.blender_render import run_still
    from .animation.plates import plate_path  # noqa: F401 (assure le style « plaques »)
    cast = cfg.cast["characters"]
    lib = cfg.path(cfg["animation"]["blender"]["asset_library"])
    bc = cfg.get("brand") or {}
    res = {}
    for cid, c in cast.items():
        shot = {"episode_lang": "brand", "scene_id": f"silhouette-{cid}", "duration": 0.1, "fps": 12,
                "resolution": [int(bc.get("render_px", 512))] * 2, "background": "plate",
                "set": {"id": "none", "blend": "", "plate": "", "color": "#000000", "label": ""},
                "characters": [{"id": cid, "slot": 0, "display": c["display"], "rig": str(lib / "characters" / c["rig"]),
                                "color": c["color"], "signature": c.get("signature", {})}],
                "performances": [], "shots": [{"camera": "wide", "start": 0, "end": 0.1, "speaker": cid}],
                "style": cfg.style}
        png = run_still(shot, out_dir / "work" / cid, cfg["animation"]["blender"], frame=1,
                        samples=int(bc.get("samples", 2)), engine=bc.get("engine", "CYCLES"))
        dst = out_dir / f"{cid}.png"
        shutil.copy2(png, dst)
        res[cid] = dst
    shutil.rmtree(out_dir / "work", ignore_errors=True)
    return res


def _write_pgm(mask: list[list[bool]], path: Path, scale: int = 3) -> Path:
    h, w = len(mask), len(mask[0])
    rows = bytearray()
    for row in mask:
        line = bytearray()
        for v in row:
            line += bytes([0 if v else 255]) * scale  # silhouette noire sur blanc
        rows += bytes(line) * scale
    path.write_bytes(f"P5 {w * scale} {h * scale} 255\n".encode() + bytes(rows))
    return path


def contact_sheet(cfg: Config, masks: dict[str, list[list[bool]]], out: Path) -> Path:
    """Planche « vignette » : silhouettes normalisées 64 px (agrandies ×3) + couleurs signatures."""
    inputs, chains = [], []
    for i, (cid, m) in enumerate(masks.items()):
        c = cfg.cast["characters"][cid]
        color = (c.get("signature") or {}).get("outfit_color") or c["color"]
        inputs += ["-i", _write_pgm(m, out.with_name(f"_{cid}.pgm"))]
        label = ffmpeg.escape_drawtext(c["display"])
        chains.append(f"[{i}:v]format=rgb24,pad=232:262:20:16:white,"
                      f"drawbox=x=0:y=222:w=232:h=40:color=0x{color.lstrip('#')}:t=fill,"
                      f"drawtext=text='{label}':x=(w-tw)/2:y=232:fontsize=20:fontcolor=white[s{i}]")
    n = len(masks)
    graph = ";".join(chains) + ";" + "".join(f"[s{i}]" for i in range(n)) + f"hstack=inputs={n}"
    if n == 1:
        graph = chains[0].rsplit("[", 1)[0]
    ffmpeg.run([*inputs, "-filter_complex", graph, "-frames:v", 1, out])
    for cid in masks:
        out.with_name(f"_{cid}.pgm").unlink(missing_ok=True)
    return out


def check(cfg: Config, pngs: dict[str, Path] | None = None) -> dict:
    bc = cfg.get("brand") or {}
    max_iou = float(bc.get("max_silhouette_iou", 0.8))
    min_de = float(bc.get("min_color_delta_e", 25))
    min_bg = float(bc.get("min_background_delta_e", 15))
    out_dir = (cfg.series_dir or cfg.root) / "brand"
    out_dir.mkdir(parents=True, exist_ok=True)
    pngs = pngs or render_silhouettes(cfg, out_dir)
    masks, shapes = {}, {}
    for cid, p in pngs.items():
        m, w, h = alpha_mask(p)
        masks[cid], shapes[cid] = normalize(m)
    tallest = max(s["height_px"] for s in shapes.values()) or 1
    for s in shapes.values():
        s["relative_height"] = round(s["height_px"] / tallest, 2)
    problems = []
    pairs = []
    for a, b in combinations(masks, 2):
        v = iou(masks[a], masks[b])
        dh = abs(shapes[a]["relative_height"] - shapes[b]["relative_height"])
        # une vignette montre souvent un personnage SEUL : la taille relative ne compense pas
        same = v > max_iou
        pairs.append({"a": a, "b": b, "silhouette_iou": v, "height_gap": round(dh, 2), "confusable": same})
        if same:
            problems.append(f"silhouettes de {a} et {b} trop proches (IoU {v}, écart de taille {dh:.2f})")
    colors = {cid: (c.get("signature") or {}).get("outfit_color") or c["color"]
              for cid, c in cfg.cast["characters"].items()}
    bg = (cfg.style or {}).get("palette", {}).get("background", "#55c8f0")
    cpairs = []
    for a, b in combinations(colors, 2):
        d = delta_e(colors[a], colors[b])
        cpairs.append({"a": a, "b": b, "delta_e": d, "ok": d >= min_de})
        if d < min_de:
            problems.append(f"couleurs de {a} et {b} trop proches (ΔE {d} < {min_de})")
    for cid, col in colors.items():
        d = delta_e(col, bg)
        if d < min_bg:
            problems.append(f"{cid} se fond dans le décor (ΔE {d} avec {bg})")
    sheet = contact_sheet(cfg, masks, out_dir / "silhouettes.png")
    report = {"ok": not problems, "problems": problems, "shapes": shapes, "silhouettes": pairs,
              "colors": cpairs, "background": bg, "sheet": str(sheet)}
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
