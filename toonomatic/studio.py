"""STUDIO à la demande : des machines qui n'existent que le temps d'un travail.

    COMMAND (téléphone) ──► Factory.order()/decide() ──► VAULT queue/ ──► Launcher ──► worker --once
                                                                              │          (pull → run → push → exit)
                                                                              └─ CPU : en ligne, sur le serveur
                                                                                 GPU : machine louée à la minute

- Brouillon et validation tournent sur CPU (aucun GPU payant tant que l'histoire n'est pas validée).
- Le niveau final déclenche le lanceur GPU configuré, puis la machine s'arrête d'elle-même.
- Lanceurs interchangeables : `inline` (thread local), `command` (n'importe quelle CLI de
  fournisseur : SSH, runpodctl, vastai, gcloud…), `webhook` (point d'entrée serverless), `none`
  (un worker permanent ailleurs viendra chercher le travail).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

from .config import Config
from .ideas import load_bank
from .store import make_store
from .vault import claim, enqueue, make_vault, pull_tree, push_tree, release

log = logging.getLogger(__name__)
NEXT_TIER = {"draft": "validation", "validation": "final"}


def cloud(cfg: Config) -> bool:
    return (cfg.get("state.backend") or "sqlite") == "vault"


# --------------------------------------------------------------------------- lanceurs
class Launcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sc = cfg.get("studio") or {}

    def start(self, job: dict) -> str:
        kind = self.sc.get("gpu_launcher" if job.get("needs_gpu") else "cpu_launcher", "inline")
        if kind == "inline":
            threading.Thread(target=run_worker, args=(self.cfg,), kwargs={"once": True, "can_gpu": True},
                             daemon=True).start()
            return "inline"
        if kind == "command":
            tpl = self.sc.get("gpu_command" if job.get("needs_gpu") else "cpu_command", "")
            if not tpl:
                raise RuntimeError("studio.*_command vide")
            cmd = tpl.format(episode_id=shlex.quote(job["episode_id"]), tier=shlex.quote(job.get("tier", "")))
            subprocess.Popen(cmd, shell=True, start_new_session=True)  # la machine démarre et s'éteint seule
            return "command"
        if kind == "webhook":
            wh = self.sc.get("webhook") or {}
            headers = {}
            if wh.get("token_env") and os.environ.get(wh["token_env"]):
                headers["Authorization"] = f"Bearer {os.environ[wh['token_env']]}"
            r = requests.post(wh["url"], json=job, headers=headers, timeout=30)
            r.raise_for_status()
            return "webhook"
        return "queued"  # 'none' : un worker externe viendra prendre le travail


# --------------------------------------------------------------------------- worker
def _episode_prefix(eid: str) -> str:
    return f"episodes/{eid}/files"


def _sync_series(cfg: Config, vault, direction: str) -> None:
    sd = cfg.series_dir
    prefix = f"series/{sd.name}"
    if direction == "pull":
        pull_tree(vault, prefix, sd)
    else:
        push_tree(vault, sd, prefix)


def execute(cfg: Config, job: dict) -> str:
    """Exécute une commande sur la machine courante."""
    from .pipeline import Pipeline
    pipe = Pipeline(cfg)
    eid, act = job["episode_id"], job.get("action", "run")
    if act == "promote":
        pipe.promote(eid, job["tier"])
    elif act == "fix":
        pipe._use_tier(eid)
        pipe.apply_fixes(eid, job["fixes"], by=job.get("by", "humain"))
    if act == "publish":
        pipe.publish(eid)
        return pipe.store.get(eid)["status"]
    return pipe.run(eid)


def run_worker(cfg: Config, once: bool = True, can_gpu: bool = False, poll_s: int = 15) -> int:
    """Prend une commande, synchronise le strict nécessaire, travaille, renvoie, s'arrête."""
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    done = 0
    vault = make_vault(cfg.get("vault") or {}, cfg.root)  # local (une machine) ou S3/R2 (cloud)
    while True:
        got = claim(vault, worker_id, can_gpu=can_gpu)
        if not got:
            if once:
                return done
            time.sleep(poll_s)
            continue
        _, job = got
        eid = job["episode_id"]
        local = cfg.workdir / eid
        try:
            if cloud(cfg):
                _sync_series(cfg, vault, "pull")
                st = pull_tree(vault, _episode_prefix(eid), local)
                log.info("[%s] rapatrié : %.1f Mo", eid, st["bytes_received"] / 1e6)
            status = execute(cfg, job)
            log.info("[%s] %s → %s", eid, job.get("action"), status)
        finally:
            if cloud(cfg):
                st = push_tree(vault, local, _episode_prefix(eid), exclude=["*_frames/*", "*.tmp*"])
                log.info("[%s] envoyé : %.1f Mo", eid, st["bytes_sent"] / 1e6)
                _sync_series(cfg, vault, "push")  # mémoire de continuité, plaques générées
            release(vault, eid)
        done += 1
        if once:
            return done


