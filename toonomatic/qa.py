"""Contrôle qualité automatique → score de risque (0 = sûr, 1 = à bloquer).

SIMULER/ÉVALUER avant d'EXÉCUTER : un risque faible peut être publié automatiquement
(si `qa.always_human_review: false`), un risque élevé passe obligatoirement en revue humaine.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import ffmpeg
from .config import Config
from .models import Script
from .writing.llm import LLM, LLMError

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["risk", "issues", "notes"],
    "properties": {
        "risk": {"type": "number", "description": "0 = sans risque, 1 = impubliable"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}
REVIEW_SYSTEM = (
    "Tu es responsable conformité d'une web-série d'humour publiée sur YouTube/TikTok/Instagram. "
    "Évalue le script : diffamation ou mise en scène de personnes réelles, discours haineux ou "
    "stéréotypes dégradants, contenu sexuel, violence réaliste, danger pour mineurs, marques déposées "
    "dénigrées, violation des règles des plateformes. L'humour caustique ou absurde est acceptable. "
    "Donne un risque entre 0 et 1 et la liste précise des problèmes (scène + réplique).")


def black_ratio(video: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf",
                           "scale=160:-2,blackdetect=d=0.5:pix_th=0.05", "-an", "-f", "null", "-"],
                          capture_output=True, text=True)
    total = sum(float(m) for m in re.findall(r"black_duration:([\d.]+)", proc.stderr))
    return total / max(1e-6, ffmpeg.duration(video))


def evaluate(cfg: Config, llm: LLM, scripts: dict[str, Script], finals: dict[str, Path]) -> dict:
    ep, q = cfg["episode"], cfg["qa"]
    checks: list[dict] = []
    risk = 0.0
    for lang, video in finals.items():
        dur = ffmpeg.duration(video) / 60
        ok = ep["min_minutes"] * 0.9 <= dur <= ep["max_minutes"] * 1.1
        checks.append({"lang": lang, "check": "durée", "value": round(dur, 2), "ok": ok})
        if not ok:
            risk = max(risk, 0.4)
        br = black_ratio(video)
        checks.append({"lang": lang, "check": "images noires", "value": round(br, 3), "ok": br < 0.05})
        if br >= 0.05:
            risk = max(risk, 0.6)
        s = scripts[lang]
        empty = sum(1 for ln in s.lines if not ln.audio or ln.end <= ln.start)
        checks.append({"lang": lang, "check": "répliques sans audio", "value": empty, "ok": empty == 0})
        if empty:
            risk = max(risk, 0.7)
    content: dict = {}
    if q.get("llm_content_review", True):
        for lang, s in scripts.items():
            text = "\n".join(f"[{sc.id}] {ln.character}: {ln.text}" for sc in s.scenes for ln in sc.lines)
            try:
                content[lang] = llm.generate_json("content_review", REVIEW_SYSTEM,
                                                  f"LANGUE: {lang}\nSCRIPT:\n{text}", REVIEW_SCHEMA)
            except LLMError as e:
                content[lang] = {"risk": 1.0, "issues": [f"revue de contenu impossible : {e}"], "notes": ""}
            risk = max(risk, float(content[lang]["risk"]))
    needs_human = bool(q.get("always_human_review", True)) or risk >= float(q["min_risk_for_human"])
    return {"risk": round(risk, 3), "needs_human": needs_human, "checks": checks, "content": content}


def save(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
