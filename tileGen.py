#!/usr/bin/env python3
"""Seamless 256x256 background tile generator.

Usage:
    python tileGen.py                       # all patterns x default palette -> ./tiles
    python tileGen.py --out bg              # choose output folder
    python tileGen.py --colors 3B6EA5 C8AD7F --patterns diamonds scales
    python tileGen.py --colors 1E3A5F-F0A868-E8837C --patterns cubes   # multi-colour
    python tileGen.py --colors sunset ocean --patterns cubes weave     # named scheme
    python tileGen.py --colors sunset --patterns bricks --scale 2     # bigger pattern
    python tileGen.py --schemes             # also render every built-in scheme
    python tileGen.py --list                # show patterns, palette, schemes
    python tileGen.py --zip                 # also write tiles.zip

Output files are named KEY_patternName.png where KEY is the hex code
(3B6EA5_diamonds.png) or, for multi-colour palettes, the hex codes joined by
dashes with the background colour first (1E3A5F-F0A868-E8837C_cubes.png).
A --scale other than 1 adds a suffix: 3B6EA5_bricks_x2.png.

COLOURS
  One colour  -> tone-on-tone: accents are derived from the base colour
                 (lighter on dark bases, darker on light ones).
  2-4 colours -> the first is the background/base; the others are used for the
                 shapes. On the 2.5D patterns that fully cover the tile
                 (cubes, bevels, weave, bricks, shingles, steps, planks) they
                 become the faces / tiles / strips.

PATTERNS
  flat : diamonds scales chevrons crosses pyramids stars waves triangles
  2.5D : cubes (tumbling blocks) bevels (cushion tiles) studs (domed bumps)
         weave (shaded basket weave) bricks (raised running bond)
         shingles (overlapping roof tiles) steps (stepped ziggurat blocks)
         planks (floorboards with grain) ridges (45-degree corrugations)
         rods (woven cylinders) gems (faceted diamonds) slabs (skewed blocks)

SCALE
  --scale multiplies the pattern size inside the tile. 0.5 = twice as many
  repeats, 2 = half as many (bigger motifs). Allowed: 0.25, 0.5, 1, 2. The
  repeat must divide the 256px tile into an even number of cells so the tile
  still wraps seamlessly. Strokes keep their pixel width at every scale.

Every pattern repeats on a grid that divides 256 exactly, is drawn at 4x
supersampling, and extends past the canvas so shapes crossing an edge continue
on the opposite side -> seamless when tiled.
"""
import argparse
import math
import os
import re
import shutil
from PIL import Image, ImageDraw, ImageFont

SIZE = 256
SS = 4          # supersample factor
CELL = 64       # pattern period at scale 1
WHITE, BLACK = (255, 255, 255), (0, 0, 0)

# Pattern-space state, set by set_scale() before each render.
#   F: pattern units -> final pixels (1 / scale)   U: pattern extent   N: cells per U
F = 1.0
U = SIZE
N = SIZE // CELL
SCALES = (0.25, 0.5, 1.0, 2.0)

# name -> hex.  Add / edit freely.
PALETTE = {
    "blue":        "3B6EA5",
    "dark blue":   "1E3A5F",
    "grey":        "6B7280",
    "charcoal":    "3F454D",
    "tan":         "C8AD7F",
    "light orange": "F0A868",
    "light red":   "E8837C",
    "purple":      "8C6BB1",
    "dark purple": "4B2E6F",
    "teal":        "4F8F7B",
    "sage":        "7E9C6B",
    "gold":        "E0C068",
}

# Multi-colour schemes: first colour = background / base.
SCHEMES = {
    "sunset":       ["2B2D5B", "F0A868", "E8837C", "F6D58A"],
    "ocean":        ["1E3A5F", "3B6EA5", "7FB5D6", "DCEBF2"],
    "forest":       ["2F4A3A", "7E9C6B", "C8AD7F", "E9DFC4"],
    "berry":        ["4B2E6F", "8C6BB1", "E8837C", "F4C7C3"],
    "slate-orange": ["3F454D", "F0A868", "6B7280"],
    "teal-gold":    ["234E52", "4F8F7B", "E0C068"],
    "retro":        ["3B6EA5", "E8837C", "F0C27B", "F2E8CF"],
}


# ---------------------------------------------------------------- colour --
def hex_to_rgb(h):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"not a hex colour: {h!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(c):
    return "%02X%02X%02X" % tuple(c)


