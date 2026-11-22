"""Render the Fair Winds banner and logo as pixel art.

Writes three PNGs into the folder given (default: this one):

  banner.png     1280 x 640  GitHub social preview and README hero
  logo.png       1120 x 320  wordmark with the balloon mark, for the web page
  logo-mark.png   512 x 512  the balloon mark alone

Everything is drawn on a small canvas and scaled up with hard edges, the way
the game draws with half blocks. The palette and the dusk sky follow
docs/design.md. No dependencies beyond the standard library; it writes the
PNGs itself, and the same run always gives the same bytes.

usage: python art/render_art.py [output folder]
"""
import math
import os
import struct
import sys
import zlib

IVORY, GOLD = (250, 240, 214), (255, 196, 110)
TEAL, CREAM, CORAL = (44, 172, 172), (246, 232, 202), (240, 112, 92)
NIGHT = (11, 16, 30)
SKY = [(14, 14, 46), (40, 28, 92), (112, 50, 112), (212, 96, 98), (250, 160, 86), (255, 222, 144)]

FONT = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01111 10000 10000 10000 10000 10000 01111", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01111 10000 10000 10011 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "11111 00100 00100 00100 00100 00100 11111", "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111", "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001", "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "W": "10001 10001 10001 10101 10101 11011 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", ".": "00000 00000 00000 00000 00000 00000 00100",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def ramp(stops, t):
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(len(stops) - 2, int(t))
    return mix(stops[i], stops[i + 1], t - i)


def smooth(v, a, b):
    t = min(max((v - a) / (b - a), 0.0), 1.0)
    return t * t * (3 - 2 * t)


def hash2(ix, iy, seed):
    h = (ix * 374761393 + iy * 668265263 + seed * 974711) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 4294967296


def vnoise(x, y, seed):
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    u, v = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a, b = hash2(ix, iy, seed), hash2(ix + 1, iy, seed)
    c, d = hash2(ix, iy + 1, seed), hash2(ix + 1, iy + 1, seed)
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v


def fbm(x, y, octaves, seed):
    total, amp, freq, norm = 0.0, 0.5, 1.0, 0.0
    for i in range(octaves):
        total += amp * vnoise(x * freq, y * freq, seed + i * 17)
        norm += amp
        amp *= 0.5
        freq *= 2.03
    return total / norm


