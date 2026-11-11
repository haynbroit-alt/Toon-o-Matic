from pathlib import Path

from toonomatic.animation.shotlist import camera_plan
from toonomatic.editing.subtitles import make_cues, write_ass, write_srt
from toonomatic.models import Line, Premise, Scene, Script
from toonomatic.store import Store
from toonomatic.voice.tts import envelope, strip_tags
from toonomatic.writing.llm import MockLLM
from toonomatic.writing.showrunner import Showrunner


def test_strip_tags():
    assert strip_tags("Bon. [soupir] On y va [rire]") == "Bon. On y va"


def test_envelope_normalized():
    import array
    env = envelope(array.array("h", [0] * 100 + [10000] * 100), 200, 2)
    assert env == [0.0, 1.0]


def test_cues_split_long_lines_and_keep_timing():
    ln = Line("momo", "mot " * 60, start=10.0, end=20.0)
    cues = make_cues([ln], max_chars=42)
    assert len(cues) > 1
    assert cues[0].start == 10.0 and abs(cues[-1].end - 20.0) < 0.01
    assert all(c.text.count("\n") <= 1 for c in cues)


def test_srt_and_ass_offsets(tmp_path):
    cues = make_cues([Line("momo", "Salut [rire]", start=5.0, end=6.5)])
    srt = write_srt(cues, tmp_path / "a.srt").read_text()
    assert "00:00:05,000 --> 00:00:06,500" in srt and "[rire]" not in srt
    ass = write_ass(cues, tmp_path / "a.ass", {"size": 54}, (1080, 1920), offset=5.0,
                    speaker_colors={"momo": "#e8833a"}).read_text()
    assert "PlayResY: 1920" in ass and "0:00:00.00,0:00:01.50" in ass and "&H3A83E8&" in ass


def test_camera_plan_covers_scene():
    lines = [Line("momo", "a", start=0, end=1), Line("lina", "b", emotion="angry", start=1.3, end=4),
             Line("momo", "c", start=4.3, end=5)]
    sc = Scene("s01", "rue", ["momo", "lina"], "x", lines, start=0, end=6)
    shots = camera_plan(sc)
    assert shots[0]["camera"] == "wide" and shots[0]["start"] == 0
    assert shots[-1]["end"] == 6
    assert any(s["camera"] == "close:lina" for s in shots)
    for a, b in zip(shots, shots[1:]):
        assert a["end"] == b["start"]


def test_showrunner_fits_duration_and_marks_highlight(cfg):
    cfg.data["episode"].update(target_minutes=2, min_minutes=1.5, max_minutes=2.5)
    sr = Showrunner(cfg, MockLLM(lines_per_scene=30))  # trop long → doit être resserré
    premise = sr.premise("ep", {"id": "x", "situation": "Un colis perdu.", "cast": ["momo", "lina"],
                                "sets": ["rue", "salon"]})
    assert isinstance(premise, Premise) and len(premise.beats) >= 4
    script = sr.write_script(premise, "fr")
    wpm = cfg["languages"]["fr"]["words_per_minute"]
    assert script.estimated_minutes(wpm) <= cfg["episode"]["max_minutes"] + 0.01
    assert sum(s.highlight for s in script.scenes) == 1
    assert all(ln.character in ("momo", "lina") for ln in script.lines)
    assert all(ln.action in cfg.cast["actions"] for ln in script.lines)
    assert script.title


def test_showrunner_extends_short_script(cfg):
    cfg.data["episode"].update(target_minutes=3, min_minutes=2.5, max_minutes=4)
    sr = Showrunner(cfg, MockLLM(lines_per_scene=2))
    premise = sr.premise("ep", {"id": "x", "situation": "Test.", "cast": ["momo", "lina"], "sets": ["rue"]})
    before = 2 * len(premise.beats)
    script = sr.write_script(premise, "en")
    assert len(script.lines) > before  # au moins une passe d'allongement


def test_translate_keeps_line_count(cfg):
    sr = Showrunner(cfg, MockLLM())
    s = Script("fr", "t", [Scene("s01", "rue", ["momo"], "x", [Line("momo", f"l{i}") for i in range(130)])])
    out = sr.translate_lines(s, "en")
    assert len(out) == 130 and out[0].startswith("[EN]")


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    st.create("e1", "sit")
    st.update("e1", stage_done="premise", info={"a": 1})
    st.update("e1", stage_done="script_fr", status="review", risk=0.2)
    ep = st.get("e1")
    assert ep["stages"] == ["premise", "script_fr"] and ep["info"]["a"] == 1 and ep["risk"] == 0.2
    st.reset_stages("e1", ["premise"])
    assert st.get("e1")["stages"] == ["premise"]
    assert st.recent_situations(0) == {"sit"}
