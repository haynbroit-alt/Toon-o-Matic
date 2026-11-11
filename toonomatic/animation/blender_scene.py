"""Script maître Blender (bpy), exécuté en mode headless :

    blender -b --factory-startup --python blender_scene.py -- --shotlist s01.json --out frames_s01/

Il lit la shot list d'UNE scène et :
  1. applique le STYLE de la série (styles/*.yaml : cel-shading + contour, rendu doux, pâte à modeler) ;
  2. importe le décor (sets/<x>.blend, collection "SET") — ou génère un décor placeholder ;
  3. importe les acteurs (characters/<x>.blend, collection = id du personnage) — ou des avatars placeholder ;
  4. joue la performance : actions NLA (avec variantes), expressions `emo_*`, lip-sync par VISÈMES
     (`viseme_A`…`viseme_X`, repli `mouth_open`), et le « jeu naturel » : clignements, respiration,
     regard qui suit le locuteur ;
  5. pose les caméras (large / deux-plans / gros plans, profondeur de champ) et les coupes ;
  6. rend en Eevee une séquence PNG (encodée en H.264 par le lanceur).

Conventions des assets (à respecter par les modeleurs) :
  - set .blend : une collection "SET" ; optionnel : des Empty "SLOT_0..n" pour placer les acteurs.
  - personnage .blend : une collection nommée comme l'id (ex. "momo"), un Armature avec un os "head",
    un mesh avec shape keys "viseme_A..H/X" (ou "mouth_open"), "emo_*", "blink", "breathe",
    et des Actions "idle", "talk", "angry"… (variantes : "talk.001", "talk.002").
"""
import argparse
import json
import math
import os
import random
import re
import sys

import bpy  # type: ignore

STRONG_POSE = {"angry": (0.0, 0.0, 0.25), "shocked": (0.0, 0.0, 0.6), "laugh": (0.0, 0.2, 0.0)}
# forme de la bouche placeholder (largeur, ouverture) par visème — notation Preston Blair
MOUTH = {"A": (0.9, 0.04), "B": (1.1, 0.22), "C": (1.25, 0.45), "D": (1.3, 0.8), "E": (0.85, 0.6),
         "F": (0.6, 0.45), "G": (1.1, 0.18), "H": (1.15, 0.5), "X": (1.1, 0.1)}


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--shotlist", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--engine", default="BLENDER_EEVEE_NEXT")
    p.add_argument("--samples", type=int, default=0, help="0 = valeur du style")
    p.add_argument("--fps", type=int, default=0, help="surcharge (animation en 2 = fps/2)")
    p.add_argument("--scale", type=int, default=100, help="% de la résolution (aperçus rapides)")
    return p.parse_args(argv)


def hex_rgba(h, mul=1.0):
    h = h.lstrip("#")
    # couleurs du style en sRGB → espace linéaire de Blender
    lin = [((int(h[i:i + 2], 16) / 255) ** 2.2) * mul for i in (0, 2, 4)]
    return tuple(lin) + (1.0,)


def mix(c1, c2, t):
    return tuple(a * (1 - t) + b * t for a, b in zip(c1[:3], c2[:3])) + (1.0,)


