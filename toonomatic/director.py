"""Le RÉALISATEUR AUTOMATIQUE : relit chaque épisode selon cinq critères et commande des
corrections CIBLÉES (une voix, une scène, une réplique) au lieu de tout recommencer.

    Critère       Question                               Contrôles
    histoire      Est-ce intéressant ?                   LLM : rythme, escalade, chutes (note /10)
    personnages   Sont-ils cohérents ?                   LLM vs bible + règles de continuité
    animation     Défauts visibles ?                     FFmpeg : images figées, images noires, par scène
    son           Voix compréhensibles ?                 par réplique : silence, saturation, débit
    publication   Prêt à publier ?                       titre, miniature, chapitres, légende TikTok

Le dernier mot reste humain : le réalisateur corrige, puis l'épisode part en revue.
"""
from __future__ import annotations

import array
import json
import logging
import re
import subprocess
import wave
from pathlib import Path

from .config import Config
from .models import Script, load_json
from .voice.tts import strip_tags
from .writing.llm import LLM, LLMError

log = logging.getLogger(__name__)
CRITERIA = ["histoire", "personnages", "animation", "son", "publication"]

RUBRIC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["histoire", "personnages", "notes", "fixes"],
    "properties": {
        "histoire": {"type": "integer", "description": "0-10 : intérêt, escalade, chutes, rythme"},
        "personnages": {"type": "integer", "description": "0-10 : fidélité aux fiches et à la continuité"},
        "notes": {"type": "string"},
        "fixes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["line", "criterion", "instruction"],
            "properties": {
                "line": {"type": "integer", "description": "numéro de réplique (#) à réécrire"},
                "criterion": {"type": "string", "enum": ["histoire", "personnages"]},
                "instruction": {"type": "string", "description": "consigne de réécriture précise"},
            }}},
    },
}
REWRITE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text"],
                  "properties": {"text": {"type": "string"}}}


# --------------------------------------------------------------------------- contrôles techniques
def wav_stats(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        data = array.array("h")
        data.frombytes(w.readframes(w.getnframes()))
        sr = w.getframerate()
    if not data:
        return {"seconds": 0.0, "peak": 0.0, "clip_ratio": 0.0}
    peak = max(abs(min(data)), max(data)) / 32768
    clip = sum(1 for x in data[::4] if abs(x) >= 32700) / max(1, len(data[::4]))
    return {"seconds": len(data) / sr, "peak": round(peak, 4), "clip_ratio": round(clip, 5)}


def frozen_seconds(video: Path, min_freeze: float = 2.5) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf",
                           f"scale=160:-2,freezedetect=n=0.002:d={min_freeze}", "-an", "-f", "null", "-"],
                          capture_output=True, text=True)
    return sum(float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", proc.stderr))


def black_seconds(video: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf",
                           "scale=160:-2,blackdetect=d=0.4:pix_th=0.05", "-an", "-f", "null", "-"],
                          capture_output=True, text=True)
    return sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", proc.stderr))


def check_sound(cfg: Config, script: Script) -> tuple[float, list[dict]]:
    max_cps = float(cfg.get("director.max_chars_per_second", 19))
    fixes, bad = [], 0
    for i, ln in enumerate(script.lines):
        if not ln.audio or not Path(ln.audio).exists():
            fixes.append({"action": "regen_voice", "line": i, "why": "audio manquant"})
            bad += 1
            continue
        st = wav_stats(Path(ln.audio))
        chars = len(strip_tags(ln.text))
        why = None
        if st["peak"] < 0.02:
            why = "voix quasi silencieuse"
        elif st["clip_ratio"] > 0.002:
            why = "saturation"
        elif st["seconds"] > 0 and chars / st["seconds"] > max_cps:
            why = f"débit trop rapide ({chars / st['seconds']:.0f} car./s)"
        elif chars > 0 and st["seconds"] > max(4.0, chars / 3):
            why = "réplique anormalement longue (hallucination du moteur vocal ?)"
        if why:
            fixes.append({"action": "regen_voice", "line": i, "why": why})
            bad += 1
    score = 10 * (1 - bad / max(1, len(script.lines)))
    return round(score, 1), fixes


def check_animation(ep_dir: Path, lang: str, script: Script, real_animation: bool = True) -> tuple[float, list[dict]]:
    """`real_animation=False` (animatique du brouillon) : l'image est volontairement presque fixe,
    on ne contrôle alors que la présence du rendu et les images noires."""
    fixes = []
    for sc in script.scenes:
        mp4 = ep_dir / lang / "render" / f"{sc.id}.mp4"
        if not mp4.exists():
            fixes.append({"action": "rerender_scene", "scene": sc.id, "why": "rendu manquant"})
            continue
        dur = max(0.1, sc.end - sc.start)
        blk = black_seconds(mp4)
        frz = frozen_seconds(mp4) if real_animation else 0.0
        if blk > 0.5:
            fixes.append({"action": "rerender_scene", "scene": sc.id, "why": f"{blk:.1f} s d'images noires"})
        elif frz > 0.6 * dur and dur > 6:
            fixes.append({"action": "rerender_scene", "scene": sc.id, "why": f"image figée {frz:.1f} s"})
    score = 10 * (1 - len(fixes) / max(1, len(script.scenes)))
    return round(score, 1), fixes


