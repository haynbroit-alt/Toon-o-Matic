"""Dessin vectoriel des personnages (Cairo) : qualité « dessin animé », sur simple processeur.

Chaque personnage est construit à partir de sa fiche (silhouette, couleurs, accessoires) avec des
courbes lisses (Catmull-Rom → Bézier), des volumes ombrés (dégradés), un contour encré, de vraies
mains et des yeux expressifs. Le personnage est dessiné tourné vers la droite, vue de trois quarts ;
le miroir est appliqué par l'appelant.

Unités : 1 unité = `u` pixels ; origine = entre les pieds, y vers le bas (convention Cairo).
"""
from __future__ import annotations

import math

import cairo

INK = (0.13, 0.09, 0.12)


def rgbf(c):
    return tuple(v / 255 for v in c[:3])


def mix(a, b, t):
    return tuple(x * (1 - t) + y * t for x, y in zip(a, b))


def lighten(c, t):
    return mix(c, (1, 1, 1), t)


def darken(c, t):
    return mix(c, (0, 0, 0), t)


def ink_of(c):
    return mix(darken(c, 0.62), INK, 0.35)


# --------------------------------------------------------------------------- courbes
def smooth_path(ctx, pts, closed=True, tension=1.0):
    """Courbe lisse passant par tous les points (Catmull-Rom convertie en Bézier cubiques)."""
    n = len(pts)
    if n < 2:
        return
    ctx.move_to(*pts[0])
    rng = range(n) if closed else range(n - 1)
    for i in rng:
        p0 = pts[(i - 1) % n] if closed or i > 0 else pts[i]
        p1, p2 = pts[i], pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if closed or i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6 * tension, p1[1] + (p2[1] - p0[1]) / 6 * tension)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6 * tension, p2[1] - (p3[1] - p1[1]) / 6 * tension)
        ctx.curve_to(*c1, *c2, *p2)
    if closed:
        ctx.close_path()


def shaded_fill(ctx, base, x0, y0, x1, y1, light=0.16, shadow=0.24):
    """Dégradé de volume : lumière en haut à gauche, ombre en bas à droite."""
    g = cairo.LinearGradient(x0, y0, x1, y1)
    g.add_color_stop_rgb(0, *lighten(base, light))
    g.add_color_stop_rgb(0.5, *base)
    g.add_color_stop_rgb(1, *darken(base, shadow))
    ctx.set_source(g)


def fill_ink(ctx, fill_fn, ink, lw):
    """Remplit le chemin courant (via fill_fn) puis l'encre."""
    fill_fn()
    ctx.fill_preserve()
    ctx.set_source_rgb(*ink)
    ctx.set_line_width(lw)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.stroke()


def blob(ctx, pts, base, lw, box=None, ink=None, light=0.16, shadow=0.24):
    smooth_path(ctx, pts)
    x0, y0, x1, y1 = box or (min(p[0] for p in pts), min(p[1] for p in pts),
                             max(p[0] for p in pts), max(p[1] for p in pts))
    fill_ink(ctx, lambda: shaded_fill(ctx, base, x0, y0, x1, y1, light, shadow), ink or ink_of(base), lw)


def ellipse(ctx, cx, cy, rx, ry, rot=0.0):
    ctx.save()
    ctx.translate(cx, cy)
    ctx.rotate(rot)
    ctx.scale(max(rx, 1e-3), max(ry, 1e-3))
    ctx.arc(0, 0, 1, 0, 2 * math.pi)
    ctx.restore()


def tube(ctx, pts, width, color, lw):
    """Membre encré : trait d'encre plus large, puis trait de couleur par-dessus."""
    for w, col in ((width + 2 * lw, ink_of(color)), (width, color)):
        ctx.set_line_width(w)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        ctx.set_source_rgb(*col)
        smooth_path(ctx, pts, closed=False, tension=0.8) if len(pts) > 2 else (ctx.move_to(*pts[0]), ctx.line_to(*pts[1]))
        ctx.stroke()


# --------------------------------------------------------------------------- pièces du visage
VISEME = {"A": (0.30, 0.00), "B": (0.34, 0.09), "C": (0.34, 0.19), "D": (0.37, 0.31), "E": (0.22, 0.25),
          "F": (0.15, 0.16), "G": (0.31, 0.07), "H": (0.31, 0.23), "X": (0.30, 0.00)}
BROWS = {"angry": ((0.02, -0.16), (0.02, 0.16)), "sad": ((-0.05, 0.12), (-0.05, -0.12)),
         "surprised": ((-0.2, 0.0), (-0.2, 0.0)), "scared": ((-0.17, 0.1), (-0.17, -0.1)),
         "sarcastic": ((0.03, 0.0), (-0.18, 0.06)), "smug": ((0.02, 0.0), (-0.12, 0.07)),
         "happy": ((-0.07, 0.0), (-0.07, 0.0)), "neutral": ((0.0, 0.0), (0.0, 0.0))}
