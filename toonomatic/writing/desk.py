"""BUREAU D'ÉCRITURE : le scénario s'écrit HORS de l'usine, sans clé API.

1. `brief()` fabrique un texte unique à coller dans n'importe quelle application de chat
   (Claude, Le Chat, ChatGPT, Gemini…) : bible de la série, noyau des personnages, mémoire,
   style d'humour de chaque langue, contraintes et format de réponse.
2. Le modèle répond par UN objet JSON (structure commune + un script par langue).
3. `parse()` lit ce que l'utilisateur recolle — même entouré de texte ou de balises de code —,
   corrige ce qui peut l'être (émotion, geste ou décor inconnus) et explique clairement ce qui
   ne peut pas l'être (personnage inconnu, scène vide, langue manquante).

L'usine fait ensuite tout le reste : voix, animation, montage, déclinaisons.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ..config import Config
from ..models import Beat, Line, Premise, Scene, Script
from .showrunner import Showrunner


class DeskError(ValueError):
    """Le texte collé n'est pas exploitable ; `problems` est lisible par un humain."""

    def __init__(self, problems: list[str]):
        super().__init__(" ; ".join(problems))
        self.problems = problems


def _allowed_sets(cfg: Config, situation: dict) -> list[str]:
    sets = situation.get("sets") or list(cfg.cast["sets"])
    allowed = cfg["episode"].get("allowed_sets")
    if allowed:
        sets = [s for s in sets if s in allowed] or list(allowed)
    return sets


def plan(cfg: Config, situation: dict) -> dict:
    ep = cfg["episode"]
    target = float(ep["target_minutes"])
    n = max(4, math.ceil(target / float(ep.get("minutes_per_scene", 1.3))))
    words = {l: int(target / n * float(cfg["languages"][l]["words_per_minute"])) for l in cfg.languages}
    return {"scenes": n, "minutes": target, "words_per_scene": words,
            "cast": situation.get("cast") or list(cfg.cast["characters"]), "sets": _allowed_sets(cfg, situation)}


def brief(cfg: Config, situation: dict, lessons: list[str] | None = None) -> str:
    """Le texte complet à coller dans une application de chat."""
    from .llm import MockLLM
    sr = Showrunner(cfg, MockLLM(), lessons or [])
    p = plan(cfg, situation)
    langs = cfg.languages
    styles = "\n\n".join(
        f"=== STYLE D'ÉCRITURE POUR LA VERSION « {l} » ===\n"
        + Path(cfg.path(cfg["languages"][l]["showrunner"])).read_text(encoding="utf-8").strip() for l in langs)
    example_line = '{"character": "momo", "emotion": "smug", "action": "point", "text": "J\'ai un plan."}'
    lang_block = ",\n".join(
        f'  "{l}": {{\n    "title": "titre court et drôle en {l}",\n    "scenes": [\n'
        f'      {{"summary": "résumé en une phrase", "funniness": 7, "lines": [{example_line}]}}\n    ],\n'
        f'    "meta": {{"youtube_title": "…", "youtube_description": "2 phrases", "tags": ["…"], '
        f'"tiktok_caption": "…", "hashtags": ["#…"], "thumbnail_text": "3 mots max"}}\n  }}' for l in langs)
    words = ", ".join(f"{l} ≈ {w} mots" for l, w in p["words_per_scene"].items())
    return f"""Tu es le showrunner d'une web-série animée comique. Écris UN épisode complet, en {len(langs)} versions ({' et '.join(langs)}).
Les versions ne sont PAS des traductions l'une de l'autre : même structure (mêmes scènes, mêmes décors, mêmes personnages présents), mais chaque langue a ses propres dialogues et son propre humour.

{sr._bible('fr')}
{styles}

=== L'ÉPISODE À ÉCRIRE ===
Situation de départ : {situation['situation']}
Durée visée : {p['minutes']:g} minutes par version → exactement {p['scenes']} scènes.
Longueur des dialogues par scène : {words}.
Personnages autorisés (identifiants exacts) : {', '.join(p['cast'])}
Décors autorisés (identifiants exacts) : {', '.join(p['sets'])}
Chaque réplique choisit UNE émotion et UN geste dans les listes ÉMOTIONS et ACTIONS ANIMÉES ci-dessus (identifiants exacts, en anglais).
Construction : escalade comique en trois actes, un running gag qui revient trois fois, une chute à la fin de chaque scène. La contradiction de chaque personnage est le moteur comique.
"funniness" est ta note honnête de la scène, de 1 à 10.

=== FORMAT DE RÉPONSE ===
Réponds UNIQUEMENT par un bloc de code JSON, sans aucun texte avant ni après, exactement de cette forme :
```json
{{
  "logline": "l'épisode en une phrase",
  "running_gag": "le gag récurrent",
  "scenes": [
    {{"set": "{p['sets'][0]}", "characters": ["{p['cast'][0]}"], "purpose": "but comique de la scène"}}
  ],
{lang_block}
}}
```
Règles du format : "scenes" contient exactement {p['scenes']} scènes ; chaque langue contient le même nombre de scènes, dans le même ordre ; guillemets droits uniquement ; JSON complet (ne coupe pas la réponse).
"""


