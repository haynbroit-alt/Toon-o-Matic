import json

from conftest import needs_ffmpeg
from toonomatic import ffmpeg
from toonomatic.learning import load_lessons
from toonomatic.pipeline import Pipeline
from toonomatic.review import apply_decision


@needs_ffmpeg
def test_end_to_end_mock(cfg):
    cfg.data["animation"]["renderer"] = "preview"
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=3)
    ep = pipe.store.get(eid)
    assert ep["status"] == "review", ep["info"].get("error")
    kinds = {(d["lang"], d["kind"]) for d in ep["info"]["deliverables"]}
    assert kinds == {("fr", "episode"), ("fr", "short"), ("en", "episode"), ("en", "short")}
    for d in ep["info"]["deliverables"]:
        assert ffmpeg.duration(d["video"]) > 1
    d = pipe.epdir(eid)
    assert (d / "fr" / "subs.en.srt").exists() and (d / "en" / "subs.fr.srt").exists()
    for lang in ("fr", "en"):
        pkg = d / "package" / lang
        yt = json.loads((pkg / "youtube.json").read_text())
        assert yt["containsSyntheticMedia"] and yt["title"] and (pkg / "thumbnail.jpg").exists()
        tk = json.loads((pkg / "tiktok.json").read_text())
        assert tk["ai_generated"] and len(tk["caption"]) <= 2200 and (pkg / "tiktok_cover.jpg").exists()
    script = json.loads((d / "fr" / "script_timed.json").read_text())
    assert all(ln["visemes"] for sc in script["scenes"] for ln in sc["lines"])
    report = json.loads((d / "qa_report.json").read_text())
    assert report["needs_human"] is True  # always_human_review par défaut

    # Refaire avec motif → leçon + régénération des dialogues uniquement
    apply_decision(cfg, pipe.store, eid, "redo", "Moins de répétitions dans les vannes", "script")
    assert "Moins de répétitions dans les vannes" in load_lessons(cfg)[-1]
    assert pipe.store.get(eid)["stages"] == ["premise"]
    assert pipe.resume() == [eid]
    assert pipe.store.get(eid)["status"] == "review"

    # Valider → publication (dry-run : manifeste)
    apply_decision(cfg, pipe.store, eid, "approve")
    res = pipe.publish(eid)
    assert res["dry_run"] and (d / "publish_manifest.json").exists()
    from toonomatic.continuity import load_facts
    assert load_facts(cfg)[-1]["episode"] == eid  # l'épisode validé nourrit la mémoire de la série
    pipe.publish(eid)
    assert sum(f["episode"] == eid for f in load_facts(cfg)) == 1  # pas de doublon
