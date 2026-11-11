"""Synchronisation labiale par VISÈMES.

- `rhubarb` : Rhubarb Lip Sync (open source, hors ligne), reconnaissance `phonetic`
  indépendante de la langue → marche en français comme en anglais. Le texte de la réplique
  est fourni (`--dialogFile`) pour guider l'alignement.
- `envelope` : repli sans dépendance, déduit une bouche plausible de l'amplitude du son.
- `auto` : Rhubarb s'il est installé, sinon l'enveloppe.

Sortie par réplique : [{"t": secondes depuis le début de la réplique, "v": "A".."H"|"X"}, ...]
(notation Preston Blair, la même que les shape keys `viseme_*` des rigs).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .tts import strip_tags

log = logging.getLogger(__name__)
VISEMES = ["A", "B", "C", "D", "E", "F", "G", "H", "X"]


def from_envelope(env: list[float], fps: int) -> list[dict]:
    cues: list[dict] = []
    last = None
    for i, a in enumerate(env):
        v = "X" if a < 0.08 else "B" if a < 0.3 else "C" if a < 0.6 else "D"
        # fermetures brèves entre syllabes → lecture plus naturelle que "ouvert en continu"
        if v != "X" and last in ("C", "D") and a < 0.45 and i % 4 == 0:
            v = "A"
        if v != last:
            cues.append({"t": round(i / fps, 3), "v": v})
            last = v
    if not cues or cues[-1]["v"] != "X":
        cues.append({"t": round(len(env) / fps, 3), "v": "X"})
    return cues


def rhubarb(wav: Path, text: str, exe: str = "rhubarb", timeout: int = 300) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        dialog = Path(td) / "dialog.txt"
        dialog.write_text(strip_tags(text), encoding="utf-8")
        out = Path(td) / "out.json"
        proc = subprocess.run([exe, "-f", "json", "-r", "phonetic", "--dialogFile", str(dialog),
                               "-o", str(out), str(wav)], capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"rhubarb a échoué : {proc.stderr[-500:]}")
        data = json.loads(out.read_text(encoding="utf-8"))
    return [{"t": round(float(c["start"]), 3), "v": c["value"]} for c in data.get("mouthCues", [])]


class LipSync:
    def __init__(self, cfg: dict):
        self.provider = cfg.get("provider", "auto")
        self.exe = cfg.get("rhubarb_path", "rhubarb")
        if self.provider == "auto":
            self.provider = "rhubarb" if shutil.which(self.exe) else "envelope"
        log.info("lip-sync : %s", self.provider)

    def visemes(self, wav: Path, text: str, env: list[float], fps: int) -> list[dict]:
        if self.provider == "rhubarb":
            try:
                return rhubarb(wav, text, self.exe)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                log.warning("rhubarb indisponible pour %s (%s) → enveloppe", wav.name, e)
        return from_envelope(env, fps)
