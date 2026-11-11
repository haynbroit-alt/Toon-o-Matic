"""Rendu "animatique" sans Blender : fonds colorés par décor, avatars en pastilles,
locuteur mis en avant, action/émotion affichées, et "bouche" animée par l'enveloppe audio.

Sert (1) à tester la chaîne complète sans GPU, (2) de pré-visualisation rapide envoyée en
revue avant de lancer un rendu 3D coûteux.
"""
from __future__ import annotations

from pathlib import Path

from .. import ffmpeg


def _hex(c: str) -> str:
    return "0x" + c.lstrip("#")


def render_scene(shot: dict, out: Path, font: str | None = None) -> Path:
    w, h = shot["resolution"]
    fps, dur = shot["fps"], max(0.5, shot["duration"])
    chars = shot["characters"]
    n = max(1, len(chars))
    slot_x = {c["id"]: int(w * (i + 1) / (n + 1)) for i, c in enumerate(chars)}
    color = {c["id"]: c["color"] for c in chars}
    disp = {c["id"]: c["display"] for c in chars}
    fnt = f"fontfile={font}:" if font else ""
    f: list[str] = []
    label = ffmpeg.escape_drawtext(f"{shot['scene_id']} · {shot['set']['label']}")
    f.append(f"drawtext={fnt}text='{label}':x=40:y=40:fontsize=42:fontcolor=white@0.85")
    # Avatars (corps) toujours visibles
    bw, bh = int(w * 0.12), int(h * 0.42)
    for cid, x in slot_x.items():
        f.append(f"drawbox=x={x - bw // 2}:y={h - bh - 60}:w={bw}:h={bh}:color={_hex(color[cid])}@0.55:t=fill")
        f.append(f"drawtext={fnt}text='{ffmpeg.escape_drawtext(disp[cid])}':x={x}-tw/2:y={h - 50}:"
                 f"fontsize=34:fontcolor=white")
    # Performances : surbrillance du locuteur + bouche (lip-sync quantifié) + action/émotion
    for p in shot["performances"]:
        x = slot_x.get(p["character"], w // 2)
        a, b = p["start"], p["end"]
        en = f"between(t,{a:.2f},{b:.2f})"
        f.append(f"drawbox=x={x - bw // 2}:y={h - bh - 60}:w={bw}:h={bh}:color=white@0.9:t=6:enable='{en}'")
        info = ffmpeg.escape_drawtext(f"{p['action']} · {p['emotion']}")
        f.append(f"drawtext={fnt}text='{info}':x={x}-tw/2:y={h - bh - 120}:fontsize=30:"
                 f"fontcolor=yellow:enable='{en}'")
        env = p.get("envelope") or []
        step = max(1, fps // 6)  # 6 positions de bouche / seconde suffisent pour l'animatique
        for k in range(0, len(env), step):
            v = max(env[k:k + step])
            if v < 0.15:
                continue
            t0 = a + k / fps
            t1 = min(b, t0 + step / fps)
            mh = int(8 + 50 * v)
            f.append(f"drawbox=x={x - 30}:y={h - bh - 10 - mh // 2}:w=60:h={mh}:color=black@0.8:t=fill:"
                     f"enable='between(t,{t0:.2f},{t1:.2f})'")
    # Indicateur de plan caméra
    for s in shot["shots"]:
        cam = ffmpeg.escape_drawtext("CAM " + s["camera"])
        f.append(f"drawtext={fnt}text='{cam}':x=w-tw-40:y=40:fontsize=30:fontcolor=white@0.6:"
                 f"enable='between(t,{s['start']:.2f},{s['end']:.2f})'")
    script = out.with_suffix(".filter.txt")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(",\n".join(f), encoding="utf-8")
    ffmpeg.run(["-f", "lavfi", "-i", f"color=c={_hex(shot['set']['color'])}:s={w}x{h}:r={fps}:d={dur:.3f}",
                "-filter_script:v", script, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-an", out])
    script.unlink(missing_ok=True)
    return out
