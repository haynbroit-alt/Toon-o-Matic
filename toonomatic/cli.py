"""Interface en ligne de commande : `toonomatic <commande>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toonomatic", description="Usine de web-séries animées 3D FR/EN")
    p.add_argument("-c", "--config", help="fichier YAML qui surcharge config/default.yaml")
    p.add_argument("-s", "--series", help="dossier de série (défaut : config `series`)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("produce", help="produire N épisodes (idée → vidéo → revue)")
    s.add_argument("--count", type=int, help="nombre d'épisodes (défaut : cadence.episodes_per_day)")
    s.add_argument("--seed", type=int)
    s.add_argument("--minutes", type=float, help="surcharge la durée visée")

    sub.add_parser("resume", help="reprendre les épisodes en file / en échec / à refaire")
    sub.add_parser("daily", help="tâche planifiée : reprend la file puis produit la fournée du jour")
    sub.add_parser("list", help="lister les épisodes")
    s = sub.add_parser("show", help="détail d'un épisode")
    s.add_argument("episode_id")

    s = sub.add_parser("review", help="décision humaine")
    s.add_argument("decision", choices=["approve", "redo", "reject"])
    s.add_argument("episode_id")
    s.add_argument("--reason", default="")
    s.add_argument("--from", dest="redo_from", default="script", choices=["premise", "script", "voice", "render"])

    sub.add_parser("serve-review", help="interface web de validation")
    s = sub.add_parser("telegram-poll", help="traiter les clics Telegram (boucle)")
    s.add_argument("--interval", type=int, default=10)

    s = sub.add_parser("publish", help="publier un épisode validé")
    s.add_argument("episode_id")
    s = sub.add_parser("series-new", help="créer une nouvelle série (bible, distribution, mémoire)")
    s.add_argument("slug")
    s.add_argument("--from", dest="template", default="series/les-voisins", help="série modèle")
    s = sub.add_parser("plates", help="générer les décors 2D manquants de la série")
    s.add_argument("--force", action="store_true", help="régénérer aussi les plaques existantes")
    s.add_argument("sets", nargs="*", help="ids de décors (défaut : tous)")
    s = sub.add_parser("series-memory", help="afficher la mémoire de continuité de la série")

    s = sub.add_parser("kpi-import", help="importer des KPI (CSV) pour l'apprentissage")
    s.add_argument("csv")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    overrides = {}
    if getattr(a, "minutes", None):
        m = a.minutes
        overrides = {"episode": {"target_minutes": m, "min_minutes": m * 0.85, "max_minutes": m * 1.25}}
    if a.series:
        overrides["series"] = a.series
    if a.cmd == "series-new":
        return _series_new(a.slug, a.template)
    cfg = load_config(a.config, overrides)

    from .pipeline import Pipeline
    from .store import Store

    if a.cmd in ("produce", "resume", "daily", "publish"):
        pipe = Pipeline(cfg)
        if a.cmd == "produce":
            ids = pipe.produce(a.count or cfg["cadence"]["episodes_per_day"], a.seed)
        elif a.cmd == "resume":
            ids = pipe.resume()
        elif a.cmd == "daily":
            ids = pipe.resume() + pipe.produce(cfg["cadence"]["episodes_per_day"])
        else:
            print(json.dumps(pipe.publish(a.episode_id), ensure_ascii=False, indent=2))
            return 0
        for i in ids:
            ep = pipe.store.get(i)
            print(f"{i}\t{ep['status']}\trisque={ep['risk']}")
        return 0 if all(pipe.store.get(i)["status"] != "failed" for i in ids) else 1

    store = Store(cfg.path(cfg["database"]))
    if a.cmd == "list":
        for ep in store.list():
            print(f"{ep['id']}\t{ep['status']}\trisque={ep['risk']}\tétapes={len(ep['stages'])}")
    elif a.cmd == "show":
        print(json.dumps(store.get(a.episode_id), ensure_ascii=False, indent=2))
    elif a.cmd == "review":
        from .review import apply_decision
        ep = apply_decision(cfg, store, a.episode_id, a.decision, a.reason, a.redo_from)
        print(f"{ep['id']} → {ep['status']}")
        if ep["status"] == "approved":
            Pipeline(cfg).publish(ep["id"])
    elif a.cmd == "serve-review":
        from .review import serve
        serve(cfg, store)
    elif a.cmd == "telegram-poll":
        from .review import Telegram
        tg, offset = Telegram(cfg), 0
        while True:
            offset = tg.poll(cfg, store, offset)
            time.sleep(a.interval)
    elif a.cmd == "plates":
        from .animation.plates import ensure_plates
        for pth in ensure_plates(cfg, a.sets or None, a.force):
            print(pth)
    elif a.cmd == "series-memory":
        from .continuity import aired, load_facts
        for e in aired(cfg, 1000):
            print(f"📺 {e['episode']}  {e['title']}")
        for f in load_facts(cfg, 10_000):
            print(f"• [{f['kind']}] {f['fact']}  ({f['episode']})")
    elif a.cmd == "kpi-import":
        from .learning import import_kpis
        print(f"{import_kpis(store, a.csv)} lignes importées")
    return 0


def _series_new(slug: str, template: str) -> int:
    import re
    import shutil

    from .config import ROOT
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", slug):
        print("slug invalide (minuscules, chiffres, tirets)")
        return 2
    src, dst = ROOT / template, ROOT / "series" / slug
    if dst.exists():
        print(f"{dst} existe déjà")
        return 2
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.jsonl"))
    (dst / "memory").mkdir(exist_ok=True)
    (dst / "memory" / "continuity.jsonl").touch()
    sy = (dst / "series.yaml").read_text(encoding="utf-8")
    (dst / "series.yaml").write_text(re.sub(r"(?m)^slug: .*$", f"slug: {slug}", sy), encoding="utf-8")
    print(f"Série créée : {dst}\n→ éditez series.yaml (univers, ton, règles) et cast.yaml (personnages),\n"
          f"  puis : toonomatic -s series/{slug} produce --count 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