def check_publication(cfg: Config, ep_dir: Path, lang: str) -> tuple[float, list[dict]]:
    if not (cfg.get("package.enabled", True)):
        return 10.0, []  # niveaux brouillon/validation : pas de kit de publication attendu
    pkg = ep_dir / "package" / lang
    problems = []
    yt = load_json(pkg / "youtube.json") if (pkg / "youtube.json").exists() else None
    tk = load_json(pkg / "tiktok.json") if (pkg / "tiktok.json").exists() else None
    if not yt:
        problems.append("youtube.json absent")
    else:
        if not (5 <= len(yt.get("title", "")) <= 100):
            problems.append("titre YouTube hors limites")
        if not (pkg / "thumbnail.jpg").exists():
            problems.append("miniature absente")
        if not yt.get("containsSyntheticMedia"):
            problems.append("mention de contenu IA manquante")
    if not tk or len(tk.get("caption", "")) > 2200:
        problems.append("légende TikTok absente ou trop longue")
    fixes = [{"action": "regen_package", "lang": lang, "why": "; ".join(problems)}] if problems else []
    return (10.0 if not problems else max(0.0, 10 - 3 * len(problems))), fixes


# --------------------------------------------------------------------------- critique éditoriale
def critique(cfg: Config, llm: LLM, script: Script, bible: str) -> dict:
    numbered = "\n".join(f"#{i} [{sc.id}] {ln.character} ({ln.emotion}): {ln.text}"
                         for i, (sc, ln) in enumerate((sc, ln) for sc in script.scenes for ln in sc.lines))
    system = ("Tu es le réalisateur d'une série animée comique. Tu notes un script sans complaisance et tu "
              "proposes AU PLUS 5 corrections ciblées de répliques précises (numéro #), seulement si elles "
              "améliorent nettement l'épisode. Pas de réécriture globale.\n\n" + bible)
    user = f"LANGUE: {script.lang}\nTITRE: {script.title}\nSCRIPT:\n{numbered}\n<context>{{}}</context>"
    try:
        return llm.generate_json("director_review", system, user, RUBRIC_SCHEMA)
    except LLMError as e:
        log.warning("critique éditoriale impossible : %s", e)
        return {"histoire": 5, "personnages": 5, "notes": f"critique indisponible : {e}", "fixes": []}


def rewrite_line(llm: LLM, script: Script, idx: int, instruction: str, bible: str) -> str:
    lines = script.lines
    ctx = "\n".join(f"{'>>' if j == idx else '  '} {lines[j].character}: {lines[j].text}"
                    for j in range(max(0, idx - 4), min(len(lines), idx + 4)))
    system = "Tu es scénariste. Tu réécris UNE réplique, même personnage, même langue, longueur proche.\n\n" + bible
    user = (f"LANGUE: {script.lang}\nCONTEXTE (la réplique à réécrire est marquée >>):\n{ctx}\n"
            f"CONSIGNE: {instruction}\n<context>" + json.dumps({"text": lines[idx].text, "lang": script.lang},
                                                                ensure_ascii=False) + "</context>")
    return llm.generate_json("rewrite_line", system, user, REWRITE_SCHEMA)["text"].strip() or lines[idx].text


NOT_RATED = ("Scénario importé du bureau d'écriture, aucun modèle branché : histoire et personnages ne sont "
             "pas notés automatiquement. À vous de juger.")


def review(cfg: Config, llm: LLM, ep_dir: Path, bible: dict[str, str], editorial: bool = True) -> dict:
    """Rapport complet : note par critère et liste de corrections ciblées par langue."""
    min_score = float(cfg.get("director.min_score", 6))
    report = {"languages": {}, "scores": {}, "fixes": []}
    for lang in cfg.languages:
        script = Script.from_dict(load_json(ep_dir / lang / "script_timed.json"))
        crit = critique(cfg, llm, script, bible.get(lang, "")) if editorial else \
            {"histoire": None, "personnages": None, "notes": NOT_RATED, "fixes": []}
        s_sound, f_sound = check_sound(cfg, script)
        s_anim, f_anim = check_animation(ep_dir, lang, script,
                                         real_animation=cfg.get("animation.renderer") in ("blender", "puppet"))
        s_pub, f_pub = check_publication(cfg, ep_dir, lang)
        scores = {"histoire": crit["histoire"], "personnages": crit["personnages"],
                  "animation": s_anim, "son": s_sound, "publication": s_pub}
        fixes = [{**f, "lang": lang} for f in f_sound + f_anim + f_pub]
        for f in crit.get("fixes", []):
            if scores[f["criterion"]] < min_score and 0 <= f["line"] < len(script.lines):
                fixes.append({"action": "rewrite_line", "lang": lang, "line": f["line"],
                              "instruction": f["instruction"], "why": f"{f['criterion']} {scores[f['criterion']]}/10"})
        report["languages"][lang] = {"scores": scores, "notes": crit.get("notes", "")}
        report["fixes"] += fixes
    for c in CRITERIA:
        vals = [report["languages"][l]["scores"][c] for l in cfg.languages]
        report["scores"][c] = None if any(v is None for v in vals) else round(min(vals), 1)
    rated = [v for v in report["scores"].values() if v is not None]
    report["verdict"] = "ok" if all(v >= min_score for v in rated) else "à corriger"
    if len(rated) < len(CRITERIA):
        report["verdict"] += " (technique seulement)"
    return report
