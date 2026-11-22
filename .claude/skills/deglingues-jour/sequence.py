"""Séquence du jour des Déglingués : `brief <id>` affiche la consigne d'écriture,
`produire <id> <script.json> <sortie>` contrôle le script, fabrique la séquence et affiche l'état."""
import json
import sys
from pathlib import Path

import yaml

from toonomatic.config import load_config
from toonomatic.pipeline import Pipeline
from toonomatic.studio import Factory

BANK = Path("series/les-deglingues/situations.yaml")


def main() -> int:
    mode, sid = sys.argv[1], sys.argv[2]
    sit = next((s for s in yaml.safe_load(BANK.read_text(encoding="utf-8")) if s["id"] == sid), None)
    if sit is None:
        print(f"thème inconnu : {sid} (l'ajouter à {BANK})")
        return 2
    if mode == "brief":
        cfg = load_config("config/deglingues.yaml")
        print(Factory(cfg).desk_brief(situation_text=sit["situation"])["brief"])
        return 0
    text, out = Path(sys.argv[3]).read_text(encoding="utf-8"), Path(sys.argv[4]).resolve()
    cfg = load_config("config/deglingues.yaml", {
        "workdir": str(out / "runs"), "database": str(out / "runs" / "toonomatic.sqlite"),
        "vault": {"backend": "local", "path": str(out / "vault")},
        "production": {"tiers": {"final": {"overrides": {"animation": {"resolution": [1280, 720]}}}}}})
    check = Factory(cfg).desk_check(sit, text)
    print("contrôle", json.dumps({k: check[k] for k in ("ok", "blocking", "to_review", "warnings")}, ensure_ascii=False))
    pipe = Pipeline(cfg)
    eid, _ = pipe.import_script(sit, text, "final")
    print("statut", pipe.run(eid))
    info = pipe.store.get(eid)["info"]
    report = json.loads((pipe.epdir(eid) / "director_report.json").read_text(encoding="utf-8"))
    print("épisode", eid, "voice_engine", info.get("voice_engine"), "notes", json.dumps(report["scores"], ensure_ascii=False))
    print("dossier", pipe.epdir(eid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