def parse_colors(spec):
    """Normalise a colour spec to a list of (r, g, b) tuples (background first).

    Accepts '3B6EA5', '1E3A5F-F0A868-E8837C' (dash/comma separated),
    a scheme or palette name ('sunset', 'dark blue'), an (r, g, b) tuple, or a
    list of any of these.
    """
    if isinstance(spec, str):
        name = spec.strip().lower()
        for table in (SCHEMES, PALETTE):
            if name in table:
                spec = table[name]
                break
    if isinstance(spec, str):
        parts = [p for p in re.split(r"[-,\s]+", spec.strip()) if p]
        if not parts:
            raise ValueError("empty colour spec")
        return [hex_to_rgb(p) for p in parts]
    spec = list(spec)
    if len(spec) == 3 and all(isinstance(v, int) for v in spec):
        return [tuple(spec)]
    out = []
    for c in spec:
        out.extend(parse_colors(c))
    return out


def color_key(spec):
    """'3B6EA5' or '1E3A5F-F0A868-...' - used in file names."""
    return "-".join(rgb_to_hex(c) for c in parse_colors(spec))


def mix(c, t, amt):
    amt = max(0.0, min(1.0, amt))
    return tuple(round(a + (b - a) * amt) for a, b in zip(c, t))


def lin(v):
    v /= 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(c):
    r, g, b = (lin(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


ACCENT_MODES = ("auto", "lighter", "darker")


def tones(colors, contrast=1.0, accents="auto"):
    """Colour roles used by the pattern functions.

    colors:   one colour or a palette (see parse_colors); first = background.
    contrast: single colour -> multiplier on every blend amount (1 = default).
              palette       -> vividness of the accent colours (1 = full colour,
                               lower fades them into the background).
    accents:  single colour only: 'auto' picks lighter accents on dark bases and
              darker on light ones; 'lighter' / 'darker' force the direction.
    """
    pal = parse_colors(colors)
    base = pal[0]
    if accents == "lighter":
        go_dark = False
    elif accents == "darker":
        go_dark = True
    else:
        go_dark = luminance(base) > 0.40
    up, down = (BLACK, WHITE) if go_dark else (WHITE, BLACK)
    k = contrast
    T = {
        "base":  base,
        "a":     mix(base, up, 0.07 * k),     # lighter (or darker) fill
        "line":  mix(base, up, 0.20 * k),
        "inner": mix(base, down, 0.10 * k),
        "dot":   mix(base, up, 0.30 * k),
        "ring":  mix(base, up, 0.14 * k),
        "soft":  mix(base, up, 0.035 * k),
        "shade": mix(base, down, 0.06 * k),
        "deep":  mix(base, down, 0.11 * k),
        # for the 2.5D patterns: palette + physical (light from top-left) shading
        "pal":   pal,
        "k":     k,
        "lit":   lambda c, amt=0.16: mix(c, WHITE, amt * k),
        "dim":   lambda c, amt=0.20: mix(c, BLACK, amt * k),
    }
    if len(pal) > 1:
        v = min(1.0, k)
        A = pal[1]
        B = pal[2] if len(pal) > 2 else A
        C = pal[3] if len(pal) > 3 else B

        def acc(c):
            return mix(base, c, v)
        a_up = BLACK if luminance(A) > 0.40 else WHITE
        T.update({
            "a":     acc(A),
            "line":  acc(B),
            "dot":   acc(C),
            "ring":  acc(B),
            "inner": mix(acc(A), a_up, 0.25),
            "soft":  mix(base, A, 0.30 * v),
            "shade": mix(base, BLACK, 0.10 * k),
            "deep":  mix(base, BLACK, 0.20 * k),
        })
    return T


# --------------------------------------------------------------- helpers --
def s(v):
    """Pattern units -> supersampled pixels, honouring the current scale."""
    return round(v * SS * F)


def diamond(cx, cy, r):
    return [(s(cx), s(cy - r)), (s(cx + r), s(cy)), (s(cx), s(cy + r)), (s(cx - r), s(cy))]


def circle(d, cx, cy, r, **kw):
    d.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], **kw)


def rect(d, x0, y0, x1, y1, **kw):
    d.rectangle([s(x0), s(y0), s(x1) - 1, s(y1) - 1], **kw)


def poly(d, pts, **kw):
    d.polygon([(s(x), s(y)) for x, y in pts], **kw)


def fill_all(d, color):
    d.rectangle([0, 0, SIZE * SS, SIZE * SS], fill=color)


W2 = 2 * SS      # a 2px line at final resolution


