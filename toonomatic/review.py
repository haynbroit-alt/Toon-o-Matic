"""Humain dans la boucle : [Valider] / [Refaire] avant publication.

Canaux : interface web locale (sans dépendance), bot Telegram (boutons inline), ou CLI.
Une décision « Refaire » avec un motif devient une LEÇON réinjectée dans les prompts.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from .config import Config
from .learning import add_lesson
from .store import Store

log = logging.getLogger(__name__)

# étape à partir de laquelle on régénère lors d'un « Refaire »
REDO_FROM = {
    "premise": [],                                     # tout refaire (nouvelle structure)
    "script": ["premise"],                             # réécrire les dialogues
    "voice": ["premise", "script"],                    # réenregistrer les voix
    "render": ["premise", "script", "voice", "subs"],  # relancer l'animation
}


def apply_decision(cfg: Config, store: Store, episode_id: str, decision: str, reason: str = "",
                   redo_from: str = "script") -> dict:
    ep = store.get(episode_id)
    if not ep:
        raise KeyError(episode_id)
    if decision == "approve":
        store.update(episode_id, status="approved", info={"review": {"decision": "approve", "reason": reason}})
    elif decision == "redo":
        if reason:
            add_lesson(cfg, reason, source=f"revue {episode_id}")
        keep_prefixes = REDO_FROM.get(redo_from, REDO_FROM["script"])
        keep = [s for s in ep["stages"] if s.split("_")[0] in keep_prefixes]
        store.reset_stages(episode_id, keep)
        retries = ep["info"].get("retries", 0) + 1
        store.update(episode_id, status="queued",
                     info={"review": {"decision": "redo", "reason": reason, "from": redo_from},
                           "retries": retries})
    elif decision == "reject":
        if reason:
            add_lesson(cfg, f"À éviter : {reason}", source=f"rejet {episode_id}")
        store.update(episode_id, status="rejected", info={"review": {"decision": "reject", "reason": reason}})
    else:
        raise ValueError(decision)
    store.log(episode_id, "review", {"decision": decision, "reason": reason, "redo_from": redo_from})
    return store.get(episode_id)


# --------------------------------------------------------------------------- web
PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Toon-o-Matic · Revue</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px;background:#111;color:#eee}}
.ep{{background:#1c1c22;border-radius:12px;padding:16px;margin:16px 0}}
video{{width:100%;max-height:60vh;background:#000;border-radius:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
button{{font-size:1rem;padding:10px 18px;border-radius:8px;border:0;margin:4px;cursor:pointer}}
.ok{{background:#2e9e5b;color:#fff}} .redo{{background:#d98b2b;color:#fff}} .no{{background:#a33;color:#fff}}
input,select{{padding:8px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}}
.risk{{font-weight:700}} pre{{white-space:pre-wrap;font-size:.8rem;color:#aaa}}
</style><h1>Épisodes en attente de validation</h1>{body}</html>"""


def _episode_html(cfg: Config, ep: dict) -> str:
    wd = cfg.workdir
    vids = []
    for item in ep["info"].get("deliverables", []):
        rel = Path(item["video"]).resolve().relative_to(wd.resolve())
        vids.append(f"<div><b>{html.escape(item['lang'].upper())} · {item['kind']}</b> — "
                    f"{html.escape(item['title'])}<video controls preload=metadata "
                    f"src='/files/{urllib.parse.quote(str(rel))}'></video></div>")
    qa = ep["info"].get("qa", {})
    color = "#2e9e5b" if (ep["risk"] or 0) < cfg["qa"]["min_risk_for_human"] else "#d98b2b"
    return f"""<div class=ep><h2>{html.escape(ep['id'])}</h2>
<p class=risk style="color:{color}">Risque : {ep['risk']}</p>
<div class=grid>{''.join(vids)}</div>
<details><summary>Rapport QA</summary><pre>{html.escape(json.dumps(qa, ensure_ascii=False, indent=1))}</pre></details>
<form method=post action=/decision>
<input type=hidden name=episode_id value="{html.escape(ep['id'])}">
<input name=reason placeholder="Motif (devient une leçon pour les prochains épisodes)" size=60>
<select name=redo_from><option value=script>réécrire dialogues</option><option value=premise>nouvelle structure</option>
<option value=voice>refaire les voix</option><option value=render>refaire l'animation</option></select><br>
<button class=ok name=decision value=approve>✅ Valider</button>
<button class=redo name=decision value=redo>🔁 Refaire</button>
<button class=no name=decision value=reject>⛔ Rejeter</button></form></div>"""


