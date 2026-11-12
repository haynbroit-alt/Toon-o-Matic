import importlib.util
import json
import subprocess

import pytest

from conftest import needs_ffmpeg
from toonomatic import brand, stickers
from toonomatic.models import Line, Scene, Script
from toonomatic.writing.llm import MockLLM

needs_bpy = pytest.mark.skipif(importlib.util.find_spec("bpy") is None, reason="pip install bpy")


def test_delta_e_separates_brand_colors():
    assert brand.delta_e("#e8833a", "#e8833a") == 0
    assert brand.delta_e("#e8833a", "#2f6fe0") > 60          # orange / bleu : très distincts
    assert brand.delta_e("#2f6fe0", "#3466e0") < 10          # deux bleus voisins : sous le seuil de 25


def test_silhouette_normalization_and_iou():
    tall = [[2 <= x <= 3 for x in range(10)] for _ in range(10)]          # « I »
    wide = [[y >= 4 for _ in range(10)] for y in range(10)]                # bloc large
    nt, st = brand.normalize(tall, 16)
    nw, sw = brand.normalize(wide, 16)
    assert st["aspect"] < 0.3 and sw["aspect"] > 1.5
    assert brand.iou(nt, nt) == 1.0 and brand.iou(nt, nw) < 0.3


def test_select_moments_prefers_meme_gestures_and_is_diverse():
    lines = [Line("momo", "Bon, on discute du budget annuel de la copropriété en détail", "neutral", "talk"),
             Line("momo", "C'est une opportunité !", "smug", "point"),
             Line("lina", "Non.", "sarcastic", "facepalm"),
             Line("lina", "Non, non et non.", "sarcastic", "facepalm")]   # même combinaison → 1 seul
    s = Script("fr", "t", [Scene("s01", "rue", ["momo", "lina"], "x", lines, highlight=True)])
    picked = stickers.select_moments(s, 5)
    assert [(m["character"], m["action"]) for m in picked] == [("lina", "facepalm"), ("momo", "point")]
    assert stickers.select_moments(s, 5, seen={("lina", "sarcastic", "facepalm")})[0]["character"] == "momo"


def test_captions_are_short_and_local():
    m = {"character": "momo", "emotion": "smug", "action": "point", "line": "J'ai un plan génial les amis ce soir"}
    caps = stickers.captions(MockLLM(), "fr", [m], {"momo": {"catchphrases": {"fr": ["J'ai un plan."]}}})
    assert caps == ["J'ai un plan."]                                   # réplique fétiche, pas une phrase coupée
    assert stickers.captions(MockLLM(), "en", [m], {}) == ["Nailed it."]
    assert stickers.captions(MockLLM(), "fr", [{**m, "line": "Non."}], {}) == ["Non."]


@needs_ffmpeg
def test_compose_whatsapp_spec(tmp_path):
    pose = tmp_path / "pose.png"   # personnage factice : un disque opaque sur fond transparent
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=orange@1:s=300x400,format=rgba",
                    "-vf", "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(hypot(X-150,Y-200),120),255,0)'",
                    "-frames:v", "1", str(pose)], check=True)
    out = stickers.compose(pose, "J'ai un plan", tmp_path / "s.png")
    webp = stickers.to_webp(out, tmp_path / "s.webp")
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                     capture_output=True, text=True).stdout)["streams"][0]
    assert (info["width"], info["height"]) == (512, 512)
    assert webp.stat().st_size <= stickers.MAX_WEBP_BYTES
    mask, w, h = brand.alpha_mask(out)
    assert not any(mask[0]) and not any(row[0] for row in mask)            # marge transparente sur les bords
    assert sum(map(sum, mask)) > sum(map(sum, brand.alpha_mask(pose)[0]))  # contour blanc ajouté


@needs_bpy
@needs_ffmpeg
def test_brand_check_on_series_cast(cfg):
    r = brand.check(cfg)
    assert r["ok"], r["problems"]
    assert {s["a"] + s["b"] for s in r["silhouettes"]} == {"momolina", "momotonton", "linatonton"}
    assert (cfg.series_dir / "brand" / "silhouettes.png").exists()


@needs_bpy
@needs_ffmpeg
def test_brand_check_flags_lookalikes(cfg):
    sig = cfg.cast["characters"]["momo"]["signature"]
    cfg.cast["characters"]["lina"]["signature"] = {**sig, "outfit_color": "#e98a40"}  # clone de Momo
    r = brand.check(cfg)
    assert not r["ok"]
    assert any("momo et lina" in p for p in r["problems"])


@needs_bpy
@needs_ffmpeg
def test_stickers_extracted_and_packed(cfg):
    from toonomatic.pipeline import Pipeline
    cfg.data["director"]["enabled"] = False
    pipe = Pipeline(cfg)
    [eid] = pipe.produce(1, seed=3)
    new = stickers.extract(cfg, MockLLM(), pipe.epdir(eid), eid, n=3)
    assert 1 <= len(new) <= 3 and all(set(e["captions"]) == {"fr", "en"} for e in new)
    pack = cfg.series_dir / "stickers" / "packs" / "fr"
    contents = json.loads((pack / "contents.json").read_text())
    assert len(contents["sticker_packs"][0]["stickers"]) == len(new) and (pack / "tray.png").exists()
    stickers.extract(cfg, MockLLM(), pipe.epdir(eid), eid, n=3)  # 2e passage : seulement de NOUVELLES expressions
    ids = [e["id"] for e in stickers.load_index(cfg)]
    assert len(ids) == len(set(ids))
