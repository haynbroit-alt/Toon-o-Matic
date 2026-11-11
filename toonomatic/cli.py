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
    s.add_argument("--tier", help="niveau de production : draft | validation | final")

    s = sub.add_parser("promote", help="passer un épisode au niveau supérieur (garde le texte)")
    s.add_argument("episode_id")
    s.add_argument("tier", help="validation | final")
    s.add_argument("--now", action="store_true", help="produire tout de suite sur cette machine")

    s = sub.add_parser("serve", help="serveur de COMMAND + application mobile (PWA)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", 8080)))

    s = sub.add_parser("worker", help="machine du STUDIO : prend une commande dans la file, produit, s'arrête")
    s.add_argument("--loop", action="store_true", help="rester actif et surveiller la file")
    s.add_argument("--gpu", action="store_true", help="cette machine accepte les travaux GPU (niveau final)")

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
    sub.add_parser("brand-check", help="contrôle de marque : silhouettes (64 px) et couleurs signatures")
    s = sub.add_parser("stickers", help="extraire les stickers d'un épisode et reconstruire les packs")
    s.add_argument("episode_id", nargs="?", help="épisode source (sinon : seulement reconstruire les packs)")
    s.add_argument("--max", type=int, default=6)
    s = sub.add_parser("presence", help="décliner les scènes d'un épisode (scène, réaction, short, FR/EN)")
    s.add_argument("episode_id")
    s.add_argument("--scene", action="append", help="scène à décliner (répétable) ; défaut : les meilleures")
    s.add_argument("--plan", action="store_true", help="afficher seulement le plan chiffré, sans rien fabriquer")
    s = sub.add_parser("social-import", help="importer des mesures d'audience (CSV) dans la mémoire sociale")
    s.add_argument("csv")
    s.add_argument("--provenance", default="measured", choices=["measured", "declared", "estimated"])
    s = sub.add_parser("social-comments", help="importer des commentaires publics (CSV : asset_id,platform,text)")
    s.add_argument("csv")
    sub.add_parser("social", help="profil social des personnages (ce que le public en fait)")
    s = sub.add_parser("character", help="fiche d'un personnage : identité, histoire vécue, mémoire sociale")
    s.add_argument("character_id")
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
    from .store import make_store

    if a.cmd == "serve":
        from .command.server import serve
        serve(cfg, a.host, a.port)
        return 0
    if a.cmd == "worker":
        from .studio import run_worker
        n = run_worker(cfg, once=not a.loop, can_gpu=a.gpu)
        print(f"{n} commande(s) traitée(s)")
        return 0
    if a.cmd == "promote":
        pipe = Pipeline(cfg)
        pipe.promote(a.episode_id, a.tier)
        print(f"{a.episode_id} → {a.tier}" + ("" if a.now else " (en file : `toonomatic resume`)"))
        if a.now:
            print(pipe.run(a.episode_id))
        return 0

    if a.cmd in ("produce", "resume", "daily", "publish"):
        pipe = Pipeline(cfg)
        if a.cmd == "produce":
            ids = pipe.produce(a.count or cfg["cadence"]["episodes_per_day"], a.seed, tier=a.tier)
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

    store = make_store(cfg)
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
    elif a.cmd == "presence":
        from . import presence
        d = cfg.workdir / a.episode_id
        if a.plan:
            for x in presence.plan(cfg, d):
                print(f"{x['scene']}  score {x['score']:>5}  {', '.join(x['characters']):<22} "
                      f"pic : {x['peak']['character']} {x['peak']['emotion']}/{x['peak']['action']}  "
                      f"« {x['peak']['text'][:50]} »")
            return 0
        from .writing.llm import make_llm
        man = presence.build(cfg, make_llm(cfg["llm"]), d, a.episode_id, a.scene, with_stickers=False)
        for x in man["assets"]:
            print(f"{x['id']:<52} {x['duration']:>5.1f} s  {x['video']}")
    elif a.cmd in ("social-import", "social-comments"):
        from . import social
        n = social.import_metrics(cfg, a.csv, a.provenance) if a.cmd == "social-import" else \
            social.import_comments(cfg, a.csv)
        print(f"{n} ligne(s) importée(s)")
    elif a.cmd == "social":
        from . import social
        prof = social.profile(cfg)
        print(f"objets mesurés : {prof['assets_measured']} (seuil de décision : n ≥ {prof['min_samples']})")
        for x in prof["characters"]:
            print(f"  {x['character']:<8} engagement {x['engagement']:.3f}  n={x['n']}"
                  f"{'' if x['sufficient'] else '  (données insuffisantes)'}")
        for q in prof["quoted_lines"][:5]:
            print(f"  « {q['line']} » — {q['character']}, reprise {q['quotes']} fois (commentaires publics)")
        print("  partages WhatsApp privés : " + prof["not_measurable"]["whatsapp_private_shares"])
    elif a.cmd == "character":
        from .social import card
        print(json.dumps(card(cfg, a.character_id), ensure_ascii=False, indent=2))
    elif a.cmd == "brand-check":
        from .brand import check
        r = check(cfg)
        for x in r["silhouettes"]:
            print(f"silhouette {x['a']:>8} / {x['b']:<8} recouvrement {x['silhouette_iou']:.2f}"
                  f"{'  ⚠️ confondables' if x['confusable'] else ''}")
        for x in r["colors"]:
            print(f"couleur    {x['a']:>8} / {x['b']:<8} ΔE {x['delta_e']:>5}{'' if x['ok'] else '  ⚠️ trop proches'}")
        print(("✅ marque lisible" if r["ok"] else "❌ " + " ; ".join(r["problems"])) + f"\nplanche : {r['sheet']}")
        return 0 if r["ok"] else 1
    elif a.cmd == "stickers":
        from . import stickers
        from .writing.llm import make_llm
        if a.episode_id:
            new = stickers.extract(cfg, make_llm(cfg["llm"]), cfg.workdir / a.episode_id, a.episode_id, a.max)
            for e in new:
                print(f"+ {e['id']}  " + "  ".join(f"[{l}] {c}" for l, c in e["captions"].items()))
        for lang, d in stickers.build_packs(cfg).items():
            print(f"pack {lang} : {d}")
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
