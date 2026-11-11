import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from conftest import needs_ffmpeg
from toonomatic.continuity import aired, load_facts, record_episode
from toonomatic.models import Line, Scene, Script
from toonomatic.packaging import chapters
from toonomatic.voice import lipsync
from toonomatic.voice.tts import HttpTTS, MockTTS
from toonomatic.writing.llm import MockLLM
from toonomatic.writing.showrunner import Showrunner


def test_series_bible_is_injected(cfg):
    bible = Showrunner(cfg, MockLLM())._bible("fr")
    assert "Les Voisins du 3e" in bible and "RÈGLES DE CONTINUITÉ" in bible
    assert "J'ai un plan." in bible and "apparence:" in bible
    assert cfg.style["shading"] == "toon"


def test_continuity_memory_roundtrip(cfg):
    s = Script("fr", "Le colis", [Scene("s01", "rue", ["momo"], "x", [Line("momo", "Salut")])])
    facts = record_episode(cfg, MockLLM(), "ep1", "Un colis perdu", s)
    assert facts and load_facts(cfg)[-1]["episode"] == "ep1"
    assert aired(cfg)[-1]["title"] == "Le colis"
    assert "CE QUE LA SÉRIE A DÉJÀ ÉTABLI" in Showrunner(cfg, MockLLM())._bible("fr")


def test_visemes_from_envelope():
    cues = lipsync.from_envelope([0, 0, 0.5, 0.9, 0.9, 0.2, 0, 0], fps=4)
    assert cues[0] == {"t": 0.0, "v": "X"} and cues[-1]["v"] == "X"
    assert {c["v"] for c in cues} <= set(lipsync.VISEMES)
    assert any(c["v"] == "D" for c in cues)


def test_rhubarb_parsing(tmp_path, monkeypatch):
    fake = tmp_path / "rhubarb"
    fake.write_text("#!/usr/bin/env python3\nimport sys,json\no=sys.argv[sys.argv.index('-o')+1]\n"
                    "json.dump({'mouthCues':[{'start':0.0,'end':0.1,'value':'X'},{'start':0.1,'end':0.3,'value':'D'}]},"
                    "open(o,'w'))\n")
    fake.chmod(0o755)
    ls = lipsync.LipSync({"provider": "rhubarb", "rhubarb_path": str(fake)})
    assert ls.visemes(tmp_path / "a.wav", "Salut [rire]", [], 24) == [{"t": 0.0, "v": "X"}, {"t": 0.1, "v": "D"}]
    broken = lipsync.LipSync({"provider": "rhubarb", "rhubarb_path": str(tmp_path / "absent")})
    assert broken.visemes(tmp_path / "a.wav", "x", [0.9, 0.9], 24)  # repli enveloppe


def test_chapters_follow_youtube_rules():
    scenes = [Scene(f"s{i}", "rue", ["momo"], f"scène {i}", start=t) for i, t in enumerate([0, 5, 30, 70, 100])]
    ch = chapters(Script("fr", "t", scenes), ["A", "B", "C", "D", "E"])
    assert ch[0] == (0.0, "A") and len(ch) >= 3
    assert all(b[0] - a[0] >= 10 for a, b in zip(ch, ch[1:]))
    assert chapters(Script("fr", "t", scenes[:2]), []) == []  # < 3 chapitres → aucun


@needs_ffmpeg
def test_http_tts_provider(tmp_path):
    src = tmp_path / "src.wav"
    MockTTS({"sample_rate": 16000}).synthesize("Bonjour tout le monde", "fr", {}, "neutral", src)
    payload = src.read_bytes()
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    tts = HttpTTS({"sample_rate": 22050, "http": {"url": f"http://127.0.0.1:{srv.server_port}/tts",
                                                 "json": {"text": "{clean_text}", "lang": "{lang}", "ref": "{ref}"}}})
    out = tts.synthesize("Salut [rire]", "fr", {"chatterbox_ref": "voix.wav"}, "happy", tmp_path / "o.wav")
    assert seen == {"text": "Salut", "lang": "fr", "ref": "voix.wav"}
    with wave.open(str(out)) as w:
        assert w.getframerate() == 22050 and w.getnchannels() == 1
