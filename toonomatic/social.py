"""MÉMOIRE SOCIALE : ce que le public fait réellement des personnages.

Règle d'honnêteté : chaque chiffre porte sa PROVENANCE et sa taille d'échantillon.
  - measured  : relevé par une plateforme (export YouTube Studio / TikTok, API) ou observé
                (commentaires publics qui citent une réplique, installations de pack si l'outil
                de distribution les compte) ;
  - declared  : rapporté par un humain (« ce sticker a circulé dans 3 groupes ») ;
  - estimated : déduit par un modèle ou une hypothèse.
Les partages privés WhatsApp ne sont PAS mesurables : ils n'existent ici que déclarés ou estimés,
et seules les données `measured` pilotent les décisions automatiques.

Données (dans le Vault de la série) :
  memory/social_events.jsonl    un événement par mesure (asset, plateforme, métrique, valeur, provenance)
  memory/social_comments.jsonl  commentaires publics (pour repérer les répliques reprises)
  memory/social.json            profil agrégé, recalculé à la demande
Chaque objet publié porte un identifiant stable `<épisode>.<scène>.<type>.<langue>` (Presence Engine).
"""
from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
from itertools import combinations
from pathlib import Path

from .config import Config

PROVENANCES = ("measured", "declared", "estimated")
ENGAGEMENT = {"likes": 1.0, "comments": 2.0, "shares": 3.0, "saves": 2.0, "sticker_sends": 3.0}


def _mem(cfg: Config) -> Path:
    d = (cfg.series_dir or cfg.root) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _norm(text: str) -> str:
    """minuscules, sans accents ni ponctuation ni balises [rire] : pour comparer des citations."""
    t = unicodedata.normalize("NFKD", re.sub(r"\[[^\]]*\]", " ", text.lower()))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", t).split())


# --------------------------------------------------------------------------- import
def import_metrics(cfg: Config, csv_path: Path, default_provenance: str = "measured") -> int:
    """CSV : asset_id,platform,metric,value[,date][,provenance][,note]"""
    out = _mem(cfg) / "social_events.jsonl"
    n = 0
    with Path(csv_path).open(encoding="utf-8") as fh, out.open("a", encoding="utf-8") as dst:
        for row in csv.DictReader(fh):
            prov = (row.get("provenance") or default_provenance).strip()
            if prov not in PROVENANCES:
                raise ValueError(f"provenance inconnue « {prov} » (attendu : {', '.join(PROVENANCES)})")
            ev = {"asset_id": row["asset_id"].strip(), "platform": row.get("platform", "").strip(),
                  "metric": row["metric"].strip(), "value": float(row["value"]), "provenance": prov,
                  "date": row.get("date") or time.strftime("%Y-%m-%d"), "note": row.get("note", "")}
            dst.write(json.dumps(ev, ensure_ascii=False) + "\n")
            n += 1
    return n


def import_comments(cfg: Config, csv_path: Path) -> int:
    """CSV : asset_id,platform,text[,date] — commentaires PUBLICS uniquement."""
    out = _mem(cfg) / "social_comments.jsonl"
    n = 0
    with Path(csv_path).open(encoding="utf-8") as fh, out.open("a", encoding="utf-8") as dst:
        for row in csv.DictReader(fh):
            dst.write(json.dumps({"asset_id": row["asset_id"].strip(), "platform": row.get("platform", ""),
                                  "text": row["text"], "date": row.get("date", "")}, ensure_ascii=False) + "\n")
            n += 1
    return n


# --------------------------------------------------------------------------- catalogue des objets publiés
def asset_catalog(cfg: Config) -> dict[str, dict]:
    """asset_id → {characters, expressions, lines, scene, episode, kind, lang} (écrit par le Presence Engine)."""
    return {a["id"]: a for a in _jsonl(_mem(cfg) / "presence_assets.jsonl")}


# --------------------------------------------------------------------------- agrégation
def _mean(xs):
    return round(sum(xs) / len(xs), 4) if xs else None


