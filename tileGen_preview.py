#!/usr/bin/env python3
"""Interactive terminal preview for the tile generator (tileGen.py).

Keep this file next to tileGen.py and termgfx.py. It shows the tile repeated
across your terminal and lets you tweak colours and patterns live.

Rendering
    Real images when the terminal supports them (kitty graphics protocol, iTerm2
    inline images, or sixel) - at zoom 1.0 you see the tile at its true pixel
    size. Otherwise it falls back to half-block characters (zoom is then a
    thumbnail scale). The protocol is auto-detected; force one with --protocol.

Usage:
    python tileGen_preview.py                          # blue diamonds
    python tileGen_preview.py C8AD7F scales            # start colour + pattern
    python tileGen_preview.py 2B2D5B-F0A868-E8837C cubes    # multi-colour (background first)
    python tileGen_preview.py sunset studs             # built-in scheme name
    python tileGen_preview.py --protocol sixel         # kitty | iterm2 | sixel | blocks
    python tileGen_preview.py --once --size 120x40 ocean weave   # print one frame, no UI
    python tileGen_preview.py --256                    # force 256-colour block mode
    python tileGen_preview.py --truecolor              # force 24-bit blocks (tmux/ssh)

Keys:
    Left/Right  (or [ ])   previous / next pattern
    Up/Down                accent contrast +/- 0.1 (multi-colour: vividness)
    Tab, or 1-4            pick colour slot (1-4 creates it if missing)
    n / d                  add / delete a colour slot (max 4; the first is the background)
    h / H   s / S   l / L  hue / saturation / lightness of the active slot (lowercase = down)
    c / C                  set the active slot from the single-colour palette
    m / M                  load a multi-colour scheme
    x                      type hex: 3B6EA5 for one slot, or 1E3A5F-F0A868-E8837C for a palette
    + / -                  zoom in / out
    t                      toggle repeating tiles / single tile
    a                      accents: auto -> lighter -> darker (single-colour only)
    g                      switch between image mode and half-block mode
    w                      save the current tile as KEY_pattern.png
    r                      reset
    q                      quit (prints the tileGen.py command to reproduce)
"""
import argparse
import colorsys
import os
import shutil
import string
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tileGen as mt  # noqa: E402
import termgfx as gfx   # noqa: E402

IS_WIN = gfx.IS_WIN
if IS_WIN:
    import msvcrt

ESC = "\x1b"
BG_FILL = (24, 26, 30)
ZOOM_MIN, ZOOM_MAX = 0.0625, 3.0
MAX_COLORS = 4
RESERVED = 4                      # status line + 3 help lines
IMAGE_ID = 1
PX_LIMIT = {"kitty": (2000, 1200), "iterm2": (2000, 1200), "sixel": (1280, 720)}
DEFAULT_CELL = (9, 18)


def zkey(mode):
    return "blocks" if mode == "blocks" else "image"


# ------------------------------------------------------------- app state --
def parse_spec(s):
    """'3B6EA5', '1E3A5F-F0A868', or a scheme name -> list of rgb (None if invalid)."""
    try:
        return mt.parse_colors(s.strip())[:MAX_COLORS]
    except (ValueError, KeyError):
        return None


class State:
    def __init__(self, spec, pattern, zooms):
        self._args = (spec, pattern, dict(zooms))
        self.names = list(mt.PATTERNS)
        self.pi = self.names.index(pattern)
        self.set_colors(mt.parse_colors(spec))
        self.contrast = 1.0
        self.accents = "auto"
        self.zoom = dict(zooms)
        self.tiled = True
        self.preset = -1
        self.scheme = -1

    def reset(self):
        self.__init__(*self._args)

    def set_colors(self, rgbs):
        self.hls = [list(colorsys.rgb_to_hls(r / 255, g / 255, b / 255)) for r, g, b in rgbs[:MAX_COLORS]]
        self.active = 0

    def set_active_rgb(self, rgb):
        r, g, b = rgb
        self.hls[self.active] = list(colorsys.rgb_to_hls(r / 255, g / 255, b / 255))

    def add_slot(self):
        if len(self.hls) >= MAX_COLORS:
            return
        h, l, s = self.hls[-1]
        self.hls.append([(h + 0.09) % 1.0, 0.68 if l < 0.5 else 0.32, max(0.35, s)])
        self.active = len(self.hls) - 1

    @property
    def rgbs(self):
        out = []
        for h, l, s in self.hls:
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            out.append((round(r * 255), round(g * 255), round(b * 255)))
        return out

    @property
    def key(self):
        return "-".join(mt.rgb_to_hex(c) for c in self.rgbs)

    @property
    def pattern(self):
        return self.names[self.pi]


