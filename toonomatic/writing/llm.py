"""Fournisseurs LLM interchangeables. Tous exposent `generate_json(task, system, user, schema)`.

- ClaudeLLM  : API Anthropic (SDK officiel), sortie JSON contrainte par schéma.
- OllamaLLM  : modèles open-source locaux (Llama, Mistral...) via l'API Ollama, `format=<schema>`.
- MockLLM    : générateur déterministe, sans réseau — pour tester toute la chaîne.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
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


# --------------------------------------------------------------------------- compatible OpenAI
def _violations(schema: dict, data, path: str = "$") -> list[str]:
    """Validation minimale d'un schéma JSON (types, champs requis, énumérations)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            return [f"{path}: objet attendu"]
        out = [f"{path}.{k}: champ manquant" for k in schema.get("required", []) if k not in data]
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                out += _violations(sub, data[k], f"{path}.{k}")
        return out
    if t == "array":
        if not isinstance(data, list):
            return [f"{path}: liste attendue"]
        out = []
        for i, item in enumerate(data[:200]):
            out += _violations(schema.get("items") or {}, item, f"{path}[{i}]")
        return out
    if t == "string":
        if not isinstance(data, str):
            return [f"{path}: texte attendu"]
        if schema.get("enum") and data not in schema["enum"]:
            return [f"{path}: valeur « {data} » hors liste {schema['enum']}"]
    if t in ("integer", "number") and (isinstance(data, bool) or not isinstance(data, (int, float))):
        return [f"{path}: nombre attendu"]
    return []


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.index("\n") + 1:] if "\n" in text else text
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise json.JSONDecodeError("pas d'objet JSON", text, 0)
    return json.loads(text[a:b + 1])


class RateLimited(LLMError):
    pass


class OpenAICompatLLM:
    """Tout serveur « compatible OpenAI » : Ollama (local ou cloud), Groq, Mistral, OpenRouter,
    NVIDIA NIM, vLLM, LM Studio… Un seul connecteur pour tous les modèles open source.

    La sortie est contrainte par schéma quand le serveur le permet (`json_schema`), sinon en
    mode `json_object` avec le schéma dans le prompt ; elle est TOUJOURS revalidée ici, et une
    réponse invalide est renvoyée une fois au modèle avec la liste de ses erreurs."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.base = cfg["base_url"].rstrip("/")
        self.model = os.environ.get(cfg.get("model_env", ""), "") or cfg["model"]
        self.key = os.environ.get(cfg.get("api_key_env", ""), "") if cfg.get("api_key_env") else ""
        self.timeout = int(cfg.get("timeout_s", 180))
        self.max_tokens = int(cfg.get("max_tokens", 4096))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.strict = cfg.get("json_schema", True)
        self.max_wait = float(cfg.get("max_rate_limit_wait_s", 25))

    def _post(self, messages: list, response_format: dict):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return requests.post(f"{self.base}/chat/completions", headers=headers, timeout=self.timeout, json={
            "model": self.model, "messages": messages, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "response_format": response_format})

    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        import time
        hint = ("\n\nRéponds UNIQUEMENT par un objet JSON valide conforme à ce schéma JSON :\n"
                + json.dumps(schema, ensure_ascii=False))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user + hint}]
        fmt = {"type": "json_schema", "json_schema": {"name": task, "schema": schema, "strict": True}} \
            if self.strict else {"type": "json_object"}
        waited = False
        last = ""
        for attempt in range(4):
            try:
                r = self._post(messages, fmt)
            except requests.RequestException as e:
                raise LLMError(f"[{task}] {self.name} injoignable : {e}") from e
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 999) if (r.headers.get("retry-after") or "").replace(
                    ".", "", 1).isdigit() else 999
                if wait <= self.max_wait and not waited:
                    waited = True
                    time.sleep(wait + 0.5)
                    continue
                raise RateLimited(f"[{task}] {self.name} : quota atteint")
            if r.status_code == 400 and fmt["type"] == "json_schema":
                fmt = {"type": "json_object"}  # ce modèle ne gère pas la sortie contrainte par schéma
                continue
            if r.status_code >= 400:
                raise LLMError(f"[{task}] {self.name} HTTP {r.status_code} : {r.text[:200]}")
            try:
                data = _extract_json(r.json()["choices"][0]["message"]["content"] or "")
                bad = _violations(schema, data)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                data, bad = None, [f"JSON illisible : {e}"]
            # Valeur hors liste (décor, émotion, geste inventés) : une relance, puis on accepte —
            # le scénariste remplace ces valeurs par un choix valide. Le reste est bloquant.
            soft = bool(bad) and all("hors liste" in x for x in bad)
            if not bad or (soft and attempt >= 1):
                return data
            last = "; ".join(bad[:6])
            messages = messages[:2] + [{"role": "user", "content":
                                        f"Ta réponse précédente était invalide ({last}). Renvoie l'objet JSON corrigé."}]
        raise LLMError(f"[{task}] {self.name} : réponse invalide après corrections ({last})")


class ChainLLM:
    """SALLE D'ÉCRITURE : plusieurs modèles open source en relais.

    - Chaque langue a son ordre de préférence (`routes`) : un modèle français pour l'humour
      français, un autre pour l'anglais — les deux versions ne sortent pas du même moule.
    - Quota gratuit épuisé ou panne chez l'un → le suivant prend le relais, sans intervention."""

    def __init__(self, members: list[tuple[str, LLM]], routes: dict[str, list[str]] | None = None):
        if not members:
            raise LLMError("chaîne LLM vide")
        self.members = members
        self.routes = routes or {}
        self.used: dict[str, int] = {}

    def _order(self, user: str) -> list[tuple[str, LLM]]:
        import re
        m = re.search(r'"(?:lang|target)":\s*"([a-z]{2})"', user) or re.search(r"LANGUE(?: DE SORTIE)?\s*:\s*([a-z]{2})", user)
        pref = self.routes.get(m.group(1), []) if m else []
        by = dict(self.members)
        return [(n, by[n]) for n in pref if n in by] + [(n, x) for n, x in self.members if n not in pref]

    def generate_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        errors = []
        for name, llm in self._order(user):
            try:
                out = llm.generate_json(task, system, user, schema)
                self.used[name] = self.used.get(name, 0) + 1
                return out
            except LLMError as e:
                log.warning("%s → modèle suivant", e)
                errors.append(str(e))
        raise LLMError(f"[{task}] tous les modèles ont échoué : " + " | ".join(errors)[:800])


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
        if task == "sticker_caption":
            return {"captions": [" ".join(t.replace("[", " ").split("]")[-1].split()[:3]) or "Non." for t in ctx["lines"]]}
        if task == "director_review":
            return {"histoire": 7, "personnages": 8, "notes": "mock : rythme correct, chute à renforcer.",
                    "fixes": []}
        if task == "rewrite_line":
            return {"text": ctx.get("text", "") + (" [rire]" if ctx.get("lang") == "fr" else " [laughs]")}
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