# -------------------------------------------------------------- patterns --
def p_diamonds(d, T):
    half = CELL // 2
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            cx, cy = i * CELL + half, j * CELL + half
            d.polygon(diamond(cx, cy, half), fill=T["a"])
            d.polygon(diamond(cx, cy, 17), outline=T["inner"], width=W2)
            circle(d, cx, cy, 4, fill=T["dot"])
            bx, by = i * CELL, j * CELL
            circle(d, bx, by, 9, outline=T["ring"], width=W2)
            circle(d, bx, by, 2.5, fill=T["ring"])
            o = diamond(cx, cy, half)
            d.line(o + [o[0]], fill=T["line"], width=W2)


def p_scales(d, T):
    # overlapping circles (fish-scale / seigaiha). Draw top->bottom so lower
    # rows overlap upper rows, including across the wrap.
    r = 32
    step = 32
    for j in range(-2, U // step + 2):
        y = j * step
        for i in range(-2, N + 2):
            x = i * CELL + (step if j % 2 else 0)
            circle(d, x, y, r, fill=T["a"] if j % 2 == 0 else T["base"],
                   outline=T["line"], width=W2)
            circle(d, x, y, 24, outline=T["ring"], width=W2)
            circle(d, x, y, 16, outline=T["ring"], width=W2)
            circle(d, x, y, 8, outline=T["ring"], width=W2)


def p_chevrons(d, T):
    half = 32      # half period, vertical band spacing
    amp = 16

    def path(k):
        return [(s(n * half), s(k * half + (amp if n % 2 else 0)))
                for n in range(-2, U // half + 3)]

    for k in range(-2, U // half + 2):
        top, bot = path(k), path(k + 1)
        if k % 2 == 0:
            d.polygon(top + bot[::-1], fill=T["a"])
    for k in range(-2, U // half + 3):
        pts = path(k)
        d.line(pts, fill=T["line"], width=W2, joint="curve")
        if k % 2 == 0:
            for n in range(-2, U // half + 3):
                if n % 2 == 0:
                    circle(d, n * half, k * half, 2.6, fill=T["dot"])


def p_crosses(d, T):
    half = CELL // 2
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            # plus sign
            cx, cy = i * CELL + half, j * CELL + half
            arm, th = 12, 3.5
            d.rectangle([s(cx - arm), s(cy - th), s(cx + arm), s(cy + th)], fill=T["dot"])
            d.rectangle([s(cx - th), s(cy - arm), s(cx + th), s(cy + arm)], fill=T["dot"])
            circle(d, cx, cy, 1.6, fill=T["base"])
            # small x at the grid corner, with a faint ring
            bx, by = i * CELL, j * CELL
            circle(d, bx, by, 13, outline=T["soft"], width=W2)
            for sgn in (1, -1):
                d.line([s(bx - 7), s(by - 7 * sgn), s(bx + 7), s(by + 7 * sgn)],
                       fill=T["ring"], width=int(SS * 2.6))
            # faint square lattice
            d.line([s(bx), s(by), s(bx + CELL), s(by)], fill=T["soft"], width=SS)
            d.line([s(bx), s(by), s(bx), s(by + CELL)], fill=T["soft"], width=SS)


def p_pyramids(d, T):
    # each 64px cell split by both diagonals into four lit/shaded facets
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x, y = i * CELL, j * CELL
            TL, TR = (s(x), s(y)), (s(x + CELL), s(y))
            BL, BR = (s(x), s(y + CELL)), (s(x + CELL), s(y + CELL))
            C = (s(x + CELL / 2), s(y + CELL / 2))
            d.polygon([TL, TR, C], fill=T["a"])        # top: lit
            d.polygon([TL, BL, C], fill=T["soft"])     # left: soft light
            d.polygon([TR, BR, C], fill=T["shade"])    # right: shade
            d.polygon([BL, BR, C], fill=T["deep"])     # bottom: deepest shade
            for a, b in ((TL, TR), (TL, BL), (TL, C), (TR, C), (BL, C), (BR, C)):
                d.line([a, b], fill=T["ring"], width=SS)
            circle(d, x + CELL / 2, y + CELL / 2, 2, fill=T["dot"])


# ------------------------------------------------------------ 2.5D patterns
# Light comes from the top-left: top / left faces are lit, bottom / right dim.
# Colours: 1 colour -> shaded tones of it; 2 colours -> bg + one accent;
# 3-4 colours -> the extra colours become the individual faces / tiles.

def p_cubes(d, T):
    """Tumbling blocks: isometric-style cubes, identical everywhere."""
    pal, lit, dim = T["pal"], T["lit"], T["dim"]
    n = len(pal)
    if n == 1:
        top, left, right = lit(pal[0], 0.14), pal[0], dim(pal[0], 0.16)
    elif n == 2:
        top, left, right = pal[1], pal[0], dim(pal[0], 0.20)
    else:
        top, left, right = pal[1], pal[0], pal[2]
    edge = pal[3] if n > 3 else dim(left, 0.30)

    a = U / 8              # half cube width  -> 4 cubes across
    h = a / 2              # 2:1 top face
    rows = 6               # even, so the half-step row offset wraps cleanly
    step = U / rows
    e = step - h           # vertical edge length
    for j in range(-2, rows + 2):
        for i in range(-2, U // int(2 * a) + 2):
            cx = i * 2 * a + (a if j % 2 else 0)
            cy = j * step
            faces = (
                (top,   [(cx, cy), (cx - a, cy - h), (cx, cy - 2 * h), (cx + a, cy - h)]),
                (left,  [(cx, cy), (cx - a, cy - h), (cx - a, cy - h + e), (cx, cy + e)]),
                (right, [(cx, cy), (cx + a, cy - h), (cx + a, cy - h + e), (cx, cy + e)]),
            )
            for col, pts in faces:
                P = [(s(x), s(y)) for x, y in pts]
                d.polygon(P, fill=col)
                d.line(P + [P[0]], fill=edge, width=int(SS * 1.5))


def p_bevels(d, T):
    """Cushion tiles: square tiles with lit top/left and shaded bottom/right bevels."""
    pal, lit, dim = T["pal"], T["lit"], T["dim"]
    n = len(pal)
    if n == 1:
        grout, A, B = dim(pal[0], 0.32), pal[0], lit(pal[0], 0.05)
    elif n == 2:
        grout, A, B = pal[0], pal[1], mix(pal[1], pal[0], 0.45)
    else:
        grout, A, B = pal[0], pal[1], pal[2]
    fill_all(d, grout)
    gap, bw = 2, 9
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x0, y0 = i * CELL + gap, j * CELL + gap
            x1, y1 = (i + 1) * CELL - gap, (j + 1) * CELL - gap
            c = A if (i + j) % 2 == 0 else B
            poly(d, [(x0, y0), (x1, y0), (x1 - bw, y0 + bw), (x0 + bw, y0 + bw)], fill=lit(c, 0.24))
            poly(d, [(x0, y0), (x0 + bw, y0 + bw), (x0 + bw, y1 - bw), (x0, y1)], fill=lit(c, 0.10))
            poly(d, [(x0, y1), (x1, y1), (x1 - bw, y1 - bw), (x0 + bw, y1 - bw)], fill=dim(c, 0.30))
            poly(d, [(x1, y0), (x1, y1), (x1 - bw, y1 - bw), (x1 - bw, y0 + bw)], fill=dim(c, 0.15))
            rect(d, x0 + bw, y0 + bw, x1 - bw, y1 - bw, fill=c)


def p_studs(d, T):
    """Domed studs with a soft drop shadow, on two interleaved lattices."""
    pal, lit, dim = T["pal"], T["lit"], T["dim"]
    n = len(pal)
    bg = pal[0]
    if n == 1:
        cA = cB = lit(bg, 0.10)
    elif n == 2:
        cA = cB = pal[1]
    else:
        cA, cB = pal[1], pal[2]
    R = 19
    studs = []
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            studs.append((i * CELL + CELL / 2, j * CELL + CELL / 2, cA))
            studs.append((i * CELL, j * CELL, cB))
    for cx, cy, _ in studs:                       # shadows first, so no dome is overdrawn
        circle(d, cx + 3, cy + 4, R + 1.5, fill=dim(bg, 0.12))
        circle(d, cx + 3.5, cy + 4.5, R, fill=dim(bg, 0.26))
    steps = 12
    for cx, cy, c in studs:                       # domes: rim -> lit highlight
        dark, light = dim(c, 0.24), lit(c, 0.30)
        for t in range(steps + 1):
            f = t / steps
            r = R * (1 - 0.66 * f)
            off = (R - r) * 0.42
            circle(d, cx - off, cy - off, r, fill=mix(dark, light, f))


def p_weave(d, T):
    """Basket weave: 4 shaded strips per cell, alternating direction."""
    pal, lit, dim = T["pal"], T["lit"], T["dim"]
    n = len(pal)
    if n == 1:
        gap_c, H, V = dim(pal[0], 0.50), pal[0], lit(pal[0], 0.07)
    elif n == 2:
        gap_c, H, V = dim(pal[0], 0.50), pal[0], pal[1]
    else:
        gap_c, H, V = pal[0], pal[1], pal[2]
    fill_all(d, gap_c)
    sw, sg, m = 14, 2, 1          # strip width, gap, margin: 4*14 + 3*2 + 2*1 = 64
    hi, lo, end = 3.5, 3.5, 4
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x, y = i * CELL, j * CELL
            horiz = (i + j) % 2 == 0
            c = H if horiz else V
            for k in range(4):
                off = m + k * (sw + sg)
                if horiz:
                    x0, y0, x1, y1 = x, y + off, x + CELL, y + off + sw
                    rect(d, x0, y0, x1, y1, fill=c)
                    rect(d, x0, y0, x1, y0 + hi, fill=lit(c, 0.20))
                    rect(d, x0, y1 - lo, x1, y1, fill=dim(c, 0.26))
                    rect(d, x0, y0 + hi, x0 + end, y1 - lo, fill=dim(c, 0.14))
                    rect(d, x1 - end, y0 + hi, x1, y1 - lo, fill=dim(c, 0.14))
                else:
                    x0, y0, x1, y1 = x + off, y, x + off + sw, y + CELL
                    rect(d, x0, y0, x1, y1, fill=c)
                    rect(d, x0, y0, x0 + hi, y1, fill=lit(c, 0.20))
                    rect(d, x1 - lo, y0, x1, y1, fill=dim(c, 0.26))
                    rect(d, x0 + hi, y0, x1 - lo, y0 + end, fill=dim(c, 0.14))
                    rect(d, x0 + hi, y1 - end, x1 - lo, y1, fill=dim(c, 0.14))


def _pal25(T):
    """(grout, A, B) for the block-style 2.5D patterns below."""
    pal, lit, dim = T["pal"], T["lit"], T["dim"]
    n = len(pal)
    if n == 1:
        return dim(pal[0], 0.40), pal[0], lit(pal[0], 0.08)
    if n == 2:
        return pal[0], pal[1], mix(pal[1], pal[0], 0.35)
    return pal[0], pal[1], pal[2]


def p_bricks(d, T):
    """Raised running-bond bricks: rows of bevelled bricks, every other row offset."""
    grout, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    fill_all(d, grout)
    gap, bw, rh = 2, 3, 32
    for j in range(-1, U // rh + 1):
        off = CELL // 2 if j % 2 else 0
        for i in range(-1, N + 1):
            x0, x1 = i * CELL + off + gap, (i + 1) * CELL + off - gap
            y0, y1 = j * rh + gap, (j + 1) * rh - gap
            c = A if (i + j) % 2 == 0 else B
            poly(d, [(x0, y0), (x1, y0), (x1 - bw, y0 + bw), (x0 + bw, y0 + bw)], fill=lit(c, 0.24))
            poly(d, [(x0, y0), (x0 + bw, y0 + bw), (x0 + bw, y1 - bw), (x0, y1)], fill=lit(c, 0.10))
            poly(d, [(x0, y1), (x1, y1), (x1 - bw, y1 - bw), (x0 + bw, y1 - bw)], fill=dim(c, 0.30))
            poly(d, [(x1, y0), (x1, y1), (x1 - bw, y1 - bw), (x1 - bw, y0 + bw)], fill=dim(c, 0.15))
            rect(d, x0 + bw, y0 + bw, x1 - bw, y1 - bw, fill=c)


def p_shingles(d, T):
    """Overlapping roof shingles: each row is laid over the one above, with a shadow under its top edge."""
    grout, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    fill_all(d, grout)
    rh = 32
    for j in range(-2, U // rh + 1):
        y0, y1 = j * rh, j * rh + 2 * rh
        off = CELL // 2 if j % 2 else 0
        for i in range(-1, N + 1):
            x0, x1 = i * CELL + off, i * CELL + off + CELL
            c = A if i % 2 == 0 else B
            rect(d, x0 + 1, y0, x1 - 1, y1, fill=c)
            rect(d, x0 + 1, y0, x1 - 1, y0 + 4, fill=dim(c, 0.35))
            rect(d, x0 + 1, y0 + 4, x1 - 1, y0 + 7, fill=lit(c, 0.20))


def p_steps(d, T):
    """Stepped ziggurat blocks: three nested levels per cell, each with lit top/left edges."""
    grout, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    fill_all(d, grout)
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x, y = i * CELL, j * CELL
            c = A if (i + j) % 2 == 0 else B
            for k, cl in enumerate((c, lit(c, 0.08), lit(c, 0.18))):
                ins = k * 12
                x0, y0 = x + ins + 1, y + ins + 1
                x1, y1 = x + CELL - ins - 1, y + CELL - ins - 1
                rect(d, x0, y0, x1, y1, fill=cl)
                rect(d, x0, y0, x1, y0 + 4, fill=lit(cl, 0.22))
                rect(d, x0, y0, x0 + 4, y1, fill=lit(cl, 0.12))
                rect(d, x0, y1 - 4, x1, y1, fill=dim(cl, 0.22))
                rect(d, x1 - 4, y0, x1, y1, fill=dim(cl, 0.12))


def p_planks(d, T):
    """Floorboards: staggered rows of planks with a bevelled edge and a faint grain line."""
    grout, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    fill_all(d, grout)
    rh = 32
    for j in range(-1, U // rh + 1):
        off = CELL // 2 if j % 2 else 0
        y0, y1 = j * rh, (j + 1) * rh
        yc = y0 + rh / 2
        for i in range(-1, N + 1):
            x0, x1 = i * CELL + off + 1, (i + 1) * CELL + off - 1
            c = A if (i + j) % 2 == 0 else B
            rect(d, x0, y0 + 1, x1, y1 - 1, fill=c)
            rect(d, x0, y0 + 1, x1, y0 + 3, fill=lit(c, 0.22))
            rect(d, x0, y1 - 3, x1, y1 - 1, fill=dim(c, 0.25))
            d.line([(s(x0 + 6), s(yc)), (s(x1 - 6), s(yc))], fill=dim(c, 0.12), width=SS)


def p_stars(d, T):
    """Eight-point stars centred in each cell, with a dot at the cell centre and corner."""
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            cx, cy = i * CELL + CELL / 2, j * CELL + CELL / 2
            pts = []
            for k in range(16):
                ang = math.pi * k / 8 - math.pi / 2
                r = 26 if k % 2 == 0 else 11
                pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
            poly(d, pts, fill=T["a"])
            poly(d, pts, outline=T["line"], width=W2)
            circle(d, cx, cy, 4, fill=T["dot"])
            circle(d, i * CELL, j * CELL, 3, fill=T["ring"])


def p_waves(d, T):
    """Horizontal bands bounded by sine waves; alternate bands take the background tone."""
    step, amp = 16, 4
    xs = list(range(-CELL, U + CELL + 1, 4))

    def wave(x, off):
        return off + amp * math.sin(2 * math.pi * x / CELL)

    for j in range(-2, U // step + 2):
        y0 = j * step
        top = [(x, wave(x, y0)) for x in xs]
        bot = [(x, wave(x, y0 + step)) for x in xs]
        poly(d, top + bot[::-1], fill=T["a"] if j % 2 == 0 else T["base"])
        d.line([(s(x), s(y)) for x, y in top], fill=T["line"], width=W2, joint="curve")


def p_triangles(d, T):
    """Square cells split on alternating diagonals into two triangles of different tone."""
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x, y = i * CELL, j * CELL
            x1, y1 = x + CELL, y + CELL
            if (i + j) % 2 == 0:
                t1 = [(x, y), (x1, y), (x1, y1)]
                t2 = [(x, y), (x1, y1), (x, y1)]
            else:
                t1 = [(x, y), (x1, y), (x, y1)]
                t2 = [(x1, y), (x1, y1), (x, y1)]
            poly(d, t1, fill=T["a"])
            poly(d, t2, fill=T["soft"])
            poly(d, t1, outline=T["line"], width=W2)
            poly(d, t2, outline=T["line"], width=W2)


def p_ridges(d, T):
    """45-degree corrugations: bands of two tones, each with a lit leading edge and shaded trailing edge."""
    _, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    w = 32                      # band width measured along x + y
    lo, hi = -U, 2 * U          # y-range the bands are drawn over (clipped to the canvas)
    for k in range(-4, 2 * U // w + 5):
        c0, c1 = k * w, (k + 1) * w
        c = A if k % 2 == 0 else B
        poly(d, [(c0 - lo, lo), (c1 - lo, lo), (c1 - hi, hi), (c0 - hi, hi)], fill=c)
        poly(d, [(c0 - lo, lo), (c0 + 3 - lo, lo), (c0 + 3 - hi, hi), (c0 - hi, hi)], fill=lit(c, 0.25))
        poly(d, [(c1 - 3 - lo, lo), (c1 - lo, lo), (c1 - hi, hi), (c1 - 3 - hi, hi)], fill=dim(c, 0.25))


def p_rods(d, T):
    """Woven cylinders: horizontal rods in one checker colour, vertical rods in the other."""
    grout, A, B = _pal25(T)
    fill_all(d, grout)
    H, steps = 22, 8            # rod diameter, shading bands across it
    for i in range(-1, N + 1):
        for j in range(-1, N + 1):
            x, y = i * CELL, j * CELL
            horiz = (i + j) % 2 == 0
            c = A if horiz else B
            dark, light = T["dim"](c, 0.30), T["lit"](c, 0.35)
            for t in range(steps):
                f = (t + 0.5) / steps
                shade = mix(dark, light, max(0.0, 1 - abs(f - 0.35) * 1.8))
                a0 = -H / 2 + t * H / steps
                a1 = a0 + H / steps + 0.5
                if horiz:
                    cy = y + CELL / 2
                    rect(d, x + 1, cy + a0, x + CELL - 1, cy + a1, fill=shade)
                else:
                    cx = x + CELL / 2
                    rect(d, cx + a0, y + 1, cx + a1, y + CELL - 1, fill=shade)


def p_gems(d, T):
    """Faceted diamonds on two offset lattices; each facet is lit by its angle to the top-left light."""
    grout, A, B = _pal25(T)
    lit, dim = T["lit"], T["dim"]
    fill_all(d, grout)
    r = 30
    for off, c in ((0.0, A), (0.5, B)):
        for i in range(-1, N + 1):
            for j in range(-1, N + 1):
                cx, cy = (i + off) * CELL, (j + off) * CELL
                Tp, Rp, Bp, Lp, C = (cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy)
                poly(d, [Lp, Tp, C], fill=lit(c, 0.22))
                poly(d, [Tp, Rp, C], fill=c)
                poly(d, [Rp, Bp, C], fill=dim(c, 0.22))
                poly(d, [Bp, Lp, C], fill=dim(c, 0.08))
                poly(d, [Tp, Rp, Bp, Lp], outline=dim(c, 0.35), width=int(SS * 1.5))


def p_slabs(d, T):
    """Skewed slabs in offset rows: each shows a top face, a front face and a side face."""
    grout, A, B = _pal25(T)
    dim = T["dim"]
    fill_all(d, grout)
    D, E, S = 24, 8, 14         # top depth, front-face height, skew
    W = CELL - S - 4            # slab width, so neighbouring tops never touch
    for j in range(-2, U // 32 + 2):
        y0 = j * 32
        off = CELL // 2 if j % 2 else 0
        for i in range(-2, N + 2):
            x0 = i * CELL + off + 2
            c = A if (i + j) % 2 == 0 else B
            poly(d, [(x0, y0 + D), (x0 + W, y0 + D), (x0 + W, y0 + D + E), (x0, y0 + D + E)], fill=dim(c, 0.25))
            poly(d, [(x0 + W, y0 + D), (x0 + W + S, y0), (x0 + W + S, y0 + E), (x0 + W, y0 + D + E)], fill=dim(c, 0.40))
            poly(d, [(x0 + S, y0), (x0 + S + W, y0), (x0 + W, y0 + D), (x0, y0 + D)], fill=c)


PATTERNS = {
    "diamonds": p_diamonds,
    "scales": p_scales,
    "chevrons": p_chevrons,
    "crosses": p_crosses,
    "pyramids": p_pyramids,
    "cubes": p_cubes,
    "bevels": p_bevels,
    "studs": p_studs,
    "weave": p_weave,
    "bricks": p_bricks,
    "shingles": p_shingles,
    "steps": p_steps,
    "planks": p_planks,
    "stars": p_stars,
    "waves": p_waves,
    "triangles": p_triangles,
    "ridges": p_ridges,
    "rods": p_rods,
    "gems": p_gems,
    "slabs": p_slabs,
}
PATTERNS_25D = ("cubes", "bevels", "studs", "weave", "bricks", "shingles", "steps", "planks",
                "ridges", "rods", "gems", "slabs")


# ---------------------------------------------------------------- render --
def set_scale(scale):
    """Select the pattern scale used by the next draw call."""
    global F, U, N
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {SCALES}, got {scale}")
    F = float(scale)
    U = int(round(SIZE / scale))
    N = U // CELL


def render(color, pattern, contrast=1.0, accents="auto", scale=1.0):
    """color: hex / palette string, (r, g, b) tuple, or list of them.
    Returns a 256x256 RGB image."""
    set_scale(scale)
    pal = parse_colors(color)
    img = Image.new("RGB", (SIZE * SS, SIZE * SS), pal[0])
    PATTERNS[pattern](ImageDraw.Draw(img), tones(pal, contrast, accents))
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def seam_score(tile):
    """Mean |wrapped-edge diff| relative to mean |adjacent interior diff|."""
    import numpy as np
    a = np.asarray(tile).astype(int)
    wrap = max(np.abs(a[:, 0] - a[:, -1]).mean(), np.abs(a[0] - a[-1]).mean())
    inner = max(np.abs(a[:, 127] - a[:, 128]).mean(), np.abs(a[127] - a[128]).mean(), 0.5)
    return wrap / inner


def contact_sheet(out, colors, patterns, suffix="", thumb=None):
    thumb = thumb or (150 if len(patterns) <= 6 else 120)
    pad, head = 8, 22
    lab = max(88, 6 * max(len(c) for c in colors) + 16)
    w = lab + len(patterns) * (thumb + pad) + pad
    h = head + len(colors) * (thumb + pad) + pad
    sheet = Image.new("RGB", (w, h), (24, 26, 30))
    d = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for c, p in enumerate(patterns):
        d.text((lab + c * (thumb + pad) + pad, 6), p, fill=(220, 222, 226), font=font)
    for r, key in enumerate(colors):
        y = head + r * (thumb + pad) + pad
        d.text((pad, y + thumb // 2 - 5), key, fill=(220, 222, 226), font=font)
        for c, p in enumerate(patterns):
            t = Image.open(os.path.join(out, f"{key}_{p}{suffix}.png"))
            # show 2x2 repeats so the pattern reads at thumbnail size
            m = Image.new("RGB", (SIZE * 2, SIZE * 2))
            for a in range(2):
                for b in range(2):
                    m.paste(t, (a * SIZE, b * SIZE))
            sheet.paste(m.resize((thumb, thumb), Image.LANCZOS),
                        (lab + c * (thumb + pad) + pad, y))
    path = os.path.join(out, f"contact_sheet{suffix}.png")
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="tiles")
    ap.add_argument("--colors", nargs="*",
                    help="hex codes, dash-joined palettes (1E3A5F-F0A868), or scheme names "
                         "(default: built-in single-colour palette)")
    ap.add_argument("--schemes", action="store_true", help="also render every built-in multi-colour scheme")
    ap.add_argument("--patterns", nargs="*", choices=list(PATTERNS), help="default: all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--contrast", type=float, default=1.0,
                    help="accent contrast multiplier (default 1.0)")
    ap.add_argument("--accents", choices=ACCENT_MODES, default="auto",
                    help="force lighter/darker accents on single-colour tiles (default auto)")
    ap.add_argument("--scale", type=float, choices=SCALES, default=1.0,
                    help="pattern size inside the tile: 0.25, 0.5, 1 (default) or 2")
    a = ap.parse_args()

    if a.list:
        print("flat patterns:", ", ".join(p for p in PATTERNS if p not in PATTERNS_25D))
        print("2.5D patterns:", ", ".join(PATTERNS_25D))
        print("scales:", ", ".join(f"{x:g}" for x in SCALES))
        print("single colours:")
        for n, h in PALETTE.items():
            print(f"  {h}  {n}")
        print("schemes (background first):")
        for n, v in SCHEMES.items():
            print(f"  {'-'.join(v)}  {n}")
        return

    colors = [color_key(c) for c in (a.colors or PALETTE.values())]
    if a.schemes:
        colors += [color_key(v) for v in SCHEMES.values()]
    patterns = a.patterns or list(PATTERNS)
    suffix = "" if a.scale == 1 else f"_x{a.scale:g}"
    os.makedirs(a.out, exist_ok=True)

    worst = 0
    for key in colors:
        for p in patterns:
            t = render(key, p, a.contrast, a.accents, a.scale)
            t.save(os.path.join(a.out, f"{key}_{p}{suffix}.png"), optimize=True)
            worst = max(worst, seam_score(t))
    print(f"wrote {len(colors) * len(patterns)} tiles to {a.out}/  (worst seam ratio {worst:.2f}; ~1 = invisible)")
    print("contact sheet:", contact_sheet(a.out, colors, patterns, suffix))
    if a.zip:
        print("zip:", shutil.make_archive(a.out, "zip", a.out))


if __name__ == "__main__":
    main()