class TileCache:
    """Re-renders the 256px tile only when colours/pattern/contrast change."""

    def __init__(self):
        self.key = None
        self.tile = None
        self._s = {}

    def get(self, st):
        key = (tuple(st.rgbs), st.pattern, round(st.contrast, 3), st.accents)
        if key != self.key:
            self.tile = mt.render(st.rgbs, st.pattern, st.contrast, st.accents)
            self.key = key
            self._s.clear()
        return self.tile

    def _img(self, st, ts):
        tile = self.get(st)
        if ts == mt.SIZE:
            return tile
        return tile.resize((ts, ts), Image.LANCZOS if ts < mt.SIZE else Image.BICUBIC)

    def rgb(self, st, ts):
        self.get(st)
        k = ("rgb", ts)
        if k not in self._s:
            self._s[k] = np.asarray(self._img(st, ts))
        return self._s[k]

    def indexed(self, st, ts):
        """(index array, flat palette) with index 255 reserved for the backdrop."""
        self.get(st)
        k = ("p", ts)
        if k not in self._s:
            q = gfx.quantize(self._img(st, ts), 255)
            pal = list(q.getpalette() or [])[:765]
            pal += [0] * (765 - len(pal)) + list(BG_FILL)
            self._s[k] = (np.asarray(q), pal)
        return self._s[k]


# ------------------------------------------------------------- rendering --
def make_color_fns(truecolor):
    """-> (fmt(top, bottom), fg(c), bg(c)) ANSI colour helpers."""
    if truecolor:
        def fmt(t, b):
            return (f"{ESC}[38;2;{t[0]};{t[1]};{t[2]};"
                    f"48;2;{b[0]};{b[1]};{b[2]}m")

        def fg(c):
            return f"{ESC}[38;2;{c[0]};{c[1]};{c[2]}m"

        def bg(c):
            return f"{ESC}[48;2;{c[0]};{c[1]};{c[2]}m"
        return fmt, fg, bg

    memo = {}

    def idx(c):
        key = tuple(c)
        v = memo.get(key)
        if v is None:
            r, g, b = key
            if max(key) - min(key) < 10:           # greys -> 24-step ramp
                avg = (r + g + b) / 3
                if avg < 8:
                    v = 16
                elif avg > 238:
                    v = 231
                else:
                    v = 232 + min(23, round((avg - 8) / 10))
            else:                                  # 6x6x6 colour cube
                v = 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)
            memo[key] = v
        return v

    def fmt(t, b):
        return f"{ESC}[38;5;{idx(t)};48;5;{idx(b)}m"

    def fg(c):
        return f"{ESC}[38;5;{idx(c)}m"

    def bg(c):
        return f"{ESC}[48;5;{idx(c)}m"
    return fmt, fg, bg


