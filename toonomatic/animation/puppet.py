"""THÉÂTRE DE MARIONNETTES 2D : un vrai dessin animé sur simple processeur.

Aucun GPU, aucun modèle 3D, aucun service externe : les personnages sont DESSINÉS par le code à
partir de leur fiche (`signature` de cast.yaml : silhouette, couleurs, accessoires), puis animés
image par image d'après la même partition que le moteur 3D (shot list) :

  - bouche synchronisée sur les visèmes (A…H, X), expressions par émotion (sourcils, paupières,
    bouche au repos), clignements, regard vers celui qui parle ;
  - gestes : parler, parler avec les mains, pointer, hausser les épaules, facepalm, colère,
    sursaut, rire, téléphone ; petits mouvements de tête sur la voix ;
  - mise en scène face à face, plans large / deux-plans / gros plan avec le décor recadré ;
  - décors dessinés (rue pavillonnaire, salon, garage, épicerie, voiture).

La silhouette et les couleurs étant tirées de la fiche, l'identité reste la même que celle
contrôlée par `brand-check`. Le jour où de vrais modèles 3D existent, on change de moteur
(`animation.renderer: blender`) sans toucher au reste.
"""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .plates import shot_framing

SS = 2  # suréchantillonnage (anticrénelage)
FEET_V, CHAR_H = 0.885, 0.56  # position des pieds et hauteur d'un personnage « moyen » en plan large


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def shade(c, f: float):
    return tuple(max(0, min(255, int(v * f))) for v in c[:3])


# --------------------------------------------------------------------------- personnage
class Spec:
    """Fiche de dessin d'un personnage, tirée de cast.yaml."""

    def __init__(self, char: dict, style: dict):
        sig = char.get("signature") or {}
        b = sig.get("build") or {}
        ph = (style or {}).get("placeholder", {})
        self.id = char["id"]
        self.skin = rgb(sig.get("skin", "#d9a066"))
        self.outfit = rgb(sig.get("outfit_color", char.get("color", "#888888")))
        self.hair = rgb(sig.get("hair", "#2b1d14"))
        self.acc = frozenset(sig.get("accessories") or [])
        self.shape = b.get("shape", "column")
        self.bh = 1.6 * float(b.get("height", 1.0))
        self.bw = 0.42 * float(b.get("width", 1.0))                      # demi-largeur du corps
        self.neck = max(0.0, float(b.get("neck", ph.get("neck", 0.3))))
        self.R = 0.5 * float(sig.get("head_scale", 1.0)) * float(ph.get("head_scale", 1.3))
        self.height = self.bh + self.neck + 2 * self.R * 1.08

    def key(self):
        return (self.id, self.skin, self.outfit, self.hair, self.acc, self.shape, self.bh, self.bw, self.neck, self.R)


# poses des bras : (coude dx, dy), (main dx, dy) depuis l'épaule, en unités ; x > 0 = devant, y > 0 = bas
REST = ((0.06, 0.5), (0.1, 0.98))
POSES = {
    "talk": (((0.32, 0.42), (0.72, 0.05)), REST),
    "talk_hands": (((0.34, 0.38), (0.78, -0.02)), ((0.3, 0.42), (0.66, 0.08))),
    "point": (((0.6, 0.02), (1.22, -0.12)), REST),
    "shrug": (((0.48, 0.3), (0.86, -0.1)), ((0.44, 0.3), (0.8, -0.1))),
    "angry": (((0.22, 0.46), (0.5, 0.86)), ((0.2, 0.46), (0.46, 0.86))),
    "shocked": (((0.42, -0.22), (0.62, -0.86)), ((0.38, -0.22), (0.56, -0.86))),
    "laugh": (((0.3, 0.5), (0.32, 0.86)), ((0.26, 0.5), (0.3, 0.9))),
}


def _ell(d, cx, cy, rx, ry, fill, outline=None, w=0):
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill, outline=outline, width=w)


def _limb(d, pts, width, fill):
    d.line(pts, fill=fill, width=int(width), joint="curve")
    for x, y in pts:
        _ell(d, x, y, width / 2, width / 2, fill)


_CACHE: dict[tuple, tuple] = {}
_CACHE_BYTES = [0]
CACHE_BUDGET = 120 * 1024 * 1024   # mémoire maximale des dessins gardés (petites machines)


def _sprite(spec_key, spec: "Spec", u: int, emotion: str, viseme: str, blink: bool, action: str, phase: int,
            look: int, bob: int) -> tuple[Image.Image, int, int]:
    """Dessin mis en cache : les mêmes états reviennent sans cesse (auditeurs immobiles, visèmes)."""
    key = (spec_key, u, emotion, viseme, blink, action, phase, look, bob)
    hit = _CACHE.get(key)
    if hit is None:
        hit = _draw(spec, u, emotion, viseme, blink, action, phase, look, bob)
        size = hit[0].width * hit[0].height * 4
        if _CACHE_BYTES[0] + size > CACHE_BUDGET:
            _CACHE.clear()
            _CACHE_BYTES[0] = 0
        _CACHE[key] = hit
        _CACHE_BYTES[0] += size
    return hit