def profile(cfg: Config) -> dict:
    sc = cfg.get("social") or {}
    min_n = int(sc.get("min_samples", 5))
    cat = asset_catalog(cfg)
    events = _jsonl(_mem(cfg) / "social_events.jsonl")
    comments = _jsonl(_mem(cfg) / "social_comments.jsonl")

    # métriques par objet et par provenance
    per: dict[str, dict[str, dict[str, float]]] = {}
    for ev in events:
        per.setdefault(ev["asset_id"], {}).setdefault(ev["provenance"], {})
        m = per[ev["asset_id"]][ev["provenance"]]
        m[ev["metric"]] = m.get(ev["metric"], 0.0) + ev["value"]

    def engagement(m: dict) -> float | None:
        views = m.get("views", 0)
        if views <= 0:
            return None
        return sum(m.get(k, 0) * w for k, w in ENGAGEMENT.items()) / views

    measured = {a: engagement(v["measured"]) for a, v in per.items() if "measured" in v}
    measured = {a: e for a, e in measured.items() if e is not None and a in cat}
    retention = {a: v["measured"]["avg_view_pct"] for a, v in per.items()
                 if "measured" in v and "avg_view_pct" in v["measured"] and a in cat}

    def bucket(keyfn):
        acc: dict = {}
        for a, e in measured.items():
            for k in keyfn(cat[a]):
                acc.setdefault(k, []).append(e)
        return acc

    def scored(acc, label):
        out = []
        for k, xs in acc.items():
            out.append({label: k, "engagement": _mean(xs), "n": len(xs), "provenance": "measured",
                        "sufficient": len(xs) >= min_n})
        return sorted(out, key=lambda x: -(x["engagement"] or 0))

    chars = scored(bucket(lambda a: a["characters"]), "character")
    exprs = scored(bucket(lambda a: [tuple(x) for x in a.get("expressions", [])]), "expression")
    pairs = scored(bucket(lambda a: ["+".join(p) for p in combinations(sorted(a["characters"]), 2)]), "pair")

    # répliques reprises dans les commentaires publics (observé = measured)
    names = {cid: [_norm(c["display"]), _norm(cid)] for cid, c in cfg.cast["characters"].items()}
    quoted: dict[str, dict] = {}
    mentions = {cid: 0 for cid in names}
    for cm in comments:
        t = _norm(cm["text"])
        for cid, ns in names.items():
            if any(re.search(rf"\b{re.escape(n)}\b", t) for n in ns if n):
                mentions[cid] += 1
        for ln in cat.get(cm["asset_id"], {}).get("lines", []):
            words = _norm(ln["text"]).split()
            # une réplique est « reprise » si un commentaire en contient au moins 4 mots consécutifs
            # (ou toute la réplique si elle est plus courte)
            k = min(4, len(words))
            if k and any(" ".join(words[i:i + k]) in t for i in range(len(words) - k + 1)):
                q = quoted.setdefault(ln["text"], {"line": ln["text"], "character": ln["character"], "quotes": 0,
                                                   "provenance": "measured"})
                q["quotes"] += 1

    # déclaratif / estimé : affichés, jamais utilisés pour décider
    side = {p: {} for p in ("declared", "estimated")}
    for a, v in per.items():
        for p in side:
            for metric, val in v.get(p, {}).items():
                side[p][metric] = side[p].get(metric, 0) + val

    weak = []
    if len(measured) >= max(4, min_n):
        cut = sorted(measured.values())[len(measured) // 4]
        for a, e in measured.items():
            if e <= cut and cat[a].get("summary"):
                weak.append({"asset": a, "summary": cat[a]["summary"], "engagement": round(e, 4),
                             "provenance": "measured"})
    prof = {"updated": time.strftime("%Y-%m-%d %H:%M"), "min_samples": min_n,
            "assets_measured": len(measured), "characters": chars, "expressions": exprs, "pairs": pairs,
            "mentions": [{"character": c, "comments": n, "provenance": "measured"} for c, n in
                         sorted(mentions.items(), key=lambda x: -x[1])],
            "quoted_lines": sorted(quoted.values(), key=lambda q: -q["quotes"]),
            "weak": weak[:5],
            "retention": {"mean": _mean(list(retention.values())), "n": len(retention), "provenance": "measured"},
            "not_measurable": {"whatsapp_private_shares": "non mesurable : uniquement déclaré ou estimé",
                               "declared": side["declared"], "estimated": side["estimated"]}}
    (_mem(cfg) / "social.json").write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    return prof


def load_profile(cfg: Config) -> dict | None:
    p = _mem(cfg) / "social.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# --------------------------------------------------------------------------- retour vers la production
def expression_weights(cfg: Config) -> dict[tuple, float]:
    """Bonus (0..2) pour les expressions qui ont fait réagir — uniquement sur données mesurées suffisantes."""
    prof = load_profile(cfg)
    if not prof:
        return {}
    ok = [e for e in prof["expressions"] if e["sufficient"] and e["engagement"]]
    if not ok:
        return {}
    top = max(e["engagement"] for e in ok)
    return {tuple(e["expression"]): round(2.0 * e["engagement"] / top, 2) for e in ok}


def bible_section(cfg: Config, lang: str) -> str:
    """Ce que le scénariste doit savoir — sans jamais réécrire le noyau des personnages."""
    prof = load_profile(cfg)
    if not prof:
        return ""
    disp = {k: v["display"] for k, v in cfg.cast["characters"].items()}
    parts = []
    q = [x for x in prof["quoted_lines"] if x["quotes"] >= 2][:3]
    if q:
        parts.append("Répliques que le public reprend (rappel possible, 1 seule par épisode, jamais à l'identique) : "
                     + " / ".join(f"{disp.get(x['character'], x['character'])} : « {x['line']} » ({x['quotes']} reprises)"
                                  for x in q))
    ex = [e for e in prof["expressions"] if e["sufficient"]][:3]
    if ex:
        parts.append("Expressions qui font réagir (à faire revenir) : " +
                     ", ".join(f"{disp.get(e['expression'][0], e['expression'][0])} {e['expression'][1]}/{e['expression'][2]}"
                               f" (n={e['n']})" for e in ex))
    pr = [p for p in prof["pairs"] if p["sufficient"]][:2]
    if pr:
        parts.append("Duos qui fonctionnent : " + ", ".join(
            " + ".join(disp.get(c, c) for c in p["pair"].split("+")) + f" (n={p['n']})" for p in pr))
    wk = prof.get("weak") or []
    if wk:
        parts.append("Situations qui ont peu touché le public (ne pas refaire telles quelles) : " +
                     " ; ".join(w["summary"][:80] for w in wk[:3]))
    if not parts:
        return ""
    return ("\nMÉMOIRE SOCIALE (données MESURÉES uniquement ; le noyau des personnages — désir, peur, "
            "contradiction — prime toujours sur ces tendances) :\n" + "\n".join(f"- {x}" for x in parts) + "\n")


def card(cfg: Config, cid: str) -> dict:
    """Fiche d'identité complète d'un personnage : ce qu'il EST, ce qu'il a VÉCU, ce que le public en FAIT."""
    from .continuity import load_facts
    c = cfg.cast["characters"][cid]
    prof = load_profile(cfg) or {}
    hist = _jsonl(_mem(cfg) / "characters" / f"{cid}.jsonl")
    return {
        "id": cid, "display": c["display"], "kernel": c.get("kernel", {}), "personality": c.get("personality", []),
        "catchphrases": c.get("catchphrases", {}), "signature": c.get("signature", {}), "look": c.get("look", ""),
        "facts": [f for f in load_facts(cfg, 10_000) if cid in f.get("characters", [])][-10:],
        "history": hist[-10:], "scenes_lived": len(hist),
        "social": {
            "engagement": next((x for x in prof.get("characters", []) if x["character"] == cid), None),
            "mentions": next((x for x in prof.get("mentions", []) if x["character"] == cid), None),
            "quoted_lines": [q for q in prof.get("quoted_lines", []) if q["character"] == cid][:5],
            "best_expressions": [e for e in prof.get("expressions", []) if e["expression"][0] == cid][:3],
        },
    }