# --------------------------------------------------------------------------- façade pour COMMAND
class Factory:
    """Ce que le téléphone peut demander. Ne calcule jamais rien lui-même : il commande."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = make_store(cfg)
        self.vault = make_vault(cfg.get("vault") or {}, cfg.root)
        self.launcher = Launcher(cfg)

    def tiers(self) -> list[str]:
        return list((self.cfg.get("production.tiers") or {}).keys())

    def needs_gpu(self, tier: str) -> bool:
        return bool(((self.cfg.get("production.tiers") or {}).get(tier) or {}).get("gpu"))

    def situations(self) -> list[dict]:
        return [{"id": s["id"], "situation": s["situation"]} for s in load_bank(self.cfg)]

    def _dispatch(self, job: dict) -> dict:
        job = {**job, "needs_gpu": self.needs_gpu(job.get("tier") or self.tier_of(job["episode_id"])),
               "requested": time.time()}
        enqueue(self.vault, job)
        how = self.launcher.start(job)
        self.store.log(job["episode_id"], "dispatch", {"action": job.get("action"), "launcher": how,
                                                       "gpu": job["needs_gpu"]})
        return {"episode_id": job["episode_id"], "launcher": how, "gpu": job["needs_gpu"]}

    def tier_of(self, eid: str) -> str:
        return (self.store.get(eid) or {}).get("info", {}).get("tier") or self.cfg.get("production.default_tier")

    def order(self, situation_id: str | None = None, situation_text: str | None = None,
              tier: str | None = None) -> dict:
        from .models import save_json
        from .pipeline import Pipeline
        tier = tier or self.cfg.get("production.default_tier", "draft")
        if tier not in self.tiers():
            raise ValueError(f"niveau inconnu : {tier}")
        if situation_text:
            sit = {"id": "commande-" + time.strftime("%H%M%S"), "situation": situation_text.strip()[:600],
                   "source": "mobile"}
        elif situation_id:
            sit = next((s for s in load_bank(self.cfg) if s["id"] == situation_id), None)
            if not sit:
                raise ValueError(f"situation inconnue : {situation_id}")
        else:
            sit = None
        pipe = Pipeline(self.cfg)
        [eid] = pipe.produce(1, tier=tier, situation=sit, run=False)
        if cloud(self.cfg):  # la commande (situation.json) part au vault ; le disque du serveur est jetable
            push_tree(self.vault, self.cfg.workdir / eid, _episode_prefix(eid))
        return self._dispatch({"episode_id": eid, "action": "run", "tier": tier})

    # -- bureau d'écriture (scénario écrit hors de l'usine, sans clé)
    def _situation(self, situation_id: str | None, situation_text: str | None) -> dict:
        import random
        if situation_text and situation_text.strip():
            return {"id": "idee-" + time.strftime("%H%M%S"), "situation": situation_text.strip()[:600], "source": "mobile"}
        bank = load_bank(self.cfg)
        if situation_id:
            sit = next((s for s in bank if s["id"] == situation_id), None)
            if not sit:
                raise ValueError(f"situation inconnue : {situation_id}")
            return sit
        return random.choice(bank)

    def desk_brief(self, situation_id: str | None = None, situation_text: str | None = None) -> dict:
        from .learning import load_lessons
        from .writing import desk
        sit = self._situation(situation_id, situation_text)
        return {"situation": sit, "brief": desk.brief(self.cfg, sit, load_lessons(self.cfg)),
                "plan": desk.plan(self.cfg, sit)}

    def _clean_situation(self, situation) -> dict:
        if not isinstance(situation, dict) or not situation.get("situation"):
            raise ValueError("situation manquante : préparez d'abord le brief")
        return {"id": re.sub(r"[^a-z0-9-]+", "-", str(situation.get("id") or "idee").lower())[:40] or "idee",
                "situation": str(situation["situation"])[:600],
                **{k: situation[k] for k in ("cast", "sets") if isinstance(situation.get(k), list)}}

    def desk_check(self, situation: dict, text: str) -> dict:
        """Vérification à blanc : rien n'est créé. Lève DeskError si le texte est illisible."""
        from .writing import checker, desk
        sit = self._clean_situation(situation)
        premise, scripts, _, warnings = desk.parse(self.cfg, text, sit)
        return {**checker.check(self.cfg, premise, scripts), "warnings": warnings,
                "titles": {l: s.title for l, s in scripts.items()},
                "lines": {l: len(s.lines) for l, s in scripts.items()}}

    def milestones(self, ep: dict) -> list[dict]:
        """Quatre états à ne jamais confondre : écrit · produit · regardable · apprécié."""
        info = ep.get("info", {})
        tiers = self.tiers()
        tier = info.get("tier")
        final = bool(tiers) and tier == tiers[-1]
        chk = info.get("script_check") or {}
        by = info.get("written_by")
        if not any(st.startswith("script_") for st in ep.get("stages", [])):
            m1 = {"state": "pending", "detail": "pas encore écrit"}
        elif by == "démo":
            m1 = {"state": "fail", "detail": "texte FACTICE (mode démo, aucun modèle) : sert à tester la chaîne, pas à juger"}
        else:
            src = {"bureau": "bureau d'écriture", "modèle": "modèle branché"}.get(by, "écriture en cours de contrôle")
            m1 = {"state": "ok" if chk.get("ok", True) and not chk.get("to_review") else "warn",
                  "detail": f"{src} · vérificateur : {chk.get('blocking', 0)} bloquant, {chk.get('to_review', 0)} à revoir"
                  if chk else src}
        sc = (info.get("director") or {}).get("scores") or {}
        beeps = info.get("voice_engine") == "MockTTS"
        if ep["status"] == "failed":
            m2 = {"state": "fail", "detail": "la production a échoué"}
        elif ep["status"] in ("queued", "running") or not sc:
            m2 = {"state": "pending", "detail": "production en cours" if ep["status"] in ("queued", "running") else "pas encore produit"}
        else:
            tech = [sc.get("son"), sc.get("animation")]
            good = all(v is not None and v >= 6 for v in tech)
            m2 = {"state": "ok" if good and final and not beeps else "warn" if good else "fail",
                  "detail": f"niveau {tier} · son {sc.get('son')}/10 · image {sc.get('animation')}/10"
                  + (" · voix : bips de remplacement" if beeps else "")
                  + ("" if final else " · pas encore le rendu final")}
        rev = (info.get("review") or {}).get("decision")
        if ep["status"] == "rejected":
            m3 = {"state": "fail", "detail": "rejeté par vous"}
        elif ep["status"] in ("approved", "published") and final:
            m3 = {"state": "ok", "detail": "validé par vous au niveau final"}
        elif ep["status"] == "review":
            m3 = {"state": "pending", "detail": "à vous de juger : seul un humain décide si c'est regardable"}
        else:
            m3 = {"state": "pending", "detail": "pas encore jugé" + (f" (dernière décision : {rev})" if rev else "")}
        from .social import _jsonl, _mem, asset_catalog
        mine = {a for a, v in asset_catalog(self.cfg).items() if v.get("episode") == ep["id"]}
        ev = [e for e in _jsonl(_mem(self.cfg) / "social_events.jsonl")
              if e["asset_id"] in mine and e["provenance"] == "measured"]
        views = sum(e["value"] for e in ev if e["metric"] == "views")
        if not ev:
            m4 = {"state": "unknown", "detail": "inconnu : aucune mesure d'audience importée"}
        else:
            eng = sum(e["value"] for e in ev if e["metric"] in ("likes", "comments", "shares", "saves"))
            m4 = {"state": "ok" if views >= 100 and eng / max(1, views) >= 0.05 else "warn",
                  "detail": f"{int(views)} vues, {int(eng)} réactions (mesuré) — à comparer aux autres épisodes"}
        names = ["Scénario écrit", "Produit techniquement", "Regardable", "Apprécié du public"]
        return [{"name": n, **m} for n, m in zip(names, (m1, m2, m3, m4))]

    def desk_import(self, situation: dict, text: str, tier: str | None = None) -> dict:
        from .pipeline import Pipeline
        tier = tier or self.cfg.get("production.default_tier", "draft")
        if tier not in self.tiers():
            raise ValueError(f"niveau inconnu : {tier}")
        sit = self._clean_situation(situation)
        eid, warnings = Pipeline(self.cfg).import_script(sit, text, tier)
        if cloud(self.cfg):
            push_tree(self.vault, self.cfg.workdir / eid, _episode_prefix(eid))
        return {**self._dispatch({"episode_id": eid, "action": "run", "tier": tier}), "warnings": warnings}

    def decide(self, eid: str, decision: str, reason: str = "", redo_from: str = "script",
               tier: str | None = None, fixes: list | None = None) -> dict:
        from .review import apply_decision
        ep = self.store.get(eid)
        if not ep:
            raise KeyError(eid)
        if ep["status"] in ("running",):
            raise RuntimeError("épisode en cours de production")
        current = self.tier_of(eid)
        if decision == "promote" or (decision == "approve" and current in NEXT_TIER):
            target = tier or NEXT_TIER.get(current)
            if not target:
                raise ValueError("déjà au niveau final")
            self.store.log(eid, "review", {"decision": "promote", "from": current, "to": target, "reason": reason})
            return self._dispatch({"episode_id": eid, "action": "promote", "tier": target})
        if decision == "fix":
            return self._dispatch({"episode_id": eid, "action": "fix", "fixes": fixes or [], "tier": current})
        apply_decision(self.cfg, self.store, eid, decision, reason, redo_from)
        if decision == "approve":
            return self._dispatch({"episode_id": eid, "action": "publish", "tier": current})
        if decision == "redo":
            return self._dispatch({"episode_id": eid, "action": "run", "tier": current})
        return {"episode_id": eid, "status": self.store.get(eid)["status"]}

    # -- lecture
    def episode(self, eid: str) -> dict | None:
        ep = self.store.get(eid)
        if not ep:
            return None
        rep = self.read_json(eid, "director_report.json")
        return {**ep, "tier": self.tier_of(eid), "director_report": rep,
                "next_tier": NEXT_TIER.get(self.tier_of(eid))}

    def read_json(self, eid: str, rel: str) -> dict | None:
        p = self.cfg.workdir / eid / rel
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        raw = self.vault.get_bytes(f"{_episode_prefix(eid)}/{rel}") if cloud(self.cfg) else None
        return json.loads(raw) if raw else None

    def media(self, eid: str, rel: str) -> tuple[str, str]:
        """('url', lien signé) en cloud S3 — le téléphone lit en streaming ; sinon ('file', chemin)."""
        if ".." in Path(rel).parts:
            raise ValueError("chemin invalide")
        if cloud(self.cfg):
            url = self.vault.url(f"{_episode_prefix(eid)}/{rel}")
            if url:
                return "url", url
            root = getattr(self.vault, "root", None)
            return "file", str(Path(root) / _episode_prefix(eid) / rel)
        return "file", str(self.cfg.workdir / eid / rel)