MOUTH_CURVE = {"happy": 0.15, "smug": 0.09, "sad": -0.11, "angry": -0.09, "sarcastic": 0.05, "scared": -0.05}


def draw_eye(ctx, ex, ey, r, k, st, lw, skin, look):
    emo = st["emotion"]
    wide = emo in ("surprised", "scared")
    lid = 0.0 if wide else 0.45 if emo in ("sarcastic", "smug") else 0.32 if emo in ("angry", "sad") else 0.12
    rx, ry = 0.19 * r * k, (0.27 if wide else 0.23) * r * k
    if st["blink"]:
        ctx.set_source_rgb(*ink_of(skin))
        ctx.set_line_width(lw * 1.6)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(ex - rx, ey)
        ctx.curve_to(ex - rx * 0.4, ey + ry * 0.5, ex + rx * 0.4, ey + ry * 0.5, ex + rx, ey)
        ctx.stroke()
        return
    ellipse(ctx, ex, ey, rx, ry)
    fill_ink(ctx, lambda: ctx.set_source_rgb(0.98, 0.97, 0.95), ink_of(skin), lw)
    # iris + pupille + reflet, décalés vers ce qu'il regarde
    ctx.save()
    ellipse(ctx, ex, ey, rx, ry)
    ctx.clip()
    px = ex + (0.25 + 0.25 * look) * rx
    py = ey + 0.08 * ry
    ir = rx * (0.55 if wide else 0.66)
    ellipse(ctx, px, py, ir, ir)
    g = cairo.RadialGradient(px, py - ir * 0.3, ir * 0.1, px, py, ir)
    g.add_color_stop_rgb(0, 0.52, 0.36, 0.22)
    g.add_color_stop_rgb(1, 0.26, 0.16, 0.10)
    ctx.set_source(g)
    ctx.fill()
    ellipse(ctx, px, py, ir * 0.5, ir * 0.5)
    ctx.set_source_rgb(0.06, 0.04, 0.05)
    ctx.fill()
    ellipse(ctx, px - ir * 0.32, py - ir * 0.38, ir * 0.22, ir * 0.22)
    ctx.set_source_rgba(1, 1, 1, 0.9)
    ctx.fill()
    # paupière supérieure (émotion)
    if lid > 0:
        ctx.rectangle(ex - rx * 1.2, ey - ry * 1.2, rx * 2.4, ry * 1.2 * (lid * 1.8))
        ctx.set_source_rgb(*darken(skin, 0.06))
        ctx.fill()
    ctx.restore()
    # trait de paupière
    ctx.set_source_rgb(*INK)
    ctx.set_line_width(lw * 1.5)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    y_lid = ey - ry + lid * 2 * ry * 0.9
    ctx.move_to(ex - rx * 1.05, y_lid + ry * 0.12)
    ctx.curve_to(ex - rx * 0.4, y_lid - ry * 0.1, ex + rx * 0.4, y_lid - ry * 0.1, ex + rx * 1.05, y_lid + ry * 0.08)
    ctx.stroke()


def draw_mouth(ctx, mx, my, r, st, lw, skin):
    w, o = VISEME.get(st["viseme"], VISEME["X"])
    w *= r
    lip = darken(mix(skin, (0.75, 0.3, 0.3), 0.45), 0.25)
    if o <= 0.001:
        c = MOUTH_CURVE.get(st["emotion"], 0.02) * r
        ctx.set_source_rgb(*lip)
        ctx.set_line_width(lw * 1.7)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(mx - w, my - c * 0.4)
        ctx.curve_to(mx - w * 0.3, my + c, mx + w * 0.3, my + c, mx + w, my - c * 0.4)
        ctx.stroke()
        if st["emotion"] in ("smug", "sarcastic"):   # coin relevé d'un seul côté
            ctx.move_to(mx + w * 0.85, my - c * 0.2)
            ctx.line_to(mx + w * 1.15, my - c * 1.4 - 0.03 * r)
            ctx.stroke()
        return
    o *= r
    curve = MOUTH_CURVE.get(st["emotion"], 0.0) * r * 0.5
    ctx.move_to(mx - w, my - curve)
    ctx.curve_to(mx - w * 0.5, my - o * 0.55, mx + w * 0.5, my - o * 0.55, mx + w, my - curve)
    ctx.curve_to(mx + w * 0.6, my + o * 1.15, mx - w * 0.6, my + o * 1.15, mx - w, my - curve)
    ctx.close_path()
    ctx.set_source_rgb(0.32, 0.07, 0.09)
    ctx.fill_preserve()
    ctx.save()
    ctx.clip_preserve()
    ctx.new_path()
    if st["viseme"] in ("B", "C", "D", "G", "H"):          # dents du haut
        ctx.rectangle(mx - w, my - o, 2 * w, o * 0.42)
        ctx.set_source_rgb(0.98, 0.97, 0.93)
        ctx.fill()
    if st["viseme"] in ("D", "E", "H"):                    # langue
        ellipse(ctx, mx + w * 0.1, my + o * 0.75, w * 0.55, o * 0.35)
        ctx.set_source_rgb(0.85, 0.42, 0.45)
        ctx.fill()
    ctx.restore()
    ctx.move_to(mx - w, my - curve)
    ctx.curve_to(mx - w * 0.5, my - o * 0.55, mx + w * 0.5, my - o * 0.55, mx + w, my - curve)
    ctx.curve_to(mx + w * 0.6, my + o * 1.15, mx - w * 0.6, my + o * 1.15, mx - w, my - curve)
    ctx.close_path()
    ctx.set_source_rgb(*lip)
    ctx.set_line_width(lw * 1.3)
    ctx.stroke()


