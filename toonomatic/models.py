"""Modèle de données d'un épisode, sérialisable en JSON à chaque étape."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Line:
    character: str
    text: str
    emotion: str = "neutral"
    action: str = "talk"
    # Remplis par l'étape voix
    audio: str | None = None
    start: float = 0.0
    end: float = 0.0
    envelope: list[float] = field(default_factory=list)  # amplitude par frame → lip-sync


@dataclass
class Scene:
    id: str
    set: str
    characters: list[str]
    summary: str
    lines: list[Line] = field(default_factory=list)
    highlight: bool = False      # meilleure scène → extrait vertical
    start: float = 0.0
    end: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Scene":
        d = dict(d)
        d["lines"] = [Line(**ln) for ln in d.get("lines", [])]
        return cls(**d)


@dataclass
class Beat:
    """Une scène de l'outline commune (avant écriture par langue)."""
    id: str
    set: str
    characters: list[str]
    purpose: str


@dataclass
class Premise:
    id: str
    situation: str
    logline: str
    cast: list[str]
    beats: list[Beat]
    target_minutes: float
    running_gag: str = ""
    source: str = "bank"

    @classmethod
    def from_dict(cls, d: dict) -> "Premise":
        d = dict(d)
        d["beats"] = [Beat(**b) for b in d["beats"]]
        return cls(**d)


@dataclass
class Script:
    lang: str
    title: str
    scenes: list[Scene]
    style: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Script":
        d = dict(d)
        d["scenes"] = [Scene.from_dict(s) for s in d["scenes"]]
        return cls(**d)

    @property
    def lines(self) -> list[Line]:
        return [ln for s in self.scenes for ln in s.lines]

    def word_count(self) -> int:
        return sum(len(ln.text.split()) for ln in self.lines)

    def estimated_minutes(self, wpm: float) -> float:
        # +0.6 s de respiration par réplique, +2 s de transition par scène
        return self.word_count() / wpm + (len(self.lines) * 0.6 + len(self.scenes) * 2) / 60


def save_json(obj: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
