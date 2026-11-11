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
