"""TOON CORE : vérificateur sans IA, quatre états distincts, voix gratuites."""
import json
import os
import stat
import wave

import pytest

from conftest import needs_ffmpeg
from test_desk import SIT, answer
from toonomatic.writing import checker, desk


def parsed(cfg, mutate=None):
    d = answer(cfg)
    if mutate:
        mutate(d)
    premise, scripts, _, _ = desk.parse(cfg, json.dumps(d), SIT)
    return premise, scripts


def topics(rep, level):
    return {f["topic"] for f in rep["findings"] if f["level"] == level}


def test_checker_passes_a_sound_script(cfg):
    rep = checker.check(cfg, *parsed(cfg))
    assert rep["ok"] and rep["blocking"] == 0 and "sans IA" in rep["scope"]


def test_checker_catches_wrong_language_and_copied_versions(cfg):
    def french_everywhere(d):
        d["en"] = json.loads(json.dumps(d["fr"]))
    rep = checker.check(cfg, *parsed(cfg, french_everywhere))
    assert not rep["ok"] and "langue" in topics(rep, "bloquant")


def test_checker_catches_identity_cast_voice_and_flat_acting(cfg):
    def spoil(d):
        for sc in d["fr"]["scenes"]:
            for ln in sc["lines"]:
                ln["emotion"], ln["action"] = "neutral", "talk"            # jeu plat
                if ln["character"] == "lina":
                    ln["character"] = "momo"                               # Lina ne parle plus
        d["fr"]["scenes"][0]["lines"][0] = {"character": "momo", "emotion": "neutral", "action": "talk",
                                            "text": "Je connais quelqu'un. " + "et encore un mot " * 12}
    rep = checker.check(cfg, *parsed(cfg, spoil))
    assert rep["ok"]                                                        # rien de bloquant…
    assert {"identité", "distribution", "voix", "jeu"} <= topics(rep, "à revoir")   # …mais tout est signalé
    msgs = " ".join(f["message"] for f in rep["findings"])
    assert "Tonton Rachid" in msgs and "Lina est annoncé" in msgs


def test_checker_forbidden_terms_and_deja_vu(cfg):
    from toonomatic.continuity import _path
    cfg.series["forbidden_terms"] = [{"term": "abonnement moral", "reason": "test"}]
    p = _path(cfg).parent / "episodes.jsonl"
    p.write_text(json.dumps({"episode": "e0", "title": "Déjà fait", "logline": "Le tonton monnaie le code wifi."}) + "\n")
    rep = checker.check(cfg, *parsed(cfg))
    assert not rep["ok"] and "continuité" in topics(rep, "bloquant")
    assert any("Déjà fait" in f["message"] for f in rep["findings"])


def test_import_refuses_blocking_script(cfg):
    from toonomatic.pipeline import Pipeline
    d = answer(cfg)
    d["en"] = json.loads(json.dumps(d["fr"]))
    with pytest.raises(desk.DeskError) as e:
        Pipeline(cfg).import_script(SIT, json.dumps(d), "draft")
    assert any("langue" in p or "même texte" in p or "semble écrite" in p for p in e.value.problems)


@needs_ffmpeg
def test_four_milestones_are_never_confused(cfg):
    from toonomatic.pipeline import Pipeline
    from toonomatic.review import apply_decision
    from toonomatic.studio import Factory
    cfg.data["stickers"]["enabled"] = False
    cfg.data["episode"].update(target_minutes=1, min_minutes=0.7, max_minutes=1.3)   # durée du scénario d'essai
    f = Factory(cfg)
    pipe = Pipeline(cfg)
    eid, _ = pipe.import_script(SIT, json.dumps(answer(cfg, 4)), "final")
    st = lambda: [m["state"] for m in f.milestones(f.store.get(eid))]      # noqa: E731
    assert st() == ["ok", "pending", "pending", "unknown"]                 # écrit, mais rien d'autre
    pipe.run(eid)
    ms = f.milestones(f.store.get(eid))
    assert [m["state"] for m in ms] == ["ok", "warn", "pending", "unknown"]
    assert "bips" in ms[1]["detail"] and "seul un humain" in ms[2]["detail"]   # produit ≠ regardable
    apply_decision(cfg, pipe.store, eid, "approve")
    pipe.publish(eid)
    assert st()[2:] == ["ok", "unknown"]                                   # regardable ≠ apprécié
    from toonomatic import social
    (cfg.series_dir / "m.csv").write_text(
        "asset_id,platform,metric,value\n" + "".join(
            f"{a},tiktok,{m},{v}\n" for a in list(social.asset_catalog(cfg))[:1] for m, v in (("views", 2000), ("likes", 300))))
    social.import_metrics(cfg, cfg.series_dir / "m.csv")
    last = f.milestones(f.store.get(eid))[3]
    assert last["state"] == "ok" and "mesuré" in last["detail"]


@needs_ffmpeg
def test_demo_text_is_flagged_as_fake(cfg):
    from toonomatic.pipeline import Pipeline
    from toonomatic.studio import Factory
    cfg.data["stickers"]["enabled"] = False
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=2, tier="draft")
    m = Factory(cfg).milestones(pipe.store.get(eid))
    assert m[0]["state"] == "fail" and "FACTICE" in m[0]["detail"]


@needs_ffmpeg
def test_piper_voice_per_character_with_fallbacks(cfg, tmp_path):
    from toonomatic.voice.tts import MockTTS, PiperTTS, TTSError, make_tts
    fake = tmp_path / "piper"   # faux binaire : écrit un WAV 22050 Hz et journalise ses arguments
    fake.write_text("#!/bin/sh\necho \"$@\" >> " + str(tmp_path / "calls.log") + "\n"
                    "while [ \"$1\" != \"--output_file\" ]; do shift; done\n"
                    "ffmpeg -v error -y -f lavfi -i sine=frequency=200:sample_rate=22050:duration=1 -ac 1 \"$2\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    for n in ("fr.onnx", "tom.onnx"):
        (tmp_path / n).write_bytes(b"x")
    conf = {"provider": "piper", "sample_rate": 16000, "fallback": "mock",
            "piper": {"executable": str(fake), "models": {"fr": str(tmp_path / "fr.onnx")}}}
    tts = make_tts(conf)
    assert isinstance(tts, PiperTTS)
    out = tts.synthesize("Salut [rire]", "fr", {"piper_model": str(tmp_path / "tom.onnx")}, "angry", tmp_path / "a.wav")
    with wave.open(str(out)) as w:
        assert w.getframerate() == 16000
    tts.synthesize("Salut", "fr", {"piper_model": str(tmp_path / "absente.onnx"), "mock_pitch": 120}, "neutral",
                   tmp_path / "b.wav")
    log = (tmp_path / "calls.log").read_text().splitlines()
    assert "tom.onnx" in log[0] and "--length_scale 0.9" in log[0]         # voix du personnage, débit « colère »
    assert "fr.onnx" in log[1]                                             # voix absente → voix de la langue
    with pytest.raises(TTSError):
        tts.synthesize("Hello", "en", {}, "neutral", tmp_path / "c.wav")   # pas de voix anglaise
    conf["piper"]["models"] = {"fr": str(tmp_path / "manquante.onnx")}
    assert isinstance(make_tts(conf), MockTTS)                             # voix non téléchargées → bips, sans planter
