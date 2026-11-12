#!/usr/bin/env python3
"""termgfx - show real images inside a terminal (kitty, iTerm2 and sixel protocols).

Library use:
    import termgfx
    proto = termgfx.env_hint() or "sixel"          # or termgfx.detect(rt) with a RawTerminal
    sys.stdout.write(termgfx.encode(pil_image, proto, cols=40, rows=12)); sys.stdout.flush()

Command line (diagnostic): show an image file where the cursor is.
    python termgfx.py picture.png
    python termgfx.py picture.png --protocol sixel
    python termgfx.py --detect                     # report what this terminal supports

Protocols
    kitty   APC "ESC _ G ... ESC \\", PNG payload.   kitty, WezTerm, Ghostty, Konsole (subset)
    iterm2  OSC 1337 ; File=...                      iTerm2, WezTerm, mintty
    sixel   DCS q ...                                Konsole, Windows Terminal (1.22+), foot,
                                                     mlterm, xterm (-ti vt340), WezTerm, ...
Inside tmux/screen image escapes are swallowed unless passthrough is configured;
sixel works in tmux >= 3.4 built with sixel support.
"""
import argparse
import base64
import io
import os
import re
import struct
import sys
import time

import numpy as np
from PIL import Image

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
else:
    import fcntl
    import select
    import termios
    import tty

ESC = "\x1b"
PROTOCOLS = ("kitty", "iterm2", "sixel")


# ----------------------------------------------------------- raw terminal --
class RawTerminal:
    """cbreak-mode keyboard access (needed to read the terminal's replies)."""

    def __enter__(self):
        if IS_WIN:
            os.system("")        # enables ANSI escape handling on Windows 10+
        else:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if not IS_WIN:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        return False

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def read_raw(self, timeout):
        """Return whatever characters arrive within `timeout` seconds ('' if none)."""
        if IS_WIN:
            end, out = time.time() + timeout, ""
            while True:
                while msvcrt.kbhit():
                    out += msvcrt.getwch()
                if out or time.time() >= end:
                    return out
                time.sleep(0.01)
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return ""
        return os.read(self.fd, 65536).decode("utf-8", "ignore")


# -------------------------------------------------------------- detection --
def env_hint(env=None):
    """Best guess from environment variables alone (no terminal I/O)."""
    env = os.environ if env is None else env
    term, prog = env.get("TERM", ""), env.get("TERM_PROGRAM", "")
    if env.get("TMUX") or term.startswith("screen"):
        return None              # multiplexers swallow image escapes; let probe() decide
    if env.get("KONSOLE_VERSION"):
        return "sixel"           # Konsole: sixel is its most complete image path
    if (env.get("KITTY_WINDOW_ID") or "kitty" in term or "ghostty" in term.lower()
            or prog.lower() == "ghostty" or prog == "WezTerm" or env.get("WEZTERM_EXECUTABLE")):
        return "kitty"
    if prog in ("iTerm.app", "mintty") or env.get("ITERM_SESSION_ID"):
        return "iterm2"
    if env.get("WT_SESSION"):
        return "sixel"           # Windows Terminal 1.22+
    if term.startswith(("foot", "mlterm", "contour")) or "sixel" in term:
        return "sixel"
    return None


def probe(write, read, timeout=0.8):
    """Ask the terminal what it supports. Returns 'kitty', 'sixel' or None.

    write(str) sends text to the terminal; read(seconds) returns raw reply
    characters ('' on timeout). Sends a kitty graphics query followed by DA1
    (primary device attributes); every terminal answers DA1, so its arrival
    marks the end of the replies.
    """
    write(f"{ESC}_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA{ESC}\\{ESC}[c")
    buf, end = "", time.time() + timeout
    while time.time() < end:
        buf += read(0.1)
        if re.search(r"\x1b\[\?[\d;]*c", buf):
            break
    if "_Gi=31;OK" in buf:
        return "kitty"
    m = re.search(r"\x1b\[\?([\d;]*)c", buf)
    if m and "4" in m.group(1).split(";"):
        return "sixel"
    return None


def detect(rt=None, override="auto"):
    """Pick a protocol: explicit override > environment hint > active probe."""
    if override and override != "auto":
        return None if override == "blocks" else override
    hint = env_hint()
    if hint:
        return hint
    if rt is not None and not IS_WIN:
        return probe(rt.write, rt.read_raw)
    return None


def cell_px(fd=1):
    """(width, height) of one character cell in pixels from the tty, or None."""
    if IS_WIN:
        return None
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols and xp and yp:
            return max(1, xp // cols), max(1, yp // rows)
    except Exception:
        pass
    return None


def query_cell_px(write, read, timeout=0.4):
    """Ask the terminal for its cell size (CSI 16 t). Returns (w, h) or None."""
    write(f"{ESC}[16t")
    buf, end = "", time.time() + timeout
    while time.time() < end:
        buf += read(0.1)
        m = re.search(r"\x1b\[6;(\d+);(\d+)t", buf)
        if m:
            return int(m.group(2)), int(m.group(1))
    return None


# --------------------------------------------------------------- encoders --
def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)       # fast; pattern tiles compress well anyway
    return buf.getvalue()


def delete_kitty(image_id=1):
    return f"{ESC}_Ga=d,d=I,i={image_id},q=2{ESC}\\"