# ----------------------------------------------------------------------------- style
class Style:
    def __init__(self, style, engine):
        self.s = style or {}
        self.kind = self.s.get("shading", "soft")
        self.toon_ok = self.kind == "toon" and engine != "CYCLES"  # Shader→RGB = Eevee uniquement
        self.acting = self.s.get("acting", {})

    def material(self, name, color):
        m = bpy.data.materials.new(name)
        if bpy.app.version < (5, 0, 0):
            m.use_nodes = True
        m.diffuse_color = color
        nt = m.node_tree
        out = next(n for n in nt.nodes if n.type == "OUTPUT_MATERIAL")
        bsdf = nt.nodes.get("Principled BSDF")
        if self.toon_ok:
            t = self.s.get("toon", {})
            steps = max(2, int(t.get("steps", 3)))
            shadow = mix(color, hex_rgba(t.get("shadow_tint", "#4b3f72")), 0.55)
            diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
            to_rgb = nt.nodes.new("ShaderNodeShaderToRGB")
            ramp = nt.nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.interpolation = "CONSTANT"
            els = ramp.color_ramp.elements
            els[0].position, els[0].color = 0.0, shadow
            els[1].position, els[1].color = 1.0 / steps, mix(shadow, color, 1 / (steps - 1))
            for i in range(2, steps):
                e = els.new(i / steps)
                e.color = mix(shadow, color, i / (steps - 1))
            emit = nt.nodes.new("ShaderNodeEmission")
            nt.links.new(diff.outputs[0], to_rgb.inputs[0])
            nt.links.new(to_rgb.outputs["Color"], ramp.inputs["Fac"])
            rim_amt = float(t.get("rim", 0))
            if rim_amt > 0:
                lw = nt.nodes.new("ShaderNodeLayerWeight")
                lw.inputs["Blend"].default_value = 0.2
                add = nt.nodes.new("ShaderNodeMix")
                add.data_type, add.blend_type = "RGBA", "ADD"
                nt.links.new(lw.outputs["Facing"], add.inputs["Factor"])
                add.inputs["Factor"].default_value = rim_amt
                nt.links.new(ramp.outputs["Color"], add.inputs[6])
                add.inputs[7].default_value = (rim_amt,) * 3 + (1,)
                nt.links.new(add.outputs[2], emit.inputs["Color"])
            else:
                nt.links.new(ramp.outputs["Color"], emit.inputs["Color"])
            nt.links.new(emit.outputs[0], out.inputs["Surface"])
        elif bsdf:
            bsdf.inputs["Base Color"].default_value = color
            bsdf.inputs["Roughness"].default_value = {"clay": 0.95, "toon": 0.7}.get(self.kind, 0.45)
            if self.kind == "soft" and "Coat Weight" in bsdf.inputs:
                bsdf.inputs["Coat Weight"].default_value = 0.15
        return m

    def apply_scene(self, sc):
        col = self.s.get("color", {})
        for attr, val in (("view_transform", col.get("view_transform")), ("look", col.get("look"))):
            if val:
                try:
                    setattr(sc.view_settings, attr, val)
                except TypeError:
                    pass
        sc.view_settings.exposure = float(col.get("exposure", 0.0))
        sc.render.use_motion_blur = bool(self.s.get("render", {}).get("motion_blur", False))
        ol = self.s.get("outline", {})
        if ol.get("enabled") and hasattr(sc.render, "use_freestyle"):
            try:
                sc.render.use_freestyle = True
                sc.render.line_thickness_mode = "ABSOLUTE"
                sc.render.line_thickness = float(ol.get("thickness", 2))
                fs = bpy.context.view_layer.freestyle_settings
                ls = fs.linesets[0] if len(fs.linesets) else fs.linesets.new("outline")
                ls.linestyle.color = hex_rgba(ol.get("color", "#000000"))[:3]
                ls.linestyle.thickness = float(ol.get("thickness", 2))
            except (AttributeError, TypeError, RuntimeError) as e:
                print(f"contour indisponible : {e}")
        world = sc.world or bpy.data.worlds.new("World")
        sc.world = world
        bg = hex_rgba(self.s.get("palette", {}).get("background", "#5a6680"))
        world.color = bg[:3]
        if world.node_tree:
            node = world.node_tree.nodes.get("Background")
            if node:
                node.inputs["Color"].default_value = bg
                node.inputs["Strength"].default_value = 0.6


STYLE = None


# ----------------------------------------------------------------------------- scène
def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)


def append_collection(blend, name):
    if not os.path.exists(blend):
        return None
    with bpy.data.libraries.load(blend, link=False) as (src, dst):
        if name in src.collections:
            dst.collections = [name]
        if hasattr(src, "actions"):
            dst.actions = list(src.actions)
    coll = bpy.data.collections.get(name)
    if coll:
        bpy.context.scene.collection.children.link(coll)
    return coll


def _move_to(ob, coll):
    for c in ob.users_collection:
        c.objects.unlink(ob)
    coll.objects.link(ob)


def build_set(info):
    coll = append_collection(info["blend"], "SET")
    if coll:
        return coll, True
    coll = bpy.data.collections.new("SET")
    bpy.context.scene.collection.children.link(coll)
    col = hex_rgba(info["color"])
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.data.materials.append(STYLE.material("floor", mix(col, (0, 0, 0, 1), 0.3)))
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 6, 10), rotation=(math.radians(90), 0, 0))
    wall = bpy.context.active_object
    wall.data.materials.append(STYLE.material("wall", col))
    for ob in (floor, wall):
        _move_to(ob, coll)
    return coll, False


