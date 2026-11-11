"""Mémoire de la série : ce qui a été établi dans les épisodes publiés.

Après validation, un appel LLM extrait des « faits de continuité » (nouveau running gag, objet
cassé, relation qui évolue, lieu découvert…) qui sont réinjectés dans les prompts suivants :
les personnages se souviennent, la série construit sa mythologie.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import Config
from .models import Script
from .writing.llm import LLM

FACTS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["fact", "characters", "kind"],
        "properties": {
            "fact": {"type": "string"},
            "characters": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["running_gag", "event", "relationship", "place", "object", "trait"]},
        }}}},
}


def _path(cfg: Config) -> Path:
    rel = (cfg.series.get("memory") or {}).get("continuity_file", "memory/continuity.jsonl")
    return (cfg.series_dir or cfg.root) / rel


def load_facts(cfg: Config, limit: int | None = None) -> list[dict]:
    p = _path(cfg)
    if not p.exists():
        return []
    facts = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    limit = limit or int((cfg.series.get("memory") or {}).get("max_facts_in_prompt", 25))
    return facts[-limit:]


def record_episode(cfg: Config, llm: LLM, episode_id: str, logline: str, script: Script) -> list[dict]:
    text = "\n".join(f"[{s.id}] {ln.character}: {ln.text}" for s in script.scenes for ln in s.lines)
    system = ("Tu es le scripte (responsable de la continuité) d'une série animée. Extrais au plus 5 faits "
              "DURABLES établis par cet épisode, utiles pour les épisodes suivants. Pas de résumé d'intrigue.")
    known = "\n".join(f"- {f['fact']}" for f in load_facts(cfg))
    user = (f"FAITS DÉJÀ CONNUS :\n{known or '(aucun)'}\n\nÉPISODE {episode_id} — {logline}\n{text}\n"
            "<context>" + json.dumps({"episode": episode_id, "logline": logline,
                                      "characters": sorted({ln.character for ln in script.lines})},
                                     ensure_ascii=False) + "</context>")
    facts = llm.generate_json("continuity", system, user, FACTS_SCHEMA)["facts"][:5]
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for f in facts:
            fh.write(json.dumps({**f, "episode": episode_id, "date": dt.date.today().isoformat()},
                                ensure_ascii=False) + "\n")
    # journal des épisodes diffusés (évite de refaire deux fois la même intrigue)
    with (p.parent / "episodes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"episode": episode_id, "logline": logline, "title": script.title},
                            ensure_ascii=False) + "\n")
    return facts


def aired(cfg: Config, limit: int = 20) -> list[dict]:
    p = _path(cfg).parent / "episodes.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()][-limit:]