def available_endpoints(cfg: dict) -> list[str]:
    """Points d'accès utilisables maintenant (clé présente, ou serveur sans clé comme Ollama local)."""
    out = []
    for name in cfg.get("chain") or list((cfg.get("endpoints") or {}).keys()):
        ep = (cfg.get("endpoints") or {}).get(name)
        if ep and (not ep.get("api_key_env") or os.environ.get(ep["api_key_env"])):
            out.append(name)
    return out


def _has_claude_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def llm_problem(cfg: dict) -> str | None:
    p = cfg.get("provider")
    if p == "claude" and not _has_claude_key():
        return "clé ANTHROPIC_API_KEY absente"
    if p == "chain" and not available_endpoints(cfg):
        return "aucune clé de modèle open source (GROQ_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY…)"
    if p == "auto" and not available_endpoints(cfg) and not _has_claude_key():
        return "aucune clé de modèle (GROQ_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY… ou ANTHROPIC_API_KEY)"
    return None


def describe(cfg: dict) -> str:
    """Nom lisible du scénariste réellement utilisé."""
    p = cfg.get("provider", "mock")
    if llm_problem(cfg):
        return cfg.get("fallback", p)
    if p in ("chain", "auto"):
        eps = available_endpoints(cfg)
        return " → ".join(eps + (["claude"] if p == "auto" and _has_claude_key() else [])) or p
    return p


def make_llm(cfg: dict) -> LLM:
    provider = cfg.get("provider", "mock")
    why = llm_problem(cfg)
    if why and cfg.get("fallback"):
        log.warning("LLM %s indisponible (%s) → repli sur %s (MODE DÉMO : scénarios factices)",
                    provider, why, cfg["fallback"])
        provider = cfg["fallback"]
    elif why:
        raise LLMError(why)
    if provider in ("chain", "auto"):
        eps = cfg.get("endpoints") or {}
        members: list[tuple[str, LLM]] = [(n, OpenAICompatLLM(n, eps[n])) for n in available_endpoints(cfg)]
        if provider == "auto" and _has_claude_key():
            members.append(("claude", ClaudeLLM(cfg.get("claude", {}))))
        return ChainLLM(members, cfg.get("routes"))
    if provider == "claude":
        return ClaudeLLM(cfg.get("claude", {}))
    if provider == "ollama":
        return OllamaLLM(cfg.get("ollama", {}))
    if provider == "mock":
        return MockLLM(cfg.get("mock", {}))
    raise LLMError(f"Fournisseur LLM inconnu: {provider}")


__all__ = ["LLM", "LLMError", "ClaudeLLM", "OllamaLLM", "OpenAICompatLLM", "ChainLLM", "MockLLM", "make_llm"]