def slot_location(i, n):
    empty = bpy.data.objects.get(f"SLOT_{i}")
    if empty:
        return tuple(empty.location)
    return ((i - (n - 1) / 2) * 2.4, 0.0, 0.0)


# ----------------------------------------------------------------------------- acteurs
class Actor:
    HEAD_Z = 2.0

    def __init__(self, info, n, scene_seed):
        self.id = info["id"]
        self.loc = slot_location(info["slot"], n)
        self.rng = random.Random(f"{scene_seed}:{self.id}")
        self.coll = append_collection(info["rig"], self.id)
        self.rigged = self.coll is not None
        self.armature = self.mesh = self.root = self.mouth = self.head = None
        self.eyes = []
        self.gaze = None
        if self.rigged:
            for ob in self.coll.all_objects:
                if ob.type == "ARMATURE" and not self.armature:
                    self.armature = ob
                if ob.type == "MESH" and ob.data.shape_keys and not self.mesh:
                    self.mesh = ob
            self.root = self.armature or next(iter(self.coll.all_objects), None)
            if self.root:
                self.root.location = self.loc
            self._rig_gaze()
        else:
            self._placeholder(info)

    def head_pos(self):
        return (self.loc[0], self.loc[1], self.HEAD_Z)

    def _placeholder(self, info):
        col = hex_rgba(info["color"])
        x, y, _ = self.loc
        bpy.ops.object.empty_add(location=(x, y, 0))
        self.root = bpy.context.active_object
        self.root.name = f"{self.id}_root"
        bpy.ops.mesh.primitive_cylinder_add(radius=0.45, depth=1.6, location=(x, y, 0.8))
        body = bpy.context.active_object
        body.data.materials.append(STYLE.material(f"{self.id}_skin", col))
        self._parent(body, self.root)
        # pivot de tête : porte le regard (rotation Z) indépendamment du corps
        bpy.ops.object.empty_add(location=(x, y, 1.7))
        self.head = bpy.context.active_object
        self.head.name = f"{self.id}_head"
        self._parent(self.head, self.root)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(x, y, self.HEAD_Z))
        skull = bpy.context.active_object
        skull.data.materials.append(STYLE.material(f"{self.id}_skin2", col))
        self._parent(skull, self.head)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.15, location=(x, y - 0.45, 1.85))
        self.mouth = bpy.context.active_object
        self.mouth.data.materials.append(STYLE.material(f"{self.id}_mouth", (0.03, 0.01, 0.01, 1)))
        self.mouth.scale = (1.1, 0.4, 0.1)
        self._parent(self.mouth, self.head)
        for ex in (-0.17, 0.17):
            bpy.ops.mesh.primitive_uv_sphere_add(radius=0.08, location=(x + ex, y - 0.45, 2.15))
            e = bpy.context.active_object
            e.data.materials.append(STYLE.material("eye", (1, 1, 1, 1)))
            self._parent(e, self.head)
            self.eyes.append(e)

    @staticmethod
    def _parent(child, parent):
        child.parent = parent
        child.matrix_parent_inverse = parent.matrix_world.inverted()

    def _rig_gaze(self):
        """Os "head" → contrainte Damped Track vers une cible que l'on déplace sur le locuteur."""
        if not self.armature or not STYLE.acting.get("look_at_speaker", True):
            return
        bone = next((b for b in self.armature.pose.bones if b.name.lower() in ("head", "tete", "tête")), None)
        if not bone:
            return
        bpy.ops.object.empty_add(location=(self.loc[0], self.loc[1] - 5, self.HEAD_Z))
        self.gaze = bpy.context.active_object
        self.gaze.name = f"{self.id}_gaze"
        c = bone.constraints.new("DAMPED_TRACK")
        c.target, c.track_axis, c.influence = self.gaze, "TRACK_Y", 0.6

    # -- jeu
    def perform(self, perf, fps):
        f0 = int(perf["start"] * fps) + 1
        f1 = max(int(perf["end"] * fps) + 1, f0 + 1)
        if self.rigged:
            self._nla(perf["action"], f0, f1)
            self._shape(f"emo_{perf['emotion']}", f0, f1, 1.0)
        else:
            self._procedural(perf["action"], f0, f1)
        if perf.get("visemes"):
            self._visemes(perf["visemes"], f0, fps)
        else:
            self._envelope(perf.get("envelope") or [], f0)

    def _pick_action(self, name):
        names = [a.name for a in bpy.data.actions if re.fullmatch(re.escape(name) + r"(\.\d{3})?", a.name)]
        if not names:
            return bpy.data.actions.get("talk")
        if not STYLE.acting.get("gesture_variants", True):
            return bpy.data.actions.get(name) or bpy.data.actions[names[0]]
        return bpy.data.actions[self.rng.choice(sorted(names))]  # variation = moins robotique

    def _nla(self, action_name, f0, f1):
        act = self._pick_action(action_name)
        if not (act and self.armature):
            return
        ad = self.armature.animation_data or self.armature.animation_data_create()
        ant = int(STYLE.acting.get("anticipation_frames", 3))
        start = max(1, f0 - ant)  # le geste démarre juste AVANT la parole : anticipation
        for track in list(ad.nla_tracks) + [None]:
            if track is None:
                track = ad.nla_tracks.new()
                track.name = f"perf{len(ad.nla_tracks)}"
            try:
                strip = track.strips.new(f"{action_name}_{f0}", start, act)
                break
            except RuntimeError:  # chevauchement → piste suivante
                continue
        length = max(1.0, act.frame_range[1] - act.frame_range[0])
        strip.repeat = max(1.0, (f1 - start) / length)
        strip.blend_in = strip.blend_out = min(6, (f1 - start) / 3)

    def _key_shape(self, key, frame, value):
        if not (self.mesh and self.mesh.data.shape_keys):
            return False
        kb = self.mesh.data.shape_keys.key_blocks.get(key)
        if not kb:
            return False
        kb.value = value
        kb.keyframe_insert("value", frame=max(1, frame))
        return True

    def _shape(self, key, f0, f1, value):
        for f, v in ((f0 - 3, 0.0), (f0 + 2, value), (f1 - 2, value), (f1 + 3, 0.0)):
            self._key_shape(key, f, v)

    def _visemes(self, cues, f0, fps):
        has_keys = self.rigged and self.mesh and self.mesh.data.shape_keys and \
            self.mesh.data.shape_keys.key_blocks.get("viseme_X")
        prev = "X"
        for cue in cues:
            f = f0 + int(round(cue["t"] * fps))
            v = cue["v"] if cue["v"] in MOUTH else "X"
            if has_keys:
                for name in MOUTH:  # transition de 1 image : la bouche "claque" sans flotter
                    self._key_shape(f"viseme_{name}", f - 1, 1.0 if name == prev else 0.0)
                    self._key_shape(f"viseme_{name}", f, 1.0 if name == v else 0.0)
            elif self.rigged:
                self._key_shape("mouth_open", f, MOUTH[v][1])
            elif self.mouth:
                w, o = MOUTH[v]
                self.mouth.scale = (w, 0.4, max(0.04, o * 0.9))
                self.mouth.keyframe_insert("scale", frame=max(1, f))
            prev = v

    def _envelope(self, env, f0):
        for i in range(0, len(env), 2):
            if self.rigged:
                self._key_shape("mouth_open", f0 + i, env[i])
            elif self.mouth:
                self.mouth.scale = (1.1, 0.4, 0.1 + 0.8 * env[i])
                self.mouth.keyframe_insert("scale", frame=f0 + i)

    def _procedural(self, action, f0, f1):
        r, base = self.root, self.loc
        r.location, r.rotation_euler = base, (0, 0, 0)
        r.keyframe_insert("location", frame=max(1, f0 - 2))
        r.keyframe_insert("rotation_euler", frame=max(1, f0 - 2))
        mid = (f0 + f1) // 2
        if action in STRONG_POSE:
            dx, dy, dz = STRONG_POSE[action]
            r.location = (base[0] + dx, base[1] + dy, base[2] + dz)
            r.keyframe_insert("location", frame=f0 + 3)
        elif action in ("talk_hands", "point", "shrug"):
            r.rotation_euler = (0, math.radians(6), math.radians(self.rng.uniform(-15, 15)))
            r.keyframe_insert("rotation_euler", frame=mid)
        elif action == "facepalm":
            r.rotation_euler = (math.radians(15), 0, 0)
            r.keyframe_insert("rotation_euler", frame=mid)
        r.location, r.rotation_euler = base, (0, 0, 0)
        r.keyframe_insert("location", frame=f1)
        r.keyframe_insert("rotation_euler", frame=f1)

    # -- micro-jeu continu
    def blinks(self, frame_end, fps):
        lo, hi = STYLE.acting.get("blink_every_s", [2.5, 5.5])
        t = self.rng.uniform(0.3, hi)
        while t * fps < frame_end:
            f = int(t * fps) + 1
            if self.rigged:
                for ff, v in ((f - 1, 0.0), (f + 1, 1.0), (f + 3, 0.0)):
                    self._key_shape("blink", ff, v)
            else:
                for e in self.eyes:
                    for ff, z in ((f - 1, 1.0), (f + 1, 0.1), (f + 3, 1.0)):
                        e.scale = (1, 1, z)
                        e.keyframe_insert("scale", frame=max(1, ff))
            t += self.rng.uniform(lo, hi)

    def breathing(self, frame_end, fps):
        amp = float(STYLE.acting.get("breathing", 0.015))
        if amp <= 0:
            return
        period = int(self.rng.uniform(3.2, 4.2) * fps / 2)
        phase = self.rng.randint(0, period)
        for i, f in enumerate(range(1 + phase, frame_end + period, period)):
            v = 1.0 + (amp if i % 2 else -amp)
            if self.rigged:
                if not self._key_shape("breathe", f, 1.0 if i % 2 else 0.0):
                    return
            else:
                self.root.scale = (1.0, 1.0, v)
                self.root.keyframe_insert("scale", frame=f)

    def look_at(self, target, frame):
        if self.gaze:  # rig : la cible du regard rejoint la tête du locuteur (en ~4 images)
            self.gaze.keyframe_insert("location", frame=max(1, frame - 4))
            self.gaze.location = target
            self.gaze.keyframe_insert("location", frame=max(1, frame))
        elif self.head:
            dx = target[0] - self.loc[0]
            ang = max(-0.6, min(0.6, math.atan2(dx, 4.0)))
            self.head.keyframe_insert("rotation_euler", frame=max(1, frame - 4))
            self.head.rotation_euler = (0, 0, ang)
            self.head.keyframe_insert("rotation_euler", frame=max(1, frame))