# --------------------------------------------------------------------------- lecture du texte collé
def _load(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # certaines applications remplacent les guillemets droits par des guillemets courbes
        return json.loads(raw.replace("\u201c", '"').replace("\u201d", '"'))


def _extract(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise DeskError(["Aucun objet JSON trouvé. Copiez toute la réponse du modèle, y compris les accolades { }."])
    raw = re.sub(r",\s*([}\]])", r"\1", t[a:b + 1])  # virgules en trop avant } ou ]
    try:
        data = _load(raw)
    except json.JSONDecodeError as e:
        hint = "La réponse semble coupée : demandez au modèle « renvoie le JSON complet »." \
            if raw.count("{") != raw.count("}") or raw.count("[") != raw.count("]") else \
            "Demandez au modèle : « renvoie exactement le même contenu en JSON valide »."
        raise DeskError([f"JSON illisible (ligne {e.lineno}) : {e.msg}. {hint}"]) from e
    if not isinstance(data, dict):
        raise DeskError(["La réponse doit être un objet JSON { … }."])
    return data


def parse(cfg: Config, text: str, situation: dict, episode_id: str = "import") -> tuple[Premise, dict[str, Script], dict, list[str]]:
    """→ (prémisse, scripts par langue, métadonnées par langue, avertissements). Lève DeskError sinon."""
    data = _extract(text)
    p = plan(cfg, situation)
    cast_ok, sets_ok = set(cfg.cast["characters"]), p["sets"]
    actions, emotions = set(cfg.cast["actions"]), set(cfg.cast["emotions"])
    problems: list[str] = []
    warnings: list[str] = []
    scenes_in = data.get("scenes")
    if not isinstance(scenes_in, list) or not scenes_in:
        raise DeskError(['Champ "scenes" absent ou vide : la réponse ne suit pas le format demandé.'])
    missing = [l for l in cfg.languages if not isinstance(data.get(l), dict) or not isinstance(data[l].get("scenes"), list)]
    if missing:
        raise DeskError([f"Version(s) manquante(s) : {', '.join(missing)}. Demandez au modèle de les ajouter au même JSON."])
    for l in cfg.languages:
        if len(data[l]["scenes"]) != len(scenes_in):
            problems.append(f"La version {l} a {len(data[l]['scenes'])} scènes au lieu de {len(scenes_in)}.")
    if problems:
        raise DeskError(problems)

    fixed = {"set": 0, "emotion": 0, "action": 0}
    beats: list[Beat] = []
    for i, sc in enumerate(scenes_in):
        sc = sc if isinstance(sc, dict) else {}
        st = sc.get("set")
        if st not in sets_ok:
            fixed["set"] += 1
            st = sets_ok[i % len(sets_ok)]
        chars = [c for c in (sc.get("characters") or []) if c in cast_ok]
        beats.append(Beat(id=f"s{i + 1:02d}", set=st, characters=chars, purpose=str(sc.get("purpose", ""))[:300]))

    scripts: dict[str, Script] = {}
    metas: dict[str, dict] = {}
    unknown: set[str] = set()
    for l in cfg.languages:
        out_scenes = []
        for i, sc in enumerate(data[l]["scenes"]):
            sc = sc if isinstance(sc, dict) else {}
            lines = []
            for ln in sc.get("lines") or []:
                if not isinstance(ln, dict) or not str(ln.get("text", "")).strip():
                    continue
                ch = ln.get("character")
                if ch not in cast_ok:
                    unknown.add(str(ch))
                    continue
                emo, act = ln.get("emotion"), ln.get("action")
                if emo not in emotions:
                    fixed["emotion"] += 1
                    emo = "neutral"
                if act not in actions:
                    fixed["action"] += 1
                    act = "talk"
                lines.append(Line(character=ch, text=str(ln["text"]).strip(), emotion=emo, action=act))
                if ch not in beats[i].characters:
                    beats[i].characters.append(ch)
            if not lines:
                problems.append(f"Version {l}, scène {i + 1} : aucune réplique exploitable.")
                continue
            s = Scene(id=beats[i].id, set=beats[i].set, characters=[], summary=str(sc.get("summary") or beats[i].purpose)[:300],
                      lines=lines)
            s.__dict__["_fun"] = sc.get("funniness") if isinstance(sc.get("funniness"), (int, float)) else 5
            out_scenes.append(s)
        if out_scenes:
            max(out_scenes, key=lambda s: s.__dict__["_fun"]).highlight = True
            for s in out_scenes:
                s.__dict__.pop("_fun", None)
        scripts[l] = Script(lang=l, title=str(data[l].get("title") or situation["situation"][:60]).strip(),
                            scenes=out_scenes, style="bureau d'écriture")
        if isinstance(data[l].get("meta"), dict):
            metas[l] = data[l]["meta"]
    if unknown:
        problems.append(f"Personnage(s) inconnu(s) : {', '.join(sorted(unknown))}. Autorisés : {', '.join(sorted(cast_ok))}.")
    if problems:
        raise DeskError(problems)
    for l, s in scripts.items():  # les personnages présents sont ceux de la structure commune
        for sc, b in zip(s.scenes, beats):
            sc.characters = list(b.characters)

    if fixed["set"]:
        warnings.append(f"{fixed['set']} décor(s) inconnu(s) remplacé(s) par un décor autorisé.")
    if fixed["emotion"] or fixed["action"]:
        warnings.append(f"{fixed['emotion']} émotion(s) et {fixed['action']} geste(s) inconnus remplacés par des valeurs neutres.")
    if len(scenes_in) != p["scenes"]:
        warnings.append(f"{len(scenes_in)} scènes reçues pour {p['scenes']} demandées (accepté).")
    ep = cfg["episode"]
    premise = Premise(id=episode_id, situation=situation["situation"],
                      logline=str(data.get("logline") or situation["situation"])[:400],
                      cast=sorted({c for b in beats for c in b.characters}), beats=beats,
                      target_minutes=float(ep["target_minutes"]), running_gag=str(data.get("running_gag", ""))[:300],
                      source="bureau")
    return premise, scripts, metas, warnings
