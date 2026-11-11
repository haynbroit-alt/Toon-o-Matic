import shutil
from pathlib import Path

import pytest

from toonomatic.config import load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path):
    lessons = tmp_path / "lessons.md"
    lessons.write_text("# Leçons\n\n", encoding="utf-8")
    series = tmp_path / "series"
    shutil.copytree(ROOT / "series" / "les-voisins", series)
    (series / "memory" / "continuity.jsonl").write_text("", encoding="utf-8")
    for generated in ("plates/*.png", "stickers", "brand"):  # isolation : jamais d'artefacts d'un essai local
        for g in series.glob(generated):
            shutil.rmtree(g) if g.is_dir() else g.unlink()
    return load_config(overrides={
        "series": str(series),
        "workdir": str(tmp_path / "runs"),
        "database": str(tmp_path / "runs" / "db.sqlite"),
        "episode": {"target_minutes": 0.5, "min_minutes": 0.3, "max_minutes": 0.7},
        "animation": {"resolution": [320, 180], "fps": 12},
        "editing": {"cutdown": {"resolution": [180, 320], "max_seconds": 10}},
        "learning": {"lessons_file": str(lessons)},
        "llm": {"provider": "mock"}, "tts": {"provider": "mock", "sample_rate": 16000},
        "vault": {"backend": "local", "path": str(tmp_path / "vault")},
        "production": {"default_tier": "final", "tiers": {
            "draft": {"overrides": {"animation": {"resolution": [256, 144]}}},
            "validation": {"overrides": {"animation": {"renderer": "preview", "resolution": [320, 180]}}},
            "final": {"overrides": {"animation": {"renderer": "preview"}}}}},
    })


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg requis")