def direct_gazes(actors, performances, fps):
    """Les auditeurs regardent celui qui parle ; le locuteur regarde son interlocuteur principal."""
    if not STYLE.acting.get("look_at_speaker", True) or len(actors) < 2:
        return
    by_id = {a.id: a for a in actors}
    last = None
    for p in performances:
        sp = by_id.get(p["character"])
        if not sp:
            continue
        f = int(p["start"] * fps) + 1
        for a in actors:
            if a is sp:
                tgt = by_id.get(last) if last and last != sp.id else min(
                    (x for x in actors if x is not sp), key=lambda x: abs(x.loc[0] - sp.loc[0]))
                a.look_at(tgt.head_pos(), f)
            else:
                # petit décalage par personnage : les têtes ne tournent pas toutes à la même image
                a.look_at(sp.head_pos(), f + a.rng.randint(1, 5))
        last = sp.id


# ----------------------------------------------------------------------------- caméras
def add_camera(name, loc, target, lens=35):
    bpy.ops.object.camera_add(location=loc)
    cam = bpy.context.active_object
    cam.name, cam.data.lens = name, lens
    bpy.ops.object.empty_add(location=target)
    tgt = bpy.context.active_object
    tgt.name = name + "_target"
    c = cam.constraints.new("TRACK_TO")
    c.target, c.track_axis, c.up_axis = tgt, "TRACK_NEGATIVE_Z", "UP_Y"
    cs = STYLE.s.get("camera", {})
    if cs.get("dof"):
        cam.data.dof.use_dof = True
        cam.data.dof.focus_object = tgt
        cam.data.dof.aperture_fstop = float(cs.get("f_stop", 2.8))
    return cam


