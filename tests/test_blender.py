"""Rendu Blender réel (Cycles CPU, très basse résolution). Ignoré si le module `bpy` est absent."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(importlib.util.find_spec("bpy") is None, reason="pip install bpy")


def test_blender_scene_renders_with_style_visemes_and_gaze(tmp_path):
    shot = {
        "episode_lang": "fr", "scene_id": "s01", "duration": 1.0, "fps": 12, "resolution": [320, 180],
        "set": {"id": "rue", "blend": "/absent.blend", "color": "#5b5f73", "label": "La rue"},
        "characters": [{"id": "momo", "slot": 0, "display": "Momo", "rig": "/absent.blend", "color": "#e8833a"},
                       {"id": "lina", "slot": 1, "display": "Lina", "rig": "/absent.blend", "color": "#3a9be8"}],
        "performances": [{"character": "momo", "start": 0.0, "end": 0.5, "action": "talk_hands", "emotion": "happy",
                          "envelope": [], "visemes": [{"t": 0.0, "v": "X"}, {"t": 0.1, "v": "D"}, {"t": 0.3, "v": "A"}]},
                         {"character": "lina", "start": 0.5, "end": 1.0, "action": "shocked", "emotion": "angry",
                          "envelope": [0.2, 0.8, 0.5], "visemes": []}],
        "shots": [{"camera": "wide", "start": 0, "end": 0.5, "speaker": "momo"},
                  {"camera": "close:lina", "start": 0.5, "end": 1.0, "speaker": "lina"}],
        "style": yaml.safe_load((ROOT / "styles" / "toon.yaml").read_text()),
    }
    sl = tmp_path / "s01.json"
    sl.write_text(json.dumps(shot))
    out = tmp_path / "frames"
    proc = subprocess.run([sys.executable, str(ROOT / "toonomatic/animation/blender_scene.py"), "--",
                           "--shotlist", str(sl), "--out", str(out), "--engine", "CYCLES", "--samples", "1",
                           "--scale", "50"], capture_output=True, text=True, timeout=600)
    assert "TOONOMATIC_RENDER_OK" in proc.stdout, proc.stderr[-2000:]
    assert len(list(out.glob("frame_*.png"))) == 12
