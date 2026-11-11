"""Orchestrateur : enchaîne les étapes d'un épisode, de façon idempotente et reprenable.

    premise → script_<lang> → voice_<lang> → subs_<lang> → render_<lang> → edit_<lang> → qa → review
                                                                     (approved) → publish

Chaque étape écrit ses artefacts dans runs/<episode_id>/ et se marque « faite » dans SQLite :
un crash, un « Refaire » ou un changement de fournisseur ne relance que ce qui est nécessaire.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

from . import ffmpeg, qa
from .animation import blender_render, preview_render
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
from .store import Store
from .voice.lipsync import LipSync
from .voice.tts import build_dialogue_track, make_tts
from .writing.llm import make_llm
from .writing.showrunner import Showrunner

log = logging.getLogger(__name__)
MAX_RETRIES = 3


class Pipeline:
    def __init__(self, cfg: Config):
        ffmpeg.require()
        self.cfg = cfg
        self.store = Store(cfg.path(cfg["database"]))
        self._llm = self._tts = None

    # lazy : on ne charge un modèle GPU que si l'étape en a besoin
    @property
    def llm(self):
        if self._llm is None:
            self._llm = make_llm(self.cfg["llm"])
        return self._llm

    @property
    def tts(self):
        if self._tts is None:
            self._tts = make_tts(self.cfg["tts"])
        return self._tts

    def epdir(self, episode_id: str) -> Path:
        return self.cfg.workdir / episode_id

    # ------------------------------------------------------------------ entrée principale
    def produce(self, count: int, seed: int | None = None) -> list[str]:
        situations = pick_situations(self.cfg, self.store, count, seed, kpi_weights(self.store))
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
            save_json(sit, self.epdir(eid) / "situation.json")
            ids.append(eid)
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
        self.store.update(episode_id, status="running")
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
            status = self._qa_and_review(episode_id)
        except Exception as e:  # noqa: BLE001 - on journalise toute panne d'étape
            log.exception("épisode %s en échec", episode_id)
            self.store.update(episode_id, status="failed",
                              info={"error": f"{type(e).__name__}: {e}"[:2000],
                                    "retries": (ep or {}).get("info", {}).get("retries", 0) + 1})
            self.store.log(episode_id, "failed", {"error": str(e)[:2000]})
            return "failed"
        self.store.log(episode_id, "produced", {"seconds": round(time.time() - t0, 1), "status": status})
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
        self._done(eid, f"voice_{lang}", **{f"duration_{lang}": round(total, 2)})

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
        if self._stage(eid, f"package_{lang}"):
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

    def _qa_and_review(self, eid: str) -> str:
        scripts = {l: Script.from_dict(load_json(self.epdir(eid) / l / "script_timed.json"))
                   for l in self.cfg.languages}
        finals = {l: self.epdir(eid) / l / f"{eid}_{l}.mp4" for l in self.cfg.languages}
        report = qa.evaluate(self.cfg, self.llm, scripts, finals)
        qa.save(report, self.epdir(eid) / "qa_report.json")
        status = "review" if report["needs_human"] else "approved"
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