def draw_hand(ctx, x, y, ang, size, skin, lw, closed=False):
    """Main en « moufle » orientée selon l'avant-bras, avec pouce."""
    ctx.save()
    ctx.translate(x, y)
    ctx.rotate(ang)
    s = size
    ink = ink_of(skin)
    pts = [(-0.1 * s, -0.42 * s), (0.55 * s, -0.4 * s), (0.85 * s, -0.08 * s), (0.82 * s, 0.25 * s),
           (0.45 * s, 0.42 * s), (-0.05 * s, 0.38 * s)] if not closed else \
        [(-0.1 * s, -0.4 * s), (0.45 * s, -0.42 * s), (0.62 * s, 0.0), (0.45 * s, 0.4 * s), (-0.05 * s, 0.38 * s)]
    smooth_path(ctx, pts)
    fill_ink(ctx, lambda: shaded_fill(ctx, skin, -0.2 * s, -0.4 * s, 0.8 * s, 0.4 * s, 0.1, 0.18), ink, lw)
    ellipse(ctx, 0.25 * s, -0.42 * s, 0.24 * s, 0.13 * s, -0.5)
    fill_ink(ctx, lambda: ctx.set_source_rgb(*skin), ink, lw)
    if not closed:
        ctx.set_source_rgb(*ink)
        ctx.set_line_width(lw * 0.8)
        for fy in (-0.12, 0.08):
            ctx.move_to(0.55 * s, fy * s)
            ctx.line_to(0.82 * s, (fy + 0.02) * s)
            ctx.stroke()
    ctx.restore()


# --------------------------------------------------------------------------- personnage complet
def head_points(R, jaw):
    """Contour de tête de trois quarts (vers la droite), en unités de rayon."""
    j = jaw
    return [(-0.93 * R, 0.0), (-0.82 * R, -0.62 * R), (-0.32 * R, -1.04 * R), (0.34 * R, -0.98 * R),
            (0.80 * R, -0.58 * R), (0.92 * R, -0.08 * R), (0.90 * R, 0.38 * R), (0.68 * R, (0.82 + 0.05 * j) * R),
            (0.22 * R, (1.02 + 0.08 * j) * R), (-0.30 * R, (0.90 + 0.04 * j) * R), (-0.72 * R, 0.52 * R)]


