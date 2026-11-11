import json
import threading
import time
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from toonomatic import director
from toonomatic.models import Script, load_json
from toonomatic.pipeline import Pipeline
from toonomatic.store import VaultStore
from toonomatic.studio import Factory, run_worker
from toonomatic.vault import LocalVault, claim, enqueue, pull_tree, push_tree, release
from toonomatic.writing.llm import MockLLM


# --------------------------------------------------------------------------- VAULT
def test_vault_sync_only_moves_what_changed(tmp_path):
    v = LocalVault(tmp_path / "v")
    src = tmp_path / "src"
    (src / "fr").mkdir(parents=True)
    (src / "fr" / "big.mp4").write_bytes(b"x" * 100_000)
    (src / "script.json").write_text("{}")
    assert push_tree(v, src, "episodes/e1/files")["bytes_sent"] == 100_002
    assert push_tree(v, src, "episodes/e1/files")["bytes_sent"] == 0          # rien n'a changé
    (src / "script.json").write_text('{"a": 1}')
    assert push_tree(v, src, "episodes/e1/files")["bytes_sent"] == 8           # seulement le petit fichier
    dst = tmp_path / "dst"
    got = pull_tree(v, "episodes/e1/files", dst, include=["*.json"])           # l'étape n'a besoin que du texte
    assert (dst / "script.json").exists() and not (dst / "fr" / "big.mp4").exists() and got["bytes_received"] == 8
    assert pull_tree(v, "episodes/e1/files", dst, include=["*.json"])["bytes_received"] == 0


def test_queue_claim_lock_release(tmp_path):
    v = LocalVault(tmp_path / "v")
    enqueue(v, {"episode_id": "e1", "action": "run", "needs_gpu": True})
    assert claim(v, "cpu-box", can_gpu=False) is None                           # pas de GPU → pas de travail final
    key, job = claim(v, "gpu-box", can_gpu=True)
    assert job["episode_id"] == "e1" and v.list("queue/") == []
    enqueue(v, {"episode_id": "e1", "action": "run"})
    assert claim(v, "other", can_gpu=True) is None                              # épisode verrouillé
    release(v, "e1")
    assert claim(v, "other", can_gpu=True)[1]["episode_id"] == "e1"


def test_vault_store_roundtrip(tmp_path):
    st = VaultStore(LocalVault(tmp_path / "v"))
    st.create("e1", "sit")
    st.update("e1", stage_done="premise", info={"tier": "draft"}, status="review", risk=0.2)
    st.log("e1", "stage", {"stage": "premise"})
    ep = st.get("e1")
    assert ep["stages"] == ["premise"] and ep["info"]["tier"] == "draft" and ep["events"][-1]["kind"] == "stage"
    assert [e["id"] for e in st.list("review")] == ["e1"]


# --------------------------------------------------------------------------- niveaux
@needs_ffmpeg
def test_tiers_draft_then_promote_keeps_text_and_voices(cfg):
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=5, tier="draft")
    ep = pipe.store.get(eid)
    assert ep["status"] == "review" and ep["info"]["tier"] == "draft"           # brouillon : jamais publié tel quel
    assert not (pipe.epdir(eid) / "package").exists()                           # pas de kit de publication en brouillon
    script_before = (pipe.epdir(eid) / "fr" / "script.json").read_text()
    wav = sorted((pipe.epdir(eid) / "fr" / "audio").glob("*.wav"))[0]
    draft_mtime = wav.stat().st_mtime_ns
    pipe.promote(eid, "validation")                                             # voix brouillon (Piper) → vraies voix
    assert pipe.run(eid) == "review"
    assert wav.stat().st_mtime_ns != draft_mtime                                # moteur différent → voix refaites
    val_mtime = wav.stat().st_mtime_ns
    pipe.promote(eid, "final")
    assert pipe.run(eid) == "review"
    assert (pipe.epdir(eid) / "fr" / "script.json").read_text() == script_before  # texte jamais réécrit
    assert wav.stat().st_mtime_ns == val_mtime                                  # même moteur vocal → voix gardées
    assert (pipe.epdir(eid) / "package" / "fr" / "youtube.json").exists()
    assert pipe.store.get(eid)["info"]["tier_history"] == ["draft", "validation"]


# --------------------------------------------------------------------------- réalisateur
@needs_ffmpeg
def test_director_rewrites_weak_lines_in_each_language(cfg, monkeypatch):
    calls = {"n": 0}
    real = MockLLM.generate_json

    def fake(self, task, system, user, schema):
        if task == "director_review":
            calls["n"] += 1
            if calls["n"] <= 2:  # 1re relecture (FR puis EN) : la réplique #1 est faible
                return {"histoire": 4, "personnages": 8, "notes": "chute faible", "fixes": [
                    {"line": 1, "criterion": "histoire", "instruction": "rendre la vanne plus sèche"}]}
        return real(self, task, system, user, schema)

    monkeypatch.setattr(MockLLM, "generate_json", fake)
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=6)
    d = pipe.epdir(eid) / "fr"
    script = Script.from_dict(load_json(d / "script.json"))
    assert script.lines[1].text.endswith("[rire]")                               # réplique réécrite
    report = load_json(pipe.epdir(eid) / "director_report.json")
    assert report["verdict"] == "ok" and set(report["scores"]) == set(director.CRITERIA)
    fixes = [e for e in pipe.store.get(eid)["info"]["director"]["rounds"][0]["fixes"]]
    assert {f["action"] for f in fixes} == {"rewrite_line"} and len(fixes) == 2   # 1 par langue


