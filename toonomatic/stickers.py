"""EXTRACTEUR DE STICKERS : les personnages entrent dans les conversations privées.

Chaque épisode validé ajoute ses meilleures expressions à la bibliothèque de la série, puis les
packs WhatsApp / Telegram sont reconstruits par langue :

    series/<slug>/stickers/library/index.jsonl        toutes les expressions extraites
    series/<slug>/stickers/library/<id>_<lang>.png    sticker avec légende (PNG 512 px, transparent)
    series/<slug>/stickers/packs/<lang>/
        01.webp … 30.webp   WhatsApp : 512×512, WebP, < 100 Ko, marge 16 px
        01.png  …           Telegram  : PNG 512 px
        tray.png            icône du pack 96×96
        contents.json       métadonnées au format de l'application d'exemple WhatsApp

Choix des moments : émotions fortes, gestes « mèmes » (facepalm, choc, rire, haussement
d'épaules…), répliques courtes, bonus pour la scène phare ; un seul sticker par combinaison
personnage × émotion × geste pour garder un pack varié.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path

from . import ffmpeg
from .brand import alpha_mask
from .config import Config
from .models import Script, load_json
from .voice.tts import strip_tags
from .writing.llm import LLM, LLMError

log = logging.getLogger(__name__)
CANVAS, MARGIN = 512, 16
MAX_PACK = 30          # limite WhatsApp par pack
MAX_WEBP_BYTES = 100_000

EMOTION_W = {"angry": 3, "surprised": 3, "smug": 3, "sarcastic": 2.5, "scared": 2.5, "happy": 2, "sad": 2,
             "neutral": 0}
ACTION_W = {"facepalm": 4, "shocked": 4, "laugh": 3.5, "shrug": 3, "point": 2.5, "angry": 3, "talk_hands": 1.5,
            "phone": 1, "talk": 0, "idle": 0}
EMOJI = {"angry": ["😠"], "surprised": ["😲"], "smug": ["😏"], "sarcastic": ["🙄"], "scared": ["😱"],
         "happy": ["😂"], "sad": ["😢"], "neutral": ["😐"]}
MOUTH = {"angry": "D", "surprised": "E", "scared": "E", "happy": "D", "smug": "A", "sarcastic": "B",
         "sad": "F", "neutral": "X"}

CAPTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["captions"],
                  "properties": {"captions": {"type": "array", "items": {"type": "string"}}}}


def score(line, highlight: bool) -> float:
    words = len(strip_tags(line.text).split())
    brevity = 2.0 if words <= 6 else 1.0 if words <= 10 else 0.0
    return EMOTION_W.get(line.emotion, 1) + ACTION_W.get(line.action, 1) + brevity + (1.5 if highlight else 0)


def select_moments(script: Script, n: int = 6, seen: set | None = None, scenes: set | None = None,
                   weights: dict | None = None) -> list[dict]:
    """`scenes` : limiter à certaines scènes ; `weights` : bonus appris de la mémoire sociale
    pour une combinaison (personnage, émotion, geste)."""
    seen = set(seen or ())
    weights = weights or {}
    cands = []
    for sc in script.scenes:
        if scenes and sc.id not in scenes:
            continue
        for ln in sc.lines:
            bonus = weights.get((ln.character, ln.emotion, ln.action), 0.0)
            cands.append((score(ln, sc.highlight) + bonus, sc.id, ln))
    out = []
    for s, scene_id, ln in sorted(cands, key=lambda c: -c[0]):
        key = (ln.character, ln.emotion, ln.action)
        if key in seen or s < 3:
            continue
        seen.add(key)
        out.append({"character": ln.character, "emotion": ln.emotion, "action": ln.action,
                    "line": strip_tags(ln.text), "scene": scene_id, "score": round(s, 1)})
        if len(out) >= n:
            break
    return out


def captions(llm: LLM, lang: str, moments: list[dict]) -> list[str]:
    """Légendes ≤ 4 mots, réutilisables comme réaction en conversation (pas un résumé du gag)."""
    system = ("Tu écris des légendes de stickers de messagerie pour une série comique. Chaque légende fait 1 à 4 "
              "mots, se comprend hors contexte, et sert de réaction dans une conversation (« Non. », « J'ai un plan », "
              "« C'est une opportunité »). Pas de nom propre de personne réelle.")
    user = f"LANGUE : {lang}\n" + "\n".join(f"{i + 1}. {m['character']} ({m['emotion']}, {m['action']}) : « {m['line']} »"
                                             for i, m in enumerate(moments)) + \
        "\n<context>" + json.dumps({"lang": lang, "lines": [m["line"] for m in moments]}, ensure_ascii=False) + "</context>"
    try:
        caps = llm.generate_json("sticker_caption", system, user, CAPTION_SCHEMA)["captions"]
    except LLMError as e:
        log.warning("légendes de stickers indisponibles : %s", e)
        caps = []
    fallback = [" ".join(m["line"].split()[:4]).rstrip(",;:") or {"fr": "Non.", "en": "No."}.get(lang, "…")
                for m in moments]
    return [(c or f).strip()[:32] for c, f in zip(list(caps) + [""] * len(moments), fallback)]


# --------------------------------------------------------------------------- rendu
def render_pose(cfg: Config, character: str, emotion: str, action: str, work: Path) -> Path:
    """Le personnage seul, en gros plan, fond transparent, au sommet de son geste."""
    from .animation.blender_render import run_still
    c = cfg.cast["characters"][character]
    lib = cfg.path(cfg["animation"]["blender"]["asset_library"])
    sc = cfg.get("stickers") or {}
    shot = {"episode_lang": "stickers", "scene_id": f"{character}-{emotion}-{action}", "duration": 1.0, "fps": 12,
            "resolution": [int(sc.get("render_px", 768))] * 2, "background": "plate",
            "set": {"id": "none", "blend": "", "plate": "", "color": "#000000", "label": ""},
            "characters": [{"id": character, "slot": 0, "display": c["display"],
                            "rig": str(lib / "characters" / c["rig"]), "color": c["color"],
                            "signature": c.get("signature", {})}],
            "performances": [{"character": character, "start": 0.0, "end": 1.0, "action": action, "emotion": emotion,
                              "envelope": [], "visemes": [{"t": 0.0, "v": MOUTH.get(emotion, "C")}]}],
            "shots": [{"camera": f"close:{character}", "start": 0, "end": 1.0, "speaker": character}],
            "style": {**(cfg.style or {}), "acting": {**(cfg.style or {}).get("acting", {}), "breathing": 0,
                                                      "blink_every_s": [99, 99]}}}
    # image 4 : le geste procédural atteint son extrémité (anticipation + pose forte)
    return run_still(shot, work, cfg["animation"]["blender"], frame=4, samples=int(sc.get("samples", 8)),
                     engine=sc.get("engine", "CYCLES"))


def _bbox(png: Path) -> tuple[int, int, int, int]:
    mask, w, h = alpha_mask(png)
    ys = [y for y, row in enumerate(mask) if any(row)]
    xs = [x for row in mask for x, v in enumerate(row) if v]
    if not ys:
        return 0, 0, w, h
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def compose(pose_png: Path, caption: str, out_png: Path, outline_px: int = 8) -> Path:
    """Recadrage sur le personnage, contour blanc « sticker », légende, marge de 16 px."""
    x, y, w, h = _bbox(pose_png)
    inner = CANVAS - 2 * MARGIN - 2 * outline_px
    cap_h = 92 if caption else 0
    font_px = max(30, min(62, int(1.6 * inner / max(6, len(caption))))) if caption else 0
    dil = ",".join(["dilation"] * outline_px)
    txt = ffmpeg.escape_drawtext(caption.upper())
    graph = (f"[0:v]crop={w}:{h}:{x}:{y},scale={inner}:{inner - cap_h}:force_original_aspect_ratio=decrease,"
             f"format=rgba,pad={CANVAS}:{CANVAS}:(ow-iw)/2:{MARGIN + outline_px}:color=black@0,split[fig][m];"
             f"[m]alphaextract,{dil}[a];"
             f"color=white:s={CANVAS}x{CANVAS},format=rgba[w];[w][a]alphamerge[halo];"
             f"[halo][fig]overlay=format=auto[st]")
    if caption:
        graph += (f";[st]drawtext=text='{txt}':fontsize={font_px}:fontcolor=white:borderw=7:bordercolor=black:"
                  f"x=(w-tw)/2:y=h-th-{MARGIN + 6}")
    else:
        graph = graph.replace("[st]", "")
    ffmpeg.run(["-i", pose_png, "-filter_complex", graph, "-frames:v", 1, out_png])
    return out_png


def to_webp(png: Path, out: Path) -> Path:
    for q in (82, 70, 55, 40):  # WhatsApp refuse au-delà de 100 Ko
        ffmpeg.run(["-i", png, "-c:v", "libwebp", "-lossless", 0, "-q:v", q, "-frames:v", 1, out])
        if out.stat().st_size <= MAX_WEBP_BYTES:
            break
    return out


# --------------------------------------------------------------------------- bibliothèque et packs
def _lib(cfg: Config) -> Path:
    return (cfg.series_dir or cfg.root) / "stickers" / "library"


def load_index(cfg: Config) -> list[dict]:
    p = _lib(cfg) / "index.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def extract(cfg: Config, llm: LLM, episode_dir: Path, episode_id: str, n: int = 6,
            scenes: set | None = None) -> list[dict]:
    lib = _lib(cfg)
    lib.mkdir(parents=True, exist_ok=True)
    index = load_index(cfg)
    seen = {(e["character"], e["emotion"], e["action"]) for e in index}
    langs = cfg.languages
    base = Script.from_dict(load_json(episode_dir / langs[0] / "script_timed.json"))
    from .social import expression_weights
    moments = select_moments(base, n, seen, scenes, expression_weights(cfg))
    if not moments:
        log.info("[%s] aucune nouvelle expression pour les stickers", episode_id)
        return []
    caps = {}
    for lang in langs:
        script = Script.from_dict(load_json(episode_dir / lang / "script_timed.json"))
        # même personnage × émotion × geste, mais la réplique et la légende viennent de SA langue
        local = []
        for m in moments:
            same = [x for x in script.lines if x.character == m["character"]]
            ln = (next((x for x in same if (x.emotion, x.action) == (m["emotion"], m["action"])), None)
                  or next((x for x in same if x.emotion == m["emotion"]), None)
                  or next((x for x in same if x.action == m["action"]), None)
                  or (same[0] if same else None))
            # jamais la réplique d'une autre langue : à défaut, la légende sera écrite par le LLM
            local.append({**m, "line": strip_tags(ln.text) if ln else ""})
        caps[lang] = captions(llm, lang, local)
    new = []
    work = episode_dir / "_stickers_work"
    for i, m in enumerate(moments):
        sid = f"{m['character']}-{m['emotion']}-{m['action']}"
        pose = render_pose(cfg, m["character"], m["emotion"], m["action"], work / sid)
        shutil.copy2(pose, lib / f"{sid}_pose.png")
        entry = {**m, "id": sid, "episode": episode_id, "date": time.strftime("%Y-%m-%d"),
                 "emojis": EMOJI.get(m["emotion"], ["😄"]), "captions": {}}
        for lang in langs:
            compose(pose, caps[lang][i], lib / f"{sid}_{lang}.png")
            entry["captions"][lang] = caps[lang][i]
        new.append(entry)
    shutil.rmtree(work, ignore_errors=True)
    with (lib / "index.jsonl").open("a", encoding="utf-8") as fh:
        for e in new:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    build_packs(cfg)
    return new


def build_packs(cfg: Config) -> dict[str, Path]:
    """Packs par langue : les MAX_PACK expressions les plus récentes (≥ 3 requis par WhatsApp)."""
    lib = _lib(cfg)
    index = load_index(cfg)[-MAX_PACK:]
    ser = cfg.series or {}
    out = {}
    for lang in cfg.languages:
        d = (cfg.series_dir or cfg.root) / "stickers" / "packs" / lang
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        stickers = []
        for i, e in enumerate(index, 1):
            src = lib / f"{e['id']}_{lang}.png"
            if not src.exists():
                continue
            shutil.copy2(src, d / f"{i:02d}.png")
            to_webp(src, d / f"{i:02d}.webp")
            stickers.append({"image_file": f"{i:02d}.webp", "emojis": e["emojis"]})
        if not stickers:
            continue
        ffmpeg.run(["-i", d / "01.png", "-vf", "scale=96:96", d / "tray.png"])
        title = (ser.get("title") or {}).get(lang, ser.get("slug", "toonomatic")) if isinstance(ser.get("title"), dict) \
            else ser.get("slug", "toonomatic")
        contents = {"android_play_store_link": "", "ios_app_store_link": "", "sticker_packs": [{
            "identifier": re.sub(r"[^a-z0-9_.-]", "_", f"{ser.get('slug', 'serie')}_{lang}"),
            "name": title[:128], "publisher": title[:128], "tray_image_file": "tray.png",
            "image_data_version": str(len(load_index(cfg))), "avoid_cache": False,
            "publisher_email": "", "publisher_website": "", "privacy_policy_website": "", "license_agreement_website": "",
            "stickers": stickers}]}
        (d / "contents.json").write_text(json.dumps(contents, ensure_ascii=False, indent=2), encoding="utf-8")
        if len(stickers) < 3:
            log.info("pack %s : %d sticker(s) — WhatsApp en exige au moins 3", lang, len(stickers))
        out[lang] = d
    return out
