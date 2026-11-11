"""VAULT : la mémoire permanente de l'usine (local ou S3 / Cloudflare R2 / MinIO…).

Arborescence (identique quel que soit le stockage) :

    series/<slug>/series.yaml           bible de la série
    series/<slug>/cast.yaml             identités persistantes (voix, rigs, signatures…)
    series/<slug>/memory/*.jsonl        continuité, épisodes diffusés
    series/<slug>/plates/*.png          décors 2D
    series/<slug>/assets/...            rigs .blend, échantillons de voix, planches
    episodes/<id>/state.json            ÉTAT DE RÉFÉRENCE de l'épisode (statut, niveau, étapes, infos, journal)
    episodes/<id>/files/...             artefacts (script, audio, rendus, vidéos, kit de publication)
    queue/<horodatage>-<id>.json        commandes en attente pour le STUDIO
    locks/<id>.json                     jeton d'exclusivité d'un worker

Règle d'or : on ne déplace jamais un gros fichier pour rien. Chaque arbre synchronisé porte
un manifeste (empreinte SHA-256 par fichier) : `push_tree` / `pull_tree` ne transfèrent que ce
qui a changé, et `pull_tree(include=…)` ne rapatrie que ce dont l'étape a besoin.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Protocol

MANIFEST = ".vault-manifest.json"


class VaultError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Vault(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get_bytes(self, key: str) -> bytes | None: ...
    def put_file(self, key: str, path: Path) -> None: ...
    def get_file(self, key: str, path: Path) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def url(self, key: str, expires: int = 3600) -> str | None: ...


# --------------------------------------------------------------------------- local
class LocalVault:
    """Vault sur disque : développement, tests, ou volume réseau monté."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise VaultError(f"clé hors du vault : {key}")
        return p

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        tmp.replace(p)  # écriture atomique

    def get_bytes(self, key):
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def put_file(self, key, path):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, p)

    def get_file(self, key, path):
        p = self._p(key)
        if not p.is_file():
            return False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, path)
        return True

    def list(self, prefix):
        base = self._p(prefix) if prefix else self.root
        if base.is_file():
            return [prefix]
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*")
                      if p.is_file() and ".tmp" not in p.name)

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)

    def url(self, key, expires=3600):
        return None  # servi par le serveur de commande


# --------------------------------------------------------------------------- S3 / R2
class S3Vault:
    """Tout stockage compatible S3 : Cloudflare R2 (sans frais de sortie), AWS S3, MinIO, Backblaze B2…"""

    def __init__(self, cfg: dict):
        try:
            import boto3  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise VaultError("pip install 'toonomatic[cloud]' (boto3)") from e
        self.bucket = cfg["bucket"]
        self.prefix = cfg.get("prefix", "").strip("/")
        self.s3 = boto3.client(
            "s3", endpoint_url=cfg.get("endpoint_url") or None, region_name=cfg.get("region", "auto"),
            aws_access_key_id=os.environ.get(cfg.get("access_key_env", "VAULT_ACCESS_KEY_ID")),
            aws_secret_access_key=os.environ.get(cfg.get("secret_key_env", "VAULT_SECRET_ACCESS_KEY")))

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key, data, content_type="application/octet-stream"):  # pragma: no cover - réseau
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, ContentType=content_type)

    def get_bytes(self, key):  # pragma: no cover - réseau
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put_file(self, key, path):  # pragma: no cover - réseau
        self.s3.upload_file(str(path), self.bucket, self._k(key))  # multipart automatique

    def get_file(self, key, path):  # pragma: no cover - réseau
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.s3.download_file(self.bucket, self._k(key), str(path))
            return True
        except Exception:  # noqa: BLE001 - clé absente
            return False

    def list(self, prefix):  # pragma: no cover - réseau
        out, token = [], None
        cut = len(self.prefix) + 1 if self.prefix else 0
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._k(prefix)}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"][cut:] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(out)
            token = r["NextContinuationToken"]

    def delete(self, key):  # pragma: no cover - réseau
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))

    def url(self, key, expires=3600):  # pragma: no cover - réseau
        # lien signé temporaire : le téléphone lit la vidéo en streaming, sans téléchargement
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": self._k(key)},
                                              ExpiresIn=expires)