def setup_cameras(actors):
    cs = STYLE.s.get("camera", {})
    cams = {"wide": add_camera("CAM_wide", (0, -11, 3.2), (0, 0, 1.4), cs.get("lens_wide", 30)),
            "two_shot": add_camera("CAM_two", (1.5, -8, 2.4), (0, 0, 1.6), cs.get("lens_two", 40))}
    for a in actors:
        x, y, _ = a.loc
        cams[f"close:{a.id}"] = add_camera(f"CAM_close_{a.id}", (x * 0.6 + 0.6, y - 4.5, 2.1),
                                           (x, y, 1.9), cs.get("lens_close", 55))
    return cams


def cut_cameras(shots, cams, fps):
    scene = bpy.context.scene
    for s in shots:
        m = scene.timeline_markers.new(s["camera"], frame=int(s["start"] * fps) + 1)
        m.camera = cams.get(s["camera"]) or cams["wide"]
    scene.camera = cams["wide"]


def lights():
    pal, li = STYLE.s.get("palette", {}), STYLE.s.get("lighting", {})
    bpy.ops.object.light_add(type="SUN", location=(4, -6, 10), rotation=(math.radians(50), 0, math.radians(30)))
    key = bpy.context.active_object.data
    key.energy, key.color = float(li.get("key_energy", 3)), hex_rgba(pal.get("key_light", "#ffffff"))[:3]
    bpy.ops.object.light_add(type="AREA", location=(-4, -5, 5))
    fill = bpy.context.active_object.data
    fill.energy, fill.color = float(li.get("fill_energy", 300)), hex_rgba(pal.get("fill_light", "#ffffff"))[:3]
    fill.size = 6
    bpy.ops.object.light_add(type="AREA", location=(0, 5, 4), rotation=(math.radians(-60), 0, 0))
    rim = bpy.context.active_object.data
    rim.energy = float(li.get("rim_energy", 300))


