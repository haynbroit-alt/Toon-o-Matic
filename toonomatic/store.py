"""Registre SQLite des épisodes : statut, étapes terminées, décisions de revue."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,          -- queued|running|review|approved|rejected|published|failed
    situation_id TEXT,
    stages TEXT NOT NULL DEFAULT '[]',
    risk REAL,
    info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    episode_id TEXT, ts REAL, kind TEXT, payload TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def create(self, episode_id: str, situation_id: str) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO episodes(id, created, updated, status, situation_id) "
                      "VALUES (?,?,?,?,?)", (episode_id, now, now, "queued", situation_id))

    def get(self, episode_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["stages"] = json.loads(d["stages"])
        d["info"] = json.loads(d["info"])
        return d

    def list(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT id FROM episodes", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self._conn() as c:
            ids = [r["id"] for r in c.execute(q + " ORDER BY created", args)]
        return [self.get(i) for i in ids]

    def update(self, episode_id: str, *, status: str | None = None, stage_done: str | None = None,
               risk: float | None = None, info: dict | None = None) -> None:
        ep = self.get(episode_id)
        if ep is None:
            raise KeyError(episode_id)
        stages = ep["stages"]
        if stage_done and stage_done not in stages:
            stages.append(stage_done)
        merged = {**ep["info"], **(info or {})}
        with self._conn() as c:
            c.execute("UPDATE episodes SET updated=?, status=?, stages=?, risk=?, info=? WHERE id=?",
                      (time.time(), status or ep["status"], json.dumps(stages),
                       ep["risk"] if risk is None else risk, json.dumps(merged, ensure_ascii=False),
                       episode_id))

    def reset_stages(self, episode_id: str, keep: list[str]) -> None:
        ep = self.get(episode_id)
        with self._conn() as c:
            c.execute("UPDATE episodes SET stages=? WHERE id=?",
                      (json.dumps([s for s in ep["stages"] if s in keep]), episode_id))

    def log(self, episode_id: str, kind: str, payload: dict | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events VALUES (?,?,?,?)",
                      (episode_id, time.time(), kind, json.dumps(payload or {}, ensure_ascii=False)))

    def events(self, episode_id: str, limit: int = 30) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT ts, kind, payload FROM events WHERE episode_id=? ORDER BY ts DESC LIMIT ?",
                             (episode_id, limit)).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"])} for r in reversed(rows)]

    def recent_situations(self, since_ts: float) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT situation_id FROM episodes WHERE created>=? AND status!='rejected'",
                             (since_ts,))
            return {r["situation_id"] for r in rows}


class VaultStore:
    """Même interface que `Store`, mais l'état vit dans le VAULT (episodes/<id>/state.json).

    Indispensable en cloud : le serveur de commande (Render…) a un disque éphémère et les
    machines du STUDIO sont jetables ; seul le vault est permanent. Un épisode n'a qu'un
    écrivain à la fois (le worker qui détient son verrou, ou le serveur pendant la revue).
    """

    MAX_EVENTS = 500

    def __init__(self, vault):
        self.v = vault

    def _key(self, episode_id: str) -> str:
        return f"episodes/{episode_id}/state.json"

    def _save(self, ep: dict) -> None:
        ep["updated"] = time.time()
        self.v.put_bytes(self._key(ep["id"]), json.dumps(ep, ensure_ascii=False, indent=1).encode(),
                         "application/json")

    def create(self, episode_id: str, situation_id: str) -> None:
        if self.get(episode_id):
            return
        now = time.time()
        self._save({"id": episode_id, "created": now, "updated": now, "status": "queued",
                    "situation_id": situation_id, "stages": [], "risk": None, "info": {}, "events": []})

    def get(self, episode_id: str) -> dict | None:
        raw = self.v.get_bytes(self._key(episode_id))
        return json.loads(raw) if raw else None

    def list(self, status: str | None = None) -> list[dict]:
        eps = [self.get(k.split("/")[1]) for k in self.v.list("episodes/") if k.endswith("/state.json")]
        eps = [e for e in eps if e and (status is None or e["status"] == status)]
        return sorted(eps, key=lambda e: e["created"])

    def update(self, episode_id: str, *, status: str | None = None, stage_done: str | None = None,
               risk: float | None = None, info: dict | None = None) -> None:
        ep = self.get(episode_id)
        if ep is None:
            raise KeyError(episode_id)
        if stage_done and stage_done not in ep["stages"]:
            ep["stages"].append(stage_done)
        ep["status"] = status or ep["status"]
        ep["risk"] = ep["risk"] if risk is None else risk
        ep["info"] = {**ep["info"], **(info or {})}
        self._save(ep)

    def reset_stages(self, episode_id: str, keep: list[str]) -> None:
        ep = self.get(episode_id)
        ep["stages"] = [s for s in ep["stages"] if s in keep]
        self._save(ep)

    def log(self, episode_id: str, kind: str, payload: dict | None = None) -> None:
        ep = self.get(episode_id)
        if ep is None:
            return
        ep.setdefault("events", []).append({"ts": time.time(), "kind": kind, "payload": payload or {}})
        ep["events"] = ep["events"][-self.MAX_EVENTS:]
        self._save(ep)

    def events(self, episode_id: str, limit: int = 30) -> list[dict]:
        return ((self.get(episode_id) or {}).get("events") or [])[-limit:]

    def recent_situations(self, since_ts: float) -> set[str]:
        return {e["situation_id"] for e in self.list() if e["created"] >= since_ts and e["status"] != "rejected"}


def make_store(cfg) -> "Store | VaultStore":
    if (cfg.get("state.backend") or "sqlite") == "vault":
        from .vault import make_vault
        return VaultStore(make_vault(cfg.get("vault") or {}, cfg.root))
    return Store(cfg.path(cfg["database"]))
