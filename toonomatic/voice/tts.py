"""Fournisseurs TTS interchangeables + construction de la piste de dialogue.

Tous les fournisseurs produisent un WAV mono 16 bits au taux `tts.sample_rate`.
- xtts       : Coqui XTTS v2 (clonage de voix multilingue à partir d'un échantillon).
- bark       : Suno Bark (rires, soupirs, hésitations très naturels).
- elevenlabs : API cloud (qualité/émotion maximale, payant).
- mock       : "babillage" synthétique FFmpeg dont la durée suit la longueur du texte.
"""
from __future__ import annotations

import array
import logging
import os
import re
import wave
from pathlib import Path
from typing import Protocol

import requests

from .. import ffmpeg

log = logging.getLogger(__name__)
TAG_RE = re.compile(r"\[[^\]]+\]")


class TTSError(RuntimeError):
    pass


def strip_tags(text: str) -> str:
    return re.sub(r"\s{2,}", " ", TAG_RE.sub("", text)).strip()


class TTS(Protocol):
    def synthesize(self, text: str, lang: str, voice: dict, emotion: str, out: Path) -> Path: ...


def _to_wav(src: Path, out: Path, sr: int) -> Path:
    ffmpeg.run(["-i", src, "-ac", 1, "-ar", sr, "-sample_fmt", "s16", out])
    return out


class MockTTS:
    def __init__(self, cfg: dict):
        self.sr = int(cfg.get("sample_rate", 48000))

    def synthesize(self, text, lang, voice, emotion, out):
        clean = strip_tags(text) or "..."
        secs = max(0.8, len(clean) / 14.5 + (0.6 if TAG_RE.search(text) else 0))
        pitch = int(voice.get("mock_pitch", 180)) * (1.15 if emotion in ("angry", "surprised") else 1)
        # sinus modulé en amplitude à ~5 Hz → enveloppe "syllabique" pour tester le lip-sync
        ffmpeg.run(["-f", "lavfi", "-i", f"sine=frequency={pitch:.0f}:sample_rate={self.sr}:duration={secs:.2f}",
                    "-af", "tremolo=f=5:d=0.9,volume=0.4", "-ac", 1, "-sample_fmt", "s16", out])
        return out


class XTTS:
    def __init__(self, cfg: dict):
        self.sr = int(cfg.get("sample_rate", 48000))
        c = cfg.get("xtts", {})
        try:
            from TTS.api import TTS as CoquiTTS  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise TTSError("pip install 'toonomatic[xtts]'") from e
        self.model = CoquiTTS(c.get("model", "tts_models/multilingual/multi-dataset/xtts_v2")).to(c.get("device", "cuda"))

    def synthesize(self, text, lang, voice, emotion, out):  # pragma: no cover - GPU
        wav = voice.get("xtts_speaker_wav")
        if not wav or not Path(wav).exists():
            raise TTSError(f"échantillon de voix XTTS manquant : {wav}")
        tmp = out.with_suffix(".raw.wav")
        self.model.tts_to_file(text=strip_tags(text), language=lang, speaker_wav=wav, file_path=str(tmp))
        _to_wav(tmp, out, self.sr)
        tmp.unlink(missing_ok=True)
        return out


class BarkTTS:
    def __init__(self, cfg: dict):
        self.sr = int(cfg.get("sample_rate", 48000))
        try:
            from bark import SAMPLE_RATE, generate_audio, preload_models  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise TTSError("pip install 'toonomatic[bark]'") from e
        preload_models()
        self._gen, self._bark_sr = generate_audio, SAMPLE_RATE

    def synthesize(self, text, lang, voice, emotion, out):  # pragma: no cover - GPU
        import numpy as np  # type: ignore
        # Bark comprend nativement [laughs], [sighs], [clears throat], "..." et "—"
        bark_text = text.replace("[rire]", "[laughs]").replace("[soupir]", "[sighs]").replace("[hésite]", "...")
        audio = self._gen(bark_text, history_prompt=voice.get("bark_preset"))
        tmp = out.with_suffix(".raw.wav")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self._bark_sr)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        _to_wav(tmp, out, self.sr)
        tmp.unlink(missing_ok=True)
        return out


