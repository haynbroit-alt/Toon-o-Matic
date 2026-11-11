"""Pièges de déploiement (Render, Docker) : rien de tout cela ne se voit en installation de développement."""
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_root_found_from_cwd_when_package_is_installed_elsewhere(tmp_path):
    # simule `pip install .` : le code est ailleurs que la configuration
    code = ("import os, sys; sys.path.insert(0, %r); os.chdir(%r); "
            "import toonomatic.config as c; print(c.ROOT)") % (str(ROOT), str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "TOONOMATIC_HOME": ""}, cwd=tmp_path)
    assert out.stdout.strip() == str(ROOT)


def test_toonomatic_home_wins(tmp_path):
    code = "import toonomatic.config as c; print(c.ROOT)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "TOONOMATIC_HOME": str(ROOT), "PYTHONPATH": str(ROOT)}, cwd=tmp_path)
    assert out.stdout.strip() == str(ROOT)


def test_public_server_without_token_starts_locked_with_instructions(cfg):
    from toonomatic.command.server import make_handler
    c = cfg.derive({"vault": {"backend": "s3", "fallback_local": True, "path": str(cfg.workdir.parent / "v"),
                              "s3": {"bucket": "x", "endpoint_url": "https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com"}},
                    "llm": {"provider": "claude", "fallback": "mock"}})
    env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, None, None, locked=True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        h = json.loads(urllib.request.urlopen(base + "/api/health").read())
        assert h["ok"] and h["locked"] and h["vault"] == "local éphémère" and h["llm"] == "mock"
        assert any("TOONOMATIC_TOKEN" in w for w in h["warnings"])
        assert urllib.request.urlopen(base + "/").status == 200          # la page d'installation s'affiche
        try:
            urllib.request.urlopen(base + "/api/episodes")
            raise AssertionError("aurait dû être refusé")
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        srv.shutdown()
        if env_backup:
            os.environ["ANTHROPIC_API_KEY"] = env_backup


def test_vault_and_llm_fallbacks(tmp_path, monkeypatch):
    from toonomatic.vault import LocalVault, VaultError, make_vault
    from toonomatic.writing.llm import MockLLM, make_llm
    monkeypatch.delenv("VAULT_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    s3 = {"backend": "s3", "path": str(tmp_path / "v"), "s3": {"bucket": "b", "endpoint_url": "https://x"}}
    try:
        make_vault(s3, tmp_path)
        raise AssertionError("sans repli autorisé, l'absence de clés doit être une erreur explicite")
    except VaultError as e:
        assert "VAULT_ACCESS_KEY_ID" in str(e)
    assert isinstance(make_vault({**s3, "fallback_local": True}, tmp_path), LocalVault)
    assert isinstance(make_llm({"provider": "claude", "fallback": "mock"}), MockLLM)


def test_r2_configurable_from_environment(monkeypatch):
    from toonomatic.vault import s3_problem, s3_settings
    cfg = {"backend": "s3", "s3": {"bucket": "toonomatic", "endpoint_url": "https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com"}}
    for k in ("VAULT_ENDPOINT_URL", "VAULT_BUCKET", "VAULT_ACCESS_KEY_ID", "VAULT_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert "VAULT_ENDPOINT_URL" in s3_problem(cfg)
    monkeypatch.setenv("VAULT_ENDPOINT_URL", " abc123.r2.cloudflarestorage.com/ ")
    monkeypatch.setenv("VAULT_BUCKET", "mon-studio")
    assert s3_settings(cfg) == {"bucket": "mon-studio", "endpoint_url": "https://abc123.r2.cloudflarestorage.com"}
    assert "VAULT_ACCESS_KEY_ID" in s3_problem(cfg)
    monkeypatch.setenv("VAULT_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("VAULT_SECRET_ACCESS_KEY", "b")
    assert s3_problem(cfg) is None
