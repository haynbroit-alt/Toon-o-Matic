"""Publication. `publish.dry_run: true` (défaut) n'envoie rien : écrit seulement un manifeste."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


def _youtube(cfg: Config, video: Path, title: str, description: str, lang: str, short: bool) -> str:
    try:  # pragma: no cover - réseau
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as e:
        raise PublishError("pip install 'toonomatic[youtube]'") from e
    yc = cfg["publish"]["youtube"]  # pragma: no cover
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.path(yc["client_secrets"])),
                                                     ["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=flow.run_local_server(port=0))
    body = {"snippet": {"title": (title + (" #Shorts" if short else ""))[:100], "description": description,
                        "defaultLanguage": lang, "defaultAudioLanguage": lang, "categoryId": "23"},
            "status": {"privacyStatus": yc.get("privacy", "private"), "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": True}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtu.be/{resp['id']}"


def publish(cfg: Config, episode_dir: Path, manifest: dict) -> dict:
    pc = cfg["publish"]
    results: dict = {"dry_run": pc.get("dry_run", True), "targets": {}}
    if results["dry_run"]:
        (episode_dir / "publish_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        log.info("dry-run : rien n'est envoyé (manifeste écrit)")
        return results
    for target in pc.get("targets", []):
        out = []
        for item in manifest["items"]:
            if target == "local":
                dest = cfg.path(pc["local_dir"]) / manifest["episode_id"]
                dest.mkdir(parents=True, exist_ok=True)
                for key in ("video", "srt", "srt_translated"):
                    for f in ([item[key]] if isinstance(item.get(key), str) else item.get(key) or []):
                        shutil.copy2(f, dest)
                out.append(str(dest))
            elif target == "youtube":
                out.append(_youtube(cfg, Path(item["video"]), item["title"], item["description"],
                                    item["lang"], item["kind"] == "short"))
            else:
                # TikTok (Content Posting API) et Instagram (Graph API, Reels) exigent une app validée :
                # brancher ici un connecteur (n8n, Zapier/Make, Metricool...) une fois les accès obtenus.
                raise PublishError(f"cible '{target}' non implémentée : utilisez un connecteur externe")
        results["targets"][target] = out
    return results
