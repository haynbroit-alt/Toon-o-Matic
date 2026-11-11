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

    def recent_situations(self, since_ts: float) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT situation_id FROM episodes WHERE created>=? AND status!='rejected'",
                             (since_ts,))
            return {r["situation_id"] for r in rows}
