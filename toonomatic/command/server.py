"""Serveur de COMMAND : l'API que pilote le téléphone + l'application mobile (PWA).

Léger (bibliothèque standard), sans calcul lourd : il commande le STUDIO et lit le VAULT.
Déployable sur Render (voir render.yaml), une petite VM, ou en local.

Sécurité : un jeton (variable TOONOMATIC_TOKEN) est OBLIGATOIRE dès que le serveur écoute
ailleurs que sur 127.0.0.1. La PWA l'échange contre un cookie HttpOnly (les balises <video>
ne peuvent pas envoyer d'en-tête d'authentification).
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Config
from ..studio import Factory

log = logging.getLogger(__name__)
PWA = Path(__file__).with_name("pwa")
COOKIE = "tm_token"
EP_RE = re.compile(r"^/api/episodes/([A-Za-z0-9._-]+)(/.*)?$")


def _summary(f: Factory, ep: dict) -> dict:
    info = ep.get("info", {})
    return {"id": ep["id"], "status": ep["status"], "tier": info.get("tier"), "risk": ep.get("risk"),
            "title": next((d["title"] for d in info.get("deliverables", []) if d.get("lang") == "fr"), ep["id"]),
            "scores": (info.get("director") or {}).get("scores"), "updated": ep.get("updated"),
            "error": info.get("error")}


def _media_list(f: Factory, ep: dict) -> list[dict]:
    out = []
    wd = f.cfg.workdir / ep["id"]
    for d in ep.get("info", {}).get("deliverables", []):
        p = Path(d["video"])
        try:
            rel = p.relative_to(wd) if p.is_absolute() else p
        except ValueError:
            rel = Path(d["lang"]) / p.name
        out.append({"lang": d["lang"], "kind": d["kind"], "title": d["title"],
                    "src": f"/api/episodes/{ep['id']}/media/{urllib.parse.quote(str(rel))}"})
    return out


def make_handler(cfg: Config, factory: Factory, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ToonOMatic/1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        # ---------------------------------------------------------------- utilitaires
        def _authed(self) -> bool:
            if not token:
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Bearer ") and hmac.compare_digest(hdr[7:], token):
                return True
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v, token):
                    return True
            return False

        def _json(self, code: int, obj, headers: dict | None = None):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("requête trop grosse")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _static(self, name: str):
            p = (PWA / name).resolve()
            if PWA.resolve() not in p.parents or not p.is_file():
                return self._json(404, {"error": "introuvable"})
            body = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if p.suffix == ".webmanifest":
                ctype = "application/manifest+json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if name == "sw.js":
                self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, path: Path):
            """Lecture vidéo en streaming (requêtes Range) : rien à télécharger sur le téléphone."""
            if not path.is_file():
                return self._json(404, {"error": "média introuvable"})
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            start, end = 0, size - 1
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = int(rng.group(2)) if rng.group(2) else end
                else:
                    start = max(0, size - int(rng.group(2)))
                end = min(end, size - 1, start + 8 * 1024 * 1024 - 1)  # morceaux de 8 Mo max
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with path.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # ---------------------------------------------------------------- routes
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/app.js", "/style.css", "/manifest.webmanifest", "/sw.js", "/icon.svg"):
                return self._static(path.lstrip("/"))
            if path == "/api/health":
                return self._json(200, {"ok": True, "auth": bool(token)})
            if not self._authed():
                return self._json(401, {"error": "jeton requis"})
            if path == "/api/config":
                return self._json(200, {"tiers": factory.tiers(), "languages": cfg.languages,
                                        "series": (cfg.series or {}).get("title"),
                                        "gpu_tiers": [t for t in factory.tiers() if factory.needs_gpu(t)]})
            if path == "/api/characters":
                from ..social import card
                return self._json(200, [card(cfg, cid) for cid in cfg.cast["characters"]])
            if path == "/api/social":
                from ..social import load_profile
                return self._json(200, load_profile(cfg) or {})
            if path == "/api/situations":
                return self._json(200, factory.situations())
            if path == "/api/episodes":
                eps = sorted(factory.store.list(), key=lambda e: e.get("updated") or 0, reverse=True)
                return self._json(200, [_summary(factory, e) for e in eps[:100]])
            m = EP_RE.match(path)
            if m:
                eid, rest = m.group(1), m.group(2) or ""
                ep = factory.episode(eid)
                if not ep:
                    return self._json(404, {"error": "épisode inconnu"})
                if rest == "":
                    ep["media"] = _media_list(factory, ep)
                    man = factory.read_json(eid, "presence/manifest.json") or {}
                    ep["presence"] = [{**{k: v for k, v in a.items() if k not in ("video", "lines")},
                                       "src": f"/api/episodes/{eid}/media/" + urllib.parse.quote(
                                           f"presence/{a['lang']}/{a['scene']}/{a['kind']}.mp4")}
                                      for a in man.get("assets", [])]
                    ep["presence_plan"] = (factory.read_json(eid, "presence/plan.json") or [])[:6]
                    ep["events"] = factory.store.events(eid, 30)
                    return self._json(200, ep)
                if rest.startswith("/media/"):
                    rel = urllib.parse.unquote(rest[len("/media/"):])
                    try:
                        kind, target = factory.media(eid, rel)
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    if kind == "url":
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.end_headers()
                        return None
                    return self._stream(Path(target))
            return self._json(404, {"error": "route inconnue"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            if path == "/api/login":
                if token and not hmac.compare_digest(str(body.get("token", "")), token):
                    return self._json(401, {"error": "jeton invalide"})
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self._json(200, {"ok": True}, {
                    "Set-Cookie": f"{COOKIE}={token or ''}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000{secure}"})
            if not self._authed():
                return self._json(401, {"error": "jeton requis"})
            try:
                if path == "/api/orders":
                    return self._json(202, factory.order(body.get("situation_id"), body.get("situation_text"),
                                                         body.get("tier")))
                m = EP_RE.match(path)
                if m and m.group(2) == "/decision":
                    return self._json(202, factory.decide(m.group(1), body.get("decision", ""),
                                                          body.get("reason", ""), body.get("from", "script"),
                                                          body.get("tier"), body.get("fixes")))
            except KeyError as e:
                return self._json(404, {"error": f"inconnu : {e}"})
            except (ValueError, RuntimeError) as e:
                return self._json(409, {"error": str(e)})
            return self._json(404, {"error": "route inconnue"})

    return Handler


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080) -> None:
    token = os.environ.get((cfg.get("command") or {}).get("token_env", "TOONOMATIC_TOKEN")) or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("TOONOMATIC_TOKEN obligatoire pour écouter sur une interface publique")
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, Factory(cfg), token))
    log.info("COMMAND sur http://%s:%s (jeton %s)", host, port, "actif" if token else "désactivé (local)")
    srv.serve_forever()