class ElevenLabsTTS:
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    # réglages par émotion : plus de "style", moins de stabilité = jeu plus expressif
    EMOTION_SETTINGS = {"angry": (0.25, 0.8), "surprised": (0.3, 0.7), "happy": (0.35, 0.6),
                        "sarcastic": (0.5, 0.5), "sad": (0.55, 0.4)}

    def __init__(self, cfg: dict):
        self.sr = int(cfg.get("sample_rate", 48000))
        c = cfg.get("elevenlabs", {})
        self.model_id = c.get("model_id", "eleven_multilingual_v2")
        self.key = os.environ.get(c.get("api_key_env", "ELEVENLABS_API_KEY"), "")
        if not self.key:
            raise TTSError("clé ElevenLabs absente (variable d'environnement)")

    def synthesize(self, text, lang, voice, emotion, out):  # pragma: no cover - réseau
        vid = voice.get("elevenlabs_voice_id")
        if not vid:
            raise TTSError("elevenlabs_voice_id manquant dans cast.yaml")
        stab, style = self.EMOTION_SETTINGS.get(emotion, (0.5, 0.3))
        r = requests.post(self.URL.format(voice_id=vid), timeout=120,
                          headers={"xi-api-key": self.key, "accept": "audio/mpeg"},
                          json={"text": text if "v3" in self.model_id else strip_tags(text),
                                "model_id": self.model_id, "language_code": lang,
                                "voice_settings": {"stability": stab, "similarity_boost": 0.8, "style": style}})
        if r.status_code != 200:
            raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
        tmp = out.with_suffix(".mp3")
        tmp.write_bytes(r.content)
        _to_wav(tmp, out, self.sr)
        tmp.unlink(missing_ok=True)
        return out


def make_tts(cfg: dict) -> TTS:
    p = cfg.get("provider", "mock")
    cls = {"mock": MockTTS, "xtts": XTTS, "bark": BarkTTS, "elevenlabs": ElevenLabsTTS}.get(p)
    if not cls:
        raise TTSError(f"Fournisseur TTS inconnu: {p}")
    return cls(cfg)


# --------------------------------------------------------------------------- audio utils
def read_wav(path: Path) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise TTSError(f"{path}: WAV mono 16 bits attendu")
        data = array.array("h")
        data.frombytes(w.readframes(w.getnframes()))
        return data, w.getframerate()


def envelope(samples: array.array, sr: int, fps: int) -> list[float]:
    """RMS normalisé par frame vidéo → valeur d'ouverture de bouche (0..1)."""
    hop = max(1, sr // fps)
    vals = []
    for i in range(0, len(samples), hop):
        chunk = samples[i:i + hop]
        vals.append((sum(x * x for x in chunk[::8]) / max(1, len(chunk[::8]))) ** 0.5 if chunk else 0.0)
    peak = max(vals or [1.0]) or 1.0
    return [round(min(1.0, v / peak), 3) for v in vals]


PAUSE_AFTER = {"angry": 0.15, "surprised": 0.2, "sad": 0.6, "sarcastic": 0.5}


def build_dialogue_track(scenes, sr: int, fps: int, out: Path, line_gap: float = 0.3,
                         scene_gap: float = 1.2) -> float:
    """Place chaque réplique sur la timeline (start/end), calcule l'enveloppe et écrit la piste."""
    track = array.array("h")
    t = 0.0
    for scene in scenes:
        scene.start = t
        for ln in scene.lines:
            samples, rate = read_wav(Path(ln.audio))
            if rate != sr:
                raise TTSError(f"{ln.audio}: {rate} Hz au lieu de {sr}")
            ln.start = t
            track.extend(samples)
            t += len(samples) / sr
            ln.end = t
            ln.envelope = envelope(samples, sr, fps)
            gap = PAUSE_AFTER.get(ln.emotion, line_gap)
            track.extend(array.array("h", bytes(2 * int(gap * sr))))
            t += gap
        track.extend(array.array("h", bytes(2 * int(scene_gap * sr))))
        t += scene_gap
        scene.end = t
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(track.tobytes())
    return t
