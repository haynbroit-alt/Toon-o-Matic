"""Orchestrateur : enchaîne les étapes d'un épisode, de façon idempotente et reprenable.

    premise → script_<lang> → voice_<lang> → subs_<lang> → render_<lang> → edit_<lang> → package_<lang>
            → director (corrections ciblées) → qa → review → (promote → niveau supérieur) → publish

Trois NIVEAUX (config `production.tiers`) : brouillon → validation → final. Le texte (prémisse,
scripts) est écrit une fois ; promouvoir un épisode ne recalcule que ce qui dépend du niveau,
et garde même les voix si le moteur vocal ne change pas.

Chaque étape écrit ses artefacts dans runs/<episode_id>/ et se marque « faite » dans SQLite :
un crash, un « Refaire » ou un changement de fournisseur ne relance que ce qui est nécessaire.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import logging
import time
from pathlib import Path

from . import ffmpeg, qa
from .animation import blender_render, preview_render
from .animation.plates import ensure_plates
from .animation.shotlist import build_shotlist, scene_paths
from .config import Config
from .editing.assemble import assemble_episode, make_cutdown
from .editing.subtitles import make_cues, write_ass, write_srt
from .ideas import pick_situations
from .learning import kpi_weights, load_lessons
from .models import Premise, Script, load_json, save_json
from .continuity import record_episode
from .packaging import build_package
from .publish import publish
from .store import make_store
from . import director
from .voice.lipsync import LipSync
from .voice.tts import build_dialogue_track, make_tts
from .writing.llm import make_llm
from .writing.showrunner import Showrunner

log = logging.getLogger(__name__)
MAX_RETRIES = 3


class Pipeline:
    def __init__(self, cfg: Config):
        ffmpeg.require()
        self.base = cfg
        self.cfg = cfg
        self.store = make_store(cfg)
        self._llm = None
        self._tts_cache: dict[str, object] = {}

    # lazy : on ne charge un modèle GPU que si l'étape en a besoin
    @property
    def llm(self):
        if self._llm is None:
            self._llm = make_llm(self.cfg["llm"])
        return self._llm

    @property
    def tts(self):
        key = self.tts_signature()
        if key not in self._tts_cache:  # un moteur par configuration (les niveaux peuvent en changer)
            self._tts_cache[key] = make_tts(self.cfg["tts"])
        return self._tts_cache[key]

    def tts_signature(self) -> str:
        t = self.cfg["tts"]
        sig = {"provider": t.get("provider"), "sample_rate": t.get("sample_rate"),
               "conf": t.get(t.get("provider"), {}),
               "voices": {k: v["voices"] for k, v in self.cfg.cast["characters"].items()}}
        return hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]

    def tier_of(self, episode_id: str) -> str:
        ep = self.store.get(episode_id) or {}
        return ep.get("info", {}).get("tier") or self.base.get("production.default_tier", "final")

    def _use_tier(self, episode_id: str) -> str:
        tier = self.tier_of(episode_id)
        tiers = self.base.get("production.tiers") or {}
        self.cfg = self.base.tier(tier) if tier in tiers else self.base
        return tier

    def epdir(self, episode_id: str) -> Path:
        return self.cfg.workdir / episode_id

    # ------------------------------------------------------------------ entrée principale
    def produce(self, count: int, seed: int | None = None, tier: str | None = None,
                situation: dict | None = None, run: bool = True) -> list[str]:
        situations = [situation] if situation else \
            pick_situations(self.cfg, self.store, count, seed, kpi_weights(self.store))
        day = dt.date.today().strftime("%Y%m%d")
        existing = {e["id"] for e in self.store.list()}
        ids = []
        n = 1
        for sit in situations:
            while f"{day}-{n:02d}-{sit['id']}" in existing:
                n += 1
            eid = f"{day}-{n:02d}-{sit['id']}"
            existing.add(eid)
            self.store.create(eid, sit["id"])
            self.store.update(eid, info={"tier": tier or self.base.get("production.default_tier", "final")})
            save_json(sit, self.epdir(eid) / "situation.json")
            ids.append(eid)
        if run:
            for eid in ids:
                self.run(eid)
        return ids

    def resume(self) -> list[str]:
        """Reprend les épisodes en file (nouveaux, interrompus ou « Refaire »)."""
        done = []
        for ep in self.store.list("queued") + self.store.list("running") + self.store.list("failed"):
            if ep["info"].get("retries", 0) > MAX_RETRIES:
                continue
            self.run(ep["id"])
            done.append(ep["id"])
        return done

    def run(self, episode_id: str) -> str:
        ep = self.store.get(episode_id)
        tier = self._use_tier(episode_id)
        self.store.update(episode_id, status="running")
        log.info("[%s] niveau de production : %s", episode_id, tier)
        t0 = time.time()
        try:
            self._premise(episode_id)
            for lang in self.cfg.languages:
                self._script(episode_id, lang)
                self._voice(episode_id, lang)
                self._subs(episode_id, lang)
                self._render(episode_id, lang)
                self._edit(episode_id, lang)
                self._package(episode_id, lang)
            self._direct(episode_id)
            status = self._qa_and_review(episode_id)
        except Exception as e:  # noqa: BLE001 - on journalise toute panne d'étape
            log.exception("épisode %s en échec", episode_id)
            self.store.update(episode_id, status="failed",
                              info={"error": f"{type(e).__name__}: {e}"[:2000],
                                    "retries": (ep or {}).get("info", {}).get("retries", 0) + 1})
            self.store.log(episode_id, "failed", {"error": str(e)[:2000]})
            return "failed"
        self.store.log(episode_id, "produced", {"seconds": round(time.time() - t0, 1), "status": status,
                                                "tier": tier})
        if status == "approved":
            self.publish(episode_id)
        return status

    # ------------------------------------------------------------------ étapes
    def _stage(self, episode_id: str, name: str) -> bool:
        return name in self.store.get(episode_id)["stages"]

    def _done(self, episode_id: str, name: str, **info) -> None:
        self.store.update(episode_id, stage_done=name, info=info or None)
        self.store.log(episode_id, "stage", {"stage": name})
        log.info("[%s] ✓ %s", episode_id, name)

    def _premise(self, eid: str) -> None:
        if self._stage(eid, "premise"):
            return
        sit = load_json(self.epdir(eid) / "situation.json")
        sr = Showrunner(self.cfg, self.llm, load_lessons(self.cfg))
        save_json(sr.premise(eid, sit), self.epdir(eid) / "premise.json")
        self._done(eid, "premise")

    def _script(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"script_{lang}"):
            return
        premise = Premise.from_dict(load_json(self.epdir(eid) / "premise.json"))
        sr = Showrunner(self.cfg, self.llm, load_lessons(self.cfg))
        script = sr.write_script(premise, lang)
        save_json(script, self.epdir(eid) / lang / "script.json")
        self._done(eid, f"script_{lang}")

    def _voice(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"voice_{lang}"):
            return
        d = self.epdir(eid) / lang
        script = Script.from_dict(load_json(d / "script.json"))
        chars = self.cfg.cast["characters"]
        for i, ln in enumerate(script.lines):
            out = d / "audio" / f"{i:04d}_{ln.character}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                self.tts.synthesize(ln.text, lang, chars[ln.character]["voices"][lang], ln.emotion, out)
            ln.audio = str(out)
        fps = int(self.cfg["animation"]["fps"])
        total = build_dialogue_track(script.scenes, int(self.cfg["tts"]["sample_rate"]), fps, d / "dialogue.wav")
        lips = LipSync(self.cfg.get("lipsync", {}) or {})
        for ln in script.lines:
            ln.visemes = lips.visemes(Path(ln.audio), ln.text, ln.envelope, fps)
        save_json(script, d / "script_timed.json")
        self._done(eid, f"voice_{lang}", **{f"duration_{lang}": round(total, 2),
                                            f"tts_signature_{lang}": self.tts_signature()})

    def _subs(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"subs_{lang}"):
            return
        d = self.epdir(eid) / lang
        script = Script.from_dict(load_json(d / "script_timed.json"))
        st = self.cfg["editing"]["subtitle_style"]
        colors = {k: v["color"] for k, v in self.cfg.cast["characters"].items()}
        cues = make_cues(script.lines, max_chars=st["max_chars_per_line"])
        write_srt(cues, d / f"subs.{lang}.srt")
        write_ass(cues, d / f"subs.{lang}.ass", st, tuple(self.cfg["animation"]["resolution"]),
                  speaker_colors=colors)
        sr = Showrunner(self.cfg, self.llm)
        for target in self.cfg["languages"][lang].get("subtitle_translation_to", []):
            tr = sr.translate_lines(script, target)
            write_srt(make_cues(script.lines, tr, st["max_chars_per_line"]), d / f"subs.{target}.srt")
        self._done(eid, f"subs_{lang}")

    def _render(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"render_{lang}"):
            return
        d = self.epdir(eid) / lang
        script = Script.from_dict(load_json(d / "script_timed.json"))
        anim = self.cfg["animation"]
        if (self.cfg.style or {}).get("backgrounds", "sets") in ("plates", "auto"):
            ensure_plates(self.cfg, sorted({s.set for s in script.scenes}))
        jobs = []
        for scene in script.scenes:
            sl_path, mp4 = scene_paths(self.epdir(eid), lang, scene.id)
            save_json(build_shotlist(self.cfg, script, scene), sl_path)
            if not mp4.exists():
                jobs.append((sl_path, mp4))
        if anim["renderer"] == "blender":
            blender_render.render_many(jobs, anim["blender"], int(anim["fps"]))
        else:
            for sl_path, mp4 in jobs:
                preview_render.render_scene(load_json(sl_path), mp4)
        self._done(eid, f"render_{lang}")

    def _edit(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"edit_{lang}"):
            return
        d = self.epdir(eid) / lang
        script = Script.from_dict(load_json(d / "script_timed.json"))
        ec = self.cfg["editing"]
        videos = [scene_paths(self.epdir(eid), lang, s.id)[1] for s in script.scenes]
        music = self.cfg.path(ec["music_bed"]) if ec.get("music_bed") else None
        final = d / f"{eid}_{lang}.mp4"
        clean = d / f"{eid}_{lang}_clean.mp4"
        assemble_episode(videos, d / "dialogue.wav", final,
                         ass=(d / f"subs.{lang}.ass") if ec["burn_subtitles"] else None,
                         music=music, clean_out=clean)
        deliverables = [i for i in self.store.get(eid)["info"].get("deliverables", []) if i["lang"] != lang]
        srts = sorted(str(p) for p in d.glob("subs.*.srt") if p.name != f"subs.{lang}.srt")
        deliverables.append({"lang": lang, "kind": "episode", "title": script.title, "video": str(final),
                             "srt": str(d / f"subs.{lang}.srt"), "srt_translated": srts})
        cut = ec.get("cutdown", {})
        if cut.get("enabled"):
            hl = next((s for s in script.scenes if s.highlight), script.scenes[0])
            start, end = hl.start, min(hl.end, hl.start + float(cut["max_seconds"]))
            # coupe propre : on s'arrête à la fin de la dernière réplique entièrement incluse
            inside = [ln.end for ln in hl.lines if ln.end <= end]
            end = (inside[-1] + 0.4) if inside else end
            st = self.cfg["editing"]["subtitle_style"]
            colors = {k: v["color"] for k, v in self.cfg.cast["characters"].items()}
            vass = write_ass(make_cues(hl.lines, max_chars=24), d / f"short.{lang}.ass", st,
                             tuple(cut["resolution"]), offset=start, speaker_colors=colors)
            short = make_cutdown(clean, start, end, d / f"{eid}_{lang}_short.mp4",
                                 size=tuple(cut["resolution"]), ass=vass)
            deliverables.append({"lang": lang, "kind": "short", "title": script.title, "video": str(short),
                                 "srt": None, "srt_translated": []})
        self._done(eid, f"edit_{lang}", deliverables=deliverables)

    def _package(self, eid: str, lang: str) -> None:
        if self._stage(eid, f"package_{lang}") or not self.cfg.get("package.enabled", True):
            return
        d = self.epdir(eid) / lang
        script = Script.from_dict(load_json(d / "script_timed.json"))
        short = d / f"{eid}_{lang}_short.mp4"
        pkg = build_package(self.cfg, self.llm, eid, lang, script, d / f"{eid}_{lang}_clean.mp4",
                            short if short.exists() else None, self.epdir(eid) / "package" / lang)
        deliverables = self.store.get(eid)["info"].get("deliverables", [])
        for it in deliverables:
            if it["lang"] == lang:
                it["package"] = pkg["dir"]
                it["title"] = pkg["youtube"]["title"] if it["kind"] == "episode" else it["title"]
        self._done(eid, f"package_{lang}", deliverables=deliverables)

    # ------------------------------------------------------------------ réalisateur automatique
    def _bibles(self) -> dict[str, str]:
        sr = Showrunner(self.cfg, self.llm, load_lessons(self.cfg))
        return {lang: sr._bible(lang) for lang in self.cfg.languages}

    def _direct(self, eid: str) -> None:
        if not self.cfg.get("director.enabled", True) or self._stage(eid, "director"):
            return
        rounds = int(self.cfg.get("director.max_rounds", 2))
        history, applied, unresolved = [], set(), []

        def key(f):
            return (f["action"], f["lang"], f.get("line"), f.get("scene"))

        for r in range(rounds + 1):
            rep = director.review(self.cfg, self.llm, self.epdir(eid), self._bibles())
            history.append({"round": r, "scores": rep["scores"], "fixes": rep["fixes"]})
            # pas d'acharnement : une correction déjà tentée qui revient est laissée à l'humain
            fresh = [f for f in rep["fixes"] if key(f) not in applied]
            unresolved = [f for f in rep["fixes"] if key(f) in applied]
            rep["unresolved"] = unresolved
            save_json(rep, self.epdir(eid) / "director_report.json")
            if not fresh or r == rounds:
                break
            log.info("[%s] réalisateur : %d correction(s) ciblée(s)", eid, len(fresh))
            self.apply_fixes(eid, fresh, by="réalisateur")
            applied |= {key(f) for f in fresh}
            for lang in self.cfg.languages:  # on ne refait que ce qui a été invalidé
                self._voice(eid, lang)
                self._subs(eid, lang)
                self._render(eid, lang)
                self._edit(eid, lang)
                self._package(eid, lang)
        self._done(eid, "director", director={"scores": rep["scores"], "verdict": rep["verdict"],
                                              "unresolved": unresolved,
                                              "notes": {l: rep["languages"][l]["notes"] for l in rep["languages"]},
                                              "rounds": history})

    DOWNSTREAM = {"voice": ["voice", "subs", "render", "edit", "package"], "render": ["render", "edit", "package"],
                  "package": ["package"]}

    def _invalidate(self, eid: str, lang: str, frm: str) -> None:
        ep = self.store.get(eid)
        drop = {f"{s}_{lang}" for s in self.DOWNSTREAM[frm]} | {"director", "qa"}
        self.store.reset_stages(eid, [s for s in ep["stages"] if s not in drop])

    def _scene_of_line(self, script: Script, idx: int) -> str:
        n = 0
        for sc in script.scenes:
            if idx < n + len(sc.lines):
                return sc.id
            n += len(sc.lines)
        return script.scenes[-1].id

    def apply_fixes(self, eid: str, fixes: list[dict], by: str = "humain") -> list[dict]:
        """Corrections CIBLÉES : seules les ressources concernées sont supprimées puis recalculées."""
        done = []
        for f in fixes:
            lang = f["lang"]
            d = self.epdir(eid) / lang
            act = f["action"]
            if act in ("regen_voice", "rewrite_line"):
                script = Script.from_dict(load_json(d / "script.json"))
                i = int(f["line"])
                if act == "rewrite_line":
                    old = script.lines[i].text
                    script.lines[i].text = director.rewrite_line(self.llm, script, i, f.get("instruction", ""),
                                                                 self._bibles().get(lang, ""))
                    save_json(script, d / "script.json")
                    f = {**f, "before": old, "after": script.lines[i].text}
                for wav in (d / "audio").glob(f"{i:04d}_*.wav"):
                    wav.unlink()
                (d / "render" / f"{self._scene_of_line(script, i)}.mp4").unlink(missing_ok=True)
                self._invalidate(eid, lang, "voice")
            elif act == "rerender_scene":
                (d / "render" / f"{f['scene']}.mp4").unlink(missing_ok=True)
                self._invalidate(eid, lang, "render")
            elif act == "regen_package":
                shutil.rmtree(self.epdir(eid) / "package" / lang, ignore_errors=True)
                self._invalidate(eid, lang, "package")
            else:
                raise ValueError(f"correction inconnue : {act}")
            done.append(f)
            self.store.log(eid, "fix", {**f, "by": by})
        return done

    # ------------------------------------------------------------------ promotion de niveau
    def promote(self, eid: str, tier: str) -> str:
        """Brouillon → validation → final : garde le texte, recalcule le reste (voix gardées si
        le moteur vocal est identique)."""
        old = self.tier_of(eid)
        tiers = list((self.base.get("production.tiers") or {}).keys())
        if tier not in tiers:
            raise KeyError(tier)
        ep = self.store.get(eid)
        self.cfg = self.base.tier(tier)
        keep = ["premise"] + [f"script_{l}" for l in self.cfg.languages]
        for lang in self.cfg.languages:
            d = self.epdir(eid) / lang
            same_voice = ep["info"].get(f"tts_signature_{lang}") == self.tts_signature()
            if same_voice:
                keep += [f"voice_{lang}", f"subs_{lang}"]
            else:
                shutil.rmtree(d / "audio", ignore_errors=True)
            shutil.rmtree(d / "render", ignore_errors=True)
            for mp4 in d.glob("*.mp4"):
                mp4.unlink()
            shutil.rmtree(self.epdir(eid) / "package" / lang, ignore_errors=True)
        self.store.reset_stages(eid, [s for s in ep["stages"] if s in keep])
        self.store.update(eid, status="queued", info={"tier": tier, "deliverables": [],
                                                      "tier_history": ep["info"].get("tier_history", []) + [old]})
        self.store.log(eid, "promote", {"from": old, "to": tier})
        return tier

    def _qa_and_review(self, eid: str) -> str:
        scripts = {l: Script.from_dict(load_json(self.epdir(eid) / l / "script_timed.json"))
                   for l in self.cfg.languages}
        finals = {l: self.epdir(eid) / l / f"{eid}_{l}.mp4" for l in self.cfg.languages}
        report = qa.evaluate(self.cfg, self.llm, scripts, finals)
        qa.save(report, self.epdir(eid) / "qa_report.json")
        # Seul le niveau final peut être publié ; les niveaux inférieurs attendent une décision
        # (corriger / passer au niveau suivant).
        is_final = self.tier_of(eid) == list((self.base.get("production.tiers") or {"final": 0}).keys())[-1]
        status = "review" if (report["needs_human"] or not is_final) else "approved"
        self.store.update(eid, status=status, risk=report["risk"], stage_done="qa",
                          info={"qa": {"risk": report["risk"], "checks": report["checks"],
                                       "content": report["content"]}})
        if status == "review":
            self._notify(eid)
        return status

    def _notify(self, eid: str) -> None:
        if self.cfg["review"]["channel"] == "telegram":
            from .review import Telegram
            try:
                Telegram(self.cfg).notify(self.store.get(eid))
            except Exception as e:  # noqa: BLE001 - la notification ne doit pas casser la prod
                log.warning("notification Telegram impossible : %s", e)

    # ------------------------------------------------------------------ publication
    def publish(self, eid: str) -> dict:
        ep = self.store.get(eid)
        if ep["status"] != "approved":
            raise RuntimeError(f"{eid} n'est pas validé (statut {ep['status']})")
        premise = load_json(self.epdir(eid) / "premise.json")
        items = []
        for it in ep["info"].get("deliverables", []):
            pkg = Path(it.get("package") or self.epdir(eid) / "package" / it["lang"])
            meta = load_json(pkg / ("youtube.json" if it["kind"] == "episode" else "tiktok.json")) \
                if pkg.exists() else {}
            items.append({**it, "description": meta.get("description") or meta.get("caption") or premise["logline"],
                          "thumbnail": str(pkg / "thumbnail.jpg") if it["kind"] == "episode" else meta.get("cover")})
        res = publish(self.cfg, self.epdir(eid), {"episode_id": eid, "items": items})
        # MÉMOIRE : l'épisode validé enrichit la continuité de la série (une seule fois)
        if not ep["info"].get("continuity_recorded"):
            lang0 = self.cfg.languages[0]
            script = Script.from_dict(load_json(self.epdir(eid) / lang0 / "script_timed.json"))
            facts = record_episode(self.cfg, self.llm, eid, premise["logline"], script)
            self.store.update(eid, info={"continuity_recorded": True, "continuity_facts": facts})
        self.store.update(eid, status="approved" if res["dry_run"] else "published", info={"publish": res})
        self.store.log(eid, "publish", res)
        return res
