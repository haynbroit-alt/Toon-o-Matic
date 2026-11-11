"""APPRENDRE / RÉVISER : leçons issues des revues humaines et pondérations issues des KPI."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from .config import Config
from .store import Store


def load_lessons(cfg: Config) -> list[str]:
    p = cfg.path(cfg["learning"]["lessons_file"])
    if not p.exists():
        return []
    return [ln[2:].strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]


def add_lesson(cfg: Config, text: str, source: str = "review") -> None:
    p = cfg.path(cfg["learning"]["lessons_file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# Leçons apprises\n\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"- {text.strip()} _(source: {source}, {dt.date.today().isoformat()})_\n")


def import_kpis(store: Store, csv_path: Path) -> int:
    """CSV attendu : episode_id,lang,views,avg_view_pct,likes,shares (export YouTube Studio adapté)."""
    n = 0
    with Path(csv_path).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ep = store.get(row["episode_id"])
            if not ep:
                continue
            kpis = ep["info"].get("kpis", {})
            kpis[row.get("lang", "fr")] = {k: float(v) for k, v in row.items()
                                           if k not in ("episode_id", "lang") and v not in (None, "")}
            store.update(row["episode_id"], info={"kpis": kpis})
            n += 1
    return n


def kpi_weights(store: Store) -> dict[str, float]:
    """Situations dont les épisodes retiennent le mieux l'audience → tirées plus souvent."""
    scores: dict[str, list[float]] = {}
    for ep in store.list():
        for k in ep["info"].get("kpis", {}).values():
            if "avg_view_pct" in k:
                scores.setdefault(ep["situation_id"], []).append(k["avg_view_pct"])
    if not scores:
        return {}
    mean = sum(sum(v) / len(v) for v in scores.values()) / len(scores) or 1.0
    return {sid: max(0.2, min(3.0, (sum(v) / len(v)) / mean)) for sid, v in scores.items()}