def compose(t, tiled, w, h, fill):
    """Tile array `t` (ts x ts [x 3]) repeated to a w x h canvas, or one centred tile."""
    ts = t.shape[0]
    if tiled:
        reps = (-(-h // ts), -(-w // ts)) + (1,) * (t.ndim - 2)
        return np.tile(t, reps)[:h, :w]
    canvas = np.empty((h, w) + t.shape[2:], t.dtype)
    canvas[...] = fill
    x0, y0 = (w - ts) // 2, (h - ts) // 2
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(w, x0 + ts), min(h, y0 + ts)
    if dx1 > dx0 and dy1 > dy0:
        canvas[dy0:dy1, dx0:dx1] = t[dy0 - y0:dy1 - y0, dx0 - x0:dx1 - x0]
    return canvas


def row_ansi(top, bot, fmt):
    parts, pt, pb = [], None, None
    for t, b in zip(top, bot):
        if t != pt or b != pb:
            parts.append(fmt(t, b))
            pt, pb = t, b
        parts.append("▀")
    parts.append(f"{ESC}[0m")
    return "".join(parts)


def pad(text, cols):
    return text[:cols].ljust(cols)


def auto_zoom(cols, rows):
    """Block-mode default: one full tile fits the sample height (and half the width)."""
    h_px = max(2, (rows - RESERVED) * 2)
    z = min(h_px, cols / 2) / mt.SIZE
    return round(min(1.0, max(ZOOM_MIN * 2, z)), 2)


class App:
    def __init__(self, st, truecolor, protocol="auto", cell=None, zoom_cli=None):
        self.st = st
        self.fmt, self.fg, self.bg = make_color_fns(truecolor)
        self.cache = TileCache()
        self.protocol, self.cell_arg, self.zoom_cli = protocol, cell, zoom_cli
        self.cell = cell or DEFAULT_CELL
        self.modes, self.mode = ["blocks"], "blocks"
        self.msg = ""
        self.last_size = None
        self.drawn_mode = None
        self.img_key = None

    # -- mode / terminal setup -------------------------------------------
    def setup(self, term):
        proto = gfx.detect(term, self.protocol)
        self.modes = [proto, "blocks"] if proto else ["blocks"]
        self.mode = "blocks" if self.protocol == "blocks" else self.modes[0]
        if proto:
            self.cell = self.cell_arg or gfx.cell_px(1) or gfx.query_cell_px(term.write, term.read_raw) or DEFAULT_CELL
        if self.zoom_cli:
            self.st.zoom[zkey(self.mode)] = self.zoom_cli

    # -- pieces of the screen --------------------------------------------
    def status_line(self, cols):
        st = self.st
        base = f"{ESC}[0m{self.bg((34, 36, 41))}{self.fg((220, 222, 226))}"
        lead = f" {st.pattern} ({st.pi + 1}/{len(st.names)}) "
        sw, sw_len = "", 0
        for n, c in enumerate(st.rgbs):
            lb, rb = ("[", "]") if n == st.active else (" ", " ")
            sw += f"{lb}{self.fg(c)}██{self.fg((220, 222, 226))}{rb}"
            sw_len += 4
        h, l, s = st.hls[st.active]
        z = st.zoom[zkey(self.mode)]
        # most important first: the line is cut at the terminal width
        tail = (f" [{self.mode}] {z:.2f}x {'tiled' if st.tiled else 'single'}  contrast {st.contrast:.1f}"
                f"  slot {st.active + 1}/{len(st.hls)} H{h * 360:3.0f} S{s * 100:3.0f} L{l * 100:3.0f}"
                + (f"  accents {st.accents}" if len(st.hls) == 1 and st.accents != "auto" else "")
                + f"  #{st.key}")
        room = max(0, cols - len(lead) - sw_len)
        return base + lead + sw + pad(tail, room) + f"{ESC}[0m"

    def help_lines(self, cols, prompt):
        h1 = " Left/Right pattern  Up/Down contrast  Tab/1-4 colour slot  n add  d delete"
        h2 = " h/H hue  s/S sat  l/L light  c/C preset  m/M scheme  x hex (or A-B-C palette)"
        h3 = " +/- zoom  t tiled/single  a accents  g image/blocks  w save PNG  r reset  q quit"
        if prompt is not None:
            h3 = prompt
        elif self.msg:
            h3 = " " + self.msg
        style3 = f"{ESC}[1m" if (prompt is not None or self.msg) else f"{ESC}[2m"
        return [f"{ESC}[2m{pad(h1, cols)}{ESC}[0m", f"{ESC}[2m{pad(h2, cols)}{ESC}[0m",
                f"{style3}{pad(h3, cols)}{ESC}[0m"]

    def block_rows(self, cols, area):
        st = self.st
        ts = max(8, round(mt.SIZE * st.zoom["blocks"]))
        img = compose(self.cache.rgb(st, ts), st.tiled, cols, area * 2, BG_FILL)
        top, bot = img[0::2].tolist(), img[1::2].tolist()
        return [row_ansi(top[y], bot[y], self.fmt) for y in range(area)]

    def image_payload(self, cols, area):
        """Escape sequence that draws the sample as a real image. -> (seq, cells_w, cells_h)"""
        st, proto = self.st, self.mode
        cw, ch = self.cell
        max_w, max_h = PX_LIMIT[proto]
        W = min(cols * cw, (max_w // cw) * cw)
        H = min(area * ch, (max_h // ch) * ch)
        if proto == "sixel":
            H -= H % 6
        ccols, crows = max(1, W // cw), max(1, H // ch)
        ts = max(8, round(mt.SIZE * st.zoom["image"]))
        if proto == "sixel":
            idx, pal = self.cache.indexed(st, ts)
            im = Image.fromarray(compose(idx, st.tiled, W, H, 255))
            im.putpalette(pal)
            return gfx.encode_sixel(im, period_x=ts if st.tiled else None), ccols, crows
        im = Image.fromarray(compose(self.cache.rgb(st, ts), st.tiled, W, H, BG_FILL))
        return gfx.encode(im, proto, ccols, crows, IMAGE_ID), ccols, crows

    # -- drawing -----------------------------------------------------------
    def frame(self, cols, rows, prompt=None):
        """Full-screen update as one string (absolute cursor addressing)."""
        out = []
        area = rows - RESERVED
        if cols < 30 or area < 2:
            return f"{ESC}[2J{ESC}[1;1Hterminal too small"
        size = (cols, rows)
        if size != self.last_size or self.mode != self.drawn_mode:
            if self.drawn_mode == "kitty":
                out.append(gfx.delete_kitty(IMAGE_ID))
            out.append(f"{ESC}[2J")
            self.last_size, self.drawn_mode, self.img_key = size, self.mode, None
        out.append(f"{ESC}[1;1H{self.status_line(cols)}")
        if self.mode == "blocks":
            out += [f"{ESC}[{y + 2};1H{ln}" for y, ln in enumerate(self.block_rows(cols, area))]
        else:
            self.cache.get(self.st)
            key = (self.cache.key, self.st.zoom["image"], self.st.tiled, cols, area, self.mode, self.cell)
            if key != self.img_key:
                try:
                    seq, _, _ = self.image_payload(cols, area)
                except Exception as e:                       # fall back rather than crash
                    self.msg = f"image mode failed ({e}); using blocks"
                    self.mode = "blocks"
                    return self.frame(cols, rows, prompt)
                if self.mode == "kitty":
                    out.append(gfx.delete_kitty(IMAGE_ID))
                out.append(f"{ESC}[2;1H{seq}")
                self.img_key = key
        for i, ln in enumerate(self.help_lines(cols, prompt)):
            out.append(f"{ESC}[{area + 2 + i};1H{ln}")
        return "".join(out)

    def draw(self, prompt=None):
        size = shutil.get_terminal_size((80, 24))
        sys.stdout.write(self.frame(size.columns, size.lines, prompt))
        sys.stdout.flush()

    # -- input ---------------------------------------------------------------
    def ask_hex(self, term):
        buf = ""
        allowed = string.hexdigits + "-,#"
        while True:
            self.draw(f" hex colour(s): {buf}_    (Enter = apply, Esc = cancel)")
            for k in term.keys(0.5):
                if k in ("\r", "\n"):
                    return parse_spec(buf)
                if k == "esc":
                    return None
                if k in ("\x7f", "\x08"):
                    buf = buf[:-1]
                elif len(k) == 1 and k in allowed and len(buf) < 4 * 7:
                    buf += k

    def save(self):
        st = self.st
        name = f"{st.key}_{st.pattern}.png"
        self.cache.get(st).save(name, optimize=True)
        self.msg = f"saved {os.path.abspath(name)}"

    def handle(self, k, term):
        """Apply one key. Returns False to quit."""
        st = self.st
        presets = list(mt.PALETTE.values())
        schemes = list(mt.SCHEMES)
        z = zkey(self.mode)
        hl = st.hls[st.active]
        if k in ("q", "Q", "\x03"):
            return False
        elif k in ("left", "["):
            st.pi = (st.pi - 1) % len(st.names)
        elif k in ("right", "]"):
            st.pi = (st.pi + 1) % len(st.names)
        elif k == "up":
            st.contrast = min(3.0, round(st.contrast + 0.1, 2))
        elif k == "down":
            st.contrast = max(0.0, round(st.contrast - 0.1, 2))
        elif k == "\t":
            st.active = (st.active + 1) % len(st.hls)
        elif len(k) == 1 and k in "1234":
            want = int(k)
            while len(st.hls) < want and len(st.hls) < MAX_COLORS:
                st.add_slot()
            st.active = min(want - 1, len(st.hls) - 1)
        elif k == "n":
            if len(st.hls) < MAX_COLORS:
                st.add_slot()
            else:
                self.msg = f"palette is full ({MAX_COLORS} colours)"
        elif k == "d":
            if len(st.hls) > 1:
                del st.hls[st.active]
                st.active = min(st.active, len(st.hls) - 1)
            else:
                self.msg = "need at least one colour"
        elif k in ("h", "H"):
            hl[0] = (hl[0] + (5 / 360) * (1 if k == "H" else -1)) % 1.0
        elif k in ("s", "S"):
            hl[2] = min(1.0, max(0.0, hl[2] + 0.03 * (1 if k == "S" else -1)))
        elif k in ("l", "L"):
            hl[1] = min(1.0, max(0.0, hl[1] + 0.02 * (1 if k == "L" else -1)))
        elif k in ("c", "C"):
            st.preset = (st.preset + (1 if k == "c" else -1)) % len(presets)
            st.set_active_rgb(mt.hex_to_rgb(presets[st.preset]))
            self.msg = f"slot {st.active + 1}: {list(mt.PALETTE)[st.preset]}"
        elif k in ("m", "M"):
            st.scheme = (st.scheme + (1 if k == "m" else -1)) % len(schemes)
            st.set_colors(mt.parse_colors(mt.SCHEMES[schemes[st.scheme]]))
            self.msg = f"scheme: {schemes[st.scheme]}"
        elif k == "x":
            cols = self.ask_hex(term)
            if not cols:
                self.msg = "no colour applied"
            elif len(cols) == 1:
                st.set_active_rgb(cols[0])
            else:
                st.set_colors(cols)
        elif k in ("+", "="):
            st.zoom[z] = min(ZOOM_MAX, round(st.zoom[z] * 1.25, 3))
        elif k in ("-", "_"):
            st.zoom[z] = max(ZOOM_MIN, round(st.zoom[z] / 1.25, 3))
        elif k == "t":
            st.tiled = not st.tiled
        elif k == "a":
            modes = mt.ACCENT_MODES
            st.accents = modes[(modes.index(st.accents) + 1) % len(modes)]
            if len(st.hls) > 1:
                self.msg = "accents only affect single-colour palettes"
        elif k == "g":
            if len(self.modes) > 1:
                self.mode = self.modes[(self.modes.index(self.mode) + 1) % len(self.modes)]
                self.msg = f"render mode: {self.mode}"
            else:
                self.msg = "no image protocol detected - try --protocol kitty|iterm2|sixel"
        elif k == "w":
            self.save()
        elif k == "r":
            st.reset()
        return True

    def run(self):
        with Term() as term:
            self.setup(term)
            dirty = True
            try:
                while True:
                    size = shutil.get_terminal_size((80, 24))
                    if dirty or (size.columns, size.lines) != self.last_size:
                        self.draw()
                        dirty = False
                    for k in term.keys(0.25):
                        self.msg = ""
                        if not self.handle(k, term):
                            return
                        dirty = True
            finally:
                if self.mode == "kitty":
                    term.write(gfx.delete_kitty(IMAGE_ID))


# ----------------------------------------------------------------- input --
def parse_keys(s):
    names = {"A": "up", "B": "down", "C": "right", "D": "left"}
    keys, i = [], 0
    while i < len(s):
        c = s[i]
        if c == ESC:
            if i + 1 < len(s) and s[i + 1] in "[O":
                j = i + 2
                while j < len(s) and not ("@" <= s[j] <= "~"):
                    j += 1
                keys.append(names.get(s[j] if j < len(s) else "", "?"))
                i = j + 1
            else:
                keys.append("esc")
                i += 1
        else:
            keys.append(c)
            i += 1
    return keys


class Term(gfx.RawTerminal):
    """Alternate screen + keyboard input, restored on exit."""

    def __enter__(self):
        super().__enter__()
        self.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[2J")
        return self

    def __exit__(self, *exc):
        self.write(f"{ESC}[0m{ESC}[?25h{ESC}[?1049l")
        return super().__exit__(*exc)

    def keys(self, timeout):
        if IS_WIN:
            end = time.time() + timeout
            while True:
                out = []
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        ch2 = msvcrt.getwch()
                        out.append({"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, "?"))
                    elif ch == "\x03":
                        raise KeyboardInterrupt
                    else:
                        out.append(ch)
                if out:
                    return out
                if time.time() >= end:
                    return []
                time.sleep(0.01)
        data = self.read_raw(timeout)
        return parse_keys(data) if data else []


# ------------------------------------------------------------------ main --
def detect_truecolor(args):
    if args.truecolor:
        return True
    if args.colors256:
        return False
    return (IS_WIN or os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
            or "truecolor" in os.environ.get("TERM", "") or "24bit" in os.environ.get("TERM", ""))


def main():
    ap = argparse.ArgumentParser(description="Interactive terminal tile preview",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("colors", nargs="?", default="3B6EA5",
                    help="hex colour, dash-joined palette (bg first), or scheme name (default 3B6EA5)")
    ap.add_argument("pattern", nargs="?", default="diamonds", choices=list(mt.PATTERNS))
    ap.add_argument("--protocol", default="auto", choices=("auto", "blocks") + gfx.PROTOCOLS,
                    help="image protocol (default: auto-detect)")
    ap.add_argument("--cell", help="character cell size in pixels, WxH (default: ask the terminal)")
    ap.add_argument("--zoom", type=float,
                    help="initial zoom: 1.0 = real pixels in image mode, 1.0 = 256px tile in block mode")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--size", help="COLSxROWS for --once (default: terminal size)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--truecolor", action="store_true", help="force 24-bit colour in block mode")
    g.add_argument("--256", dest="colors256", action="store_true", help="force 256-colour block mode")
    args = ap.parse_args()

    spec = parse_spec(args.colors)
    if not spec:
        ap.error(f"not a hex colour, palette or scheme name: {args.colors}")
    cell = None
    if args.cell:
        cw, ch = (int(v) for v in args.cell.lower().split("x"))
        cell = (cw, ch)

    if args.once and args.size:
        cols, rows = (int(v) for v in args.size.lower().split("x"))
    else:
        sz = shutil.get_terminal_size((100, 30))
        cols, rows = sz.columns, sz.lines
    st = State(spec, args.pattern, {"blocks": auto_zoom(cols, rows), "image": 1.0})
    app = App(st, detect_truecolor(args), args.protocol, cell, args.zoom)

    if args.once:
        proto = None if args.protocol == "blocks" else (
            args.protocol if args.protocol != "auto" else gfx.env_hint())
        app.mode = app.drawn_mode = proto or "blocks"
        app.modes = [app.mode]
        app.last_size = (cols, rows)
        app.cell = cell or (gfx.cell_px(1) if sys.stdout.isatty() else None) or DEFAULT_CELL
        if args.zoom:
            st.zoom[zkey(app.mode)] = args.zoom
        print(app.status_line(cols))
        area = rows - RESERVED
        if app.mode == "blocks":
            print("\n".join(app.block_rows(cols, area)))
            print("\n".join(app.help_lines(cols, None)))
        else:
            seq, _, crows = app.image_payload(cols, area)
            sys.stdout.write(seq + ("\n" * crows if app.mode == "kitty" else "\n"))
            sys.stdout.flush()
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        ap.error("interactive mode needs a terminal (use --once to print a single frame)")

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    extra = ""
    if abs(st.contrast - 1.0) > 1e-9:
        extra += f" --contrast {st.contrast:g}"
    if st.accents != "auto" and len(st.hls) == 1:
        extra += f" --accents {st.accents}"
    print(f"python tileGen.py --colors {st.key} --patterns {st.pattern}{extra}")


if __name__ == "__main__":
    main()
