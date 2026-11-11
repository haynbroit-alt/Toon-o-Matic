"""Salle d'écriture open source : connecteur compatible OpenAI, relais entre fournisseurs, routes par langue."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from toonomatic.writing import llm as L
from toonomatic.writing.showrunner import Showrunner

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title"],
          "properties": {"title": {"type": "string"}}}


def serve(behaviour):
    """behaviour(n, body) -> (status, headers, payload) ; n = numéro de l'appel."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"body": body, "auth": self.headers.get("Authorization")})
            status, headers, payload = behaviour(len(calls), body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1", calls


def reply(obj):
    return 200, {}, {"choices": [{"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}}]}


def test_schema_fallback_and_self_repair(monkeypatch):
    def b(n, body):
        if n == 1:
            assert body["response_format"]["type"] == "json_schema"
            return 400, {}, {"error": "json_schema non géré"}
        if n == 2:
            assert body["response_format"]["type"] == "json_object"
            return reply("Voici : ```json\n{\"titre\": \"oups\"}\n```")      # mauvais champ
        assert "invalide" in body["messages"][-1]["content"]
        return reply({"title": "Le code wifi"})
    srv, url, calls = serve(b)
    monkeypatch.setenv("K1", "secret")
    out = L.OpenAICompatLLM("x", {"base_url": url, "model": "m", "api_key_env": "K1"}).generate_json(
        "title", "sys", "user", SCHEMA)
    srv.shutdown()
    assert out == {"title": "Le code wifi"} and len(calls) == 3 and calls[0]["auth"] == "Bearer secret"


def test_chain_relays_on_quota_and_routes_by_language(monkeypatch):
    a, url_a, calls_a = serve(lambda n, body: (429, {"retry-after": "3600"}, {}))
    b, url_b, calls_b = serve(lambda n, body: reply({"title": "de B"}))
    monkeypatch.setenv("KA", "a")
    monkeypatch.setenv("KB", "b")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    cfg = {"provider": "auto", "chain": ["a", "b", "sans_cle"], "routes": {"en": ["b", "a"]},
           "endpoints": {"a": {"base_url": url_a, "model": "m", "api_key_env": "KA"},
                         "b": {"base_url": url_b, "model": "m", "api_key_env": "KB"},
                         "sans_cle": {"base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key_env": "ABSENTE"}}}
    assert L.available_endpoints(cfg) == ["a", "b"] and L.describe(cfg) == "a → b"
    chain = L.make_llm(cfg)
    assert chain.generate_json("title", "s", 'x <context>{"lang": "fr"}</context>', SCHEMA) == {"title": "de B"}
    assert len(calls_a) == 1                                  # A a répondu « quota » → relais vers B
    chain.generate_json("title", "s", 'x <context>{"lang": "en"}</context>', SCHEMA)
    assert len(calls_a) == 1 and len(calls_b) == 2            # EN : B d'abord, A jamais sollicité
    a.shutdown(); b.shutdown()


def test_no_key_means_demo_or_explicit_error(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = {"provider": "auto", "endpoints": {"g": {"base_url": "http://x/v1", "model": "m", "api_key_env": "ABSENTE"}}}
    assert "aucune clé" in L.llm_problem(cfg)
    with pytest.raises(L.LLMError):
        L.make_llm(cfg)
    assert isinstance(L.make_llm({**cfg, "fallback": "mock"}), L.MockLLM)


def test_showrunner_writes_through_open_model_server(cfg, monkeypatch):
    """Le scénariste complet (prémisse + script) à travers HTTP, comme avec Groq / Mistral / Ollama."""
    mock = L.MockLLM(lines_per_scene=3)

    def b(n, body):
        task = body["response_format"]["json_schema"]["name"]
        user = body["messages"][1]["content"].split("\n\nRéponds UNIQUEMENT")[0]
        out = mock.generate_json(task, "", user, {})
        if task == "premise":
            out["beats"][0]["set"] = "lune"                    # un modèle plus faible invente un décor
        return reply(out)
    srv, url, calls = serve(b)
    monkeypatch.setenv("KO", "k")
    chain = L.make_llm({"provider": "chain", "endpoints": {"o": {"base_url": url, "model": "m", "api_key_env": "KO"}}})
    sr = Showrunner(cfg, chain)
    # 1er essai refusé par la validation (décor hors liste) → le modèle est relancé, puis on assainit
    premise = sr.premise("ep", {"id": "x", "situation": "Un colis perdu.", "cast": ["momo", "lina"], "sets": ["rue"]})
    assert all(bt.set == "rue" for bt in premise.beats)
    script = sr.write_script(premise, "fr")
    srv.shutdown()
    assert script.title and len(script.lines) >= 3 * len(premise.beats) - 4