def draw_character(ctx, spec, st, u):
    """Dessine le personnage, pieds en (0,0), tourné vers la droite.
    `st` : emotion, viseme, blink, look (-1..1), pose (dict lissé), t (secondes), amp (voix 0..1)."""
    p = st["pose"]
    U = u
    lw = max(1.2, 0.045 * U)
    skin, outfit, hair = rgbf(spec.skin), rgbf(spec.outfit), rgbf(spec.hair)
    R = spec.R * U
    bh, bw = spec.bh * U, spec.bw * U
    jump = p["jump"] * U
    lean = p["lean"] * U
    breathe = 1 + 0.012 * math.sin(st["t"] * 2 * math.pi / 3.6 + hash(spec.id) % 7)
    top = -bh * breathe + jump
    hip = -0.34 * bh + jump
    neck_top = top - spec.neck * U - p["shrug"] * U
    hx = lean + p["head_dx"] * U
    hy = neck_top - R * 0.92 + p["bob"] * U
    shape = spec.shape

    if not st.get("no_shadow"):   # ombre portée au sol (pas sur les stickers)
        ellipse(ctx, 0, 0, bw * 1.5, 0.12 * U)
        ctx.set_source_rgba(0, 0, 0, 0.22)
        ctx.fill()

    # ---- bras arrière (derrière le corps)
    al = max(0.72, min(1.15, spec.bh / 1.6)) * U
    shw = {"pear": 0.42, "block": 0.82}.get(shape, 0.9) * bw     # demi-largeur des épaules
    sh_back = (-0.62 * shw + lean * 0.5, top + 0.2 * U)
    sh_front = (0.72 * shw + lean * 0.7, top + 0.2 * U - p["shrug"] * U)

    def arm_pts(sh, e, h):
        return [sh, (sh[0] + e[0] * al, sh[1] + e[1] * al), (sh[0] + h[0] * al, sh[1] + h[1] * al)]

    back = arm_pts(sh_back, p["be"], p["bh"])
    front = arm_pts(sh_front, p["fe"], p["fh"])
    sleeve = darken(outfit, 0.08)
    arm_w = (0.19 + 0.07 * spec.bw) * U          # bras proportionnés à la carrure
    long_sleeves = shape == "column"
    bare = (0.96, 0.95, 0.92) if shape == "block" else skin   # Lina : t-shirt blanc sous la salopette
    tube(ctx, back[:2], arm_w, darken(sleeve if long_sleeves else bare, 0.1), lw)
    tube(ctx, back[1:], arm_w * 0.82, darken(sleeve if long_sleeves else skin, 0.1), lw)
    draw_hand(ctx, *back[2], math.atan2(back[2][1] - back[1][1], back[2][0] - back[1][0]), 0.26 * U,
              darken(skin, 0.08), lw, closed=p["fist"])

    # ---- jambes (pantalon) et chaussures
    trouser = (0.22, 0.22, 0.32) if shape != "block" else darken(outfit, 0.05)
    for lx in (-0.42, 0.42):
        x = lx * bw * (0.7 if shape == "pear" else 1)
        tube(ctx, [(x, hip), (x + 0.02 * U, -0.18 * U + jump)], 0.32 * U, trouser, lw)
        ellipse(ctx, x + 0.12 * U, -0.07 * U + jump, 0.24 * U, 0.11 * U)
        fill_ink(ctx, lambda: ctx.set_source_rgb(0.16, 0.13, 0.15), INK, lw)

    # ---- cou (avant le torse : le col le recouvre)
    if spec.neck > -0.2:
        nw = 0.14 * U * max(1.0, spec.R / 0.7)
        nx = hx - 0.04 * U
        ctx.rectangle(nx - nw, neck_top - R * 0.3, 2 * nw, top + 0.15 * U - (neck_top - R * 0.3))   # haut caché sous la tête
        shaded_fill(ctx, darken(skin, 0.05), nx - nw, 0, nx + nw, 0, 0.05, 0.2)
        ctx.fill()
        ctx.set_source_rgb(*ink_of(skin))
        ctx.set_line_width(lw)
        for xx in (nx - nw, nx + nw):
            ctx.move_to(xx, neck_top)
            ctx.line_to(xx, top + 0.1 * U)
            ctx.stroke()
        ctx.rectangle(nx - nw, neck_top + R * 0.05, 2 * nw, 0.2 * U)          # ombre sous le menton
        ctx.set_source_rgba(0, 0, 0, 0.16)
        ctx.fill()

    # ---- torse
    if shape == "pear":
        pts = [(-0.5 * bw, top), (0.5 * bw, top), (1.0 * bw, top + 0.45 * (hip - top)), (0.95 * bw, hip - 0.05 * U),
               (0.3 * bw, hip + 0.12 * U), (-0.3 * bw, hip + 0.12 * U), (-0.9 * bw, hip - 0.05 * U),
               (-0.95 * bw, top + 0.45 * (hip - top))]
    elif shape == "block":
        pts = [(-0.95 * bw, top + 0.06 * U), (0.95 * bw, top + 0.06 * U), (1.05 * bw, hip), (0.6 * bw, hip + 0.08 * U),
               (-0.6 * bw, hip + 0.08 * U), (-1.05 * bw, hip)]
    else:
        pts = [(-1.05 * bw, top + 0.04 * U), (1.05 * bw, top + 0.04 * U), (1.1 * bw, top + 0.5 * (hip - top)),
               (0.95 * bw, hip + 0.05 * U), (-0.95 * bw, hip + 0.05 * U), (-1.1 * bw, top + 0.5 * (hip - top))]
    pts = [(x + lean * 0.55 * (1 - (y - top) / max(1, hip - top)), y) for x, y in pts]
    if shape == "block":   # t-shirt blanc dessous
        rr = 0.28 * U
        x0_, x1_, y0_, y1_ = -0.9 * bw + lean * 0.45, 0.9 * bw + lean * 0.45, top + 0.02 * U, top + 0.8 * U
        ctx.new_sub_path()
        ctx.arc(x1_ - rr, y0_ + rr, rr, -math.pi / 2, 0)
        ctx.line_to(x1_, y1_)
        ctx.line_to(x0_, y1_)
        ctx.arc(x0_ + rr, y0_ + rr, rr, math.pi, 1.5 * math.pi)
        ctx.close_path()
        fill_ink(ctx, lambda: shaded_fill(ctx, (0.96, 0.95, 0.92), x0_, y0_, x1_, y1_, 0.05, 0.12), ink_of((0.9, 0.9, 0.88)), lw)
        bib = [(-0.62 * bw + lean * 0.45, top + 0.28 * U), (0.62 * bw + lean * 0.45, top + 0.28 * U),
               (0.66 * bw + lean * 0.2, top + 0.6 * U), (0.92 * bw, top + 0.72 * U), (0.92 * bw, hip),
               (0.6 * bw, hip + 0.06 * U), (-0.6 * bw, hip + 0.06 * U), (-0.92 * bw, hip), (-0.92 * bw, top + 0.72 * U),
               (-0.66 * bw + lean * 0.2, top + 0.6 * U)]
        blob(ctx, bib, outfit, lw, box=(-bw, top, bw, hip))
        ctx.set_source_rgb(*ink_of(outfit))
        ctx.set_line_width(lw * 2.2)
        for sx in (-0.5, 0.5):   # bretelles
            ctx.move_to(sx * bw * 1.25 + lean * 0.5, top + 0.02 * U)
            ctx.line_to(sx * bw * 1.05 + lean * 0.45, top + 0.32 * U)
            ctx.stroke()
        ctx.rectangle(-0.3 * bw + lean * 0.3, top + 0.4 * U, 0.6 * bw, 0.28 * U)
        ctx.set_line_width(lw)
        ctx.stroke()
    else:
        blob(ctx, pts, outfit, lw, box=(-bw, top, bw, hip))
        ctx.set_source_rgb(*ink_of(outfit))
        ctx.set_line_width(lw)
        if shape == "column":   # blouson : fermeture, poche, col
            ctx.move_to(0.12 * bw + lean * 0.5, top + 0.15 * U)
            ctx.line_to(0.1 * bw + lean * 0.05, hip)
            ctx.stroke()
            ctx.rectangle(0.3 * bw + lean * 0.4, top + 0.45 * U, 0.45 * bw, 0.22 * U)
            ctx.stroke()
        else:                   # chemise : boutons
            for i in range(4):
                yy = top + (0.2 + 0.22 * i) * (hip - top)
                ellipse(ctx, 0.15 * bw + lean * 0.4 * (1 - i / 4), yy, 0.035 * U, 0.035 * U)
                ctx.fill()
    # col
    collar = (0.96, 0.95, 0.92) if shape == "pear" else darken(outfit, 0.12)
    smooth_path(ctx, [(-0.42 * bw + lean * 0.6, top + 0.02 * U), (hx - 0.05 * U, top + 0.24 * U),
                      (0.48 * bw + lean * 0.6, top + 0.02 * U), (hx, top - 0.03 * U)])
    fill_ink(ctx, lambda: ctx.set_source_rgb(*collar), ink_of(collar), lw)

    # ---- tête
    ctx.save()
    ctx.translate(hx, hy)
    ctx.rotate(p["tilt"])
    jaw = 1.0 if spec.shape == "pear" else 0.6 if spec.shape == "column" else 0.0
    acc = spec.acc
    hairstyle = "cap" if "cap" in acc else "bun" if "bun" in acc else "bald" if "moustache" in acc else "short"
    if hairstyle == "bun":
        ellipse(ctx, -0.32 * R, -1.08 * R, 0.42 * R, 0.4 * R)
        fill_ink(ctx, lambda: shaded_fill(ctx, hair, -0.7 * R, -1.5 * R, 0.1 * R, -0.7 * R), ink_of(hair), lw)
        ctx.set_source_rgb(0.95, 0.76, 0.19)
        ctx.set_line_width(0.07 * R)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(-0.85 * R, -0.95 * R)
        ctx.line_to(0.2 * R, -1.38 * R)
        ctx.stroke()
    if hairstyle in ("short", "bun", "cap"):   # masse de cheveux derrière la tête
        smooth_path(ctx, [(-1.0 * R, 0.2 * R), (-0.95 * R, -0.65 * R), (-0.35 * R, -1.1 * R), (0.3 * R, -1.05 * R),
                          (0.65 * R, -0.75 * R), (-0.2 * R, -0.6 * R), (-0.55 * R, 0.45 * R)])
        fill_ink(ctx, lambda: shaded_fill(ctx, hair, -R, -R, R, R), ink_of(hair), lw)
    if hairstyle == "bald":   # couronne de cheveux bouclés autour de la nuque, derrière le crâne
        curls = [(-0.97, 0.05), (-0.9, -0.28), (-0.78, -0.55), (-1.0, -0.12), (-0.92, 0.3), (-0.62, -0.75),
                 (-0.85, 0.48), (-0.72, -0.4), (-0.86, 0.15)]
        for i, (cx_, cy_) in enumerate(curls):
            r_ = (0.15 + 0.03 * ((i * 7) % 3)) * R
            ellipse(ctx, cx_ * R, cy_ * R, r_, r_ * 0.9)
            fill_ink(ctx, lambda: shaded_fill(ctx, hair, cx_ * R - r_, cy_ * R - r_, cx_ * R + r_, cy_ * R + r_, 0.25, 0.25),
                     ink_of(hair), lw * 0.7)
    # oreille
    ellipse(ctx, -0.48 * R, 0.08 * R, 0.17 * R, 0.25 * R)
    fill_ink(ctx, lambda: shaded_fill(ctx, skin, -0.7 * R, 0, -0.3 * R, 0), ink_of(skin), lw)
    # visage
    hp = head_points(R, jaw)
    smooth_path(ctx, hp)
    g = cairo.RadialGradient(0.15 * R, -0.35 * R, 0.1 * R, 0.0, 0.0, 1.25 * R)
    g.add_color_stop_rgb(0, *lighten(skin, 0.14))
    g.add_color_stop_rgb(0.6, *skin)
    g.add_color_stop_rgb(1, *darken(skin, 0.22))
    flush = (0.85, 0.3, 0.25) if st["emotion"] == "angry" else None
    fill_ink(ctx, lambda: ctx.set_source(g), ink_of(skin), lw)
    if flush:   # colère : le visage rougit
        smooth_path(ctx, hp)
        ctx.set_source_rgba(*flush, 0.18)
        ctx.fill()
    if st["emotion"] in ("happy", "smug") or hairstyle == "bun":   # joues
        ellipse(ctx, 0.62 * R, 0.35 * R, 0.17 * R, 0.1 * R)
        ctx.set_source_rgba(0.95, 0.45, 0.45, 0.28)
        ctx.fill()
    # rides du front (tonton) / barbe naissante
    if hairstyle == "bald":
        ctx.set_source_rgba(*ink_of(skin), 0.55)
        ctx.set_line_width(lw * 0.8)
        for i in range(3):
            yy = -0.7 * R + i * 0.1 * R
            ctx.move_to(0.0 * R, yy)
            ctx.curve_to(0.2 * R, yy - 0.04 * R, 0.45 * R, yy - 0.04 * R, 0.6 * R, yy + 0.02 * R)
            ctx.stroke()
        ellipse(ctx, -0.1 * R, -0.75 * R, 0.22 * R, 0.1 * R, -0.3)   # reflet du crâne
        ctx.set_source_rgba(1, 1, 1, 0.35)
        ctx.fill()
    # yeux (l'œil lointain est plus petit : trois quarts)
    eyes = [(0.0 * R, -0.12 * R, 0.82), (0.46 * R, -0.12 * R, 1.0)]
    for ex, ey, k in eyes:
        if "sunglasses" in acc:
            continue
        draw_eye(ctx, ex, ey, R, k, st, lw, skin, st["look"])
    # sourcils
    (d1, s1), (d2, s2) = BROWS.get(st["emotion"], BROWS["neutral"])
    for (ex, ey, k), (dy, sl) in zip(eyes, ((d1, s1), (d2, s2))):
        by = ey - (0.42 if "sunglasses" not in acc else 0.4) * R + dy * R
        ctx.set_source_rgb(*darken(hair if hairstyle != "bald" else rgbf(spec.hair), 0.35))
        ctx.set_line_width(0.075 * R * k)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(ex - 0.2 * R * k, by + sl * R)
        ctx.curve_to(ex - 0.05 * R, by + sl * R * 0.3 - 0.04 * R, ex + 0.08 * R, by - sl * R * 0.3 - 0.04 * R,
                     ex + 0.22 * R * k, by - sl * R)
        ctx.stroke()
    # lunettes
    if "sunglasses" in acc:
        for ex, ey, k in eyes:
            ctx.new_path()
            w_, h_ = 0.3 * R * k, 0.21 * R
            smooth_path(ctx, [(ex - w_, ey - h_), (ex + w_, ey - h_), (ex + w_ * 0.9, ey + h_ * 0.7), (ex, ey + h_),
                              (ex - w_ * 0.9, ey + h_ * 0.7)], tension=0.6)
            g2 = cairo.LinearGradient(ex, ey - h_, ex, ey + h_)
            g2.add_color_stop_rgb(0, 0.22, 0.22, 0.28)
            g2.add_color_stop_rgb(1, 0.04, 0.04, 0.06)
            fill_ink(ctx, lambda: ctx.set_source(g2), INK, lw)
            ctx.move_to(ex - w_ * 0.6, ey - h_ * 0.5)
            ctx.line_to(ex - w_ * 0.1, ey - h_ * 0.5)
            ctx.set_source_rgba(1, 1, 1, 0.45)
            ctx.set_line_width(lw)
            ctx.stroke()
    if "round_glasses" in acc:
        for ex, ey, k in eyes:
            ellipse(ctx, ex, ey, 0.29 * R * k, 0.29 * R)
            ctx.set_source_rgba(0.75, 0.88, 1.0, 0.12)
            ctx.fill_preserve()
            ctx.set_source_rgb(0.12, 0.1, 0.12)
            ctx.set_line_width(lw * 1.6)
            ctx.stroke()
    if acc & {"sunglasses", "round_glasses"}:
        ctx.set_source_rgb(0.1, 0.09, 0.1)
        ctx.set_line_width(lw * 1.6)
        ctx.move_to(eyes[0][0] + 0.25 * R, eyes[0][1] - 0.03 * R)
        ctx.curve_to(eyes[0][0] + 0.3 * R, eyes[0][1] - 0.1 * R, eyes[1][0] - 0.3 * R, eyes[1][1] - 0.1 * R,
                     eyes[1][0] - 0.27 * R, eyes[1][1] - 0.03 * R)
        ctx.stroke()
        ctx.move_to(eyes[0][0] - 0.25 * R, eyes[0][1] - 0.05 * R)
        ctx.line_to(-0.62 * R, 0.0)
        ctx.stroke()
    # nez caricatural
    smooth_path(ctx, [(0.58 * R, -0.12 * R), (0.92 * R, 0.08 * R), (1.08 * R, 0.3 * R), (0.95 * R, 0.44 * R),
                      (0.7 * R, 0.42 * R), (0.62 * R, 0.25 * R)])
    fill_ink(ctx, lambda: shaded_fill(ctx, skin, 0.55 * R, -0.1 * R, 1.1 * R, 0.45 * R, 0.12, 0.2), ink_of(skin), lw)
    ellipse(ctx, 0.88 * R, 0.39 * R, 0.06 * R, 0.035 * R)
    ctx.set_source_rgba(*ink_of(skin), 0.7)
    ctx.fill()
    # bouche, moustache
    mx, my = 0.5 * R, 0.66 * R
    draw_mouth(ctx, mx, my, R, st, lw, skin)
    if "moustache" in acc:
        smooth_path(ctx, [(0.15 * R, 0.5 * R), (0.5 * R, 0.4 * R), (0.95 * R, 0.5 * R), (0.85 * R, 0.62 * R),
                          (0.5 * R, 0.56 * R), (0.2 * R, 0.64 * R)])
        fill_ink(ctx, lambda: shaded_fill(ctx, hair, 0, 0.4 * R, R, 0.65 * R, 0.25, 0.15), ink_of(hair), lw)
    # casquette / frange
    if hairstyle == "cap":
        smooth_path(ctx, [(-0.98 * R, -0.25 * R), (-0.8 * R, -0.85 * R), (-0.2 * R, -1.16 * R), (0.45 * R, -1.08 * R),
                          (0.86 * R, -0.62 * R), (0.85 * R, -0.42 * R), (-0.95 * R, -0.18 * R)])
        fill_ink(ctx, lambda: shaded_fill(ctx, outfit, -R, -1.2 * R, R, -0.2 * R), ink_of(outfit), lw)
        smooth_path(ctx, [(0.55 * R, -0.5 * R), (1.45 * R, -0.42 * R), (1.5 * R, -0.3 * R), (0.7 * R, -0.36 * R)],
                    tension=0.5)
        fill_ink(ctx, lambda: ctx.set_source_rgb(*darken(outfit, 0.18)), ink_of(outfit), lw)
    elif hairstyle in ("bun", "short"):
        smooth_path(ctx, [(-0.85 * R, -0.5 * R), (-0.45 * R, -1.02 * R), (0.25 * R, -1.02 * R), (0.75 * R, -0.62 * R),
                          (0.55 * R, -0.5 * R), (0.2 * R, -0.62 * R), (-0.2 * R, -0.52 * R), (-0.55 * R, -0.3 * R)])
        fill_ink(ctx, lambda: shaded_fill(ctx, hair, -R, -R, R, -0.3 * R), ink_of(hair), lw)
    ctx.restore()

    # ---- bras avant (peut passer devant le visage : facepalm, téléphone)
    col_up = sleeve if long_sleeves else bare
    col_lo = sleeve if long_sleeves else skin
    tube(ctx, front[:2], arm_w, col_up, lw)
    tube(ctx, front[1:], arm_w * 0.82, col_lo, lw)
    ang = math.atan2(front[2][1] - front[1][1], front[2][0] - front[1][0])
    draw_hand(ctx, *front[2], ang, 0.28 * U, skin, lw, closed=p["fist"])
    if p.get("phone"):
        ctx.save()
        ctx.translate(*front[2])
        ctx.rotate(ang - 1.2)
        ctx.rectangle(-0.06 * U, -0.22 * U, 0.12 * U, 0.3 * U)
        fill_ink(ctx, lambda: ctx.set_source_rgb(0.1, 0.1, 0.12), INK, lw)
        ctx.restore()
    return {"head": (hx, hy), "R": R}


