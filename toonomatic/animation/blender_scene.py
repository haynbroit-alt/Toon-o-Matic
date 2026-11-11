"""Script maître Blender (bpy), exécuté en mode headless :

    blender -b --factory-startup --python blender_scene.py -- --shotlist s01.json --out frames_s01/

Il lit la shot list d'UNE scène et :
  1. importe le décor pré-fabriqué (sets/<x>.blend, collection "SET") — ou génère un décor placeholder ;
  2. importe les acteurs (characters/<x>.blend, collection = id du personnage) — ou des avatars placeholder ;
  3. applique les animations pré-enregistrées (actions NLA nommées comme les clés `actions` du cast),
     les expressions (shape keys "emo_<émotion>") et le lip-sync (shape key "mouth_open") ;
  4. pose les caméras (large / deux-plans / gros plans) et les coupes via des marqueurs liés aux caméras ;
  5. rend en Eevee une séquence PNG (encodée en H.264 par le lanceur).

Conventions des assets (à respecter par les modeleurs) :
  - set .blend : une collection "SET" ; optionnel : des Empty "SLOT_0..n" pour placer les acteurs.
  - personnage .blend : une collection nommée comme l'id (ex. "momo"), un Armature, un mesh avec
    shape keys "mouth_open" et "emo_*", et des Actions nommées "idle", "talk", "angry", ...
"""
import argparse
import json
import math
import os
import sys

import bpy  # type: ignore

STRONG_POSE = {"angry": (0.0, 0.0, 0.25), "shocked": (0.0, 0.0, 0.6), "laugh": (0.0, 0.2, 0.0)}


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--shotlist", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--engine", default="BLENDER_EEVEE_NEXT")
    p.add_argument("--samples", type=int, default=16)
    p.add_argument("--fps", type=int, default=0, help="surcharge (animation en 2 = fps/2)")
    p.add_argument("--scale", type=int, default=100, help="% de la résolution (aperçus rapides)")
    return p.parse_args(argv)


def hex_rgba(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (1.0,)


def material(name, color):
    m = bpy.data.materials.new(name)
    if bpy.app.version < (5, 0, 0):
        m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = color
        bsdf.inputs["Roughness"].default_value = 0.6
    m.diffuse_color = color
    return m


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


# ----------------------------------------------------------------------------- décor
def build_set(info):
    coll = append_collection(info["blend"], "SET")
    if coll:
        return coll, True
    coll = bpy.data.collections.new("SET")
    bpy.context.scene.collection.children.link(coll)
    col = hex_rgba(info["color"])
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.data.materials.append(material("floor", tuple(c * 0.7 for c in col[:3]) + (1,)))
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 6, 10), rotation=(math.radians(90), 0, 0))
    wall = bpy.context.active_object
    wall.data.materials.append(material("wall", col))
    for ob in (floor, wall):
        for c in ob.users_collection:
            c.objects.unlink(ob)
        coll.objects.link(ob)
    return coll, False


def slot_location(i, n):
    empty = bpy.data.objects.get(f"SLOT_{i}")
    if empty:
        return tuple(empty.location)
    spread = 2.4
    return ((i - (n - 1) / 2) * spread, 0.0, 0.0)


