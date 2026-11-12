"""Théâtre de marionnettes 2D : un vrai dessin animé sur processeur, sans GPU ni modèle 3D."""
import importlib.util
import json
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from toonomatic import ffmpeg
from toonomatic.animation import puppet

needs_cairo = pytest.mark.skipif(importlib.util.find_spec("cairo") is None, reason="pip install pycairo")


def shot(cfg, tmp_path, cams=("wide", "close:lina", "two_shot")):
    cast = cfg.cast["characters"]
    chars = [{"id": c, "slot": i, "display": cast[c]["display"], "color": cast[c]["color"],
              "signature": cast[c]["signature"]} for i, c in enumerate(["momo", "lina", "tonton"])]
    plate = puppet.draw_plate("rue", "#5b5f73", tmp_path / "rue.png", size=(640, 360))
    perfs = [{"character": c, "start": i * 1.0, "end": i * 1.0 + 1.0, "action": a, "emotion": e,
              "envelope": [0.7] * 12, "visemes": [{"t": 0, "v": "D"}, {"t": 0.5, "v": "X"}]}
             for i, (c, a, e) in enumerate([("momo", "talk_hands", "smug"), ("lina", "facepalm", "sarcastic"),
                                            ("tonton", "shrug", "happy")])]
    return {"scene_id": "s01", "duration": 3.0, "fps": 12, "resolution": [320, 180], "style": cfg.style,
            "set": {"id": "rue", "plate": str(plate), "color": "#5b5f73"}, "characters": chars, "performances": perfs,
            "shots": [{"camera": c, "start": i * 1.0, "end": i * 1.0 + 1.0, "speaker": p["character"]}
                      for i, (c, p) in enumerate(zip(cams, perfs))]}


@needs_cairo
def test_vector_engine_is_used_and_frames_change(cfg, tmp_path):
    st = puppet.Stage(shot(cfg, tmp_path))
    assert type(st).__name__ == "VectorStage"
    a, b, c = st.frame(0.5), st.frame(1.5), st.frame(2.5)
    assert a.size == (320, 180)
    assert a.tobytes() != b.tobytes() != c.tobytes()               # plans différents (large, gros plan, deux-plans)


@needs_cairo
def test_gestures_are_smoothed_not_snapped(cfg, tmp_path):
    sh = shot(cfg, tmp_path)
    sh["fps"] = 24
    st = puppet.Stage(sh)
    for k in range(12, 24):                                        # images successives, comme au rendu
        st.frame(k / 24)
    before = st.pose["lina"]["fh"]
    st.frame(1.0)                                                  # Lina commence son facepalm
    mid = st.pose["lina"]["fh"]
    for k in range(1, 12):
        st.frame(1.0 + k / 24)
    after = st.pose["lina"]["fh"]
    assert mid != before and mid != after                          # une position intermédiaire existe
    assert abs(after[1] - before[1]) > abs(mid[1] - before[1])


@needs_cairo
def test_face_reacts_to_viseme_and_emotion(cfg, tmp_path):
    char = {"id": "momo", **cfg.cast["characters"]["momo"]}
    closed = puppet.still(char, cfg.style, "neutral", "idle", "X", 256)
    open_ = puppet.still(char, cfg.style, "neutral", "idle", "D", 256)
    angry = puppet.still(char, cfg.style, "angry", "angry", "X", 256)
    assert closed.tobytes() != open_.tobytes() != angry.tobytes()
    assert closed.mode == "RGBA" and closed.getpixel((0, 0))[3] == 0   # fond transparent (stickers)


@needs_ffmpeg
def test_render_scene_and_vertical_follow(cfg, tmp_path):
    sh = shot(cfg, tmp_path)
    out = puppet.render_scene(sh, tmp_path / "s.mp4")
    assert abs(ffmpeg.duration(out) - 3.0) < 0.2
    vert = puppet.render_scene(sh, tmp_path / "v.mp4", size=(180, 320), window=(0.5, 2.5), follow_speaker=True)
    info = json.loads(__import__("subprocess").run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(vert)], capture_output=True, text=True).stdout)
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (180, 320)
    assert abs(ffmpeg.duration(vert) - 2.0) < 0.2


def test_drawn_plates_for_every_set(cfg, tmp_path):
    for sid, info in cfg.cast["sets"].items():
        p = puppet.draw_plate(sid, info["color"], tmp_path / f"{sid}.png", size=(320, 180))
        assert Path(p).exists()


@needs_ffmpeg
def test_episode_with_puppet_renderer(cfg):
    from toonomatic.pipeline import Pipeline
    cfg.data["animation"]["renderer"] = "puppet"
    cfg.data["plates"]["provider"] = "draw"
    cfg.data["production"]["tiers"]["final"]["overrides"]["animation"]["renderer"] = "puppet"
    cfg.data["stickers"]["enabled"] = False
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=4)
    ep = pipe.store.get(eid)
    assert ep["status"] == "review", ep["info"].get("error")
    assert ep["info"]["director"]["scores"]["animation"] >= 6     # image animée, pas figée
    assert (cfg.series_dir / "plates" / "rue.png").exists()
