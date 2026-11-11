"""Fournisseurs LLM interchangeables. Tous exposent `generate_json(task, system, user, schema)`.

- ClaudeLLM  : API Anthropic (SDK officiel), sortie JSON contrainte par schéma.
- OllamaLLM  : modèles open-source locaux (Llama, Mistral...) via l'API Ollama, `format=<schema>`.
- MockLLM    : générateur déterministe, sans réseau — pour tester toute la chaîne.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict: ...


# --------------------------------------------------------------------------- Claude
class ClaudeLLM:
    def __init__(self, cfg: dict):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("pip install 'toonomatic[claude]' pour utiliser Claude") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY / profil `ant auth login`
        self.model = cfg.get("model", "claude-opus-5-5")
        self.effort = cfg.get("effort", "high")
        self.max_tokens = int(cfg.get("max_tokens", 64000))
        self.fallback = bool(cfg.get("server_side_fallback", True))

    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            # Le prompt système (bible de la série) est stable → mis en cache entre les appels.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self.fallback:
                with self.client.beta.messages.stream(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs) as s:
                    msg = s.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as s:
                    msg = s.get_final_message()
        except self._anthropic.RateLimitError as e:
            raise LLMError(f"[{task}] limite de débit atteinte, réessayer plus tard") from e
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"[{task}] erreur API {e.status_code}: {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise LLMError(f"[{task}] erreur réseau vers l'API Claude") from e

        if msg.stop_reason == "refusal":
            cat = getattr(msg.stop_details, "category", None) if msg.stop_details else None
            raise LLMError(f"[{task}] requête déclinée par le modèle (catégorie: {cat})")
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"[{task}] sortie tronquée (max_tokens={self.max_tokens})")
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"[{task}] aucune sortie texte")
        return json.loads(text)


# --------------------------------------------------------------------------- Ollama
class OllamaLLM:
    def __init__(self, cfg: dict):
        self.host = cfg.get("host", "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model", "mistral-nemo")
        self.timeout = cfg.get("timeout_s", 600)

    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": self.model, "stream": False, "format": schema,
            "options": {"temperature": 0.9, "num_ctx": 32768},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            raise LLMError(f"[{task}] Ollama: {e}") from e


# --------------------------------------------------------------------------- Mock
class MockLLM:
    """Produit des scripts plausibles et déterministes (graine = contenu du prompt)."""

    FR = ["Attends, attends, attends.", "Mais t'es sérieux là ?", "J'ai un plan. Écoute-moi bien.",
          "Non mais c'est pas possible, ça.", "Tu vois, c'est exactement ça le problème.",
          "[soupir] Pourquoi c'est toujours moi ?", "Je connais quelqu'un qui connaît quelqu'un.",
          "On se calme, on respire, on réfléchit.", "Franchement, ça pouvait pas être pire.",
          "Ah si, ça pouvait être pire. [rire]", "C'est pas un problème, c'est une opportunité.",
          "Bon. On va faire comme si j'avais rien entendu."]
    EN = ["Right. Brilliant. Lovely.", "I'm not saying it's your fault, I'm just saying it is.",
          "[sighs] Of course it is.", "I have a plan. It's not a good plan, but it is a plan.",
          "Could be worse.", "[awkward pause] It's got worse.", "I know a guy. He knows a guy.",
          "Let's all just stay calm and British about this.", "That's not ideal, is it?",
          "No, no, it's fine. Everything's fine. [laughs]", "Bit of a situation, really.",
          "I'm going to pretend I didn't hear that."]
    ACTIONS = ["talk", "talk_hands", "angry", "facepalm", "laugh", "shrug", "point", "shocked", "idle"]
    EMOTIONS = ["neutral", "happy", "angry", "surprised", "sarcastic", "smug", "sad"]

    def __init__(self, cfg: dict | None = None, lines_per_scene: int = 34):
        self.lines_per_scene = lines_per_scene

    def _rng(self, *parts: str) -> random.Random:
        return random.Random(hashlib.sha256("|".join(parts).encode()).hexdigest())

    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        ctx = json.loads(user[user.index("<context>") + 9:user.index("</context>")]) \
            if "<context>" in user else {}
        rng = self._rng(task, user)
        if task == "premise":
            sets, cast = ctx["sets"], ctx["cast"]
            beats = [{"id": f"s{i + 1:02d}", "set": sets[i % len(sets)],
                      "characters": cast if i % 3 == 0 else rng.sample(cast, k=min(2, len(cast))),
                      "purpose": f"Étape {i + 1} de l'escalade autour de : {ctx['situation'][:60]}"}
                     for i in range(ctx["n_beats"])]
            return {"logline": ctx["situation"], "running_gag": "Le tonton « connaît quelqu'un ».",
                    "beats": beats}
        if task == "title":
            return {"title": ("Épisode : " if ctx.get("lang") == "fr" else "Episode: ")
                    + ctx["situation"].split(",")[0][:40]}
        if task in ("scene", "extend"):
            pool = self.FR if ctx.get("lang") == "fr" else self.EN
            chars = ctx["beat"]["characters"]
            n = self.lines_per_scene if task == "scene" else self.lines_per_scene // 2
            lines = []
            for i in range(n):
                txt = " ".join(rng.sample(pool, k=rng.choice([1, 2, 2, 3])))
                lines.append({"character": chars[i % len(chars)], "text": txt,
                              "emotion": rng.choice(self.EMOTIONS), "action": rng.choice(self.ACTIONS)})
            return {"lines": lines, "summary": ctx["beat"]["purpose"], "funniness": rng.randint(4, 9)}
        if task == "translate":
            prefix = "[EN] " if ctx["target"] == "en" else "[FR] "
            return {"lines": [prefix + t for t in ctx["lines"]]}
        if task == "continuity":
            chars = ctx.get("characters") or ["momo"]
            return {"facts": [{"fact": f"{chars[0]} a juré de ne plus jamais parler de « {ctx['logline'][:40]} ».",
                               "characters": chars[:1], "kind": "running_gag"}]}
        if task == "metadata":
            fr = ctx.get("lang") == "fr"
            t = ctx.get("title", "Épisode")
            return {"youtube_title": t[:90], "youtube_description": ctx.get("logline", ""),
                    "tags": ["animation", "humour" if fr else "comedy", "3D", "webserie" if fr else "cartoon"],
                    "tiktok_caption": (t + " 😂")[:150],
                    "hashtags": ["#animation", "#humour" if fr else "#comedy", "#3danimation"],
                    "thumbnail_text": t.split(":")[-1].strip()[:28] or t[:28],
                    "chapters": [("Partie " if fr else "Part ") + str(i + 1) for i in range(20)]}
        if task == "content_review":
            return {"risk": 0.1, "issues": [], "notes": "mock: aucun problème détecté"}
        raise LLMError(f"MockLLM: tâche inconnue {task}")


def make_llm(cfg: dict) -> LLM:
    provider = cfg.get("provider", "mock")
    if provider == "claude":
        return ClaudeLLM(cfg.get("claude", {}))
    if provider == "ollama":
        return OllamaLLM(cfg.get("ollama", {}))
    if provider == "mock":
        return MockLLM(cfg.get("mock", {}))
    raise LLMError(f"Fournisseur LLM inconnu: {provider}")


__all__ = ["LLM", "LLMError", "ClaudeLLM", "OllamaLLM", "MockLLM", "make_llm"]
