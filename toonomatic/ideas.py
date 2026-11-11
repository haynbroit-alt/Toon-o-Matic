"""Déclencheur : banque de situations pondérée par les KPI + tendances web (flux RSS)."""
from __future__ import annotations

import logging
import random
import re
import time
import xml.etree.ElementTree as ET

import requests
import yaml

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def load_bank(cfg: Config) -> list[dict]:
    return yaml.safe_load(cfg.path(cfg["ideas"]["bank"]).read_text(encoding="utf-8")) or []


def fetch_trends(urls: list[str], limit: int = 10) -> list[str]:
    titles: list[str] = []
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "toonomatic/0.1"})
            r.raise_for_status()
            for item in ET.fromstring(r.content).iter("item"):
                t = (item.findtext("title") or "").strip()
                if t:
                    titles.append(t)
        except (requests.RequestException, ET.ParseError) as e:
            log.warning("flux tendances ignoré %s : %s", url, e)
    return titles[:limit]


def trend_situation(title: str) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "tendance"
    return {"id": f"trend-{slug}", "source": "trend",
            "situation": f"Les personnages découvrent la tendance du moment « {title} » et chacun "
                         "la comprend de travers. (Ne jamais mettre en scène ni nommer de personne réelle.)"}


def pick_situations(cfg: Config, store: Store, count: int, seed: int | None = None,
                    kpi_weights: dict[str, float] | None = None) -> list[dict]:
    rng = random.Random(seed)
    since = time.time() - 86400 * int(cfg["ideas"].get("avoid_repeat_days", 60))
    used = store.recent_situations(since)
    bank = [s for s in load_bank(cfg) if s["id"] not in used] or load_bank(cfg)
    trends = [trend_situation(t) for t in fetch_trends(cfg["ideas"].get("trend_feeds") or [])]
    trends = [t for t in trends if t["id"] not in used]
    picks: list[dict] = []
    if trends:  # au plus 1 tendance par fournée, le reste depuis la banque (plus sûr)
        picks.append(trends[0])
    weights = kpi_weights or {}
    pool = list(bank)
    while len(picks) < count and pool:
        w = [weights.get(s["id"], 1.0) for s in pool]
        choice = rng.choices(pool, weights=w, k=1)[0]
        pool.remove(choice)
        picks.append(choice)
    while len(picks) < count:  # banque épuisée : on réutilise (variation garantie par le LLM)
        picks.append(rng.choice(load_bank(cfg)))
    return picks