class Canvas:
    def __init__(self, w, h, fill):
        self.w, self.h = w, h
        self.px = [[fill for _ in range(w)] for _ in range(h)]

    def put(self, x, y, c):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = c

    def blend(self, x, y, c, a):
        x, y = int(math.floor(x)), int(math.floor(y))
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = mix(self.px[y][x], c, a)

    def text(self, x, y, s, scale, color, shadow=None, bottom=None):
        """5x7 lettering; `bottom` grades the fill toward a second colour."""
        off = max(1, scale // 3)
        for layer in ("shadow", "fill"):
            if layer == "shadow" and shadow is None:
                continue
            cx = x
            for ch in s:
                for ry, row in enumerate(FONT[ch].split()):
                    for rx, bit in enumerate(row):
                        if bit != "1":
                            continue
                        for dy in range(scale):
                            for dx in range(scale):
                                if layer == "shadow":
                                    self.put(cx + rx * scale + dx + off, y + ry * scale + dy + off, shadow)
                                else:
                                    c = mix(color, bottom, (ry * scale + dy) / (7 * scale)) if bottom else color
                                    self.put(cx + rx * scale + dx, y + ry * scale + dy, c)
                cx += 6 * scale
        return x + 6 * scale * len(s)

    def save(self, path, scale):
        raw = bytearray()
        for row in self.px:
            line = bytearray()
            for c in row:
                line += bytes(max(0, min(255, int(round(v)))) for v in c) * scale
            for _ in range(scale):
                raw += b"\x00" + line

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w * scale, self.h * scale, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
        with open(path, "wb") as f:
            f.write(png)


def balloon(cv, cx, cy, r, lit=1.0, glow=True):
    """A striped envelope lit from the right, with burner, ropes and basket."""
    tail = r * 0.5
    for y in range(int(cy - r), int(cy + r + tail) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            dx, dy = x - cx, y - cy
            hw = r * math.sqrt(max(0.0, 1 - (dy / r) ** 2)) if dy <= 0 else r * (1 - (dy / (r + tail)) ** 1.7)
            if hw < 0.8 or abs(dx) > hw + 0.3:
                continue
            u = max(-1.0, min(1.0, dx / max(hw, 1)))
            gore = int((math.asin(u) / math.pi + 0.5) * 7.999)
            col = (TEAL, CREAM, CORAL, CREAM)[gore % 4]
            lum = (0.56 + 0.4 * u + 0.1 * (dy / r)) * lit
            c = (col[0] * lum * 1.05, col[1] * lum * 0.92, col[2] * lum * 0.94)
            c = mix(c, (60, 36, 90), min(0.6, max(0.0, 0.5 - u * 0.6)) * 0.5)
            if glow and dy > r * 0.8:
                c = mix(c, (255, 196, 120), min(1.0, (dy - r * 0.8) / (r * 0.7)) * 0.6)
            cv.put(x, y, c)
    by = int(cy + r + tail) + 1
    bw = max(1, int(r / 6))
    rope = max(2, int(r / 5))
    if glow:
        cv.put(cx, by, (255, 226, 140))
        cv.blend(cx - 1, by, (255, 190, 110), 0.5)
        cv.blend(cx + 1, by, (255, 190, 110), 0.5)
    for i in range(1, rope):
        cv.put(cx - bw, by + i, (58, 46, 70))
        cv.put(cx + bw, by + i, (58, 46, 70))
    for y in range(by + rope, by + 2 * rope):
        for x in range(int(cx - bw), int(cx + bw) + 1):
            cv.put(x, y, (112, 76, 50) if y == by + rope else (86, 58, 40))


def wind(cv, x0, y0, length, rise, strength=0.8, back=False):
    """One streak of wind: a shallow curve that fades toward its tail."""
    for i in range(length):
        t = i / length
        y = y0 + rise * math.sin(t * math.pi * 0.9) + 1.2 * math.sin(t * 9)
        cv.blend(x0 - i if back else x0 + i, y, (244, 248, 255), strength * t * t)


def dusk(cv, horizon, sun_x, seed=7):
    w, h = cv.w, cv.h
    sun_y = horizon - int(h * 0.1)
    for y in range(h):
        for x in range(w):
            t = min(1.0, y / horizon)
            c = ramp(SKY, t ** 1.15)
            d = math.hypot(x - sun_x, (y - sun_y) * 1.1)
            g = math.exp(-d / (w * 0.085)) * 0.5
            c = (c[0] + 255 * g, c[1] + 196 * g, c[2] + 120 * g)
            if y < horizon * 0.5 and hash2(x, y, 3) < 0.006:
                c = mix(c, (255, 255, 255), (1 - t * 1.8) * 0.7)
            if d < w * 0.03:
                c = (255, 246, 206)
            n = fbm(x / (w * 0.16), y / (h * 0.07), 4, seed)
            a = smooth(n, 0.56, 0.72) * smooth(y, h * 0.06, h * 0.18) * (1 - smooth(y, horizon - h * 0.1, horizon))
            lit = min(1.0, max(0.0, 1.15 - (horizon - y) / horizon * 1.25 - abs(x - sun_x) / (w * 1.2)))
            cv.px[y][x] = mix(c, mix((66, 40, 92), (255, 172, 112), lit), a * 0.86)


def ridge(cv, scale, row, seed, top, amp, color, haze):
    for x in range(cv.w):
        ty = top - amp * fbm(x / scale, row, 4, seed)
        for y in range(max(0, math.ceil(ty)), cv.h):
            cv.px[y][x] = mix(color, haze, min(1.0, (y - ty) / (cv.h * 0.3)) * 0.35)


def render_banner():
    w, h, hz = 320, 160, 112
    cv = Canvas(w, h, NIGHT)
    dusk(cv, hz, 288)
    ridge(cv, 70, 0.5, 91, hz - 6, 30, (118, 70, 122), (230, 130, 112))
    ridge(cv, 48, 1.5, 47, hz + 4, 22, (70, 44, 98), (150, 84, 110))
    for x in range(w):
        ty = hz + 22 - 14 * fbm(x / 40, 2.5, 4, 63)
        for y in range(math.ceil(ty), h):
            cv.px[y][x] = mix((34, 26, 60), (12, 12, 26), min(1.0, (y - ty) / 16))
    # layers of wind, each going its own way
    wind(cv, 150, 20, 150, -5, strength=0.95)
    wind(cv, 166, 33, 110, -4, strength=0.55)
    wind(cv, 312, 52, 80, 3, strength=0.6, back=True)
    wind(cv, 118, 78, 120, 5, strength=0.7)
    wind(cv, 190, 98, 110, 3, strength=0.5)
    balloon(cv, 236, 58, 30)
    balloon(cv, 186, 34, 6, lit=0.9, glow=False)
    cv.text(16, 28, "FAIR", 5, IVORY, shadow=(40, 22, 70), bottom=GOLD)
    cv.text(16, 70, "WINDS", 5, IVORY, shadow=(40, 22, 70), bottom=GOLD)
    cv.text(16, 134, "READ THE WIND. CARRY THE POST. FINISH THE MAP.", 1, IVORY)
    cv.text(16, 146, "A DOOR GAME FOR NETBBS", 1, GOLD)
    return cv, 4


def mark(cv, cx, cy, r):
    """The balloon in a roundel of dusk sky, crossed by one streak of wind."""
    for y in range(int(cy - r), int(cy + r) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            if math.hypot(x - cx, y - cy) <= r - 1.5:
                cv.put(x, y, ramp(SKY, 0.1 + (y - (cy - r)) / (2 * r) * 0.9))
    wind(cv, int(cx - r * 0.8), cy + r * 0.36, int(r * 1.6), -r * 0.14, strength=0.85)
    balloon(cv, cx, cy - r * 0.22, r * 0.44)
    for y in range(cv.h):  # trim what crossed the ring, then draw the ring
        for x in range(int(cx - r) - 3, int(cx + r) + 4):
            d = math.hypot(x - cx, y - cy)
            if d > r:
                cv.put(x, y, NIGHT)
            elif d > r - 1.5:
                cv.put(x, y, GOLD)


def render_logo():
    cv = Canvas(280, 80, NIGHT)
    mark(cv, 40, 40, 32.5)
    cv.text(86, 20, "FAIR WINDS", 3, IVORY, shadow=(40, 26, 72), bottom=GOLD)
    cv.text(87, 52, "READ THE WIND. CARRY THE POST.", 1, (150, 164, 190))
    return cv, 4


def render_mark():
    cv = Canvas(128, 128, NIGHT)
    mark(cv, 64, 64, 56.5)
    return cv, 4


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    for name, fn in (("banner.png", render_banner), ("logo.png", render_logo), ("logo-mark.png", render_mark)):
        canvas, scale = fn()
        canvas.save(os.path.join(out, name), scale)
        print(name, canvas.w * scale, "x", canvas.h * scale)
