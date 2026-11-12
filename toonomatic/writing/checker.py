"""VÉRIFICATEUR : contrôle un scénario SANS aucune IA (règles déterministes, gratuites, instantanées).

Il ne juge pas si c'est drôle — c'est le rôle du réalisateur (avec modèle) ou de l'humain. Il
attrape ce qui casserait la production ou la cohérence de la série :

    langue       une version écrite dans la mauvaise langue ; deux versions identiques
    distribution personnage annoncé qui ne parle jamais ; un personnage qui monopolise
    voix         répliques trop longues pour être dites et sous-titrées
    jeu          émotions/gestes monotones → animation plate
    durée        trop court/long ; écart FR/EN anormal
    identité     réplique fétiche dite par le mauvais personnage, ou usée (> 2 fois)
    continuité   termes interdits par la bible ; intrigue trop proche d'un épisode diffusé

Niveaux : « bloquant » (ne pas produire), « à revoir » (produire possible), « info ».
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

from ..config import Config
from ..models import Premise, Script
from ..voice.tts import strip_tags

STOP = {
    "fr": {"le", "la", "les", "un", "une", "des", "est", "et", "je", "tu", "il", "elle", "on", "nous", "vous",
           "pas", "que", "qui", "de", "du", "ce", "ça", "c", "j", "t", "pour", "avec", "mais", "moi", "toi",
           "mon", "ton", "son", "au", "en", "y", "ne", "se", "sur", "dans", "quoi", "oui", "non", "très"},
    "en": {"the", "a", "an", "is", "are", "and", "i", "you", "he", "she", "it", "we", "they", "not", "that",
           "this", "of", "to", "for", "with", "but", "me", "my", "your", "his", "her", "in", "on", "what",
           "yes", "no", "very", "don", "t", "s", "it's", "was", "have", "do", "just", "there"},
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zà-ÿœ']+", strip_tags(text).lower())


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", strip_tags(text).lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", "".join(c for c in t if not unicodedata.combining(c))).split())


def guess_lang(text: str) -> str | None:
    w = _words(text)
    if len(w) < 12:
        return None
    score = {l: sum(1 for x in w if x in s) for l, s in STOP.items()}
    best = max(score, key=score.get)
    other = max(v for l, v in score.items() if l != best)
    return best if score[best] >= 1.5 * max(1, other) else None


def _content(text: str) -> set[str]:
    allstop = STOP["fr"] | STOP["en"]
    return {w for w in _norm(text).split() if len(w) > 3 and w not in allstop}


def check(cfg: Config, premise: Premise, scripts: dict[str, Script]) -> dict:
    from ..continuity import aired
    ep, cast = cfg["episode"], cfg.cast["characters"]
    out: list[dict] = []

    def add(level, topic, msg, lang=None):
        out.append({"level": level, "topic": topic, "lang": lang, "message": msg})

    est = {}
    for lang, s in scripts.items():
        lines = s.lines
        if not lines:
            add("bloquant", "distribution", "aucune réplique", lang)
            continue
        # langue
        g = guess_lang(" ".join(l.text for l in lines))
        if g and g != lang and lang in STOP:
            add("bloquant", "langue", f"la version « {lang} » semble écrite en « {g} »", lang)
        # distribution
        by = Counter(l.character for l in lines)
        for c in premise.cast:
            if c not in by:
                add("à revoir", "distribution", f"{cast[c]['display']} est annoncé mais ne dit rien", lang)
        top, n = by.most_common(1)[0]
        if len(by) > 1 and n / len(lines) > 0.65:
            add("à revoir", "distribution", f"{cast[top]['display']} dit {n * 100 // len(lines)} % des répliques", lang)
        # voix
        long_ = [(sc.id, len(_words(l.text))) for sc in s.scenes for l in sc.lines if len(_words(l.text)) > 28]
        if long_:
            add("à revoir", "voix", f"{len(long_)} réplique(s) de plus de 28 mots (ex. scène {long_[0][0]}, "
                f"{long_[0][1]} mots) : difficiles à dire et à sous-titrer", lang)
        # jeu
        flat = sum(1 for l in lines if l.emotion == "neutral" and l.action in ("talk", "idle"))
        if len(lines) >= 8 and flat / len(lines) > 0.6:
            add("à revoir", "jeu", f"{flat * 100 // len(lines)} % des répliques sont neutres sans geste : "
                "l'animation sera plate", lang)
        # durée
        est[lang] = s.estimated_minutes(float(cfg["languages"][lang]["words_per_minute"]))
        lo, hi = float(ep["min_minutes"]), float(ep["max_minutes"])
        if est[lang] < lo * 0.7 or est[lang] > hi * 1.3:
            add("à revoir", "durée", f"durée estimée {est[lang]:.1f} min pour {lo:g}–{hi:g} min visées", lang)
        # identité : répliques fétiches
        for cid, c in cast.items():
            for phrase in (c.get("catchphrases") or {}).get(lang, []):
                key = _norm(phrase)
                if len(key) < 6:
                    continue
                said = [l.character for l in lines if key in _norm(l.text)]
                wrong = sorted({cast[x]["display"] for x in said if x != cid})
                if wrong:
                    add("à revoir", "identité", f"« {phrase} » est la réplique de {c['display']}, "
                        f"mais elle est dite par {', '.join(wrong)}", lang)
                if said.count(cid) > 2:
                    add("info", "identité", f"« {phrase} » revient {said.count(cid)} fois : à doser", lang)
        # continuité : termes interdits par la bible
        text = " " + _norm(" ".join(l.text for l in lines)) + " "
        for item in (cfg.series or {}).get("forbidden_terms") or []:
            term = item["term"] if isinstance(item, dict) else str(item)
            if f" {_norm(term)} " in text:
                why = item.get("reason", "") if isinstance(item, dict) else ""
                add("bloquant", "continuité", f"terme interdit par la bible : « {term} »" + (f" ({why})" if why else ""), lang)
        # répétitions involontaires (les répliques courtes peuvent être un running gag)
        rep = [t for t, k in Counter(_norm(l.text) for l in lines if len(_words(l.text)) > 5).items() if k >= 3]
        if rep:
            add("info", "voix", f"{len(rep)} réplique(s) longue(s) répétée(s) 3 fois ou plus", lang)

    langs = list(scripts)
    if len(langs) >= 2:
        a, b = scripts[langs[0]].lines, scripts[langs[1]].lines
        same = sum(1 for x, y in zip(a, b) if _norm(x.text) == _norm(y.text) and len(_words(x.text)) > 3)
        if a and same / max(1, min(len(a), len(b))) > 0.5:
            add("bloquant", "langue", f"les versions {langs[0]} et {langs[1]} ont le même texte")
        if len(est) >= 2 and min(est.values()) > 0 and max(est.values()) / min(est.values()) > 1.6:
            add("à revoir", "durée", "écart de durée important entre les versions : " +
                ", ".join(f"{l} {m:.1f} min" for l, m in est.items()))
    # continuité : déjà-vu
    mine = [m for m in (_content(premise.logline), _content(premise.situation)) if len(m) >= 3]
    for old in aired(cfg, 200):
        theirs = _content(old.get("logline", ""))
        # part des mots importants en commun, rapportée au plus court des deux pitchs
        if len(theirs) >= 3 and any(len(m & theirs) / min(len(m), len(theirs)) > 0.7 for m in mine):
            add("à revoir", "continuité", f"intrigue très proche d'un épisode déjà diffusé : « {old.get('title', old['episode'])} »")
    counts = Counter(f["level"] for f in out)
    return {"ok": counts["bloquant"] == 0, "blocking": counts["bloquant"], "to_review": counts["à revoir"],
            "info": counts["info"], "findings": out,
            "scope": "Règles automatiques sans IA : langue, distribution, voix, jeu, durée, répliques fétiches, "
                     "termes interdits, déjà-vu. La qualité comique et la continuité fine restent à juger."}