def configure_render(sl, out, a):
    sc = bpy.context.scene
    fps = a.fps or sl["fps"]
    sc.render.fps = fps
    sc.frame_start = 1
    sc.frame_end = max(2, int(math.ceil(sl["duration"] * fps)))
    for eng in (a.engine, "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            sc.render.engine = eng
            break
        except TypeError:
            continue
    samples = a.samples or int((sl.get("style") or {}).get("render", {}).get("samples", 16))
    if sc.render.engine == "CYCLES":  # repli sans GPU (ou ferme de rendu CPU)
        sc.cycles.samples = samples
        sc.cycles.use_denoising = False
    elif hasattr(sc, "eevee") and hasattr(sc.eevee, "taa_render_samples"):
        sc.eevee.taa_render_samples = samples
    sc.render.resolution_x, sc.render.resolution_y = sl["resolution"]
    sc.render.resolution_percentage = a.scale
    sc.render.film_transparent = False
    # Séquence PNG (compatible Blender 4.x/5.x, reprenable après crash) → encodée par FFmpeg ensuite.
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.use_overwrite = False      # reprise : les images déjà rendues sont conservées
    sc.render.use_placeholder = True
    os.makedirs(out, exist_ok=True)
    sc.render.filepath = os.path.join(out, "frame_####")
    return fps


def main():
    global STYLE
    a = args()
    with open(a.shotlist, encoding="utf-8") as fh:
        sl = json.load(fh)
    clear_scene()
    fps = configure_render(sl, a.out, a)
    sc = bpy.context.scene
    STYLE = Style(sl.get("style"), sc.render.engine)
    STYLE.apply_scene(sc)
    build_set(sl["set"])
    seed = f"{sl.get('episode_lang')}:{sl['scene_id']}"
    actors = [Actor(c, len(sl["characters"]), seed) for c in sl["characters"]]
    by_id = {x.id: x for x in actors}
    for perf in sl["performances"]:
        if perf["character"] in by_id:
            by_id[perf["character"]].perform(perf, fps)
    for actor in actors:
        actor.blinks(sc.frame_end, fps)
        actor.breathing(sc.frame_end, fps)
    direct_gazes(actors, sl["performances"], fps)
    cut_cameras(sl["shots"], setup_cameras(actors), fps)
    lights()
    bpy.ops.render.render(animation=True)
    print(f"TOONOMATIC_RENDER_OK {a.out}")


if __name__ == "__main__":
    main()