@needs_ffmpeg
def test_targeted_fix_redoes_only_one_voice_and_one_scene(cfg):
    cfg.data["director"]["enabled"] = False
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=7)
    d = pipe.epdir(eid) / "fr"
    wavs = {p.name: p.stat().st_mtime_ns for p in (d / "audio").glob("*.wav")}
    mp4s = {p.name: p.stat().st_mtime_ns for p in (d / "render").glob("*.mp4")}
    en_mp4s = {p.name: p.stat().st_mtime_ns for p in (pipe.epdir(eid) / "en" / "render").glob("*.mp4")}
    pipe.apply_fixes(eid, [{"action": "regen_voice", "lang": "fr", "line": 0, "why": "test"}])
    assert pipe.run(eid) == "review"
    changed_w = [n for n, t in wavs.items() if (d / "audio" / n).stat().st_mtime_ns != t]
    changed_v = [n for n, t in mp4s.items() if (d / "render" / n).stat().st_mtime_ns != t]
    assert changed_w == [n for n in wavs if n.startswith("0000_")]             # une seule voix refaite
    assert changed_v == ["s01.mp4"]                                             # une seule scène re-rendue
    assert all((pipe.epdir(eid) / "en" / "render" / n).stat().st_mtime_ns == t for n, t in en_mp4s.items())


def test_sound_check_flags_silent_and_too_fast_lines(cfg, tmp_path):
    def wav(path, secs, amp):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"".join(int(amp * (1 if i % 20 < 10 else -1)).to_bytes(2, "little", signed=True)
                                   for i in range(int(8000 * secs))))
        return str(path)
    from toonomatic.models import Line, Scene
    lines = [Line("momo", "Bonjour tout le monde", audio=wav(tmp_path / "a.wav", 1.5, 9000)),
             Line("momo", "Bonjour", audio=wav(tmp_path / "b.wav", 1.0, 10)),
             Line("momo", "Une très longue réplique dite beaucoup trop vite pour être comprise",
                  audio=wav(tmp_path / "c.wav", 1.0, 9000))]
    score, fixes = director.check_sound(cfg, Script("fr", "t", [Scene("s01", "rue", ["momo"], "x", lines)]))
    assert {f["line"] for f in fixes} == {1, 2} and score < 6


# --------------------------------------------------------------------------- usine cloud
@needs_ffmpeg
def test_cloud_order_runs_on_a_fresh_disposable_worker(cfg, tmp_path):
    cloudcfg = cfg.derive({"state": {"backend": "vault"}, "studio": {"cpu_launcher": "none"},
                           "production": {"default_tier": "draft"}})
    f = Factory(cloudcfg)
    res = f.order(situation_text="Le tonton vend la même voiture à deux voisins le même jour.")
    eid = res["episode_id"]
    assert res["launcher"] == "queued" and res["gpu"] is False
    # une machine NEUVE (autre disque) prend la commande, produit, renvoie au vault, s'arrête
    worker_cfg = cloudcfg.derive({"workdir": str(tmp_path / "fresh-machine")})
    assert run_worker(worker_cfg, once=True) == 1
    ep = f.store.get(eid)
    assert ep["status"] == "review" and ep["info"]["tier"] == "draft"
    assert f.vault.get_bytes(f"episodes/{eid}/files/fr/script.json")             # le résultat vit dans le vault
    assert f.vault.list("locks/") == []
    # GPU jamais demandé tant que le niveau final n'est pas commandé
    out = f.decide(eid, "promote")
    assert out["gpu"] is False                                                   # draft → validation : CPU
    assert run_worker(worker_cfg, once=True) == 1
    out = f.decide(eid, "promote")
    assert out["gpu"] is True                                                    # validation → final : GPU
    assert run_worker(worker_cfg, once=True, can_gpu=False) == 0                 # une machine CPU ne le prend pas


# --------------------------------------------------------------------------- COMMAND (API + PWA)
@needs_ffmpeg
def test_command_server_api_auth_order_and_streaming(cfg):
    from toonomatic.command.server import make_handler
    c = cfg.derive({"production": {"default_tier": "draft"}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, Factory(c), "secret"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(path, data=None, token="secret", headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {}),
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    try:
        assert call("/")[0] == 200 and b"Toon-o-Matic" in call("/")[1]           # PWA servie
        assert call("/manifest.webmanifest")[0] == 200
        assert call("/api/episodes", token=None)[0] == 401                       # jeton obligatoire
        assert call("/api/login", {"token": "faux"}, token=None)[0] == 401
        st, _, hdr = call("/api/login", {"token": "secret"}, token=None)
        assert st == 200 and "HttpOnly" in hdr["Set-Cookie"]
        st, body, _ = call("/api/orders", {"situation_id": "code-wifi", "tier": "draft"})
        assert st == 202
        eid = json.loads(body)["episode_id"]
        for _ in range(240):                                                     # worker « inline » sur le serveur
            ep = json.loads(call(f"/api/episodes/{eid}")[1])
            if ep["status"] in ("review", "failed"):
                break
            time.sleep(0.5)
        assert ep["status"] == "review", ep["info"].get("error")
        assert ep["director_report"]["scores"] and ep["next_tier"] == "validation"
        src = ep["media"][0]["src"]
        st, chunk, hdr = call(src, headers={"Range": "bytes=0-99"})               # lecture en streaming
        assert st == 206 and len(chunk) == 100 and hdr["Accept-Ranges"] == "bytes"
        assert call(f"/api/episodes/{eid}/media/../../etc/passwd")[0] in (400, 404)
        st, body, _ = call(f"/api/episodes/{eid}/decision", {"decision": "promote"})
        assert st == 202 and json.loads(body)["gpu"] is False
    finally:
        srv.shutdown()