# ----------------------------------------------------------------------------- acteurs
class Actor:
    def __init__(self, info, n):
        self.id = info["id"]
        self.loc = slot_location(info["slot"], n)
        self.coll = append_collection(info["rig"], self.id)
        self.rigged = self.coll is not None
        self.armature = self.mesh = self.root = self.mouth = None
        if self.rigged:
            for ob in self.coll.all_objects:
                if ob.type == "ARMATURE" and not self.armature:
                    self.armature = ob
                if ob.type == "MESH" and ob.data.shape_keys and not self.mesh:
                    self.mesh = ob
            self.root = self.armature or next(iter(self.coll.all_objects), None)
            if self.root:
                self.root.location = self.loc
        else:
            self._placeholder(info)

    def _placeholder(self, info):
        col = hex_rgba(info["color"])
        x, y, z = self.loc
        bpy.ops.object.empty_add(location=(x, y, 0))
        self.root = bpy.context.active_object
        self.root.name = f"{self.id}_root"
        parts = []
        bpy.ops.mesh.primitive_cylinder_add(radius=0.45, depth=1.6, location=(x, y, 0.8))
        parts.append(bpy.context.active_object)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(x, y, 2.0))
        parts.append(bpy.context.active_object)
        for p in parts:
            p.data.materials.append(material(f"{self.id}_skin", col))
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.15, location=(x, y - 0.45, 1.85))
        self.mouth = bpy.context.active_object
        self.mouth.data.materials.append(material(f"{self.id}_mouth", (0.05, 0.02, 0.02, 1)))
        self.mouth.scale = (1.2, 0.4, 0.15)
        parts.append(self.mouth)
        for eye_x in (-0.17, 0.17):
            bpy.ops.mesh.primitive_uv_sphere_add(radius=0.08, location=(x + eye_x, y - 0.45, 2.15))
            e = bpy.context.active_object
            e.data.materials.append(material("eye", (1, 1, 1, 1)))
            parts.append(e)
        for p in parts:
            p.parent = self.root
            p.matrix_parent_inverse = self.root.matrix_world.inverted()

    # -- animation
    def perform(self, perf, fps):
        f0, f1 = int(perf["start"] * fps) + 1, max(int(perf["end"] * fps) + 1, int(perf["start"] * fps) + 2)
        if self.rigged:
            self._nla(perf["action"], f0, f1)
            self._shape(f"emo_{perf['emotion']}", f0, f1, 1.0)
        else:
            self._procedural(perf["action"], f0, f1)
        self._lipsync(perf.get("envelope") or [], f0)

    def _nla(self, action_name, f0, f1):
        act = bpy.data.actions.get(action_name) or bpy.data.actions.get("talk")
        if not (act and self.armature):
            return
        ad = self.armature.animation_data or self.armature.animation_data_create()
        track = ad.nla_tracks.get("perf") or ad.nla_tracks.new()
        track.name = "perf"
        try:
            strip = track.strips.new(f"{action_name}_{f0}", f0, act)
        except RuntimeError:  # chevauchement : nouvelle piste
            track = ad.nla_tracks.new()
            strip = track.strips.new(f"{action_name}_{f0}", f0, act)
        length = max(1.0, act.frame_range[1] - act.frame_range[0])
        strip.repeat = max(1.0, (f1 - f0) / length)
        strip.blend_in = strip.blend_out = min(4, (f1 - f0) / 4)

    def _shape(self, key, f0, f1, value):
        if not (self.mesh and self.mesh.data.shape_keys):
            return
        kb = self.mesh.data.shape_keys.key_blocks.get(key)
        if not kb:
            return
        for f, v in ((f0 - 3, 0.0), (f0 + 2, value), (f1 - 2, value), (f1 + 3, 0.0)):
            kb.value = v
            kb.keyframe_insert("value", frame=max(1, f))

    def _lipsync(self, env, f0):
        if self.rigged:
            kb = self.mesh.data.shape_keys.key_blocks.get("mouth_open") if self.mesh else None
            if not kb:
                return
            for i in range(0, len(env), 2):
                kb.value = env[i]
                kb.keyframe_insert("value", frame=f0 + i)
        elif self.mouth:
            for i in range(0, len(env), 2):
                self.mouth.scale = (1.2, 0.4, 0.15 + 0.9 * env[i])
                self.mouth.keyframe_insert("scale", frame=f0 + i)

    def _procedural(self, action, f0, f1):
        r = self.root
        base = self.loc
        r.location = base
        r.rotation_euler = (0, 0, 0)
        r.keyframe_insert("location", frame=max(1, f0 - 2))
        r.keyframe_insert("rotation_euler", frame=max(1, f0 - 2))
        mid = (f0 + f1) // 2
        if action in STRONG_POSE:
            dx, dy, dz = STRONG_POSE[action]
            r.location = (base[0] + dx, base[1] + dy, base[2] + dz)
            r.keyframe_insert("location", frame=f0 + 3)
        elif action in ("talk_hands", "point", "shrug"):
            r.rotation_euler = (0, math.radians(6), math.radians(15))
            r.keyframe_insert("rotation_euler", frame=mid)
        elif action == "facepalm":
            r.rotation_euler = (math.radians(15), 0, 0)
            r.keyframe_insert("rotation_euler", frame=mid)
        r.location = base
        r.rotation_euler = (0, 0, 0)
        r.keyframe_insert("location", frame=f1)
        r.keyframe_insert("rotation_euler", frame=f1)


