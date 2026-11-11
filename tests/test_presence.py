import csv
import json

from conftest import needs_ffmpeg
from toonomatic import ffmpeg, presence, social, stickers
from toonomatic.models import Line, Scene, Script
from toonomatic.pipeline import Pipeline
from toonomatic.review import apply_decision


def _scene():
    lines = [Line("momo", f"réplique {i}", "neutral", "talk", start=10 + 3 * i, end=12 + 3 * i) for i in range(25)]
    lines[12] = Line("momo", "C'est une facture ?!", "surprised", "shocked", start=46, end=47.5)
    return Scene("s02", "rue", ["momo", "lina"], "Momo découvre la facture", lines, start=10, end=86)


def test_windows_reaction_and_short():
    w = presence.windows(_scene())
    a, b = w["reaction"]
    assert b - a <= 10 and a <= 46 and b >= 47.5                   # la réaction contient le pic
    s0, s1 = w["short"]
    assert s0 == 10 and s1 - s0 <= 59.5                            # short ≤ 59 s…
    assert any(abs((s1 - 0.5) - (12 + 3 * i)) < 1e-6 for i in range(25))  # …coupé sur une fin de réplique
    assert w["scene"] == (10, 86)


def test_social_weights_change_sticker_choice():
    lines = [Line("momo", "Non.", "smug", "point"), Line("lina", "Oui.", "smug", "point")]
    s = Script("fr", "t", [Scene("s01", "rue", ["momo", "lina"], "x", lines)])
    assert stickers.select_moments(s, 1)[0]["character"] == "momo"
    assert stickers.select_moments(s, 1, weights={("lina", "smug", "point"): 2.0})[0]["character"] == "lina"


@needs_ffmpeg
def test_presence_engine_end_to_end_and_canon_only_after_approval(cfg):
    cfg.data["director"]["enabled"] = False
    cfg.data["stickers"]["enabled"] = False   # rendu 3D testé ailleurs
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=8)
    man = json.loads((pipe.epdir(eid) / "presence" / "manifest.json").read_text())
    assert len(man["scenes"]) == 2 and not man["committed"]
    kinds = {(a["scene"], a["kind"], a["lang"]) for a in man["assets"]}
    assert len(kinds) == 2 * 3 * 2                                  # 2 scènes × 3 formats × FR/EN
    for a in man["assets"]:
        d = ffmpeg.duration(a["video"])
        assert abs(d - a["duration"]) < 1.0
        if a["kind"] == "reaction":
            assert d <= 10.6
        if a["kind"] == "short":
            assert d <= 60
        assert a["id"] == f"{eid}.{a['scene']}.{a['kind']}.{a['lang']}"
    plan = json.loads((pipe.epdir(eid) / "presence" / "plan.json").read_text())
    assert len(plan) == len(json.loads((pipe.epdir(eid) / "fr" / "script.json").read_text())["scenes"])
    mem = cfg.series_dir / "memory"
    assert not (mem / "presence_assets.jsonl").exists()            # pas canon tant que non validé
    apply_decision(cfg, pipe.store, eid, "approve")
    pipe.publish(eid)
    assert json.loads((pipe.epdir(eid) / "presence" / "manifest.json").read_text())["committed"]
    assert len(social.asset_catalog(cfg)) == 12
    hist = [json.loads(x) for x in (mem / "characters" / f"{man['assets'][0]['characters'][0]}.jsonl")
            .read_text().splitlines()]
    assert hist and hist[0]["episode"] == eid
    pipe.publish(eid)                                              # idempotent : pas de doublon
    assert len(social.asset_catalog(cfg)) == 12


def test_social_memory_provenance_and_thresholds(cfg, tmp_path):
    cfg.data["social"]["min_samples"] = 3
    mem = cfg.series_dir / "memory"
    with (mem / "presence_assets.jsonl").open("w") as fh:
        for i in range(6):
            chars = ["momo", "lina"] if i < 4 else ["tonton"]
            fh.write(json.dumps({"id": f"e.s{i}.reaction.fr", "characters": chars, "summary": f"scène {i}",
                                 "expressions": [[chars[0], "surprised", "shocked"]],
                                 "lines": [{"character": chars[0], "text": "C'est une opportunité en or"}]}) + "\n")
    m = tmp_path / "m.csv"
    with m.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["asset_id", "platform", "metric", "value", "provenance"])
        for i in range(6):
            w.writerow([f"e.s{i}.reaction.fr", "tiktok", "views", 1000, "measured"])
            w.writerow([f"e.s{i}.reaction.fr", "tiktok", "likes", 200 if i < 4 else 10, "measured"])
        w.writerow(["e.s5.reaction.fr", "whatsapp", "sticker_sends", 5000, "declared"])  # non mesurable
    assert social.import_metrics(cfg, m) == 13
    c = tmp_path / "c.csv"
    with c.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["asset_id", "platform", "text"])
        w.writerow(["e.s0.reaction.fr", "tiktok", "MOMO 😂 « c'est une OPPORTUNITÉ en or »"])
        w.writerow(["e.s1.reaction.fr", "tiktok", "c'est une opportunité en or mdr"])
        w.writerow(["e.s2.reaction.fr", "tiktok", "Lina a raison"])
    social.import_comments(cfg, c)
    prof = social.profile(cfg)
    by = {x["character"]: x for x in prof["characters"]}
    assert by["momo"]["engagement"] > by["tonton"]["engagement"]   # le déclaratif WhatsApp ne compte pas
    assert by["momo"]["sufficient"] and not by["tonton"]["sufficient"]
    assert prof["not_measurable"]["declared"]["sticker_sends"] == 5000
    assert prof["quoted_lines"][0]["quotes"] == 2
    men = {x["character"]: x["comments"] for x in prof["mentions"]}
    assert men["momo"] == 1 and men["lina"] == 1
    w8 = social.expression_weights(cfg)
    assert ("momo", "surprised", "shocked") in w8 and ("tonton", "surprised", "shocked") not in w8
    sec = social.bible_section(cfg, "fr")
    assert "opportunité" in sec and "MESURÉES" in sec and "prime toujours" in sec
    card = social.card(cfg, "momo")
    assert card["kernel"]["contradiction"] and card["social"]["engagement"]["n"] == 4


def test_no_social_data_means_no_influence(cfg):
    assert social.bible_section(cfg, "fr") == "" and social.expression_weights(cfg) == {}
    prof = social.profile(cfg)
    assert prof["assets_measured"] == 0 and social.bible_section(cfg, "fr") == ""
