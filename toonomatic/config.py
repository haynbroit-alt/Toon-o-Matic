"""Chargement de la configuration (YAML) avec surcharge par variables d'environnement."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict[str, Any]
    cast: dict[str, Any]
    root: Path = field(default=ROOT)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, p: str | os.PathLike) -> Path:
        p = Path(p)
        return p if p.is_absolute() else self.root / p

    @property
    def workdir(self) -> Path:
        return self.path(self.data["workdir"])

    @property
    def languages(self) -> list[str]:
        return list(self.data["languages"].keys())


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None,
                root: Path | None = None) -> Config:
    root = Path(root) if root else ROOT
    base = yaml.safe_load((ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    if path:
        base = _deep_merge(base, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    # Surcharges rapides : TOONOMATIC_LLM=claude, TOONOMATIC_TTS=xtts, TOONOMATIC_RENDERER=blender
    env_map = {"TOONOMATIC_LLM": ("llm", "provider"), "TOONOMATIC_TTS": ("tts", "provider"),
               "TOONOMATIC_RENDERER": ("animation", "renderer")}
    for env, (sec, key) in env_map.items():
        if os.environ.get(env):
            base[sec][key] = os.environ[env]
    base = _deep_merge(base, overrides or {})
    cast_path = Path(base.get("cast", ROOT / "config" / "cast.yaml"))
    cast = yaml.safe_load(cast_path.read_text(encoding="utf-8"))
    return Config(data=base, cast=cast, root=root)