def make_vault(cfg: dict, root: Path) -> Vault:
    kind = cfg.get("backend", "local")
    if kind == "local":
        p = Path(cfg.get("path", "vault"))
        return LocalVault(p if p.is_absolute() else root / p)
    if kind == "s3":
        return S3Vault(cfg.get("s3", {}))
    raise VaultError(f"backend de vault inconnu : {kind}")


# --------------------------------------------------------------------------- synchronisation
def _local_manifest(local: Path, exclude: Iterable[str]) -> dict[str, dict]:
    out = {}
    for p in sorted(local.rglob("*")):
        rel = str(p.relative_to(local))
        if not p.is_file() or rel == MANIFEST or any(fnmatch.fnmatch(rel, x) for x in exclude):
            continue
        st = p.stat()
        out[rel] = {"sha256": sha256(p), "size": st.st_size}
    return out


def push_tree(vault: Vault, local: Path, prefix: str, exclude: Iterable[str] = ()) -> dict:
    """Envoie un dossier vers le vault ; ne transfère que les fichiers nouveaux ou modifiés."""
    remote = json.loads(vault.get_bytes(f"{prefix}/{MANIFEST}") or b"{}")
    here = _local_manifest(local, exclude)
    sent = 0
    for rel, meta in here.items():
        if remote.get(rel, {}).get("sha256") != meta["sha256"]:
            vault.put_file(f"{prefix}/{rel}", local / rel)
            sent += meta["size"]
    merged = {**remote, **here}
    vault.put_bytes(f"{prefix}/{MANIFEST}", json.dumps(merged, indent=0).encode(), "application/json")
    return {"files": len(here), "bytes_sent": sent}


def pull_tree(vault: Vault, prefix: str, local: Path, include: Iterable[str] | None = None) -> dict:
    """Rapatrie un dossier ; seulement les fichiers absents/modifiés, et seulement ceux de `include`."""
    remote = json.loads(vault.get_bytes(f"{prefix}/{MANIFEST}") or b"{}")
    inc = list(include) if include else None
    got = 0
    for rel, meta in remote.items():
        if inc and not any(fnmatch.fnmatch(rel, pat) for pat in inc):
            continue
        dst = local / rel
        if dst.is_file() and dst.stat().st_size == meta["size"] and sha256(dst) == meta["sha256"]:
            continue
        if vault.get_file(f"{prefix}/{rel}", dst):
            got += meta["size"]
    return {"files": len(remote), "bytes_received": got}


# --------------------------------------------------------------------------- file d'attente
def enqueue(vault: Vault, job: dict) -> str:
    key = f"queue/{time.strftime('%Y%m%dT%H%M%S')}-{job['episode_id']}-{job.get('action', 'run')}.json"
    vault.put_bytes(key, json.dumps(job, ensure_ascii=False).encode(), "application/json")
    return key


def claim(vault: Vault, worker_id: str, can_gpu: bool = True, ttl: int = 6 * 3600) -> tuple[str, dict] | None:
    """Prend la plus ancienne commande compatible. Verrou par épisode (expire après `ttl`)."""
    for key in vault.list("queue/"):
        raw = vault.get_bytes(key)
        if raw is None:
            continue
        job = json.loads(raw)
        if job.get("needs_gpu") and not can_gpu:
            continue
        lock_key = f"locks/{job['episode_id']}.json"
        lock = vault.get_bytes(lock_key)
        if lock and time.time() - json.loads(lock).get("ts", 0) < ttl:
            continue
        vault.put_bytes(lock_key, json.dumps({"worker": worker_id, "ts": time.time(), "job": key}).encode())
        if json.loads(vault.get_bytes(lock_key) or b"{}").get("worker") != worker_id:
            continue  # un autre worker a gagné la course
        vault.delete(key)
        return key, job
    return None


def release(vault: Vault, episode_id: str) -> None:
    vault.delete(f"locks/{episode_id}.json")