# --------------------------------------------------------------------------- poses et lissage
POSES = {   # (coude avant, main avant, coude arrière, main arrière) depuis l'épaule ; x>0 devant, y>0 bas
    "idle": ((0.08, 0.48), (0.14, 0.95), (0.04, 0.48), (0.06, 0.95)),
    "talk": ((0.34, 0.4), (0.74, 0.08), (0.04, 0.48), (0.06, 0.95)),
    "talk_hands": ((0.36, 0.36), (0.8, 0.0), (0.3, 0.42), (0.64, 0.08)),
    "point": ((0.58, 0.02), (1.2, -0.1), (0.04, 0.48), (0.06, 0.95)),
    "shrug": ((0.12, 0.42), (0.52, 0.18), (0.1, 0.42), (0.46, 0.2)),
    "angry": ((0.24, 0.45), (0.5, 0.82), (0.22, 0.45), (0.46, 0.82)),
    "shocked": ((0.42, -0.24), (0.6, -0.86), (0.38, -0.24), (0.56, -0.86)),
    "laugh": ((0.3, 0.48), (0.3, 0.84), (0.26, 0.48), (0.28, 0.88)),
    "walk_in": ((0.12, 0.48), (0.3, 0.9), (-0.1, 0.48), (-0.2, 0.9)),
    "walk_out": ((0.12, 0.48), (0.3, 0.9), (-0.1, 0.48), (-0.2, 0.9)),
    "drive": ((0.42, 0.3), (0.85, 0.2), (0.38, 0.3), (0.8, 0.2)),
    "sit": ((0.3, 0.42), (0.6, 0.6), (0.26, 0.42), (0.55, 0.6)),
}
BODY = {"angry": (0.1, 0.0, 0.0), "point": (0.07, 0.0, 0.0), "talk_hands": (0.03, 0.0, 0.0),
        "shocked": (-0.05, -0.18, 0.0), "laugh": (-0.04, -0.04, 0.0), "shrug": (0.0, 0.0, 0.08),
        "facepalm": (0.04, 0.0, 0.0)}   # (inclinaison, saut, haussement d'épaules)