def encode_kitty(img, cols=None, rows=None, image_id=1):
    """Kitty graphics: transmit + display at the cursor (cursor does not move)."""
    data = base64.standard_b64encode(_png(img.convert("RGB"))).decode()
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    ctl = f"a=T,f=100,t=d,i={image_id},q=2,C=1"
    if cols:
        ctl += f",c={cols}"
    if rows:
        ctl += f",r={rows}"
    out = []
    for n, chunk in enumerate(chunks):
        more = 1 if n < len(chunks) - 1 else 0
        head = f"{ctl},m={more}" if n == 0 else f"m={more}"
        out.append(f"{ESC}_G{head};{chunk}{ESC}\\")
    return "".join(out)


def encode_iterm2(img, cols=None, rows=None):
    """iTerm2 inline image (OSC 1337). cols/rows are in character cells."""
    raw = _png(img.convert("RGB"))
    args = ["inline=1", f"size={len(raw)}", "preserveAspectRatio=0"]
    if cols:
        args.append(f"width={cols}")
    if rows:
        args.append(f"height={rows}")
    return f"{ESC}]1337;File={';'.join(args)}:{base64.standard_b64encode(raw).decode()}\x07"


def quantize(img, colors=256):
    """RGB -> palette image without dithering (pattern tiles have few colours)."""
    return img.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT,
                                       dither=Image.Dither.NONE)


def _rle(a):
    """Sixel run-length encoding of a uint8 array of sixel characters."""
    n = len(a)
    change = np.flatnonzero(a[1:] != a[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [n]))
    parts = []
    for st, en, v in zip(starts.tolist(), ends.tolist(), a[starts].tolist()):
        run, ch = en - st, chr(v)
        parts.append(f"!{run}{ch}" if run > 3 else ch * run)
    return "".join(parts)


def encode_sixel(img, colors=256, period_x=None):
    """Sixel image. `img` may be an RGB image (quantised here) or a 'P' image.

    period_x: if the image repeats horizontally every `period_x` pixels (a tiled
    background), the caller can say so and only one period is encoded and then
    repeated - several times faster for wide images.
    """
    if img.mode != "P":
        img = quantize(img, colors)
    w, h = img.size
    idx = np.asarray(img).astype(np.int16)
    pal = list(img.getpalette() or [])
    pal += [0] * (768 - len(pal))
    pad = (-h) % 6
    if pad:                                           # replicate the last row up to a band edge
        idx = np.concatenate([idx, np.repeat(idx[-1:], pad, axis=0)])
    used = np.unique(idx)
    out = [f"{ESC}P0;0;0q", f'"1;1;{w};{h}']
    for c in used.tolist():
        r, g, b = pal[3 * c:3 * c + 3]
        out.append(f"#{c};2;{round(r * 100 / 255)};{round(g * 100 / 255)};{round(b * 100 / 255)}")
    p = period_x if period_x and 0 < period_x < w else w
    reps, rem = divmod(w, p)
    weights = (1 << np.arange(6)).astype(np.int16)[:, None]
    for top in range(0, idx.shape[0], 6):
        sub = idx[top:top + 6]
        first = True
        for c in np.unique(sub[:, :p]).tolist():
            vals = ((sub[:, :p] == c) * weights).sum(axis=0)
            chars = (vals + 63).astype(np.uint8)
            body = _rle(chars) * reps + (_rle(chars[:rem]) if rem else "")
            out.append(("" if first else "$") + f"#{c}" + body)
            first = False
        out.append("-")
    out[-1] = f"{ESC}\\"                              # replace the final band break with ST
    return "".join(out)


def encode(img, protocol, cols=None, rows=None, image_id=1, period_x=None):
    if protocol == "kitty":
        return encode_kitty(img, cols, rows, image_id)
    if protocol == "iterm2":
        return encode_iterm2(img, cols, rows)
    if protocol == "sixel":
        return encode_sixel(img, period_x=period_x)
    raise ValueError(f"unknown protocol {protocol!r}")


# -------------------------------------------------------------------- CLI --
def main():
    import shutil
    ap = argparse.ArgumentParser(description="Show an image in the terminal (kitty / iTerm2 / sixel)")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--protocol", default="auto", choices=("auto",) + PROTOCOLS)
    ap.add_argument("--detect", action="store_true", help="report the detected protocol and exit")
    a = ap.parse_args()
    if not a.file and not a.detect:
        ap.error("give an image file, or use --detect")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        ap.error("needs a terminal")
    with RawTerminal() as rt:
        proto = detect(rt, a.protocol)
        cell = cell_px(1) or query_cell_px(rt.write, rt.read_raw) or (9, 18)
    if a.detect:
        print(f"protocol: {proto or 'none (use half-block fallback)'}   cell: {cell[0]}x{cell[1]}px"
              f"   hint: {env_hint()}")
        return
    if not proto:
        sys.exit("no image protocol detected; force one with --protocol kitty|iterm2|sixel")
    img = Image.open(a.file).convert("RGB")
    tcols = shutil.get_terminal_size().columns
    cols = min(tcols, -(-img.width // cell[0]))
    rows = -(-img.height // cell[1])
    if cols * cell[0] < img.width:                      # too wide: scale down to the window
        img = img.resize((cols * cell[0], round(img.height * cols * cell[0] / img.width)), Image.LANCZOS)
        rows = -(-img.height // cell[1])
    sys.stdout.write(encode(img, proto, cols, rows) + "\n" * (rows if proto == "kitty" else 1))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
