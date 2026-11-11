"""Le "Showrunner IA".

1. Une **prémisse commune** (situation, distribution, outline en scènes) est écrite une fois.
2. Chaque langue a **son propre agent d'écriture** (prompt système distinct : humour FR de rue /
   absurde vs. deadpan britannique ou rythme US). Ce ne sont PAS des traductions : seule la
   structure (scènes, décors, personnages) est partagée — ce qui permet de réutiliser décors,
   plans de caméra et rigs dans l'animation.
3. Les épisodes longs (10–15 min) sont écrits scène par scène avec un résumé glissant, puis
   allongés si l'estimation de durée est trop courte.
4. Les sous-titres de l'autre langue sont produits par traduction (fidèle au sens, pas au gag).
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from ..config import Config
from ..models import Beat, Line, Premise, Scene, Script
from .llm import LLM, LLMError

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- schémas JSON
def _premise_schema(cast: list[str], sets: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["logline", "running_gag", "beats"],
        "properties": {
            "logline": {"type": "string"},
            "running_gag": {"type": "string"},
            "beats": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "set", "characters", "purpose"],
                "properties": {
                    "id": {"type": "string"},
                    "set": {"type": "string", "enum": sets},
                    "characters": {"type": "array", "items": {"type": "string", "enum": cast}},
                    "purpose": {"type": "string"},
                }}},
        },
    }


def _scene_schema(chars: list[str], actions: list[str], emotions: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["summary", "lines", "funniness"],
        "properties": {
            "summary": {"type": "string"},
            "funniness": {"type": "integer", "description": "auto-évaluation 1-10"},
            "lines": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["character", "text", "emotion", "action"],
                "properties": {
                    "character": {"type": "string", "enum": chars},
                    "text": {"type": "string"},
                    "emotion": {"type": "string", "enum": emotions},
                    "action": {"type": "string", "enum": actions},
                }}},
        },
    }


TITLE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title"],
                "properties": {"title": {"type": "string"}}}
TRANSLATE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines"],
                    "properties": {"lines": {"type": "array", "items": {"type": "string"}}}}


def _ctx(d: dict) -> str:
    return "<context>" + json.dumps(d, ensure_ascii=False) + "</context>"


class Showrunner:
    def __init__(self, cfg: Config, llm: LLM, lessons: list[str] | None = None):
        self.cfg, self.llm = cfg, llm
        self.cast = cfg.cast
        self.lessons = lessons or []
        self.actions = list(self.cast["actions"].keys())
        self.emotions = list(self.cast["emotions"])

    # ----------------------------------------------------------------- bible commune
    def _bible(self, lang: str | None = None) -> str:
        from ..continuity import aired, load_facts
        L = lang or "fr"
        ser = self.cfg.series or {}

        def loc(v):
            return v.get(L, next(iter(v.values()), "")) if isinstance(v, dict) else (v or "")

        chars = []
        for k, v in self.cast["characters"].items():
            bits = [f"- {k} ({v['display']}): {v.get('bio_' + L, v.get('bio_fr', ''))}"]
            if v.get("personality"):
                bits.append(f"  traits: {', '.join(v['personality'])}")
            if v.get("catchphrases"):
                bits.append(f"  répliques fétiches (avec parcimonie): {' / '.join(loc(v['catchphrases']) or [])}")
            if v.get("look"):
                bits.append(f"  apparence: {v['look']}")
            chars.append("\n".join(bits))
        sets = ", ".join(self.cast["sets"].keys())
        acts = ", ".join(f"{k} = {v}" for k, v in self.cast["actions"].items())
        lessons = "\n".join(f"- {x}" for x in self.lessons[-self.cfg.get("learning.max_lessons_in_prompt", 15):])
        facts = "\n".join(f"- {f['fact']}" for f in load_facts(self.cfg))
        past = "\n".join(f"- {e['title']}: {e['logline']}" for e in aired(self.cfg))
        rules = "\n".join(f"- {r}" for r in ser.get("rules", []))
        head = ""
        if ser:
            head = (f"SÉRIE : {loc(ser.get('title', {}))}\nPITCH : {loc(ser.get('logline', {}))}\n"
                    f"UNIVERS : {ser.get('universe', '').strip()}\nTON : {loc(ser.get('tone', {}))}\n"
                    + (f"RÈGLES DE CONTINUITÉ (intangibles) :\n{rules}\n" if rules else "") + "\n")
        return (head + f"PERSONNAGES:\n" + "\n".join(chars) + f"\n\nDÉCORS DISPONIBLES: {sets}\n\n"
                f"ACTIONS ANIMÉES: {acts}\nÉMOTIONS: {', '.join(self.emotions)}\n"
                + (f"\nCE QUE LA SÉRIE A DÉJÀ ÉTABLI (à respecter, à réutiliser en clin d'œil):\n{facts}\n" if facts else "")
                + (f"\nÉPISODES DÉJÀ DIFFUSÉS (ne pas refaire la même intrigue):\n{past}\n" if past else "")
                + (f"\nLEÇONS DES ÉPISODES PRÉCÉDENTS (à respecter):\n{lessons}\n" if lessons else ""))

    def _showrunner_prompt(self, lang: str) -> str:
        path = self.cfg.path(self.cfg["languages"][lang]["showrunner"])
        return Path(path).read_text(encoding="utf-8") + "\n\n" + self._bible(lang)

    # ----------------------------------------------------------------- 1. prémisse
    def premise(self, episode_id: str, situation: dict) -> Premise:
        ep = self.cfg["episode"]
        target = float(ep["target_minutes"])
        n_beats = max(4, math.ceil(target / float(ep.get("minutes_per_scene", 1.3))))
        cast = situation.get("cast") or list(self.cast["characters"])
        sets = situation.get("sets") or list(self.cast["sets"])
        allowed = ep.get("allowed_sets")  # pilote : un seul décor principal
        if allowed:
            sets = [x for x in sets if x in allowed] or list(allowed)
        system = ("Tu es le directeur d'écriture d'une web-série animée bilingue. Tu conçois la "
                  "structure d'un épisode (indépendante de la langue) : une escalade comique en "
                  "trois actes, un running gag, et une chute finale.\n\n" + self._bible())
        user = (f"Situation de départ : {situation['situation']}\n"
                f"Durée visée : {target} minutes → exactement {n_beats} scènes.\n"
                f"Personnages autorisés : {cast}. Décors autorisés : {sets}.\n"
                "Pour chaque scène : décor, personnages présents, et but dramatique/comique.\n"
                + _ctx({"situation": situation["situation"], "cast": cast, "sets": sets,
                        "n_beats": n_beats}))
        data = self.llm.generate_json("premise", system, user, _premise_schema(cast, sets))
        beats = [Beat(id=b["id"] or f"s{i + 1:02d}", set=b["set"],
                      characters=[c for c in b["characters"] if c in cast] or cast[:2],
                      purpose=b["purpose"]) for i, b in enumerate(data["beats"])]
        for i, b in enumerate(beats):  # ids uniques et ordonnés
            b.id = f"s{i + 1:02d}"
        return Premise(id=episode_id, situation=situation["situation"], logline=data["logline"],
                       cast=cast, beats=beats, target_minutes=target,
                       running_gag=data.get("running_gag", ""), source=situation.get("id", "bank"))

    # ----------------------------------------------------------------- 2. script par langue
    def write_script(self, premise: Premise, lang: str) -> Script:
        system = self._showrunner_prompt(lang)
        wpm = float(self.cfg["languages"][lang]["words_per_minute"])
        per_scene_min = premise.target_minutes / len(premise.beats)
        scenes: list[Scene] = []
        recap: list[str] = []
        outline = "\n".join(f"{b.id} [{b.set}] {', '.join(b.characters)} — {b.purpose}"
                            for b in premise.beats)
        for beat in premise.beats:
            words = int(per_scene_min * wpm)
            user = (f"LANGUE DE SORTIE : {lang}\nPRÉMISSE : {premise.logline}\n"
                    f"RUNNING GAG : {premise.running_gag}\n\nOUTLINE COMPLÈTE :\n{outline}\n\n"
                    f"DÉJÀ ÉCRIT (résumé) :\n" + ("\n".join(recap[-6:]) or "(début de l'épisode)") +
                    f"\n\nÉCRIS MAINTENANT LA SCÈNE {beat.id} — décor « {beat.set} », personnages "
                    f"{beat.characters}. But : {beat.purpose}\nLongueur : environ {words} mots de "
                    "dialogue (≈ {:.1f} min). Termine par une chute.\n".format(per_scene_min)
                    + _ctx({"lang": lang, "beat": beat.__dict__}))
            data = self.llm.generate_json("scene", system,  user,
                                          _scene_schema(beat.characters, self.actions, self.emotions))
            scene = Scene(id=beat.id, set=beat.set, characters=beat.characters,
                          summary=data["summary"], lines=self._clean_lines(data["lines"], beat))
            scene.__dict__["funniness"] = data.get("funniness", 5)
            scenes.append(scene)
            recap.append(f"{beat.id}: {data['summary']}")

        script = Script(lang=lang, title="", scenes=scenes, style=self.cfg["languages"][lang]["showrunner"])
        self._fit_duration(script, premise, system, wpm)
        best = max(script.scenes, key=lambda s: s.__dict__.get("funniness", 0))
        best.highlight = True
        for s in script.scenes:
            s.__dict__.pop("funniness", None)
        script.title = self.llm.generate_json(
            "title", system, f"Donne un titre court et drôle ({lang}) pour cet épisode : "
            f"{premise.logline}\n" + _ctx({"lang": lang, "situation": premise.situation}),
            TITLE_SCHEMA)["title"]
        return script

    def _clean_lines(self, raw: list[dict], beat: Beat) -> list[Line]:
        out = []
        for ln in raw:
            if not ln.get("text", "").strip():
                continue
            out.append(Line(character=ln["character"] if ln["character"] in beat.characters else beat.characters[0],
                            text=ln["text"].strip(),
                            emotion=ln["emotion"] if ln["emotion"] in self.emotions else "neutral",
                            action=ln["action"] if ln["action"] in self.actions else "talk"))
        if not out:
            raise LLMError(f"scène {beat.id} vide")
        return out

    def _fit_duration(self, script: Script, premise: Premise, system: str, wpm: float) -> None:
        ep = self.cfg["episode"]
        lo, hi = float(ep["min_minutes"]), float(ep["max_minutes"])
        for _ in range(int(ep.get("max_extension_rounds", 3))):
            est = script.estimated_minutes(wpm)
            if est >= lo:
                break
            # On allonge la scène la plus courte (souvent la plus faible) au lieu d'ajouter du remplissage.
            short = min(script.scenes, key=lambda s: len(s.lines))
            beat = next(b for b in premise.beats if b.id == short.id)
            user = (f"La scène {short.id} est trop courte (épisode estimé {est:.1f} min, minimum {lo}).\n"
                    f"Voici la scène actuelle :\n" +
                    "\n".join(f"{ln.character}: {ln.text}" for ln in short.lines) +
                    "\n\nÉcris la SUITE de cette scène (nouvelles répliques uniquement), en relançant "
                    "l'escalade avant la chute.\n" + _ctx({"lang": script.lang, "beat": beat.__dict__}))
            data = self.llm.generate_json("extend", system, user,
                                          _scene_schema(beat.characters, self.actions, self.emotions))
            short.lines.extend(self._clean_lines(data["lines"], beat))
        # Trop long : on resserre les scènes les plus bavardes (minimum 3 répliques : amorce, relance, chute).
        while script.estimated_minutes(wpm) > hi and any(len(s.lines) > 3 for s in script.scenes):
            longest = max(script.scenes, key=lambda s: len(s.lines))
            # retire l'avant-dernière réplique (garde la chute)
            longest.lines.pop(-2)
        log.info("script %s : %.1f min estimées, %d répliques", script.lang,
                 script.estimated_minutes(wpm), len(script.lines))

    # ----------------------------------------------------------------- 3. sous-titres traduits
    def translate_lines(self, script: Script, target: str, chunk: int = 60) -> list[str]:
        texts = [ln.text for ln in script.lines]
        out: list[str] = []
        system = ("Tu traduis des sous-titres de dessin animé. Traduis le SENS et l'intention comique, "
                  "de façon concise (lisible en sous-titre), sans les balises entre crochets. "
                  "Renvoie exactement une ligne traduite par ligne source, dans le même ordre.")
        for i in range(0, len(texts), chunk):
            part = texts[i:i + chunk]
            user = (f"Traduis de {script.lang} vers {target} ces {len(part)} répliques :\n"
                    + "\n".join(f"{j + 1}. {t}" for j, t in enumerate(part))
                    + "\n" + _ctx({"target": target, "lines": part}))
            res = self.llm.generate_json("translate", system, user, TRANSLATE_SCHEMA)["lines"]
            if len(res) != len(part):  # garde-fou : on ne décale jamais les timings
                res = (res + part)[:len(part)]
            out.extend(res)
        return out
