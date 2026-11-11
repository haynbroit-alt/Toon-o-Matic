import shutil
from pathlib import Path

import pytest

from toonomatic.config import load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path):
    lessons = tmp_path / "lessons.md"
    lessons.write_text("# Leçons\n\n", encoding="utf-8")
    return load_config(overrides={
        "workdir": str(tmp_path / "runs"),
        "database": str(tmp_path / "runs" / "db.sqlite"),
        "episode": {"target_minutes": 0.5, "min_minutes": 0.3, "max_minutes": 0.7},
        "animation": {"resolution": [320, 180], "fps": 12},
        "editing": {"cutdown": {"resolution": [180, 320], "max_seconds": 10}},
        "learning": {"lessons_file": str(lessons)},
        "llm": {"provider": "mock"}, "tts": {"provider": "mock", "sample_rate": 16000},
    })


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg requis")
