"""Bureau d'écriture : scénario écrit hors de l'usine (copier-coller), sans clé."""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import needs_ffmpeg
from toonomatic.writing import desk

SIT = {"id": "code-wifi", "situation": "Le code wifi a changé et seul le tonton le connaît.",
       "cast": ["momo", "lina", "tonton"], "sets": ["rue", "salon"]}


def answer(cfg, n=None, **over):
    """Ce qu'un bon modèle de chat renverrait."""
    p = desk.plan(cfg, SIT)
    n = n or p["scenes"]
    fr = ["Tonton, le code wifi, s'il te plaît.", "Doucement, doucement. Un code, ça se mérite.",
          "Il veut de l'argent. Pour un mot de passe.", "C'est pas de l'argent, c'est un abonnement moral."]
    en = ["Uncle, the wifi code. Please.", "Easy, easy. A code must be earned.",
          "He wants money. For a password.", "It's not money. It's a moral subscription."]

    def lines(txt):
        who = ["momo", "tonton", "lina", "tonton"]
        return [{"character": who[i], "emotion": ["neutral", "smug", "sarcastic", "happy"][i],
                 "action": ["talk", "shrug", "facepalm", "talk_hands"][i], "text": t} for i, t in enumerate(txt)]
    data = {"logline": "Le tonton monnaie le code wifi.", "running_gag": "« Doucement, doucement »",
            "scenes": [{"set": "rue", "characters": ["momo", "tonton"], "purpose": f"étape {i + 1}"} for i in range(n)],
            "fr": {"title": "Le prix du wifi", "scenes": [{"summary": f"scène {i + 1}", "funniness": 5 + (i == 1) * 4,
                                                           "lines": lines(fr)} for i in range(n)],
                   "meta": {"youtube_title": "Le prix du wifi", "youtube_description": "Tonton négocie.",
                            "tags": ["humour"], "tiktok_caption": "Un code, ça se mérite", "hashtags": ["#humour"],
                            "thumbnail_text": "LE CODE"}},
            "en": {"title": "The Price of Wifi", "scenes": [{"summary": f"scene {i + 1}", "funniness": 6,
                                                             "lines": lines(en)} for i in range(n)]}}
    data.update(over)
    return data


def test_brief_contains_everything_a_chat_model_needs(cfg):
    b = desk.brief(cfg, SIT, ["Moins de répétitions"])
    for needle in ("Les Voisins du 3e", "CONTRADICTION", "travaille énormément pour ne pas avoir à travailler",
                   "ACTIONS ANIMÉES", "facepalm", "deadpan", "humour de rue", SIT["situation"],
                   "momo, lina, tonton", "rue, salon", "```json", '"fr"', '"en"', "Moins de répétitions"):
        assert needle in b, needle
    assert len(b) < 12000                       # tient dans un message de chat


def test_parse_accepts_messy_paste_and_repairs_soft_errors(cfg):
    d = answer(cfg)
    d["scenes"][0]["set"] = "lune"                                   # décor inventé
    d["fr"]["scenes"][0]["lines"][0]["emotion"] = "furieux"          # émotion hors liste
    d["en"]["scenes"][0]["lines"][1]["action"] = "moonwalk"          # geste hors liste
    raw = json.dumps(d, ensure_ascii=False, indent=1).replace('"lines": [', '"lines": [').rstrip("}") + ",}"
    pasted = "Voici l'épisode demandé !\n```json\n" + raw + "\n```\nBonne production 🎬"
    premise, scripts, metas, warnings = desk.parse(cfg, pasted, SIT)
    assert premise.beats[0].set in ("rue", "salon") and premise.source == "bureau"
    assert scripts["fr"].lines[0].emotion == "neutral" and scripts["en"].lines[1].action == "talk"
    assert sum(s.highlight for s in scripts["fr"].scenes) == 1 and scripts["fr"].scenes[1].highlight
    assert "lina" in premise.beats[0].characters                     # parle dans la scène → ajoutée
    assert scripts["fr"].title == "Le prix du wifi" and metas["fr"]["thumbnail_text"] == "LE CODE"
    assert any("décor" in w for w in warnings) and any("émotion" in w for w in warnings)


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("en"), "manquante"),
    (lambda d: d["en"]["scenes"].pop(), "scènes au lieu de"),
    (lambda d: d["fr"]["scenes"][0]["lines"].__setitem__(0, {"character": "kevin", "text": "Yo", "emotion": "happy",
                                                              "action": "talk"}), "kevin"),
    (lambda d: d["fr"]["scenes"][2].__setitem__("lines", []), "aucune réplique"),
])
def test_parse_explains_blocking_problems(cfg, mutate, expect):
    d = answer(cfg)
    mutate(d)
    with pytest.raises(desk.DeskError) as e:
        desk.parse(cfg, json.dumps(d), SIT)
    assert any(expect in p for p in e.value.problems), e.value.problems