# ----------------------------------------------------------------------------- caméras
def add_camera(name, loc, target, lens=35):
    bpy.ops.object.camera_add(location=loc)
    cam = bpy.context.active_object
    cam.name = name
    cam.data.lens = lens
    c = cam.constraints.new("TRACK_TO")
    bpy.ops.object.empty_add(location=target)
    tgt = bpy.context.active_object
    tgt.name = name + "_target"
    c.target = tgt
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    return cam


def setup_cameras(actors):
    cams = {"wide": add_camera("CAM_wide", (0, -11, 3.2), (0, 0, 1.4), 30),
            "two_shot": add_camera("CAM_two", (1.5, -8, 2.4), (0, 0, 1.6), 40)}
    for a in actors:
        x, y, _ = a.loc
        cams[f"close:{a.id}"] = add_camera(f"CAM_close_{a.id}", (x * 0.6 + 0.6, y - 4.5, 2.1),
                                           (x, y, 1.9), 55)
    return cams


def cut_cameras(shots, cams, fps):
    scene = bpy.context.scene
    for s in shots:
        cam = cams.get(s["camera"]) or cams["wide"]
        m = scene.timeline_markers.new(s["camera"], frame=int(s["start"] * fps) + 1)
        m.camera = cam
    scene.camera = cams["wide"]


def lights():
    bpy.ops.object.light_add(type="SUN", location=(4, -6, 10))
    bpy.context.active_object.data.energy = 3.0
    bpy.ops.object.light_add(type="AREA", location=(-4, -5, 5))
    bpy.context.active_object.data.energy = 600
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.color = (0.35, 0.4, 0.5)


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
    if sc.render.engine == "CYCLES":  # repli sans GPU (ou ferme de rendu CPU)
        sc.cycles.samples = a.samples
        sc.cycles.use_denoising = False
    elif hasattr(sc, "eevee") and hasattr(sc.eevee, "taa_render_samples"):
        sc.eevee.taa_render_samples = a.samples
    sc.render.resolution_x, sc.render.resolution_y = sl["resolution"]
    sc.render.resolution_percentage = a.scale
    sc.render.film_transparent = False
    sc.render.use_motion_blur = False
    # Séquence PNG (compatible Blender 4.x/5.x, reprenable après crash) → encodée par FFmpeg ensuite.
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.use_overwrite = False      # reprise : les images déjà rendues sont conservées
    sc.render.use_placeholder = True
    os.makedirs(out, exist_ok=True)
    sc.render.filepath = os.path.join(out, "frame_####")
    return fps


def main():
    a = args()
    with open(a.shotlist, encoding="utf-8") as fh:
        sl = json.load(fh)
    clear_scene()
    build_set(sl["set"])
    actors = [Actor(c, len(sl["characters"])) for c in sl["characters"]]
    by_id = {x.id: x for x in actors}
    fps = configure_render(sl, a.out, a)
    for perf in sl["performances"]:
        if perf["character"] in by_id:
            by_id[perf["character"]].perform(perf, fps)
    cut_cameras(sl["shots"], setup_cameras(actors), fps)
    lights()
    bpy.ops.render.render(animation=True)
    print(f"TOONOMATIC_RENDER_OK {a.out}")


if __name__ == "__main__":
    main()