def _draw(spec: "Spec", u: int, emotion: str, viseme: str, blink: bool, action: str, phase: int,
          look: int, bob: int) -> tuple[Image.Image, int, int]:
    """Personnage dessiné tourné vers la DROITE. → (image RGBA, x des pieds, y des pieds)."""
    U = u * SS
    R = spec.R * U
    pad = int(1.5 * U)
    W = int(2 * pad + max(2 * spec.bw * U, 2.2 * R))
    H = int(pad + spec.height * U + 0.25 * U)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, feet = W // 2, H - int(0.15 * U)
    line = max(2, int(0.035 * U))
    dark = shade(spec.skin, 0.62)

    # ---- attitude du corps selon le geste
    jump = {"shocked": [0.0, -0.22, -0.3, -0.12], "laugh": [0.0, -0.06, 0.0, -0.06]}.get(action, [0, 0, 0, 0])[phase] * U
    lean = {"angry": 0.1, "point": 0.06, "talk_hands": 0.03}.get(action, 0.0) * U
    shake = (0.03 * U * (1 if phase % 2 else -1)) if action == "angry" else 0
    raise_ = -0.07 * U if action == "shrug" else 0
    bx = cx + shake
    top = feet - spec.bh * U + jump                                     # haut du corps
    bw = spec.bw * U
    neck_top = top - spec.neck * U + raise_
    hx, hy = bx + lean, neck_top - R * 1.0 + bob * 0.02 * U            # centre de la tête

    def arm(pose, near: bool, wiggle: float):
        sx = bx + (0.55 if near else -0.35) * bw + lean * 0.6
        sy = top + 0.2 * U + raise_
        al = max(0.72, min(1.1, spec.bh / 1.6))                         # bras proportionnés au corps
        (ex, ey), (px, py) = ((v[0] * al, v[1] * al) for v in pose)
        k = 1.0 if near else 0.9
        e = (sx + ex * U * k, sy + ey * U)
        h = (sx + px * U * k, sy + (py + wiggle) * U)
        return [(sx, sy), e, h]

    wig = [0.0, -0.1, 0.0, 0.08][phase] if action in ("talk", "talk_hands") else 0.0
    near_pose, far_pose = POSES.get(action, (REST, REST))
    near, far = arm(near_pose, True, wig), arm(far_pose, False, -wig)
    if action == "facepalm":
        near = [near[0], (near[0][0] + 0.42 * U, near[0][1] - 0.25 * U), (hx + 0.3 * R, hy - 0.05 * R)]
    if action == "phone":
        near = [near[0], (near[0][0] + 0.4 * U, near[0][1] - 0.1 * U), (hx - 0.55 * R, hy + 0.1 * R)]
    aw = 0.17 * U
    sleeve = shade(spec.outfit, 0.86)

    # ---- bras arrière, jambes, corps
    _limb(d, far[:2], aw, sleeve)
    _limb(d, far[1:], aw * 0.85, shade(spec.skin, 0.9))
    for lx in (-0.45, 0.45):
        d.rectangle([bx + lx * bw - 0.1 * U, feet - 0.3 * U + jump, bx + lx * bw + 0.1 * U, feet + jump], fill=(45, 42, 60))
        _ell(d, bx + lx * bw + 0.06 * U, feet + jump, 0.17 * U, 0.08 * U, (30, 28, 40))
    body_box = [bx - bw + lean * 0.5, top, bx + bw + lean * 0.5, feet - 0.22 * U + jump]
    if spec.shape == "pear":
        d.ellipse([body_box[0], top - 0.1 * U, body_box[2], body_box[3] + 0.1 * U], fill=spec.outfit,
                  outline=shade(spec.outfit, 0.7), width=line)
    else:
        d.rounded_rectangle(body_box, radius=int((0.2 if spec.shape == "block" else 0.3) * U), fill=spec.outfit,
                            outline=shade(spec.outfit, 0.7), width=line)
    if spec.shape == "block":  # salopette : bretelles et poche
        for s in (-0.5, 0.5):
            d.line([bx + s * bw, top, bx + s * bw * 0.8, top + 0.5 * U], fill=shade(spec.outfit, 0.7), width=line * 2)
        d.rounded_rectangle([bx - 0.3 * bw, top + 0.45 * U, bx + 0.3 * bw, top + 0.8 * U], radius=int(0.05 * U),
                            outline=shade(spec.outfit, 0.7), width=line)
    # cou
    if spec.neck > 0.02:
        d.rectangle([hx - 0.12 * U - lean * 0.3, neck_top - 0.1 * U, hx + 0.12 * U - lean * 0.3, top + 0.12 * U],
                    fill=shade(spec.skin, 0.88))

    # ---- tête (vue de trois quarts)
    f = 0.2 * R                                                          # décalage des traits vers l'avant
    if "bun" in spec.acc:
        _ell(d, hx - 0.25 * R, hy - 1.25 * R, 0.4 * R, 0.4 * R, spec.hair)
        d.line([hx - 0.75 * R, hy - 1.1 * R, hx + 0.3 * R, hy - 1.5 * R], fill=(242, 194, 48), width=line * 2)
    bald = "moustache" in spec.acc and "cap" not in spec.acc
    if not bald:
        _ell(d, hx - 0.12 * R, hy - 0.18 * R, 1.0 * R, 1.12 * R, spec.hair)   # masse de cheveux derrière
    else:
        for sy_ in (-0.15, 0.2):
            _ell(d, hx - 0.85 * R, hy + sy_ * R, 0.3 * R, 0.34 * R, spec.hair)  # touffes sur les côtés
    _ell(d, hx - 0.8 * R, hy + 0.08 * R, 0.17 * R, 0.24 * R, shade(spec.skin, 0.9), dark, line)  # oreille
    _ell(d, hx, hy, 0.9 * R, 1.06 * R, spec.skin, dark, line)                                    # crâne
    _ell(d, hx + 0.1 * R, hy + 0.55 * R, 0.62 * R, 0.56 * R, spec.skin)                          # mâchoire
    if not bald and "cap" not in spec.acc:
        d.pieslice([hx - 0.95 * R, hy - 1.12 * R, hx + 0.8 * R, hy + 0.2 * R], 180, 335, fill=spec.hair)
    if "cap" in spec.acc:
        d.pieslice([hx - 0.95 * R, hy - 1.2 * R, hx + 0.95 * R, hy + 0.3 * R], 180, 360, fill=spec.outfit,
                   outline=shade(spec.outfit, 0.7), width=line)
        d.rounded_rectangle([hx + 0.2 * R, hy - 0.52 * R, hx + 1.5 * R, hy - 0.36 * R], radius=int(0.08 * R),
                            fill=shade(spec.outfit, 0.8))
    # yeux
    wide = emotion in ("surprised", "scared")
    half = emotion in ("sarcastic", "smug", "angry", "sad")
    eyes = [(hx + f - 0.22 * R, hy - 0.12 * R, 0.9), (hx + f + 0.32 * R, hy - 0.12 * R, 1.0)]
    for ex, ey, k in eyes:
        rx, ry = 0.17 * R * k, (0.25 if wide else 0.2) * R * k
        if "sunglasses" in spec.acc:
            d.rounded_rectangle([ex - 0.27 * R * k, ey - 0.2 * R, ex + 0.27 * R * k, ey + 0.2 * R],
                                radius=int(0.08 * R), fill=(18, 18, 24))
            d.line([ex - 0.18 * R, ey - 0.1 * R, ex + 0.02 * R, ey - 0.1 * R], fill=(90, 90, 110), width=line)
            continue
        if blink:
            d.arc([ex - rx, ey - ry * 0.4, ex + rx, ey + ry * 0.6], 0, 180, fill=dark, width=line * 2)
        else:
            _ell(d, ex, ey, rx, ry, (255, 255, 255), dark, line)
            _ell(d, ex + (0.06 + 0.05 * look) * R, ey + (0.03 if half else 0) * R, rx * 0.5, rx * 0.5, (40, 28, 20))
            if half:
                d.pieslice([ex - rx - line, ey - ry - line, ex + rx + line, ey + ry + line], 180, 360, fill=spec.skin)
                d.line([ex - rx, ey, ex + rx, ey], fill=dark, width=line)
        if "round_glasses" in spec.acc:
            d.ellipse([ex - 0.27 * R * k, ey - 0.27 * R, ex + 0.27 * R * k, ey + 0.27 * R], outline=(30, 30, 40),
                      width=line * 2)
    if spec.acc & {"sunglasses", "round_glasses"}:
        d.line([eyes[0][0] + 0.2 * R, eyes[0][1], eyes[1][0] - 0.2 * R, eyes[1][1]], fill=(25, 25, 32), width=line * 2)
        d.line([eyes[0][0] - 0.26 * R, eyes[0][1], hx - 0.78 * R, hy - 0.02 * R], fill=(25, 25, 32), width=line * 2)
    # sourcils : (hauteur, pente) pour l'œil arrière puis l'œil avant ; pente > 0 = extérieur relevé
    brow = {"angry": ((0.0, -0.13), (0.0, 0.13)), "sad": ((-0.04, 0.1), (-0.04, -0.1)),
            "surprised": ((-0.16, 0.0), (-0.16, 0.0)), "scared": ((-0.14, 0.08), (-0.14, -0.08)),
            "sarcastic": ((0.02, 0.0), (-0.15, 0.05)), "smug": ((0.02, 0.0), (-0.1, 0.06)),
            "happy": ((-0.06, 0.0), (-0.06, 0.0))}.get(emotion, ((0.0, 0.0), (0.0, 0.0)))
    for (ex, ey, k), (dy, sl) in zip(eyes, brow):
        by = ey - (0.42 if "sunglasses" not in spec.acc else 0.36) * R + dy * R
        d.line([ex - 0.2 * R, by + sl * R, ex + 0.2 * R, by - sl * R], fill=shade(spec.hair, 0.8), width=int(line * 2.6))
    # nez caricatural, moustache
    d.polygon([(hx + f + 0.3 * R, hy - 0.02 * R), (hx + f + 0.72 * R, hy + 0.3 * R), (hx + f + 0.34 * R, hy + 0.38 * R)],
              fill=shade(spec.skin, 0.9))
    _ell(d, hx + f + 0.56 * R, hy + 0.3 * R, 0.17 * R, 0.13 * R, shade(spec.skin, 0.9))
    mx, my = hx + f + 0.12 * R, hy + 0.62 * R
    if "moustache" in spec.acc:
        d.chord([mx - 0.4 * R, my - 0.36 * R, mx + 0.5 * R, my - 0.06 * R], 180, 360, fill=spec.hair)
    # bouche
    lip, inside, teeth = (120, 40, 40), (70, 14, 18), (250, 250, 245)
    w_, o_ = {"A": (0.3, 0.0), "B": (0.34, 0.1), "C": (0.34, 0.2), "D": (0.36, 0.32), "E": (0.2, 0.26),
              "F": (0.13, 0.16), "G": (0.3, 0.07), "H": (0.3, 0.24), "X": (0.3, 0.0)}.get(viseme, (0.3, 0.0))
    if o_ == 0.0:
        curve = {"happy": 0.16, "smug": 0.1, "sad": -0.12, "angry": -0.1, "sarcastic": 0.05}.get(emotion, 0.03)
        if viseme == "A" and curve >= 0:
            curve = 0.0
        if abs(curve) < 0.02:
            d.line([mx - w_ * R, my, mx + w_ * R, my], fill=lip, width=int(line * 1.8))
        elif curve > 0:
            d.arc([mx - w_ * R, my - curve * 2 * R, mx + w_ * R, my + curve * R], 15, 165, fill=lip, width=int(line * 1.8))
        else:
            d.arc([mx - w_ * R, my, mx + w_ * R, my - curve * 3 * R], 195, 345, fill=lip, width=int(line * 1.8))
    else:
        _ell(d, mx, my + o_ * R * 0.3, w_ * R, o_ * R, inside, lip, line)
        if viseme in ("B", "C", "D", "G"):
            d.chord([mx - w_ * R * 0.8, my - o_ * R * 0.7, mx + w_ * R * 0.8, my + o_ * R * 0.5], 180, 360, fill=teeth)
        if viseme in ("D", "H"):
            _ell(d, mx, my + o_ * R * 0.95, w_ * R * 0.5, o_ * R * 0.32, (214, 96, 104))
    # ---- bras avant (peut passer devant le visage)
    _limb(d, near[:2], aw, sleeve)
    _limb(d, near[1:], aw * 0.85, spec.skin)
    _ell(d, near[2][0], near[2][1], 0.12 * U, 0.12 * U, spec.skin, dark, line)
    _ell(d, far[2][0], far[2][1], 0.1 * U, 0.1 * U, shade(spec.skin, 0.9))
    if action == "phone":
        d.rounded_rectangle([near[2][0] - 0.07 * U, near[2][1] - 0.2 * U, near[2][0] + 0.07 * U, near[2][1] + 0.12 * U],
                            radius=int(0.03 * U), fill=(25, 25, 30))
    out = img.resize((W // SS, H // SS), Image.LANCZOS)
    return out, cx // SS, feet // SS


# --------------------------------------------------------------------------- décors dessinés
def draw_plate(set_id: str, color: str, out: Path, size=(1920, 1080)) -> Path:
    W, H = size
    rng = random.Random(set_id)
    img = Image.new("RGB", (W * SS // 2, H * SS // 2))
    w, h = img.size
    d = ImageDraw.Draw(img)
    base = rgb(color)

    def sky(y1):
        for y in range(0, y1, 2):
            t = y / y1
            d.rectangle([0, y, w, y + 2], fill=(int(70 + 90 * t), int(185 + 45 * t), int(240 + 10 * t)))

    if set_id in ("rue", "street"):
        sky(int(h * 0.7))
        for i in range(7):   # immeubles lointains
            x = i * w / 6 - 60 + rng.randint(-30, 30)
            d.rectangle([x, h * (0.36 + rng.random() * 0.12), x + w / 7, h * 0.7], fill=(120, 190, 215))
        for x0, hw in ((0.04, 0.3), (0.42, 0.26), (0.74, 0.3)):   # pavillons
            x, ww = x0 * w, hw * w
            y = h * (0.3 + rng.random() * 0.06)
            d.rectangle([x, y, x + ww, h * 0.7], fill=(246, 240, 228))
            d.polygon([(x - 0.03 * w, y), (x + ww / 2, y - h * 0.17), (x + ww + 0.03 * w, y)], fill=(205, 100, 78))
            for k in (0.18, 0.62):
                d.rectangle([x + ww * k, y + h * 0.07, x + ww * (k + 0.2), y + h * 0.2], fill=(150, 215, 240),
                            outline=(190, 150, 120), width=6)
        for i in range(14):   # haies
            cx = i * w / 12 + rng.randint(-40, 40)
            r = h * (0.09 + rng.random() * 0.06)
            _ell(d, cx, h * 0.66, r * 1.5, r, (64, 140, 96))
        d.rectangle([0, h * 0.62, w, h * 0.78], fill=(226, 138, 112))       # mur de briques
        for r_ in range(5):
            for c in range(24):
                if rng.random() < 0.3:
                    x = c * w / 22 + (r_ % 2) * w / 44
                    d.rectangle([x, h * (0.635 + r_ * 0.028), x + w / 26, h * (0.652 + r_ * 0.028)], fill=(208, 120, 98))
        d.rectangle([0, h * 0.78, w, h * 0.82], fill=(92, 176, 96))         # herbe
        d.rectangle([0, h * 0.82, w, h * 0.9], fill=(198, 196, 190))        # trottoir
        d.rectangle([0, h * 0.9, w, h], fill=(96, 96, 108))                 # chaussée
        d.rectangle([w * 0.9, h * 0.1, w * 0.905, h * 0.82], fill=(110, 110, 125))   # lampadaire
        _ell(d, w * 0.9, h * 0.1, w * 0.02, h * 0.012, (110, 110, 125))
    else:
        wall = shade(base, 1.45)
        d.rectangle([0, 0, w, h * 0.78], fill=wall)
        d.rectangle([0, h * 0.78, w, h], fill=shade(base, 0.75))
        d.rectangle([0, h * 0.76, w, h * 0.79], fill=shade(base, 0.55))     # plinthe
        if set_id in ("salon", "living"):
            d.rectangle([w * 0.06, h * 0.14, w * 0.3, h * 0.5], fill=(150, 215, 240), outline=(250, 245, 235), width=14)
            d.rounded_rectangle([w * 0.52, h * 0.52, w * 0.95, h * 0.8], radius=40, fill=(168, 92, 84))
            d.rounded_rectangle([w * 0.5, h * 0.6, w * 0.97, h * 0.84], radius=40, fill=(190, 108, 98))
            for i in range(3):
                d.rectangle([w * (0.56 + i * 0.12), h * 0.2, w * (0.64 + i * 0.12), h * 0.34], fill=(250, 240, 220),
                            outline=(120, 90, 70), width=8)
            _ell(d, w * 0.4, h * 0.9, w * 0.3, h * 0.06, (200, 120, 110))
        elif set_id in ("garage",):
            d.rectangle([0, h * 0.46, w, h * 0.5], fill=(180, 50, 50))
            for sx in (0.05, 0.62):
                for sy in range(3):
                    d.rectangle([w * sx, h * (0.2 + sy * 0.17), w * (sx + 0.3), h * (0.22 + sy * 0.17)], fill=(60, 70, 110))
                    for i in range(5):
                        cc = rng.choice([(232, 190, 50), (60, 110, 200), (200, 80, 60)])
                        d.rectangle([w * (sx + 0.01 + i * 0.058), h * (0.12 + sy * 0.17), w * (sx + 0.055 + i * 0.058),
                                     h * (0.2 + sy * 0.17)], fill=cc)
            d.rectangle([w * 0.44, h * 0.08, w * 0.48, h * 0.78], fill=(200, 45, 45))
            for i in range(3):
                _ell(d, w * 0.9, h * (0.74 - i * 0.07), w * 0.07, h * 0.04, (30, 30, 34))
        elif set_id in ("epicerie", "shop"):
            for sy in range(4):
                d.rectangle([w * 0.03, h * (0.2 + sy * 0.14), w * 0.97, h * (0.215 + sy * 0.14)], fill=shade(base, 0.6))
                for i in range(26):
                    cc = rng.choice([(232, 190, 50), (220, 80, 70), (80, 170, 110), (70, 120, 210), (240, 240, 235)])
                    hh = rng.uniform(0.05, 0.1)
                    d.rectangle([w * (0.04 + i * 0.035), h * (0.2 + sy * 0.14 - hh), w * (0.066 + i * 0.035),
                                 h * (0.2 + sy * 0.14)], fill=cc)
            d.rectangle([w * 0.6, h * 0.62, w, h * 0.84], fill=shade(base, 0.9), outline=shade(base, 0.5), width=10)
        else:   # voiture et autres : habitacle
            sky(int(h * 0.6))
            d.polygon([(0, 0), (w, 0), (w, h * 0.12), (w * 0.86, h * 0.2), (w * 0.14, h * 0.2), (0, h * 0.12)], fill=(60, 58, 72))
            d.polygon([(0, h * 0.12), (w * 0.14, h * 0.2), (w * 0.1, h * 0.62), (0, h * 0.7)], fill=(60, 58, 72))
            d.polygon([(w, h * 0.12), (w * 0.86, h * 0.2), (w * 0.9, h * 0.62), (w, h * 0.7)], fill=(60, 58, 72))
            d.rectangle([0, h * 0.6, w, h], fill=(52, 50, 64))
            for sx in (0.2, 0.58):
                d.rounded_rectangle([w * sx, h * 0.42, w * (sx + 0.22), h * 0.98], radius=40, fill=(150, 104, 78))
                d.rounded_rectangle([w * (sx + 0.04), h * 0.3, w * (sx + 0.18), h * 0.46], radius=30, fill=(150, 104, 78))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).save(out)
    return out


# --------------------------------------------------------------------------- mise en scène et rendu
def _slots(n: int) -> list[float]:
    return [0.5] if n == 1 else [0.28, 0.72] if n == 2 else [0.2 + 0.6 * i / (n - 1) for i in range(n)]


def _viseme_at(perf: dict, t: float) -> str:
    v = "X"
    for cue in perf.get("visemes") or []:
        if cue["t"] <= t - perf["start"]:
            v = cue["v"]
        else:
            break
    return v


class Stage:
    def __init__(self, shot: dict, size: tuple[int, int] | None = None, follow_speaker: bool = False):
        """`size` : autre format que celui de la scène (ex. vertical 1080×1920) ;
        `follow_speaker` : la caméra reste en gros plan sur celui qui parle (formats verticaux)."""
        self.shot = shot
        self.W, self.H = size or shot["resolution"]
        self.SW = max(self.W, int(self.H * 16 / 9))   # largeur de la scène : le décor reste en 16:9
        self.follow = follow_speaker
        self.fps = shot["fps"]
        chars = shot["characters"]
        self.specs = {c["id"]: Spec(c, shot.get("style") or {}) for c in chars}
        us = _slots(len(chars))
        self.u = {c["id"]: us[i] for i, c in enumerate(chars)}
        order = sorted(self.u, key=self.u.get)
        mid = (len(order) - 1) / 2
        # face à face : ceux de gauche regardent à droite, ceux de droite à gauche
        self.facing = {cid: (1 if i < mid or (i == mid and len(order) == 1) else -1 if i > mid else 1)
                       for i, cid in enumerate(order)}
        self.perfs = sorted(shot["performances"], key=lambda p: p["start"])
        plate = shot["set"].get("plate")
        if plate and Path(plate).exists():
            self.plate = Image.open(plate).convert("RGB")
        else:
            self.plate = Image.new("RGB", (self.W, self.H), rgb(shot["set"]["color"]))
        self._bg: dict[tuple, Image.Image] = {}
        self.blinks = {}
        for cid in self.specs:
            rng, t, times = random.Random(f"{shot['scene_id']}:{cid}"), 0.0, []
            while t < shot["duration"]:
                t += rng.uniform(2.2, 5.5)
                times.append(t)
            self.blinks[cid] = times

    def _camera(self, t: float) -> tuple[float, float, float]:
        if self.follow:
            sp = next((p["character"] for p in self.perfs if p["start"] <= t < p["end"]), None) or \
                next((p["character"] for p in reversed(self.perfs) if p["end"] <= t), None) or \
                (self.perfs[0]["character"] if self.perfs else next(iter(self.u)))
            return 1.5, self.u[sp] + 0.02 * self.facing[sp], 0.56
        cur = self.shot["shots"][0] if self.shot["shots"] else {"camera": "wide"}
        for s in self.shot["shots"]:
            if s["start"] <= t:
                cur = s
        cam = cur["camera"]
        if cam.startswith("close:") and cam.split(":", 1)[1] in self.u:
            cid = cam.split(":", 1)[1]
            return 2.0, self.u[cid] + 0.04 * self.facing[cid], 0.5
        z, _, _ = shot_framing(cur, self.shot)
        return z, 0.5, 0.5

    def _background(self, cam) -> tuple[Image.Image, float, float]:
        z, cx, cy = cam
        zw, zh = int(self.SW * z), int(self.H * z)
        ox = min(max(0, cx * zw - self.W / 2), zw - self.W)
        oy = min(max(0, cy * zh - self.H / 2), zh - self.H)
        key = (round(z, 3), int(ox), int(oy))
        if key not in self._bg:
            big = self.plate.resize((zw, zh), Image.BILINEAR)
            self._bg[key] = big.crop((int(ox), int(oy), int(ox) + self.W, int(oy) + self.H))
        return self._bg[key], ox, oy

    def state(self, cid: str, t: float, speaker: str | None) -> dict:
        perf = next((p for p in self.perfs if p["character"] == cid and p["start"] <= t < p["end"]), None)
        last = next((p for p in reversed(self.perfs) if p["character"] == cid and p["end"] <= t), None)
        blink = any(0 <= t - b < 0.12 for b in self.blinks[cid])
        look = 0
        if speaker and speaker != cid:   # regarde celui qui parle
            look = 1 if (self.u[speaker] - self.u[cid]) * self.facing[cid] > 0 else -1
        if perf:
            env = perf.get("envelope") or []
            i = int((t - perf["start"]) * self.fps)
            amp = env[i] if i < len(env) else 0.0
            return {"emotion": perf["emotion"], "viseme": _viseme_at(perf, t), "blink": blink,
                    "action": perf["action"], "phase": int((t - perf["start"]) * 6) % 4, "look": 1,
                    "bob": -2 if amp > 0.66 else -1 if amp > 0.33 else 0}
        # à l'écoute : garde un instant l'émotion de sa dernière réplique, puis redevient neutre
        emo = last["emotion"] if last and t - last["end"] < 1.5 else "neutral"
        return {"emotion": emo, "viseme": "X", "blink": blink, "action": "idle", "phase": 0, "look": look, "bob": 0}

    def frame(self, t: float) -> Image.Image:
        cam = self._camera(t)
        z = cam[0]
        bg, ox, oy = self._background(cam)
        img = bg.copy()
        speaker = next((p["character"] for p in self.perfs if p["start"] <= t < p["end"]), None)
        mean_h = sum(s.height for s in self.specs.values()) / len(self.specs)
        u_px = max(8, int(CHAR_H * self.H * z / mean_h))
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        todo = []
        for cid in sorted(self.specs, key=lambda c: (c == speaker, self.u[c])):   # le locuteur passe devant
            spec = self.specs[cid]
            st = self.state(cid, t, speaker)
            sprite, ax, ay = _sprite(spec.key(), spec, u_px, st["emotion"], st["viseme"], st["blink"], st["action"],
                                     st["phase"], st["look"], st["bob"])
            if self.facing[cid] < 0:
                sprite, ax = sprite.transpose(Image.FLIP_LEFT_RIGHT), sprite.width - ax
            x = self.u[cid] * self.SW * z - ox
            y = FEET_V * self.H * z - oy
            sd.ellipse([x - spec.bw * u_px * 1.3, y - 0.07 * u_px, x + spec.bw * u_px * 1.3, y + 0.09 * u_px], fill=(0, 0, 0, 70))
            todo.append((sprite, int(x - ax), int(y - ay)))
        img.paste(shadow, (0, 0), shadow)
        for sprite, x, y in todo:
            img.paste(sprite, (x, y), sprite)
        return img


def render_scene(shot: dict, out: Path, size: tuple[int, int] | None = None,
                 window: tuple[float, float] | None = None, follow_speaker: bool = False) -> Path:
    """`window` = (début, fin) en secondes dans la scène, pour n'en rendre qu'un extrait."""
    stage = Stage(shot, size, follow_speaker)
    t0, t1 = window or (0.0, shot["duration"])
    n = max(2, int(math.ceil((t1 - t0) * stage.fps)))
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{stage.W}x{stage.H}", "-r", str(stage.fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "21", "-pix_fmt", "yuv420p", "-an", str(out)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            proc.stdin.write(stage.frame(t0 + i / stage.fps).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"rendu marionnettes échoué : {err[-800:]}")
    return out


def still(char: dict, style: dict, emotion: str, action: str, viseme: str = "X", px: int = 768) -> Image.Image:
    """Personnage seul sur fond transparent (stickers, fiches) au sommet de son geste."""
    spec = Spec(char, style)
    u = int(px * 0.8 / spec.height)
    sprite, _, _ = _sprite(spec.key(), spec, u, emotion, viseme, False, action, 1, 1, 0)
    box = sprite.getbbox()
    return sprite.crop(box) if box else sprite


# =========================================================================== moteur vectoriel (Cairo)
try:
    import cairo as _cairo  # noqa: F401
    from . import toon2d as T
    VECTOR = True
except ImportError:  # pragma: no cover - Cairo absent : on garde le dessin simple ci-dessus
    VECTOR = False

LegacyStage = Stage


class VectorStage:
    """Scène dessinée en vectoriel : caméras en coordonnées « monde », poussée lente dans chaque plan,
    décor adouci avec parallaxe, gestes lissés, regards et clignements."""
    SPACING = 2.55

    def __init__(self, shot: dict, size: tuple[int, int] | None = None, follow_speaker: bool = False):
        from PIL import ImageFilter  # noqa: F401
        self.shot = shot
        self.W, self.H = size or shot["resolution"]
        self.fps = shot["fps"]
        self.follow = follow_speaker
        chars = shot["characters"]
        self.specs = {c["id"]: Spec(c, shot.get("style") or {}) for c in chars}
        n = len(chars)
        order = [c["id"] for c in sorted(chars, key=lambda c: c["slot"])]
        self.x = {cid: (i - (n - 1) / 2) * self.SPACING for i, cid in enumerate(order)}
        mid = (n - 1) / 2
        self.facing = {cid: (1 if i < mid or n == 1 else -1 if i > mid else 1) for i, cid in enumerate(order)}
        self.perfs = sorted(shot["performances"], key=lambda p: p["start"])
        self.Hmax = max(s.height for s in self.specs.values())
        self.Rmax = max(s.R for s in self.specs.values())
        span = self.SPACING * (n - 1) + 2.6 * self.Rmax + 0.6
        self.s_wide = min(self.H * 0.96 / (self.Hmax * 0.8), self.W * 0.94 / span)
        plate = shot["set"].get("plate")
        self.plate = Image.open(plate).convert("RGB") if plate and Path(plate).exists() else \
            Image.new("RGB", (1920, 1080), rgb(shot["set"]["color"]))
        self._bg: dict = {}
        self.pose: dict = {}
        self.last_t = None
        self.blinks = {}
        for cid in self.specs:
            rng, t, times = random.Random(f"{shot['scene_id']}:{cid}"), 0.0, []
            while t < shot["duration"] + 6:
                t += rng.uniform(2.2, 5.5)
                times.append(t)
            self.blinks[cid] = times

    # ---- qui parle, et quel plan
    def speaker(self, t):
        return next((p["character"] for p in self.perfs if p["start"] <= t < p["end"]), None)

    def _shot_at(self, t):
        cur, start, end = {"camera": "wide"}, 0.0, self.shot["duration"]
        for s_ in self.shot["shots"] or []:
            if s_["start"] <= t:
                cur, start, end = s_, s_["start"], s_["end"]
        return cur, start, end

    def head_y(self, cid):
        sp = self.specs[cid]
        return -(sp.bh + sp.neck) - sp.R * 0.92

    def camera(self, t):
        """→ (échelle px/unité, centre x monde, centre y monde, zoom du décor)."""
        cur, start, end = self._shot_at(t)
        prog = min(1.0, max(0.0, (t - start) / max(0.5, end - start)))
        push = 1 + 0.035 * round(prog * 6) / 6          # poussée lente, quantifiée (cache du décor)
        sp = self.speaker(t) or cur.get("speaker") or next(iter(self.specs))
        cam = "follow" if self.follow else cur["camera"]
        if cam.startswith("close:") and cam.split(":", 1)[1] in self.specs:
            cid = cam.split(":", 1)[1]
            R = self.specs[cid].R
            s = self.H * 0.88 / (3.3 * R) * push
            return s, self.x[cid] + 0.25 * R * self.facing[cid], self.head_y(cid) + 0.55 * R, 1.55
        if cam == "follow":
            spec = self.specs[sp]
            s = min(self.H * 0.6 / (spec.height * 0.9), self.W * 0.95 / (2.7 * spec.R)) * push
            cy = self.head_y(sp) + (self.H * 0.5 - self.H * 0.3) / s
            return s, self.x[sp] + 0.2 * spec.R * self.facing[sp], cy, 1.4
        if cam == "two_shot":
            others = [c for c in self.specs if c != sp]
            mate = min(others, key=lambda c: abs(self.x[c] - self.x[sp])) if others else sp
            s = self.s_wide * 1.38 * push
            cx = (self.x[sp] + self.x[mate]) / 2
            cy = self.head_y(sp) + (self.H * 0.5 - self.H * 0.34) / s
            return s, cx, cy, 1.22
        s = self.s_wide * push
        cy = -self.Hmax + (self.H * 0.5 - self.H * 0.05) / s
        return s, 0.0, cy, 1.0

    def background(self, cam):
        s, cx, cy, zb = cam
        from PIL import ImageFilter
        zb = zb * (s / max(1e-6, self.s_wide * zb)) ** 0.15 if zb > 1 else s / self.s_wide
        zb = max(1.0, round(zb, 2))
        world_w = self.W / self.s_wide
        offx = int(cx / world_w * self.W * 0.55 * zb)      # parallaxe : le décor bouge moins que les personnages
        offy = int((cy - (-self.Hmax + (self.H * 0.45) / self.s_wide)) * self.s_wide * 0.25)
        key = (zb, offx, offy)
        img = self._bg.get(key)
        if img is None:
            pw, ph = int(self.W * zb * 1.06), int(self.H * zb * 1.06)
            big = self.plate.resize((pw, ph), Image.BICUBIC)
            x0 = min(max(0, (pw - self.W) // 2 + offx), pw - self.W)
            y0 = min(max(0, (ph - self.H) // 2 + offy), ph - self.H)
            img = big.crop((x0, y0, x0 + self.W, y0 + self.H))
            radius = self.H / 1080 * (1.6 + 4.5 * (zb - 1))   # profondeur de champ : plus floue en gros plan
            img = img.filter(ImageFilter.GaussianBlur(radius))
            if len(self._bg) > 24:
                self._bg.clear()
            self._bg[key] = img
        return img

    def state(self, cid, t, speaker, dt):
        perf = next((p for p in self.perfs if p["character"] == cid and p["start"] <= t < p["end"]), None)
        last = next((p for p in reversed(self.perfs) if p["character"] == cid and p["end"] <= t), None)
        spec = self.specs[cid]
        blink = any(0 <= t - b < 0.11 for b in self.blinks[cid])
        if perf:
            env = perf.get("envelope") or []
            i = int((t - perf["start"]) * self.fps)
            amp = env[i] if i < len(env) else 0.0
            action, emo, vis, phase_t = perf["action"], perf["emotion"], _viseme_at(perf, t), t - perf["start"]
        else:
            amp, vis, phase_t = 0.0, "X", t
            action = "idle"
            emo = last["emotion"] if last and t - last["end"] < 1.6 else "neutral"
        look = 1
        if speaker and speaker != cid:
            look = 1 if (self.x[speaker] - self.x[cid]) * self.facing[cid] > 0 else -1
        tgt = T.target_pose(spec, action, t, phase_t, amp)
        k = 1.0 if dt is None or dt <= 0 or dt > 0.5 else min(1.0, dt * 9.0)   # ~0,15 s pour changer de geste
        self.pose[cid] = T.smooth(self.pose.get(cid) if dt is not None else None, tgt, k)
        return {"emotion": emo, "viseme": vis, "blink": blink, "look": look, "pose": self.pose[cid], "t": t, "amp": amp}

    def frame(self, t: float) -> Image.Image:
        import cairo
        dt = None if self.last_t is None else t - self.last_t
        self.last_t = t
        cam = self.camera(t)
        s, cx, cy, _ = cam
        img = self.background(cam).copy()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        ctx = cairo.Context(surf)
        ctx.set_antialias(cairo.ANTIALIAS_BEST)
        speaker = self.speaker(t)
        states = {cid: self.state(cid, t, speaker, dt) for cid in self.specs}
        for cid in sorted(self.specs, key=lambda c: (c == speaker, abs(self.x[c]))):
            px = (self.x[cid] - cx) * s + self.W / 2
            py = (0 - cy) * s + self.H / 2
            if s > self.s_wide * 1.6:   # gros plan : pas de demi-visage coupé au bord du cadre
                R = self.specs[cid].R * s
                if px + 0.6 * R < 0 or px - 0.6 * R > self.W or min(px, self.W - px) < 0.55 * R:
                    continue
            ctx.save()
            ctx.translate(px, py)
            if self.facing[cid] < 0:
                ctx.scale(-1, 1)
            T.draw_character(ctx, self.specs[cid], states[cid], s)
            ctx.restore()
        surf.flush()
        layer = T.surface_to_pil(surf)
        img.paste(layer, (0, 0), layer)
        return img


def _vector_still(char: dict, style: dict, emotion: str, action: str, viseme: str = "X", px: int = 768) -> Image.Image:
    import cairo
    spec = Spec(char, style)
    u = px * 0.82 / spec.height
    W, H = int(px * 1.2), int(px * 1.05)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surf)
    pose = T.target_pose(spec, action, 0.4, 0.4, 0.6)
    st = {"emotion": emotion, "viseme": viseme, "blink": False, "look": 1, "pose": pose, "t": 0.3, "amp": 0.6,
          "no_shadow": True}
    ctx.translate(W / 2, H - 0.12 * u)
    T.draw_character(ctx, spec, st, u)
    surf.flush()
    img = T.surface_to_pil(surf)
    box = img.getbbox()
    return img.crop(box) if box else img


if VECTOR:
    Stage = VectorStage  # noqa: F811
    still = _vector_still  # noqa: F811