def test_parse_detects_truncated_or_absent_json(cfg):
    with pytest.raises(desk.DeskError) as e:
        desk.parse(cfg, json.dumps(answer(cfg))[:400], SIT)
    assert "coupée" in e.value.problems[0] or "Aucun objet" in e.value.problems[0]
    with pytest.raises(desk.DeskError):
        desk.parse(cfg, "Désolé, je ne peux pas.", SIT)


@needs_ffmpeg
def test_import_runs_without_any_model_and_invents_nothing(cfg):
    from toonomatic.pipeline import Pipeline
    cfg.data["stickers"]["enabled"] = False
    pipe = Pipeline(cfg)
    calls = []
    # espion posé sur CETTE instance (un espion de classe capterait aussi les workers d'autres tests)
    real = pipe.llm.generate_json
    pipe.llm.generate_json = lambda task, *a, **k: (calls.append(task), real(task, *a, **k))[1]
    try:
        eid, warnings = pipe.import_script(SIT, json.dumps(answer(cfg)), "final")
        assert pipe.store.get(eid)["stages"] == ["premise", "script_fr", "script_en"]
        assert pipe.run(eid) == "review"
        d = pipe.epdir(eid)
        script = json.loads((d / "fr" / "script.json").read_text())
        assert script["scenes"][0]["lines"][0]["text"] == "Tonton, le code wifi, s'il te plaît."   # texte intact
        rep = json.loads((d / "director_report.json").read_text())
        assert rep["scores"]["histoire"] is None and rep["scores"]["son"] is not None
        assert "technique seulement" in rep["verdict"]
        assert not (d / "fr" / "subs.en.srt").exists()                 # pas de fausse traduction
        assert json.loads((d / "package" / "fr" / "youtube.json").read_text())["title"].startswith("Le prix du wifi")
        from toonomatic.review import apply_decision
        apply_decision(cfg, pipe.store, eid, "approve")
        pipe.publish(eid)
        from toonomatic.continuity import aired, load_facts
        assert aired(cfg)[-1]["episode"] == eid and load_facts(cfg) == []   # journalisé, aucun fait inventé
    finally:
        pipe.llm.generate_json = real
    assert set(calls) <= {"sticker_caption"}, calls                    # aucun appel d'écriture ni de critique


@needs_ffmpeg
def test_desk_api_roundtrip(cfg):
    from toonomatic.command.server import make_handler
    from toonomatic.studio import Factory
    c = cfg.derive({"production": {"default_tier": "draft"}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, Factory(c), "secret"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer secret"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        st, b = call("/api/desk/brief", {"situation_id": "code-wifi"})
        assert st == 200 and "```json" in b["brief"] and b["plan"]["scenes"] >= 4
        st, bad = call("/api/desk/import", {"situation": b["situation"], "text": "pas du json", "tier": "draft"})
        assert st == 422 and bad["problems"]
        st, ok = call("/api/desk/import", {"situation": b["situation"], "tier": "draft",
                                           "text": json.dumps(answer(c, b["plan"]["scenes"]))})
        assert st == 202 and ok["gpu"] is False
        for _ in range(240):
            ep = call(f"/api/episodes/{ok['episode_id']}")[1]
            if ep["status"] in ("review", "failed"):
                break
            time.sleep(0.5)
        assert ep["status"] == "review", ep["info"].get("error")
        assert ep["info"]["written_by"] == "bureau" and ep["media"]
    finally:
        srv.shutdown()