def make_handler(cfg: Config, store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D401
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/files/"):
                rel = urllib.parse.unquote(self.path[len("/files/"):])
                p = (cfg.workdir / rel).resolve()
                if cfg.workdir.resolve() not in p.parents or not p.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            eps = store.list("review")
            body = "".join(_episode_html(cfg, e) for e in eps) or "<p>Rien à valider 🎉</p>"
            self._send(200, PAGE.format(body=body).encode())

        def do_POST(self):
            if self.path != "/decision":
                return self._send(404, b"", "text/plain")
            n = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
            try:
                apply_decision(cfg, store, form["episode_id"], form["decision"], form.get("reason", ""),
                               form.get("redo_from", "script"))
            except (KeyError, ValueError) as e:
                return self._send(400, str(e).encode(), "text/plain")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    return Handler


def serve(cfg: Config, store: Store) -> None:
    w = cfg["review"]["web"]
    srv = ThreadingHTTPServer((w["host"], int(w["port"])), make_handler(cfg, store))
    log.info("revue web sur http://%s:%s", w["host"], w["port"])
    srv.serve_forever()


# --------------------------------------------------------------------------- telegram
class Telegram:
    API = "https://api.telegram.org/bot{token}/{method}"
    MAX_UPLOAD = 49 * 1024 * 1024

    def __init__(self, cfg: Config):
        t = cfg["review"]["telegram"]
        self.token = os.environ.get(t["bot_token_env"], "")
        self.chat_id = os.environ.get(t["chat_id_env"], "")
        if not (self.token and self.chat_id):
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants")

    def _call(self, method: str, **kw):
        r = requests.post(self.API.format(token=self.token, method=method), timeout=300, **kw)
        r.raise_for_status()
        return r.json()["result"]

    def notify(self, ep: dict) -> None:
        kb = {"inline_keyboard": [[{"text": "✅ Valider", "callback_data": f"approve:{ep['id']}"},
                                   {"text": "🔁 Refaire", "callback_data": f"redo:{ep['id']}"},
                                   {"text": "⛔ Rejeter", "callback_data": f"reject:{ep['id']}"}]]}
        caption = f"🎬 {ep['id']} — risque {ep['risk']}"
        # Les épisodes longs dépassent la limite d'upload : on envoie les extraits verticaux.
        sent = False
        for item in ep["info"].get("deliverables", []):
            p = Path(item["video"])
            if p.stat().st_size <= self.MAX_UPLOAD:
                with p.open("rb") as fh:
                    self._call("sendVideo", data={"chat_id": self.chat_id,
                                                  "caption": f"{caption}\n{item['lang']} · {item['kind']} · {item['title']}"},
                               files={"video": fh})
                sent = True
        self._call("sendMessage", json={"chat_id": self.chat_id, "reply_markup": kb,
                                        "text": caption + ("" if sent else "\n(vidéos trop lourdes : voir l'interface web)")
                                        + "\nRépondez à ce message pour donner un motif de « Refaire »."})

    def poll(self, cfg: Config, store: Store, offset: int = 0) -> int:
        """Traite les clics ; à appeler périodiquement (cron) ou en boucle."""
        pending_reason: dict[str, str] = {}
        for upd in self._call("getUpdates", params={"offset": offset, "timeout": 0}):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if msg and msg.get("reply_to_message"):
                pending_reason[str(msg["reply_to_message"]["message_id"])] = msg.get("text", "")
            cb = upd.get("callback_query")
            if cb:
                decision, ep_id = cb["data"].split(":", 1)
                reason = pending_reason.get(str(cb["message"]["message_id"]), "")
                apply_decision(cfg, store, ep_id, decision, reason)
                self._call("answerCallbackQuery", json={"callback_query_id": cb["id"], "text": f"{decision} ✔"})
        return offset