def _reach(spec, lean: float, target: tuple[float, float]):
    """Coude et main (unités de bras, depuis l'épaule avant) pour poser la main sur `target` (unités)."""
    al = max(0.72, min(1.15, spec.bh / 1.6))
    sx, sy = 0.62 * spec.bw + lean * 0.7, -spec.bh + 0.22
    hx, hy = (target[0] - sx) / al, (target[1] - sy) / al
    return (hx * 0.5 + 0.22, hy * 0.5 + 0.28), (hx, hy)


def target_pose(spec, action: str, t: float, phase_t: float, amp: float) -> dict:
    fe, fh, be, bh_ = POSES.get(action, POSES["idle"])
    wig = math.sin(phase_t * 2 * math.pi * 1.4) * 0.07 if action in ("talk", "talk_hands") else 0.0
    lean, jump, shrug = BODY.get(action, (0.0, 0.0, 0.0))
    if action == "shocked":
        jump *= max(0.0, math.sin(min(1.0, phase_t * 2.2) * math.pi))   # un sursaut, pas un vol
    if action == "laugh":
        jump = -0.05 * abs(math.sin(phase_t * 2 * math.pi * 3))
    shake = 0.03 * math.sin(phase_t * 2 * math.pi * 5) if action == "angry" else 0.0
    if spec.shape == "pear" and action in ("idle", "talk", "angry", "laugh"):   # les bras épousent le ventre
        fe = (fe[0] + 0.16 * spec.bw, fe[1] + 0.04)
        fh = (fh[0] + 0.26 * spec.bw, fh[1] - 0.06)
    head = (lean, -(spec.bh + spec.neck) - spec.R * 0.92)
    if action == "facepalm":
        fe, fh = _reach(spec, lean, (head[0] + 0.42 * spec.R, head[1] - 0.12 * spec.R))
    elif action == "phone":
        fe, fh = _reach(spec, lean, (head[0] - 0.35 * spec.R, head[1] + 0.18 * spec.R))
    return {"fe": (fe[0], fe[1] + wig * 0.5), "fh": (fh[0], fh[1] + wig), "be": be, "bh": (bh_[0], bh_[1] - wig * 0.6),
            "lean": lean + shake, "jump": jump, "shrug": shrug,
            "tilt": (0.06 if action in ("sarcastic",) else 0.0) + 0.04 * math.sin(phase_t * 2.1) * (amp > 0.2),
            "head_dx": 0.0, "bob": -0.03 * amp, "fist": action in ("angry",),
            "facepalm": action == "facepalm", "phone": action == "phone"}


def smooth(cur: dict | None, tgt: dict, k: float) -> dict:
    """Rapproche la pose courante de la pose visée : les gestes s'enchaînent au lieu de sauter."""
    if cur is None:
        return dict(tgt)
    out = {}
    for key, v in tgt.items():
        c = cur.get(key, v)
        if isinstance(v, tuple):
            out[key] = tuple(a + (b - a) * k for a, b in zip(c, v))
        elif isinstance(v, bool):
            out[key] = v
        else:
            out[key] = c + (v - c) * k
    return out


def surface_to_pil(surf):
    from PIL import Image
    w, h = surf.get_width(), surf.get_height()
    return Image.frombuffer("RGBa", (w, h), bytes(surf.get_data()), "raw", "BGRa", surf.get_stride(), 1).convert("RGBA")
