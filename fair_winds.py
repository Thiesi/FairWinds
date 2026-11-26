#!/usr/bin/env python3
"""Fair Winds: a cooperative, turn-based balloon navigation door game.

One file, standard library only, Python 3.11 or newer. Runs as a native stdio
door under NetBBS (launch details in the JSON named by NETBBS_DOOR_INFO), or
standalone in a terminal for development.

This build reaches slice S05 of docs/design.md: the host adapter and terminal
lifecycle, the world generator with its terrain cache, the weather, the turn
engine, and the plain flight: the whole turn on an 80 x 24 text screen, to
find out whether it is fun before anything is drawn.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import math
import os
import queue
import shutil
import signal
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
import weakref
import zoneinfo
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0.dev29"
DOOR_API = 5
MIN_WIDTH, MIN_HEIGHT = 80, 24
MAX_DIMENSION = 1000
# NetBBS gives a bundled door's guest call an identity no account can have.
GUEST_ID_FLOOR = 2 ** 62
# Leave before the host's wall-clock limit, so the host never has to stop us.
DEADLINE_GRACE = 2.0
DEADLINE_WARNINGS = (300.0, 60.0)
GEOMETRY_POLL = 1.0
ESCAPE_TIMEOUT = 0.05
IDLE_TIMEOUT = 0.25
DIAGNOSTIC_LIMIT = 300

COLOR_DEPTHS = ("truecolor", "256", "16")
CHARSETS = ("utf-8", "cp437", "ascii")


# --------------------------------------------------------------------------
# Launch: one host-neutral record. Nothing after start-up knows which host
# it is on (docs/systems.md, section 13).
# --------------------------------------------------------------------------

class LaunchError(Exception):
    """The host supplied launch details the door cannot use."""


@dataclass
class Launch:
    host: str                     # "netbbs" or "standalone"
    handle: str
    user_id: int | None
    node_id: str | None
    node_name: str
    width: int
    height: int
    color_depth: str
    charset: str
    quadrants: bool
    unicode_style: bool
    transport: str
    timezone: str
    datetime_format: str
    session_limit: float | None   # seconds of wall clock for this launch
    outbound: dict | None
    guest: bool
    info_path: Path | None = None


def clean_text(value: str, limit: int) -> str:
    """Printable text only: no control characters, so no escape sequences."""
    kept = "".join(ch for ch in value if ch.isprintable())
    return " ".join(kept.split())[:limit]


def _field(info: dict, name: str, kind, default):
    """A documented optional field: absent means unknown, a wrong type is an error."""
    if name not in info or info[name] is None:
        return default
    value = info[name]
    if isinstance(value, bool) and kind is not bool:
        raise LaunchError(f"'{name}' has the wrong type")
    if not isinstance(value, kind):
        raise LaunchError(f"'{name}' has the wrong type")
    return value


def _dimension(info: dict, name: str, default: int) -> int:
    value = _field(info, name, int, default)
    if not 1 <= value <= MAX_DIMENSION:
        raise LaunchError(f"'{name}' is out of range")
    return value


def _guest_ids(environ) -> set[int]:
    ids = set()
    for part in environ.get("DOOR_FW_GUEST_IDS", "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def parse_door_info(text: str, environ, info_path: Path | None = None) -> Launch:
    """NetBBS's door_info.json into a launch record."""
    try:
        info = json.loads(text)
    except ValueError:
        raise LaunchError("the launch file is not valid JSON") from None
    if not isinstance(info, dict):
        raise LaunchError("the launch file is not a JSON object")
    api = info.get("door_api")
    if isinstance(api, bool) or not isinstance(api, int):
        raise LaunchError("the launch file has no usable 'door_api'")
    if api != DOOR_API:
        raise LaunchError(f"door_api {api} is not supported; this door needs door_api {DOOR_API}")

    user_id = _field(info, "user_id", int, None)
    if user_id is not None and user_id < 0:
        raise LaunchError("'user_id' is out of range")
    node_id = _field(info, "node_id", str, None)
    if node_id is not None:
        node_id = clean_text(node_id, 128) or None
    color_depth = _field(info, "color_depth", str, "16")
    if color_depth not in COLOR_DEPTHS:
        raise LaunchError("'color_depth' is not one of truecolor, 256, 16")
    charset = _field(info, "charset", str, "utf-8")
    if charset not in CHARSETS:
        raise LaunchError("'charset' is not one of utf-8, cp437, ascii")
    limit = _field(info, "session_limit_seconds", (int, float), None)
    if limit is not None and limit <= 0:
        raise LaunchError("'session_limit_seconds' is out of range")
    guest = (user_id is None or node_id is None or user_id >= GUEST_ID_FLOOR
             or user_id in _guest_ids(environ))
    return Launch(
        host="netbbs",
        handle=clean_text(_field(info, "handle", str, ""), 32) or "Pilot",
        user_id=user_id,
        node_id=node_id,
        node_name=clean_text(_field(info, "node_name", str, ""), 48) or "the BBS",
        width=_dimension(info, "terminal_width", MIN_WIDTH),
        height=_dimension(info, "terminal_height", MIN_HEIGHT),
        color_depth=color_depth,
        charset=charset,
        quadrants=_field(info, "quadrants", bool, False),
        unicode_style=_field(info, "unicode_style", bool, True),
        transport=clean_text(_field(info, "transport", str, "unknown"), 16) or "unknown",
        timezone=clean_text(_field(info, "timezone", str, "UTC"), 64) or "UTC",
        datetime_format=_field(info, "datetime_format", str, "%d.%m.%Y %H:%M"),
        session_limit=float(limit) if limit is not None else None,
        outbound=_field(info, "outbound", dict, None),
        guest=guest,
        info_path=info_path,
    )


def standalone_launch(environ) -> Launch:
    """No host: a development launch in whatever terminal this is."""
    size = shutil.get_terminal_size((MIN_WIDTH, MIN_HEIGHT))
    truecolor = environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    return Launch(
        host="standalone",
        handle=clean_text(environ.get("USER") or environ.get("USERNAME") or "", 32) or "Pilot",
        user_id=1, node_id="standalone", node_name="the shell",
        width=max(1, min(MAX_DIMENSION, size.columns)), height=max(1, min(MAX_DIMENSION, size.lines)),
        color_depth="truecolor" if truecolor else "256", charset="utf-8",
        quadrants=False, unicode_style=True, transport="local", timezone="UTC",
        datetime_format="%d.%m.%Y %H:%M", session_limit=None, outbound=None, guest=False)


def load_launch(environ) -> Launch:
    """The launch record for this run.

    A launch file that was supplied but cannot be used is an error, never a
    quiet fall back to standalone play.
    """
    if "NETBBS_DOOR_INFO" not in environ:
        return standalone_launch(environ)
    name = environ["NETBBS_DOOR_INFO"]
    if not name:
        raise LaunchError("NETBBS_DOOR_INFO is empty")
    path = Path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise LaunchError("the launch file cannot be read") from None
    return parse_door_info(text, environ, path)


def current_geometry(launch: Launch) -> tuple[int, int]:
    """The caller's size now. A file caught mid-change keeps the last size."""
    if launch.host == "standalone":
        size = shutil.get_terminal_size((launch.width, launch.height))
        return max(1, min(MAX_DIMENSION, size.columns)), max(1, min(MAX_DIMENSION, size.lines))
    try:
        info = json.loads(launch.info_path.read_text(encoding="utf-8"))
        width, height = info.get("terminal_width"), info.get("terminal_height")
        if all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_DIMENSION
               for v in (width, height)):
            return width, height
    except (OSError, ValueError, AttributeError):
        pass
    return launch.width, launch.height


def layout_tier(width: int, height: int) -> str:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return "undersize"
    if width >= 168 and height >= 46:
        return "wide"
    if width >= 132 and height >= 40:
        return "medium"
    return "small"


# --------------------------------------------------------------------------
# Determinism: every draw is keyed (docs/systems.md, section 14). Nothing in
# the game calls an unseeded generator.
# --------------------------------------------------------------------------

def world_seed(node_id: str, generation: int = 0) -> int:
    """The 64-bit seed of a node's world."""
    digest = hashlib.sha256(f"fair-winds/1/{node_id}/{generation}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def draw(seed: int, purpose: str, *keys) -> int:
    """A 64-bit number fixed by the seed, a purpose and its keys."""
    text = "/".join([str(seed), purpose, *(str(k) for k in keys)])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def draw_unit(seed: int, purpose: str, *keys) -> float:
    """The same draw as a number from 0 up to, not including, 1."""
    return (draw(seed, purpose, *keys) >> 11) / 9007199254740992.0


# --------------------------------------------------------------------------
# The world: one continent per seed (docs/systems.md, section 3).
#
# Terrain uses only +, -, *, / and comparisons on doubles, never the maths
# library, so every platform bakes byte-identical tiles.
# --------------------------------------------------------------------------

WORLD_W, WORLD_H = 1600.0, 1000.0      # km
SAMPLE_KM = 2.5                        # one terrain sample
TILE_N = 32                            # samples along a tile's side
TILE_KM = SAMPLE_KM * TILE_N           # 80 km
TILES_X, TILES_Y = 20, 13              # the last row runs past the south edge
CHART_KM = 10.0                        # one chart cell
CHART_W, CHART_H = 160, 100
BAKE_VERSION = 1                       # change it and every cache is rebuilt

SEA_DEEP, SEA_SHALLOW, SHORE, GRASS, FOREST, DRY, HILLS, MOUNTAIN, SNOW = range(9)
BIOME_NAMES = ("deep sea", "shallow sea", "shore", "grassland", "forest", "dry plain", "hills",
               "high mountain", "snow")
LAND_TARGET = (0.41, 0.49)             # share of the world that is land
TOWN_SPACING = 8                       # chart cells: 80 km

_NAME_FIRST = ("Aster", "Brine", "Cinder", "Dun", "Kettle", "Hollow", "Salt", "Lee", "Marrow", "Thistle",
               "Ember", "Gull", "Harrow", "Wren", "Tarn", "Quill", "Morrow", "Fen", "Alder", "Rook",
               "Sorrel", "Vane", "Pike", "Cobble", "Larch", "Heron", "Flint", "Mallow", "Osier", "Tansy")
_NAME_LAST = ("wick", "mere", "ford", "haven", "stead", "holm", "by", "gate", "moor", "reach",
              " Rock", " Tarn", " Cross", " Landing", " Hythe")


def _lattice(seed32: int, octave: int, ix: int, iy: int) -> float:
    h = (ix * 0x9E3779B1 + iy * 0x85EBCA77 + seed32 * 0xC2B2AE3D + octave * 0x27D4EB2F) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    h = (h * 0x297A2D39) & 0xFFFFFFFF
    h ^= h >> 15
    return h / 4294967296.0


def _axis(start: float, step: float, count: int, cell: float):
    """Lattice index and smoothed weight for each sample along one axis."""
    out = []
    for i in range(count):
        f = (start + i * step) / cell
        ix = int(f // 1)
        t = f - ix
        out.append((ix, t * t * (3.0 - 2.0 * t)))
    return out


def fbm_grid(seed32: int, scale: float, octaves: int, x0: float, y0: float, step: float, nx: int, ny: int):
    """Value-noise fBm over a grid of samples, each 0 to 1, row by row.

    The lattice is hashed once per octave for the whole grid, which is what
    makes baking a tile cheap.
    """
    out = [0.0] * (nx * ny)
    amp, norm, cell = 0.5, 0.0, scale
    for octave in range(octaves):
        cols, rows = _axis(x0, step, nx, cell), _axis(y0, step, ny, cell)
        lo, hi = cols[0][0], cols[-1][0] + 1
        cache: dict[int, list[float]] = {}
        for j, (iy, v) in enumerate(rows):
            for row in (iy, iy + 1):
                if row not in cache:
                    cache[row] = [_lattice(seed32, octave, ix, row) for ix in range(lo, hi + 1)]
            top, bottom = cache[iy], cache[iy + 1]
            line = [top[k] + (bottom[k] - top[k]) * v for k in range(hi - lo + 1)]
            base = j * nx
            for i, (ix, u) in enumerate(cols):
                a = line[ix - lo]
                out[base + i] += amp * (a + (line[ix - lo + 1] - a) * u)
        norm += amp
        amp *= 0.5
        cell *= 0.5
    return [v / norm for v in out]


def _smooth(v: float, a: float, b: float) -> float:
    t = (v - a) / (b - a)
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


def metres(e: float) -> int:
    """Shaped height to metres: most land is low, and only ridges are high."""
    if e >= 0.0:
        return min(4500, round(4000.0 * e * e))
    return max(-3000, round(e * 5000.0))


def biome_of(m: int, moisture: float) -> int:
    if m < -200:
        return SEA_DEEP
    if m < 0:
        return SEA_SHALLOW
    if m < 30:
        return SHORE
    if m >= 2800:
        return SNOW
    if m >= 1500:
        return MOUNTAIN
    if m >= 500:
        return HILLS
    if moisture < 0.42:
        return DRY
    if moisture > 0.58:
        return FOREST
    return GRASS


@dataclass(frozen=True)
class Tile:
    """32 by 32 terrain samples: metres and biome, row by row."""
    tx: int
    ty: int
    elevation: tuple
    biome: bytes


class TerrainCacheError(Exception):
    """A cached file is damaged, stale or for another world."""


class World:
    """A node's continent: shape, settlements and features, all from one seed.

    Terrain is baked into tiles on first need and cached, in memory and, when
    a folder is given, on disk. A cached file is checked before it is trusted
    and rebuilt if it fails. Nothing here is evaluated per frame.
    """

    def __init__(self, seed: int, cache_dir: Path | None = None):
        self.seed = seed
        self._seeds = {name: draw(seed, "noise", name) & 0xFFFFFFFF
                       for name in ("shape", "ridge", "detail", "moisture")}
        self.cx = 720.0 + 160.0 * draw_unit(seed, "shape", "cx")
        self.cy = 460.0 + 80.0 * draw_unit(seed, "shape", "cy")
        self.rx = 700.0 + 100.0 * draw_unit(seed, "shape", "rx")
        self.ry = 430.0 + 60.0 * draw_unit(seed, "shape", "ry")
        self.cache_dir = None
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir) / f"terrain-{seed:016x}-b{BAKE_VERSION}"
        self._tiles: dict[tuple[int, int], Tile] = {}
        self._load_index()

    # -- shape ---------------------------------------------------------------

    def _raw(self, x0, y0, step, nx, ny):
        """Shaped height before the sea level is set, and moisture."""
        s = self._seeds
        shape = fbm_grid(s["shape"], 260.0, 5, x0, y0, step, nx, ny)
        ridge = fbm_grid(s["ridge"], 170.0, 4, x0, y0, step, nx, ny)
        detail = fbm_grid(s["detail"], 42.0, 3, x0, y0, step, nx, ny)
        moisture = fbm_grid(s["moisture"], 300.0, 3, x0, y0, step, nx, ny)
        out = []
        for j in range(ny):
            dy = (y0 + j * step - self.cy) / self.ry
            for i in range(nx):
                k = j * nx + i
                dx = (x0 + i * step - self.cx) / self.rx
                d2 = dx * dx + dy * dy
                e = (shape[k] - 0.5) * 1.3 + 0.36 - d2 * 0.75
                if d2 > 0.8:
                    e -= (d2 - 0.8) * 3.0          # the world's edge is always sea
                r = 2.0 * ridge[k] - 1.0
                r = 1.0 - (r if r >= 0.0 else -r)
                inland = _smooth(e, 0.02, 0.3)
                e += (r * r - 0.5) * 0.7 * inland + (detail[k] - 0.5) * 0.16 * inland
                out.append(e)
        return out, moisture

    def _grid(self, x0, y0, step, nx, ny):
        raw, moisture = self._raw(x0, y0, step, nx, ny)
        heights = [metres(e - self.sea_level) for e in raw]
        return heights, bytes(biome_of(m, moisture[k]) for k, m in enumerate(heights))

    # -- the index: sea level, chart grid, settlements, features ---------------

    def _load_index(self) -> None:
        if self.cache_dir is not None:
            try:
                self._read_index()
                return
            except (OSError, ValueError, KeyError, TypeError, TerrainCacheError):
                pass
        self._build_index()
        self._write_index()

    def _build_index(self) -> None:
        half = CHART_KM / 2.0
        raw, moisture = self._raw(half, half, CHART_KM, CHART_W, CHART_H)
        target = LAND_TARGET[0] + (LAND_TARGET[1] - LAND_TARGET[0]) * draw_unit(self.seed, "shape", "land")
        ordered = sorted(raw)
        self.sea_level = ordered[min(len(ordered) - 1, int((1.0 - target) * len(ordered)))]
        self.chart_elevation = [metres(e - self.sea_level) for e in raw]
        self.chart_biome = bytes(biome_of(m, moisture[k]) for k, m in enumerate(self.chart_elevation))
        self._settle()

    def _components(self, member):
        """Connected groups of chart cells for which member(index) holds."""
        seen, groups = set(), []
        for start in range(CHART_W * CHART_H):
            if start in seen or not member(start):
                continue
            group, stack = [], [start]
            seen.add(start)
            while stack:
                k = stack.pop()
                group.append(k)
                x, y = k % CHART_W, k // CHART_W
                for nx_, ny_ in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx_ < CHART_W and 0 <= ny_ < CHART_H:
                        n = ny_ * CHART_W + nx_
                        if n not in seen and member(n):
                            seen.add(n)
                            stack.append(n)
            group.sort()
            groups.append(group)
        return groups

    def _settle(self) -> None:
        biome, elevation, seed = self.chart_biome, self.chart_elevation, self.seed
        cells = CHART_W * CHART_H
        land = sorted(self._components(lambda k: biome[k] >= SHORE), key=lambda g: (-len(g), g[0]))
        water = self._components(lambda k: biome[k] < SHORE)
        edge = lambda k: k % CHART_W in (0, CHART_W - 1) or k // CHART_W in (0, CHART_H - 1)
        ocean = set()
        lakes = []
        for group in water:
            if any(edge(k) for k in group):
                ocean.update(group)
            else:
                lakes.append(group)
        main = set(land[0]) if land else set()
        self.land_cells = sum(len(g) for g in land)

        def dist2(a, b):
            return (a % CHART_W - b % CHART_W) ** 2 + (a // CHART_W - b // CHART_W) ** 2

        def neighbours(k):
            x, y = k % CHART_W, k // CHART_W
            return [ny_ * CHART_W + nx_ for nx_, ny_ in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
                    if 0 <= nx_ < CHART_W and 0 <= ny_ < CHART_H]

        # Towns: ports first, then inland, never closer than 80 km, all on the mainland.
        buildable = [k for k in sorted(main) if biome[k] in (SHORE, GRASS, FOREST, DRY)]
        order = sorted(buildable, key=lambda k: draw(seed, "town", k))
        ports = [k for k in order if any(n in ocean for n in neighbours(k))]
        want = 14 + draw(seed, "town-count") % 5
        chosen: list[int] = []

        def take(candidates, limit, spacing):
            for k in candidates:
                if len(chosen) >= limit:
                    return
                if all(dist2(k, c) >= spacing * spacing for c in chosen):
                    chosen.append(k)

        take(ports, min(6, want), TOWN_SPACING)
        port_cells = set(chosen)
        take(order, want, TOWN_SPACING)
        if len(chosen) < 14:                       # a cramped continent: use hills too
            take(sorted((k for k in main if biome[k] == HILLS), key=lambda k: draw(seed, "town", k)), 14, TOWN_SPACING)
        used: set[str] = set()
        self.towns = []
        for k in chosen:
            self.towns.append({"name": self._name(used, k, k in port_cells), "cell": k, "port": k in port_cells,
                               "x": (k % CHART_W) * CHART_KM + 5.0, "y": (k // CHART_W) * CHART_KM + 5.0})
        self.start_town = 0
        start = chosen[0] if chosen else 0

        # Three weather stations near the start town, so nobody begins without a forecast.
        self.stations = []
        near = sorted((k for k in main if 16 <= dist2(k, start) <= 144 and biome[k] != SNOW),
                      key=lambda k: draw(seed, "station", k))
        picked: list[int] = []
        for k in near:
            if len(picked) == 3:
                break
            if all(dist2(k, c) >= 9 for c in chosen) and all(dist2(k, p) >= 16 for p in picked):
                picked.append(k)
        for k in picked:
            self.stations.append({"name": self._name(used, k, False, station=True), "cell": k,
                                  "x": (k % CHART_W) * CHART_KM + 5.0, "y": (k // CHART_W) * CHART_KM + 5.0})

        # The Gyre: inland, far from the start town, as far from every town as the land allows.
        far = [k for k in sorted(main) if dist2(k, start) >= 900] or sorted(main) or [0]
        gyre = max(far, key=lambda k: (min((dist2(k, c) for c in chosen), default=0), -k))
        self.gyre = {"cell": gyre, "x": (gyre % CHART_W) * CHART_KM + 5.0, "y": (gyre // CHART_W) * CHART_KM + 5.0}

        # Features: what callers will chart and name.
        fixed = []
        for group in sorted(lakes, key=lambda g: (-len(g), g[0]))[:20]:
            fixed.append(("lake", group))
        for group in land[1:16]:
            fixed.append(("island", group))
        peaks = []
        for k in range(cells):
            if elevation[k] < 1500:
                continue
            x, y = k % CHART_W, k // CHART_W
            around = [elevation[ny_ * CHART_W + nx_] for nx_ in (x - 1, x, x + 1) for ny_ in (y - 1, y, y + 1)
                      if (nx_, ny_) != (x, y) and 0 <= nx_ < CHART_W and 0 <= ny_ < CHART_H]
            if all(elevation[k] > a for a in around):
                peaks.append(k)
        peaks.sort(key=lambda k: (-elevation[k], k))
        kept: list[int] = []
        for k in peaks:
            if len(kept) == 40:
                break
            if all(dist2(k, p) >= 9 for p in kept):
                kept.append(k)
        fixed += [("peak", [k]) for k in kept]
        kinds = {SHORE: "plain", GRASS: "plain", FOREST: "forest", DRY: "dry plain", HILLS: "hills",
                 MOUNTAIN: "range", SNOW: "range"}
        pieces = []
        for kind in ("plain", "forest", "dry plain", "hills", "range"):
            for by in range(0, CHART_H, 8):
                for bx in range(0, CHART_W, 8):
                    inside = lambda k: (bx <= k % CHART_W < bx + 8 and by <= k // CHART_W < by + 8
                                        and kinds.get(biome[k]) == kind)
                    block = [k for y in range(by, min(by + 8, CHART_H)) for x in range(bx, min(bx + 8, CHART_W))
                             for k in (y * CHART_W + x,) if inside(k)]
                    if not block:
                        continue
                    members = set(block)
                    seen: set[int] = set()
                    for first in block:
                        if first in seen:
                            continue
                        group, stack = [], [first]
                        seen.add(first)
                        while stack:
                            k = stack.pop()
                            group.append(k)
                            for n in neighbours(k):
                                if n in members and n not in seen:
                                    seen.add(n)
                                    stack.append(n)
                        group.sort()
                        pieces.append((kind, group))
        pieces.sort(key=lambda p: (-len(p[1]), p[0], p[1][0]))
        count = 200 + draw(seed, "feature-count") % 61
        features = (fixed + pieces)[:count]
        self.features = [{"id": i, "kind": kind, "cells": group} for i, (kind, group) in enumerate(features)]

    def _name(self, used: set, cell: int, port: bool, station: bool = False) -> str:
        for attempt in range(200):
            first = _NAME_FIRST[draw(self.seed, "name", cell, attempt, 0) % len(_NAME_FIRST)]
            if station:
                last = (" Station", " Cairn", " Post")[draw(self.seed, "name", cell, attempt, 1) % 3]
                name = first + last
            elif port and draw(self.seed, "name", cell, attempt, 2) % 2:
                name = "Port " + first
            else:
                name = first + _NAME_LAST[draw(self.seed, "name", cell, attempt, 1) % len(_NAME_LAST)]
            if name not in used:
                used.add(name)
                return name
        name = f"Waypoint {cell}"
        used.add(name)
        return name

    @property
    def land_fraction(self) -> float:
        return self.land_cells / (CHART_W * CHART_H)

    # -- the cache -------------------------------------------------------------

    @staticmethod
    def _seal(payload: bytes) -> bytes:
        return payload + hashlib.sha256(payload).digest()[:8]

    @staticmethod
    def _unseal(data: bytes) -> bytes:
        if len(data) < 8 or hashlib.sha256(data[:-8]).digest()[:8] != data[-8:]:
            raise TerrainCacheError("checksum")
        return data[:-8]

    def _store(self, name: str, payload: bytes) -> None:
        """Write a cache file whole or not at all. A cache that cannot be written is only slower."""
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temp = self.cache_dir / f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
            temp.write_bytes(self._seal(payload))
            temp.replace(self.cache_dir / name)
        except OSError:
            pass

    def _write_index(self) -> None:
        index = {"seed": self.seed, "bake": BAKE_VERSION, "sea_level": self.sea_level,
                 "land_cells": self.land_cells, "towns": self.towns, "start_town": self.start_town,
                 "stations": self.stations, "gyre": self.gyre, "features": self.features,
                 "chart_elevation": self.chart_elevation, "chart_biome": list(self.chart_biome)}
        self._store("index.json", json.dumps(index, separators=(",", ":")).encode("utf-8"))

    def _read_index(self) -> None:
        index = json.loads(self._unseal((self.cache_dir / "index.json").read_bytes()).decode("utf-8"))
        if index["seed"] != self.seed or index["bake"] != BAKE_VERSION:
            raise TerrainCacheError("another world")
        cells = CHART_W * CHART_H
        if len(index["chart_elevation"]) != cells or len(index["chart_biome"]) != cells:
            raise TerrainCacheError("size")
        self.sea_level = float(index["sea_level"])
        self.land_cells = int(index["land_cells"])
        self.towns, self.start_town = index["towns"], index["start_town"]
        self.stations, self.gyre, self.features = index["stations"], index["gyre"], index["features"]
        self.chart_elevation = index["chart_elevation"]
        self.chart_biome = bytes(index["chart_biome"])

    _TILE_HEAD = struct.Struct("<4sHQhh")

    def bake_tile(self, tx: int, ty: int) -> Tile:
        half = SAMPLE_KM / 2.0
        heights, biome = self._grid(tx * TILE_KM + half, ty * TILE_KM + half, SAMPLE_KM, TILE_N, TILE_N)
        return Tile(tx, ty, tuple(heights), biome)

    def _tile_bytes(self, tile: Tile) -> bytes:
        return (self._TILE_HEAD.pack(b"FWT1", BAKE_VERSION, self.seed, tile.tx, tile.ty)
                + struct.pack(f"<{TILE_N * TILE_N}h", *tile.elevation) + tile.biome)

    def _read_tile(self, tx: int, ty: int) -> Tile:
        data = self._unseal((self.cache_dir / f"t{tx:02d}_{ty:02d}.bin").read_bytes())
        head, count = self._TILE_HEAD.size, TILE_N * TILE_N
        if len(data) != head + count * 3:
            raise TerrainCacheError("size")
        if self._TILE_HEAD.unpack_from(data) != (b"FWT1", BAKE_VERSION, self.seed, tx, ty):
            raise TerrainCacheError("another tile")
        biome = data[head + count * 2:]
        if max(biome) > SNOW:
            raise TerrainCacheError("biome")
        return Tile(tx, ty, struct.unpack_from(f"<{count}h", data, head), bytes(biome))

    def tile(self, tx: int, ty: int) -> Tile:
        """A tile from memory, else from disk if it checks out, else freshly baked."""
        if not (0 <= tx < TILES_X and 0 <= ty < TILES_Y):
            raise IndexError("tile outside the world")
        key = (tx, ty)
        found = self._tiles.get(key)
        if found is None:
            if self.cache_dir is not None:
                try:
                    found = self._read_tile(tx, ty)
                except (OSError, struct.error, ValueError, TerrainCacheError):
                    found = None
            if found is None:
                found = self.bake_tile(tx, ty)
                self._store(f"t{tx:02d}_{ty:02d}.bin", self._tile_bytes(found))
            self._tiles[key] = found
        return found

    def sample(self, x: float, y: float) -> tuple[int, int]:
        """Metres and biome at a place, from the nearest terrain sample. Off the world is deep sea."""
        if not (0.0 <= x < WORLD_W and 0.0 <= y < WORLD_H):
            return -3000, SEA_DEEP
        sx, sy = int(x / SAMPLE_KM), int(y / SAMPLE_KM)
        tile = self.tile(sx // TILE_N, sy // TILE_N)
        k = (sy % TILE_N) * TILE_N + sx % TILE_N
        return tile.elevation[k], tile.biome[k]


def world_map(world: World, width: int = 80) -> str:
    """The whole continent as characters, for a look at a seed from the command line."""
    glyphs = " .:-\"',^A*"
    step = CHART_W // width if width < CHART_W else 1
    marks = {t["cell"]: "#" if i == world.start_town else "o" for i, t in enumerate(world.towns)}
    marks.update({s["cell"]: "+" for s in world.stations})
    marks[world.gyre["cell"]] = "@"
    lines = []
    for y in range(0, CHART_H, step * 2):
        row = []
        for x in range(0, CHART_W, step):
            mark = next((marks[yy * CHART_W + xx] for yy in range(y, min(CHART_H, y + step * 2))
                         for xx in range(x, x + step) if yy * CHART_W + xx in marks), None)
            row.append(mark or glyphs[world.chart_biome[y * CHART_W + x]])
        lines.append("".join(row).rstrip())
    lines.append("")
    lines.append(f"seed {world.seed:016x}   land {world.land_fraction:.0%}   towns {len(world.towns)}   "
                 f"features {len(world.features)}   # start town   o town   + station   @ the Gyre")
    lines.append("towns: " + ", ".join(t["name"] for t in world.towns))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Weather: a pure function of the seed, the world day and the minute
# (docs/systems.md, section 4). Nothing is stored and nothing runs between
# calls. Like the terrain it avoids the maths library, apart from sqrt,
# which every platform rounds the same way.
# --------------------------------------------------------------------------

LAYERS = 6
LAYER_NAMES = ("Surface", "Low", "Mid", "Upper", "High", "Jet")
LAYER_BLOCK_M = (500, 1500, 2750, None, None, None)   # terrain this high fills the layer
BLOCKED_FACTOR = 0.15
WIND_BASE = (9.0, 24.0, 36.0, 48.0, 68.0, 105.0)      # prevailing speed, km/h
WIND_CAP = (35.0, 60.0, 80.0, 100.0, 140.0, 200.0)    # never exceeded
WIND_NOISE = (5.0, 8.0, 10.0, 11.0, 13.0, 15.0)
SYSTEM_REACH = (0.35, 0.6, 0.85, 1.0, 1.15, 1.3)      # how much of a system each layer feels
SEASON_DAYS = 28
SYSTEM_SLOT_DAYS = 1.5
GYRE_RADIUS, GYRE_SPEED = 150.0, 110.0
# The Gyre's bands, by distance from its centre (S23). Out to the outer band a forecast is worth half at most;
# inside the inner band nothing is forecast and nothing can be planted; the wall is crossed only from the
# wall's layer up; and inside the wall is the eye.
GYRE_OUTER_KM, GYRE_INNER_KM, GYRE_WALL_KM, GYRE_EYE_KM = 300.0, 150.0, 60.0, 30.0
GYRE_OUTER_COVER = 0.5               # the most a forecast is worth in the outer band
GYRE_WALL_LAYER = 5                  # the lowest layer that gets through the wall
GYRE_WEAR = 4                        # what the wall does to a balloon it throws back
EYE_PAY, EYE_POINTS = (50, 200), (30, 100)       # for reaching the eye; and for being the first on the node to
MINUTES_PER_DAY = 1440
TURN_MINUTES = 15


def _unit(tau: float) -> tuple[float, float]:
    """A unit vector from the tangent of half its angle: no trigonometry needed."""
    d = 1.0 + tau * tau
    return (1.0 - tau * tau) / d, 2.0 * tau / d


def _bump(v: float, start: float, peak: float, end: float) -> float:
    """0 at start, 1 at peak, 0 at end, smooth in between."""
    if v <= start or v >= end:
        return 0.0
    return _smooth(v, start, peak) if v < peak else 1.0 - _smooth(v, peak, end)


def _noise(seed32: int, channel: int, x: float, y: float) -> float:
    """One octave of value noise at a point, 0 to 1."""
    ix, iy = int(x // 1), int(y // 1)
    fx, fy = x - ix, y - iy
    u, v = fx * fx * (3.0 - 2.0 * fx), fy * fy * (3.0 - 2.0 * fy)
    a, b = _lattice(seed32, channel, ix, iy), _lattice(seed32, channel, ix + 1, iy)
    c, d = _lattice(seed32, channel, ix, iy + 1), _lattice(seed32, channel, ix + 1, iy + 1)
    top = a + (b - a) * u
    return top + (c + (d - c) * u - top) * v


def lead_error_km(lead_minutes: float) -> float:
    """How far a forecast may misplace a system, with no station in range."""
    return min(400.0, max(0.0, lead_minutes) * (400.0 / MINUTES_PER_DAY))


class WeatherFrame:
    """The weather of one moment: everything that does not depend on the place.

    Built once per turn; sampling it at a place is then cheap.
    """

    __slots__ = ("weather", "day", "minute", "hour", "systems", "base", "noise_know", "drift",
                 "sea_breeze", "land_breeze", "katabatic", "fog_hour", "icing_layer")

    def __init__(self, weather: "Weather", day: int, minute: int, lead: float, coverage: float):
        self.weather, self.day, self.minute = weather, day, minute
        self.hour = hour = minute / 60.0
        t = day + minute / MINUTES_PER_DAY
        blur = lead_error_km(lead) * (1.0 - coverage)
        self.systems = weather._systems(t, blur)
        self.base = weather._regime(day, minute)
        # A forecast knows less and less of the small eddies as it looks ahead.
        self.noise_know = 1.0 - _smooth(lead, 0.0, 180.0) * (1.0 - coverage)
        self.drift = t * 90.0
        self.sea_breeze = _bump(hour, 11.0, 15.0, 19.0)
        night = hour + 24.0 if hour < 12.0 else hour
        self.land_breeze = _bump(night, 22.0, 27.0, 30.0)
        self.katabatic = _bump(hour, 3.5, 6.0, 8.5)
        self.fog_hour = _bump(hour, 4.5, 6.5, 8.5)
        self.icing_layer = weather._icing(day)

    def wind(self, x: float, y: float, layer: int) -> tuple[float, float]:
        """Where the air at a place and layer is going, in km/h east and south."""
        w = self.weather
        li = layer - 1
        tau, speed, sign = self.base[li]
        tau += 0.25 * (y / WORLD_H - 0.5)
        d = 1.0 + tau * tau
        vx = sign * speed * (1.0 - tau * tau) / d
        vy = sign * speed * 2.0 * tau / d
        reach = SYSTEM_REACH[li]
        for sx, sy, inv_r, strength, turn in self.systems:
            dx, dy = (x - sx) * inv_r, (y - sy) * inv_r
            q2 = dx * dx + dy * dy
            if q2 > 6.25:
                continue
            g = strength * reach * 2.0 / (1.0 + q2 * q2 * q2)
            vx += g * (turn * dy - 0.25 * turn * dx)
            vy += g * (-turn * dx - 0.25 * turn * dy)
        amp = WIND_NOISE[li] * self.noise_know
        if amp:
            nx, ny = (x + self.drift) / 140.0, y / 140.0
            vx += amp * (2.0 * _noise(w.noise_seed, li * 2, nx, ny) - 1.0)
            vy += amp * (2.0 * _noise(w.noise_seed, li * 2 + 1, nx, ny) - 1.0)
        cell = w.cell(x, y)
        if cell is not None:
            if li < 2:
                breeze = (14.0 * self.sea_breeze - 5.0 * self.land_breeze) * (1.0 if li == 0 else 0.5)
                if breeze:
                    cx, cy = w.coast[cell]
                    vx += breeze * cx
                    vy += breeze * cy
                if li == 0 and self.katabatic:
                    sx_, sy_ = w.slope[cell]
                    vx += 12.0 * self.katabatic * sx_
                    vy += 12.0 * self.katabatic * sy_
            if li < 3:
                gap = w.gap[cell]
                if gap:
                    vx *= 1.6
                    vy *= 1.6
                if w.world.chart_elevation[cell] >= LAYER_BLOCK_M[li]:
                    vx *= BLOCKED_FACTOR
                    vy *= BLOCKED_FACTOR
        cap = WIND_CAP[li]
        s2 = vx * vx + vy * vy
        if s2 > cap * cap:
            k = cap / math.sqrt(s2)
            vx *= k
            vy *= k
        return vx, vy

    def surface_speed(self, x: float, y: float) -> float:
        vx, vy = self.wind(x, y, 1)
        return math.sqrt(vx * vx + vy * vy)

    def pressure(self, x: float, y: float) -> float:
        """Sea-level pressure in hPa, from the same systems that turn the wind."""
        p = 1013.0
        for sx, sy, inv_r, strength, turn in self.systems:
            dx, dy = (x - sx) * inv_r, (y - sy) * inv_r
            f = 1.0 + dx * dx + dy * dy
            p -= turn * strength * 0.9 / (f * f)
        return p

    def _low(self, x: float, y: float) -> tuple[float, float]:
        """How strongly a low is felt here, and how near its squall ring this is."""
        near = ring = 0.0
        for sx, sy, inv_r, strength, turn in self.systems:
            if turn < 0:
                continue
            dx, dy = (x - sx) * inv_r, (y - sy) * inv_r
            q2 = dx * dx + dy * dy
            f = 1.0 + q2
            felt = min(1.0, strength / 20.0) / (f * f)
            near = max(near, felt)
            # Only a strong low has squalls, in a narrow ring about two thirds of a radius out.
            force = (strength - 14.0) / 10.0
            force = 0.0 if force < 0.0 else 1.0 if force > 1.0 else force
            off = (q2 - 0.45) / 0.2
            ring = max(ring, force * (1.0 - min(1.0, off if off >= 0 else -off)))
        return near, ring

    def cloud(self, x: float, y: float) -> tuple[float, int]:
        """Cloud cover from 0 to 1, and the layer its deck sits in."""
        near, _ = self._low(x, y)
        patch = _noise(self.weather.noise_seed, 40, (x + self.drift) / 180.0, y / 180.0)
        cover = near * 0.9 + patch * 0.9 - 0.35
        return (0.0 if cover < 0.0 else 1.0 if cover > 1.0 else cover), (3 if near > 0.3 else 4)

    def squall(self, x: float, y: float) -> float:
        """Squall strength from 0 to 1: a broken ring around each low."""
        _, ring = self._low(x, y)
        if ring <= 0.0:
            return 0.0
        return ring * _smooth(_noise(self.weather.noise_seed, 41, (x + self.drift) / 45.0, y / 45.0), 0.5, 0.75)

    def fog(self, x: float, y: float) -> float:
        """Dawn fog from 0 to 1: calm lowland only."""
        if not self.fog_hour:
            return 0.0
        cell = self.weather.cell(x, y)
        if cell is None or not 0 <= self.weather.world.chart_elevation[cell] < 250:
            return 0.0
        calm = 1.0 - _smooth(self.surface_speed(x, y), 6.0, 14.0)
        patch = _smooth(_noise(self.weather.noise_seed, 42, x / 60.0, y / 60.0 + self.day * 0.37), 0.5, 0.7)
        return self.fog_hour * calm * patch


class Weather:
    """The weather of one world.

    `frame(day, minute)` is the truth. `frame(day, minute, lead, coverage)`
    is what a forecast made `lead` minutes earlier says, with `coverage`
    from 0 (no station in range) to 1 (at a station).
    """

    FRAMES_KEPT = 64

    def __init__(self, world: World):
        self.world = world
        self.seed = world.seed
        self.noise_seed = draw(world.seed, "noise", "weather") & 0xFFFFFFFF
        self._frames: dict[tuple, WeatherFrame] = {}
        self._seasons: dict[int, tuple] = {}
        self._slots: dict[int, tuple | None] = {}
        self._chart_fields()

    # -- fields worked out once from the chart ------------------------------

    def cell(self, x: float, y: float) -> int | None:
        if not (0.0 <= x < WORLD_W and 0.0 <= y < WORLD_H):
            return None
        return int(y / CHART_KM) * CHART_W + int(x / CHART_KM)

    def _chart_fields(self) -> None:
        w, h = CHART_W, CHART_H
        elevation, biome = self.world.chart_elevation, self.world.chart_biome
        land = [1.0 if b >= SHORE else 0.0 for b in biome]
        # A wide blur of land and sea: its slope points inland, and it is
        # steepest at the coast.
        rows = [0.0] * (w * h)
        for y in range(h):
            for x in range(w):
                total = 0.0
                for xx in range(x - 2, x + 3):
                    total += land[y * w + (0 if xx < 0 else w - 1 if xx >= w else xx)]
                rows[y * w + x] = total
        blur = [0.0] * (w * h)
        for y in range(h):
            for x in range(w):
                total = 0.0
                for yy in range(y - 2, y + 3):
                    total += rows[(0 if yy < 0 else h - 1 if yy >= h else yy) * w + x]
                blur[y * w + x] = total / 25.0
        self.coast, self.slope, self.gap = [], [], bytearray(w * h)
        for y in range(h):
            for x in range(w):
                k = y * w + x
                xl, xr = max(0, x - 1), min(w - 1, x + 1)
                yu, yd = max(0, y - 1), min(h - 1, y + 1)
                gx, gy = blur[y * w + xr] - blur[y * w + xl], blur[yd * w + x] - blur[yu * w + x]
                size = math.sqrt(gx * gx + gy * gy)
                weight = 4.0 * blur[k] * (1.0 - blur[k])
                self.coast.append((gx / size * weight, gy / size * weight) if size > 1e-9 else (0.0, 0.0))
                hx = max(0, elevation[y * w + xr]) - max(0, elevation[y * w + xl])
                hy = max(0, elevation[yd * w + x]) - max(0, elevation[yu * w + x])
                steep = math.sqrt(hx * hx + hy * hy)
                pull = min(1.0, steep / 1200.0)
                self.slope.append((-hx / steep * pull, -hy / steep * pull) if steep > 1e-9 else (0.0, 0.0))
                if 0 <= elevation[k] < 500:
                    def high(dx, dy):
                        return any(0 <= x + dx * n < w and 0 <= y + dy * n < h
                                   and elevation[(y + dy * n) * w + x + dx * n] >= 700 for n in (1, 2, 3))
                    if (high(-1, 0) and high(1, 0)) or (high(0, -1) and high(0, 1)):
                        self.gap[k] = 1

    # -- the season's regime ---------------------------------------------------

    def _season(self, season: int) -> tuple:
        found = self._seasons.get(season)
        if found is None:
            seed = self.seed
            lean = (draw_unit(seed, "regime", season, "lean") - 0.5) * 0.6
            trades = draw_unit(seed, "regime", season, "trades") < 0.35
            layers = []
            for li in range(LAYERS):
                swing = 0.2 + 0.4 * draw_unit(seed, "regime", season, "swing", li)
                if li == LAYERS - 1:
                    tau = lean * 0.3
                else:
                    tau = lean + (swing if li % 2 == 0 else -swing)
                speed = WIND_BASE[li] * (0.8 + 0.4 * draw_unit(seed, "regime", season, "speed", li))
                layers.append((tau, speed, -1.0 if trades and li < 2 else 1.0))
            pulse = 0.85 + 0.3 * draw_unit(seed, "regime", season, "gyre")
            cold = draw_unit(seed, "regime", season, "cold") < 0.3
            found = self._seasons[season] = (tuple(layers), pulse, cold)
        return found

    def _blend(self, day: int, minute: int):
        """This season and the last, and how far the change between them has gone.

        A season's first day eases from the old regime to the new one, so
        the wind never jumps at midnight.
        """
        season = (day - 1) // SEASON_DAYS
        into = ((day - 1) % SEASON_DAYS) + minute / MINUTES_PER_DAY
        return self._season(season), self._season(season - 1), _smooth(into, 0.0, 1.0)

    def _regime(self, day: int, minute: int) -> tuple:
        now, before, w = self._blend(day, minute)
        hour = minute / 60.0
        veer = 0.12 * (_bump(hour, 6.0, 15.0, 24.0) - 0.5)       # winds veer a little through the day
        out = []
        for (tau, speed, sign), (tau0, speed0, sign0) in zip(now[0], before[0]):
            if sign != sign0:      # a layer turning round passes through calm, not through a jump
                speed_now = speed0 * (1.0 - 2.0 * w) if w < 0.5 else speed * (2.0 * w - 1.0)
                out.append(((tau0 if w < 0.5 else tau) + veer, speed_now, sign0 if w < 0.5 else sign))
            else:
                out.append((tau0 + (tau - tau0) * w + veer, speed0 + (speed - speed0) * w, sign))
        return tuple(out)

    def _icing(self, day: int) -> int:
        return 4 if self._season((day - 1) // SEASON_DAYS)[2] else 5

    # -- travelling highs and lows, and the Gyre ------------------------------

    def _slot(self, slot: int):
        if slot not in self._slots:
            seed = self.seed
            if draw_unit(seed, "system", slot, "exists") >= 0.85:
                self._slots[slot] = None
            else:
                low = draw_unit(seed, "system", slot, "kind") < 0.55
                self._slots[slot] = (
                    3.0 + 3.0 * draw_unit(seed, "system", slot, "life"),
                    200.0 + 120.0 * draw_unit(seed, "system", slot, "radius"),
                    (12.0 + 12.0 * draw_unit(seed, "system", slot, "strength")) if low
                    else (10.0 + 6.0 * draw_unit(seed, "system", slot, "strength")),
                    1.0 if low else -1.0,
                    100.0 + 800.0 * draw_unit(seed, "system", slot, "y"),
                    -60.0 + 120.0 * draw_unit(seed, "system", slot, "vy"),
                    80.0 * draw_unit(seed, "system", slot, "wobble"),
                    2.0 * draw_unit(seed, "forecast", slot, "x") - 1.0,
                    2.0 * draw_unit(seed, "forecast", slot, "y") - 1.0,
                    2.0 * draw_unit(seed, "forecast", slot, "strength") - 1.0,
                )
            if len(self._slots) > 4096:
                self._slots.clear()
        return self._slots[slot]

    def _systems(self, t: float, blur: float) -> tuple:
        """(x, y, 1/radius, strength, turn) for every system alive at time t.

        `blur` is how many km a forecast misplaces each system; the direction
        of the error is fixed per system, so every caller sees the same
        forecast and it closes on the truth as the lead shrinks.
        """
        out = []
        last = int(t // SYSTEM_SLOT_DAYS)
        for slot in range(last - 4, last + 1):
            system = self._slot(slot)
            if system is None:
                continue
            life, radius, strength, turn, y0, vy, wobble, ex, ey, es = system
            age = t - slot * SYSTEM_SLOT_DAYS
            if not 0.0 <= age <= life:
                continue
            x = -radius + (WORLD_W + 2.0 * radius) * age / life
            y = y0 + vy * age + wobble * (2.0 * _noise(self.noise_seed, 50 + (slot & 0xFF), age * 0.7, 0.5) - 1.0)
            fade = _smooth(age, 0.0, 0.5) * (1.0 - _smooth(age, life - 0.5, life))
            out.append((x + ex * blur, y + ey * blur, 1.0 / radius,
                        strength * fade * (1.0 + es * blur / 400.0), turn))
        day = int(t // 1)
        now, before, w = self._blend(day, int((t - day) * MINUTES_PER_DAY))
        pulse = before[1] + (now[1] - before[1]) * w
        gyre = self.world.gyre
        out.append((gyre["x"], gyre["y"], 1.0 / GYRE_RADIUS, GYRE_SPEED * pulse, 1.0))
        return tuple(out)

    # -- sampling ------------------------------------------------------------

    def frame(self, day: int, minute: int, lead: float = 0.0, coverage: float = 1.0) -> WeatherFrame:
        day, minute = day + minute // MINUTES_PER_DAY, minute % MINUTES_PER_DAY
        coverage = 0.0 if coverage < 0.0 else 1.0 if coverage > 1.0 else coverage
        if lead <= 0.0 or coverage >= 1.0:
            lead, coverage = 0.0, 1.0
        key = (day, minute, lead, coverage)
        found = self._frames.get(key)
        if found is None:
            if len(self._frames) >= self.FRAMES_KEPT:
                self._frames.clear()
            found = self._frames[key] = WeatherFrame(self, day, minute, lead, coverage)
        return found

    def wind(self, day: int, minute: int, x: float, y: float, layer: int) -> tuple[float, float]:
        """The true wind."""
        return self.frame(day, minute).wind(x, y, layer)

    def forecast(self, day: int, minute: int, x: float, y: float, layer: int,
                 lead: float, coverage: float) -> tuple[float, float]:
        """The wind a forecast made `lead` minutes before this moment expects."""
        return self.frame(day, minute, lead, coverage).wind(x, y, layer)


def compass(vx: float, vy: float) -> str:
    """The sixteen-point name of where a wind comes from."""
    names = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    if vx == 0.0 and vy == 0.0:
        return "calm"
    fx, fy = -vx, -vy                        # where it comes from; north is -y
    # Walk the sixteen directions without trigonometry: compare against the
    # tangents of the sector edges.
    ax, ay = (fx if fx >= 0 else -fx), (fy if fy >= 0 else -fy)
    t1, t3 = 0.19891236737965800, 0.66817863791929892       # tan 11.25, tan 33.75 degrees
    if ax <= ay * t1:
        step = 0
    elif ax <= ay * t3:
        step = 1
    elif ay > ax * t3:
        step = 2
    elif ay > ax * t1:
        step = 3
    else:
        step = 4
    # step counts sixteenths from the north-south axis toward the east-west axis
    if fy < 0:                               # from the north half
        index = step if fx >= 0 else (16 - step) % 16
    else:                                    # from the south half
        index = 8 - step if fx >= 0 else 8 + step
    return names[index]


# --------------------------------------------------------------------------
# Flight: the turn engine (docs/systems.md, section 5). A turn is fifteen
# minutes. Every number is in the tables below, and every chance is a keyed
# draw, so the same turn always ends the same way.
# --------------------------------------------------------------------------

DAWN_MINUTE = 6 * 60
DUSK_MINUTE = 20 * 60
DAY_TURNS = (MINUTES_PER_DAY - DAWN_MINUTE) // TURN_MINUTES      # 72: dawn to midnight
SOLAR_MINUTES = (11 * 60, 15 * 60)                               # free lift

FUEL_START, BALLAST_START, PIBALS_START = 60, 5, 3
COST_LAUNCH = 2
COST_HOLD_DAY, COST_HOLD_NIGHT = 1, 2
COST_CLIMB = 3
COST_CLIMB_EXTRA = {5: 1, 6: 2}             # on top, for climbing into these layers
PIBAL_TURNS = 4
UNREAD_LEAD = 120.0                         # minutes: how stale the forecast of an unread layer is
TURN_HOURS = TURN_MINUTES / 60.0

LANDING_CLASSES = ("clean", "rough", "hard")
LANDING_WIND = (15.0, 30.0)                 # up to here clean, up to here rough, beyond hard
LANDING_WEAR = {"clean": 0, "rough": 10, "hard": 30}
WEAR_GROUNDED = 100
SQUALL_THRESHOLD = 0.4
SQUALL_WEAR = 6
FOG_THRESHOLD = 0.3
ROTOR_CHANCE = 0.35
ROTOR_WEAR = 2
ROTOR_REACH_KM = 15.0
RIDGE_WEAR = 2
# The trail rope: in the lowest layer a rope dragged over the ground (and a scrap of sail) bends the course.
ROPE_COS, ROPE_SIN = 0.8660254037844387, 0.5      # thirty degrees either side of the wind
ROPE_SPEED = 0.75                                 # of the wind's speed: the rope is a brake
ROPE_WEAR = 1                                     # a turn on the rope, on the balloon


def roped(vx: float, vy: float, side: str) -> tuple[float, float]:
    """The drift on the trail rope: the wind's, turned thirty degrees to the left or the right, and slower."""
    turn = ROPE_SIN if side == "left" else -ROPE_SIN
    return ((vx * ROPE_COS + vy * turn) * ROPE_SPEED, (-vx * turn + vy * ROPE_COS) * ROPE_SPEED)
PLACE_RADIUS_KM = 12.5                      # this close to a town or station is at it

ACTIONS = ("launch", "hold", "climb", "sink", "jettison", "left", "right", "land", "pibal")


@dataclass(frozen=True)
class FlightState:
    """A balloon between two turns. Frozen: a turn makes a new one."""
    x: float
    y: float
    day: int = 1
    turn: int = 0                 # turns since dawn
    layer: int = 0                # 0 on the ground
    status: str = "ground"        # ground, aloft, stranded, ditched
    fuel: int = FUEL_START
    ballast: int = BALLAST_START
    pibals: int = PIBALS_START
    wear: int = 0
    pibal_until: int = -1         # true winds in every layer through this turn
    distance: float = 0.0         # km drifted today
    cargo: tuple = ()             # (id, fragile, condition) with condition "ok" or "damaged"
    ceiling: int = 3              # highest layer the envelope reaches
    oxygen: bool = False
    night_gear: bool = False
    iceproof: bool = False
    reads: int = 0                # layers either side of the balloon's own that its instruments read true
    climb_cost: int = COST_CLIMB

    @property
    def minute(self) -> int:
        return DAWN_MINUTE + self.turn * TURN_MINUTES

    @property
    def grounded(self) -> bool:
        """Too worn to fly until repaired."""
        return self.wear >= WEAR_GROUNDED

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["cargo"] = [list(item) for item in self.cargo]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "FlightState":
        fields = {f.name for f in dataclasses.fields(cls)}
        kept = {k: v for k, v in data.items() if k in fields}
        kept["cargo"] = tuple(tuple(item) for item in kept.get("cargo", ()))
        return cls(**kept)


@dataclass(frozen=True)
class TurnResult:
    state: FlightState
    events: tuple                 # (code, detail...) in the order they happened


class Flight:
    """The rules of flying, for one pilot in one world.

    `actions(state)` says what can be done and what it costs. `step(state,
    action)` plays one turn against the true weather and returns the new
    state. `preview(state, action)` follows the forecast instead.
    """

    def __init__(self, world: World, weather: Weather, pilot_key: str):
        self.world, self.weather, self.pilot_key = world, weather, pilot_key
        self.set_stations(world.stations)

    def set_stations(self, stations) -> None:
        """The places of the Post: every town, and each station somebody keeps (a first one, or a mast)."""
        self._places = [(t["x"], t["y"], "town", i) for i, t in enumerate(self.world.towns)]
        self._places += [(s["x"], s["y"], "station", i) for i, s in enumerate(stations) if staffed(s)]

    # -- the ground and the air above it ------------------------------------

    def lowest_open(self, x: float, y: float) -> int:
        """The lowest layer the terrain here leaves open: 1 over lowland."""
        height = self.world.sample(x, y)[0]
        layer = 1
        for block in LAYER_BLOCK_M:
            if block is None or height < block:
                break
            layer += 1
        return layer

    def reads(self, state: FlightState, layer: int) -> bool:
        """Does the pilot know the true wind in this layer, or only a forecast of it?

        Known: every layer while a pibal's reading lasts; on the ground, the
        layer a launch would enter; aloft, the balloon's own layer and as
        many either side as its instruments reach.
        """
        if state.pibal_until >= state.turn:
            return True
        if state.layer == 0:
            return layer == self.lowest_open(state.x, state.y)
        return abs(layer - state.layer) <= state.reads

    def top_layer(self, state: FlightState) -> int:
        return min(LAYERS if state.oxygen else LAYERS - 1, state.ceiling)

    def place(self, x: float, y: float):
        """The town or station here, as (kind, index), or None in the wild."""
        for px, py, kind, index in self._places:
            if (x - px) * (x - px) + (y - py) * (y - py) <= PLACE_RADIUS_KM * PLACE_RADIUS_KM:
                return kind, index
        return None

    def over_water(self, x: float, y: float) -> bool:
        return self.world.sample(x, y)[1] < SHORE

    def gyre_km(self, x: float, y: float) -> float:
        """How far a place is from the middle of the Gyre."""
        gyre = self.world.gyre
        return math.sqrt((x - gyre["x"]) ** 2 + (y - gyre["y"]) ** 2)

    def in_wall(self, x: float, y: float) -> bool:
        """Is this place in the wall of the Gyre: between the eye and the wall's outer edge?"""
        return GYRE_EYE_KM < self.gyre_km(x, y) <= GYRE_WALL_KM

    def wall_stops(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """Does the wall of the Gyre stop a balloon below its layer from making this move?

        The whole of the move is tried, not where it ends: a fast wind
        does not carry a balloon over the wall in one bound.

        From outside, nothing gets into the wall. From the eye, nothing
        gets out of it. A balloon already in the wall (it came down there,
        or sank there) is not held: it may be carried about in it and out
        of it, but not into the eye.
        """
        gyre = self.world.gyre
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 <= 0.0 else max(0.0, min(1.0, ((gyre["x"] - x0) * dx + (gyre["y"] - y0) * dy) / length2))
        nearest = self.gyre_km(x0 + dx * t, y0 + dy * t)       # the closest the move comes to the middle
        start, end = self.gyre_km(x0, y0), self.gyre_km(x1, y1)
        if start > GYRE_WALL_KM:
            return nearest <= GYRE_WALL_KM
        if start <= GYRE_EYE_KM:
            return end > GYRE_EYE_KM
        return nearest <= GYRE_EYE_KM

    def is_night(self, state: FlightState) -> bool:
        return state.minute >= DUSK_MINUTE

    # -- costs ---------------------------------------------------------------

    def hold_cost(self, state: FlightState, layer: int) -> int:
        minute = state.minute
        if minute >= DUSK_MINUTE:
            cost = COST_HOLD_DAY if state.night_gear else COST_HOLD_NIGHT
        elif SOLAR_MINUTES[0] <= minute < SOLAR_MINUTES[1]:
            cost = 0
        else:
            cost = COST_HOLD_DAY
        if layer >= self.weather.frame(state.day, minute).icing_layer and not state.iceproof:
            cost = max(1, cost) * 2
        return cost

    def climb_cost(self, state: FlightState, to_layer: int) -> int:
        return state.climb_cost + COST_CLIMB_EXTRA.get(to_layer, 0)

    def landing_class(self, state: FlightState) -> str:
        """How a landing here and now would go: clean, rough, hard or ditch."""
        if self.over_water(state.x, state.y):
            return "ditch"
        frame = self.weather.frame(state.day, state.minute)
        wind = frame.surface_speed(state.x, state.y)
        rank = 0 if wind <= LANDING_WIND[0] else 1 if wind <= LANDING_WIND[1] else 2
        lit = self.place(state.x, state.y) is not None
        if self.is_night(state) and not lit and not state.night_gear:
            rank += 1
        if frame.fog(state.x, state.y) > FOG_THRESHOLD and not lit:
            rank += 1
        return LANDING_CLASSES[min(rank, 2)]

    def actions(self, state: FlightState) -> dict:
        """What the pilot may do now, and its cost in fuel."""
        out: dict[str, int] = {}
        if state.turn >= DAY_TURNS:
            return out
        if state.status == "ground":
            low = self.lowest_open(state.x, state.y)
            if not state.grounded and state.fuel >= COST_LAUNCH and low <= self.top_layer(state):
                out["launch"] = COST_LAUNCH
            return out
        if state.status != "aloft":
            return out
        low, top = self.lowest_open(state.x, state.y), self.top_layer(state)
        hold = self.hold_cost(state, state.layer)
        if hold <= state.fuel:
            out["hold"] = hold
        if state.layer < top:
            cost = self.climb_cost(state, state.layer + 1)
            if cost <= state.fuel:
                out["climb"] = cost
            if state.ballast > 0:
                out["jettison"] = 0
        if state.layer > low:
            out["sink"] = 0
        if state.pibals > 0 and state.pibal_until < state.turn:
            out["pibal"] = 0
        if state.layer <= low and "hold" in out:
            out["left"] = out["right"] = out["hold"]    # the rope reaches the ground from the lowest layer only
        if state.layer <= low:
            # Too much wind to land in is refused while there is any other way to stay up.
            stay_up = any(a in out for a in ("hold", "climb", "jettison"))
            if self.landing_class(state) != "hard" or not stay_up:
                out["land"] = 0
        return out

    # -- one turn ------------------------------------------------------------

    def _chance(self, state: FlightState, purpose: str) -> float:
        return draw_unit(self.world.seed, "turn", self.pilot_key, state.day, state.turn, purpose)

    def step(self, state: FlightState, action: str) -> TurnResult:
        """Play one turn. Raises ValueError for an action that is not on offer."""
        offered = self.actions(state)
        if action not in offered:
            raise ValueError(f"{action} is not possible now")
        events: list[tuple] = []
        if action == "pibal":       # reading the air takes no time
            return TurnResult(dataclasses.replace(state, pibals=state.pibals - 1,
                                                  pibal_until=state.turn + PIBAL_TURNS - 1), (("pibal",),))
        if action == "land":
            # Judged by the weather at the moment it is chosen; then it takes the turn.
            landed = self._land(state, events)
            return TurnResult(dataclasses.replace(landed, turn=state.turn + 1), tuple(events))

        cost, layer, ballast = offered[action], state.layer, state.ballast
        if action == "launch":
            layer = self.lowest_open(state.x, state.y)
        elif action in ("climb", "jettison"):
            layer += 1
            if action == "jettison":
                ballast -= 1
        elif action == "sink":
            layer -= 1
        events.append((action, layer, cost))
        fuel = state.fuel - cost
        wear = state.wear

        # Drift on the true wind of the layer the balloon is now in.
        frame = self.weather.frame(state.day, state.minute)
        vx, vy = frame.wind(state.x, state.y, layer)
        if action in ("left", "right"):
            vx, vy = roped(vx, vy, action)
            wear += ROPE_WEAR
        x, y = state.x + vx * TURN_HOURS, state.y + vy * TURN_HOURS
        stranded = False
        need = self.lowest_open(x, y)
        # The layer the balloon would be in over there: its own, or the one high ground would force it up to.
        over_there = need if layer < need <= self.top_layer(state) else layer
        if over_there < GYRE_WALL_LAYER and self.wall_stops(state.x, state.y, x, y):
            # The wall of the Gyre lets nothing through below its layer: the balloon is thrown back where it
            # was, before any ground ahead is met or paid for.
            x, y = state.x, state.y
            wear += GYRE_WEAR
            events.append(("wall",))
        elif need > layer:
            # High ground ahead fills this layer.
            if need > self.top_layer(state):
                x, y = state.x, state.y                 # it cannot be crossed: held in its lee
                wear += RIDGE_WEAR
                events.append(("ridge", need))
            else:
                while layer < need:
                    price = self.climb_cost(state, layer + 1) * 2
                    if fuel >= price:
                        fuel -= price
                        events.append(("forced_climb", layer + 1, price))
                    elif ballast > 0:
                        ballast -= 1
                        events.append(("forced_jettison", layer + 1))
                    else:
                        stranded = True
                        break
                    layer += 1
        moved = math.sqrt((x - state.x) * (x - state.x) + (y - state.y) * (y - state.y))
        if moved:
            events.append(("drift", round(moved, 1), compass(vx, vy)))
        if self.gyre_km(x, y) <= GYRE_EYE_KM < self.gyre_km(state.x, state.y):
            events.append(("eye",))
        after = dataclasses.replace(state, x=x, y=y, layer=layer, status="aloft", fuel=fuel, ballast=ballast,
                                    wear=wear, turn=state.turn + 1, distance=state.distance + moved)
        if stranded:
            events.append(("high_ground",))
            return TurnResult(self._land(after, events, force="hard"), tuple(events))

        # Hazards, each a keyed draw for this pilot, day and turn.
        low, top = self.lowest_open(x, y), self.top_layer(state)
        squall = frame.squall(x, y)
        if squall > SQUALL_THRESHOLD and layer <= 4:
            up = self._chance(state, "squall") < 0.5
            shoved = min(top, layer + 1) if up else max(low, layer - 1)
            after = dataclasses.replace(after, layer=shoved, wear=after.wear + round(SQUALL_WEAR * squall))
            events.append(("squall", shoved))
        elif layer <= 2 and self._in_lee(x, y, vx, vy, layer) and self._chance(state, "rotor") < ROTOR_CHANCE:
            # Rough air behind ground that reaches this layer: it wears the
            # balloon, and drops it a layer where there is one below.
            dropped = max(low, layer - 1)
            after = dataclasses.replace(after, layer=dropped, wear=after.wear + ROTOR_WEAR)
            events.append(("rotor", dropped))

        if after.turn >= DAY_TURNS:                     # midnight: the day is over, down it comes
            events.append(("midnight",))
            return TurnResult(self._land(after, events), tuple(events))
        return TurnResult(after, tuple(events))

    def _in_lee(self, x: float, y: float, vx: float, vy: float, layer: int) -> bool:
        """Is there ground upwind, close by, high enough to fill this layer?"""
        speed = math.sqrt(vx * vx + vy * vy)
        if speed < 1.0:
            return False
        ux, uy = x - vx / speed * ROTOR_REACH_KM, y - vy / speed * ROTOR_REACH_KM
        return self.world.sample(ux, uy)[0] >= LAYER_BLOCK_M[layer - 1]

    def _land(self, state: FlightState, events: list, force: str | None = None) -> FlightState:
        """Put the balloon down here. The class decides wear and what the cargo suffers."""
        kind = force or self.landing_class(state)
        if kind == "ditch":
            events.append(("ditch",))
            return dataclasses.replace(state, layer=0, status="ditched", cargo=())
        cargo = state.cargo
        if kind == "rough":
            cargo = tuple((i, fragile, "damaged" if fragile else cond) for i, fragile, cond in cargo)
        elif kind == "hard":
            # Half the cargo is lost: which half is a keyed draw per item.
            cargo = tuple((i, fragile, "damaged" if fragile else cond) for i, fragile, cond in cargo
                          if draw_unit(self.world.seed, "turn", self.pilot_key, state.day, state.turn, "cargo", i) < 0.5)
        place = self.place(state.x, state.y)
        status = "ground" if place is not None or state.fuel > 0 else "stranded"
        events.append(("land", kind, place))
        if status == "stranded":
            events.append(("stranded",))
        return dataclasses.replace(state, layer=0, status=status, cargo=cargo,
                                   wear=min(WEAR_GROUNDED, state.wear + LANDING_WEAR[kind]))

    # -- looking ahead ---------------------------------------------------------

    def preview(self, state: FlightState, action: str, horizon: int, coverage: float = 0.0) -> dict:
        """Where the forecast says this choice leads: the action, then holding.

        The pilot knows the true wind only where they have read it: in their
        own layer, the layer a launch enters (they are standing in it), and
        every layer while a pibal's reading lasts. There the
        track starts from the truth and looks ahead turn by turn. Any other
        layer is forecast from a bulletin `UNREAD_LEAD` minutes old, so it is
        never the truth, not even for the first turn.

        The track follows the fuel and ballast too, so it ends where the
        flight would. Returns:

          layer    the layer the first turn is flown in
          cost     the action's cost, or None if it is not on offer
          points   (x, y, layer) after each turn
          blocked  True if high ground forces a climb along the way
          forced   fuel the forced climbs would cost
          sacks    ballast sacks the forced climbs would spend
          stop     why the track ends early: "ridge" (ground above the
                   ceiling), "high_ground" (a forced climb that cannot be
                   paid for), "fuel" (nothing left to hold with), "wall"
                   (the Gyre's wall, below the layer that crosses it), or None
        """
        offered = self.actions(state)
        if action not in offered or action in ("land", "pibal"):
            return {"layer": state.layer, "cost": None, "points": [], "blocked": False, "forced": 0, "sacks": 0,
                    "stop": None}
        layer = state.layer
        if action == "launch":
            layer = self.lowest_open(state.x, state.y)
        elif action in ("climb", "jettison"):
            layer += 1
        elif action == "sink":
            layer -= 1
        first = layer
        # On the ground the pilot stands in the air they would launch into.
        read = action == "launch" or self.reads(state, layer)
        stale = 0.0 if read else UNREAD_LEAD
        fuel = state.fuel - offered[action]
        ballast = state.ballast - (1 if action == "jettison" else 0)
        x, y, points, blocked, forced, sacks, stop = state.x, state.y, [], False, 0, 0, None
        top = self.top_layer(state)
        for i in range(horizon):
            turn = state.turn + i
            if turn >= DAY_TURNS:
                break
            if i:
                hold = self.hold_cost(dataclasses.replace(state, turn=turn), layer)
                if hold > fuel:
                    stop = "fuel"
                    break
                fuel -= hold
            minute = DAWN_MINUTE + turn * TURN_MINUTES
            vx, vy = self.weather.frame(state.day, minute, stale + i * TURN_MINUTES, coverage).wind(x, y, layer)
            if action in ("left", "right") and layer == first:
                vx, vy = roped(vx, vy, action)          # the track of staying on the rope, while it still reaches
            nx, ny = x + vx * TURN_HOURS, y + vy * TURN_HOURS
            need = self.lowest_open(nx, ny)
            if (need if layer < need <= top else layer) < GYRE_WALL_LAYER and self.wall_stops(x, y, nx, ny):
                stop = "wall"                           # the track ends at the wall of the Gyre, as the turn would
                break
            if need > layer:
                blocked = True
                if need > top:
                    stop = "ridge"
                    break
                while layer < need:
                    price = self.climb_cost(state, layer + 1) * 2
                    if fuel >= price:
                        fuel -= price
                        forced += price
                    elif ballast > 0:
                        ballast -= 1
                        sacks += 1
                    else:
                        stop = "high_ground"
                        break
                    layer += 1
            x, y = nx, ny
            points.append((x, y, layer))
            if stop:
                break
        return {"layer": first, "cost": offered[action], "points": points, "blocked": blocked,
                "forced": forced, "sacks": sacks, "stop": stop}


# --------------------------------------------------------------------------
# Keys: an incremental decoder. Escape sequences may arrive split across
# reads; nothing depends on key-up, chords or timing beyond a lone Escape.
# --------------------------------------------------------------------------

_CSI_FINAL = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
_CSI_TILDE = {"1": "HOME", "7": "HOME", "4": "END", "8": "END", "5": "PGUP", "6": "PGDN", "11": "F1"}
_SS3 = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END", "P": "F1"}


class KeyDecoder:
    """Bytes in, key names out: a character, or UP, ENTER, ESC and so on."""

    MAX_SEQUENCE = 16

    def __init__(self):
        self._buf = bytearray()
        self._after_cr = False

    @property
    def pending(self) -> bool:
        """True while bytes are held back waiting for the rest of a key."""
        return bool(self._buf)

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        keys: list[str] = []
        while self._buf:
            used = self._one(keys)
            if used == 0:
                break
            del self._buf[:used]
        return keys

    def flush(self) -> list[str]:
        """No more bytes came in time: a held Escape was a key of its own."""
        keys: list[str] = []
        if self._buf and self._buf[0] == 0x1B:
            keys.append("ESC")
            del self._buf[:1]
            keys += self.feed(b"")
        self._buf.clear()
        return keys

    def _one(self, keys: list[str]) -> int:
        """Decode one key from the front of the buffer; 0 means wait for more."""
        buf = self._buf
        b = buf[0]
        if self._after_cr:
            self._after_cr = False
            if b in (0x0A, 0x00):       # CR LF and CR NUL are one Enter
                return 1
        if b == 0x1B:
            return self._escape(keys)
        if b == 0x0D:
            keys.append("ENTER")
            self._after_cr = True
            return 1
        if b == 0x0A:
            keys.append("ENTER")
            return 1
        if b in (0x7F, 0x08):
            keys.append("BACKSPACE")
            return 1
        if b == 0x09:
            keys.append("TAB")
            return 1
        if b == 0x00:
            return 1
        if b < 0x20:
            keys.append("CTRL-" + chr(b + 0x40))
            return 1
        if b < 0x7F:
            keys.append(chr(b))
            return 1
        return self._utf8(keys)

    def _escape(self, keys: list[str]) -> int:
        buf = self._buf
        if len(buf) == 1:
            return 0
        second = buf[1]
        if second == 0x5B:              # CSI: parameters, then one final byte
            for i in range(2, len(buf)):
                if 0x40 <= buf[i] <= 0x7E:
                    params, final = buf[2:i].decode("ascii", "replace"), chr(buf[i])
                    key = _CSI_TILDE.get(params.split(";")[0]) if final == "~" else (
                        _CSI_FINAL.get(final) if not params or params.startswith("1;") else None)
                    if key:
                        keys.append(key)
                    return i + 1
                if i >= self.MAX_SEQUENCE:
                    return i + 1        # runaway sequence: drop it, stay bounded
            return 0 if len(buf) <= self.MAX_SEQUENCE else len(buf)
        if second == 0x4F:              # SS3: application-mode arrows and F1
            if len(buf) < 3:
                return 0
            key = _SS3.get(chr(buf[2]))
            if key:
                keys.append(key)
            return 3
        keys.append("ESC")              # Escape followed by an ordinary key
        return 1

    def _utf8(self, keys: list[str]) -> int:
        buf = self._buf
        b = buf[0]
        need = 2 if 0xC2 <= b <= 0xDF else 3 if 0xE0 <= b <= 0xEF else 4 if 0xF0 <= b <= 0xF4 else 0
        if need == 0:
            return 1                    # not the start of a character: drop it
        if len(buf) < need:
            if all(0x80 <= c <= 0xBF for c in buf[1:]):
                return 0
            return 1
        try:
            ch = bytes(buf[:need]).decode("utf-8")
        except UnicodeDecodeError:
            return 1
        if ch.isprintable():
            keys.append(ch)
        return need


# --------------------------------------------------------------------------
# Terminal streams.
# --------------------------------------------------------------------------

class SessionClosed(Exception):
    """The caller is gone: input ended or output broke."""


class Output:
    def __init__(self, fd: int):
        self._fd = fd

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        try:
            while data:
                data = data[os.write(self._fd, data):]
        except OSError:
            raise SessionClosed() from None


class Input:
    """Timed reads from the caller.

    A thread does the blocking read, so the same code serves pipes, sockets
    and consoles on POSIX and Windows, where select() does not cover them all.
    """

    def __init__(self, fd: int, console: bool = False):
        self._queue: queue.Queue[bytes] = queue.Queue()
        target = self._read_console if console else self._read_fd
        threading.Thread(target=target, args=(fd,), name="fw-input", daemon=True).start()

    def _read_fd(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 256)
            except OSError:
                data = b""
            self._queue.put(data)
            if not data:
                return

    def _read_console(self, fd: int) -> None:      # Windows console, standalone only
        import msvcrt
        special = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",
                   "G": b"\x1b[H", "O": b"\x1b[F", "I": b"\x1b[5~", "Q": b"\x1b[6~", ";": b"\x1b[11~"}
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                self._queue.put(special.get(msvcrt.getwch(), b""))
            else:
                self._queue.put(ch.encode("utf-8", "replace"))

    def read(self, timeout: float) -> bytes | None:
        """Bytes, b"" at end of input, or None when nothing came in time."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class LocalTerminal:
    """Standalone play in a real terminal: raw keys in, restored on the way out."""

    def __init__(self):
        self._restore = None
        self.console = False

    def enter(self) -> None:
        if not sys.stdin.isatty():
            return
        if os.name == "nt":
            self.console = True
            try:
                import ctypes
                kernel = ctypes.windll.kernel32
                handle = kernel.GetStdHandle(-11)
                mode = ctypes.c_uint()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel.SetConsoleMode(handle, mode.value | 0x0004)   # ANSI sequences
                    self._restore = lambda: kernel.SetConsoleMode(handle, mode.value)
            except (OSError, AttributeError):
                pass
            return
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def leave(self) -> None:
        if self._restore:
            try:
                self._restore()
            except Exception:
                pass
            self._restore = None


# --------------------------------------------------------------------------
# The cell buffer (S06). Every screen is drawn onto a grid of cells, each a
# glyph with a foreground and a background. What goes to the caller is only
# the cells that changed since the last frame.
#
# Colours are written once, as RGB or as a named ink, and read three ways:
# truecolor as they are, 256 colours by the nearest palette entry, and
# sixteen colours by an art-directed choice for named inks and the nearest
# colour otherwise, using only what every ANSI terminal takes: bold for a
# bright foreground, and the eight plain backgrounds.
# --------------------------------------------------------------------------

ANSI16 = ((0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0), (0, 0, 170), (170, 0, 170), (0, 170, 170),
          (170, 170, 170), (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85), (85, 85, 255),
          (255, 85, 255), (85, 255, 255), (255, 255, 255))

# name: (truecolor, sixteen-colour index). The sixteen-colour reading is
# chosen, not computed: a nearest colour would turn half of these grey.
INKS = {
    "text": ((206, 214, 226), 7),
    "dim": ((112, 126, 148), 6),
    "accent": ((255, 196, 110), 11),
    "warn": ((255, 150, 130), 9),
    "bright": ((255, 255, 255), 15),
    "sea": ((70, 130, 190), 4),
    "land": ((120, 180, 110), 2),
    "cool": ((120, 228, 255), 14),
    "warm": ((255, 150, 196), 13),
    # What a figure is called, and the figure: told apart at a glance wherever both stand on a line.
    "label": ((128, 146, 176), 6),
    "value": ((240, 244, 250), 15),
    # A track that is not the chosen one: its choice's colour, further off. Each has a sixteen-colour reading
    # of its own, so that even there the three are told apart.
    "cool_dim": ((90, 171, 191), 6),
    "warm_dim": ((191, 112, 147), 5),
    "accent_dim": ((191, 147, 82), 3),
}
_ASCII_RAMP = " .:-=+*#%@"
HALF_BLOCK = "▀"           # upper half block: foreground on top, background below
LOWER_BLOCK = "▄"

_near16: dict = {}
_near256: dict = {}


def nearest16(rgb, limit: int = 16) -> int:
    """The nearest of the sixteen ANSI colours, or of the first `limit` of them."""
    key = (rgb, limit)
    found = _near16.get(key)
    if found is None:
        r, g, b = rgb
        found = min(range(limit), key=lambda i: (ANSI16[i][0] - r) ** 2 + (ANSI16[i][1] - g) ** 2
                    + (ANSI16[i][2] - b) ** 2)
        if len(_near16) > 4096:
            _near16.clear()
        _near16[key] = found
    return found


def nearest256(rgb) -> int:
    """The nearest xterm-256 colour: the 6x6x6 cube or the grey ramp, whichever is closer."""
    found = _near256.get(rgb)
    if found is None:
        r, g, b = rgb
        steps = (0, 95, 135, 175, 215, 255)

        def off(c):
            return (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2

        def around(v):
            low = max(i for i in range(6) if steps[i] <= v)
            return (low, min(5, low + 1))
        # The cube entry nearest by distance to a dark green is a grey: each
        # axis rounds to the same step. So look at the entries around the
        # colour and count a loss of saturation against them too.
        chroma = max(r, g, b) - min(r, g, b)
        ri, gi, bi = min(((i, j, k) for i in around(r) for j in around(g) for k in around(b)),
                         key=lambda c: off((steps[c[0]], steps[c[1]], steps[c[2]]))
                         + (max(steps[c[0]], steps[c[1]], steps[c[2]]) - min(steps[c[0]], steps[c[1]], steps[c[2]])
                            - chroma) ** 2)
        cube = (steps[ri], steps[gi], steps[bi])
        grey_i = min(23, max(0, round(((r + g + b) / 3 - 8) / 10)))
        grey = 8 + 10 * grey_i
        # The grey ramp is for greys. A dark green or a deep blue is nearer to a
        # grey by distance, but a forest drawn grey is no longer a forest.
        neutral = max(r, g, b) - min(r, g, b) < 24
        found = 232 + grey_i if neutral and off((grey, grey, grey)) < off(cube) else 16 + 36 * ri + 6 * gi + bi
        if len(_near256) > 8192:
            _near256.clear()
        _near256[rgb] = found
    return found


def _rgb(ink):
    """An ink as three whole numbers from 0 to 255, whatever arithmetic produced it."""
    if isinstance(ink, str):
        return INKS[ink][0]
    return tuple(0 if v < 0 else 255 if v > 255 else int(v) for v in ink)


_ASCII_GLYPHS = {HALF_BLOCK: "#", LOWER_BLOCK: "#", "\u2588": "#", "\u2591": ".", "\u2592": ":", "\u2593": "#",
                 "\u00b7": "-", "\u2022": "*", "\u00b0": "o", "\u25a0": "#"}
# A track on the map: a dot for each turn, a heavier one on the hour, and an arrowhead where it ends. CP437
# draws its arrows where the control codes are (the left arrow sits on Escape), so they cannot be sent as
# text: only UTF-8 gets arrows.
TRACK_DOT, TRACK_HOUR = "\u00b7", "\u2022"
TRACK_HEADS = ("\u2192", "\u2198", "\u2193", "\u2199", "\u2190", "\u2196", "\u2191", "\u2197")    # east, then clockwise
TRACK_PLAIN = ("*", "*", (">", "v", "<", "^"))      # the same three where there is no UTF-8


def track_glyphs(points: list, start: tuple, utf8: bool) -> list[str]:
    """The glyph for each step of a track: dots, an hour mark every fourth turn, and a head that points the way."""
    out = []
    for i, (x, y, _) in enumerate(points):
        if i < len(points) - 1:
            out.append((TRACK_HOUR if utf8 else TRACK_PLAIN[1]) if (i + 1) % 4 == 0
                       else (TRACK_DOT if utf8 else TRACK_PLAIN[0]))
            continue
        px, py = (points[i - 1][0], points[i - 1][1]) if i else start
        dx, dy = x - px, y - py
        if dx == 0 and dy == 0:
            out.append(TRACK_HOUR if utf8 else TRACK_PLAIN[1])     # a calm: it ends where it is, pointing nowhere
            continue
        if utf8:
            # Eight ways: which of dx, dy dominates, or both.
            ax, ay = abs(dx), abs(dy)
            if ax > 2.414 * ay:
                way = 0 if dx > 0 else 4
            elif ay > 2.414 * ax:
                way = 2 if dy > 0 else 6
            else:
                way = (1 if dy > 0 else 7) if dx > 0 else (3 if dy > 0 else 5)
            out.append(TRACK_HEADS[way])
        else:
            heads = TRACK_PLAIN[2]
            out.append((heads[0] if dx > 0 else heads[2]) if abs(dx) >= abs(dy) else (heads[1] if dy > 0 else heads[3]))
    return out


def safe_glyph(ch: str) -> str:
    """One printable character that takes exactly one cell, or a question mark.

    The buffer is only right if what it writes moves the cursor one cell. A
    control character, an escape, or a wide or zero-width character would
    put the caller's terminal out of step with it.
    """
    if len(ch) != 1 or not ch.isprintable():
        return "?"
    if " " <= ch <= "~":
        return ch
    if unicodedata.east_asian_width(ch) in ("W", "F") or unicodedata.combining(ch) \
            or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return "?"
    return ch


class Screen:
    """One frame: a grid of cells.

    `put` and `text` take an ink for the foreground and the background: a
    name from INKS, an (r, g, b) tuple, or None for the terminal's own.
    `pixel` paints half a cell, so a cell holds two square pixels.
    `render(previous)` returns the bytes that turn the previous frame into
    this one, and nothing at all when the two are the same.
    """

    def __init__(self, width: int, height: int, depth: str = "truecolor", charset: str = "utf-8"):
        self.width, self.height = max(1, width), max(1, height)
        self.depth, self.charset = depth, charset
        n = self.width * self.height
        self.ch = [" "] * n
        self.fg: list = [None] * n
        self.bg: list = [None] * n
        self.split = bytearray(n)          # 1 where the cell holds two pixels: fg on top, bg below
        self._cells: list | None = None    # what goes out for each cell, worked out once per frame

    # -- drawing -------------------------------------------------------------

    def put(self, x: int, y: int, ch: str, fg=None, bg=None) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            k = y * self.width + x
            self.ch[k], self.fg[k], self.bg[k], self.split[k] = safe_glyph(ch), fg, bg, 0
            self._cells = None

    def text(self, x: int, y: int, text: str, fg="text", bg=None) -> None:
        """A run of text, clipped at the edges. One character is one cell."""
        if not 0 <= y < self.height:
            return
        for i, ch in enumerate(text):
            if x + i >= self.width:
                break
            if x + i >= 0:
                self.put(x + i, y, ch, fg, bg)

    def fill(self, x0: int, y0: int, x1: int, y1: int, bg=None, ch: str = " ", fg=None) -> None:
        for y in range(max(0, y0), min(self.height, y1 + 1)):
            for x in range(max(0, x0), min(self.width, x1 + 1)):
                self.put(x, y, ch, fg, bg)

    def pixel(self, x: int, py: int, rgb) -> None:
        """Paint one square pixel; row py is half a cell tall."""
        y = py >> 1
        if 0 <= x < self.width and 0 <= y < self.height:
            k = y * self.width + x
            if not self.split[k]:
                base = _rgb(self.bg[k]) if self.bg[k] is not None else (0, 0, 0)
                self.ch[k], self.fg[k], self.bg[k], self.split[k] = HALF_BLOCK, base, base, 1
            if py & 1:
                self.bg[k] = _rgb(rgb)
            else:
                self.fg[k] = _rgb(rgb)
            self._cells = None

    def pixel_row(self, x: int, y: int, tops, bottoms) -> None:
        """A run of pixel cells along one row: the upper and lower colour of each. For whole pictures."""
        if not 0 <= y < self.height:
            return
        k, width = y * self.width, self.width
        for i in range(len(tops)):
            if 0 <= x + i < width:
                self.ch[k + x + i], self.fg[k + x + i], self.bg[k + x + i] = HALF_BLOCK, tops[i], bottoms[i]
                self.split[k + x + i] = 1
        self._cells = None

    def over(self, x: int, y: int, ch: str, fg="bright", darken: float = 0.6) -> None:
        """A glyph on top of pixels: it takes the cell, on the darkened mean of what was there."""
        if 0 <= x < self.width and 0 <= y < self.height:
            k = y * self.width + x
            if self.split[k]:
                top, bottom = self.fg[k], self.bg[k]
                self.put(x, y, ch, fg, _rgb(tuple((top[i] + bottom[i]) / 2 * (1 - darken) for i in range(3))))
            else:
                self.put(x, y, ch, fg, self.bg[k])

    # -- output --------------------------------------------------------------

    def _cell(self, k: int) -> tuple:
        """What actually goes out for a cell: (glyph, foreground code, background code)."""
        ch, fg, bg = self.ch[k], self.fg[k], self.bg[k]
        depth = self.depth
        if self.split[k]:
            top, bottom = fg, bg
            if self.charset == "ascii":
                lum = (0.3 * (top[0] + bottom[0]) + 0.59 * (top[1] + bottom[1]) + 0.11 * (top[2] + bottom[2])) / 510
                ch = _ASCII_RAMP[min(len(_ASCII_RAMP) - 1, int(lum * len(_ASCII_RAMP)))]
                fg, bg = tuple((top[i] + bottom[i]) // 2 for i in range(3)), None
            elif top == bottom:
                ch, fg, bg = " ", None, top
            elif depth == "16":
                # Only eight backgrounds exist. Put the half that suffers least there.
                t16, b16 = nearest16(top), nearest16(bottom)
                if t16 == b16:
                    return " ", "", self._bg_code(top)
                if b16 < 8 or (t16 >= 8 and b16 >= 8):
                    ch, fg, bg = HALF_BLOCK, top, bottom
                else:
                    ch, fg, bg = LOWER_BLOCK, bottom, top
        if ch == " " and bg is None:
            return " ", "", ""
        if self.charset == "ascii" and not ch.isascii():
            ch = _ASCII_GLYPHS.get(ch, "?")
        return ch, ("" if ch == " " else self._fg_code(fg)), self._bg_code(bg)

    def cells(self) -> list:
        """What goes out for every cell. Worked out once: a frame is drawn, then rendered."""
        if self._cells is None:
            self._cells = [self._cell(k) for k in range(self.width * self.height)]
        return self._cells

    def _fg_code(self, ink) -> str:
        if ink is None:
            return "39"
        depth = self.depth
        if depth == "16":
            index = INKS[ink][1] if isinstance(ink, str) else nearest16(ink)
            return f"3{index}" if index < 8 else f"1;3{index - 8}"
        rgb = _rgb(ink)
        if depth == "256":
            return f"38;5;{nearest256(rgb)}"
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}"

    def _bg_code(self, ink) -> str:
        if ink is None:
            return "49"
        depth = self.depth
        if depth == "16":
            return f"4{nearest16(_rgb(ink), 8)}"
        rgb = _rgb(ink)
        if depth == "256":
            return f"48;5;{nearest256(rgb)}"
        return f"48;2;{rgb[0]};{rgb[1]};{rgb[2]}"

    def render(self, previous: "Screen | None" = None) -> str:
        """The bytes that turn `previous` into this frame. None means a cleared screen.

        The bottom-right cell is never written: many terminals scroll when it
        is. Unchanged cells cost nothing, and an unchanged frame is "".
        """
        w, h = self.width, self.height
        same_size = previous is not None and previous.width == w and previous.height == h \
            and previous.depth == self.depth and previous.charset == self.charset
        blank = (" ", "", "")
        mine = self.cells()
        theirs = previous.cells() if same_size else [blank] * (w * h)
        out: list[str] = []
        state = None
        sixteen = self.depth == "16"
        for y in range(h):
            x = 0
            limit = w - 1 if y == h - 1 else w
            while x < limit:
                k = y * w + x
                cell = mine[k]
                if cell == theirs[k]:
                    x += 1
                    continue
                out.append(f"\x1b[{y + 1};{x + 1}H")
                while x < limit:
                    k = y * w + x
                    cell = mine[k]
                    if cell == theirs[k]:
                        # A short gap is cheaper to write through than to jump over,
                        # and it keeps words whole in the byte stream.
                        ahead = next((n for n in range(1, 5) if x + n < limit and mine[k + n] != theirs[k + n]), 0)
                        if not ahead:
                            break
                    ch, fg, bg = cell
                    key = (fg, bg)
                    if ch == " " and not bg and state is not None and (state[1] or "49") == "49":
                        key = state         # a plain space needs no colour of its own
                    if key != state:
                        if sixteen or state is None:
                            # Sixteen colours restart from a reset, so bold never leaks.
                            out.append("\x1b[0;" + ";".join(p for p in (fg, bg or "49") if p) + "m")
                        else:
                            parts = []
                            if fg and fg != state[0]:
                                parts.append(fg)
                            if (bg or "49") != (state[1] or "49"):
                                parts.append(bg or "49")
                            if parts:
                                out.append("\x1b[" + ";".join(parts) + "m")
                            key = (fg or state[0], bg)
                        state = key
                    out.append(ch)
                    x += 1
        if not out:
            return ""
        out.append("\x1b[0m")
        return "".join(out)


# --------------------------------------------------------------------------
# The map (S07): a window on the world, drawn as relief in half-block pixels
# where the terminal can show it and in characters where it cannot.
#
# The window holds still. It moves only when the balloon leaves its middle
# half, and then by half a window: a window that followed the balloon would
# be a whole screen to send for every turn.
# --------------------------------------------------------------------------

MAP_CELL_KM = (5.0, 10.0)            # a character is twice as tall as it is wide
MAP_PIXEL_KM = 5.0                   # so half a cell is a square of 5 km
CLOUD_GRID = 4                       # cloud and squall are sampled every fourth pixel and blended between
LABEL_MIN = 4                        # a name shortened to fewer letters than this says nothing
WIND_GRID_KM = 20.0                  # the wind shown is sampled this far apart and blended between
STREAK_CELLS_EACH = 50               # one streak of wind for this many map cells
STREAKS_MIN = 12
STREAK_TRAIL = 5                     # pixels in a streak's tail
STREAK_LIFE = 60                     # frames before a streak starts again somewhere else
STREAK_FAINT, STREAK_BRIGHT = 0.22, 0.6
STREAK_SHARE_MIN = 0.2               # however dear a frame is, a fifth of the streaks stay
FRAME_BYTES = 5500                   # what one frame of moving wind should cost; the streaks thin to keep to it


def streak_step(speed: float) -> float:
    """Pixels a streak moves in a frame, for a wind speed in km/h.

    Faster wind moves faster, but not in proportion: a frame costs what its
    streaks move, and a jet drawn at ten times the surface's speed would
    cost ten times the bytes. 6 km/h is 0.17 of a pixel, 25 is 0.29, 100 is
    0.46.
    """
    return 0.1 + 0.5 * speed / (speed + 40.0)
FRAME_SECONDS = 1 / 7                # the wind moves at seven frames a second
BURST_FRAMES = 10                    # after a turn, and
LOOK_FRAMES = 6                      # after looking at another layer; then it holds still
BURST_BYTES_PER_SECOND = 44 * 1024   # while the wind moves, no more than this goes out in any one second
_MAP_GLYPHS = ("~", "~", ".", ",", '"', ":", "n", "^", "A")
_MAP_ROLES = ("sea", "sea", "text", "land", "land", "text", "dim", "text", "text")
_SEA_RAMP = ((62, 158, 168), (28, 106, 150), (14, 58, 108), (9, 32, 70))
_OFF_WORLD = (9, 32, 70)
_VEIL = ((15, 20, 31), (25, 31, 46))
_CLOUD_RGB, _SQUALL_RGB = (244, 247, 252), (58, 60, 84)


NIGHT_STEPS = ((DUSK_MINUTE - 30, 1 / 3), (DUSK_MINUTE, 2 / 3), (DUSK_MINUTE + 45, 1.0))


def night_level(minute: int) -> float:
    """How dark the map is, from 0 to 1.

    Night falls in three steps around dusk, not smoothly: every change of
    light re-sends the whole map, so there are three of them and no more.
    """
    level = 0.0
    for start, value in NIGHT_STEPS:
        if minute >= start:
            level = value
    return level


def _mix(a, b, t: float):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def sketch_rgb(c) -> tuple:
    """Ground nobody has charted: its own shape, in a few dim greys, as on a map drawn from hearsay.

    The land still shows where it is, and what has been flown over stands
    out from it in colour. A handful of tones, so a veiled sea is one long
    run of a single colour on the wire.
    """
    grey = int((c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11) / 24) * 24
    return (14 + int(grey * 0.30), 18 + int(grey * 0.33), 28 + int(grey * 0.40))


def terrain_rgb(m: int, biome: int):
    """The colour of a piece of ground before the light falls on it."""
    if biome <= SEA_SHALLOW:
        t = min(1.0, -m / 2500.0) * (len(_SEA_RAMP) - 1)
        i = min(len(_SEA_RAMP) - 2, int(t))
        return _mix(_SEA_RAMP[i], _SEA_RAMP[i + 1], t - i)
    if biome == SHORE:
        return _mix((218, 204, 146), (150, 176, 98), min(1.0, m / 30.0))       # sand at the water, green behind it
    if biome == GRASS:
        return _mix((104, 164, 86), (134, 172, 90), min(1.0, m / 500.0))
    if biome == FOREST:
        return _mix((52, 112, 72), (70, 128, 78), min(1.0, m / 500.0))
    if biome == DRY:
        return _mix((204, 168, 104), (186, 142, 92), min(1.0, m / 500.0))
    if biome == HILLS:
        return _mix((164, 152, 98), (138, 116, 92), min(1.0, (m - 500) / 1000.0))
    if biome == MOUNTAIN:
        return _mix((128, 122, 124), (180, 180, 186), min(1.0, (m - 1500) / 1300.0))
    return (244, 247, 250)


class MapView:
    """Draws a window on the world onto a Screen, and says where things are in it."""

    def __init__(self, world: World, weather: Weather, pixels: bool):
        self.world, self.weather, self.pixels = world, weather, pixels
        self._lit: dict[tuple[int, int], list] = {}     # shaded colours per terrain tile
        self._base_key = None
        self._base: tuple[list, list] | None = None
        self.x0 = self.y0 = 0.0
        self.cols = self.rows = 0
        self.left = self.top = 0
        self._taken: set[tuple[int, int]] = set()
        self._wind_key = None
        self._wind: list | None = None
        self._wgw = 0
        self._streaks: list = []
        self._lcg = 1
        self.share = 1.0                  # the part of the streaks drawn: thinned when frames cost too much
        self._settling = False            # the share has just changed: the next frame's cost is not a fair measure

    # -- the window ------------------------------------------------------------

    @staticmethod
    def origin(x: float, y: float, cols: int, rows: int) -> tuple[float, float]:
        """The window's corner for a balloon at (x, y): it moves by half windows."""
        half_w, half_h = cols * MAP_CELL_KM[0] / 2, rows * MAP_CELL_KM[1] / 2
        return (x // half_w) * half_w - half_w / 2, (y // half_h) * half_h - half_h / 2

    def cell_of(self, wx: float, wy: float) -> tuple[int, int] | None:
        """The map cell that holds a place, as (column, row) on the screen, or None outside the window."""
        col, row = int((wx - self.x0) // MAP_CELL_KM[0]), int((wy - self.y0) // MAP_CELL_KM[1])
        if 0 <= col < self.cols and 0 <= row < self.rows:
            return self.left + col, self.top + row
        return None

    # -- terrain in the light --------------------------------------------------

    def _tile(self, tx: int, ty: int) -> list:
        """A tile's colours with the light on them: slopes facing north-west are bright."""
        found = self._lit.get((tx, ty))
        if found is None:
            tile, world, n = self.world.tile(tx, ty), self.world, TILE_N
            elevation, biome = tile.elevation, tile.biome
            bx, by = tx * TILE_KM + SAMPLE_KM / 2, ty * TILE_KM + SAMPLE_KM / 2

            def height(i: int, j: int) -> int:
                if 0 <= i < n and 0 <= j < n:
                    return elevation[j * n + i]
                return world.sample(bx + i * SAMPLE_KM, by + j * SAMPLE_KM)[0]      # over the edge: the neighbour's

            found = []
            for j in range(n):
                for i in range(n):
                    k = j * n + i
                    m = elevation[k]
                    c = terrain_rgb(m, biome[k])
                    if biome[k] >= SHORE:
                        if 0 < i < n - 1 and 0 < j < n - 1:
                            rise = (elevation[k + n + 1] if elevation[k + n + 1] > 0 else 0) \
                                - (elevation[k - n - 1] if elevation[k - n - 1] > 0 else 0)
                        else:
                            rise = max(0, height(i + 1, j + 1)) - max(0, height(i - 1, j - 1))
                        shade = 1.0 + (0.36 if rise > 180 else -0.44 if rise < -220 else rise / 500.0)
                        c = (c[0] * shade, c[1] * shade, c[2] * shade)
                    found.append((int(c[0]) if c[0] < 255 else 255, int(c[1]) if c[1] < 255 else 255,
                                  int(c[2]) if c[2] < 255 else 255))
            # Room for every tile of the world, so the biggest window never
            # throws away what it is in the middle of drawing. Oldest out first.
            while len(self._lit) >= TILES_X * TILES_Y:
                del self._lit[next(iter(self._lit))]
            self._lit[(tx, ty)] = found
        return found

    def _compose(self, day: int, minute: int, charted) -> tuple[list, list]:
        """Top and bottom pixel rows for every map cell: terrain, veil, cloud, squall, night."""
        cols, rows, x0, y0 = self.cols, self.rows, self.x0, self.y0
        step, n = MAP_PIXEL_KM, TILE_N
        frame = self.weather.frame(day, minute)
        # Weather on a coarse grid, blended between: cloud and squall are soft things.
        g = CLOUD_GRID
        gw, gh = cols // g + 2, (rows * 2) // g + 2
        cloud, squall = [], []
        for gy in range(gh):
            wy = y0 + gy * g * step
            for gx in range(gw):
                wx = x0 + gx * g * step
                cover = frame.cloud(wx, wy)[0]
                cloud.append(0.0 if cover < 0.35 else min(1.0, (cover - 0.35) / 0.5) * 0.72)
                squall.append(frame.squall(wx, wy))
        # Most of the sky is clear most of the time: note where there is any weather at all.
        busy = bytearray(gw * gh)
        for gy in range(gh - 1):
            for gx in range(gw - 1):
                k = gy * gw + gx
                if cloud[k] or cloud[k + 1] or cloud[k + gw] or cloud[k + gw + 1] or max(
                        squall[k], squall[k + 1], squall[k + gw], squall[k + gw + 1]) > 0.05:
                    busy[k] = 1
        night = night_level(minute)
        tiles: dict[tuple[int, int], list] = {}
        out: list[list] = []
        for py in range(rows * 2):
            wy = y0 + (py + 0.5) * step
            sy = int(wy // SAMPLE_KM)
            in_y = 0.0 <= wy < WORLD_H
            fy = py / g
            gy0 = int(fy)
            ty_ = fy - gy0
            row = []
            for px in range(cols):
                wx = x0 + (px + 0.5) * step
                if in_y and 0.0 <= wx < WORLD_W:
                    sx = int(wx // SAMPLE_KM)
                    key = (sx // n, sy // n)
                    lit = tiles.get(key)
                    if lit is None:
                        lit = tiles[key] = self._tile(*key)
                    c = lit[(sy % n) * n + sx % n]
                    if charted is not None and not charted(wx, wy):
                        c = sketch_rgb(c)
                else:
                    c = _OFF_WORLD if charted is None else sketch_rgb(_OFF_WORLD)    # nobody has charted past the edge
                k = gy0 * gw + px // g
                if busy[k]:
                    tx_ = px / g - px // g
                    left_a, right_a = cloud[k] * (1 - tx_) + cloud[k + 1] * tx_, cloud[k + gw] * (1 - tx_) + cloud[k + gw + 1] * tx_
                    left_q, right_q = squall[k] * (1 - tx_) + squall[k + 1] * tx_, squall[k + gw] * (1 - tx_) + squall[k + gw + 1] * tx_
                    # In steps, so that cloud drifting a little does not re-send every cell under it.
                    a = int((left_a * (1 - ty_) + right_a * ty_) * 8) / 8
                    q = left_q * (1 - ty_) + right_q * ty_
                    if a:
                        c = _mix(c, _CLOUD_RGB, a)
                    if q > 0.15:
                        c = _mix(c, _SQUALL_RGB, int(min(0.92, q * 1.6) * 6) / 6)
                    c = (int(c[0]), int(c[1]), int(c[2]))
                if night:
                    c = (int(c[0] * (1 - 0.55 * night)), int(c[1] * (1 - 0.5 * night)), int(c[2] * (1 - 0.3 * night)))
                row.append(c)
            out.append(row)
        return out[0::2], out[1::2]

    # -- drawing -----------------------------------------------------------------

    def draw(self, screen: Screen, left: int, top: int, cols: int, rows: int,
             x: float, y: float, day: int, minute: int, charted=None, version=None) -> None:
        """Draw the ground and the weather. Markers go on afterwards with `mark` and `label`.

        charted(wx, wy) says whether a place is on the chart; what is not is
        veiled. version changes whenever the chart does: with one given, the
        picture is composed again only when it, or anything else, has changed.
        """
        self.left, self.top, self.cols, self.rows = left, top, cols, rows
        self.x0, self.y0 = self.origin(x, y, cols, rows)
        self._taken = set()
        if self.pixels:
            key = (day, minute, self.x0, self.y0, cols, rows, charted is not None, version)
            if key != self._base_key or (charted is not None and version is None):
                self._base, self._base_key = self._compose(day, minute, charted), key
            tops, bottoms = self._base
            for r in range(rows):
                screen.pixel_row(left, top + r, tops[r], bottoms[r])
            return
        frame = self.weather.frame(day, minute)
        cw, ch = MAP_CELL_KM
        for r in range(rows):
            wy = self.y0 + (r + 0.5) * ch
            for c in range(cols):
                wx = self.x0 + (c + 0.5) * cw
                if frame.squall(wx, wy) > SQUALL_THRESHOLD:
                    glyph, role = "%", "warn"
                elif charted is not None and not charted(wx, wy):
                    glyph, role = _MAP_GLYPHS[self.world.sample(wx, wy)[1]], "dim"      # sketched in, not surveyed
                else:
                    biome = self.world.sample(wx, wy)[1]
                    glyph, role = _MAP_GLYPHS[biome], _MAP_ROLES[biome]
                screen.put(left + c, top + r, glyph, role)

    # -- the wind ----------------------------------------------------------------

    def set_wind(self, key, sampler) -> bool:
        """Take the wind to show: `sampler(wx, wy)` gives where the air goes, in km/h.

        Sampled on a coarse grid and blended between, once per `key`. Returns
        True when the wind shown has changed.
        """
        key = (key, self.x0, self.y0, self.cols, self.rows)
        if key == self._wind_key:
            return False
        step = WIND_GRID_KM
        self._wgw = int(self.cols * MAP_CELL_KM[0] / step) + 2
        gh = int(self.rows * MAP_CELL_KM[1] / step) + 2
        self._wind = [sampler(self.x0 + gx * step, self.y0 + gy * step) for gy in range(gh) for gx in range(self._wgw)]
        fresh = self._wind_key is None or self._wind_key[1:] != key[1:]
        self._wind_key = key
        if fresh or not self._streaks:
            self._seed_streaks()
        return True

    def _wind_at(self, px: float, py: float) -> tuple[float, float]:
        """The wind at a pixel, blended from the grid."""
        f = MAP_PIXEL_KM / WIND_GRID_KM
        fx, fy = px * f, py * f
        gx, gy = int(fx), int(fy)
        tx, ty = fx - gx, fy - gy
        w, k = self._wind, gy * self._wgw + gx
        a, b, c, d = w[k], w[k + 1], w[k + self._wgw], w[k + self._wgw + 1]
        return ((a[0] * (1 - tx) + b[0] * tx) * (1 - ty) + (c[0] * (1 - tx) + d[0] * tx) * ty,
                (a[1] * (1 - tx) + b[1] * tx) * (1 - ty) + (c[1] * (1 - tx) + d[1] * tx) * ty)

    def _next(self) -> float:
        """A small generator of its own, seeded per window: the streaks are decoration, not chance."""
        self._lcg = (self._lcg * 1103515245 + 12345) & 0x7FFFFFFF
        return self._lcg / 2147483648.0

    def _seed_streaks(self) -> None:
        self._lcg = (int(self.x0) * 7919 + int(self.y0) * 104729 + self.cols * 31 + self.rows) & 0x7FFFFFFF
        count = max(STREAKS_MIN, self.cols * self.rows // STREAK_CELLS_EACH)
        self._streaks = [[self._next() * self.cols, self._next() * self.rows * 2,
                          int(self._next() * STREAK_LIFE), []] for _ in range(count)]
        for _ in range(STREAK_TRAIL * 3):         # run in, so the first picture already shows the flow
            self.advance()

    def advance(self) -> None:
        """Move every streak one frame along the wind."""
        if self._wind is None:
            return
        width, height = self.cols, self.rows * 2
        for streak in self._streaks:
            x, y, age, trail = streak
            vx, vy = self._wind_at(x, y)
            speed = math.sqrt(vx * vx + vy * vy)
            if speed > 0.5:
                k = streak_step(speed) / speed
                x, y = x + vx * k, y + vy * k
            age += 1
            if not (0.0 <= x < width and 0.0 <= y < height) or age > STREAK_LIFE:
                x, y, age = self._next() * width, self._next() * height, 0
                trail.clear()
            cell = (int(x), int(y))
            if not trail or trail[-1] != cell:
                trail.append(cell)
                del trail[:-STREAK_TRAIL]
            streak[0], streak[1], streak[2] = x, y, age

    def tune(self, frame_bytes: int) -> None:
        """Told what the last frame of moving wind cost, draw fewer streaks or more to keep to FRAME_BYTES.

        The cost of a frame depends on the colour depth, the size of the map
        and how fast the wind is, and only the bytes say what it came to.
        """
        if self._settling:
            # The frame after a change of share carries the change itself:
            # streaks rubbed out or drawn whole. That is not what a frame costs.
            self._settling = False
            return
        before = self.share
        if frame_bytes > FRAME_BYTES * 1.3:
            self.share = max(STREAK_SHARE_MIN, self.share * FRAME_BYTES / frame_bytes)
        elif frame_bytes < FRAME_BYTES * 0.6 and self.share < 1.0:
            self.share = min(1.0, self.share * 1.15)
        self._settling = self.share != before

    def settled(self) -> None:
        """A paint that was not a wind frame has gone out: a change of share is paid for."""
        self._settling = False

    def draw_wind(self, screen: Screen) -> None:
        """Lay the streaks over the ground: a bright head and a dimmer tail.

        The tail is one tone, not a fade. With a fade every pixel of a streak
        changes each time its head moves; with one tone only three do: the
        new head, the old head, and the end of the tail.
        """
        if self._wind is None:
            return
        if not self.pixels:
            self._draw_wind_glyphs(screen)
            return
        tops, bottoms = self._base
        left, top = self.left, self.top
        shown = max(1, int(len(self._streaks) * self.share + 0.5))
        for _, _, _, trail in self._streaks[:shown]:
            n = len(trail)
            for i, (px, py) in enumerate(trail):
                base = (bottoms if py & 1 else tops)[py >> 1][px]
                t = STREAK_BRIGHT if i == n - 1 else STREAK_FAINT
                screen.pixel(left + px, top * 2 + py, (int(base[0] + (240 - base[0]) * t),
                                                       int(base[1] + (247 - base[1]) * t),
                                                       int(base[2] + (255 - base[2]) * t)))

    def _draw_wind_glyphs(self, screen: Screen) -> None:
        """The character map's reading of the wind: a line along it, here and there. It holds still."""
        for r in range(1, self.rows, 4):
            for c in range(3 + (r // 4 % 2) * 5, self.cols, 10):
                vx, vy = self._wind_at(c + 0.5, r * 2 + 1.0)
                ax, ay = abs(vx), abs(vy)
                if ax + ay < 4.0:
                    continue                      # nearly calm: say nothing
                if ay * 2.4 < ax:
                    glyph = "-"
                elif ax * 2.4 < ay:
                    glyph = "|"
                else:
                    glyph = "\\" if (vx > 0) == (vy > 0) else "/"
                screen.put(self.left + c, self.top + r, glyph, "text")

    def mark(self, screen: Screen, wx: float, wy: float, glyph: str, ink, force: bool = False):
        """Put a glyph on the place. Returns its cell, or None if it is off the map or the cell is taken."""
        cell = self.cell_of(wx, wy)
        if cell is None or (cell in self._taken and not force):
            return None
        self._taken.add(cell)
        if self.pixels:
            screen.over(cell[0], cell[1], glyph, ink, 0.7)
        else:
            screen.put(cell[0], cell[1], glyph, ink)
        return cell

    def label(self, screen: Screen, cell: tuple[int, int], text: str, ink) -> bool:
        """Write a name beside a marker: to its right if it fits, else to its left,
        else shortened on the roomier side, else not at all.

        A name never runs off the map, never covers a marker, the balloon or
        another name, and keeps a cell clear of the next thing along.
        """
        row, edge_l, edge_r = cell[1], self.left, self.left + self.cols

        def room(step: int) -> int:
            """Free cells beside the marker in one direction, not counting a gap kept before the next thing."""
            n, x = 0, cell[0] + step
            while edge_l <= x < edge_r and (x, row) not in self._taken:
                n, x = n + 1, x + step
            return n if not (edge_l <= x < edge_r) else n - 1      # leave one cell clear of whatever stopped it

        right, left = room(1), room(-1)
        if right >= len(text):
            start = cell[0] + 1
        elif left >= len(text):
            start = cell[0] - len(text)
        else:
            # It fits on neither side whole. A shortened name is better than none.
            side = right if right >= left else left
            if side < LABEL_MIN:
                return False
            text = text[:side]
            start = cell[0] + 1 if right >= left else cell[0] - side
        for i, letter in enumerate(text):
            self._taken.add((start + i, row))
            if self.pixels:
                screen.over(start + i, row, letter, ink, 0.7)
            else:
                screen.put(start + i, row, letter, ink)
        for pad in (start - 1, start + len(text)):                    # the gap, so two names never run together
            if pad != cell[0]:
                self._taken.add((pad, row))
        return True


# --------------------------------------------------------------------------
# The plain flight (S05): the whole turn on an 80 x 24 text screen, to find
# out whether choosing a layer is fun before anything is drawn. Its save is
# one small file per pilot; the world database replaces it in S10.
# --------------------------------------------------------------------------

PLAIN_SAVE_VERSION = 1
PLAIN_HORIZON = 6                    # turns a track looks ahead: the first instruments
PANEL_SMALL, PANEL_WIDE = 26, 40     # columns the panel takes: at 80 x 24, and from 132 x 40 up
SKY_COLS = 8                         # the sky column of the sounding: this wide, two rows a layer
BAR_FULL_KMH = 100.0                 # a full speed bar, the same for every layer
_SKY_RAMP = ((18, 28, 74), (40, 88, 168), (100, 160, 224), (172, 210, 242))
_BAR_RAMP = ((90, 170, 220), (120, 220, 170), (250, 210, 110), (250, 130, 90))
POST_CHOICES = 3                     # destinations on offer at once
STATION_REACH_KM = 150.0             # a town or station sharpens the forecast this far out
_TRACK_ROLES = {"sink": "cool", "hold": "accent", "climb": "warm", "launch": "accent", "jettison": "warm",
                "left": "accent", "right": "accent"}
DROP_KM = 30.0                       # mail can be dropped this close to where it is bound
DROP_SHARE = 0.6                     # of the payout: a bag thrown down is not a bag handed over
DROP_ABOVE = 1                       # and from no more than this many layers above the lowest
WAIT_TURNS = 4                       # an hour on the ground, waiting for the wind
FORWARD_SHARE = 0.5                  # of the payout, times the part of the way it was carried: cargo sent on by road


def data_dir(environ) -> Path:
    """Where this installation keeps its world."""
    named = environ.get("DOOR_FW_DATA_DIR")
    return Path(named) if named else Path(__file__).resolve().parent / "data"


def want_pixels(launch: Launch, environ) -> bool:
    """Relief in half-block pixels, or the character map?

    Pixels need colour to tell land from sea and a charset with the half
    block. Sixteen colours and plain ASCII get the character map, which is a
    reading made for them, not a degraded picture. DOOR_FW_PIXELS overrides:
    `half` forces pixels, `shade` forces characters.
    """
    choice = environ.get("DOOR_FW_PIXELS", "auto").strip().lower()
    if choice == "half":
        return launch.charset != "ascii"
    if choice == "shade":
        return False
    return launch.color_depth != "16" and launch.charset != "ascii"


def want_motion(launch: Launch, environ) -> bool:
    """Does the wind move? DOOR_FW_MOTION says: `auto` and `burst` yes, `off` no. DOOR_SCENES=0 means no too."""
    if environ.get("DOOR_SCENES", "").strip() == "0":
        return False
    return environ.get("DOOR_FW_MOTION", "auto").strip().lower() != "off"


def clock_text(minute: int) -> str:
    minute %= MINUTES_PER_DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


def bearing(dx: float, dy: float) -> str:
    """The compass point that lies in the direction (dx, dy)."""
    return compass(-dx, -dy)


# --------------------------------------------------------------------------
# The calendar (S11): play is paced by world days, never by calls. A world
# day opens at a fixed local time for everyone; a pilot flies the days that
# have opened, in order, each once.
# --------------------------------------------------------------------------

TEMPOS = (1, 2, 3, 4)                # world days in a real day
WAITING_DAYS = 3                     # opened days a pilot may have in hand, the one being flown included
CALENDAR_CHECK_SECONDS = 20.0        # how often an idle session looks whether a day has opened
_zones: dict = {}


def local_time(now: float, zone: str) -> datetime.datetime:
    """The wall-clock time in a zone, without a zone attached: dates and times of day are all the calendar needs.

    An IANA name is looked up; "local", or a name this host has no data
    for, means the host's own zone. A BBS host is nearly always in its
    node's zone, and a wrong hour for a day to open is better than no game.
    """
    name = (zone or "UTC").strip()
    if name.upper() in ("UTC", "Z", "GMT"):
        return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(tzinfo=None)
    if name != "local":
        if name not in _zones:
            try:
                _zones[name] = zoneinfo.ZoneInfo(name)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                _zones[name] = None
                diagnose(f"no time zone data for {name!r}: using this host's own zone")
        if _zones[name] is not None:
            return datetime.datetime.fromtimestamp(now, _zones[name]).replace(tzinfo=None)
    return datetime.datetime.fromtimestamp(now)


def world_day(local: datetime.datetime, epoch_date: datetime.date, epoch_day: int, tempo: int) -> int:
    """The world day a local wall-clock time falls in.

    Local dates are counted, not hours, so a day of 23 or 25 hours is one
    day like any other. With a tempo above one each date holds that many
    equal slots by the clock on the wall.
    """
    dates = (local.date() - epoch_date).days
    slot = min(tempo - 1, (local.hour * 3600 + local.minute * 60 + local.second) * tempo // 86400)
    return epoch_day + dates * tempo + slot


def day_opens(day: int, epoch_date: datetime.date, epoch_day: int, tempo: int) -> datetime.datetime:
    """The local wall-clock time at which a world day opens."""
    n = day - epoch_day
    return (datetime.datetime.combine(epoch_date + datetime.timedelta(days=n // tempo), datetime.time())
            + datetime.timedelta(seconds=(n % tempo) * 86400 // tempo))


def new_calendar(local: datetime.datetime) -> dict:
    """A calendar that begins today: world day 1 is the local date of its making."""
    return {"epoch_date": local.date().isoformat(), "epoch_day": 1, "tempo": 1, "high_day": 1}


def read_calendar(values: dict) -> dict | None:
    """A calendar from stored text, or None if it is not all there or not sound."""
    try:
        cal = {"epoch_date": str(values["epoch_date"]), "epoch_day": int(values["epoch_day"]),
               "tempo": int(values["tempo"]), "high_day": int(values["high_day"])}
        datetime.date.fromisoformat(cal["epoch_date"])
    except (KeyError, ValueError, TypeError):
        return None
    return cal if cal["tempo"] in TEMPOS and cal["high_day"] >= 1 else None


def calendar_day(cal: dict, local: datetime.datetime) -> int:
    """Today, by a calendar: never less than the highest day it has opened, so a clock set back repeats nothing."""
    computed = world_day(local, datetime.date.fromisoformat(cal["epoch_date"]), cal["epoch_day"], cal["tempo"])
    return max(computed, cal["high_day"])


def calendar_opens(cal: dict, day: int) -> datetime.datetime:
    return day_opens(day, datetime.date.fromisoformat(cal["epoch_date"]), cal["epoch_day"], cal["tempo"])


def retimed(cal: dict, tempo: int, local: datetime.datetime) -> dict:
    """The calendar with another tempo from today on. Today stays today; no day is skipped or repeated."""
    today = calendar_day(cal, local)
    # Counted so that the slot the clock is in now is today: the next day
    # opens at the next slot, never at the moment of the change.
    slot = world_day(local, local.date(), 0, tempo)
    return {"epoch_date": local.date().isoformat(), "epoch_day": today - slot, "tempo": tempo, "high_day": today}


def when_text(opens: datetime.datetime, local: datetime.datetime, date_format: str) -> str:
    """"at midnight", "at 12:00", or with its date when it is more than a day off."""
    clock = "midnight" if (opens.hour, opens.minute) == (0, 0) else f"{opens.hour:02d}:{opens.minute:02d}"
    if opens - local <= datetime.timedelta(days=1):
        return f"at {clock}"
    try:
        date = opens.strftime(date_format.split(" ")[0] or "%d.%m.%Y")
    except ValueError:
        date = opens.strftime("%d.%m.%Y")
    return f"on {clean_text(date, 24)} at {clock}"


# --------------------------------------------------------------------------
# The post (S12): contracts, cargo, postmarks and stores. Every amount is in
# the tables below; the code only reads them.
# --------------------------------------------------------------------------

START_POSTMARKS = 40                 # what a new pilot has in hand
LIFT = 8                             # the weight a balloon carries
PRICES = {"fuel": 1, "sack": 4, "pibal": 6, "mend": 1}     # in postmarks, at a town: a unit of fuel, a sack of
                                                           # ballast, a pibal, a point of wear mended
STATION_PRICE = 1.5                  # a weather station has less to sell and asks half as much again
STORES_FULL = {"fuel": FUEL_START, "sack": BALLAST_START, "pibal": PIBALS_START}
CARGO_KINDS = {
    # weight carried; payout = base + per_km x distance; days to deliver (one more for every 300 km);
    # what is forfeit if it is lost; and its share of what a town offers
    "mail":    {"name": "Mailbag", "weight": 1, "base": 12, "per_km": 0.20, "days": 3, "fragile": False, "lost": 5, "share": 50},
    "parcel":  {"name": "Parcel", "weight": 3, "base": 20, "per_km": 0.40, "days": 4, "fragile": False, "lost": 15, "share": 32},
    "fragile": {"name": "Instruments", "weight": 2, "base": 28, "per_km": 0.55, "days": 4, "fragile": True, "lost": 25, "share": 18},
}
LATE_SHARE = 0.5                     # of the payout, for cargo delivered after its day
DAMAGED_SHARE = 0.5                  # of the payout, for instruments shaken by a rough landing
OFFER_REACH = 6                      # a contract is for one of this many nearest places
TOWN_OFFERS = (3, 5)                 # contracts a town offers in a day: at least, at most
STATION_OFFERS = 2                   # a station has a little mail and nothing else
FIRST_ROUND_DAYS = 5                 # a new pilot's first three mailbags are due this many days on
# The relief run: what the postmaster advances a pilot who cannot pay, so that nobody is ever stuck.
RELIEF = {"below": 10, "fuel": 25, "wear": WEAR_GROUNDED - 30, "sack": 1, "payout": 8, "days": 5}


def places_near(world: "World", x: float, y: float, skip=None, stations=None) -> list[tuple[str, int]]:
    """Every town and every station with a post office, nearest first.

    stations is the world's first three and whatever has been planted since,
    in that order; a planted station has a post office once it is a mast.
    """
    stations = world.stations if stations is None else stations
    refs = [("town", i) for i in range(len(world.towns))]
    refs += [("station", i) for i, station in enumerate(stations) if staffed(station)]

    def far(ref):
        place = (world.towns if ref[0] == "town" else stations)[ref[1]]
        return ((place["x"] - x) ** 2 + (place["y"] - y) ** 2, ref)
    return sorted((r for r in refs if r != skip), key=far)


def contracts_at(world: "World", ref: tuple[str, int], day: int, stations=None) -> list[dict]:
    """What a place's post office offers on a world day. The same for every caller, and the same every time."""
    stations = world.stations if stations is None else stations
    here = (world.towns if ref[0] == "town" else stations)[ref[1]]
    near = places_near(world, here["x"], here["y"], skip=ref, stations=stations)[:OFFER_REACH]
    if not near:
        return []
    key = ("contract", ref[0], ref[1], day)
    if ref[0] == "town":
        low, high = TOWN_OFFERS
        count = low + int(draw_unit(world.seed, *key, "count") * (high - low + 1))
    else:
        count = STATION_OFFERS
    # Each offer is for a different place, so the day's wind has several to choose among.
    order = sorted(range(len(near)), key=lambda j: draw_unit(world.seed, *key, "order", j))
    shares = sum(kind["share"] for kind in CARGO_KINDS.values())
    out = []
    for i in range(min(count, len(near))):
        code = "mail"
        if ref[0] == "town" and i > 0:          # a town's first offer is always a mailbag: something for anyone
            mark = draw_unit(world.seed, *key, i, "kind") * shares
            for code, kind in CARGO_KINDS.items():
                mark -= kind["share"]
                if mark < 0:
                    break
        kind = CARGO_KINDS[code]
        dest = near[order[i]]
        there = (world.towns if dest[0] == "town" else stations)[dest[1]]
        km = math.sqrt((there["x"] - here["x"]) ** 2 + (there["y"] - here["y"]) ** 2)
        out.append({"contract": [ref[0], ref[1], day, i], "kind": code, "dest": list(dest), "origin": list(ref),
                    "weight": kind["weight"], "payout": int(kind["base"] + kind["per_km"] * km + 0.5),
                    "due": day + kind["days"] + int(km // 300)})
    return out


def price_of(item: str, ref: tuple[str, int]) -> int:
    """What one of something costs at a place, in postmarks."""
    return PRICES[item] if ref[0] == "town" else int(math.ceil(PRICES[item] * STATION_PRICE))


# --------------------------------------------------------------------------
# The licence and the balloon (S13). Tables again: grades, what earns
# points, and every tier of every piece of gear with what it does.
# --------------------------------------------------------------------------

GRADES = (("Fledgling", 0), ("Aeronaut", 60), ("Pilot", 160), ("Navigator", 360), ("Wind-reader", 700),
          ("Master", 1200), ("Commodore of the Post", 2000))          # the name, and the points it takes
POINTS = {"mail": 8, "parcel": 14, "fragile": 20, "clean": 3, "km": 0.1}     # a delivery by kind, a clean landing, a km
CLEAN_LANDING_KM = 25.0              # a clean landing counts when it ends a flight at least this far from its launch
CARGO_GRADE = {"mail": 0, "parcel": 1, "fragile": 2}                  # the grade a kind of cargo is entrusted at
# Each track's first tier is what every balloon starts with. A later tier names its price in postmarks, the
# grade it is sold at, and sometimes a tier of another track it needs first.
UPGRADES = {
    "envelope": {"name": "Envelope", "tiers": (
        {"lift": 8, "ceiling": 3},
        {"price": 150, "grade": 1, "lift": 11, "ceiling": 4},
        {"price": 450, "grade": 3, "lift": 14, "ceiling": 5})},
    "burner": {"name": "Burner", "tiers": (
        {"climb": COST_CLIMB},
        {"price": 120, "grade": 1, "climb": 2},
        {"price": 380, "grade": 3, "climb": 1})},
    "tanks": {"name": "Tanks", "tiers": (
        {"tank": FUEL_START},
        {"price": 100, "grade": 1, "tank": 90},
        {"price": 320, "grade": 3, "tank": 130})},
    "instruments": {"name": "Instruments", "tiers": (
        {"horizon": 6, "reads": 0},
        {"price": 140, "grade": 2, "horizon": 10, "reads": 1},
        {"price": 420, "grade": 4, "horizon": 16, "reads": LAYERS})},
    "wireless": {"name": "Wireless", "tiers": (
        {"reach": 150.0},
        {"price": 200, "grade": 2, "reach": 300.0})},
    "night": {"name": "Night gear", "tiers": (
        {"night": False},
        {"price": 180, "grade": 2, "night": True})},
    "oxygen": {"name": "Oxygen", "tiers": (
        {"oxygen": False},
        {"price": 300, "grade": 4, "oxygen": True, "needs": ("envelope", 2)})},
    "camera": {"name": "Camera", "tiers": (
        {"range": 1.0},
        {"price": 160, "grade": 2, "range": 1.5})},
}
TRACKS = tuple(UPGRADES)
TIER_NAMES = ("I", "II", "III")
PATTERNS = ("stripes", "halves", "chevrons", "rings")
# The colours an envelope may be painted in. Each reads as a different one of the sixteen.
ENVELOPE_COLOURS = (("teal", (44, 172, 172)), ("cream", (246, 232, 202)), ("coral", (240, 112, 92)),
                    ("navy", (36, 52, 160)), ("gold", (236, 188, 40)), ("green", (24, 170, 40)),
                    ("plum", (150, 60, 160)), ("charcoal", (72, 72, 80)))
FIRST_DESIGN = {"pattern": "stripes", "colours": [0, 1, 2, 1]}
OTHERS_SHOWN = 24                    # other pilots' balloons drawn on a map at most


def gear_text(track: str, tier: dict) -> str:
    """What a tier of gear does, in a few words."""
    if track == "envelope":
        return f"lifts {tier['lift']}, reaches layer {tier['ceiling']}"
    if track == "burner":
        return f"a climb costs {tier['climb']} fuel"
    if track == "tanks":
        return f"hold {tier['tank']} fuel"
    if track == "instruments":
        reads = "reads every layer" if tier["reads"] >= LAYERS else \
            ("reads your layer", "reads the layers next to yours")[min(tier["reads"], 1)]
        return f"tracks {tier['horizon']} turns ahead, {reads}"
    if track == "wireless":
        return f"forecasts from places {tier['reach']:.0f} km off"
    if track == "night":
        return "better night landings, no night fuel penalty" if tier["night"] else "none"
    if track == "camera":
        return "wonders from half as far again" if tier["range"] > 1.0 else "none"
    return f"layer {LAYERS}" if tier["oxygen"] else "none"


def grade_of(points: int) -> int:
    """The grade that many points make: an index into GRADES."""
    return max((i for i, (_, need) in enumerate(GRADES) if points >= need), default=0)


def design_of(value) -> dict:
    """An envelope design from stored data, or the first design if it is not one."""
    try:
        pattern, colours = value["pattern"], [int(c) for c in value["colours"]]
        if pattern in PATTERNS and len(colours) == 4 and all(0 <= c < len(ENVELOPE_COLOURS) for c in colours):
            return {"pattern": pattern, "colours": colours}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass                              # ArithmeticError: a stored number may be an infinity
    return {"pattern": FIRST_DESIGN["pattern"], "colours": list(FIRST_DESIGN["colours"])}


def design_rgb(design: dict) -> tuple:
    return tuple(ENVELOPE_COLOURS[c][1] for c in design["colours"])


# --------------------------------------------------------------------------
# The chart (S14): the map is blank beyond the places until somebody has
# flown over it. What one pilot sees from the basket is charted for
# everyone, in that pilot's name.
# --------------------------------------------------------------------------

SIGHT_KM = (10.0, 20.0, 30.0, 40.0, 50.0, 50.0)   # how far the ground is seen from each layer, lowest first
CLOUD_BLOCKS = 0.6                   # cloud cover over a cell, with its deck below the balloon, hides the cell
HOME_SIGHT_KM = 30.0                 # the ground this close to a town or a station was known from the start
CHART_PER_POSTMARK = 2               # cells of land charted first for a postmark
CHART_PER_POINT = 5                  # and for a point on the licence
DISCOVERY_SHARE = 0.6                # a feature is found when this much of it is charted
DISCOVERY_PAY, DISCOVERY_POINTS = 25, 10
FEATURE_WORDS = {
    "lake": (" Tarn", " Water", " Mere", " Lake"), "island": (" Isle", " Holm", " Skerry"),
    "peak": (" Peak", " Horn", " Tor"), "plain": (" Flats", " Leas", " Reach"),
    "forest": (" Wood", " Weald", " Holt"), "dry plain": (" Barrens", " Waste", " Pans"),
    "hills": (" Downs", " Fells", " Hills"), "range": (" Range", " Teeth", " Heights"),
}
FEATURE_LABELS = 8                   # names of features drawn on a map at most
ATLAS_ZOOMS = (1, 2, 4)              # the chart shows the whole continent, a half of it across, or a quarter
ISOBAR_HPA = 4.0                     # an isobar at every multiple of this
ATLAS_PANEL, ATLAS_PANEL_FROM = 38, 120      # the chart's side panel: its width, and the screen width it needs
ROUTE_KEPT = 40                      # where the last so many flights ended: the route drawn on the chart
FIND_LIMIT = 24                      # characters in a name looked for
ISOBAR_DEEP = (992.0, 20.0)          # below this pressure, an isobar at every so many hPa: the Gyre's core is steep
_ISOBAR_RGB = (236, 242, 252)


def isobar_level(pressure: float) -> int:
    """Which band between two isobars a pressure lies in. An isobar runs wherever two neighbours differ."""
    deep, step = ISOBAR_DEEP
    if pressure >= deep:
        return int(pressure // ISOBAR_HPA)
    return int(deep // ISOBAR_HPA) - 1 - int((deep - pressure) // step)


def cell_at(x: float, y: float) -> int | None:
    """The chart cell a place in the world falls in, or None off the chart."""
    if not (0.0 <= x < WORLD_W and 0.0 <= y < WORLD_H):
        return None
    return int(y / CHART_KM) * CHART_W + int(x / CHART_KM)


def cells_within(x: float, y: float, km: float) -> list[int]:
    """The chart cells whose centres lie within so many km of a place."""
    out = []
    reach = int(km / CHART_KM) + 1
    cx, cy = int(x / CHART_KM), int(y / CHART_KM)
    for gy in range(max(0, cy - reach), min(CHART_H, cy + reach + 1)):
        for gx in range(max(0, cx - reach), min(CHART_W, cx + reach + 1)):
            dx, dy = (gx + 0.5) * CHART_KM - x, (gy + 0.5) * CHART_KM - y
            if dx * dx + dy * dy <= km * km:
                out.append(gy * CHART_W + gx)
    return out


def known_cells(world: "World") -> frozenset:
    """What was on the map before anybody flew: the ground around every town and station."""
    if getattr(world, "_known", None) is None:
        cells = set()
        for place in world.towns + world.stations:
            cells.update(cells_within(place["x"], place["y"], HOME_SIGHT_KM))
        world._known = frozenset(cells)
        world._land = frozenset(k for k in range(CHART_W * CHART_H) if world.chart_biome[k] >= SHORE)
        by_cell: dict = {}
        for feature in world.features:
            for cell in feature["cells"]:
                by_cell.setdefault(cell, []).append(feature["id"])
        world._features_at = by_cell
    return world._known


def feature_names(world: "World", feature: dict) -> list[str]:
    """Three names a feature may be given. Composed from word lists by keyed draws: no caller's text is in them."""
    words = FEATURE_WORDS[feature["kind"]]
    names: list[str] = []
    for attempt in range(60):
        first = _NAME_FIRST[draw(world.seed, "feature-name", feature["id"], attempt, 0) % len(_NAME_FIRST)]
        last = words[draw(world.seed, "feature-name", feature["id"], attempt, 1) % len(words)]
        if first + last not in names:
            names.append(first + last)
        if len(names) == 3:
            break
    return names


# --------------------------------------------------------------------------
# Stations and masts (S15). A pilot who comes down in the wild with a kit
# aboard may plant a weather station there: the forecast is sharper around
# it for everyone, for as long as somebody keeps it stocked. Crates carried
# out from the towns raise a station into a mooring mast, which the Post
# then keeps for good: a lit field, fuel, and a post office.
# --------------------------------------------------------------------------

STATION_APART_KM = 60.0              # a station is planted at least this far from every town and station
STOCK_FULL = 14                      # days of stock a station holds; it uses one each world day
STOCK_SHARP = 7                      # with this much or more it reports in full; with less, in proportion
KIT_PRICE, KIT_WEIGHT = 50, 3        # a station kit, at a town: postmarks, and what it weighs aboard
PLANT_POINTS = 25                    # on the licence, for planting one
RESUPPLY_PAY = 2                     # postmarks for each day of stock put back
RESUPPLY_POINTS = 0.5                # and licence points
CRATE_PRICE, CRATE_WEIGHT = 10, 3    # a crate of mast freight, at a town
CRATE_PAY, CRATE_POINTS = 25, 10     # for each crate unloaded at a station
MAST_CRATES = 4                      # crates that raise a mast
STATION_WORDS = (" Hut", " Beacon", " Vane", " Lookout")


def staffed(station: dict) -> bool:
    """Is this a place of the Post: one of the world's first stations, or a mast?"""
    return "id" not in station or station["mast_day"] is not None


def stock_of(station: dict, today: int) -> int:
    """The days of stock a planted station has left."""
    return max(0, station["stock"] - max(0, today - station["stock_day"]))


def station_name(world: "World", x: float, y: float, used: set) -> str:
    """A name for a station planted here. Composed from word lists by keyed draws: no caller's text is in it."""
    cell = cell_at(x, y) or 0
    for attempt in range(200):
        first = _NAME_FIRST[draw(world.seed, "planted", cell, attempt, 0) % len(_NAME_FIRST)]
        name = first + STATION_WORDS[draw(world.seed, "planted", cell, attempt, 1) % len(STATION_WORDS)]
        if name not in used:
            return name
    return f"Station {len(used) + 1}"


def read_stations(db) -> list[dict]:
    """Every planted station, in the order they were planted."""
    given: dict = {}
    for sid, handle in db.execute("SELECT station, handle FROM contributions ORDER BY crates DESC, user_id"):
        given.setdefault(sid, []).append(handle)
    return [{"id": r[0], "name": r[1], "x": r[2], "y": r[3], "handle": r[4], "day": r[5], "stock": r[6],
             "stock_day": r[7], "freight": r[8], "mast_day": r[9], "given": given.get(r[0], [])}
            for r in db.execute("SELECT id, name, x, y, handle, day, stock, stock_day, freight, mast_day"
                                " FROM stations ORDER BY id")]


def plant_station(db, world: "World", x: float, y: float, user_id: int, handle: str, day: int) -> dict | None:
    """Plant a station here, inside the caller's transaction. None if another has been planted too near."""
    rows = db.execute("SELECT name, x, y FROM stations").fetchall()
    if any((x - px) ** 2 + (y - py) ** 2 < STATION_APART_KM ** 2 for _, px, py in rows):
        return None
    if (x - world.gyre["x"]) ** 2 + (y - world.gyre["y"]) ** 2 <= GYRE_INNER_KM ** 2:
        return None                             # nothing stands inside the Gyre's inner band
    used = {place["name"] for place in world.towns + world.stations} | {row[0] for row in rows}
    # The next number in line. Stations are never deleted, a silent one included, so the numbers have no gaps:
    # a station's number is its place in every session's list, and cargo bound for a mast is addressed by it.
    sid, name = len(world.stations) + len(rows), station_name(world, x, y, used)
    db.execute("INSERT INTO stations (id, name, x, y, user_id, handle, day, stock, stock_day)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (sid, name, x, y, user_id, handle, day, STOCK_FULL, day))
    return {"id": sid, "name": name}


def visit_station(db, sid: int, user_id: int, handle: str, day: int, crates: int) -> dict | None:
    """A pilot is down at a planted station: restock it, and unload what crates it still wants.

    Inside the caller's transaction, so stock is put back once and a mast
    is raised once however many pilots arrive together. None if there is
    no such station, or it is a mast already and wants nothing.
    """
    row = db.execute("SELECT stock, stock_day, freight, mast_day FROM stations WHERE id=?", (sid,)).fetchone()
    if row is None or row[3] is not None:
        return None
    have = max(0, row[0] - max(0, day - row[1]))
    took = max(0, min(crates, MAST_CRATES - row[2]))
    raised = took > 0 and row[2] + took >= MAST_CRATES
    db.execute("UPDATE stations SET stock=?, stock_day=?, freight=?, mast_day=? WHERE id=?",
               (STOCK_FULL, max(day, row[1]), row[2] + took, day if raised else None, sid))
    if took:
        db.execute("INSERT INTO contributions VALUES (?, ?, ?, ?) ON CONFLICT (station, user_id) DO UPDATE SET"
                   " crates=crates+excluded.crates, handle=excluded.handle", (sid, user_id, handle, took))
    return {"restored": STOCK_FULL - have, "silent": have == 0, "took": took, "raised": raised,
            "wants": MAST_CRATES - row[2] - took}


# --------------------------------------------------------------------------
# Stranded and rescued, and the visitors' books (S16). A pilot down in the
# wild who cannot fly is on every pilot's map until they walk out or
# somebody lands beside them with fuel. Every station keeps a book that a
# pilot on the ground there may sign, one line a world day.
# --------------------------------------------------------------------------

RESCUE_FUEL = 15                     # a rescuer brings the stranded pilot's fuel up to this, from their own tank
RESCUE_PAY, RESCUE_POINTS = 40, 20   # what the Post gives the rescuer
GUESTBOOK_LIMIT = 60                 # characters in a line
GUESTBOOK_SHOWN = 12                 # lines of a book read at once: the latest
GUESTBOOK_MODES = ("on", "canned", "off")
CANNED = ("Fair winds to whoever reads this.", "Came in on the last of the fuel.", "A fine view from here.",
          "The wind brought me. I had other plans.", "Stock is full. You are welcome.", "Blown off course, and glad of it.",
          "Mind the high ground to windward.", "Passing through. Clear skies.")


def book_text(value: str) -> str:
    """A line fit for a visitors' book: printable, and one cell of the screen to a character.

    Accents written as one letter stay (a caller may sign in their own
    language). Marks that stack on a letter, and characters two cells
    wide, go: a line of them would push the screen's edge about.
    """
    kept = "".join(ch for ch in value if not unicodedata.category(ch).startswith("M")
                   and unicodedata.east_asian_width(ch) not in ("W", "F"))
    return clean_text(kept, GUESTBOOK_LIMIT)


def need_text(fuel: int, repair) -> str:
    """What a stranded pilot is waiting for, in a few words."""
    parts = ([f"{fuel} fuel"] if fuel else []) + (["patches"] if repair else [])
    return " and ".join(parts) or "a hand"


def read_calls(db) -> list[dict]:
    """Every pilot who is stranded and has not walked out."""
    return [{"user_id": r[0], "handle": r[1], "x": r[2], "y": r[3], "day": r[4], "fuel": r[5], "repair": r[6],
             "by_user": r[7], "by_handle": r[8]}
            for r in db.execute("SELECT user_id, handle, x, y, day, fuel, repair, by_user, by_handle FROM calls"
                                " ORDER BY user_id")]


def call_for_help(db, user_id: int, handle: str, x: float, y: float, day: int, fuel: int, repair: int) -> bool:
    return db.execute("INSERT OR IGNORE INTO calls (user_id, handle, x, y, day, fuel, repair)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, handle, x, y, day, fuel, repair)).rowcount == 1


def end_call(db, user_id: int) -> dict | None:
    """Take a pilot's call down, and say what had come of it: once, since the row is gone after."""
    row = db.execute("SELECT fuel, repair, by_user, by_handle FROM calls WHERE user_id=?", (user_id,)).fetchone()
    db.execute("DELETE FROM calls WHERE user_id=?", (user_id,))
    return None if row is None else {"fuel": row[0], "repair": row[1], "by_user": row[2], "by_handle": row[3]}


def answer_call(db, who: int, by_user: int, by_handle: str, spare: int = 10 ** 6) -> dict | None:
    """Reach a stranded pilot. None if somebody already has, or they have gone: a call is answered once.

    spare is the fuel the rescuer can give. The call as it stands in the
    records is the one answered, not the one the rescuer last read; if it
    now wants more than can be spared it is left for somebody else.
    """
    row = db.execute("SELECT fuel, repair FROM calls WHERE user_id=? AND by_user IS NULL AND fuel<=?",
                     (who, spare)).fetchone()
    if row is None:
        return None
    db.execute("UPDATE calls SET by_user=?, by_handle=? WHERE user_id=?", (by_user, by_handle, who))
    return {"fuel": row[0], "repair": row[1]}


def read_book(db, station: int, limit: int = GUESTBOOK_SHOWN) -> list[tuple]:
    """The latest lines of a station's book, newest first: (day, handle, text)."""
    return db.execute("SELECT day, handle, text FROM guestbook WHERE station=? ORDER BY id DESC LIMIT ?",
                      (station, limit)).fetchall()


def sign_book(db, station: int, user_id: int, handle: str, day: int, text: str) -> bool:
    """One line for each pilot, station and world day. False if today's is written."""
    return db.execute("INSERT OR IGNORE INTO guestbook (station, user_id, handle, day, text) VALUES (?, ?, ?, ?, ?)",
                      (station, user_id, handle, day, book_text(text))).rowcount == 1


def guestbook_lines(folder: Path) -> list[str]:
    """Every line in every book of a data folder, for the SysOp: each with the name to remove it by."""
    lines = []
    for path in sorted(Path(folder).glob("fair_winds-*.db")):
        world = path.stem[len("fair_winds-"):]
        if len(world) != 16 or any(c not in "0123456789abcdef" for c in world):
            continue                            # not named as a world is: --guestbook-remove could not name it
        db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SECONDS)
        try:
            rows = db.execute("SELECT id, station, day, handle, text FROM guestbook ORDER BY id").fetchall()
        except sqlite3.Error as error:
            lines.append(f"{path.name}: skipped ({error})")
            continue
        finally:
            db.close()
        for row in rows:
            lines.append(f"{path.stem[-16:]}:{row[0]}  station {row[1]}  Day {row[2]}  "
                         f"{clean_text(row[3], 32)}: {clean_text(row[4], GUESTBOOK_LIMIT)}")
    return lines


def guestbook_remove(folder: Path, name: str) -> str:
    """Remove one line, named as --guestbook lists it: the world's sixteen digits, a colon, the line's number."""
    world, _, number = name.partition(":")
    if len(world) != 16 or any(c not in "0123456789abcdef" for c in world) or not number.isdigit():
        raise ValueError(f"{name!r} is not a line as --guestbook lists them (world:number)")
    path = Path(folder) / f"fair_winds-{world}.db"
    if not path.exists():
        raise ValueError(f"there is no world {world} here")
    db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SECONDS)
    try:
        gone = db.execute("DELETE FROM guestbook WHERE id=?", (int(number),)).rowcount
        db.commit()
    finally:
        db.close()
    if not gone:
        raise ValueError(f"world {world} has no line {number}")
    return f"removed {name}"


# --------------------------------------------------------------------------
# The almanac (S18). The land makes winds of its own that come back: a sea
# breeze on a coast every afternoon, air draining off steep ground at dawn,
# a rush through a gap whenever the flow is strong. Each world has twelve
# to twenty such places. A pilot who flies one while it blows, on three
# different days, enters it in the almanac under their name, and from then
# on every pilot's map shows it whenever it blows.
# --------------------------------------------------------------------------

WIND_COUNT = (12, 20)                # local winds in a world: at least, at most
WIND_APART_KM = 110.0                # no two of them nearer than this
WIND_REACH_KM = 30.0                 # a local wind is flown within this of its middle
WIND_LAYERS = 2                      # and no higher than this layer: they are winds of the ground
WIND_DAYS = 3                        # days a pilot has to have flown one to enter it
PASS_KMH = 18.0                      # a pass wind blows when the lowest layer runs this fast through the gap
SEA_BREEZE_AT, DOWNSLOPE_AT = 0.5, 0.25      # of their full strength: when they count as blowing
ALMANAC_PAY, ALMANAC_POINTS = 30, 15
WIND_KINDS = {
    "pass": {"what": "pass wind", "when": "when the air runs fast through the gap",
             "words": (" Funnel", " Rush", " Gate")},
    "sea": {"what": "sea breeze", "when": "onshore, 13:00 to 17:00",
            "words": (" Breeze", " Doctor", " Onshore")},
    "fall": {"what": "downslope wind", "when": "off the slopes, until 07:45",
             "words": (" Fall", " Drain", " Bora")},
}


def local_winds(world: "World", weather: "Weather") -> list[dict]:
    """The world's local winds: where each is, what kind, and the name it will bear. The same every time.

    Picked by turns from the strongest gaps, coasts and slopes that lie
    low enough to be flown in the lowest layer, each well apart from the
    others. Not stored: the land and the seed say where they are. The
    records know a wind by its number here, so a change to how they are
    picked moves the winds of every world already begun: a test holds one
    world's winds to what they were.
    """
    if getattr(world, "_winds", None) is None:
        low, picked, used = LAYER_BLOCK_M[0], [], set()
        pools: dict = {"pass": [], "sea": [], "fall": []}
        for k in range(CHART_W * CHART_H):
            gx, gy = k % CHART_W, k // CHART_W
            if not (3 <= gx < CHART_W - 3 and 3 <= gy < CHART_H - 3) or world.chart_elevation[k] >= low:
                continue
            jitter = draw_unit(world.seed, "wind", k) * 0.2
            if weather.gap[k]:
                pools["pass"].append((1.0 + jitter, k))
            coast = math.sqrt(weather.coast[k][0] ** 2 + weather.coast[k][1] ** 2)
            if coast > 0.8:
                pools["sea"].append((coast + jitter, k))
            slope = math.sqrt(weather.slope[k][0] ** 2 + weather.slope[k][1] ** 2)
            if slope > 0.25:
                pools["fall"].append((slope + jitter, k))
        for pool in pools.values():
            pool.sort(reverse=True)
        want = WIND_COUNT[0] + draw(world.seed, "wind-count") % (WIND_COUNT[1] - WIND_COUNT[0] + 1)
        turn = 0
        while len(picked) < want and any(pools.values()):
            kind = ("pass", "sea", "fall")[turn % 3]
            turn += 1
            while pools[kind]:
                _, k = pools[kind].pop(0)
                x, y = (k % CHART_W + 0.5) * CHART_KM, (k // CHART_W + 0.5) * CHART_KM
                if any((x - w["x"]) ** 2 + (y - w["y"]) ** 2 < WIND_APART_KM ** 2 for w in picked):
                    continue
                words, name = WIND_KINDS[kind]["words"], ""
                for attempt in range(200):
                    name = "The " + _NAME_FIRST[draw(world.seed, "wind-name", k, attempt, 0) % len(_NAME_FIRST)] \
                        + words[draw(world.seed, "wind-name", k, attempt, 1) % len(words)]
                    if name not in used:
                        break
                used.add(name)
                picked.append({"id": len(picked), "kind": kind, "x": x, "y": y, "cell": k, "name": name,
                               "cells": frozenset(cells_within(x, y, WIND_REACH_KM))})
                break
        world._winds = picked
    return world._winds


def wind_blows(weather: "Weather", wind: dict, day: int, minute: int) -> bool:
    """Is this local wind blowing at this hour of this day?"""
    frame = weather.frame(day, minute)
    if wind["kind"] == "sea":
        return frame.sea_breeze >= SEA_BREEZE_AT
    if wind["kind"] == "fall":
        return frame.katabatic >= DOWNSLOPE_AT
    vx, vy = frame.wind(wind["x"], wind["y"], 1)
    return vx * vx + vy * vy >= PASS_KMH * PASS_KMH


def fly_wind(db, wind: int, user_id: int, handle: str, day: int) -> dict:
    """A pilot has flown a local wind while it blew. Inside the caller's transaction.

    Says how many days this pilot has now flown it, whether today is a
    new one, and whether this is the flight that enters it: once, under
    the pilot whose third day it was.
    """
    if db.execute("SELECT 1 FROM almanac WHERE wind=?", (wind,)).fetchone() is not None:
        return {"days": 0, "new": False, "entered": False, "late": True}
    new = db.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?)", (wind, user_id, day)).rowcount == 1
    days = db.execute("SELECT count(*) FROM sightings WHERE wind=? AND user_id=?", (wind, user_id)).fetchone()[0]
    entered = days >= WIND_DAYS and db.execute(
        "INSERT OR IGNORE INTO almanac VALUES (?, ?, ?, ?)", (wind, user_id, handle, day)).rowcount == 1
    return {"days": days, "new": new, "entered": entered, "late": False}


def read_almanac(db, user_id: int) -> tuple[dict, dict]:
    """The winds entered: number -> (user, handle, day). And the days this pilot has flown each."""
    entered = {row[0]: tuple(row[1:]) for row in db.execute("SELECT wind, user_id, handle, day FROM almanac")}
    flown = dict(db.execute("SELECT wind, count(*) FROM sightings WHERE user_id=? GROUP BY wind", (user_id,)))
    return entered, flown


# --------------------------------------------------------------------------
# The Daily Drift (S19). One short course for each world day, the same for
# every pilot of the node: a town to start from, a mark to come down by, a
# standard balloon and that day's weather. It costs no day of the calendar
# and touches neither the pilot's own balloon nor the chart. One attempt a
# day counts; practice does not.
# --------------------------------------------------------------------------

DRIFT_START_TURN = 12                # the Drift starts at 09:00
DRIFT_TURNS = 24                     # and is over six hours later, wherever the balloon is
DRIFT_BALLOON = {"fuel": 40, "ballast": 3, "pibals": 1}      # and the first envelope, burner and instruments
DRIFT_SCORE = {"full": 1000, "km": 8, "turn": 4, "fuel": 2}  # less for each km and turn, more for each fuel left
DRIFT_PAY_PER = 25                   # a postmark for every so many points scored
DRIFT_TOP = 10                       # places on the table
DRIFT_MARK_KM = (8.0, 20.0)          # the mark lies this far from where a plain flight would end
DRIFT_SCOUT = ("launch",) + ("hold",) * 5 + ("climb",) + ("hold",) * 5      # that plain flight
DRIFT_HELP = (
    "# THE DAILY DRIFT",
    "One course a world day, the same for every pilot here: a town to start",
    "from and a mark, `X`, to come down by. Everyone flies the same standard",
    "balloon in the same weather: 40 fuel, 3 sacks, 1 pibal, three layers.",
    "It starts at 09:00 and is over after 24 turns, wherever you are.",
    "# THE SCORE",
    "  1000 points,",
    "  less 8 for each km between where you come down and the mark,",
    "  less 4 for each turn flown,",
    "  plus 2 for each unit of fuel left.",
    "  Coming down on water scores nothing.",
    "Level scores are placed by who is nearer the mark, then by fewer turns,",
    "then by who finished first.",
    "# ONE ATTEMPT COUNTS",
    "  `Enter` flies today's Drift for the table: once a world day. If you",
    "  leave part way it waits for you, as you left it, until the day is out.",
    "  `P` practises: as often as you like, never on the table.",
    "  `X` leaves a Drift. Flying is as ever: `W` `S`, `A` `D`, `Enter`, `L`.",
    "The Drift costs no day and changes nothing of your own balloon or the",
    "chart. A scored Drift pays a postmark for every 25 points.",
)


def drift_score(km: float, turns: int, fuel: int, ditched: bool = False) -> int:
    """What a Drift scores: the table in DRIFT_SCORE, and nothing for coming down on water."""
    if ditched:
        return 0
    return max(0, int(DRIFT_SCORE["full"] - DRIFT_SCORE["km"] * km - DRIFT_SCORE["turn"] * turns
                      + DRIFT_SCORE["fuel"] * fuel + 0.5))


def drift_state(x: float, y: float, day: int) -> "FlightState":
    """The standard balloon, on the ground at the start, at the hour the Drift begins."""
    return FlightState(x=x, y=y, day=day, turn=DRIFT_START_TURN, fuel=DRIFT_BALLOON["fuel"],
                       ballast=DRIFT_BALLOON["ballast"], pibals=DRIFT_BALLOON["pibals"])


def drift_course(world: "World", weather: "Weather", day: int) -> dict:
    """The course of a world day: where it starts and where the mark is. The same in every process.

    A plain flight is flown from a town drawn for the day, on that day's
    true wind, and the mark is set down on land a little way from where
    it ends: so the mark can be reached, and not by doing nothing clever.
    """
    kept = world.__dict__.setdefault("_drifts", {})
    if day not in kept:
        engine = Flight(world, weather, "drift")
        course = None
        for attempt in range(8):
            town = world.towns[draw(world.seed, "drift-start", day, attempt) % len(world.towns)]
            state = drift_state(town["x"], town["y"], day)
            for action in DRIFT_SCOUT:
                offered = engine.actions(state)
                action = action if action in offered else "hold" if "hold" in offered else None
                if action is None:
                    break
                state = engine.step(state, action).state
                if state.status != "aloft":
                    break
            for tries in range(16):
                dx = (2.0 * draw_unit(world.seed, "drift-mark", day, attempt, tries, 0) - 1.0) * DRIFT_MARK_KM[1]
                dy = (2.0 * draw_unit(world.seed, "drift-mark", day, attempt, tries, 1) - 1.0) * DRIFT_MARK_KM[1]
                mx, my = state.x + dx, state.y + dy
                if not DRIFT_MARK_KM[0] ** 2 <= dx * dx + dy * dy <= DRIFT_MARK_KM[1] ** 2:
                    continue
                if 0.0 <= mx < WORLD_W and 0.0 <= my < WORLD_H and world.sample(mx, my)[1] >= SHORE:
                    course = {"day": day, "start": town["name"], "x": town["x"], "y": town["y"], "mx": mx, "my": my}
                    break
            if course is not None:
                break
        if course is None:                      # a day of nothing but sea downwind: a mark near the town itself
            town = world.towns[draw(world.seed, "drift-start", day, 0) % len(world.towns)]
            course = {"day": day, "start": town["name"], "x": town["x"], "y": town["y"],
                      "mx": town["x"], "my": town["y"]}
        while len(kept) >= 8:
            del kept[next(iter(kept))]
        kept[day] = course
    return kept[day]


def begin_drift(db, day: int, user_id: int, handle: str, state: str, now: float) -> dict:
    """Begin the day's one scored Drift, or take up the one begun. Says how it stands."""
    db.execute("INSERT OR IGNORE INTO drifts (day, user_id, handle, state, at) VALUES (?, ?, ?, ?, ?)",
               (day, user_id, handle, state, now))
    row = db.execute("SELECT state, done, score, km, turns, fuel FROM drifts WHERE day=? AND user_id=?",
                     (day, user_id)).fetchone()
    return {"state": row[0], "done": bool(row[1]), "score": row[2], "km": row[3], "turns": row[4], "fuel": row[5]}


def turn_drift(db, day: int, user_id: int, state: str) -> bool:
    """A turn of the scored Drift, written down: the attempt is taken up from here and nowhere else."""
    return db.execute("UPDATE drifts SET state=? WHERE day=? AND user_id=? AND done=0",
                      (state, day, user_id)).rowcount == 1


def finish_drift(db, day: int, user_id: int, score: int, km: float, turns: int, fuel: int, now: float) -> bool:
    """Put the scored Drift on the table. False if it is there already: it is scored once."""
    return db.execute("UPDATE drifts SET done=1, score=?, km=?, turns=?, fuel=?, at=? WHERE day=? AND user_id=?"
                      " AND done=0", (score, km, turns, fuel, now, day, user_id)).rowcount == 1


def drift_table(db, day: int, limit: int = DRIFT_TOP) -> list[tuple]:
    """The day's table: (handle, score, km, turns), best first. Level scores: nearer, then quicker, then first."""
    return db.execute("SELECT handle, score, km, turns FROM drifts WHERE day=? AND done=1"
                      " ORDER BY score DESC, km ASC, turns ASC, at ASC, user_id ASC LIMIT ?", (day, limit)).fetchall()


def my_drift(db, day: int, user_id: int) -> dict | None:
    row = db.execute("SELECT done, score, km, turns, fuel FROM drifts WHERE day=? AND user_id=?",
                     (day, user_id)).fetchone()
    return None if row is None else {"done": bool(row[0]), "score": row[1], "km": row[2], "turns": row[3],
                                     "fuel": row[4]}


# --------------------------------------------------------------------------
# The Trials (S20). A first flight that teaches by having the caller do each
# thing once, and twelve set flights with medals. Like the Drift they are
# flown on a balloon that is not the pilot's, in weather that is fixed for
# each, and they cost no day and touch nothing shared. Each trial's mark is
# where a flight written down in the table comes down, so every trial has
# a way to gold, in every world.
# --------------------------------------------------------------------------

TRIAL_MEDALS = ("no medal", "bronze", "silver", "gold")
TRIAL_KM = (30.0, 15.0, 6.0)         # down this near the mark: bronze, silver, gold; and on a short trial...
TRIAL_SHARE = (0.75, 0.4, 0.15)      # ...no further than this share of the way from the start to the mark
TRIAL_GOLD_KM = 3.0                  # but gold is never asked for nearer than this
TUTORIAL_SPARE_TURNS = 20            # the first flight leaves room to look about
TRIAL_PAY = (0, 5, 12, 25)           # postmarks a medal is worth; a better one pays the difference
TRIAL_SPARE_TURNS = 8                # turns allowed beyond the flight the trial was laid by
TRIAL_DAY = 500                      # the weather of the trials is the weather of days from here on, for good
TRIAL_FAR_KM = 10.0                  # the mark is at least this far from the start
TRIAL_BALLOON = {"fuel": 40, "ballast": 3, "pibals": 2, "ceiling": 3}
TUTORIAL = {"name": "The first flight", "about": "Learn to fly by flying: six short lessons.", "start": 12,
            "script": ("launch", "pibal", "jettison", "hold", "sink", "land"), "balloon": {}}
TRIALS = (
    {"name": "First Hop", "about": "A short flight to a mark.", "start": 12,
     "script": ("launch",) + ("hold",) * 4 + ("land",), "balloon": {}},
    {"name": "Up and Over", "about": "The ground wind will not take you there.", "start": 12,
     "script": ("launch", "climb") + ("hold",) * 4 + ("sink", "hold", "land"), "balloon": {}},
    {"name": "Top Floor", "about": "Two layers up and down again.", "start": 16,
     "script": ("launch", "climb", "climb") + ("hold",) * 5 + ("sink", "sink", "land"), "balloon": {}},
    {"name": "The Long Tank", "about": "Six fuel, and the midday sun for a burner.", "start": 20,
     "script": ("launch",) + ("hold",) * 12 + ("land",), "balloon": {"fuel": 6}},
    {"name": "No Ballast", "about": "No sacks: every climb is paid for in fuel.", "start": 12,
     "script": ("launch", "climb") + ("hold",) * 6 + ("sink", "land"), "balloon": {"ballast": 0}},
    {"name": "Sack Race", "about": "Eight fuel. The sacks are your burner.", "start": 12,
     "script": ("launch", "jettison", "jettison") + ("hold",) * 3 + ("sink", "sink", "land"),
     "balloon": {"fuel": 8}},
    {"name": "Rope Trick", "about": "Stay low and steer with the trail rope.", "start": 12,
     "script": ("launch", "left", "left", "hold", "right", "left", "land"), "balloon": {}},
    {"name": "Dawn Run", "about": "First light, when air drains off the hills.", "start": 0,
     "script": ("launch",) + ("hold",) * 8 + ("land",), "balloon": {}},
    {"name": "Dusk Dash", "about": "Down before dark.", "start": 44,
     "script": ("launch", "climb") + ("hold",) * 6 + ("sink", "hold", "land"), "balloon": {}},
    {"name": "Night Mail", "about": "After dark, with night gear.", "start": 58,
     "script": ("launch",) + ("hold",) * 6 + ("land",), "balloon": {"night_gear": True}},
    {"name": "The High Road", "about": "A big envelope and the fast air high up.", "start": 12,
     "script": ("launch",) + ("climb",) * 4 + ("hold",) * 6 + ("sink",) * 4 + ("land",),
     "balloon": {"fuel": 60, "ceiling": 5}},
    {"name": "Long Haul", "about": "Twenty turns, one change of layer.", "start": 12,
     "script": ("launch",) + ("hold",) * 6 + ("climb",) + ("hold",) * 8 + ("sink",) + ("hold",) * 3 + ("land",),
     "balloon": {}},
)
# The lessons of the first flight: what has to be done to pass each, and what is said to ask for it.
TUTOR_STEPS = (
    ("look", "Each layer of air blows its own way. W and S look at the layer above and the one below; the dotted "
             "trail is where it would carry you. Press W or S."),
    ("launch", "Enter flies what you are looking at. Press Enter to launch."),
    ("pibal", "A wind marked ~ is a forecast. P sends up a pibal: for four turns every layer is read true. Press P."),
    ("jettison", "Climbing burns fuel. A sack over the side climbs for nothing: press J, then Enter."),
    ("sink", "Sinking is free. Press S for the layer below, then Enter."),
    ("land", "Last, because it ends the flight: you land from the lowest layer. Press L, then Enter. A gentle "
             "ground wind makes a clean landing."),
)
TRIAL_HELP = (
    "# THE TRIALS",
    "Set flights, always open. Each has its own balloon and its own weather,",
    "the same every time: fly one as often as you like.",
    "They cost no day and change nothing of your own balloon or of the chart.",
    "# THE FIRST FLIGHT",
    "  Six lessons, each done once: looking at the layers, launching, a pibal,",
    "  a sack of ballast, sinking, landing. The log says what to press.",
    "# MEDALS",
    "  Each trial has a mark, `X`. Come down on land within 6 km of it for",
    "  gold, within 15 km for silver, within 30 km for bronze: nearer than",
    "  that on a short trial, as the log says when it begins. Water wins",
    "  nothing. Every mark can be reached: a flight that can be flown ends there.",
    "  A trial allows eight turns more than that flight took; then the",
    "  balloon comes down where it is.",
    "  Your best medal for each trial is kept. A medal pays 5, 12 or 25",
    "  postmarks the first time; a better one pays the difference.",
    "# KEYS",
    "  `W` `S` choose a trial   `Enter` fly it   `B` back",
    "  `X` twice leaves a trial. Flying is as ever: `W` `S`, `A` `D`, `Enter`,",
    "  `J`, `P`, `L`.",
)


def trial_state(spec: dict, x: float, y: float, day: int) -> "FlightState":
    """The balloon a trial is flown on, standing at its start at its hour."""
    balloon = dict(TRIAL_BALLOON, **spec["balloon"])
    return FlightState(x=x, y=y, day=day, turn=spec["start"], **balloon)


def trial_course(world: "World", weather: "Weather", index: int) -> dict:
    """A trial's course in this world: its day, its start and its mark. index -1 is the first flight.

    The trial's own flight is flown from a town on a day of fixed
    weather; where it lands is the mark. Towns and days are tried in a
    fixed order until the flight can be flown as written and comes down
    on land well away from where it began. That flight is kept as "gold":
    the way to the mark.
    """
    kept = world.__dict__.setdefault("_trials", {})
    if index not in kept:
        spec = TUTORIAL if index < 0 else TRIALS[index]
        engine = Flight(world, weather, "trial")
        course = None
        for attempt in range(120):
            day = TRIAL_DAY + (index + 1) * 131 + attempt
            town = world.towns[draw(world.seed, "trial-start", index, attempt) % len(world.towns)]
            state = trial_state(spec, town["x"], town["y"], day)
            flown = True
            for action in spec["script"]:
                if action not in engine.actions(state):
                    flown = False
                    break
                state = engine.step(state, action).state
                if state.status != "aloft" and action != "land":
                    flown = False
                    break
            far = math.sqrt((state.x - town["x"]) ** 2 + (state.y - town["y"]) ** 2)
            if flown and state.status == "ground" and (far >= TRIAL_FAR_KM or index < 0):
                course = {"day": day, "start": town["name"], "x": town["x"], "y": town["y"], "mx": state.x,
                          "my": state.y, "gold": list(spec["script"])}
                break
        if course is None:
            # No day in a hundred and twenty lets the flight be flown as written: a mark by the start, and no
            # flight to vouch for it. (The tests hold that this does not happen in the worlds they try.)
            town = world.towns[draw(world.seed, "trial-start", index, 0) % len(world.towns)]
            course = {"day": TRIAL_DAY + (index + 1) * 131, "start": town["name"], "x": town["x"], "y": town["y"],
                      "mx": town["x"], "my": town["y"], "gold": []}
        kept[index] = course
    return kept[index]


def trial_limits(trip: float) -> tuple[float, float, float]:
    """How near the mark wins bronze, silver and gold in a trial whose mark is so far from its start.

    The distances of TRIAL_KM, or on a short trial a share of the way: so
    that coming down where the flight began never wins anything.
    """
    # In whole km, rounded down: what the log says is exactly what is asked.
    gold = float(int(min(TRIAL_KM[2], max(TRIAL_GOLD_KM, TRIAL_SHARE[2] * trip))))
    silver = float(int(min(TRIAL_KM[1], max(gold, TRIAL_SHARE[1] * trip))))
    bronze = float(int(min(TRIAL_KM[0], max(silver, TRIAL_SHARE[0] * trip))))
    return bronze, silver, gold


def trial_medal(km: float, down: str, trip: float = 1000.0) -> int:
    """The medal a landing wins: 0 none, 1 bronze, 2 silver, 3 gold. Only a balloon down on land wins one."""
    if down not in ("ground", "stranded"):
        return 0
    return sum(1 for limit in trial_limits(trip) if km <= limit)


# --------------------------------------------------------------------------
# Wonders (S21). Forty sights in every world, each a place and the hour and
# weather it is seen in: lights in the northern night, a salt flat at noon,
# a cloud sea at dawn from above the deck. A pilot who is there when it
# shows may take it for the album; the first on the node is credited.
# --------------------------------------------------------------------------

WONDER_COUNT = 40
WONDER_APART_KM = 150.0              # two of a kind are this far apart where the land allows
WONDER_PAY, WONDER_POINTS = (12, 40), (6, 20)        # seen for oneself; and seen first of anyone on the node
WONDER_HINTS = (0.10, 0.30)          # of the land charted: when a hint says where, and when it says how
WONDER_SAMPLE = range(2, 122, 4)     # the days a site is tried on when the world's wonders are chosen
# Each kind: how its sights are named, what is said of it before anyone knows more, how near and in which
# layers it is seen, between which hours, and the picture it is shown as. What weather it wants is in
# wonder_shows, by name.
WONDER_KINDS = {
    "aurora": {"name": "The {} Lights", "title": "Lights", "km": 40.0, "layers": (2, 6), "hours": (20.5, 24.0), "count": 5,
               "hint": "lights in the northern night sky",
               "how": "after 20:30 under a clear sky, from layer 2 or above"},
    "salt": {"name": "The {} Mirror", "title": "Mirrors", "km": 20.0, "layers": (1, 3), "hours": (11.0, 13.5), "count": 5,
             "hint": "a dry plain that shines at noon",
             "how": "between 11:00 and 13:30 under a clear sky, no higher than layer 3"},
    "falls": {"name": "{} Falls", "title": "Falls", "km": 12.0, "layers": (1, 1), "hours": (7.0, 19.0), "count": 5,
              "hint": "water coming off steep ground",
              "how": "by day, from the lowest layer the ground leaves open"},
    "cloudsea": {"name": "The {} Cloud Sea", "title": "Cloud seas", "km": 40.0, "layers": (1, 6), "hours": (6.0, 9.0), "count": 5,
                 "hint": "a floor of cloud over hills at dawn",
                 "how": "06:00 to 09:00 over overcast hills, from above the deck: layer 4 or 5"},
    "lightning": {"name": "{} Storm Light", "title": "Storm light", "km": 40.0, "layers": (1, 6), "hours": (18.0, 24.0), "count": 4,
                  "hint": "an evening squall seen from a safe way off",
                  "how": "after 18:00 with a squall 25 to 65 km off and none overhead"},
    "alpenglow": {"name": "The {} Rose", "title": "Roses", "km": 40.0, "layers": (1, 6), "hours": (19.0, 20.5), "count": 5,
                  "hint": "high peaks in the last light",
                  "how": "between 19:00 and 20:30 under a clear sky"},
    "sealights": {"name": "The {} Fire", "title": "Sea fire", "km": 15.0, "layers": (1, 1), "hours": (20.5, 24.0), "count": 5,
                  "hint": "shallow water that glows after dark",
                  "how": "after 20:30, from the lowest layer the ground leaves open"},
    "mist": {"name": "{} Mist", "title": "Mists", "km": 15.0, "layers": (1, 2), "hours": (6.0, 8.25), "count": 5,
             "hint": "low ground at dawn on a calm morning",
             "how": "between 06:00 and 08:15 when mist lies there, no higher than layer 2"},
    "eye": {"name": "The Eye of the Gyre", "title": "The Gyre", "km": 30.0, "layers": (1, 6), "hours": (0.0, 24.0), "count": 1,
            "hint": "the still middle of the great storm",
            "how": "at any hour, within 30 km of the storm's centre"},
}


def _wonder_ground(world: "World", weather: "Weather", kind: str, k: int) -> bool:
    """Is this chart cell the kind of ground such a sight belongs to?"""
    biome, m = world.chart_biome[k], world.chart_elevation[k]
    if kind == "aurora":
        return biome >= SHORE and k // CHART_W < CHART_H * 0.3
    if kind == "salt":
        return biome == DRY
    if kind == "falls":
        return 0 <= m < LAYER_BLOCK_M[0] and weather.slope[k][0] ** 2 + weather.slope[k][1] ** 2 > 0.2
    if kind == "cloudsea":
        return biome == HILLS
    if kind == "lightning":
        return biome >= SHORE and m < LAYER_BLOCK_M[0]
    if kind == "alpenglow":
        return biome in (MOUNTAIN, SNOW)
    if kind == "sealights":
        return biome == SEA_SHALLOW and weather.coast[k][0] ** 2 + weather.coast[k][1] ** 2 > 0.25
    return biome in (GRASS, FOREST) and 0 <= m < 250          # mist


def wonder_shows(weather: "Weather", wonder: dict, day: int, minute: int) -> bool:
    """Is this wonder to be seen at this hour of this day? Its hour and its weather, at the place itself."""
    kind = wonder["kind"]
    low, high = WONDER_KINDS[kind]["hours"]
    if not low * 60 <= minute < high * 60:
        return False
    x, y = wonder["x"], wonder["y"]
    if kind in ("falls", "sealights", "eye"):
        return True
    frame = weather.frame(day, minute)
    if kind in ("aurora", "salt"):
        return frame.cloud(x, y)[0] < 0.3
    if kind == "alpenglow":
        return frame.cloud(x, y)[0] < 0.4
    if kind == "cloudsea":
        return frame.cloud(x, y)[0] >= 0.6
    if kind == "mist":
        return frame.fog(x, y) > FOG_THRESHOLD
    # Lightning: a squall some way off in any direction, and none here.
    if frame.squall(x, y) > 0.1:
        return False
    return any(frame.squall(x + dx * r, y + dy * r) >= 0.4 for r in (25.0, 45.0, 65.0)
               for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)))


def wonders_of(world: "World", weather: "Weather") -> list[dict]:
    """The world's forty wonders: where each is, what kind, and its name. The same every time.

    For each kind, the ground it belongs to is gone through in a keyed
    order, and a place is taken if it is well away from the others of its
    kind and the sight does show there on at least one of the days it is
    tried on: so none of the forty is a sight that never comes. Not
    stored; the records know a wonder by its number here.
    """
    if getattr(world, "_wonders", None) is None:
        picked: list = []
        used: set = set()
        for kind, spec in WONDER_KINDS.items():
            if kind == "eye":
                picked.append({"id": len(picked), "kind": kind, "x": world.gyre["x"], "y": world.gyre["y"],
                               "name": spec["name"]})
                continue
            ground = sorted((k for k in range(CHART_W * CHART_H)
                             if 3 <= k % CHART_W < CHART_W - 3 and 3 <= k // CHART_W < CHART_H - 3
                             and _wonder_ground(world, weather, kind, k)),
                            key=lambda k: draw_unit(world.seed, "wonder", kind, k))
            low, high = spec["hours"]
            hours = [int((low + (high - low) * i / 3.0) * 60) for i in range(3)]
            mine: list = []
            for apart in (WONDER_APART_KM, WONDER_APART_KM / 2, 30.0, 0.0):
                tried = 0
                for k in ground:
                    if len(mine) >= spec["count"] or tried >= 60:
                        break
                    x, y = (k % CHART_W + 0.5) * CHART_KM, (k // CHART_W + 0.5) * CHART_KM
                    if any(w["cell"] == k or (x - w["x"]) ** 2 + (y - w["y"]) ** 2 < apart * apart for w in mine):
                        continue
                    tried += 1
                    site = {"kind": kind, "x": x, "y": y, "cell": k}
                    if not any(wonder_shows(weather, site, day, minute) for day in WONDER_SAMPLE for minute in hours):
                        continue                # a sight that would never come, here
                    for attempt in range(200):
                        name = spec["name"].format(
                            _NAME_FIRST[draw(world.seed, "wonder-name", kind, k, attempt) % len(_NAME_FIRST)])
                        if name not in used:
                            break
                    used.add(name)
                    mine.append(dict(site, name=name))
            for site in mine:
                picked.append(dict(site, id=len(picked)))
        world._wonders = picked
    return world._wonders


def see_wonder(db, wonder: int, user_id: int, handle: str, day: int) -> dict:
    """A pilot takes a wonder for the album. Says whether it is new to them, and whether they are the node's first."""
    new = db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (wonder, user_id, day)).rowcount == 1
    first = new and db.execute("INSERT OR IGNORE INTO wonders VALUES (?, ?, ?, ?)",
                               (wonder, user_id, handle, day)).rowcount == 1
    by = db.execute("SELECT user_id, handle, day FROM wonders WHERE wonder=?", (wonder,)).fetchone()
    return {"new": new, "first": first, "by": tuple(by) if by else None}       # by: whoever was first, as it stands


def read_wonders(db, user_id: int) -> tuple[dict, set]:
    """The wonders seen on the node: number -> (user, handle, day). And the ones this pilot has seen."""
    first = {row[0]: tuple(row[1:]) for row in db.execute("SELECT wonder, user_id, handle, day FROM wonders")}
    mine = {row[0] for row in db.execute("SELECT wonder FROM seen WHERE user_id=?", (user_id,))}
    return first, mine


# --------------------------------------------------------------------------
# Seasons (S22). Twenty-eight world days: a turn of the prevailing winds (the
# weather has always had it), a goal for the whole node, and in the last
# week a race. When a season is over its goal is settled and the race is
# scored, once; at most three pilots take its medals. Nothing a pilot owns
# is ever reset, and nor is the map.
# --------------------------------------------------------------------------

RACE_DAYS = 7                        # the race is the season's last so many days
SEASON_GRACE = WAITING_DAYS          # a season is closed this many days on: every day of it has lapsed by then
GOAL_PAY = 60                        # to every pilot who had a hand in a goal that was met
RACE_PAY = (120, 70, 40)             # the race's first three
RACE_PLACES = ("gold", "silver", "bronze")
SEASON_RETRY_SECONDS = 60.0          # how long before settling is tried again after it could not be written
# What a node may be set to do in a season: how much of it, and how it is said. Each is counted from what
# the records already hold for the days of the season. One active pilot can do any of them in a season
# (about 10 flying days of charting; two kits; four crates; four sights; two winds); more pilots, sooner.
SEASON_GOALS = {
    "chart": {"target": 200, "text": "Chart {n} cells nobody has seen", "unit": "cells"},
    "plant": {"target": 2, "text": "Plant {n} weather stations", "unit": "stations"},
    "mast": {"target": 1, "text": "Raise a mooring mast", "unit": "mast"},
    "wonders": {"target": 4, "text": "Be the first to {n} wonders", "unit": "wonders"},
    "winds": {"target": 2, "text": "Enter {n} winds in the almanac", "unit": "winds"},
}


def season_of(day: int) -> tuple[int, int]:
    """The season a world day is in, counted from 1, and the day of that season, from 1 to 28."""
    return (day - 1) // SEASON_DAYS + 1, (day - 1) % SEASON_DAYS + 1


def season_span(season: int) -> tuple[int, int]:
    """The first and the last world day of a season."""
    return (season - 1) * SEASON_DAYS + 1, season * SEASON_DAYS


def in_race(day: int) -> bool:
    return season_of(day)[1] > SEASON_DAYS - RACE_DAYS


def goal_progress(db, kind: str, first: int, last: int) -> tuple[int, list]:
    """How far a goal has come in a span of days, and who did how much of it: (user, handle, amount), most first."""
    if kind == "chart":
        rows = db.execute("SELECT c.user_id, coalesce(p.handle, '?'), count(*) FROM charted c LEFT JOIN pilots p"
                          " USING (user_id) WHERE c.day BETWEEN ? AND ? GROUP BY c.user_id", (first, last)).fetchall()
    elif kind == "plant":
        rows = db.execute("SELECT user_id, handle, count(*) FROM stations WHERE day BETWEEN ? AND ?"
                          " GROUP BY user_id", (first, last)).fetchall()
    elif kind == "mast":
        # A mast is the work of everyone whose crates are in it.
        total = db.execute("SELECT count(*) FROM stations WHERE mast_day BETWEEN ? AND ?", (first, last)).fetchone()[0]
        rows = db.execute("SELECT c.user_id, c.handle, sum(c.crates) FROM contributions c JOIN stations s"
                          " ON s.id=c.station WHERE s.mast_day BETWEEN ? AND ? GROUP BY c.user_id",
                          (first, last)).fetchall()
        return total, sorted(rows, key=lambda r: (-r[2], r[0]))
    elif kind == "wonders":
        rows = db.execute("SELECT user_id, handle, count(*) FROM wonders WHERE day BETWEEN ? AND ?"
                          " GROUP BY user_id", (first, last)).fetchall()
    else:
        rows = db.execute("SELECT user_id, handle, count(*) FROM almanac WHERE day BETWEEN ? AND ?"
                          " GROUP BY user_id", (first, last)).fetchall()
    return sum(row[2] for row in rows), sorted(rows, key=lambda r: (-r[2], r[0]))


def begin_season(db, seed: int, season: int, winds: int) -> tuple[str, int]:
    """The season's goal: set once, by whoever is first to look, and the same for everyone after.

    The kinds are gone through in an order drawn for the season; one that
    the world has no more room for is passed over.
    """
    row = db.execute("SELECT kind, target FROM seasons WHERE season=?", (season,)).fetchone()
    if row is not None:
        return row[0], row[1]
    room = {"winds": winds - db.execute("SELECT count(*) FROM almanac").fetchone()[0],
            "wonders": WONDER_COUNT - db.execute("SELECT count(*) FROM wonders").fetchone()[0],
            "chart": CHART_W * CHART_H - db.execute("SELECT count(*) FROM charted").fetchone()[0]}
    order = sorted(SEASON_GOALS, key=lambda kind: draw_unit(seed, "season-goal", season, kind))
    kind = next((k for k in order if room.get(k, 10 ** 9) >= SEASON_GOALS[k]["target"] * 2), "plant")
    db.execute("INSERT OR IGNORE INTO seasons (season, kind, target) VALUES (?, ?, ?)",
               (season, kind, SEASON_GOALS[kind]["target"]))
    return kind, SEASON_GOALS[kind]["target"]


def race_fly(db, user_id: int, handle: str, km: float, day: int) -> int:
    """A flight flown in a season's race: 1 if it is now the pilot's longest of that race, 0 if not.

    -1 if the race is over: a flight begun in a season's last days and
    ended after the season was settled counts for nothing, and is told so.
    """
    season = season_of(day)[0]
    if not in_race(day):
        return 0
    if db.execute("SELECT 1 FROM seasons WHERE season=? AND closed=1", (season,)).fetchone():
        return -1
    row = db.execute("SELECT km FROM race WHERE season=? AND user_id=?", (season, user_id)).fetchone()
    if row is not None and row[0] >= km:
        return 0
    db.execute("INSERT INTO race VALUES (?, ?, ?, ?, ?) ON CONFLICT (season, user_id) DO UPDATE SET"
               " km=excluded.km, day=excluded.day, handle=excluded.handle", (season, user_id, handle, km, day))
    return 1


def close_season(db, seed: int, season: int, winds: int) -> bool:
    """Settle a season that is over: whether its goal was met, and who takes the race's medals. Once.

    False if somebody has settled it already. A season nobody looked at
    while it ran is given its goal now and settled all the same.
    """
    kind, target = begin_season(db, seed, season, winds)
    if db.execute("UPDATE seasons SET closed=1 WHERE season=? AND closed=0", (season,)).rowcount != 1:
        return False
    total, hands = goal_progress(db, kind, *season_span(season))
    db.execute("UPDATE seasons SET met=? WHERE season=?", (int(total >= target), season))
    # Who had a hand in it is written down now. A pilot whose logbook still stands on a day of this season
    # (stranded, or part way through a day) may add to its count later; that changes nothing that is owed.
    db.executemany("INSERT OR IGNORE INTO season_hands VALUES (?, ?)", [(season, hand[0]) for hand in hands])
    places = db.execute("SELECT user_id, handle, km FROM race WHERE season=? ORDER BY km DESC, day ASC, user_id ASC"
                        " LIMIT ?", (season, len(RACE_PLACES))).fetchall()
    for place, (user_id, handle, km) in enumerate(places):
        db.execute("INSERT INTO season_medals VALUES (?, ?, ?, ?, ?)", (season, place, user_id, handle, km))
    return True


def claim_season(db, season: int, user_id: int) -> dict | None:
    """What a closed season owes a pilot, the one time it is asked for: a share in the goal, a place in the race."""
    row = db.execute("SELECT kind, met FROM seasons WHERE season=? AND closed=1", (season,)).fetchone()
    if row is None or db.execute("INSERT OR IGNORE INTO season_claims VALUES (?, ?)",
                                 (season, user_id)).rowcount != 1:
        return None
    hand = db.execute("SELECT 1 FROM season_hands WHERE season=? AND user_id=?", (season, user_id)).fetchone()
    place = db.execute("SELECT place FROM season_medals WHERE season=? AND user_id=?", (season, user_id)).fetchone()
    return {"season": season, "kind": row[0], "goal": bool(row[1]) and hand is not None,
            "place": None if place is None else place[0]}


def settle_seasons(db, seed: int, today: int, winds: int) -> list[int]:
    """Set the goal of the season a world day is in, and settle every season that is over and not yet settled.

    However long the node was quiet: each season between is given its
    goal and closed in turn. Returns the seasons this call settled.
    """
    season = season_of(today)[0]
    begin_season(db, seed, season, winds)
    closed = {row[0] for row in db.execute("SELECT season FROM seasons WHERE closed=1")}
    return [n for n in range(1, season) if n not in closed and today > season_span(n)[1] + SEASON_GRACE
            and close_season(db, seed, n, winds)]


def unclaimed_seasons(db, user_id: int) -> list[int]:
    """The closed seasons that have not been asked what they owe this pilot, however long ago they were."""
    return [row[0] for row in db.execute(
        "SELECT season FROM seasons WHERE closed=1 AND season NOT IN (SELECT season FROM season_claims"
        " WHERE user_id=?) ORDER BY season", (user_id,))]


def read_season(db, season: int) -> dict:
    """A season as the records have it: its goal and how far it has come, the race so far, and its medals."""
    row = db.execute("SELECT kind, target, closed, met FROM seasons WHERE season=?", (season,)).fetchone()
    out = {"season": season, "kind": None, "target": 0, "closed": False, "met": False, "total": 0, "hands": [],
           "race": [], "medals": []}
    if row is not None:
        total, hands = goal_progress(db, row[0], *season_span(season))
        out.update(kind=row[0], target=row[1], closed=bool(row[2]), met=bool(row[3]), total=total, hands=hands)
    out["race"] = db.execute("SELECT handle, km FROM race WHERE season=? ORDER BY km DESC, day ASC, user_id ASC"
                             " LIMIT 5", (season,)).fetchall()
    out["medals"] = db.execute("SELECT place, handle, km FROM season_medals WHERE season=? ORDER BY place",
                               (season,)).fetchall()
    return out


# --------------------------------------------------------------------------
# The hall (S23): what was done first on the node and is kept for good. For
# now it has one line: the first pilot into the eye of the Gyre.
# --------------------------------------------------------------------------

def enter_hall(db, feat: str, user_id: int, handle: str, day: int) -> dict:
    """Write a pilot into the hall for a feat, if nobody is there. Says whether they are, and who is."""
    first = db.execute("INSERT OR IGNORE INTO hall VALUES (?, ?, ?, ?)", (feat, user_id, handle, day)).rowcount == 1
    by = db.execute("SELECT user_id, handle, day FROM hall WHERE feat=?", (feat,)).fetchone()
    return {"first": first, "by": tuple(by)}


def read_hall(db) -> dict:
    return {row[0]: tuple(row[1:]) for row in db.execute("SELECT feat, user_id, handle, day FROM hall")}


# --------------------------------------------------------------------------
# The world's records (S10): one SQLite file for each world, shared by every
# caller of that node. Writes are short transactions that take the write
# lock at once; none is open while a caller thinks. A process killed at any
# instant leaves the turn before or the turn after, never half of one.
# --------------------------------------------------------------------------

APPLICATION_ID = 0x46574442          # "FWDB": says the file is a Fair Winds world
SCHEMA_VERSION = 9
LEASE_STALE_SECONDS = 90.0           # a session not heard from for this long may be replaced without asking
HEARTBEAT_SECONDS = 20.0
BUSY_TIMEOUT_SECONDS = 5.0           # how long a write waits for another caller's write

# Each entry brings the schema from the version before it to its own.
MIGRATIONS = {
    1: (
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
        "CREATE TABLE pilots (user_id INTEGER PRIMARY KEY, handle TEXT NOT NULL, created REAL NOT NULL,"
        " career TEXT NOT NULL)",
        "CREATE TABLE sessions (user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, started REAL NOT NULL,"
        " heartbeat REAL NOT NULL, transport TEXT NOT NULL)",
        "CREATE TABLE flights (user_id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, state TEXT NOT NULL)",
        "CREATE TABLE turns (user_id INTEGER NOT NULL, seq INTEGER NOT NULL, day INTEGER NOT NULL,"
        " turn INTEGER NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL, at REAL NOT NULL,"
        " PRIMARY KEY (user_id, seq)) WITHOUT ROWID",
    ),
    2: (
        "CREATE TABLE charted (cell INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, day INTEGER NOT NULL)",
        "CREATE TABLE features (id INTEGER PRIMARY KEY, name TEXT NOT NULL, user_id INTEGER NOT NULL,"
        " handle TEXT NOT NULL, day INTEGER NOT NULL, chosen INTEGER NOT NULL DEFAULT 0)",
    ),
    3: (
        # stock is what the station held on stock_day; a mast has the day it was raised, a station has none
        "CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,"
        " user_id INTEGER NOT NULL, handle TEXT NOT NULL, day INTEGER NOT NULL, stock INTEGER NOT NULL,"
        " stock_day INTEGER NOT NULL, freight INTEGER NOT NULL DEFAULT 0, mast_day INTEGER)",
        "CREATE TABLE contributions (station INTEGER NOT NULL, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " crates INTEGER NOT NULL, PRIMARY KEY (station, user_id)) WITHOUT ROWID",
    ),
    4: (
        # a call for help: what the pilot needs, and once somebody has reached them, who
        "CREATE TABLE calls (user_id INTEGER PRIMARY KEY, handle TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,"
        " day INTEGER NOT NULL, fuel INTEGER NOT NULL, repair INTEGER NOT NULL, by_user INTEGER, by_handle TEXT)",
        "CREATE TABLE guestbook (id INTEGER PRIMARY KEY AUTOINCREMENT, station INTEGER NOT NULL,"
        " user_id INTEGER NOT NULL, handle TEXT NOT NULL, day INTEGER NOT NULL, text TEXT NOT NULL,"
        " UNIQUE (station, user_id, day))",
    ),
    5: (
        "CREATE TABLE sightings (wind INTEGER NOT NULL, user_id INTEGER NOT NULL, day INTEGER NOT NULL,"
        " PRIMARY KEY (wind, user_id, day)) WITHOUT ROWID",
        "CREATE TABLE almanac (wind INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " day INTEGER NOT NULL)",
    ),
    6: (
        # one row for a pilot's scored Drift of a day: the flight as it stands, and once it is over, the score
        "CREATE TABLE drifts (day INTEGER NOT NULL, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " state TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, score INTEGER, km REAL, turns INTEGER,"
        " fuel INTEGER, at REAL NOT NULL, PRIMARY KEY (day, user_id)) WITHOUT ROWID",
    ),
    7: (
        # wonders: who saw each first on the node. seen: every pilot's own album.
        "CREATE TABLE wonders (wonder INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " day INTEGER NOT NULL)",
        "CREATE TABLE seen (wonder INTEGER NOT NULL, user_id INTEGER NOT NULL, day INTEGER NOT NULL,"
        " PRIMARY KEY (wonder, user_id)) WITHOUT ROWID",
    ),
    8: (
        # a season's goal, and once it is over whether it was met; each pilot's longest flight of its race;
        # the race's first three; and who has been given what a closed season owed them
        "CREATE TABLE seasons (season INTEGER PRIMARY KEY, kind TEXT NOT NULL, target INTEGER NOT NULL,"
        " closed INTEGER NOT NULL DEFAULT 0, met INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE race (season INTEGER NOT NULL, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " km REAL NOT NULL, day INTEGER NOT NULL, PRIMARY KEY (season, user_id)) WITHOUT ROWID",
        "CREATE TABLE season_medals (season INTEGER NOT NULL, place INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " handle TEXT NOT NULL, km REAL NOT NULL, PRIMARY KEY (season, place)) WITHOUT ROWID",
        "CREATE TABLE season_claims (season INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " PRIMARY KEY (season, user_id)) WITHOUT ROWID",
        # who had a hand in a season's goal, as it stood when the season was settled
        "CREATE TABLE season_hands (season INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " PRIMARY KEY (season, user_id)) WITHOUT ROWID",
        "CREATE INDEX charted_by_day ON charted (day)",       # a season's charting is counted by its days
    ),
    9: (
        "CREATE TABLE hall (feat TEXT PRIMARY KEY, user_id INTEGER NOT NULL, handle TEXT NOT NULL,"
        " day INTEGER NOT NULL) WITHOUT ROWID",
    ),
}
_NO_PLACE_TO_WRITE = ("readonly", "read-only", "unable to open", "locked", "disk i/o", "full", "busy")
_NO_PLACE_CODES = (5, 6, 8, 10, 13, 14)     # SQLITE_BUSY, LOCKED, READONLY, IOERR, FULL, CANTOPEN


def unreachable(error: sqlite3.Error) -> bool:
    """Is this the records being out of reach (busy, read-only, full), as against being damaged?"""
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in _NO_PLACE_CODES
    text = str(error).lower()
    return isinstance(error, sqlite3.OperationalError) and any(word in text for word in _NO_PLACE_TO_WRITE)


class StoreError(Exception):
    """The world's records are there and cannot be used. They are left exactly as they are."""


class StoreUnavailable(Exception):
    """Records cannot be kept here at all: no folder, or nothing may be written."""


class LeaseLost(Exception):
    """Another session has taken this pilot over."""


def db_name(seed: int) -> str:
    return f"fair_winds-{seed:016x}.db"


class Store:
    """One world's records."""

    opened: "weakref.WeakSet[Store]" = weakref.WeakSet()

    def __init__(self, folder: Path, seed: int, node: str, clock=time.time, journal: str = "wal"):
        self.path = Path(folder) / db_name(seed)
        self.journal_mode = "DELETE" if journal.strip().lower() == "delete" else "WAL"
        self.token = os.urandom(8).hex()
        self.clock = clock
        self.db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        except (OSError, sqlite3.Error) as error:
            raise StoreUnavailable(str(error)) from None
        try:
            self._prepare(seed, node)
        except sqlite3.DatabaseError as error:
            self.close()
            if unreachable(error):
                raise StoreUnavailable(str(error)) from None
            raise StoreError(f"{self.path.name} is damaged or is not a database ({error})") from None
        except StoreError:
            self.close()
            raise
        Store.opened.add(self)

    def _prepare(self, seed: int, node: str) -> None:
        db = self.db
        # The file's three marks are read as one: inside a read transaction, so that another caller who is
        # creating the world at this very moment is seen either not at all or whole. Read one after another
        # they could be caught between its writes, and a world in the making taken for somebody else's file.
        db.execute("BEGIN")
        try:
            tables = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]     # this takes the read lock
            app = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")      # nothing was written; and it cannot hide the error that brought us here
        if (app, version, tables) != (0, 0, 0):
            if app != APPLICATION_ID:
                raise StoreError(f"{self.path.name} is not a Fair Winds world")
            if version > SCHEMA_VERSION:
                raise StoreError(f"{self.path.name} was written by a newer Fair Winds (schema {version}, "
                                 f"this one knows {SCHEMA_VERSION})")
        try:
            db.execute(f"PRAGMA journal_mode={self.journal_mode}")
        except sqlite3.OperationalError:
            pass                                # a filesystem without it: the rollback journal is as safe
        db.execute("PRAGMA synchronous=FULL")   # a turn is on disk before its result is shown
        if version < SCHEMA_VERSION:
            def migrate():
                # Read again inside the write lock: another caller may have just done it.
                current = db.execute("PRAGMA user_version").fetchone()[0]
                for step in range(current + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS[step]:
                        db.execute(statement)
                if current == 0:
                    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    db.executemany("INSERT INTO meta VALUES (?, ?)",
                                   (("seed", str(seed)), ("node", node), ("created", str(self.clock()))))
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._write(migrate)
        row = db.execute("SELECT value FROM meta WHERE key='seed'").fetchone()
        if row is None or row[0] != str(seed):
            raise StoreError(f"{self.path.name} holds another world's records")

    def _write(self, work):
        """Run work() as one transaction that holds the write lock from its first statement."""
        if self.db is None:
            raise sqlite3.ProgrammingError("the records are closed")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = work()
            self.db.execute("COMMIT")
        except BaseException:
            # Whatever failed, the commit itself included, nothing stays open:
            # an open transaction would hold the write lock against every caller.
            if self.db.in_transaction:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass                        # the first error is the one worth hearing
            raise
        return result

    # -- the lease -----------------------------------------------------------

    def claim(self, user_id: int, transport: str, force: bool = False) -> dict | None:
        """Make this the pilot's one live session. Returns None, or who holds the pilot if someone does."""
        def work():
            now = self.clock()
            row = self.db.execute("SELECT token, started, heartbeat, transport FROM sessions WHERE user_id=?",
                                  (user_id,)).fetchone()
            if row is not None and row[0] != self.token and not force and now - row[2] < LEASE_STALE_SECONDS:
                return {"started": row[1], "heartbeat": row[2], "transport": row[3], "now": now}
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)",
                            (user_id, self.token, now, now, transport))
            return None
        return self._write(work)

    def beat(self, user_id: int) -> bool:
        """Say this session is alive. False if the pilot is no longer this session's."""
        return self._write(lambda: self.db.execute(
            "UPDATE sessions SET heartbeat=? WHERE user_id=? AND token=?",
            (self.clock(), user_id, self.token)).rowcount == 1)

    def holds(self, user_id: int) -> bool:
        """Is the pilot this session's? A read: it costs no write."""
        row = self.db.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return row is not None and row[0] == self.token

    def release(self, user_id: int) -> None:
        self._write(lambda: self.db.execute("DELETE FROM sessions WHERE user_id=? AND token=?",
                                            (user_id, self.token)))

    # -- the pilot and the flight ---------------------------------------------

    def pilot(self, user_id: int) -> tuple[str, str] | None:
        """The pilot's career and flight as stored text, or None for a pilot never seen."""
        row = self.db.execute("SELECT p.career, f.state FROM pilots p JOIN flights f USING (user_id)"
                              " WHERE p.user_id=?", (user_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def commit(self, user_id: int, handle: str, career, state: dict, action: str = "",
               chart=(), watch=None, deed=None) -> int:
        """Write the pilot as they now stand, and with an action a line in the day's journal.

        One transaction: the flight, the journal row, the career, the cells
        the turn charted and any feature it found go together or not at all.
        Raises LeaseLost, having written nothing, if the pilot is no longer
        this session's.

        chart   cells this turn saw. Each is charted in this pilot's name if
                nobody charted it before.
        watch   features the turn may have found: id -> (cells to count,
                how many of them must be charted, the name it gets).
        career  the career as a dict, or a function of (cells newly charted,
                features found) that returns it: what a turn earns depends
                on what turned out to be new.
        deed    something done to the shared world in the same transaction:
                a function of the database. What it returns is given to
                career as a third argument. career may then return the
                career and the flight as a pair, where the deed changed
                the flight too.
        """
        def work():
            db, now = self.db, self.clock()
            row = db.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,)).fetchone()
            if row is None or row[0] != self.token:
                raise LeaseLost()
            done = deed(db) if deed is not None else None
            new = [cell for cell in chart if db.execute(
                "INSERT OR IGNORE INTO charted VALUES (?, ?, ?)", (cell, user_id, state["day"])).rowcount == 1]
            found = []
            for fid, (cells, need, name) in (watch or {}).items():
                have = 0
                for i in range(0, len(cells), 400):
                    part = cells[i:i + 400]
                    have += db.execute(f"SELECT count(*) FROM charted WHERE cell IN ({','.join('?' * len(part))})",
                                       part).fetchone()[0]
                # Whoever's turn crosses the line finds it; the row is there once, so it is found once.
                if have >= need and db.execute("INSERT OR IGNORE INTO features (id, name, user_id, handle, day)"
                                               " VALUES (?, ?, ?, ?, ?)",
                                               (fid, name, user_id, handle, state["day"])).rowcount == 1:
                    found.append(fid)
            flight = state
            if callable(career):
                written = career(new, found, done) if deed is not None else career(new, found)
                if isinstance(written, tuple):
                    written, flight = written
            else:
                written = career
            row = db.execute("SELECT seq FROM flights WHERE user_id=?", (user_id,)).fetchone()
            seq = row[0] if row else 0
            text = json.dumps(flight)
            db.execute("INSERT INTO pilots VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
                       " handle=excluded.handle, career=excluded.career",
                       (user_id, handle, now, json.dumps(written)))
            if action:
                seq += 1
                db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (user_id, seq, flight["day"], flight["turn"], action, text, now))
                db.execute("DELETE FROM turns WHERE user_id=? AND day<?", (user_id, flight["day"]))
            db.execute("INSERT INTO flights VALUES (?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
                       " seq=excluded.seq, state=excluded.state", (user_id, seq, text))
            db.execute("UPDATE sessions SET heartbeat=? WHERE user_id=?", (now, user_id))
            return seq
        return self._write(work)

    def write(self, work, user_id: int | None = None):
        """Something written to the shared world alone, as one transaction: a function of the database.

        With a pilot named, only if the pilot is still this session's:
        otherwise LeaseLost, and nothing is written.
        """
        def checked():
            if user_id is not None:
                row = self.db.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,)).fetchone()
                if row is None or row[0] != self.token:
                    raise LeaseLost()
            return work(self.db)
        return self._write(checked)

    def read(self, query):
        """What a query of the shared world comes to: a function of the database. It writes nothing."""
        if self.db is None:
            raise sqlite3.ProgrammingError("the records are closed")
        return query(self.db)

    def stations(self) -> list[dict]:
        """Every planted station, in the order they were planted."""
        return self.read(read_stations)

    def chart(self) -> tuple[set, dict]:
        """Every charted cell, and the features found: id -> (name, user_id, handle, day)."""
        cells = {row[0] for row in self.db.execute("SELECT cell FROM charted")}
        found = {row[0]: tuple(row[1:]) for row in
                 self.db.execute("SELECT id, name, user_id, handle, day FROM features")}
        return cells, found

    def chart_size(self) -> tuple[int, int]:
        """How many cells are charted and how many features found: cheap, to see whether to read them again."""
        return (self.db.execute("SELECT count(*) FROM charted").fetchone()[0],
                self.db.execute("SELECT count(*), coalesce(sum(chosen), 0) FROM features").fetchone())

    def charters(self, limit: int = 5) -> list[tuple[str, int]]:
        """Who has charted the most cells first."""
        return self.db.execute("SELECT coalesce(p.handle, '?'), count(*) FROM charted c LEFT JOIN pilots p"
                               " USING (user_id) GROUP BY c.user_id ORDER BY count(*) DESC, c.user_id LIMIT ?",
                               (limit,)).fetchall()

    def name_feature(self, feature: int, user_id: int, name: str) -> bool:
        """The finder picks a name, once. False if it was not theirs to name, or is named already."""
        return self._write(lambda: self.db.execute(
            "UPDATE features SET name=?, chosen=1 WHERE id=? AND user_id=? AND chosen=0",
            (name, feature, user_id)).rowcount == 1)

    def others(self, user_id: int) -> list[tuple[str, str]]:
        """Every other pilot of this world: career and flight as stored text, the latest to fly first."""
        return self.db.execute(
            "SELECT p.career, f.state FROM pilots p JOIN flights f USING (user_id) WHERE p.user_id<>?"
            " ORDER BY COALESCE((SELECT max(at) FROM turns t WHERE t.user_id=p.user_id), 0) DESC, p.user_id",
            (user_id,)).fetchall()

    def journal(self, user_id: int) -> list[tuple]:
        """The committed turns of the pilot's current flying day: (seq, day, turn, action)."""
        return self.db.execute("SELECT seq, day, turn, action FROM turns WHERE user_id=? ORDER BY seq",
                               (user_id,)).fetchall()

    # -- the calendar ----------------------------------------------------------

    def today(self, local: datetime.datetime, zone: str = "") -> tuple[int, dict]:
        """The current world day and the calendar. Begins the calendar, or opens a day in it, when that is due.

        Reading is the common case and takes no lock. The write happens once
        for each day that opens, by whichever caller sees it first.
        """
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        cal = read_calendar(meta)
        if (cal is not None and calendar_day(cal, local) == cal["high_day"]
                and (not zone or meta.get("timezone") == zone)):
            return cal["high_day"], cal

        def work():
            now = read_calendar(dict(self.db.execute("SELECT key, value FROM meta"))) or new_calendar(local)
            now["high_day"] = calendar_day(now, local)
            self._put_calendar(now, zone)
            return now
        cal = self._write(work)
        return cal["high_day"], cal

    def _put_calendar(self, cal: dict, zone: str = "") -> None:
        rows = [(key, str(value)) for key, value in cal.items()]
        if zone:
            rows.append(("timezone", zone))
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", rows)

    def set_tempo(self, tempo: int, local: datetime.datetime) -> dict:
        """Change how many world days open in a real day, from today on."""
        def work():
            now = read_calendar(dict(self.db.execute("SELECT key, value FROM meta"))) or new_calendar(local)
            now = retimed(now, tempo, local)
            self._put_calendar(now)
            return now
        return self._write(work)

    def close(self) -> None:
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None

    def __del__(self):
        self.close()                      # the file is let go when the store is, not whenever memory is tidied

    @classmethod
    def close_all(cls) -> None:
        for store in list(cls.opened):
            store.close()


def set_tempo(folder: Path, tempo: int, now=time.time) -> list[str]:
    """Set the tempo of every world in a data folder, from today on. Returns a line for each."""
    lines = []
    for path in sorted(Path(folder).glob("fair_winds-*.db")):
        db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_SECONDS)
        try:
            meta = dict(db.execute("SELECT key, value FROM meta"))
        except sqlite3.Error as error:
            # A world some caller is in the middle of making, or a file that is not one: left alone, and said.
            lines.append(f"{path.name}: skipped ({error})")
            continue
        finally:
            db.close()
        if "seed" not in meta:
            lines.append(f"{path.name}: skipped (no world in it yet)")
            continue
        store = Store(folder, int(meta["seed"]), meta.get("node", ""))
        try:
            local = local_time(now(), meta.get("timezone", "local"))
            cal = store.set_tempo(tempo, local)
        finally:
            store.close()
        opens = calendar_opens(cal, cal["high_day"] + 1)
        lines.append(f"{path.name}: tempo {tempo}, day {cal['high_day']} today, "
                     f"day {cal['high_day'] + 1} opens {opens:%Y-%m-%d %H:%M}")
    return lines


def backup_worlds(source: Path, target: Path) -> list[str]:
    """Copy every world's records in a data folder to another folder. Safe while callers are playing."""
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if source.exists() and target.resolve() == source.resolve():
        raise OSError("the backup folder is the data folder: a copy there would replace the live records")
    done = []
    worlds = sorted(source.glob("fair_winds-*.db"))
    for path in worlds:                         # every refusal comes before the first copy
        if any((target / (path.name + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise OSError(f"{target} holds a database in use under the name {path.name}")
    for path in worlds:
        temp = target / (path.name + ".tmp")
        temp.unlink(missing_ok=True)
        src = dst = None
        try:
            src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
            dst = sqlite3.connect(str(temp))
            src.backup(dst)
            dst.execute("DELETE FROM sessions")     # nobody is flying in a copy: a restore keeps no caller out
            dst.commit()
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError(f"the copy of {path.name} did not pass its check")
        except BaseException:
            for db in (src, dst):
                if db is not None:
                    db.close()
            temp.unlink(missing_ok=True)        # no half copy is left behind
            raise
        src.close()
        dst.close()
        temp.replace(target / path.name)
        done.append(path.name)
    return done


class PlainFlight:
    """One pilot's game on the plain screen.

    Every committed turn is saved before its result is shown, so a session
    can end at any moment and resume on the same turn.
    """

    def __init__(self, launch: Launch, folder: Path | None, environ=None, now=time.time):
        self.launch = launch
        self.now = now                    # seconds since the epoch: what day it is hangs on this and nothing else
        self.zone = "local" if launch.host == "standalone" else launch.timezone
        self.today, self.calendar = 1, None
        self._last_look: float | None = None
        node = launch.node_id or "standalone"
        self.world = World(world_seed(node), folder)
        self.weather = Weather(self.world)
        self.map = MapView(self.world, self.weather, want_pixels(launch, environ or {}))
        self.engine = Flight(self.world, self.weather, f"{node}/{launch.user_id}")
        self.saved_ok = True
        self.log: list[str] = []
        self.typed_ahead = False          # set when a turn was committed: later keys in the same read are dropped
        self.motion = want_motion(launch, environ or {}) and self.map.pixels      # the character map's wind holds still
        self.frames_left = 0              # frames of moving wind still to show
        self.pending_scene: dict | None = None    # a scene the last turn earned, for the session to play
        # What the last turn said, each line whole (the log keeps three, wrapped).
        # A turn empties it first; lines said between turns sit here unread.
        self.heard: list = []
        self.opening_notice = ""          # shown on the first screen, where it cannot scroll away
        self.hub = False                  # the post office screen is open
        self.loft = False                 # the rigging loft screen is open
        self.others: list = []            # other pilots in this world: (x, y, colour), for the map
        self.stations: list = list(self.world.stations)   # the world's first three, then every one planted since
        self._own = None                  # with no records to share: the stations this visit plants
        self.kits = self.crates = 0       # a station kit aboard, and crates of mast freight
        self.calls: list = []             # other pilots who are stranded and waiting
        self._called = False              # this pilot's own call for help is out
        mode = (environ or {}).get("DOOR_FW_GUESTBOOK", "on").strip().lower()
        self.book_mode = mode if mode in GUESTBOOK_MODES else "on"
        self.book: dict | None = None     # a station's visitors' book, open
        self.writing: str | None = None   # the line being written in it
        self.known = known_cells(self.world)      # what was on the map before anybody flew
        self.chart: set = set()           # every cell anybody has charted since
        self.found: dict = {}             # features found: id -> (name, user_id, handle, day)
        self.chart_version = 0            # goes up whenever the chart changes: the map composes again
        self.charted = 0                  # cells of land this pilot was first to chart
        self.naming: list = []            # features this pilot has just found and may name
        self.flight_cells = self.flight_pay = 0       # what this flight has charted so far, said when it is down
        self.atlas = False                # the chart of the whole continent is open
        # Isobars from the start where there are pixels to draw them fine; in characters they are asked for (I).
        self.atlas_view = {"zoom": 0, "x": WORLD_W / 2, "y": WORLD_H / 2, "isobars": self.map.pixels, "names": True,
                           "find": None, "note": "", "found": None}
        self._atlas_made: dict | None = None
        self.route: list = []             # where each flight ended, oldest first
        self.winds = local_winds(self.world, self.weather)
        self.almanac: dict = {}           # local winds entered: number -> (user, handle, day)
        self.flown: dict = {}             # days this pilot has flown each local wind while it blew
        self.pages = False                # the almanac is open
        self.wonders = wonders_of(self.world, self.weather)
        self.sighted: dict = {}           # wonders seen on the node: number -> (user, handle, day)
        self.album: set = set()           # the wonders this pilot has seen
        self.album_open = False           # the album is open
        self.season_open = False          # the season's screen is open
        self.hall: dict = {}              # what was done first on the node: feat -> (user, handle, day)
        self.eye = False                  # this pilot has been in the eye of the Gyre
        self.season_now: dict = {"season": 0}     # the season as last read
        self.season_past: list = []       # the seasons before it, latest first
        self._claimed: set = set()        # closed seasons this session has asked its dues of
        self._season_done = None          # the (season, day) the seasons were last settled for
        self._season_wait = 0.0           # not before this moment, after a settling that could not be written
        self._season_dues: list = []      # closed seasons that still owe this pilot an answer
        self._claim_wait = 0.0            # not before this moment, after dues that could not be written
        self._seasons_read: dict = {}     # closed seasons as read: they change no more
        self.album_page = 0
        self._wonder_said = None          # the wonder last pointed out, so it is pointed out once
        self.drift: dict | None = None    # a Drift is being flown: its course, and whether it counts
        self.drift_lobby = False          # the Daily Drift's own screen is open
        self.drift_note = ""
        self._drift_seen: dict = {"table": [], "mine": None, "day": 0}
        self._main = None                 # the pilot's own flight, set aside while a Drift is flown
        self.trials = False               # the Trials' own screen is open
        self.trial_at = 0                 # the line chosen there: 0 is the first flight
        self.trial_note = ""
        self.medals = [0] * len(TRIALS)   # the pilot's best medal in each trial
        self.taught = False               # the first flight has been flown through
        self._blowing_at = None
        self._chart_size = None
        self._share = None
        self._grade_seen = 0
        self.hub_note = ""                # what the last thing done there came to
        self._vista = None                # the picture over the post office, kept while it is the same picture
        self.busy: dict | None = None     # another live session holds this pilot: who, until the caller decides
        self.ended = ""                   # "taken": another session took this pilot over, and this one is done
        self._last_beat: float | None = None
        self.store: Store | None = None
        # The save file of the plain flight before there was a database: read once, then left behind.
        self.legacy = None
        if folder is not None and not launch.guest:
            self.legacy = Path(folder) / f"pilot-{self.world.seed:016x}-{launch.user_id}.json"
            try:
                self.store = Store(folder, self.world.seed, node,
                                   journal=(environ or {}).get("DOOR_FW_JOURNAL", "wal"))
                self.busy = self.store.claim(launch.user_id, self._transport())
            except (StoreUnavailable, sqlite3.Error) as error:
                # Nothing can be kept here. The caller plays on and is told; the SysOp is told why.
                diagnose(f"records cannot be kept: {error}")
                self._drop_store()
        if self.busy is None:
            self._begin()

    def _transport(self) -> str:
        return "a local terminal" if self.launch.host == "standalone" else (self.launch.transport or "the BBS")

    def _drop_store(self) -> None:
        """Play on without records: give the pilot up first, so nobody is kept out by a session that keeps nothing."""
        if self.store is not None:
            try:
                self.store.release(self.launch.user_id)
            except sqlite3.Error:
                pass
            self.store.close()
        self.store, self.saved_ok = None, False

    def _begin(self) -> None:
        """Take up the pilot's game where it stands, or start one."""
        self._look_at_the_calendar()
        self._see_stations()              # before the pilot is read: cargo may be bound for a mast
        loaded = self._load()
        if loaded is not True:
            self._new_game()
        elif not self._dated:
            # A pilot from before the calendar counted days of their own. Theirs
            # may lie ahead of the world's: it becomes today, once.
            if self.state.day > self.today:
                self.state = dataclasses.replace(self.state, day=self.today)
            self._dated = True
        self._catch_up()
        if loaded in ("damaged", "unreachable"):
            # Saving now would destroy a record somebody might still want back.
            self.opening_notice = ("Your saved game could not be read. This game is not saved. Tell the SysOp."
                                   if loaded == "damaged" else
                                   "The records are busy. This game is not saved; yours is safe. Try again soon.")
            self._drop_store()
        self.save()                       # so a game that cannot be saved says so before the first turn
        self._grade_seen = self.grade()
        self._see_others()
        self._see_chart()
        self._see_calls()
        self._see_almanac()
        self._see_wonders()
        self._see_season()
        self._see_hall()
        self._sync_call()
        if self._imported and self.saved_ok and self.store is not None and not self.ended:
            # Only once the database has the pilot. And never over an earlier file of that name.
            aside = self.legacy.with_name(self.legacy.name + ".imported")
            try:
                if not aside.exists():
                    self.legacy.replace(aside)
            except OSError:
                pass                      # it stays; the database has the pilot now and is read first
        self._choose_default()

    # -- the save ------------------------------------------------------------

    def _new_game(self) -> None:
        start = self.world.towns[self.world.start_town]
        self.state = FlightState(x=start["x"], y=start["y"], day=self.today)      # a new pilot starts on today
        self._dated = True
        self.deliveries, self.days_flown, self.total_km = 0, 0, 0.0
        self.postmarks, self.manifest, self.taken, self.next_id = START_POSTMARKS, [], [], 1
        self.gear, self.points, self.design = {track: 0 for track in TRACKS}, 0, design_of(None)
        self.lift_off, self.charted = None, 0
        self.kits = self.crates = 0
        self.route = [[round(start["x"], 1), round(start["y"], 1)]]
        self.medals, self.taught = [0] * len(TRIALS), False
        self.eye = False
        self._fit()
        home = ("town", self.world.start_town)
        for dest in places_near(self.world, start["x"], start["y"], skip=home, stations=self.stations)[:POST_CHOICES]:
            # A first round, already aboard: three mailbags for the three nearest places.
            there = self.place_of(dest)
            km = math.sqrt((there["x"] - start["x"]) ** 2 + (there["y"] - start["y"]) ** 2)
            kind = CARGO_KINDS["mail"]
            self._load_cargo({"contract": None, "kind": "mail", "dest": list(dest), "origin": list(home),
                              "weight": kind["weight"], "payout": int(kind["base"] + kind["per_km"] * km + 0.5),
                              "due": self.today + FIRST_ROUND_DAYS})
        self.log = []
        self._say(f"You are at {start['name']}. There is post aboard for {self._names()}.")
        self._say("Read the winds and fly to whichever the day favours. New to it? R teaches.")

    _imported = False
    _dated = False                        # the pilot's days are the world's days

    # -- the calendar --------------------------------------------------------

    def _local(self) -> datetime.datetime:
        return local_time(self.now(), self.zone)

    def _look_at_the_calendar(self) -> bool:
        """Find out what day it is. True if it is a later day than was known."""
        before, local = self.today, self._local()
        if self.store is not None:
            try:
                self.today, self.calendar = self.store.today(local, self.zone)
            except sqlite3.Error as error:
                diagnose(f"the calendar cannot be read: {error}")
        if self.store is None or self.calendar is None:
            # No records: a calendar of this visit's own, beginning today.
            self.calendar = self.calendar or new_calendar(local)
            self.calendar["high_day"] = self.today = calendar_day(self.calendar, local)
        return self.today > before

    def _catch_up(self) -> bool:
        """At most three opened days wait. Older ones lapse, without penalty, when no day is in progress."""
        state = self.state
        first = self.today - (WAITING_DAYS - 1)
        if state.day >= first or state.turn > 0 or state.status != "ground":
            return False                  # nothing has lapsed, or a day in progress keeps its day
        lapsed = first - state.day
        self.state = dataclasses.replace(state, day=first)
        self._say(f"{lapsed} day{'s' if lapsed != 1 else ''} went by unflown. Day {first}.")
        return True

    def waiting(self) -> bool:
        """Has the pilot flown every day that has opened?"""
        return self.state.day > self.today and self.drift is None       # a trial's day is not a day of the calendar

    def offered(self, state: FlightState | None = None) -> dict:
        """What the pilot may do now: nothing, on a day that has not opened."""
        state = state or self.state
        if self.drift is not None:
            return self.engine.actions(state)     # a Drift or a trial has weather of its own day: it is always open
        return {} if state.day > self.today else self.engine.actions(state)

    def briefing(self) -> str:
        """When the next day opens, and how many opened days still wait. The day being flown heads the screen."""
        local = self._local()
        mine = self._main[0] if self._main is not None else self.state       # the pilot's own flight, set aside or not
        if self.waiting():
            opens = when_text(calendar_opens(self.calendar, self.state.day), local, self.launch.datetime_format)
            return f"Day {self.state.day} opens {opens}."
        more = max(0, self.today - mine.day)
        opens = when_text(calendar_opens(self.calendar, self.today + 1), local, self.launch.datetime_format)
        wait = "" if more == 0 else " One more day waits." if more == 1 else \
            f" {'Two' if more == 2 else more} more days wait."
        return f"Day {self.today + 1} opens {opens}.{wait}"

    def _load(self):
        """True if the pilot's game was read, False if there is none, "damaged" if
        there is one that cannot be used.

        Only a pilot never seen gets a new game that is saved. A record that
        is there but cannot be read is never written over.
        """
        if self.store is None:
            return False
        try:
            row = self.store.pilot(self.launch.user_id)
        except sqlite3.Error as error:
            diagnose(f"the pilot's record cannot be read: {error}")
            return "unreachable" if unreachable(error) else "damaged"
        if row is None:
            return self._import()
        try:
            self._take(json.loads(row[0]), json.loads(row[1]))
            return True
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, ArithmeticError):
            diagnose(f"the record of pilot {self.launch.user_id} cannot be used")
            return "damaged"

    def _take(self, career: dict, state: dict):
        """Set the game from stored data. If it is not a game it raises one of the errors _load reads as damage."""
        flight = FlightState.from_dict(state)
        deliveries, days_flown, total_km = int(career["deliveries"]), int(career["days_flown"]), float(career["total_km"])
        log = [str(line) for line in career.get("log", [])][-3:]
        if "manifest" in career:
            manifest = [self._cargo_item(item) for item in career["manifest"]]
            postmarks, next_id = max(0, int(career["postmarks"])), int(career["next_id"])
            taken = [[str(t[0]), int(t[1]), int(t[2]), int(t[3])] for t in career.get("taken", [])]
            aboard = {item[0] for item in flight.cargo}
            fragile = {item[0]: bool(item[1]) for item in flight.cargo}
            if aboard != {item["id"] for item in manifest} or any(
                    item["id"] >= next_id or fragile[item["id"]] != CARGO_KINDS[item["kind"]]["fragile"]
                    for item in manifest):
                raise ValueError("the cargo and its papers do not agree")
        else:
            # A pilot from before the post office carried "post for" up to three places. Each is a mailbag now.
            manifest, taken, postmarks, next_id = [], [], START_POSTMARKS, 1
            for dest in [self._ref(t) for t in career["targets"]][:POST_CHOICES]:
                there = self.place_of(dest)
                km = math.sqrt((there["x"] - flight.x) ** 2 + (there["y"] - flight.y) ** 2)
                kind = CARGO_KINDS["mail"]
                manifest.append({"id": next_id, "contract": None, "kind": "mail", "dest": list(dest), "origin": None,
                                 "weight": kind["weight"], "payout": int(kind["base"] + kind["per_km"] * km + 0.5),
                                 "due": flight.day + FIRST_ROUND_DAYS})
                next_id += 1
            flight = dataclasses.replace(flight, cargo=tuple((item["id"], False, "ok") for item in manifest))
        gear = {track: int(career.get("gear", {}).get(track, 0)) for track in TRACKS}
        if any(not 0 <= tier < len(UPGRADES[track]["tiers"]) for track, tier in gear.items()):
            raise ValueError("no such gear")
        # A pilot from before the licence is credited with the deliveries they made.
        points = int(career["points"]) if "points" in career else deliveries * POINTS["mail"]
        kits, crates = int(career.get("kits", 0)), int(career.get("crates", 0))
        if not (0 <= kits <= 1 and 0 <= crates <= 99):
            raise ValueError("no such freight")
        route = [[float(point[0]), float(point[1])] for point in career.get("route", [])][-ROUTE_KEPT:]
        if not all(math.isfinite(v) for point in route for v in point):
            raise ValueError("a route through nowhere")
        medals = [int(m) for m in career.get("medals", [])][:len(TRIALS)]
        medals += [0] * (len(TRIALS) - len(medals))
        if any(not 0 <= m < len(TRIAL_MEDALS) for m in medals):
            raise ValueError("no such medal")
        self._dated = bool(career.get("dated", False))
        self.state, self.manifest, self.taken = flight, manifest, taken
        self.postmarks, self.next_id = postmarks, next_id
        self.deliveries, self.days_flown, self.total_km, self.log = deliveries, days_flown, total_km, log
        self.gear, self.points, self.design = gear, max(0, points), design_of(career.get("design"))
        self.charted = max(0, int(career.get("charted", 0)))
        self.kits, self.crates = kits, crates
        self.route = route
        self.medals, self.taught = medals, bool(career.get("taught", False))
        self.eye = bool(career.get("eye", False))
        spot = career.get("lift_off")
        self.lift_off = [float(spot[0]), float(spot[1])] if spot else None
        if self.lift_off is not None and not all(math.isfinite(v) for v in self.lift_off):
            raise ValueError("a flight began nowhere")
        self._fit()

    def _cargo_item(self, item: dict) -> dict:
        """One line of the manifest, checked. Raises ValueError, KeyError or TypeError if it is not one."""
        if item["kind"] not in CARGO_KINDS:
            raise ValueError("no such cargo")
        km = item.get("km")
        if km is not None and not (math.isfinite(float(km)) and float(km) >= 0):
            raise ValueError("cargo from nowhere")
        return {"id": int(item["id"]), "contract": item.get("contract"), "kind": item["kind"],
                "dest": list(self._ref(item["dest"])), "origin": item.get("origin"),
                "weight": int(item["weight"]), "payout": int(item["payout"]), "due": int(item["due"]),
                "km": None if km is None else float(km)}

    def _import(self):
        """A pilot the database has never seen may have a save file from before it. Take that over, once."""
        try:
            text = self.legacy.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return False                  # a new pilot
        except UnicodeDecodeError:
            text = ""
        except OSError as error:
            # It is there and cannot be opened: a matter of minutes for the SysOp.
            # A new game saved now would shut the old one out for good, so nothing is saved this call.
            diagnose(f"the old save file {self.legacy.name} cannot be opened: {error}")
            return "unreachable"
        try:
            data = json.loads(text)
            if data["version"] != PLAIN_SAVE_VERSION or data["seed"] != self.world.seed:
                raise ValueError("not this world's save")
            self._take(data, data["state"])
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, ArithmeticError):
            # It is left where it is, and both the pilot and the SysOp are told.
            diagnose(f"the old save file {self.legacy.name} cannot be used; it is left in place")
            self.opening_notice = "An older saved game could not be read. This is a new one; the file is kept."
            return False
        self._imported = True
        return True

    def _career(self) -> dict:
        return {"manifest": self.manifest, "taken": self.taken, "postmarks": self.postmarks, "next_id": self.next_id,
                "gear": self.gear, "points": self.points, "design": self.design, "lift_off": self.lift_off,
                "charted": self.charted, "kits": self.kits, "crates": self.crates, "route": self.route,
                "medals": self.medals, "taught": self.taught, "eye": self.eye,
                "deliveries": self.deliveries, "days_flown": self.days_flown, "total_km": self.total_km,
                "log": self.log[-3:], "dated": self._dated}

    def save(self, action: str = "", chart=(), deed=None, earn=None) -> None:
        """Write the game as it stands; with an action, as a committed turn in the day's journal.

        chart is the cells the turn saw. Which of them are new, and whether a
        feature is found, is settled where it is written, so two pilots over
        the same ground at once are each credited with what was theirs.
        deed is something the turn does to the shared world in the same
        write, and earn takes what came of it.
        """
        if self.drift is not None:
            return                        # a Drift is flown on a balloon that is not the pilot's: nothing of it is kept here
        cells = [cell for cell in chart if cell not in self.chart and cell not in self.known]
        watch = self._watch(cells)
        if self.ended:
            return
        if self.store is None:
            # A visit, or records out of reach: the chart is this session's own.
            found = [fid for fid, (group, need, _) in watch.items()
                     if fid not in self.found and sum(c in self.chart or c in cells for c in group) >= need]
            for fid in found:
                self.found[fid] = (watch[fid][2], self.launch.user_id, self.launch.handle, self.state.day)
            self._charted(cells, found)
            if deed is not None:
                earn(deed(self._own_db()))
            return

        def earned(new, found, done=None):
            self._charted(new, found)     # what the charting paid
            if deed is not None:
                earn(done)                # and what the deed came to
            return self._career()         # the career as it stands after both
        # What the turn earns is worked out inside its transaction. If the transaction does not go through,
        # the earnings must not stand either: or the ground would be paid for now and again by whoever is
        # written down as first over it.
        kept = (self.charted, self.postmarks, self.points, self.flight_cells, self.flight_pay, set(self.chart),
                dict(self.found), list(self.naming), list(self.log), list(self.heard), self.chart_version,
                dict(self.almanac), dict(self.flown), dict(self.sighted), set(self.album), self.eye,
                dict(self.hall))
        try:
            self.store.commit(self.launch.user_id, self.launch.handle,
                              earned if cells or deed is not None else self._career(),
                              self.state.to_dict(), action, cells, watch, deed)
            self.saved_ok = True
            if cells:
                self.chart.update(cells)          # seen, and on the chart: first over them or not
                self.chart_version += 1
        except (LeaseLost, sqlite3.Error) as error:
            if cells or deed is not None:
                (self.charted, self.postmarks, self.points, self.flight_cells, self.flight_pay, self.chart,
                 self.found, self.naming, self.log, self.heard, self.chart_version, self.almanac, self.flown,
                 self.sighted, self.album, self.eye, self.hall) = kept
            if isinstance(error, LeaseLost):
                self.ended = "taken"
            else:
                diagnose(f"a turn could not be saved: {error}")
                self.saved_ok = False       # play on; say so on the screen
            return

    # -- the Daily Drift -------------------------------------------------------

    def help_text(self) -> tuple:
        """The help for where the caller is: the Drift has its own."""
        if self.trials or (self.drift is not None and self.drift.get("kind") == "trial"):
            return TRIAL_HELP
        return DRIFT_HELP if self.drift_lobby or self.drift is not None else PLAIN_HELP

    def _shared(self, work, write: bool = False):
        """Read or write the shared world alone: the records, or with none this visit's own."""
        if self.store is None:
            return work(self._own_db())
        # A write is this pilot's only while the pilot is this session's.
        return self.store.write(work, self.launch.user_id) if write else self.store.read(work)

    def _drift_open(self) -> None:
        self.drift_lobby, self.drift_note = True, ""
        self._drift_look()

    def _drift_look(self) -> None:
        """The day's table, and how this pilot's own Drift stands."""
        self._day_may_have_opened()       # the whole of it: a day that opened is the pilot's own flight's too
        day, me = self.today, self.launch.user_id
        try:
            self._drift_seen = {"day": day, "table": self._shared(lambda db: drift_table(db, day)),
                                "mine": self._shared(lambda db: my_drift(db, day, me))}
        except sqlite3.Error:
            self._drift_seen = {"day": day, "table": [], "mine": None}

    def _drift_lobby_key(self, k: str) -> None:
        if k in ("ESC", "b", "t", " "):
            self.drift_lobby = False
        elif k in ("p", "ENTER"):
            self._day_may_have_opened()
            if self.today != self._drift_seen["day"]:
                # A day opened while the screen stood open: the course on it was yesterday's.
                self._drift_look()
                self.drift_note = "A new day has opened. This is its course."
            else:
                self._drift_begin(scored=k == "ENTER")

    def _drift_begin(self, scored: bool) -> None:
        """Set the pilot's own flight aside and stand the standard balloon at the start."""
        if self.store is not None and not self._mine(write=False):
            return
        day, launch = self.today, self.launch
        course = drift_course(self.world, self.weather, day)
        state = drift_state(course["x"], course["y"], day)
        if scored:
            text = json.dumps(state.to_dict())
            try:
                row = self._shared(lambda db: begin_drift(db, day, launch.user_id, launch.handle, text, self.now()),
                                   write=True)
                state = FlightState.from_dict(json.loads(row["state"]))
            except LeaseLost:
                self.ended = "taken"
                return
            except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
                diagnose(f"the Drift could not be begun: {error}")
                self.drift_note = "The Drift cannot be flown for the table just now. P practises."
                return
            if row["done"]:
                self.drift_note = "You have flown today's Drift. The table stands. P practises."
                self._drift_look()
                return
        self._main = (self.state, self.engine, self.gear, self.choice, self.log, self.heard)
        self.drift = {"course": course, "scored": scored}
        self.state, self.engine = state, Flight(self.world, self.weather, "drift")
        self.gear = {track: 0 for track in TRACKS}
        self.log, self.heard, self.drift_lobby = [], [], False
        dx, dy = course["mx"] - course["x"], course["my"] - course["y"]
        self._say(f"The Daily Drift, {'for the table' if scored else 'in practice'}: from {course['start']} to the "
                  f"mark, {math.sqrt(dx * dx + dy * dy):.0f} km {bearing(dx, dy)}.")
        self._say(f"{DRIFT_TURNS} turns from 09:00. Land as near the mark as you can. X leaves.")
        self._choose_default()
        if state.status not in ("ground", "aloft") or state.turn - DRIFT_START_TURN >= DRIFT_TURNS \
                or (state.status == "ground" and state.turn > DRIFT_START_TURN):
            self._drift_end()             # taken up after its last turn was written: all that is left is the score

    def _drift_commit(self, action: str) -> None:
        """A turn of the Drift: flown like any other, but nothing of it is charted, delivered or kept as the pilot's."""
        drift, before = self.drift, self.state
        result = self.engine.step(before, action)
        # The km of a Drift are not the pilot's km: they count for nothing in the logbook.
        self.state = dataclasses.replace(result.state, distance=0.0)
        self.heard = []
        stamp = clock_text(before.minute)
        for event in result.events:
            line = self._describe(event)
            if line:
                self._say(f"{stamp}  {line}")
        if drift.get("kind") == "trial":
            self._tutor(action)
        if self.state.status == "aloft" and \
                self.state.turn - drift.get("from", DRIFT_START_TURN) >= drift.get("turns", DRIFT_TURNS):
            events: list = []
            self.state = self.engine._land(self.state, events)
            self._say(f"{clock_text(self.state.minute)}  Time is up: the balloon comes down where it is.")
        if drift["scored"]:
            text, day, me = json.dumps(self.state.to_dict()), drift["course"]["day"], self.launch.user_id
            def written(db):
                if not turn_drift(db, day, me, text):       # no row took it: as good as not written
                    raise sqlite3.DatabaseError("the Drift's record is not there to be written to")
            try:
                self._shared(written, write=True)
            except LeaseLost:
                self.ended = "taken"
                return
            except sqlite3.Error as error:
                # A turn that is not on record cannot count: or it could be flown again, knowing how it went.
                # The Drift is parked where the records have it.
                diagnose(f"a turn of the Drift could not be saved: {error}")
                self._drift_end(left=True)
                self.drift_note = "That turn could not be written. The Drift waits where it was: Enter takes it up."
                return
        if action != "pibal":
            self.typed_ahead = True
            if self.motion:
                self.frames_left = BURST_FRAMES
        if self.state.status != "aloft":
            self._drift_end()
        else:
            self._choose_default()

    def _drift_end(self, left: bool = False) -> None:
        """The Drift is over, or left: the pilot's own flight is taken up again, and a finished one is scored."""
        drift, state = self.drift, self.state
        if drift.get("kind") == "trial":
            self._trial_end(left)
            return
        course = drift["course"]
        km = math.sqrt((state.x - course["mx"]) ** 2 + (state.y - course["my"]) ** 2)
        turns, fuel = state.turn - DRIFT_START_TURN, state.fuel
        score = drift_score(km, turns, fuel, state.status == "ditched")
        self.state, self.engine, self.gear, self.choice, self.log, self.heard = self._main
        self.drift, self._main, self.drift_lobby = None, None, True
        self.frames_left = 0
        how = f"{km:.1f} km from the mark in {turns} turns with {fuel} fuel left"
        if left:
            self.drift_note = ("Left part way. It waits as you left it until the day is out: Enter takes it up."
                               if drift["scored"] else "Practice left.")
        elif not drift["scored"]:
            self.drift_note = f"Practice: {how}. That would score {score}."
        else:
            day, me, pay = course["day"], self.launch.user_id, score // DRIFT_PAY_PER
            paid = []

            def apply(done):
                if done:
                    self.postmarks += pay
                    paid.append(pay)
            wrote = self._deed("drift", lambda db: finish_drift(db, day, me, score, km, turns, fuel, self.now()), apply)
            if paid:
                self.drift_note = f"Scored {score}: {how}. {pay} postmarks."
            elif wrote:
                self.drift_note = "Today's Drift is on the table already."
            else:
                self.drift_note = "The score could not be written. Enter takes the Drift up again."
        self._drift_look()
        self._choose_default()

    def _draw_drift(self, s: "Session") -> list[str]:
        """The Daily Drift's own screen: today's course, the rules in short, the table, and what to press."""
        wide, seen = s.width - 2, self._drift_seen
        day = seen["day"] or self.today
        course = drift_course(self.world, self.weather, day)
        dx, dy = course["mx"] - course["x"], course["my"] - course["y"]
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, "The Daily Drift", "text")]
        out.append(s._kv_right(0, s.width - 1, f"Day `{day}`"))
        lines = [f"Today's course: from `{course['start']}` to a mark `{math.sqrt(dx * dx + dy * dy):.0f} km "
                 f"{bearing(dx, dy)}` of it, for every pilot.",
                 f"The standard balloon: `{DRIFT_BALLOON['fuel']}` fuel, `{DRIFT_BALLOON['ballast']}` sacks, "
                 f"`{DRIFT_BALLOON['pibals']}` pibal. `{DRIFT_TURNS}` turns from `09:00`.",
                 f"Score: `{DRIFT_SCORE['full']}`, less `{DRIFT_SCORE['km']}` a km from the mark, less "
                 f"`{DRIFT_SCORE['turn']}` a turn, plus `{DRIFT_SCORE['fuel']}` a fuel left.",
                 "Water scores nothing. Level: the nearer landing, then fewer turns, then first."]
        for i, line in enumerate(lines):
            out.append(s._kv(2 + i, 1, line, wide))
        out.append(s._line(7, 1, "TODAY'S TABLE", "accent"))
        me = clean_text(self.launch.handle, 16).replace("`", " ")
        for i, (handle, score, km, turns) in enumerate(seen["table"][:max(1, s.height - 14)]):
            name = clean_text(str(handle), 16).replace("`", " ") or "A pilot"
            out.append(s._kv(8 + i, 1, f"{i + 1:>2}  {name:<17}`{int(score):>5}`   {float(km):>5.1f} km  "
                                       f"{int(turns):>2} turns", wide, "accent" if name == me else "text", "value"))
        if not seen["table"]:
            out.append(s._line(8, 1, "Nobody has flown it yet today.", "dim"))
        mine = seen["mine"]
        if mine is None:
            stand, keys = "You have not flown it today.", [("Enter", "Fly it for the table")]
        elif not mine["done"]:
            stand, keys = "Your Drift waits part way.", [("Enter", "Take it up")]
        else:
            stand, keys = f"You scored {mine['score']} today.", []
        out.append(s._line(s.height - 4, 1, stand[:wide], "text"))
        if self.drift_note:
            out.append(s._line(s.height - 3, 1, self.drift_note[:wide], "bright"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, keys + [("P", "Practise"), ("?", "The rules"), ("B", "Back")])
        return out

    # -- the Trials --------------------------------------------------------------

    def _trials_key(self, k: str) -> None:
        if k in ("w", "UP"):
            self.trial_at = max(0, self.trial_at - 1)
        elif k in ("s", "DOWN"):
            self.trial_at = min(len(TRIALS), self.trial_at + 1)
        elif k == "ENTER":
            self._trial_begin(self.trial_at - 1)
        elif k in ("ESC", "b", "r", " "):
            self.trials = False

    def _trial_begin(self, index: int) -> None:
        """Set the pilot's own flight aside and stand the trial's balloon at its start. -1 is the first flight."""
        if self.store is not None and not self._mine(write=False):
            return
        spec = TUTORIAL if index < 0 else TRIALS[index]
        course = trial_course(self.world, self.weather, index)
        self._main = (self.state, self.engine, self.gear, self.choice, self.log, self.heard)
        spare = TUTORIAL_SPARE_TURNS if index < 0 else TRIAL_SPARE_TURNS
        self.drift = {"course": course, "scored": False, "kind": "trial", "trial": index, "from": spec["start"],
                      "turns": len(spec["script"]) + spare, "title": spec["name"], "step": 0, "done": set()}
        self.state = trial_state(spec, course["x"], course["y"], course["day"])
        self.engine = Flight(self.world, self.weather, "trial")
        self.gear = {track: 0 for track in TRACKS}
        self.log, self.heard, self.trials = [], [], False
        dx, dy = course["mx"] - course["x"], course["my"] - course["y"]
        if index < 0:
            self._say(TUTOR_STEPS[0][1])
        else:
            trip = math.sqrt(dx * dx + dy * dy)
            bronze, silver, gold = trial_limits(trip)
            self._say(f"{spec['name']}. {spec['about']} The mark is {trip:.0f} km {bearing(dx, dy)}.")
            self._say(f"Gold within {gold:.0f} km of it, silver {silver:.0f}, bronze {bronze:.0f}. X twice leaves.")
        self._choose_default()

    def _tutor(self, did: str) -> None:
        """The first flight: a lesson is passed by doing what it asks for, in whatever order.

        The log asks for the first lesson not yet done. A caller who
        launches before looking has passed the launch, and is asked to look.
        """
        drift = self.drift
        names = [step[0] for step in TUTOR_STEPS]
        if drift is None or drift.get("trial", 0) >= 0 or did not in names or did in drift["done"]:
            return
        drift["done"].add(did)
        drift["step"] = len(drift["done"])
        todo = next((text for name, text in TUTOR_STEPS if name not in drift["done"]), None)
        if todo is not None:
            self._say(todo)

    def _trial_end(self, left: bool) -> None:
        """A trial is over, or left: the pilot's own flight is taken up again, and a better medal is kept."""
        drift, state = self.drift, self.state
        course, index = drift["course"], drift["trial"]
        km = math.sqrt((state.x - course["mx"]) ** 2 + (state.y - course["my"]) ** 2)
        trip = math.sqrt((course["x"] - course["mx"]) ** 2 + (course["y"] - course["my"]) ** 2)
        # A trial that could not be laid out in this world has no flight to vouch for its mark: it wins nothing.
        medal = trial_medal(km, state.status, trip) if course["gold"] else 0
        self.state, self.engine, self.gear, self.choice, self.log, self.heard = self._main
        self.drift, self._main, self.trials = None, None, True
        self.frames_left = 0
        kept = (list(self.medals), self.postmarks, self.taught)

        def keep() -> bool:
            """Write the pilot's record, with no line in the journal: a trial is not a turn of theirs. If it
            cannot be written the medal is not held either, or it would be shown and paid and then gone."""
            self.save()
            if self.store is not None and (self.ended or not self.saved_ok):
                self.medals, self.postmarks, self.taught = kept
                self.trial_note = "That could not be written down, so it does not stand. Fly it again."
                return False
            return True
        if left:
            self.trial_note = "Left. It is there to be flown again."
        elif index < 0:
            if drift["step"] >= len(TUTOR_STEPS):
                first, self.taught = not self.taught, True
                self.trial_note = "Down, and every lesson done. That is all there is to flying: the rest is the wind."
                if first:
                    keep()
            else:
                self.trial_note = (f"Down with {len(TUTOR_STEPS) - drift['step']} of the lessons still to do. "
                                   f"Fly it again any time.")
        else:
            name = TRIALS[index]["name"]
            how = "on the water" if state.status == "ditched" else f"{km:.1f} km from the mark"
            if medal > self.medals[index]:
                pay = TRIAL_PAY[medal] - TRIAL_PAY[self.medals[index]]
                self.medals = self.medals[:index] + [medal] + self.medals[index + 1:]
                self.postmarks += pay
                self.trial_note = f"{name}: down {how}. {TRIAL_MEDALS[medal].capitalize()}, and {pay} postmarks."
                keep()
            elif medal:
                self.trial_note = f"{name}: down {how}. {TRIAL_MEDALS[medal].capitalize()}: no better than your best."
            elif not course["gold"]:
                self.trial_note = f"{name} cannot be laid out in this world: there is no medal to be had in it."
            else:
                self.trial_note = f"{name}: down {how}. No medal: bronze is within {trial_limits(trip)[0]:.0f} km."
        self._choose_default()

    def _draw_trials(self, s: "Session") -> list[str]:
        """The Trials' own screen: the first flight and the twelve, with the pilot's medals."""
        wide = s.width - 2
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, "The Trials", "text")]
        counts = [self.medals.count(m) for m in (3, 2, 1)]
        out.append(s._kv_right(0, s.width - 1, f"`{counts[0]}` gold  `{counts[1]}` silver  `{counts[2]}` bronze"))
        specs = (TUTORIAL,) + TRIALS
        room = s.height - 6
        first = max(0, min(self.trial_at - room + 1, len(specs) - room)) if len(specs) > room else 0
        for row, i in enumerate(range(first, min(len(specs), first + room))):
            spec, chosen = specs[i], i == self.trial_at
            if i == 0:
                won = "flown" if self.taught else "not yet flown"
            else:
                won = TRIAL_MEDALS[self.medals[i - 1]]
            about = spec["about"][:max(0, wide - 34)]
            line = f"{'>' if chosen else ' '} {spec['name']:<17}{about:<{max(0, wide - 34)}} {won:>13}"
            out.append(s._line(2 + row, 1, line[:wide], "bright" if chosen else
                               "accent" if i and self.medals[i - 1] == 3 else "text"))
        if self.trial_note:
            out.append(s._line(s.height - 3, 1, self.trial_note[:wide], "bright"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, [("W/S", "Choose"), ("Enter", "Fly it"), ("?", "The rules"), ("B", "Back")])
        return out

    # -- the Gyre and the hall ---------------------------------------------------

    def _in_the_eye(self, done: dict) -> None:
        """This pilot's first time in the eye. Called inside the write of the turn that got there."""
        first = done["first"]
        self.eye = True
        self.hall["eye"] = done["by"]
        self.postmarks += EYE_PAY[first]
        self.points += EYE_POINTS[first]
        if first:
            self._say(f"No pilot here had been in the eye of the Gyre. Your name is in the hall for good: "
                      f"{EYE_PAY[1]} postmarks.")
        else:
            self._say(f"You have been in the eye of the Gyre: {EYE_PAY[0]} postmarks. "
                      f"{clean_text(str(done['by'][1]), 16)} was here first.")

    def _see_hall(self) -> bool:
        if self.store is None and self._own is None:
            return False
        try:
            hall = self._read(read_hall)
        except sqlite3.Error:
            return False
        changed, self.hall = hall != self.hall, hall
        return changed

    # -- seasons -----------------------------------------------------------------

    def _see_season(self) -> bool:
        """Take in how the season stands. Settle any season that is over, and take what one owes this pilot.

        True if anything is new. The season's goal is set by the first
        session to look. A season is settled by the first to look once it
        is three days past: whoever had a hand in its goal is written
        down then, so what it owes nobody depends on when they ask.
        """
        if (self.store is None and self._own is None) or self.drift is not None:
            return False                  # no records, and nothing of this visit's own yet: no season to keep
        season, today = season_of(self.today)[0], self.today
        seed, winds, me, now = self.world.seed, len(self.winds), self.launch.user_id, self.now()
        before = self.season_now
        try:
            if self._season_done != (season, today) and now >= self._season_wait:
                # Once for each day that opens: the goal of the season it is in, and every season now over.
                self._season_wait = now + SEASON_RETRY_SECONDS      # if this cannot be written, not again at once
                self._shared(lambda db: settle_seasons(db, seed, today, winds), write=True)
                self._season_dues = self._read(lambda db: unclaimed_seasons(db, me))
                self._season_done, self._season_wait = (season, today), 0.0        # only once both are done
            self.season_now = self._read(lambda db: read_season(db, season))        # the one that still changes
            shown = []
            for n in range(season - 1, max(0, season - 4), -1):
                past = self._seasons_read.get(n)
                if past is None:
                    past = self._read(lambda db, n=n: read_season(db, n))
                    if past["closed"]:
                        self._seasons_read[n] = past            # a closed season is read once: it changes no more
                shown.append(past)                              # one over but not yet settled is shown as such
            self.season_past = shown
        except LeaseLost:
            self.ended = "taken"
            return False
        except sqlite3.Error:
            return False
        for n in list(self._season_dues):
            self._claim(n)
        return self.season_now != before

    def _claim(self, season: int) -> None:
        """Take what a closed season owes this pilot, once: said, and paid, in one write."""
        if season in self._claimed or self.ended or self.now() < self._claim_wait:
            return
        me = self.launch.user_id

        def apply(done):
            if done is None:
                return
            if done["goal"]:
                self.postmarks += GOAL_PAY
                self._say(f"Season {season} is over and its goal was met. You had a hand in it: {GOAL_PAY} postmarks.")
            if done["place"] is not None:
                pay = RACE_PAY[done["place"]]
                self.postmarks += pay
                self._say(f"You took {RACE_PLACES[done['place']]} in the race of season {season}: {pay} postmarks.")
        if self._deed("season", lambda db: claim_season(db, season, me), apply):
            self._claimed.add(season)
            self._season_dues = [n for n in self._season_dues if n != season]
        else:
            self._claim_wait = self.now() + SEASON_RETRY_SECONDS       # it cannot be written: not again at every look

    def _race(self, day: int) -> None:
        """A flight that has just ended in a season's last week: it stands if it is the pilot's longest of the race."""
        if not in_race(day) or self.lift_off is None or self.ended:
            return
        km = math.sqrt((self.state.x - self.lift_off[0]) ** 2 + (self.state.y - self.lift_off[1]) ** 2)
        launch = self.launch
        try:
            took = self._shared(lambda db: race_fly(db, launch.user_id, launch.handle, km, day), write=True) \
                if km >= 1.0 else 0
            if took > 0:
                self._say(f"{km:.0f} km in a straight line: your longest in this season's race.")
            elif took < 0:
                self._say(f"The race of season {season_of(day)[0]} is over and settled: this flight came too late "
                          f"for it.")
        except LeaseLost:
            self.ended = "taken"
        except sqlite3.Error as error:
            diagnose(f"a race flight could not be kept: {error}")

    def season_weather(self, season: int) -> str:
        """The season's turn of the weather, in a line."""
        layers, pulse, cold = self.weather._season(season - 1)
        low = "easterly trades down low" if layers[0][2] < 0 else "westerlies from the ground up"
        gyre = "the Gyre strong" if pulse > 1.05 else "the Gyre slack" if pulse < 0.95 else "the Gyre as ever"
        return f"{low[0].upper()}{low[1:]}, {gyre}" + (", and ice from layer 4" if cold else "") + "."

    def _draw_season(self, s: "Session") -> list[str]:
        """The season: its weather, the node's goal and how far it has come, the race, and the seasons before."""
        wide, now = s.width - 2, self.season_now
        season, day = season_of(self.today)
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, f"Season {season}", "text")]
        out.append(s._kv_right(0, s.width - 1, f"day `{day}` of `{SEASON_DAYS}`"))
        out.append(s._line(2, 1, self.season_weather(season)[:wide], "text"))
        out.append(s._line(4, 1, "THE NODE'S GOAL", "accent"))
        row = 5
        if now.get("kind"):
            spec = SEASON_GOALS[now["kind"]]
            done = min(now["total"], now["target"])
            out.append(s._line(row, 1, (spec["text"].format(n=now["target"]) + " before the season is out.")[:wide], "text"))
            width = min(30, wide - 24)
            filled = round(done / max(1, now["target"]) * width)
            for i in range(width):
                s.frame.put(1 + i, row + 1, "\u2588" if i < filled else "\u00b7", (120, 214, 160) if i < filled else "dim")
            out.append(s._kv(row + 1, width + 3, f"`{now['total']}` of `{now['target']}`"
                                                  + ("   met" if now["total"] >= now["target"] else ""), wide - width - 2))
            hands = "   ".join(f"{clean_text(str(h[1]), 16).replace('`', ' ')} `{h[2]}`" for h in now["hands"][:4])
            out.append(s._kv(row + 2, 1, hands or "Nobody has put a hand to it yet.", wide))
            out.append(s._line(row + 3, 1, f"Met, it pays everyone who had a hand in it {GOAL_PAY} postmarks at the "
                                           f"season's end."[:wide], "dim"))
        else:
            out.append(s._line(row, 1, "There is no goal on record: the season's is set when the records can be kept."
                               [:wide], "dim"))
        row += 5
        out.append(s._line(row, 1, "THE RACE", "accent"))
        opens = SEASON_DAYS - RACE_DAYS + 1
        if day < opens:
            out.append(s._line(row + 1, 1, f"From day {opens}: the longest straight line from launch to landing in "
                                           f"one flight."[:wide], "text"))
        else:
            out.append(s._line(row + 1, 1, f"On now, until day {SEASON_DAYS}: the longest straight line from launch "
                                           f"to landing."[:wide], "text"))
        standing = "   ".join(f"{i + 1} {clean_text(str(name), 16).replace('`', ' ')} `{km:.0f}` km"
                              for i, (name, km) in enumerate(now.get("race", [])[:4]))
        out.append(s._kv(row + 2, 1, standing or ("Nobody has flown in it yet." if day >= opens else
                                                  f"Gold, silver and bronze: {RACE_PAY[0]}, {RACE_PAY[1]} and "
                                                  f"{RACE_PAY[2]} postmarks."), wide))
        row += 4
        out.append(s._line(row, 1, "SEASONS PAST", "accent"))
        shown = 0
        for past in self.season_past:
            if row + 1 + shown * 2 >= s.height - 3:
                break
            if past["kind"] is None and not past["medals"]:
                continue
            spec = SEASON_GOALS.get(past["kind"])
            goal = "no goal" if spec is None else spec["text"].format(n=past["target"]).lower()
            state = "being settled" if not past["closed"] else "met" if past["met"] else "not met"
            out.append(s._line(row + 1 + shown * 2, 1, f"Season {past['season']}: {goal}: {state}."[:wide], "text"))
            medals = "   ".join(f"{RACE_PLACES[place]} {clean_text(str(name), 16).replace('`', ' ')} `{km:.0f}` km"
                                for place, name, km in past["medals"])
            out.append(s._kv(row + 2 + shown * 2, 3, medals or "Nobody flew its race.", wide - 2))
            shown += 1
        if not shown:
            out.append(s._line(row + 1, 1, "None yet. Nothing a pilot owns is taken back when a season ends, nor "
                                           "is the map."[:wide], "dim"))
        first = self.hall.get("eye")
        out.append(s._line(s.height - 3, 1, "THE HALL", "accent"))
        out.append(s._line(s.height - 3, 10, (f"The eye of the Gyre: {clean_text(str(first[1]), 16)} was first in, "
                                              f"on Day {int(first[2])}." if first else
                                              "The eye of the Gyre: no pilot has been in it.")[:wide - 9],
                           "text" if first else "dim"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, [("Enter", "Back")])
        return out

    # -- wonders -----------------------------------------------------------------

    def _see_wonders(self) -> bool:
        """Take in the wonders seen on the node and this pilot's own album. True if anything is new."""
        if self.store is None and self._own is None:
            return False
        try:
            first, mine = self._read(lambda db: read_wonders(db, self.launch.user_id))
        except sqlite3.Error:
            return False
        changed = first != self.sighted or mine != self.album
        self.sighted, self.album = first, mine
        return changed

    def wonder_in_view(self, wonder: dict, state: FlightState | None = None) -> bool:
        """Is the balloon where this wonder is seen from: aloft, near enough, in a layer it shows from, at its hour?"""
        state = state or self.state
        if state.status != "aloft" or self.drift is not None:
            return False
        spec = WONDER_KINDS[wonder["kind"]]
        reach = spec["km"] * self.fitted("camera")["range"]
        if (wonder["x"] - state.x) ** 2 + (wonder["y"] - state.y) ** 2 > reach * reach:
            return False
        low, high = spec["layers"]
        if wonder["kind"] in ("falls", "sealights"):
            low = high = self.engine.lowest_open(state.x, state.y)       # "the lowest layer" is the lowest there is
        if not low <= state.layer <= high:
            return False
        if not wonder_shows(self.weather, wonder, state.day, state.minute):
            return False
        if wonder["kind"] == "cloudsea":       # from above the deck, not under it or in it
            return state.layer > self.weather.frame(state.day, state.minute).cloud(wonder["x"], wonder["y"])[1]
        return True

    def wonder_here(self) -> dict | None:
        """A wonder the pilot could take for the album now: in view, and not in their album yet."""
        state = self.state
        key = (state.day, state.turn, state.x, state.y, state.layer, state.status, len(self.album),
               self.gear["camera"], self.drift is None)
        if self._wonder_at is None or self._wonder_at[0] != key:       # worked out once for a turn, not for every paint
            found = next((w for w in self.wonders if w["id"] not in self.album and self.wonder_in_view(w)), None)
            self._wonder_at = (key, found)
        return self._wonder_at[1]

    _wonder_at = None

    def _point_out(self) -> None:
        """Say so when a wonder comes into view. Once for each time it does."""
        wonder = self.wonder_here()
        mark = None if wonder is None else wonder["id"]
        if mark is not None and mark != self._wonder_said:
            self._say("There is something to see from here that is not in your album: F.")
        self._wonder_said = mark

    def _capture(self) -> None:
        """Take the wonder in view for the album. It takes no turn."""
        wonder = self.wonder_here()
        if wonder is None or (self.store is not None and not self._mine(write=False)):
            return
        launch, day = self.launch, self.state.day
        self.heard = []

        def earn(done):
            if done is None or not done["new"]:
                return
            first = done["first"]
            self.album.add(wonder["id"])
            if done["by"] is not None:        # who was first, from the records: this pilot or one who got there since
                self.sighted[wonder["id"]] = done["by"]
            self.postmarks += WONDER_PAY[first]
            self.points += WONDER_POINTS[first]
            self._say(f"{wonder['name']}. " + (f"Nobody here had seen it: {WONDER_PAY[1]} postmarks, and your name "
                                               f"beside it." if first else f"In your album: {WONDER_PAY[0]} postmarks."))
        self.save("wonder", (), lambda db: see_wonder(db, wonder["id"], launch.user_id, launch.handle, day), earn)
        if self.ended:
            return
        if wonder["id"] in self.album and self.saved_ok:
            self.pending_scene = self._wonder_scene(wonder)
        self._promote()
        self._wonder_said = None

    def _wonder_scene(self, wonder: dict) -> dict:
        """What the wonder looked like: its kind, the hour, the land under it, and its name."""
        state = self.state
        frame = self.weather.frame(state.day, state.minute)
        who = self.sighted.get(wonder["id"])
        sub = f"First seen by {clean_text(str(who[1]), 16)} on Day {int(who[2])}." if who else ""
        return {"kind": "wonder", "family": wonder["kind"], "hour": state.minute / 60.0,
                "cloud": frame.cloud(state.x, state.y)[0], "biome": self.world.sample(wonder["x"], wonder["y"])[1],
                "place": False, "seed": wonder["id"] * 7919 + 31, "caption": wonder["name"], "sub": sub,
                "design": dict(self.design)}

    def _hint_level(self, wonder: dict) -> int:
        """How much is told of a wonder nobody has seen: 0 its kind, 1 also where, 2 also how."""
        share = self.charted_share()
        if cell_at(wonder["x"], wonder["y"]) in self.chart or share >= WONDER_HINTS[1]:
            return 2
        return 1 if share >= WONDER_HINTS[0] else 0

    def wonder_hint(self, wonder: dict) -> str:
        """What the album says of a wonder nobody on the node has seen: more as the chart grows."""
        spec = WONDER_KINDS[wonder["kind"]]
        share = self.charted_share()
        known = cell_at(wonder["x"], wonder["y"]) in self.chart
        text = spec["hint"][0].upper() + spec["hint"][1:]
        if wonder["kind"] == "eye":
            return text + "."
        if share >= WONDER_HINTS[0] or known:
            text += f", {self._near(wonder['x'], wonder['y'])}"
        if share >= WONDER_HINTS[1] or known:
            text += f": {spec['how']}"
        return text + "."

    def _album_pages(self) -> list[list]:
        """The album's pages: the kinds of wonder, three to a page."""
        kinds = list(WONDER_KINDS)
        return [kinds[i:i + 3] for i in range(0, len(kinds), 3)]

    def _draw_album(self, s: "Session") -> list[str]:
        """The album: the forty wonders by kind, who saw each first, and a hint for each nobody has."""
        wide = s.width - 2
        pages = self._album_pages()
        self.album_page = max(0, min(self.album_page, len(pages) - 1))
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, "The album", "text")]
        out.append(s._kv_right(0, s.width - 1, f"You have seen `{len(self.album)}` of `{len(self.wonders)}`; the node "
                                               f"`{len(self.sighted)}`"))
        row = 2
        for kind in pages[self.album_page]:
            spec = WONDER_KINDS[kind]
            mine = [w for w in self.wonders if w["kind"] == kind]
            # How a kind is seen heads its wonders, once somebody has seen one or the chart has grown enough.
            told = any(w["id"] in self.sighted or w["id"] in self.album or self._hint_level(w) >= 2 for w in mine)
            out.append(s._line(row, 1, spec["title"].upper(), "accent"))
            out.append(s._line(row, 3 + len(spec["title"]), (spec["how"] if told else "nobody knows yet how")
                               [:max(0, wide - 3 - len(spec["title"]))], "cool" if told else "dim"))
            row += 1
            for wonder in mine:
                if row >= s.height - 3:
                    break
                who = self.sighted.get(wonder["id"])
                name = clean_text(str(who[1]), 16) if who else ""
                where = self._near(wonder["x"], wonder["y"])
                if wonder["id"] in self.album:
                    text, ink = f"* {wonder['name']}, {where}" + (f". First: {name}, Day {int(who[2])}" if who else ""), "text"
                elif who:
                    text, ink = f"- {wonder['name']}, {where}. Seen by {name}", "text"
                else:
                    hint = spec["hint"][0].upper() + spec["hint"][1:]
                    text = f"  {hint}" + (f", {where}" if self._hint_level(wonder) >= 1 and kind != "eye" else "")
                    ink = "dim"
                out.append(s._line(row, 1, text[:wide], ink))
                row += 1
        out.append(s._line(s.height - 3, 1, "* yours   - another pilot's   the rest: a hint, sharper as the chart "
                                            "grows"[:wide], "dim"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, [("W/S", f"Page {self.album_page + 1} of {len(pages)}"), ("Enter", "Back")])
        return out

    # -- the almanac ---------------------------------------------------------

    def _see_almanac(self) -> bool:
        """Take in the winds entered, and how often this pilot has flown each. True if anything is new."""
        if self.store is None and self._own is None:
            return False
        try:
            entered, flown = self._read(lambda db: read_almanac(db, self.launch.user_id))
        except sqlite3.Error:
            return False
        changed = entered != self.almanac
        self.almanac, self.flown = entered, flown
        if changed:
            self._blowing_at = None
        return changed

    def wind_here(self, state: FlightState, day: int, minute: int) -> dict | None:
        """The local wind the balloon is flying in, if one is blowing here and it is low enough to feel it."""
        if state.status != "aloft" or state.layer > WIND_LAYERS:
            return None
        cell = cell_at(state.x, state.y)
        for wind in self.winds:
            if cell in wind["cells"] and wind_blows(self.weather, wind, day, minute):
                return wind
        return None

    _almanac_stale = False

    def _wind_flown(self, wind: dict, done: dict | None, day: int) -> None:
        """What flying a local wind came to. Called inside the write of the turn that flew it."""
        if done is None:
            return
        kind = WIND_KINDS[wind["kind"]]
        if done["late"]:
            # Somebody entered it since this session last looked. Said once: the entry is taken in after the turn.
            self._almanac_stale = True
            self._say(f"The {kind['what']} here is in the almanac already: another pilot entered it. V.")
            return
        if not (done["new"] or done["entered"]):
            return
        self.flown[wind["id"]] = done["days"]
        if done["entered"]:
            self.almanac[wind["id"]] = (self.launch.user_id, self.launch.handle, day)
            self.postmarks += ALMANAC_PAY
            self.points += ALMANAC_POINTS
            self._blowing_at = None
            self._say(f"{wind['name']} enters the almanac in your name: {ALMANAC_PAY} postmarks. "
                      f"Every pilot's map shows it now when it blows.")
        else:
            self._say(f"A {kind['what']} blows here that no almanac has. Flown on {done['days']} of {WIND_DAYS} "
                      f"days: V.")

    def blowing(self) -> list[dict]:
        """The entered winds that blow at the pilot's hour: what every map shows."""
        key = (self.state.day, self.state.minute)
        if self._blowing_at is None or self._blowing_at[0] != key:
            self._blowing_at = (key, [wind for wind in self.winds if wind["id"] in self.almanac
                                      and wind_blows(self.weather, wind, *key)])
        return self._blowing_at[1]

    def _near(self, x: float, y: float) -> str:
        """Where a spot is, in words: by the nearest town."""
        town = min(self.world.towns, key=lambda t: (t["x"] - x) ** 2 + (t["y"] - y) ** 2)
        dx, dy = x - town["x"], y - town["y"]
        km = math.sqrt(dx * dx + dy * dy)
        return f"at {town['name']}" if km < 15 else f"{km:.0f} km {bearing(dx, dy)} of {town['name']}"

    def _draw_almanac(self, s: "Session") -> list[str]:
        """The almanac: the local winds entered, by whom, and the ones this pilot is part way to entering."""
        wide, state = s.width - 2, self.state
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, "The almanac", "text")]
        out.append(s._kv_right(0, s.width - 1, f"`{len(self.almanac)}` of `{len(self.winds)}` local winds entered"))
        lines = []
        for wind in self.winds:
            kind = WIND_KINDS[wind["kind"]]
            if wind["id"] in self.almanac:
                _, handle, day = self.almanac[wind["id"]]
                now = "  blowing now" if wind_blows(self.weather, wind, state.day, state.minute) else ""
                lines.append((f"{wind['name']}, a {kind['what']} {self._near(wind['x'], wind['y'])}", "text"))
                lines.append((f"    {kind['when']}. Entered by {clean_text(str(handle), 16) or 'a pilot'} "
                              f"on Day {int(day)}.{now}", "cool" if now else "dim"))
        for wind in self.winds:
            if wind["id"] not in self.almanac and self.flown.get(wind["id"]):
                kind = WIND_KINDS[wind["kind"]]
                lines.append((f"An unnamed {kind['what']} {self._near(wind['x'], wind['y'])}: flown "
                              f"{self.flown[wind['id']]} of {WIND_DAYS} days", "accent"))
                lines.append((f"    {kind['when']}.", "dim"))
        room = s.height - 8
        if not lines:
            lines = [("Nothing is entered yet.", "dim")]
        shown = lines if len(lines) <= room else lines[:room - 1 - (room - 1) % 2] + [
            (f"and {(len(lines) - (room - 1 - (room - 1) % 2)) // 2} more", "dim")]
        for i, (line, ink) in enumerate(shown):
            out.append(s._line(2 + i, 1, line[:wide], ink))
        left = len(self.winds) - len(self.almanac)
        if left:
            out.append(s._line(s.height - 5, 1, f"{left} more blow where nobody has flown them on {WIND_DAYS} days: "
                                                f"sea breezes on coasts"[:wide], "dim"))
            out.append(s._line(s.height - 4, 1, "in the afternoon, air off steep ground at dawn, a rush through gaps "
                                                "in hills."[:wide], "dim"))
            out.append(s._line(s.height - 3, 1, f"Fly one in layer {WIND_LAYERS} or below while it blows, on "
                                                f"{WIND_DAYS} days: it is yours to enter."[:wide], "dim"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, [("Enter", "Back")])
        return out

    # -- the chart -----------------------------------------------------------

    def is_charted(self, wx: float, wy: float) -> bool:
        cell = cell_at(wx, wy)
        return cell is None or cell in self.chart or cell in self.known

    def seen_cells(self, x: float, y: float, layer: int, day: int, minute: int) -> list[int]:
        """The cells seen from the basket: within the layer's sight, and not under a cloud deck below the balloon."""
        if layer < 1:
            return []
        frame = self.weather.frame(day, minute)
        out = []
        for cell in cells_within(x, y, SIGHT_KM[min(layer, len(SIGHT_KM)) - 1]):
            cx, cy = (cell % CHART_W + 0.5) * CHART_KM, (cell // CHART_W + 0.5) * CHART_KM
            cover, deck = frame.cloud(cx, cy)
            if cover >= CLOUD_BLOCKS and deck < layer:
                continue
            out.append(cell)
        return out

    def _watch(self, cells: list) -> dict:
        """The features these cells belong to that nobody has found, with what finding each takes."""
        watch = {}
        for cell in cells:
            for fid in self.world._features_at.get(cell, ()):
                if fid in watch or fid in self.found:
                    continue
                feature = self.world.features[fid]
                group = [c for c in feature["cells"] if c not in self.known]
                given = len(feature["cells"]) - len(group)
                # At least one cell has to be charted by somebody: what was known from the start finds nothing.
                need = max(1, int(math.ceil(len(feature["cells"]) * DISCOVERY_SHARE)) - given)
                watch[fid] = (group, need, feature_names(self.world, feature)[0])
        return watch

    def _charted(self, new: list, found: list) -> dict:
        """What a turn's charting came to: the cells that were new, and any feature found. Returns the career."""
        land = sum(1 for cell in new if cell in self.world._land)
        before = self.charted
        self.charted += land
        pay = self.charted // CHART_PER_POSTMARK - before // CHART_PER_POSTMARK
        self.postmarks += pay
        self.points += self.charted // CHART_PER_POINT - before // CHART_PER_POINT
        if new:
            self.chart.update(new)
            self.chart_version += 1
        # Said once, when the flight is down: a line a turn would crowd the log out.
        self.flight_cells += land
        self.flight_pay += pay
        for fid in found:
            feature = self.world.features[fid]
            names = feature_names(self.world, feature)
            self.found[fid] = (names[0], self.launch.user_id, self.launch.handle, self.state.day)
            self.postmarks += DISCOVERY_PAY
            self.points += DISCOVERY_POINTS
            self.naming.append({"id": fid, "kind": feature["kind"], "names": names})
            article = "an" if feature["kind"][0] in "aeiou" else "a"
            self._say(f"You have found {article} {feature['kind']} nobody had named: {DISCOVERY_PAY} postmarks.")
        return self._career()

    def _say_charted(self) -> None:
        """What the flight just ended put on the chart."""
        if self.flight_cells:
            cells, pay = self.flight_cells, self.flight_pay
            self._say(f"Charted on the way: {cells} cell{'s' if cells != 1 else ''} of land nobody had seen, "
                      f"{pay} postmark{'s' if pay != 1 else ''}.")
        self.flight_cells = self.flight_pay = 0

    def _name_it(self, choice: int) -> None:
        """Give the feature just found one of its three names. Any key that is not a choice takes the first."""
        offer = self.naming.pop(0)
        name = offer["names"][choice if 0 <= choice < len(offer["names"]) else 0]
        if self.store is not None:
            try:
                if not self.store.name_feature(offer["id"], self.launch.user_id, name):
                    name = offer["names"][0]      # not this pilot's to name after all: the name it has stands
            except sqlite3.Error as error:
                diagnose(f"a name could not be kept: {error}")
                name = offer["names"][0]
        old = self.found.get(offer["id"], (name, self.launch.user_id, self.launch.handle, self.state.day))
        self.found[offer["id"]] = (name,) + tuple(old[1:])
        self._say(f"It is {name} on every chart now.")

    def charted_share(self) -> float:
        """How much of the land is on the chart, from 0 to 1."""
        if self._share is None or self._share[0] != (self.chart_version, len(self.chart)):
            land = self.world._land
            share = sum(1 for cell in land if cell in self.chart or cell in self.known) / max(1, len(land))
            self._share = ((self.chart_version, len(self.chart)), share)      # counted once for each state of the chart
        return self._share[1]

    def _see_chart(self) -> bool:
        """Take in what others have charted and named since the last look. True if anything is new."""
        if self.store is None:
            return False
        try:
            size = self.store.chart_size()
            if size == self._chart_size:
                return False
            cells, found = self.store.chart()
        except sqlite3.Error:
            return False
        self._chart_size = size
        new_ground, new_names = not cells <= self.chart, found != self.found
        if new_ground:
            self.chart = cells | self.chart
            self.chart_version += 1           # the ground is drawn again; names are labels, laid on at every paint
        self.found = dict(found)
        return new_ground or new_names

    def pulse(self, now: float) -> bool:
        """Called as time passes: watch the calendar and keep the lease alive. True if the screen has to change."""
        if self.busy is not None or self.ended:
            return False
        changed = False
        if self._last_look is None or now - self._last_look >= CALENDAR_CHECK_SECONDS:
            self._last_look = now
            changed = self._day_may_have_opened()
        if self.store is None:
            return changed
        if self._last_beat is not None and now - self._last_beat < HEARTBEAT_SECONDS:
            return changed
        self._last_beat = now
        return not self._mine() or changed

    def _day_may_have_opened(self) -> bool:
        """Look at the calendar. If a day has opened, say so where it matters. True if anything changed."""
        if self.drift is not None:
            return False                  # not while a Drift is flown: the pilot's own flight is set aside
        before = self.others
        self._see_others()                # the same glance takes in where everyone else has got to
        charted = self._see_chart()       # and what they have charted
        charted = self._see_stations() or charted     # and planted, stocked and raised
        charted = self._see_calls() or charted        # and who is stranded; or that help has come
        charted = self._see_almanac() or charted      # and what winds have been entered
        charted = self._see_wonders() or charted      # and what wonders have been seen
        charted = self._see_season() or charted       # and how the season stands
        charted = self._see_hall() or charted         # and who is in the hall
        if not self._look_at_the_calendar():
            return self.others != before or charted
        self._see_season()                # a day has opened: it may be a new season's, or settle an old one
        if self._catch_up():
            self.save()
        self._choose_default()
        return True

    def _mine(self, write: bool = True) -> bool:
        """Is the pilot still this session's? If not, this session is over.

        The heartbeat writes. The look before a turn only reads: the commit
        that follows checks again inside its transaction and is the heartbeat.
        """
        try:
            if (self.store.beat if write else self.store.holds)(self.launch.user_id):
                return True
        except sqlite3.Error:
            return True                   # the records cannot be reached; the commit will say so
        self.ended = "taken"
        return False

    def take_over(self) -> None:
        """The caller chose to fly here: the other session ends at its next turn."""
        try:
            self.store.claim(self.launch.user_id, self._transport(), force=True)
        except sqlite3.Error as error:
            diagnose(f"records cannot be kept: {error}")
            self._drop_store()
        self.busy = None
        self._begin()

    def close(self) -> None:
        """The session is over: give the pilot up, so the next call finds nobody flying."""
        if self._own is not None:
            self._own.close()
            self._own = None
        if self.store is not None:
            try:
                if self.busy is None and not self.ended:
                    self.store.release(self.launch.user_id)
            except sqlite3.Error:
                pass
            self.store.close()
            self.store = None

    # -- stations and masts ----------------------------------------------------

    def _own_db(self):
        """With no records to share, the stations of this visit are its own: kept in memory, in the same tables."""
        if self._own is None:
            self._own = sqlite3.connect(":memory:", isolation_level=None)
            for step in range(1, SCHEMA_VERSION + 1):     # the same tables as the records, so the same queries run
                for statement in MIGRATIONS[step]:
                    self._own.execute(statement)
            for station in self.stations[len(self.world.stations):]:      # what was known when the records went
                self._own.execute("INSERT INTO stations VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                                  tuple(station[k] for k in ("id", "name", "x", "y", "handle", "day", "stock",
                                                             "stock_day", "freight", "mast_day")))
        return self._own

    def _see_stations(self) -> bool:
        """Take in what has been planted, stocked and raised. True if anything is new."""
        if self.store is None and self._own is None and len(self.stations) == len(self.world.stations):
            return False                  # no records, and nothing planted on this visit: there is nothing to read
        try:
            rows = self.store.stations() if self.store is not None else read_stations(self._own_db())
        except sqlite3.Error:
            return False
        base = len(self.world.stations)
        rows = [row for i, row in enumerate(rows) if row["id"] == base + i]
        if rows == self.stations[base:] or len(rows) < len(self.stations) - base:
            return False                  # nothing new; and a place cargo may be bound for is never forgotten
        self.stations = list(self.world.stations) + rows
        self.engine.set_stations(self.stations)
        return True

    def hut(self) -> dict | None:
        """The planted station the balloon is at, if it is at one that is not yet a mast."""
        state = self.state
        for station in self.stations:
            if not staffed(station) and (station["x"] - state.x) ** 2 + (station["y"] - state.y) ** 2 \
                    <= PLACE_RADIUS_KM * PLACE_RADIUS_KM:
                return station
        return None

    def _deed(self, action: str, deed, apply) -> bool:
        """Do something to the shared world and to the pilot as one write.

        deed(db) changes the world and says what came of it; apply(that)
        changes the pilot. If it cannot be written, neither has happened.
        """
        if self.ended or self.drift is not None:
            return False
        if self.store is None:
            apply(deed(self._own_db()))
            return True
        kept = (self.postmarks, self.points, self.kits, self.crates, list(self.log), list(self.heard), self.state,
                list(self.manifest), list(self.taken), self.deliveries, self.days_flown, self.total_km,
                list(self.route))

        def career(new, found, done):
            apply(done)
            return self._career(), self.state.to_dict()
        try:
            self.store.commit(self.launch.user_id, self.launch.handle, career, self.state.to_dict(), action, deed=deed)
            self.saved_ok = True
            return True
        except (LeaseLost, sqlite3.Error) as error:
            (self.postmarks, self.points, self.kits, self.crates, self.log, self.heard, self.state,
             self.manifest, self.taken, self.deliveries, self.days_flown, self.total_km, self.route) = kept
            if isinstance(error, LeaseLost):
                self.ended = "taken"
            else:
                diagnose(f"a deed could not be saved: {error}")
                self.saved_ok = False
            return False

    def cannot_plant(self) -> str:
        """Why a station cannot be planted here and now, or "" if it can."""
        state = self.state
        if not self.kits:
            return "There is no station kit aboard. The towns sell them."
        if state.status not in ("ground", "stranded"):
            return "A station is planted from the ground."
        if self.engine.gyre_km(state.x, state.y) <= GYRE_INNER_KM:
            return (f"Nothing stands this near the Gyre: a station is planted more than {GYRE_INNER_KM:.0f} km "
                    f"from its middle.")
        for place in self.world.towns + self.stations:
            if (place["x"] - state.x) ** 2 + (place["y"] - state.y) ** 2 < STATION_APART_KM ** 2:
                return (f"Too close to {place['name']}: a station stands at least "
                        f"{STATION_APART_KM:.0f} km from any other place.")
        return ""

    def _plant(self) -> None:
        """Plant the station kit where the balloon stands."""
        self.heard = []
        why = self.cannot_plant()
        if why:
            self._say(why)
            return
        if self.store is not None and not self._mine(write=False):
            return
        state, launch = self.state, self.launch

        def apply(done):
            if done is None:
                self._say("Somebody has just planted a station near here. The kit stays aboard.")
                return
            self.kits -= 1
            self.points += PLANT_POINTS
            self._say(f"You plant {done['name']}. The forecast is sharper around it for everyone, while it is kept "
                      f"stocked. {PLANT_POINTS} points on the licence.")
        self._deed("plant", lambda db: plant_station(db, self.world, state.x, state.y, launch.user_id,
                                                     launch.handle, self.today), apply)
        self._see_stations()
        self._promote()
        self._choose_default()

    def _visit(self) -> None:
        """Down at a planted station: see to it, and unload what crates it wants."""
        hut, launch, crates = self.hut(), self.launch, self.crates
        if hut is None or self.state.status not in ("ground", "stranded"):
            return

        def apply(done):
            if done is None:              # raised while this balloon was on its way
                kept = " Your crates stay aboard for another station." if crates else ""
                self._say(f"{hut['name']} is a mast now: its post office is open, O.{kept}")
                return
            name, restored, took = hut["name"], done["restored"], done["took"]
            pay = restored * RESUPPLY_PAY + took * CRATE_PAY
            self.postmarks += pay
            self.points += int(restored * RESUPPLY_POINTS) + took * CRATE_POINTS
            self.crates -= took
            if restored:
                was = "was silent and reports again" if done["silent"] else "is seen to"
                self._say(f"{name} {was}: {restored} day{'s' if restored != 1 else ''} of stock put back, "
                          f"{restored * RESUPPLY_PAY} postmarks.")
            if took and done["raised"]:
                self._say(f"Your freight raises the mast at {name}: {took * CRATE_PAY} postmarks. It has a lit "
                          f"field, fuel and a post office from now on.")
            elif took:
                wants = done["wants"]
                self._say(f"Unloaded for the mast at {name}: {took} crate{'s' if took != 1 else ''}, "
                          f"{took * CRATE_PAY} postmarks. {wants} more will raise it.")
            elif not restored:
                self._say(f"{name} is well stocked. {done['wants']} crates from a town would raise a mast here.")
        self._deed("station", lambda db: visit_station(db, hut["id"], launch.user_id, launch.handle,
                                                       self.today, crates), apply)
        self._see_stations()
        self._promote()

    # -- stranded and rescued ----------------------------------------------------

    def _read(self, query):
        return self.store.read(query) if self.store is not None else query(self._own_db())

    def _see_calls(self) -> bool:
        """Who is stranded and waiting; and whether somebody has reached this pilot. True if anything is new."""
        if self.store is None and self._own is None:
            return False                  # no records, and nothing of this visit's own yet
        try:
            rows = self._read(read_calls)
        except sqlite3.Error:
            return False
        me = self.launch.user_id
        mine = next((row for row in rows if row["user_id"] == me), None)
        calls = [row for row in rows if row["user_id"] != me and row["by_user"] is None]
        changed = calls != self.calls
        self.calls, self._called = calls, mine is not None
        if mine is not None and mine["by_user"] is not None and self.busy is None and not self.ended:
            self._rescued()
            return True
        return changed

    def _sync_call(self) -> None:
        """Keep this pilot's call for help as they stand: out while stranded, taken down when not."""
        stranded = self.phase() == "stranded"
        if stranded == self._called or self.ended:
            return
        state, launch = self.state, self.launch
        if stranded:
            fuel, repair = max(0, RESCUE_FUEL - state.fuel), int(state.grounded)
            if self._deed("call", lambda db: call_for_help(db, launch.user_id, launch.handle, state.x, state.y,
                                                           self.today, fuel, repair), lambda done: None):
                self._called = True
                self._say(f"Your call for {need_text(fuel, repair)} is on every pilot's map.")
        elif self._deed("call", lambda db: end_call(db, launch.user_id), lambda done: None):
            self._called = False

    def _take_help(self, done: dict | None) -> bool:
        """Take what a rescuer brought, if one came. Called inside the write that takes the call down."""
        if done is None or done["by_user"] is None:
            return False
        state = self.state
        self.state = dataclasses.replace(
            state, fuel=min(self.tank(), state.fuel + done["fuel"]),
            wear=min(state.wear, RELIEF["wear"]) if done["repair"] else state.wear,
            status="ground" if state.status == "stranded" else state.status)
        who = clean_text(done["by_handle"] or "", 16) or "A pilot"
        self._say(f"{who} reached you with {need_text(done['fuel'], done['repair'])}. You can fly on.")
        return True

    def _rescued(self) -> None:
        """Somebody reached this pilot while they waited: take what was brought, once."""
        if self._deed("rescued", lambda db: end_call(db, self.launch.user_id), self._take_help):
            self._called = False
            self._choose_default()

    def _rescue(self) -> None:
        """Down beside a stranded pilot: bring them what they are waiting for."""
        if self.state.status != "ground":
            return
        self._see_calls()
        launch = self.launch
        for call in list(self.calls):
            if (call["x"] - self.state.x) ** 2 + (call["y"] - self.state.y) ** 2 > PLACE_RADIUS_KM ** 2:
                continue
            who = clean_text(call["handle"], 16) or "A pilot"
            spare = max(0, self.state.fuel - COST_LAUNCH)     # patches alone can be given on any tank
            if call["fuel"] > spare:
                self._say(f"{who} is stranded here and wants {call['fuel']} fuel: more than you can spare.")
                continue

            def apply(done, who=who):
                if done is None:
                    self._say(f"{who} is no longer waiting for what you can give: somebody was here first, "
                              f"or they want more now.")
                    return
                self.state = dataclasses.replace(self.state, fuel=max(0, self.state.fuel - done["fuel"]))
                self.postmarks += RESCUE_PAY
                self.points += RESCUE_POINTS
                self._say(f"You reach {who} with {need_text(done['fuel'], done['repair'])}: {RESCUE_PAY} postmarks "
                          f"from the Post, and their thanks.")
            self._deed("rescue", lambda db, call=call, spare=spare: answer_call(
                db, call["user_id"], launch.user_id, launch.handle, spare), apply)
        self._see_calls()
        self._promote()

    # -- the visitors' books -------------------------------------------------------

    def book_place(self) -> int | None:
        """The station whose book lies open to the pilot: the one they are on the ground at."""
        if self.book_mode == "off" or self.state.status not in ("ground", "stranded") or self.drift is not None:
            return None
        ref = self.here()
        if ref is not None:
            return ref[1] if ref[0] == "station" else None
        hut = self.hut()
        return hut["id"] if hut is not None else None

    def _book_lines(self, station: int) -> list:
        try:
            return self._read(lambda db: read_book(db, station))
        except sqlite3.Error:
            return []

    def _book_key(self, k: str) -> None:
        if self.book_place() != self.book["station"] or k in ("ENTER", "ESC", "b", " "):
            self.book = None
        elif self.book_mode == "canned":
            if len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(CANNED):
                self._sign(CANNED[int(k) - 1])
        elif k == "w":
            self.writing, self.book["note"] = "", ""

    def _write_key(self, key: str) -> None:
        """A key while a line is being written: every printable key is part of the line."""
        if key == "ESC" or self.book is None:
            self.writing = None
        elif key == "ENTER":
            text, self.writing = book_text(self.writing), None
            if text:
                self._sign(text)
            else:
                self.book["note"] = "Nothing written."
        elif key in ("BACKSPACE", "DELETE"):
            self.writing = self.writing[:-1]
        elif len(key) == 1 and book_text(f"a{key}a") == f"a{key}a" and len(self.writing) < GUESTBOOK_LIMIT:
            self.writing += key           # (a lone space would be trimmed away: it is tried between letters)

    def _sign(self, text: str) -> None:
        station, launch, book = self.book["station"], self.launch, self.book
        if self.book_place() != station or (self.store is not None and not self._mine(write=False)):
            return                        # the pilot is no longer at this station: nothing is signed from afar

        def apply(done):
            book["note"] = "Signed." if done else "You have signed this book today: one line a day."
        if not self._deed("book", lambda db: sign_book(db, station, launch.user_id, launch.handle, self.today, text),
                          apply):
            book["note"] = "The line could not be kept."
        book["lines"] = self._book_lines(station)

    def _draw_book(self, s: "Session") -> list[str]:
        """A station's visitors' book: the latest lines, and a place to add one."""
        wide, book = s.width - 2, self.book
        place = self.stations[book["station"]]
        out = [s._line(0, 1, "FAIR WINDS", "accent"),
               s._line(0, 13, f"The visitors' book at {place['name']}"[:wide - 12], "text")]
        canned = self.book_mode == "canned"
        room = s.height - 6 - (len(CANNED) + 1 if canned else 0)
        if not book["lines"]:
            out.append(s._line(2, 1, "Nobody has signed it yet.", "dim"))
        for i, (day, handle, text) in enumerate(book["lines"][:max(1, room)]):
            # Shown as plain text, and cleaned again here: what is in the records is not trusted to be clean.
            line = f"Day {int(day)}  {book_text(str(handle))[:16] or 'A pilot'}: {book_text(str(text))}"
            out.append(s._line(2 + i, 1, line[:wide], "text"))
        if canned:
            top = s.height - 4 - len(CANNED)
            for i, phrase in enumerate(CANNED):
                out.append(s._line(top + i, 1, f"{i + 1}  {phrase}"[:wide], "cool"))
        if self.writing is not None:
            out.append(s._line(s.height - 4, 1, f"> {self.writing}_"[:wide], "bright"))
            out.append(s._line(s.height - 3, 1, f"{len(self.writing)} of {GUESTBOOK_LIMIT} characters", "dim"))
            keys = [("Enter", "Sign"), ("Esc", "Leave it")]
        else:
            if book["note"]:
                out.append(s._line(s.height - 3, 1, book["note"][:wide], "bright"))
            keys = [(f"1-{len(CANNED)}", "Sign with a line") if canned else ("W", "Write a line"), ("Enter", "Back")]
        out += s._keys(s.height - 1, keys)
        return out

    def kit(self) -> str:
        """Buy a station kit at a town, or with one aboard hand it back."""
        if not self.at_loft():
            return "Station kits are sold in towns."
        if self.kits:
            self.kits, self.postmarks = 0, self.postmarks + KIT_PRICE
            self.save("stores")
            return f"The station kit goes back on the shelf: {KIT_PRICE} postmarks returned."
        if self.postmarks < KIT_PRICE:
            return f"Not enough postmarks: a station kit is {KIT_PRICE}."
        if self.weight() + KIT_WEIGHT > self.lift():
            return f"Too heavy: the kit weighs {KIT_WEIGHT}, the balloon lifts {self.lift()} and carries {self.weight()}."
        self.kits, self.postmarks = 1, self.postmarks - KIT_PRICE
        self.save("stores")
        return (f"Aboard: a station kit. Land in the wild, {STATION_APART_KM:.0f} km from any place, "
                f"and K plants it.")

    def crate(self, count: int) -> str:
        """Buy a crate of mast freight at a town, or hand one back."""
        if not self.at_loft():
            return "Mast freight is sold in towns."
        if count < 0:
            if not self.crates:
                return "No mast freight aboard."
            self.crates, self.postmarks = self.crates - 1, self.postmarks + CRATE_PRICE
            self.save("stores")
            return f"A crate goes back: {CRATE_PRICE} postmarks returned."
        if self.postmarks < CRATE_PRICE:
            return f"Not enough postmarks: a crate of mast freight is {CRATE_PRICE}."
        if self.weight() + CRATE_WEIGHT > self.lift():
            return f"Too heavy: a crate weighs {CRATE_WEIGHT}, the balloon lifts {self.lift()} and carries {self.weight()}."
        self.crates, self.postmarks = self.crates + 1, self.postmarks - CRATE_PRICE
        self.save("stores")
        return (f"Aboard: {self.crates} crate{'s' if self.crates != 1 else ''} of mast freight. Any station "
                f"that is not yet a mast takes it and pays {CRATE_PAY} a crate.")

    def _raised_by(self) -> str:
        """Who raised the mast here, for its post office to say."""
        ref = self.here()
        place = self.place_of(ref) if ref is not None and ref[0] == "station" else {}
        if place.get("mast_day") is None:
            return ""
        names = [clean_text(name, 16).replace("`", " ") for name in place.get("given", [])[:4]]
        if not names:
            return f"A mooring mast, planted as a station by {clean_text(place['handle'], 16)}."
        return (f"A mooring mast since Day {place['mast_day']}, raised with the freight of "
                f"{', '.join(names[:-1]) + (' and ' if len(names) > 1 else '') + names[-1]}.")

    # -- the post ------------------------------------------------------------

    def _ref(self, value) -> tuple[str, int]:
        kind, index = value
        group = self.world.towns if kind == "town" else self.stations
        if kind not in ("town", "station") or not 0 <= int(index) < len(group):
            raise ValueError("no such place")
        return kind, int(index)

    def place_of(self, ref: tuple[str, int]) -> dict:
        return (self.world.towns if ref[0] == "town" else self.stations)[ref[1]]

    @property
    def targets(self) -> list[tuple[str, int]]:
        """Where the pilot is bound: the places there is cargo aboard for, or with none aboard the nearest places."""
        if self.drift is not None:
            return []                     # a Drift is bound for its mark and nowhere else
        seen = []
        for item in self.manifest:
            if tuple(item["dest"]) not in seen:
                seen.append(tuple(item["dest"]))
        return seen or places_near(self.world, self.state.x, self.state.y, skip=self.here(),
                                   stations=self.stations)[:POST_CHOICES]

    def here(self) -> tuple[str, int] | None:
        """The town or station the balloon is at, if it is at one."""
        return self.engine.place(self.state.x, self.state.y)

    def at_office(self) -> bool:
        """On the ground at a town or station: the post office and the stores are open."""
        return self.state.status == "ground" and self.here() is not None and self.drift is None

    def _names(self) -> str:
        names = [self.place_of(t)["name"] for t in self.targets]
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]

    def weight(self) -> int:
        return sum(item["weight"] for item in self.manifest) + self.kits * KIT_WEIGHT + self.crates * CRATE_WEIGHT

    def _load_cargo(self, offer: dict) -> dict:
        item = dict(offer, id=self.next_id)
        there = self.place_of(tuple(item["dest"]))
        # How far it has to go from where it comes aboard: what "part of the way" is measured against. From
        # the office itself, not from wherever in its field the balloon stands, so a hop across town is no way.
        ref = self.here()
        start = self.place_of(ref) if ref is not None else {"x": self.state.x, "y": self.state.y}
        item["km"] = round(math.sqrt((there["x"] - start["x"]) ** 2 + (there["y"] - start["y"]) ** 2), 1)
        self.next_id += 1
        self.manifest.append(item)
        self.state = dataclasses.replace(
            self.state, cargo=self.state.cargo + ((item["id"], CARGO_KINDS[item["kind"]]["fragile"], "ok"),))
        return item

    def _unload(self, item: dict) -> None:
        self.manifest = [other for other in self.manifest if other["id"] != item["id"]]
        self.state = dataclasses.replace(self.state, cargo=tuple(c for c in self.state.cargo if c[0] != item["id"]))

    def offers(self) -> list[dict]:
        """What the post office here offers today."""
        return contracts_at(self.world, self.here(), self.state.day, self.stations) if self.at_office() else []

    def held(self, offer: dict) -> dict | None:
        """The cargo aboard that came from this offer, if the pilot took it."""
        return next((item for item in self.manifest if item["contract"] == offer["contract"]), None)

    def cargo_text(self, item: dict, long: bool = True) -> str:
        name = CARGO_KINDS[item["kind"]]["name"]
        return f"{name} for {self.place_of(tuple(item['dest']))['name']}" + (f", by Day {item['due']}" if long else "")

    def take(self, offer: dict) -> str:
        """Take a contract, or put back one taken here today. Returns what to tell the pilot."""
        mine = self.held(offer)
        if mine is not None:
            if any(c[0] == mine["id"] and c[2] != "ok" for c in self.state.cargo):
                return "That one is damaged: it is yours to deliver now."
            self._unload(mine)
            self.taken = [t for t in self.taken if t != offer["contract"]]
            self.save("post")
            return f"Put back: {self.cargo_text(mine, long=False)}."
        if offer["contract"] in self.taken:
            return "You have carried that one already."
        if self.grade() < CARGO_GRADE[offer["kind"]]:
            return f"That is entrusted to a licence of {GRADES[CARGO_GRADE[offer['kind']]][0]} or better."
        if self.weight() + offer["weight"] > self.lift():
            return f"Too heavy: the balloon lifts {self.lift()} and carries {self.weight()} already."
        item = self._load_cargo(offer)
        self.taken.append(offer["contract"])
        self.save("post")
        return f"Aboard: {self.cargo_text(item)}. It pays {item['payout']}."

    def buy(self, item: str, count: int | None = None) -> str:
        """Buy fuel, sacks or pibals: so many, or with no count as many as fit and can be paid for."""
        state, each = self.state, price_of(item, self.here())
        have = {"fuel": state.fuel, "sack": state.ballast, "pibal": state.pibals}[item]
        room = (self.tank() if item == "fuel" else STORES_FULL[item]) - have
        want = room if count is None else min(count, room)
        got = min(want, self.postmarks // each)
        word = {"fuel": "fuel", "sack": "sacks", "pibal": "pibals"}[item]
        if room <= 0:
            return f"No room for more {word}."
        if got <= 0:
            return f"Not enough postmarks: {each} each."
        field = {"fuel": "fuel", "sack": "ballast", "pibal": "pibals"}[item]
        self.state = dataclasses.replace(state, **{field: have + got})
        self.postmarks -= got * each
        self.save("stores")
        short = "" if got == want else " That is all the postmarks would buy."
        return f"Bought {got} {word if got != 1 else word.rstrip('s')} for {got * each}.{short}"

    def mend(self) -> str:
        """Mend as much wear as can be paid for."""
        each = price_of("mend", self.here())
        points = min(self.state.wear, self.postmarks // each)
        if self.state.wear <= 0:
            return "Nothing to mend."
        if points <= 0:
            return f"Not enough postmarks: {each} a point."
        self.state = dataclasses.replace(self.state, wear=self.state.wear - points)
        self.postmarks -= points * each
        self.save("stores")
        return f"Mended {points} wear for {points * each}." + ("" if self.state.wear == 0 else " The rest wants more postmarks.")

    def relief_due(self) -> bool:
        """May the pilot ask for the relief run? Only one who cannot fly and cannot pay to."""
        if not self.at_office() or self._relief_mark() in self.taken:
            return False
        state, ref = self.state, self.here()
        # Too little fuel to fly a run on, or too worn to fly at all; and not the postmarks to put it right.
        need = max(0, RELIEF["fuel"] - state.fuel) * price_of("fuel", ref)
        need += max(0, state.wear - RELIEF["wear"]) * price_of("mend", ref) if state.grounded else 0
        short = state.fuel < RELIEF["below"] or state.grounded
        return short and self.postmarks < need

    def _relief_mark(self) -> list:
        """The line in the list of what was taken that says: the relief run, today. Shaped like any other."""
        return ["relief", 0, self.state.day, 0]

    def relief(self) -> str:
        """The postmaster fuels and patches a pilot who cannot pay, and gives them a mailbag to carry."""
        state, ref = self.state, self.here()
        self.state = dataclasses.replace(state, fuel=max(state.fuel, RELIEF["fuel"]),
                                         wear=min(state.wear, RELIEF["wear"]),
                                         ballast=max(state.ballast, RELIEF["sack"]))
        self.taken.append(self._relief_mark())
        said = "The postmaster fuels and patches the balloon on account."
        near = places_near(self.world, state.x, state.y, skip=ref, stations=self.stations)
        if near and self.weight() + CARGO_KINDS["mail"]["weight"] <= self.lift():
            item = self._load_cargo({"contract": None, "kind": "mail", "dest": list(near[0]), "origin": list(ref),
                                     "weight": CARGO_KINDS["mail"]["weight"], "payout": RELIEF["payout"],
                                     "due": state.day + RELIEF["days"]})
            said += f" Aboard: {self.cargo_text(item)}."
        self.save("relief")
        return said

    def bearings(self, x: float | None = None, y: float | None = None) -> list[tuple[float, str, dict]]:
        """Distance, direction and place for each destination, nearest first."""
        x = self.state.x if x is None else x
        y = self.state.y if y is None else y
        if self.drift is not None:
            course = self.drift["course"]
            dx, dy = course["mx"] - x, course["my"] - y
            return [(math.sqrt(dx * dx + dy * dy), bearing(dx, dy), {"name": "the mark", "x": course["mx"],
                                                                     "y": course["my"]})]
        out = []
        for ref in self.targets:
            place = self.place_of(ref)
            dx, dy = place["x"] - x, place["y"] - y
            out.append((math.sqrt(dx * dx + dy * dy), bearing(dx, dy), place))
        return sorted(out, key=lambda item: item[0])

    def coverage(self, x: float, y: float) -> float:
        """How well the best town, mast or station in range covers this place, 0 to 1.

        A town, a mast and each of the world's first stations report in
        full. A planted station reports as well as it is stocked.
        """
        best, reach = 0.0, self.fitted("wireless")["reach"]
        for place in self.world.towns + self.stations:
            d = math.sqrt((x - place["x"]) ** 2 + (y - place["y"]) ** 2)
            if d >= reach:
                continue
            share = 1.0 if staffed(place) else min(1.0, stock_of(place, self.today) / STOCK_SHARP)
            best = max(best, (1.0 - d / reach) * share)
        # The Gyre is its own weather: the nearer its middle, the less any station can say of the air.
        near = self.engine.gyre_km(x, y)
        if near <= GYRE_INNER_KM:
            return 0.0
        return min(best, GYRE_OUTER_COVER) if near <= GYRE_OUTER_KM else best

    # -- the licence and the gear ---------------------------------------------

    def fitted(self, track: str) -> dict:
        """The tier of a piece of gear this balloon has."""
        return UPGRADES[track]["tiers"][self.gear[track]]

    def lift(self) -> int:
        return self.fitted("envelope")["lift"]

    def tank(self) -> int:
        return self.fitted("tanks")["tank"]

    def score(self) -> int:
        """Logbook points: what deliveries and clean landings earned, and a point for every ten km flown."""
        return self.points + int((self.total_km + self.state.distance) * POINTS["km"])

    def grade(self) -> int:
        return grade_of(self.score())

    def at_loft(self) -> bool:
        """The rigging loft is in towns."""
        return self.at_office() and self.here()[0] == "town"

    def _fit(self) -> None:
        """Set the flight's own figures from the gear: what the engine flies by."""
        envelope = self.fitted("envelope")
        oxygen = self.fitted("oxygen")["oxygen"]
        ceiling = envelope["ceiling"] + (1 if oxygen and envelope["ceiling"] == LAYERS - 1 else 0)
        self.state = dataclasses.replace(self.state, ceiling=ceiling, oxygen=oxygen,
                                         night_gear=self.fitted("night")["night"],
                                         climb_cost=self.fitted("burner")["climb"],
                                         reads=self.fitted("instruments")["reads"])

    def _promote(self) -> None:
        """Say so when the points have made a new grade."""
        grade = self.grade()
        if grade > self._grade_seen:
            self._say(f"The Post makes you {'an' if GRADES[grade][0][0] in 'AEIOU' else 'a'} {GRADES[grade][0]}.")
        self._grade_seen = grade

    def cannot_fit(self, track: str) -> str:
        """Why the next tier of this gear cannot be bought now, or "" if it can."""
        tiers, have = UPGRADES[track]["tiers"], self.gear[track]
        if have + 1 >= len(tiers):
            return "nothing more to fit"
        tier = tiers[have + 1]
        if self.grade() < tier["grade"]:
            return f"sold to {GRADES[tier['grade']][0]}s"
        if "needs" in tier and self.gear[tier["needs"][0]] < tier["needs"][1]:
            other, need = tier["needs"]
            return f"needs {UPGRADES[other]['name']} {TIER_NAMES[need]}"
        if self.postmarks < tier["price"]:
            return "not enough postmarks"
        return ""

    def fit(self, track: str) -> str:
        """Buy the next tier of a piece of gear. Returns what to tell the pilot."""
        why = self.cannot_fit(track)
        name = UPGRADES[track]["name"]
        if why:
            return f"{name}: {why}."
        self.gear[track] += 1
        tier = self.fitted(track)
        self.postmarks -= tier["price"]
        self._fit()
        self.save("gear")
        return f"Fitted: {name}, {gear_text(track, tier)}."

    def paint(self, what: str) -> str:
        """Change the envelope's pattern, or one of its four colours, to the next."""
        if what == "pattern":
            self.design["pattern"] = PATTERNS[(PATTERNS.index(self.design["pattern"]) + 1) % len(PATTERNS)]
        else:
            self.design["colours"][what] = (self.design["colours"][what] + 1) % len(ENVELOPE_COLOURS)
        self.save("design")
        names = ", ".join(ENVELOPE_COLOURS[c][0] for c in self.design["colours"])
        return f"The envelope: {self.design['pattern']} in {names}."

    def _loft_key(self, k: str) -> None:
        if not self.at_loft() or k in ("ENTER", "ESC", "b", "g", " "):
            self.loft = False
            return
        if len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(TRACKS):
            self.hub_note = self.fit(TRACKS[int(k) - 1])
        elif k == "p":
            self.hub_note = self.paint("pattern")
        elif k in ("z", "x", "c", "v"):
            self.hub_note = self.paint("zxcv".index(k))
        self._choose_default()

    def _see_others(self) -> None:
        """Where the other pilots of this world are, for the map."""
        self.others = []
        if self.store is None:
            return
        try:
            rows = self.store.others(self.launch.user_id)
        except sqlite3.Error:
            return
        for career, state in rows:
            try:
                data = json.loads(state)
                colour = design_rgb(design_of(json.loads(career).get("design")))[0]
                x, y = float(data["x"]), float(data["y"])
                if math.isfinite(x) and math.isfinite(y):
                    self.others.append((x, y, colour))
            except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
                continue                  # somebody else's record is somebody else's trouble
        del self.others[OTHERS_SHOWN:]

    # -- choosing ------------------------------------------------------------

    def choices(self) -> list[str]:
        """The layer choices on offer, lowest first."""
        offered = self.offered(self.state)
        return [a for a in ("sink", "hold", "climb", "launch") if a in offered]

    def _choose_default(self) -> None:
        offered = self.offered(self.state)
        self.choice = "hold" if "hold" in offered else next(iter(self.choices()), None)
        if self.choice is None and "land" in offered:
            self.choice = "land"

    def preview(self, action: str) -> dict:
        return self.engine.preview(self.state, action, self.fitted("instruments")["horizon"],
                                   self.coverage(self.state.x, self.state.y))

    def phase(self) -> str:
        state = self.state
        if state.status == "aloft":
            return "aloft"
        if state.status in ("stranded", "ditched"):
            return state.status
        if state.status == "ground" and self.here() is None and (state.grounded or state.fuel < COST_LAUNCH):
            return "stranded"             # down in the wild and unable to fly: no night will change that
        if self.waiting():
            return "wait"
        return "ground" if self.offered(state) else "rest"

    # -- keys ----------------------------------------------------------------

    def key(self, key: str) -> str | None:
        """Handle a key. Returns "quit" to leave, "help" for help, else None."""
        self.frames_left = 0              # the pilot has moved on: the wind holds still until something changes
        k = key.lower() if len(key) == 1 else key
        if self.ended:
            return "quit"                 # any key: there is nothing left to do here
        if self.writing is not None and self.busy is None and k != "CTRL-C":
            self._write_key(key)          # a line is being written: q and ? are letters like any other
            return None
        if self.atlas and self.atlas_view["find"] is not None and self.busy is None and k != "CTRL-C" \
                and not self.naming:
            self._atlas_key(k, key)       # and so they are in a name being looked for
            return None
        if k in ("q", "CTRL-C"):
            return "quit"
        if self.busy is not None:
            if k == "t":
                self.take_over()
            return None
        if k in ("?", "F1"):
            return "help"
        if self.waiting() and self._day_may_have_opened():
            return None                   # the day opened while the pilot waited: show it before any key means anything
        if self.naming:
            self._name_it(int(k) - 1 if len(k) == 1 and k.isdigit() else 0)
            return None                   # the key was the name's, whatever it was
        if self.atlas:
            self._atlas_key(k, key)
            return None
        if self.book is not None:
            self._book_key(k)
            return None
        if self.pages:
            self.pages = False            # any key closes the almanac, and is used up
            return None
        if self.drift_lobby:
            self._drift_lobby_key(k)
            return None
        if self.trials:
            self._trials_key(k)
            return None
        if self.drift is not None:
            if k in ("x", "ESC"):
                # X sits among the keys that fly. A scored Drift left by a slip waits where it was; a practice
                # would be gone, so that one asks twice.
                if self.drift["scored"] or self.drift.get("leaving"):
                    self._drift_end(left=True)
                else:
                    self.drift["leaving"] = True
                    self.heard = []
                    self._say("Leave this flight? X again leaves it; any other key flies on.")
                return None
            self.drift["leaving"] = False
            if self.drift.get("kind") == "trial" and k in ("w", "s", "UP", "DOWN"):
                self._tutor("look")
        elif k == "t" and not self.hub and not self.loft and self.state.status != "aloft":
            self._drift_open()
            return None
        elif k == "r" and not self.hub and not self.loft and self.state.status != "aloft":
            self.trials, self.trial_note = True, ""
            return None
        if k == "c" and not self.hub and not self.loft:
            self.atlas = True
            return None
        if k == "v" and not self.hub and not self.loft:
            self._see_almanac()
            self.pages = True
            return None
        if self.album_open:
            if k in ("w", "UP"):
                self.album_page -= 1
            elif k in ("s", "DOWN"):
                self.album_page = min(self.album_page + 1, len(self._album_pages()) - 1)
            else:
                self.album_open = False
            self.album_page = max(0, self.album_page)
            return None
        if k == "u" and not self.hub and not self.loft and self.drift is None:
            self._see_wonders()
            self.album_open = True
            return None
        if self.season_open:
            self.season_open = False      # any key closes it, and is used up
            return None
        if k == "y" and not self.hub and not self.loft and self.drift is None:
            self._see_season()
            self.season_open = True
            return None
        if k == "f" and not self.hub and not self.loft and self.wonder_here() is not None:
            self._capture()
            return None
        if k == "b" and not self.hub and not self.loft and self.book_place() is not None:
            station = self.book_place()
            self.book = {"station": station, "lines": self._book_lines(station), "note": ""}
            return None
        if self.hub:
            self._hub_key(k)
            return None
        if self.loft:
            self._loft_key(k)
            return None
        if k == "g" and self.at_loft():
            self.loft, self.hub_note = True, ""
            return None
        if k == "o" and self.at_office():
            self.hub, self.hub_note = True, self._raised_by()
            return None
        if k == "k" and self.kits and self.state.status in ("ground", "stranded") and self.here() is None \
                and self.drift is None:
            self._plant()
            return None
        phase = self.phase()
        offered = self.offered()
        if phase in ("aloft", "ground"):
            options = self.choices()
            if k in ("w", "s", "UP", "DOWN") and self.choice in ("left", "right") and "hold" in options:
                self.choice = "hold"          # the rope is a kind of hold: the layers are counted from there
            if k in ("w", "UP") and self.choice in options:
                self.choice = options[min(len(options) - 1, options.index(self.choice) + 1)]
            elif k in ("s", "DOWN") and self.choice in options:
                self.choice = options[max(0, options.index(self.choice) - 1)]
            elif k in ("w", "s", "UP", "DOWN") and options:
                self.choice = options[0]
            elif k in ("a", "LEFT") and "left" in offered:
                self.choice = "left"
            elif k in ("d", "RIGHT") and "right" in offered:
                self.choice = "right"
            elif k == "m" and self.droppable():
                self._drop()
            elif k == "h" and phase == "ground":
                self._wait()
            elif k == "j" and "jettison" in offered:
                self.choice = "jettison"
            elif k == "l" and "land" in offered:
                self.choice = "land"
            elif k == "l" and "launch" in offered:
                self.choice = "launch"
            elif k == "p" and "pibal" in offered:
                self._commit("pibal")
            elif k in ("ENTER", " ") and self.choice in offered:
                self._commit(self.choice)
            elif k == "n" and phase == "ground":
                self._night()
        elif phase == "rest" and k in ("n", "ENTER", " "):
            self._night()
        elif phase == "stranded" and k == "w":
            self._walk_out()              # W alone: a key meant for the flight must not cost two days
        elif phase == "ditched" and k in ("n", "ENTER", " "):
            self._picked_up()
        return None

    def _hub_key(self, k: str) -> None:
        """A key at the post office."""
        if not self.at_office() or k in ("ENTER", "ESC", "b", "o", " "):
            self.hub = False
            return
        if k == "g" and self.at_loft():
            self.hub, self.loft, self.hub_note = False, True, ""
            return
        offers = self.offers()
        if len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(offers):
            self.hub_note = self.take(offers[int(k) - 1])
        elif k == "f":
            self.hub_note = self.buy("fuel", 10)
        elif k == "a":
            self.hub_note = self.buy("fuel")
        elif k == "s":
            self.hub_note = self.buy("sack", 1)
        elif k == "p":
            self.hub_note = self.buy("pibal", 1)
        elif k == "m":
            self.hub_note = self.mend()
        elif k == "r" and self.relief_due():
            self.hub_note = self.relief()
        elif k == "w":
            self.hub_note = self.forward()
        elif k == "k" and self.at_loft():
            self.hub_note = self.kit()
        elif k == "t" and self.at_loft():
            self.hub_note = self.crate(1)
        elif k == "y" and self.at_loft():
            self.hub_note = self.crate(-1)
        self._choose_default()

    def _say(self, line: str) -> None:
        """Add to the log, wrapped to the screen. The last three lines show."""
        self.heard.append(line)
        words, current = line.split(" "), ""
        for word in words:
            if current and len(current) + 1 + len(word) > 76:
                self.log.append(current)
                current = "       " + word          # a continued line sits under the text, not the clock
            else:
                current = f"{current} {word}" if current else word
        self.log.append(current)
        del self.log[:-3]

    def _commit(self, action: str) -> None:
        if self.store is not None and not self._mine(write=False):
            return                        # taken over: nothing is worked out or shown from here any more
        if self.drift is not None:
            self._drift_commit(action)
            return
        before = self.state
        result = self.engine.step(before, action)
        self.state = result.state
        self.heard = []
        stamp = clock_text(before.minute)
        for event in result.events:
            line = self._describe(event)
            if line:
                self._say(f"{stamp}  {line}")
        self._lost_cargo()
        if self.state.status == "ditched" and (self.kits or self.crates):
            self.kits = self.crates = 0
            self._say("The station freight went down with the basket.")
        landed = before.status == "aloft" and self.state.status != "aloft"
        if action == "launch":
            self.lift_off = [before.x, before.y]
        if any(event[0] == "land" and event[1] == "clean" for event in result.events) and self.lift_off is not None:
            # Hopping up and down beside the loft is not airmanship: the flight has to have gone somewhere.
            far = math.sqrt((self.state.x - self.lift_off[0]) ** 2 + (self.state.y - self.lift_off[1]) ** 2)
            if far >= CLEAN_LANDING_KM:
                self.points += POINTS["clean"]
        if landed:
            self._landed()
            self._say_charted()
            self._route_to(self.state.x, self.state.y)
        self._promote()
        seen = []
        if action != "pibal":
            # What was seen from the basket this turn: from where the turn ended, at the height it was flown.
            layer = self.state.layer or before.layer or (self.engine.lowest_open(before.x, before.y)
                                                         if action == "launch" else 0)
            # Along the way, a sight's reach at a time: a fast wind carries a balloon further in a turn than
            # it sees from one spot, and what it passed over was seen too.
            reach = SIGHT_KM[min(max(layer, 1), len(SIGHT_KM)) - 1]
            run = math.sqrt((self.state.x - before.x) ** 2 + (self.state.y - before.y) ** 2)
            steps = max(1, int(math.ceil(run / reach)))
            got: set = set()
            for i in range(1, steps + 1):
                t = i / steps
                for cell in self.seen_cells(before.x + (self.state.x - before.x) * t,
                                            before.y + (self.state.y - before.y) * t, layer, before.day, before.minute):
                    if cell not in got:
                        got.add(cell)
                        seen.append(cell)
        # A local wind flown while it blew is written with the turn that flew it. It blew when the turn
        # began, and the balloon is in it where the turn ends: the wind of a turn is the wind it sets out on.
        wind = self.wind_here(self.state, before.day, before.minute)
        launch, day = self.launch, before.day
        deeds = []                        # what the turn does to the shared world, and what comes of each for the pilot
        if wind is not None and wind["id"] not in self.almanac:
            deeds.append((lambda db: fly_wind(db, wind["id"], launch.user_id, launch.handle, day),
                          lambda done: self._wind_flown(wind, done, day)))
        if not self.eye and self.state.status == "aloft" and \
                self.engine.gyre_km(self.state.x, self.state.y) <= GYRE_EYE_KM:
            # In the eye of the Gyre and not yet written down as having been: written with this turn. By where
            # the balloon is, not by the turn it came in on, so that a turn that could not be written is made
            # good by the next.
            deeds.append((lambda db: enter_hall(db, "eye", launch.user_id, launch.handle, day), self._in_the_eye))
        if deeds:
            self.save(action, seen, lambda db: [deed(db) for deed, _ in deeds],
                      lambda done: [earn(got) for (_, earn), got in zip(deeds, done)])
            if self._almanac_stale:
                self._almanac_stale = False
                self._see_almanac()       # the wind is on the map from the next paint, under whoever entered it
        else:
            self.save(action, seen)       # before anything is shown
        if self.ended:
            return
        if landed:
            self._visit()                 # a station here is seen to, in a write of its own
            self._rescue()                # and a pilot stranded here is helped
            self._race(before.day)        # and in a season's last week the flight is one of its race
            if self.ended:
                return
        self._sync_call()                 # stranded: the call for help goes out
        if self.ended:
            return
        self._point_out()                 # a wonder that has come into view
        if (action == "launch" or landed) and self.saved_ok:
            # No picture of a turn that is not on disk: the screen says it was not saved instead.
            self.pending_scene = self._scene_for("launch" if action == "launch" else "landing")
        if action != "pibal":
            self.typed_ahead = True
            if self.motion:
                self.frames_left = BURST_FRAMES
        self._choose_default()

    def _scene_for(self, kind: str) -> dict:
        """What a launch or a landing looked like: the hour, the sky, the land, and what to say under it."""
        state = self.state
        minute = state.minute - TURN_MINUTES            # the moment of the turn just flown
        frame = self.weather.frame(state.day, minute)
        place = self.engine.place(state.x, state.y)
        name = self.place_of(place)["name"] if place else ""
        if kind == "launch":
            caption = f"Day {state.day}, {clock_text(minute)}. Up and away" + (f" from {name}." if name else ".")
            sub = ""
        else:
            lines = [line.split("  ", 1)[-1] if line[:2].isdigit() else line for line in self.heard]
            caption = next((line for line in lines if line.startswith(("Down ", "Midnight"))),
                           lines[0] if lines else "Down.")
            sub = next((line for line in lines if line != caption
                        and any(word in line for word in ("Delivered", "Nothing aboard", "stranded", "Lost:"))), "")
        return {"kind": kind, "hour": minute / 60.0, "cloud": frame.cloud(state.x, state.y)[0],
                "biome": self.world.sample(state.x, state.y)[1], "place": place is not None,
                "seed": int(state.x) * 7919 + int(state.y) * 104729 + 17, "caption": caption, "sub": sub,
                "design": dict(self.design)}

    def _describe(self, event: tuple) -> str:
        code = event[0]
        if code == "launch":
            return f"Up and away into layer {event[1]}, {LAYER_NAMES[event[1] - 1]}."
        if code in ("hold", "climb", "sink", "jettison"):
            return ""                      # the drift line says it
        if code in ("left", "right"):
            return f"The rope is out: bearing {code} of the wind."
        if code == "drift":
            return f"Layer {self.state.layer if self.state.layer else '-'}: {event[1]:g} km, wind {event[2]}."
        if code == "forced_climb":
            return f"High ground: forced up into layer {event[1]} for {event[2]} fuel."
        if code == "forced_jettison":
            return f"High ground and no fuel to spare: a sack goes over the side for layer {event[1]}."
        if code == "ridge":
            return "High ground ahead rises above your ceiling. The balloon is held in its lee."
        if code == "wall":
            return (f"The wall of the Gyre throws the balloon back. Nothing below layer {GYRE_WALL_LAYER} "
                    f"gets through it.")
        if code == "eye":
            return "The air goes still. This is the eye of the Gyre."
        if code == "high_ground":
            return "High ground, and nothing left to climb with."
        if code == "squall":
            return f"A squall throws the balloon into layer {event[1]}."
        if code == "rotor":
            return "Rough air behind the high ground shakes the balloon."
        if code == "pibal":
            return f"A pibal goes up: true winds in every layer for {PIBAL_TURNS} turns."
        if code == "midnight":
            return "Midnight. The day is over and the balloon comes down."
        if code == "ditch":
            return "Down on the water. The post is lost."
        if code == "land":
            place, hut = event[2], self.hut()
            where = "in the wild"
            if place:
                where = f"at {self.place_of(place)['name']}"
            elif hut is not None:
                where = f"at {hut['name']}"
            return f"Down {where}: a {event[1]} landing."
        if code == "stranded":
            return "No fuel, and nobody near. You are stranded."
        return ""

    def forwardable(self) -> list[tuple[dict, int]]:
        """Cargo bound elsewhere that this post office would send on by road, and what it would pay for each.

        The road pays for the part of the way the balloon carried it: nothing
        at the counter it was taken from, half its payout at the door of
        where it was going.
        """
        if not self.at_office():
            return []
        state, here, out = self.state, self.here(), []
        office = self.place_of(here)
        condition = {item[0]: item[2] for item in state.cargo}
        for item in self.manifest:
            if tuple(item["dest"]) == here or not item.get("km") or item.get("origin") == list(here):
                continue
            there = self.place_of(tuple(item["dest"]))
            left = math.sqrt((there["x"] - office["x"]) ** 2 + (there["y"] - office["y"]) ** 2)
            pay = item["payout"] * FORWARD_SHARE * max(0.0, 1.0 - left / item["km"])
            if state.day > item["due"]:
                pay *= LATE_SHARE
            if condition.get(item["id"]) == "damaged":
                pay *= DAMAGED_SHARE
            if int(pay) > 0:
                out.append((item, int(pay)))
        return out

    def forward(self) -> str:
        """Hand over everything the road will take. Returns what to tell the pilot."""
        if self.store is not None and not self._mine(write=False):
            return ""
        going = self.forwardable()
        if not going:
            return "The road pays nothing for what is aboard: it has come no nearer to where it is going."
        paid = sum(pay for _, pay in going)
        for item, _ in going:
            self._unload(item)
        self.postmarks += paid
        self.save("forward")
        what = self.cargo_text(going[0][0], long=False) if len(going) == 1 else f"{len(going)} pieces"
        return f"Sent on by road: {what}, {paid} postmarks for the part of the way you carried it."

    def droppable(self) -> list[dict]:
        """The mailbags that could be dropped now: flying low, close to where they are bound."""
        state = self.state
        if self.drift is not None or state.status != "aloft" \
                or state.layer > self.engine.lowest_open(state.x, state.y) + DROP_ABOVE:
            return []
        out = []
        for item in self.manifest:
            there = self.place_of(tuple(item["dest"]))
            left = math.sqrt((there["x"] - state.x) ** 2 + (there["y"] - state.y) ** 2)
            # Close enough to throw, and carried at least half its way: a bag for the next village is flown there.
            if item["kind"] == "mail" and left <= DROP_KM and (not item.get("km") or left <= item["km"] / 2):
                out.append(item)
        return out

    def _drop(self) -> None:
        """Throw the mail down to the place below. It takes no turn, and pays less than handing it over."""
        if self.store is not None and not self._mine(write=False):
            return
        mine = self.droppable()
        paid = 0
        for item in mine:
            pay = int(item["payout"] * DROP_SHARE)
            if self.state.day > item["due"]:
                pay = int(pay * LATE_SHARE)
            paid += pay
            self._unload(item)
        self.deliveries += len(mine)
        self.postmarks += paid
        self.points += POINTS["mail"] * len(mine)
        names = sorted({self.place_of(tuple(item["dest"]))["name"] for item in mine})
        what = "a mailbag" if len(mine) == 1 else f"{len(mine)} mailbags"
        self.heard = []
        self._say(f"{clock_text(self.state.minute)}  Dropped over {' and '.join(names)}: {what}, {paid} postmarks.")
        self._promote()
        self.save("drop")
        self._choose_default()

    def _wait(self) -> None:
        """Sit on the ground for an hour and let the wind change. It costs nothing but the hour."""
        state = self.state
        if state.turn >= DAY_TURNS or state.status != "ground" or self.drift is not None:
            return
        if self.store is not None and not self._mine(write=False):
            return
        self.state = dataclasses.replace(state, turn=min(DAY_TURNS, state.turn + WAIT_TURNS))
        self.heard = []
        self._say(f"{clock_text(self.state.minute)}  An hour on the ground.")
        self.save("wait")
        if self.motion:
            self.frames_left = BURST_FRAMES
        self._choose_default()

    def _route_to(self, x: float, y: float) -> None:
        """Another point on the route the chart draws: where a flight, a walk or a boat ended."""
        self.route = (self.route + [[round(x, 1), round(y, 1)]])[-ROUTE_KEPT:]

    def _lost_cargo(self) -> None:
        """Cargo the turn threw overboard or sank: struck from the manifest, and what the table says is forfeit."""
        aboard = {item[0] for item in self.state.cargo}
        lost = [item for item in self.manifest if item["id"] not in aboard]
        if not lost:
            return
        self.manifest = [item for item in self.manifest if item["id"] in aboard]
        forfeit = min(self.postmarks, sum(CARGO_KINDS[item["kind"]]["lost"] for item in lost))
        self.postmarks -= forfeit
        what = self.cargo_text(lost[0], long=False) if len(lost) == 1 else f"{len(lost)} pieces of cargo"
        self._say(f"Lost: {what}. {forfeit} postmarks forfeit.")

    def _landed(self, arrived: bool = False) -> None:
        """Hand over what is aboard for this place. arrived: on foot or by boat, where silence says nothing was."""
        ref = self.here()
        if self.state.status != "ground" or ref is None:
            return
        name = self.place_of(ref)["name"]
        mine = [item for item in self.manifest if tuple(item["dest"]) == ref]
        if not mine:
            if not arrived:
                self._say(f"Nothing aboard was for {name}. The post office is open: O.")
            return
        condition = {item[0]: item[2] for item in self.state.cargo}
        paid, notes = 0, []
        for item in mine:
            pay = item["payout"]
            if self.state.day > item["due"]:
                pay = int(pay * LATE_SHARE)
                notes.append("late")
            if condition.get(item["id"]) == "damaged":
                pay = int(pay * DAMAGED_SHARE)
                notes.append("damaged")
            paid += pay
            self._unload(item)
        self.deliveries += len(mine)
        self.postmarks += paid
        self.points += sum(POINTS[item["kind"]] for item in mine)
        what = self.cargo_text(mine[0], long=False).split(" for ")[0] if len(mine) == 1 else f"{len(mine)} pieces"
        note = f" ({', '.join(sorted(set(notes)))}: less is paid)" if notes else ""
        self._say(f"Delivered to {name}: {what.lower()}, {paid} postmarks{note}. That makes {self.deliveries}.")

    def _new_day(self, days: int, x: float | None = None, y: float | None = None) -> None:
        state = self.state
        self.hub = self.loft = False
        self.taken = [t for t in self.taken if t[2] >= state.day + days]
        self.total_km += state.distance        # today's distance joins the total as the day ends
        if state.distance > 0 or state.status != "ground":
            self.days_flown += 1              # a day nothing was flown on, or only waited out, is not a flying day
        self.state = dataclasses.replace(state, day=state.day + days, turn=0, layer=0, status="ground",
                                         distance=0.0, pibal_until=-1,
                                         x=state.x if x is None else x, y=state.y if y is None else y)
        self._choose_default()

    def _dawn(self, look: bool = True) -> str:
        """What to say of the day the logbook has come to: it is here, or when it will be.

        look=False inside a write: looking at the calendar may open a day,
        which is a write of its own.
        """
        if look:
            self._look_at_the_calendar()
        self._catch_up()                  # days that lapsed while this one was in progress
        if not self.waiting():
            return f"Day {self.state.day}. Dawn."
        opens = when_text(calendar_opens(self.calendar, self.state.day), self._local(), self.launch.datetime_format)
        return f"Day {self.state.day} opens {opens}."

    def _night(self) -> None:
        if self.drift is not None:
            return
        self._new_day(1)
        self._say(self._dawn())
        self.save("night")                # once, with the new day and its log together
        self._sync_call()

    def _nearest(self, towns) -> dict:
        return min(towns, key=lambda t: ((t["x"] - self.state.x) ** 2 + (t["y"] - self.state.y) ** 2, t["cell"]))

    def _walk_out(self) -> None:
        if self.store is not None and not self._mine(write=False):
            return
        self._look_at_the_calendar()      # now, not inside the write below

        def apply(done):
            # The call comes down in the same write as the walk. If somebody reached the pilot first, even in
            # this instant, their fuel is taken and nobody walks: a rescuer is never paid for nothing.
            if self._take_help(done):
                return
            town = self._nearest(self.world.towns)
            self._new_day(2, town["x"], town["y"])
            self._route_to(town["x"], town["y"])
            self._say(f"Two days on foot to {town['name']}. {self._dawn(look=False)}")
            self._landed(arrived=True)    # the post came along, and some of it may be for here
        if self._deed("walk", lambda db: end_call(db, self.launch.user_id), apply):
            self._called = False
        self._choose_default()

    def _picked_up(self) -> None:
        ports = [t for t in self.world.towns if t["port"]] or self.world.towns
        town = self._nearest(ports)
        self._new_day(1, town["x"], town["y"])
        self._route_to(town["x"], town["y"])
        self._say(f"The packet boat lands you at {town['name']}. {self._dawn()}")
        self._landed(arrived=True)
        self.save("boat")

    # -- the screen ----------------------------------------------------------

    def _draw_lease(self, s: "Session") -> list[str]:
        """The two screens about who is flying this pilot: someone else is, or someone else now is."""
        launch = self.launch
        wide = s.width - 2
        out = [s._line(0, 1, "FAIR WINDS", "accent")]
        if self.ended:
            lines = [f"{launch.handle} is now flying from another session.",
                     "Every turn flown here was saved. That session flies on from the last of them."]
            keys = [("Any key", f"Return to {launch.node_name}")]
        else:
            minutes = max(0, int((self.busy["now"] - self.busy["started"]) / 60))
            since = "a moment ago" if minutes < 1 else "a minute ago" if minutes == 1 else f"{minutes} minutes ago"
            lines = [f"{launch.handle} is already flying: a session over {self.busy['transport']}, opened {since}.",
                     "A pilot flies in one place at a time.",
                     "",
                     "If that is you on another screen, or a call that was cut off,",
                     "you can fly here. The other session then ends at its next turn.",
                     "Nothing is lost."]
            keys = [("T", "Take over here"), ("Q", f"Return to {launch.node_name}")]
        for i, line in enumerate(lines):
            out.append(s._line(2 + i, 1, line[:wide], "text" if i == 0 else "dim"))
        out += s._keys(s.height - 1, keys)
        return out

    def _status(self, s: "Session", wide: int) -> str:
        """The line under the sentence: when the next day opens first, then as much else as there is room for."""
        launch = self.launch
        # The briefing may carry a date in the caller's own format: no mark of ours may come in with it.
        status = self.briefing().replace("`", " ")[:wide]
        length = len(status)
        for part in (f"`{self.postmarks}` postmarks", f"`{self.deliveries}` delivered",
                     f"`{self.total_km + self.state.distance:.0f}` km",
                     f"`{self.charted_share() * 100:.0f}`% charted", f"`{launch.handle.replace('`', ' ')}`",
                     f"{s.width} x {s.height} {launch.color_depth} {layout_tier(s.width, s.height)}"
                     f" {'standalone' if launch.host == 'standalone' else launch.transport}"):
            plain = len(part.replace("`", ""))
            if length + 3 + plain > wide:
                break
            status += "   " + part
            length += 3 + plain
        return status                     # marked: figures between backticks, for the line that draws it

    # -- the chart of the whole continent (S17) ----------------------------------

    def _atlas_window(self) -> tuple[float, float, float, float]:
        """The part of the world the chart shows: its corner, and how wide and tall, in km."""
        view = self.atlas_view
        zoom = ATLAS_ZOOMS[view["zoom"]]
        wide, tall = WORLD_W / zoom, WORLD_H / zoom
        x0 = min(max(view["x"] - wide / 2, 0.0), WORLD_W - wide)
        y0 = min(max(view["y"] - tall / 2, 0.0), WORLD_H - tall)
        return x0, y0, wide, tall

    def _atlas_compose(self, cols: int, rows: int) -> dict:
        """The chart's ground and isobars for a window of so many cells. Kept until something it shows changes."""
        view, state, pixels = self.atlas_view, self.state, self.map.pixels
        x0, y0, wide, tall = self._atlas_window()
        key = (cols, rows, x0, y0, wide, self.chart_version, len(self.chart), state.day, state.minute,
               view["isobars"], pixels)
        if self._atlas_made is not None and self._atlas_made["key"] == key:
            return self._atlas_made
        world, chart, known = self.world, self.chart, self.known
        elevation, biomes = world.chart_elevation, world.chart_biome
        close = view["zoom"] > 0                # zoomed in: the relief of the map itself, a sample to a pixel
        tiles: dict = {}
        n = TILE_N
        pressure = self.weather.frame(state.day, state.minute).pressure if view["isobars"] else None
        down = rows * 2 if pixels else rows
        grid, above = [], None
        for py in range(down):
            wy = y0 + (py + 0.5) / down * tall
            gy = min(CHART_H - 1, int(wy / CHART_KM)) * CHART_W
            sy = int(wy // SAMPLE_KM)
            row, levels, left = [], [], None
            for px in range(cols):
                wx = x0 + (px + 0.5) / cols * wide
                cell = gy + min(CHART_W - 1, int(wx / CHART_KM))
                seen = cell in chart or cell in known
                line = False
                if pressure is not None:
                    # An isobar runs where the pressure passes a multiple of four hPa between two neighbours.
                    level = isobar_level(pressure(wx, wy))
                    line = (left is not None and level != left) or (above is not None and level != above[px])
                    levels.append(level)
                    left = level
                if not pixels:
                    biome = biomes[cell]
                    row.append(("-", "cool_dim") if line else (_MAP_GLYPHS[biome], _MAP_ROLES[biome] if seen else "dim"))
                    continue
                if close:
                    sx = int(wx // SAMPLE_KM)
                    tile = tiles.get((sx // n, sy // n))
                    if tile is None:
                        tile = tiles[(sx // n, sy // n)] = self.map._tile(sx // n, sy // n)
                    c = tile[(sy % n) * n + sx % n]
                else:
                    c = terrain_rgb(elevation[cell], biomes[cell])
                if not seen:
                    c = sketch_rgb(c)
                if line:
                    c = _mix(c, _ISOBAR_RGB, 0.5 if seen else 0.3)
                    c = (int(c[0]), int(c[1]), int(c[2]))
                row.append(c)
            grid.append(row)
            above = levels if pressure is not None else None
        self._atlas_made = {"key": key, "grid": grid, "window": (x0, y0, wide, tall)}
        return self._atlas_made

    def _atlas_places(self) -> list[tuple[str, float, float]]:
        """Everything on the chart with a name, for finding: towns, stations, features found, the stranded, the Gyre."""
        out = [(town["name"], town["x"], town["y"]) for town in self.world.towns]
        out += [(station["name"], station["x"], station["y"]) for station in self.stations]
        for fid, entry in sorted(self.found.items()):
            group = self.world.features[fid]["cells"]
            middle = group[len(group) // 2]
            out.append((entry[0], (middle % CHART_W + 0.5) * CHART_KM, (middle // CHART_W + 0.5) * CHART_KM))
        out += [(clean_text(call["handle"], 16), call["x"], call["y"]) for call in self.calls]
        out += [(wind["name"], wind["x"], wind["y"]) for wind in self.winds if wind["id"] in self.almanac]
        out.append(("The Gyre", self.world.gyre["x"], self.world.gyre["y"]))
        return out

    def _atlas_find(self, text: str) -> None:
        """Bring a named place to the middle of the chart. A name that begins so comes before one that holds it."""
        view, want = self.atlas_view, " ".join(text.lower().split())
        if not want:
            return
        places = self._atlas_places()

        def begins(name: str) -> bool:
            name = name.lower()
            return name.startswith(want) or (name.startswith("the ") and name[4:].startswith(want))
        # A name that begins so, and of those the shortest; then one that only holds it.
        hit = min((p for p in places if begins(p[0])), key=lambda p: len(p[0]), default=None) or \
            next((p for p in places if want in p[0].lower()), None)
        if hit is None:
            view["note"] = f"Nothing on the chart is called \"{clean_text(text, FIND_LIMIT)}\"."
            return
        view.update(x=hit[1], y=hit[2], zoom=max(view["zoom"], 1), found=hit, note=f"Found: {hit[0]}.")

    def _atlas_key(self, k: str, key: str) -> None:
        view = self.atlas_view
        if view["find"] is not None:              # a name is being typed: every printable key is part of it
            if k == "ESC":
                view["find"] = None
            elif k == "ENTER":
                text, view["find"] = view["find"], None
                self._atlas_find(text)
            elif k in ("BACKSPACE", "DELETE"):
                view["find"] = view["find"][:-1]
            elif len(key) == 1 and key.isprintable() and len(view["find"]) < FIND_LIMIT:
                view["find"] += key
            return
        view["note"] = ""
        if k in ("LEFT", "RIGHT", "UP", "DOWN") and view["zoom"] == 0:
            view["note"] = "This is the whole continent. Z comes closer; then the arrows move about."
        elif k in ("LEFT", "RIGHT", "UP", "DOWN"):
            # A third of the window at a time, and never past the edge of the world.
            x0, y0, wide, tall = self._atlas_window()
            cx, cy = x0 + wide / 2, y0 + tall / 2
            cx += {"LEFT": -1, "RIGHT": 1}.get(k, 0) * wide / 3
            cy += {"UP": -1, "DOWN": 1}.get(k, 0) * tall / 3
            view["x"] = min(max(cx, wide / 2), WORLD_W - wide / 2)
            view["y"] = min(max(cy, tall / 2), WORLD_H - tall / 2)
        elif k == "z":
            if view["zoom"] == 0:
                view["x"], view["y"] = self.state.x, self.state.y      # the first step in is on the pilot
            view["zoom"] = (view["zoom"] + 1) % len(ATLAS_ZOOMS)
        elif k == "-":
            view["zoom"] = max(0, view["zoom"] - 1)              # a step back out
        elif k == "i":
            view["isobars"] = not view["isobars"]
        elif k == "n":
            view["names"] = not view["names"]
        elif k == "/":
            view["find"] = ""
        elif k in ("ENTER", "ESC", "b", "c", " "):
            self.atlas = False
            view.update(find=None, found=None, note="")         # what was looked for is not marked for ever

    def _draw_atlas(self, s: "Session") -> list[str]:
        """The chart of the whole continent: what is known, the weather over it, and whose work it is."""
        wide, screen, view, state = s.width - 2, s.frame, self.atlas_view, self.state
        panel = ATLAS_PANEL if s.width >= ATLAS_PANEL_FROM else 0
        cols, rows = wide - panel - (1 if panel else 0), s.height - 5
        pixels, utf8 = self.map.pixels, self.launch.charset == "utf-8"
        made = self._atlas_compose(cols, rows)
        x0, y0, vw, vh = made["window"]
        grid = made["grid"]
        if pixels:
            for r in range(rows):
                screen.pixel_row(1, 1 + r, grid[r * 2], grid[r * 2 + 1])
        else:
            for r in range(rows):
                for c, (glyph, ink) in enumerate(grid[r]):
                    screen.put(1 + c, 1 + r, glyph, ink)
        zoom = ATLAS_ZOOMS[view["zoom"]]
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._line(0, 13, "The chart", "text")]
        head = f"Day `{state.day}`  `{clock_text(state.minute)}`"
        if zoom > 1:
            head += f"   `{zoom}` times closer"
        if view["isobars"] and s.width >= 110:
            head += f"   isobars every `{ISOBAR_HPA:.0f}` hPa"
        out.append(s._kv(0, 24, head, max(0, s.width - 49)))
        share = self.charted_share()
        out.append(s._kv_right(0, s.width - 1, f"`{share * 100:.0f}`% of the land charted"))

        taken: set = set()

        def cell_of(x: float, y: float):
            c, r = int((x - x0) / vw * cols), int((y - y0) / vh * rows)
            return (c, r) if 0 <= c < cols and 0 <= r < rows and x >= x0 and y >= y0 else None

        def put(c: int, r: int, glyph: str, ink, darken: float = 0.7) -> None:
            taken.add((c, r))
            if pixels:
                screen.over(1 + c, 1 + r, glyph, ink, darken)
            else:
                screen.put(1 + c, 1 + r, glyph, ink)

        def mark(x: float, y: float, glyph: str, ink, force: bool = False, darken: float = 0.7):
            cell = cell_of(x, y)
            if cell is None or (cell in taken and not force):
                return None
            put(cell[0], cell[1], glyph, ink, darken)
            return cell

        def label(cell, text: str, ink) -> bool:
            """A name beside its mark: to the right if there is room, else to the left, else not at all."""
            if cell is None or not text:
                return False
            c, r = cell
            # Beside the mark with a space between, else just above or below it. A clear cell at either end
            # of the name, so that names do not run together.
            for start, row in ((c + 2, r), (c - len(text) - 1, r), (c - len(text) // 2, r - 1),
                               (c - len(text) // 2, r + 1)):
                if start >= 0 and start + len(text) <= cols and 0 <= row < rows and not any(
                        (start + i, row) in taken for i in range(-1, len(text) + 1) if (start + i, row) != (c, r)):
                    for i, ch in enumerate(text):
                        put(start + i, row, ch, ink)
                    return True
            return False

        # The pilot first, then whoever needs help, then the places: nothing may hide the first two.
        mark(state.x, state.y, "@", "bright", force=True)
        found = view.get("found")
        if found is not None:                   # what was looked for, before anything can crowd it out
            # (Without UTF-8 the route's dots are stars: there the found place is an X.)
            label(mark(found[1], found[2], "*" if utf8 else "X", "bright"), found[0], "bright")
        waiting = [(mark(call["x"], call["y"], "!", "warn"), call) for call in self.calls]
        targets = self.targets
        towns = [(mark(t["x"], t["y"], "#" if ("town", i) in targets else "o",
                       "accent" if ("town", i) in targets else "text"), t, "text") for i, t in enumerate(self.world.towns)]
        stations = []
        for i, station in enumerate(self.stations):
            glyph, ink = self._station_mark(station)
            wanted = ("station", i) in targets
            stations.append((mark(station["x"], station["y"], "#" if wanted else glyph, "accent" if wanted else ink),
                             station, ink))
        for ox, oy, ink in self.others:
            mark(ox, oy, "Q", ink)
        dot = TRACK_DOT if utf8 else TRACK_PLAIN[0]
        for cell, call in waiting:
            label(cell, f"{clean_text(call['handle'], 12)}: {need_text(call['fuel'], call['repair'])}", "warn")
        gyre = self.world.gyre
        label(mark(gyre["x"], gyre["y"], "&" if not utf8 else "◎", "warn"), "THE GYRE", "warn")
        if view["isobars"]:
            frame = self.weather.frame(state.day, state.minute)
            for sx, sy, _, strength, turn in frame.systems:
                if (sx - gyre["x"]) ** 2 + (sy - gyre["y"]) ** 2 < 100.0:
                    continue                        # the Gyre has its own name
                label(mark(sx, sy, "L" if turn > 0 else "H", "warm" if turn > 0 else "cool"),
                      f"{frame.pressure(sx, sy):.0f}", "text")
        for wind in self.blowing():
            cell = mark(wind["x"], wind["y"], "\u2248" if utf8 else "=", "cool")
            if view["names"]:
                label(cell, wind["name"], "cool")
        if view["names"]:
            bound = [self.place_of(ref) for ref in targets]
            # Where the pilot is bound first: if only some names fit, those are the ones.
            for cell, place, ink in sorted(towns + stations, key=lambda item: not any(item[1] is b for b in bound)):
                label(cell, place["name"], "accent" if any(place is b for b in bound) else ink)
            shown = 0
            for fid, entry in sorted(self.found.items()):
                if shown >= FEATURE_LABELS * zoom:
                    break
                group = self.world.features[fid]["cells"]
                middle = group[len(group) // 2]
                cell = cell_of((middle % CHART_W + 0.5) * CHART_KM, (middle // CHART_W + 0.5) * CHART_KM)
                if cell is not None and cell not in taken and label((cell[0] - 2, cell[1]), entry[0], "dim"):
                    shown += 1

        # The Gyre's bands, where nothing else is: inside the inner ring nothing is forecast or planted; the
        # small ring is the wall.
        for km, glyph, ink in ((GYRE_INNER_KM, dot, "text"), (GYRE_WALL_KM, dot, "warn")):
            for step in range(72):
                angle = step * math.pi / 36         # lightly: the ground still shows through a ring
                mark(gyre["x"] + km * math.cos(angle), gyre["y"] + km * math.sin(angle), glyph, ink, darken=0.25)
        # The route so far, last of all and only where nothing else is: where each flight ended, joined up,
        # and on to where the balloon is now. A dot never takes the place of a mark or a name.
        legs = [tuple(point) for point in self.route] + [(state.x, state.y)]
        for (ax, ay), (bx, by) in zip(legs, legs[1:]):
            steps = max(1, int(max(abs(bx - ax) / vw * cols, abs(by - ay) / vh * rows)))
            for i in range(1, steps):
                mark(ax + (bx - ax) * i / steps, ay + (by - ay) * i / steps, dot, "accent")
        named = sum(1 for entry in self.found.values() if entry)
        masts = sum(1 for station in self.stations if station.get("mast_day") is not None)
        board = []
        if self.store is not None:
            try:
                board = self.store.charters()
            except sqlite3.Error:
                board = []
        if panel:
            col = cols + 3
            width = s.width - 1 - col
            s._line(1, col, "THE CONTINENT", "accent")
            filled = round(share * 24)
            for i in range(24):
                screen.put(col + i, 2, "█" if i < filled else "·", (120, 214, 160) if i < filled else "dim")
            s._kv(2, col + 25, f"`{share * 100:.0f}`%")
            s._kv(3, col, f"`{named}` of `{len(self.world.features)}` features named", width)
            s._kv(4, col, f"`{len(self.stations)}` stations, `{masts}` of them masts", width)
            s._kv(5, col, f"You were first over `{self.charted}` cells", width)
            s._line(7, col, "RECORDS", "accent")
            s._line(7, col + 9, "most land charted", "dim")
            for i, (name, count) in enumerate(board[:5]):
                s._kv(8 + i, col, f"{clean_text(name, 20).replace('`', ' '):<22}`{count}`", width)
            if not board:
                s._line(8, col, "Nobody has charted anything yet.", "dim")
            row = 14
            s._line(row, col, "STRANDED", "warn" if self.calls else "accent")
            for i, call in enumerate(self.calls[:3]):
                dx, dy = call["x"] - state.x, call["y"] - state.y
                s._line(row + 1 + i, col, f"{clean_text(call['handle'], 12)}: {need_text(call['fuel'], call['repair'])}, "
                                          f"{math.sqrt(dx * dx + dy * dy):.0f} km {bearing(dx, dy)}"[:width], "text")
            if not self.calls:
                s._line(row + 1, col, "Nobody is waiting for help.", "dim")
            row = max(row + 6, s.height - 11)
            if row + 5 < s.height - 4:
                s._line(row, col, "KEY", "accent")
                for i, line in enumerate(("`@` you   `Q` a pilot   `!` stranded", "`o` town   `+` station   `Y` mast",
                                          "`#` post or an office to make for", f"`{dot}` your route so far",
                                          "`L` `H` a low, a high, in hPa")):
                    s._kv(row + 1 + i, col, line, width, "text", "cool")
        else:
            out.append(s._kv(s.height - 4, 1, f"You were first over `{self.charted}` cell{'' if self.charted == 1 else 's'}"
                                              f" of land.   `{named}` of `{len(self.world.features)}` features are "
                                              f"found and named.", wide))
        if view["find"] is not None:
            out.append(s._line(s.height - 3, 1, f"Find: {view['find']}_"[:wide], "bright"))
        elif view["note"]:
            out.append(s._line(s.height - 3, 1, view["note"][:wide], "bright"))
        elif board and not panel:
            line = "Most cells charted: " + "   ".join(f"{clean_text(name, 16).replace('`', ' ')} `{count}`"
                                                   for name, count in board)
            out.append(s._kv(s.height - 3, 1, line, wide))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        if view["find"] is not None:
            out += s._keys(s.height - 1, [("Enter", "Find it"), ("Esc", "Leave it")])
        else:
            out += s._keys(s.height - 1, [("Arrows", "Pan"), ("Z", "In"), ("-", "Out"), ("I", "Isobars"),
                                          ("N", "Names"), ("/", "Find"), ("Enter", "Back")], 2)
        return out

    def _station_mark(self, station: dict) -> tuple[str, str]:
        """How a station is drawn: a mast, a station reporting, or one gone silent."""
        if station.get("mast_day") is not None:
            return "Y", "cool"
        return "+", "cool" if staffed(station) or stock_of(station, self.today) > 0 else "dim"

    def _draw_loft(self, s: "Session") -> list[str]:
        """The rigging loft: the licence, the gear and what more can be fitted, and the envelope's design."""
        wide = s.width - 2
        town = self.place_of(self.here())
        out = [s._line(0, 1, "FAIR WINDS", "accent"),
               s._line(0, 13, f"The rigging loft at {town['name']}"[:wide - 30], "text")]
        right = f"`{self.postmarks}` postmarks"
        out.append(s._kv_right(0, s.width - 1, right))
        grade, score = self.grade(), self.score()
        after = f"{GRADES[grade + 1][0]} at {GRADES[grade + 1][1]}" if grade + 1 < len(GRADES) else "the highest there is"
        out.append(s._line(1, 1, "LICENCE", "accent"))
        out.append(s._kv(1, 10, f"`{GRADES[grade][0]}`   `{score}` points; {after}", wide - 9))
        out.append(s._line(2, 1, "GEAR", "accent"))
        room = wide - (18 if s.width >= 100 and self.map.pixels else 0)
        for i, track in enumerate(TRACKS):
            tiers, have = UPGRADES[track]["tiers"], self.gear[track]
            label = TIER_NAMES[have] if len(tiers) > 2 else "fitted" if have else "-"
            out.append(s._kv(3 + i * 2, 1, f"`{i + 1}`  {UPGRADES[track]['name']:<12}`{label:<7}"
                                           f"{gear_text(track, tiers[have])}`", room, "text", "value"))
            if have + 1 >= len(tiers):
                out.append(s._line(4 + i * 2, 6, "nothing more to fit"[:room], "dim"))
                continue
            tier = tiers[have + 1]
            why = self.cannot_fit(track)
            label = TIER_NAMES[have + 1] if len(tiers) > 2 else "fit"
            out.append(s._line(4 + i * 2, 6, f"{'next':<10}{label:<7}{tier['price']:>3} pm  "
                                             f"{why or gear_text(track, tier)}"[:room - 5], "dim" if why else "cool"))
        row = 3 + len(TRACKS) * 2
        names = ", ".join(ENVELOPE_COLOURS[c][0] for c in self.design["colours"])
        out.append(s._line(row, 1, "ENVELOPE", "accent"))
        out.append(s._line(row, 11, f"{self.design['pattern']} in {names}"[:wide - 10], "text"))
        col = 11 + len(self.design["pattern"]) + 4
        for c in self.design["colours"]:                 # each name in its own colour, where the line has room
            name, rgb = ENVELOPE_COLOURS[c]
            if sum(rgb) < 330:
                rgb = _mix(rgb, (255, 255, 255), 0.4)      # a dark colour's name still has to be read on a dark screen
            if col + len(name) < s.width - 1:
                s.frame.text(col, row, name, (int(rgb[0]), int(rgb[1]), int(rgb[2])))
            col += len(name) + 2
        out += s._keys(row + 1, [("P", "Pattern"), ("Z", "1st"), ("X", "2nd"), ("C", "3rd"), ("V", "4th colour")])
        if s.width >= 100 and self.map.pixels:
            draw_balloon(s.frame, s.width - 9, 16, 7.0, flame=False, colours=design_rgb(self.design),
                         pattern=self.design["pattern"])
        if self.hub_note:
            out.append(s._line(row + 2, 1, self.hub_note[:wide], "bright"))
        if s.notice:
            out.append(s._line(s.height - 3, 1, s.notice[:wide], "warn"))
        elif not self.saved_ok:
            out.append(s._line(s.height - 3, 1, "This game cannot be saved here. Tell the SysOp. You can play on.", "warn"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        out += s._keys(s.height - 1, [(f"1-{len(TRACKS)}", "Fit the next"), ("Enter", "Back"), ("?", "Help"),
                                      ("Q", "Quit")])
        return out

    def _draw_hub(self, s: "Session") -> list[str]:
        """The post office: what is on offer, what is aboard, and the stores."""
        state, ref, wide = self.state, self.here(), s.width - 2
        place, town = self.place_of(ref), ref[0] == "town"
        out = [s._line(0, 1, "FAIR WINDS", "accent"),
               s._line(0, 13, f"{place['name']}, a "
                              f"{'town' if town else 'mooring mast' if place.get('mast_day') is not None else 'weather station'}"
                       [:wide - 44], "text")]
        right = f"Day `{state.day}`  `{clock_text(state.minute)}`   `{self.postmarks}` postmarks"
        out.append(s._kv_right(0, s.width - 1, right, 13))
        # Where there are rows to spare and pixels to paint with, the place itself looks in over the counter.
        vista = min(14, s.height - 24) if self.map.pixels else 0
        if vista >= 4:
            key = (ref, s.width, vista, state.minute // 60, s.launch.color_depth)
            if self._vista is None or self._vista[0] != key:
                frame = self.weather.frame(state.day, state.minute)
                self._vista = (key, Backdrop(s.width, vista, state.minute / 60.0, frame.cloud(state.x, state.y)[0],
                                             self.world.sample(state.x, state.y)[1], ref[1] * 7919 + 17, True,
                                             s.launch.color_depth != "truecolor"))
            self._vista[1].draw(s.frame, 1)
        else:
            vista = 0
        row = 1 + vista
        out.append(s._line(row, 1, "THE POST OFFICE", "accent"))
        load = f"Aboard `{self.weight()}` of `{self.lift()}` weight"
        out.append(s._kv_right(row, s.width - 1, load))
        offers = self.offers()
        for i, offer in enumerate(offers):
            there = self.place_of(tuple(offer["dest"]))
            dx, dy = there["x"] - state.x, there["y"] - state.y
            mine = self.held(offer) is not None
            gone = not mine and offer["contract"] in self.taken
            text = (f"{i + 1}  {CARGO_KINDS[offer['kind']]['name']:<12}{there['name'][:18]:<19}"
                    f"{math.sqrt(dx * dx + dy * dy):>4.0f} km {bearing(dx, dy):<4}wt {offer['weight']}"
                    f"{offer['payout']:>5} pm  by Day {offer['due']}")
            text += "  aboard" if mine else "  carried" if gone else ""
            out.append(s._line(row + 1 + i, 1, text[:wide], "accent" if mine else "dim" if gone else "text"))
        row += 1 + max(len(offers), 1)
        if not offers:
            out.append(s._line(row - 1, 1, "Nothing on offer today.", "dim"))
        if self.relief_due():
            out.append(s._line(row, 1, "R  Relief run: the postmaster fuels and patches you on account, with a mailbag to carry."[:wide],
                               "warn"))
            row += 1
        out.append(s._line(row + 1, 1, "ABOARD", "accent"))
        going = self.forwardable()
        if going:
            out.append(s._line(row + 1, 9, f"W sends on by road what is bound elsewhere: "
                                           f"{sum(pay for _, pay in going)} postmarks"[:wide - 8], "cool"))
        lines = [self.cargo_text(item) + ("" if state.day <= item["due"] else " (late)") for item in self.manifest]
        freight = (["A station kit"] if self.kits else []) + \
            ([f"{self.crates} crate{'s' if self.crates != 1 else ''} of mast freight"] if self.crates else [])
        if freight:
            lines.insert(0, " and ".join(freight).replace(" and A ", " and a ") + ", for the wild")
        shown = lines[:3] if len(lines) <= 3 else lines[:2] + [f"and {len(lines) - 2} more"]
        for i, line in enumerate(shown or ["Nothing."]):
            out.append(s._line(row + 2 + i, 1, line[:wide], "text" if lines else "dim"))
        row += 2 + max(len(shown), 1)
        out.append(s._line(row + 1, 1, "STORES", "accent"))
        out.append(s._kv(row + 1, 9, f"Fuel `{state.fuel}`/{self.tank()}   Sacks `{state.ballast}`/{BALLAST_START}   "
                                     f"Pibals `{state.pibals}`/{PIBALS_START}   Wear `{state.wear}`"
                                     f"{'  too worn to fly' if state.grounded else ''}", wide - 8))
        fuel, sack, pibal, mend = (price_of(item, ref) for item in ("fuel", "sack", "pibal", "mend"))
        out += s._keys(row + 2, [("F", f"10 fuel, {10 * fuel}"), ("A", "Fill"), ("S", f"Sack, {sack}"),
                                 ("P", f"Pibal, {pibal}"), ("M", f"Mend, {mend} a point")])
        if town:
            out += s._keys(row + 3, [("K", "Station kit back" if self.kits else f"Station kit, {KIT_PRICE}"),
                                     ("T", f"Mast crate, {CRATE_PRICE}")] + ([("Y", "Crate back")] if self.crates else []))
            row += 1
        if self.hub_note:
            out.append(s._line(row + 4, 1, self.hub_note[:wide], "bright"))
        if s.notice:
            out.append(s._line(s.height - 3, 1, s.notice[:wide], "warn"))
        elif not self.saved_ok:
            out.append(s._line(s.height - 3, 1, "This game cannot be saved here. Tell the SysOp. You can play on.", "warn"))
        out.append(s._kv(s.height - 2, 1, self._status(s, wide), wide, "dim", "text"))
        keys = [(f"1-{len(offers)}" if len(offers) > 1 else "1", "Take or put back")] if offers else []
        keys += [("G", "Rigging loft")] if self.at_loft() else []
        out += s._keys(s.height - 1, keys + [("Enter", "Back"), ("?", "Help"), ("Q", "Quit")])
        return out

    def draw(self, s: "Session") -> list[str]:
        if self.busy is not None or self.ended:
            return self._draw_lease(s)
        if self.atlas and not self.naming:
            return self._draw_atlas(s)
        if self.book is not None:
            if self.book_place() == self.book["station"]:
                return self._draw_book(s)
            self.book = self.writing = None
        if self.pages and not self.naming:
            return self._draw_almanac(s)
        if self.album_open and not self.naming:
            return self._draw_album(s)
        if self.season_open and not self.naming:
            return self._draw_season(s)
        if self.drift_lobby and not self.naming:
            return self._draw_drift(s)
        if self.trials and not self.naming:
            return self._draw_trials(s)
        if self.hub and self.at_office():
            return self._draw_hub(s)
        if self.loft and self.at_loft():
            return self._draw_loft(s)
        self.hub = self.loft = False
        state, launch = self.state, self.launch
        phase = self.phase()
        # The layout follows the terminal: a bigger window is a bigger map.
        # At 80 x 24 the map is 52 x 16 with the panel beside it and seven rows below.
        self.tier = layout_tier(s.width, s.height)
        panel = PANEL_SMALL if self.tier == "small" else PANEL_WIDE
        self.map_cols, self.map_rows = s.width - panel - 2, s.height - 8
        self.panel_col, base, wide = s.width - panel + 1, s.height - 7, s.width - 2
        # A trial's weather is a day of its own, far from the calendar's: only its hour is the caller's business.
        when = f"`{clock_text(state.minute)}`" if self.drift is not None and self.drift.get("kind") == "trial" \
            else f"Day `{state.day}`  `{clock_text(state.minute)}`"
        out = [s._line(0, 1, "FAIR WINDS", "accent"), s._kv(0, 13, when)]
        distance, toward, nearest = self.bearings()[0]
        post = f"{'Nearest post' if self.manifest else 'Nearest office'}: `{nearest['name']}, {distance:.0f} km {toward}`"
        if self.drift is not None:
            left = max(0, self.drift.get("turns", DRIFT_TURNS) - (state.turn - self.drift.get("from", DRIFT_START_TURN)))
            title = self.drift.get("title", "The Drift")[:16]
            post = f"{title}: `{left}` turns left. Mark `{distance:.0f} km {toward}`"
        out.append(s._kv_right(0, s.width - 1, post, 32))
        out += self._draw_map(s)
        out += self._draw_panel(s, phase)
        for i, line in enumerate(self.log[-3:]):
            out.append(s._line(base + i, 1, line[:wide], "dim" if i < len(self.log[-3:]) - 1 else "text"))
        if s.notice:
            out.append(s._line(base + 3, 1, s.notice[:wide], "warn"))
        elif not self.saved_ok:
            out.append(s._line(base + 3, 1, "This game cannot be saved here. Tell the SysOp. You can play on.", "warn"))
        elif launch.guest:
            out.append(s._line(base + 3, 1, "You are visiting: nothing is kept when you leave.", "dim"))
        sentence, role = self._sentence(phase)
        out.append(s._line(base + 4, 1, sentence[:wide], role))
        out.append(s._kv(base + 5, 1, self._status(s, wide), wide, "dim", "text"))
        keys, gap = self._fit_keys(self._key_help(phase), wide)
        out += s._keys(base + 6, keys, gap)
        return out

    @staticmethod
    def _fit_keys(keys: list, wide: int) -> tuple[list, int]:
        """The key line, made to fit, and the spaces between its keys.

        In order: a shorter word for leaving; shorter labels; two spaces
        between keys for three; then keys go, the ones the help names and a
        pilot can best do without first. Jettison goes last of those: it is
        the way up when the fuel is gone. Enter and Q never go.
        """
        def width(row, gap):
            return sum(len(key) + len(label) + 3 + gap for key, label in row) - gap
        keys, gap = list(keys), 3
        if width(keys, gap) > wide:
            keys = [(key, "Leave" if key == "Q" and label.startswith("Return to") else label) for key, label in keys]
        if width(keys, gap) > wide:
            short = {"Fly it": "Fly", "Drop mail": "Drop", "Jettison": "Sack"}
            keys = [(key, short.get(label, label)) for key, label in keys]
        if width(keys, gap) > wide:
            gap = 2
        # N is only ever on the ground's line and J only on the air's, so each is the last to go where it is.
        for spare in ("?", "B", "P", "H", "O", "L", "A/D", "W/S", "M", "N", "J"):
            if width(keys, gap) <= wide:
                break
            keys = [(key, label) for key, label in keys if key != spare]
        return keys, gap

    def _key_help(self, phase: str):
        if self.naming:
            return [(f"1-{len(self.naming[0]['names'])}", "Name it"), ("Enter", "The first will do")]
        leave = ("Q", f"Return to {self.launch.node_name}")
        offered = self.offered(self.state)
        if phase == "aloft":
            keys = [("W/S", "Layer")]
            if "left" in offered:
                keys.append(("A/D", "Rope"))
            keys.append(("Enter", "Fly it"))
            if self.droppable():
                keys.append(("M", "Drop mail"))
            if "land" in offered:
                keys.append(("L", "Land"))
            if "jettison" in offered:
                keys.append(("J", "Jettison"))
            if "pibal" in offered:
                keys.append(("P", "Pibal"))
            if self.drift is not None:
                keys.append(("X", "Leave"))
            if self.wonder_here() is not None:
                keys.insert(0, ("F", "Wonder"))       # first on the line: it may not be there next turn
            return keys + [("?", "Help"), ("Q", "Quit")]
        if self.drift is not None:
            return [("Enter", "Launch"), ("X", "Leave it"), ("?", "The rules"), leave]
        office = [("O", "Post")] if self.at_office() else []      # the loft is a door inside it, and G from here
        if self.kits and not self.cannot_plant():
            office = [("K", "Plant station")]                     # in the wild: there is no office here
        if self.book_place() is not None:
            office = office + [("B", "Book")]
        if phase == "ground":
            wait = [("H", "Wait")] if self.state.turn < DAY_TURNS else []
            # A pilot who has not flown the first flight is shown the way to it; after that it is in the help.
            learn = [("R", "Learn")] if not self.taught else []
            return [("Enter", "Launch")] + learn + office + wait + [("N", "Night"), ("?", "Help"), leave]
        if phase == "stranded":
            return [("W", "Walk out")] + office + [("?", "Help"), leave]
        if phase == "ditched":
            return [("N", "Wait for the boat"), ("?", "Help"), leave]
        if phase == "wait":
            return office + [("?", "Help"), leave]
        return office + [("N", "Next day"), ("?", "Help"), leave]

    def _sentence(self, phase: str) -> tuple[str, str]:
        state = self.state
        if phase == "stranded":
            why = "The balloon is too worn to fly" if state.grounded else "Stranded with no fuel"
            return f"{why}. Walking out costs two days, or wait for help.", "warn"
        if phase == "ditched":
            return "Adrift. The packet boat will take a day to bring you to port.", "warn"
        if self.naming:
            offer = self.naming[0]
            names = "   ".join(f"{i + 1} {name}" for i, name in enumerate(offer["names"]))
            return f"Name the {offer['kind']}:   {names}", "accent"
        if phase == "wait":
            opens = when_text(calendar_opens(self.calendar, state.day), self._local(), self.launch.datetime_format)
            return f"Every day that has opened is flown. Day {state.day} opens {opens}: come back then.", "accent"
        if phase == "rest":
            if state.grounded:
                return "The balloon is too worn to fly. The stores at the post office mend it: O.", "warn"
            if state.fuel < COST_LAUNCH and state.turn < DAY_TURNS:
                return "No fuel to launch with. The stores at the post office sell it: O.", "warn"
            return "Nothing more to do today. Sleep, and fly on at dawn.", "text"
        choice = self.choice
        if choice == "land":
            kind = self.engine.landing_class(state)
            if kind == "ditch":
                return "Land here? This is water: you would ditch and lose the post.", "warn"
            wind = self.weather.frame(state.day, state.minute).surface_speed(state.x, state.y)
            return f"Land here: a {kind} landing in {wind:.0f} km/h on the ground. Enter to land.", \
                   "warn" if kind != "clean" else "accent"
        if choice is None:
            return "", "text"
        look = self.preview(choice)
        layer = look["layer"]
        verb = {"hold": "Hold in", "climb": "Climb to", "sink": "Sink to", "launch": "Launch into",
                "jettison": "Drop a sack for", "left": "Rope out, bear left in", "right": "Rope out, bear right in"}[choice]
        text = f"{verb} layer {layer} {LAYER_NAMES[layer - 1]}: {look['cost']} fuel."
        if look["points"]:
            ex, ey, _ = look["points"][-1]
            left, _, place = self.bearings(ex, ey)[0]
            minutes = len(look["points"]) * TURN_MINUTES
            text += f" In {minutes} min: {left:.0f} km from {place['name']}."
        stop = look["stop"]
        if stop == "ridge":
            return text + " High ground you cannot climb stops you.", "warn"
        if stop == "wall":
            return text + f" The Gyre's wall stops all below layer {GYRE_WALL_LAYER}.", "warn"
        if stop == "high_ground":
            return text + " You cannot pay for the climb ahead!", "warn"
        if stop == "fuel":
            return text + " Then the fuel is gone.", "warn"
        if look["forced"] or look["sacks"]:
            price = f"{look['forced']} fuel" if look["forced"] else f"{look['sacks']} sack"
            return text + f" High ground will force a climb: {price}.", "warn"
        return text, "accent"

    def shown_layer(self) -> int:
        """The layer whose wind the map shows: the one the pilot is choosing, else the one they are in."""
        state = self.state
        if self.choice in ("hold", "climb", "sink", "launch", "jettison") and self.choice in self.offered(state):
            return self.engine.preview(state, self.choice, 1)["layer"]
        return state.layer or self.engine.lowest_open(state.x, state.y)

    def _show_wind(self) -> bool:
        """Give the map the wind of the layer in view, as far as the pilot knows it. True if it changed."""
        state, layer = self.state, self.shown_layer()
        here = state.layer if state.status == "aloft" else 0
        low = self.engine.lowest_open(state.x, state.y)
        read = self.engine.reads(state, layer)
        if read:
            frame = self.weather.frame(state.day, state.minute)
        else:
            frame = self.weather.frame(state.day, state.minute, UNREAD_LEAD, self.coverage(state.x, state.y))
        return self.map.set_wind((state.day, state.minute, layer, read), lambda wx, wy: frame.wind(wx, wy, layer))

    def tick(self) -> None:
        """One frame of moving wind."""
        if self.frames_left > 0:
            self.frames_left -= 1
            self.map.advance()

    def _draw_map(self, s: "Session") -> list[str]:
        state, view, screen = self.state, self.map, s.frame
        view.draw(screen, 1, 1, self.map_cols, self.map_rows, state.x, state.y, state.day, state.minute,
                  self.is_charted, self.chart_version)
        if self._show_wind():
            if self.motion:
                self.frames_left = max(self.frames_left, LOOK_FRAMES)
            else:
                for _ in range(STREAK_TRAIL):     # no motion: a new still pattern for the new wind
                    view.advance()
        view.draw_wind(screen)
        # The balloon first: nothing may cover it, and names keep clear of it.
        view.mark(screen, state.x, state.y, "@", "bright", force=True)
        if self.drift is not None:                   # the mark, before anything can take its place
            spot = view.mark(screen, self.drift["course"]["mx"], self.drift["course"]["my"], "X", "accent")
            if spot is not None:
                view.label(screen, spot, "the mark", "accent")
        wanted, others, targets = [], [], self.targets
        utf8_map = self.launch.charset == "utf-8"
        for i, place in enumerate(self.world.towns):
            (wanted if ("town", i) in targets else others).append((place, "o", "text"))
        for i, place in enumerate(self.stations):
            (wanted if ("station", i) in targets else others).append((place,) + self._station_mark(place))
        cells = []
        for place, _, _ in wanted:
            cell = view.mark(screen, place["x"], place["y"], "#", "accent")
            cells.append((cell, place))
        planted = []
        for place, glyph, ink in others:
            cell = view.mark(screen, place["x"], place["y"], glyph, ink)
            if "id" in place:
                planted.append((cell, place, ink))
        # A stranded pilot before the other balloons: theirs is one of them, on the same spot.
        waiting = [(view.mark(screen, call["x"], call["y"], "!", "warn"), call) for call in self.calls]
        for ox, oy, colour in self.others:
            view.mark(screen, ox, oy, "Q", colour)      # another pilot's balloon, in the first colour of its envelope
        if state.status in ("aloft", "ground") and self.offered(state):
            order = [a for a in self.choices() if a != self.choice]
            if self.choice in ("sink", "hold", "climb", "launch", "jettison", "left", "right"):
                order.insert(0, self.choice)        # the chosen track first: where tracks share a cell, it shows
            utf8 = self.launch.charset == "utf-8"
            for action in order:
                look = self.preview(action)
                # The chosen track in its choice's colour; the others in the same colour, further off.
                ink = _TRACK_ROLES[action] + ("" if action == self.choice else "_dim")
                glyphs = track_glyphs(look["points"], (state.x, state.y), utf8)
                for (px, py, _), glyph in zip(look["points"], glyphs):
                    view.mark(screen, px, py, glyph, ink)
        for cell, place in cells:                    # names last, where there is room left for them
            if cell is not None:
                view.label(screen, cell, place["name"], "accent")
        for cell, place, ink in planted:             # what pilots planted is named: nobody was born knowing it
            if cell is not None:
                view.label(screen, cell, place["name"], ink)
        for wind in self.blowing():                  # a wind in the almanac, where and when it blows
            cell = view.mark(screen, wind["x"], wind["y"], "\u2248" if utf8_map else "=", "cool")
            if cell is not None:
                view.label(screen, cell, wind["name"], "cool")
        for cell, call in waiting:                   # a stranded pilot, and what they are waiting for
            if cell is not None:
                view.label(screen, cell, f"{clean_text(call['handle'], 12)}: {need_text(call['fuel'], call['repair'])}",
                           "warn")
        shown = 0
        for fid, entry in sorted(self.found.items()):
            if shown >= FEATURE_LABELS:
                break
            group = self.world.features[fid]["cells"]
            middle = group[len(group) // 2]
            fx, fy = (middle % CHART_W + 0.5) * CHART_KM, (middle // CHART_W + 0.5) * CHART_KM
            cell = view.cell_of(fx, fy)
            if cell is not None and cell not in view._taken and view.label(screen, cell, entry[0], "dim"):
                shown += 1                           # a name somebody gave, where the feature lies
        return []

    def layer_winds(self) -> dict:
        """For each layer here: (vx, vy, read) as the pilot knows it, or None where the ground fills it."""
        state = self.state
        frame = self.weather.frame(state.day, state.minute)
        guess = self.weather.frame(state.day, state.minute, UNREAD_LEAD, self.coverage(state.x, state.y))
        low = self.engine.lowest_open(state.x, state.y)
        here = state.layer if state.status == "aloft" else 0
        out = {}
        for layer in range(1, LAYERS + 1):
            if layer < low:
                out[layer] = None
                continue
            read = self.engine.reads(state, layer)
            vx, vy = (frame if read else guess).wind(state.x, state.y, layer)
            out[layer] = (vx, vy, read)
        return out

    def _draw_panel(self, s: "Session", phase: str) -> list[str]:
        if self.tier != "small":
            return self._draw_sounding(s, phase)
        state, col = self.state, self.panel_col
        offered = self.offered(state)
        frame = self.weather.frame(state.day, state.minute)
        cover = self.coverage(state.x, state.y)
        low, top = self.engine.lowest_open(state.x, state.y), self.engine.top_layer(state)
        here = state.layer if state.status == "aloft" else 0
        out = [s._line(1, col, "LAYER      WIND   FUEL", "dim")]
        picked = None
        if self.choice in ("hold", "climb", "sink", "launch", "jettison"):
            picked = self.preview(self.choice)["layer"]
        costs = {}
        for action in ("sink", "hold", "climb", "launch"):
            if action in offered:
                costs[self.engine.preview(state, action, 1)["layer"]] = offered[action]
        row = 2
        for layer in range(top, 0, -1):
            mark = ">" if layer == here else "*" if layer == picked else " "
            if layer < low:
                wind = "ground"
            else:
                read = self.engine.reads(state, layer)
                if read:
                    vx, vy = frame.wind(state.x, state.y, layer)
                else:
                    vx, vy = self.weather.frame(state.day, state.minute, UNREAD_LEAD, cover).wind(state.x, state.y, layer)
                speed = math.sqrt(vx * vx + vy * vy)
                wind = f"{' ' if read else '~'}{compass(vx, vy):>3} {speed:>3.0f}"
            cost = f"{costs[layer]:>3}" if layer in costs else "   "
            role = "bright" if layer == here else "accent" if layer == picked else "text"
            out.append(s._line(row, col, f"{mark}{layer} {LAYER_NAMES[layer - 1]:<7}{wind:<9}{cost}", role))
            row += 1
        out.append(s._line(row, col, "~ forecast, not read", "dim"))
        row += 2
        out.append(s._kv(row, col, f"Fuel `{state.fuel:>3}`   Sacks `{state.ballast}`"))
        out.append(s._kv(row + 1, col, f"Pibals `{state.pibals}`   Wear `{state.wear}`"))
        if state.pibal_until >= state.turn:
            out.append(s._line(row + 2, col, f"Pibal read: {state.pibal_until - state.turn + 1} turns", "cool"))
        left = max(0, DUSK_MINUTE - state.minute)
        light = f"Dusk in `{left // 60}h{left % 60:02d}`" if left else "`Night`"
        out.append(s._kv(row + 3, col, light, None, "label", "value" if left > 90 else "warn"))
        # The ground wind is the lowest layer's wind, known only as well as that layer is.
        lowest = self.layer_winds()[low]
        ground = math.sqrt(lowest[0] * lowest[0] + lowest[1] * lowest[1])
        out.append(s._kv(row + 4, col, f"Ground wind `{'' if lowest[2] else '~'}{ground:.0f}` km/h"))
        cloud, _ = frame.cloud(state.x, state.y)
        sky = "clear" if cloud < 0.2 else "broken cloud" if cloud < 0.6 else "overcast"
        if frame.fog(state.x, state.y) > FOG_THRESHOLD:
            sky = "fog on the ground"
        out.append(s._kv(row + 5, col, f"Sky `{sky}`"))
        out.append(s._kv(row + 6, col, f"Forecast `{'sharp' if cover > 0.66 else 'fair' if cover > 0.33 else 'vague'}`"))
        for i, (distance, toward, place) in enumerate(self.bearings()[:POST_CHOICES]):
            out.append(s._line(14 + i, col, f"# {place['name'][:14]:<14}{distance:>4.0f} {toward}", "accent" if i == 0 else "text"))
        return out

    def _draw_sounding(self, s: "Session", phase: str) -> list[str]:
        """The panel from 132 x 40 up: a slice of sky with the balloon in it, and the wind in every layer."""
        state, col, screen = self.state, self.panel_col, s.frame
        offered = self.offered(state)
        frame = self.weather.frame(state.day, state.minute)
        low, top = self.engine.lowest_open(state.x, state.y), self.engine.top_layer(state)
        here = state.layer if state.status == "aloft" else 0
        winds = self.layer_winds()
        picked = None
        if self.choice in ("hold", "climb", "sink", "launch", "jettison") and self.choice in offered:
            picked = self.preview(self.choice)["layer"]
        costs = {}
        for action in ("sink", "hold", "climb", "launch"):
            if action in offered:
                costs[self.engine.preview(state, action, 1)["layer"]] = offered[action]
        if here and offered:                        # with nothing on offer at all, fuel is not the reason
            for layer in (here - 1, here, here + 1):
                if low <= layer <= top and layer not in costs:
                    costs[layer] = None             # a step away, but not on offer: it cannot be paid for
        cover, deck = frame.cloud(state.x, state.y)
        s._line(1, col, "SOUNDING", "accent")
        s._line(1, col + 9, "the wind above you", "dim")

        # The sky: four pixels to a layer, the jet at the top. Ground fills
        # the layers it reaches, the cloud deck sits in its own, and the
        # balloon hangs where it is.
        first = 2
        if self.map.pixels:
            for py in range(LAYERS * 4):
                layer = LAYERS - py // 4
                t = py / (LAYERS * 4 - 1) * (len(_SKY_RAMP) - 1)
                i = min(len(_SKY_RAMP) - 2, int(t))
                sky = _mix(_SKY_RAMP[i], _SKY_RAMP[i + 1], t - i)
                if py % 4 == 0:
                    sky = _mix(sky, (10, 16, 40), 0.22)             # a faint line where one layer ends
                for px in range(SKY_COLS):
                    c = sky
                    if layer < low:
                        c = (98, 150, 92) if (py % 4 == 0 and layer == low - 1) else (58, 104, 72)
                    elif layer == deck and cover > 0.2 and (px + py) % 3 != 0 and py % 4 in (1, 2):
                        c = _mix(sky, (248, 250, 253), min(0.9, 0.4 + cover * 0.5))
                    screen.pixel(col + px, first * 2 + py, c)
            if here:
                by = first * 2 + (LAYERS - here) * 4
                for dx, dy, c in ((3, 0, (244, 112, 92)), (4, 0, (248, 236, 206)), (2, 1, (244, 112, 92)),
                                  (3, 1, (248, 236, 206)), (4, 1, (244, 112, 92)), (5, 1, (248, 236, 206)),
                                  (3, 2, (244, 112, 92)), (4, 2, (248, 236, 206)), (3, 3, (86, 58, 40)),
                                  (4, 3, (86, 58, 40))):
                    screen.pixel(col + dx, by + dy, c)
        text = col + (SKY_COLS + 1 if self.map.pixels else 0)
        for layer in range(LAYERS, 0, -1):
            row = first + (LAYERS - layer) * 2
            mark = ">" if layer == here else "*" if layer == picked else " "
            ink = "bright" if layer == here else "accent" if layer == picked else "text" if layer <= top else "dim"
            wind = winds[layer]
            if wind is None:
                s._line(row, text, f"{mark}{layer} {LAYER_NAMES[layer - 1]:<8}ground", "dim")
                continue
            vx, vy, read = wind
            speed = math.sqrt(vx * vx + vy * vy)
            cost = "   " if layer not in costs else " --" if costs[layer] is None else f"{costs[layer]:>3}"
            point = compass(vx, vy)
            s._line(row, text, f"{mark}{layer} {LAYER_NAMES[layer - 1]:<8}{' ' if read else '~'}"
                               f"{'--' if point == 'calm' else point:>3}{speed:>4.0f}{cost}", ink)
            # One scale for every layer, so bars can be compared: full is 100 km/h.
            filled = max(1, min(10, round(speed / BAR_FULL_KMH * 10)))
            t = min(1.0, speed / 100.0) * (len(_BAR_RAMP) - 1)
            i = min(len(_BAR_RAMP) - 2, int(t))
            bar = _mix(_BAR_RAMP[i], _BAR_RAMP[i + 1], t - i)
            for n in range(10):
                screen.put(text + 2 + n, row + 1, "\u2588" if n < filled else "\u00b7",
                           (bar if layer <= top else (70, 80, 100)) if n < filled else "dim")
            if layer > top:
                note = "needs oxygen" if layer == LAYERS and state.ceiling >= LAYERS - 1 else "out of reach"
            else:
                # Icing first: it costs fuel. The layer you are in says so too.
                hazard = "icing" if layer >= frame.icing_layer and not state.iceproof else \
                    "cloud deck" if layer == deck and cover > 0.2 else ""
                note = ", ".join(word for word in ("you" if layer == here else "", hazard) if word)
            s._line(row + 1, text + 13, note[:PANEL_WIDE - 15 - (SKY_COLS + 1 if self.map.pixels else 0)],
                    "accent" if layer == here else "dim")
        row = first + LAYERS * 2
        s._line(row, text, "~ forecast, not read", "dim")

        row += 2
        s._line(row, col, "STORES", "accent")

        def bar(r: int, label: str, value: int, most: int, ink, note: str) -> None:
            s._line(r, col, f"{label:<8}", "dim")
            filled = 0 if most <= 0 else max(0, min(14, round(value / most * 14)))
            for n in range(14):
                screen.put(col + 9 + n, r, "\u2588" if n < filled else "\u00b7", ink if n < filled else "dim")
            s._line(r, col + 24, note, "text")

        bar(row + 1, "Fuel", state.fuel, self.tank(), (255, 176, 84), f"{state.fuel}")
        s._line(row + 2, col, f"{'Ballast':<8}", "dim")
        for n in range(BALLAST_START):
            screen.put(col + 9 + n * 2, row + 2, "\u25a0" if n < state.ballast else "\u00b7",
                       (226, 206, 160) if n < state.ballast else "dim")
        s._line(row + 2, col + 24, f"{state.ballast} sacks", "text")
        s._line(row + 3, col, f"{'Pibals':<8}", "dim")
        for n in range(PIBALS_START):
            screen.put(col + 9 + n * 2, row + 3, "\u25a0" if n < state.pibals else "\u00b7",
                       "cool" if n < state.pibals else "dim")
        reading = state.pibal_until - state.turn + 1      # the same test layer_winds uses
        s._line(row + 3, col + 24, f"read: {reading} turns" if reading > 0 else f"{state.pibals} left",
                "cool" if reading > 0 else "text")
        bar(row + 4, "Wear", state.wear, WEAR_GROUNDED, "warn", f"{state.wear}")
        left = max(0, DUSK_MINUTE - state.minute)
        bar(row + 5, "Daylight", left, DUSK_MINUTE - DAWN_MINUTE, (255, 224, 140),
            f"{left // 60}h{left % 60:02d}" if left else "night")

        row += 7
        # The ground wind is the lowest layer's wind, known only as well as that layer is.
        # The landing is named when it can be made: from the lowest layer, which is then read.
        lowest = winds[low]
        ground = math.sqrt(lowest[0] * lowest[0] + lowest[1] * lowest[1])
        landing = self.engine.landing_class(state) if state.status == "aloft" and here == low else ""
        s._line(row, col, f"Ground wind {'' if lowest[2] else '~'}{ground:.0f} km/h"
                + (f": a {landing} landing" if landing and landing != "ditch" else ": water below" if landing else ""),
                "warn" if landing in ("hard", "ditch") else "text")
        sky = "clear" if cover < 0.2 else "broken cloud" if cover < 0.6 else "overcast"
        if frame.fog(state.x, state.y) > FOG_THRESHOLD:
            sky = "fog on the ground"
        reach = self.coverage(state.x, state.y)
        s._kv(row + 1, col, f"Sky `{sky}`. Forecast `{'sharp' if reach > 0.66 else 'fair' if reach > 0.33 else 'vague'}`.")

        row += 3
        s._line(row, col, "POST FOR", "accent")
        for i, (distance, toward, place) in enumerate(self.bearings()[:POST_CHOICES]):
            s._line(row + 1 + i, col, f"# {place['name'][:22]:<22}{distance:>5.0f} km {toward}",
                    "accent" if i == 0 else "text")

        row += 5
        if self.tier == "wide" and row + 4 < s.height - 7:
            # The wide tier has room to look ahead: the wind of the layer in view, hour by hour.
            layer = self.shown_layer()
            s._line(row, col, "FORECAST", "accent")
            s._line(row, col + 9, f"{LAYER_NAMES[layer - 1]} here, hour by hour", "dim")
            reach = self.coverage(state.x, state.y)
            for n in range(6):
                minute = state.minute + (n + 1) * 60
                if minute >= MINUTES_PER_DAY:
                    break
                vx, vy = self.weather.frame(state.day, minute, (n + 1) * 60.0, reach).wind(state.x, state.y, layer)
                x = col + n * 6
                s._line(row + 1, x, clock_text(minute), "dim")
                point = compass(vx, vy)
                s._line(row + 2, x, f"{'--' if point == 'calm' else point:<4}", "text")
                s._line(row + 3, x, f"{math.sqrt(vx * vx + vy * vy):<4.0f}", "text")
        return []


# The help page. A line that begins "# " is a heading. Between backticks is a key or a sign from the map,
# shown in the colour keys have everywhere.
PLAIN_HELP = (
    "# FLYING",
    "A balloon cannot steer. Each layer of air blows its own way: climb or sink",
    "to the one that carries you to a place marked `#`. A turn is 15 minutes.",
    "  `W` `S`  look at the layer above or below      `Enter`  fly the choice",
    "  `A` `D`  lowest layer: trail the rope left or right. Arrows and Space work too.",
    "  `J`  drop a sack: up a layer, no fuel        `P`  pibal: true winds, 4 turns",
    "  `L`  land, from the lowest layer             `M`  drop mail within 30 km",
    "# ON THE GROUND",
    "  `O`  post office and stores; `G` rigging loft, in towns; `B` a station's book",
    "  `H`  wait an hour for the wind               `N`  stay the night",
    "# THE MAP",
    "  `@` you  `#` post for there  `o` town  `+` station  `Y` mast  `Q` a pilot  `C` chart",
    "  Dots: a choice's track. `%` squall. `!` stranded: land to help. `F` a wonder.",
    "  Without colour: `~` sea  `.` shore  `,` grass  `\"` forest  `:` plain  `n` hills  `^` peak",
    "# GOOD TO KNOW",
    "  `~` marks a forecast; your own layer is read true. A world day is flown once.",
    "  Climbing costs fuel, sinking is free, holding 1 (0 from 11:00 to 15:00, 2",
    "  after 20:00). Land in 15 km/h or less. 12 km from a place is there.",
    "  `K` plants a station kit in the wild: a sharper forecast there for everyone.",
    "  Landing at a station restocks it: no fuel there. Crates make it a mast.",
    "  `T` Drift `R` Trials `V` winds `U` album `Y` season `Ctrl-L` redraw `Q` leave (saved)",
)


# --------------------------------------------------------------------------
# Scenes (S09): full-screen pictures for the moments that deserve one. A
# sky that follows the hour and the weather, ground that follows the land,
# and the balloon. A scene plays only after what it shows has been saved,
# never waits for a key (the splash excepted, which holds under a prompt),
# and ends on any key, which it uses up.
# --------------------------------------------------------------------------

SCENE_FRAME_SECONDS = 1 / 7
SCENE_FRAME_BYTES = 6000             # a scene's frame that costs more than this is shown less often
SCENE_STRIDE_MAX = 4
SCENE_GRACE_SECONDS = 0.3            # a key this soon after a scene begins was meant for what came before it
_TITLE_FONT = {
    "F": "11111 10000 10000 11110 10000 10000 10000", "A": "01110 10001 10001 11111 10001 10001 10001",
    "I": "11111 00100 00100 00100 00100 00100 11111", "R": "11110 10001 10001 11110 10100 10010 10001",
    "W": "10001 10001 10001 10101 10101 11011 10001", "N": "10001 11001 10101 10011 10001 10001 10001",
    "D": "11110 10001 10001 10001 10001 10001 11110", "S": "01111 10000 10000 01110 00001 00001 11110",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}
# Four stops from the top of the sky to the horizon, at key hours of the day.
_SKIES = (
    (0.0, ((6, 8, 24), (10, 14, 38), (16, 22, 52), (24, 32, 66))),
    (4.5, ((6, 8, 24), (10, 14, 38), (18, 24, 56), (30, 36, 72))),
    (6.2, ((34, 40, 96), (112, 80, 130), (236, 150, 110), (255, 220, 156))),
    (8.5, ((52, 112, 196), (92, 150, 220), (140, 188, 236), (184, 216, 244))),
    (17.0, ((52, 112, 196), (92, 150, 220), (140, 188, 236), (184, 216, 244))),
    (19.6, ((16, 16, 52), (64, 36, 100), (200, 92, 100), (252, 176, 96))),
    (21.3, ((8, 10, 30), (14, 18, 46), (30, 30, 70), (60, 44, 84))),
    (24.0, ((6, 8, 24), (10, 14, 38), (16, 22, 52), (24, 32, 66))),
)
BALLOON_COLOURS = ((44, 172, 172), (246, 232, 202), (240, 112, 92), (246, 232, 202))


def sky_stops(hour: float) -> tuple:
    """The sky's four colours, top to horizon, at an hour of the day."""
    hour = hour % 24.0
    for (h0, a), (h1, b) in zip(_SKIES, _SKIES[1:]):
        if h0 <= hour <= h1:
            t = (hour - h0) / (h1 - h0) if h1 > h0 else 0.0
            return tuple(_mix(a[i], b[i], t) for i in range(4))
    return _SKIES[0][1]


class Backdrop:
    """A sky and the land under it, painted once for a scene and kept."""

    def __init__(self, width: int, height: int, hour: float, cloud: float = 0.0, biome: int = GRASS,
                 seed: int = 1, place: bool = False, flat: bool = False):
        # flat: the ground in one tone. In 256 colours a fade to dark has no
        # greens to pass through, only greys and then black.
        self.w, self.h = width, height * 2            # in pixels: two to a cell
        self.horizon = int(self.h * 0.72)
        self.hour = hour % 24.0
        night = self.hour < 5.5 or self.hour >= 20.5
        stops = sky_stops(self.hour)
        w, h, hz = self.w, self.h, self.horizon
        seed32 = seed & 0xFFFFFFFF
        # The sun rides an arc from dawn to dusk; outside it there is a moon.
        day = (self.hour - 6.0) / 14.0
        up = 0.0 <= day <= 1.0
        sun_x = w * (0.12 + 0.76 * day) if up else w * 0.78
        sun_y = hz - (hz * 0.78) * (1.0 - (2.0 * day - 1.0) ** 2) - 2 if up else hz * 0.22
        rows = []
        for y in range(h):
            row = []
            t = min(1.0, y / hz) * 3
            i = min(2, int(t))
            base = _mix(stops[i], stops[i + 1], t - i)
            for x in range(w):
                c = base
                if y < hz:
                    d2 = (x - sun_x) ** 2 + ((y - sun_y) * 1.1) ** 2
                    if up:
                        glow = 0.45 * 220.0 / (220.0 + d2)
                        c = (c[0] + (255 - c[0]) * glow, c[1] + (236 - c[1]) * glow, c[2] + (190 - c[2]) * glow)
                        if d2 < (w * 0.028) ** 2 + 2:
                            c = (255, 246, 210)
                    else:
                        if d2 < (w * 0.02) ** 2 + 1.5:
                            c = (226, 230, 240)
                        elif night and y < hz * 0.6 and _lattice(seed32, 7, x, y) < 0.012:
                            c = _mix(c, (235, 240, 255), 0.75)
                    if cloud > 0.05:
                        n = _noise(seed32, 9, x / (w * 0.16) + 3.1, y / (h * 0.055))
                        a = _smooth(n, 0.62 - cloud * 0.4, 0.82 - cloud * 0.3) * _smooth(y, h * 0.04, h * 0.2)
                        lit = _mix(stops[1], stops[3], min(1.0, y / hz))
                        c = _mix(c, _mix(lit, (236, 240, 246), 0.55 if up else 0.12), a * 0.85)
                row.append(c)
            rows.append(row)
        # Two ranges and the ground, coloured by the land and by the light.
        light = stops[3]
        ground = terrain_rgb(200, biome if biome >= SHORE else GRASS)
        dim = 0.34 if night else 0.62 if not up or day < 0.08 or day > 0.92 else 0.92
        for depth, (scale, top, amp, haze) in enumerate(((w * 0.22, hz - h * 0.03, h * 0.16, 0.62),
                                                        (w * 0.15, hz + h * 0.02, h * 0.1, 0.36))):
            body = _mix((ground[0] * dim * 0.7, ground[1] * dim * 0.7, ground[2] * dim * 0.8), light, haze)
            for x in range(w):
                ty = top - amp * _noise(seed32, 11 + depth, x / scale, depth * 3.7) \
                    - amp * 0.35 * _noise(seed32, 13 + depth, x / (scale * 0.31), depth * 5.1)
                for y in range(max(0, int(ty)), h):
                    rows[y][x] = body
        self.ground_y = []
        for x in range(w):
            gy = hz + h * 0.1 - h * 0.05 * _noise(seed32, 17, x / (w * 0.2), 9.3)
            self.ground_y.append(gy)
            for y in range(max(0, int(gy)), h):
                fade = 0.0 if flat else min(1.0, (y - gy) / (h * 0.18))
                rows[y][x] = (ground[0] * dim * (1 - 0.45 * fade), ground[1] * dim * (1 - 0.45 * fade),
                              ground[2] * dim * (1 - 0.4 * fade))
        if place:
            self._town(rows, seed32, night or not up or day < 0.06 or day > 0.9)
        self.tops = [[(int(c[0]), int(c[1]), int(c[2])) for c in rows[y]] for y in range(0, h, 2)]
        self.bottoms = [[(int(c[0]), int(c[1]), int(c[2])) for c in rows[y]] for y in range(1, h, 2)]

    def _town(self, rows, seed32: int, lit: bool) -> None:
        """A few roofs and a mast on the right of the picture, their windows lit after dark."""
        w, h = self.w, self.h
        x = int(w * 0.62)
        n = 0
        while x < w * 0.92:
            bw = 3 + int(_lattice(seed32, 21, n, 0) * 4)
            bh = 3 + int(_lattice(seed32, 22, n, 0) * (h * 0.09))
            gy = int(self.ground_y[min(w - 1, x)])
            for bx in range(bw):
                for by in range(1, bh + 1):
                    if 0 <= gy - by < h and x + bx < w:
                        window = lit and by > 1 and by < bh and by % 2 == 0 and bx % 2 == 1 \
                            and _lattice(seed32, 23, x + bx, by) < 0.7
                        rows[gy - by][x + bx] = (255, 214, 124) if window else (26, 22, 44)
            x += bw + 1 + int(_lattice(seed32, 24, n, 0) * 2)
            n += 1
        mx = int(w * 0.56)
        gy = int(self.ground_y[mx])
        for y in range(gy - int(h * 0.2), gy):
            if 0 <= y < h:
                rows[y][mx] = (70, 62, 96)
        if 0 <= gy - int(h * 0.2) - 1 < h:
            rows[gy - int(h * 0.2) - 1][mx] = (255, 84, 72)

    def draw(self, screen: Screen, top: int = 0) -> None:
        for r in range(len(self.tops)):
            screen.pixel_row(0, top + r, self.tops[r], self.bottoms[r])


def draw_balloon(screen: Screen, cx: float, cy: float, r: float, light=(1.0, 1.0, 1.0), flame: bool = True,
                 colours=BALLOON_COLOURS, pattern: str = "stripes") -> None:
    """The balloon, in pixels: an envelope in its pilot's colours and pattern, lit from the right, ropes and a basket."""
    tail = r * 0.5
    for y in range(int(cy - r), int(cy + r + tail) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            dx, dy = x - cx, y - cy
            if dy <= 0:
                inside = 1.0 - (dy / r) * (dy / r)
                hw = r * math.sqrt(inside) if inside > 0 else 0.0
            else:
                hw = r * (1.0 - (dy / (r + tail)) ** 1.7)
            if hw < 0.8 or abs(dx) > hw + 0.3:
                continue
            u = max(-1.0, min(1.0, dx / max(hw, 1.0)))
            gore = int((math.asin(u) / math.pi + 0.5) * 7.999)
            down = (dy + r) / (2.5 * r)             # 0 at the crown, 1 at the mouth
            if pattern == "halves":
                band = 2 if abs(dy) < r * 0.09 else 0 if dy < 0 else 1 if down < 0.86 else 3
            elif pattern == "rings":
                band = int(down * 6.0) % 4
            elif pattern == "chevrons":
                band = int(down * 6.0 + abs(u) * 2.2) % 4
            else:
                band = gore % 4
            col = colours[band]
            lum = 0.58 + 0.38 * u + 0.1 * (dy / r)
            c = (col[0] * lum * light[0], col[1] * lum * light[1], col[2] * lum * light[2])
            if flame and dy > r * 0.8:
                c = _mix(c, (255, 196, 120), min(1.0, (dy - r * 0.8) / (r * 0.7)) * 0.6)
            screen.pixel(x, y, c)
    by = int(cy + r + tail) + 1
    bw = max(1, int(r / 6))
    rope = max(1, int(r / 5))
    if flame:
        screen.pixel(int(cx), by, (255, 226, 140))
    for i in range(1, rope + 1):
        screen.pixel(int(cx - bw), by + i, (58, 46, 70))
        screen.pixel(int(cx + bw), by + i, (58, 46, 70))
    for y in range(by + rope + 1, by + rope + 1 + max(1, rope)):
        for x in range(int(cx - bw), int(cx + bw) + 1):
            screen.pixel(x, y, (104, 70, 46))


class Scene:
    """One full-screen picture that plays for a number of frames.

    kind       "splash", "launch" or "landing"
    frames     how many frames it plays; the balloon moves through them
    hold       True for the splash: it stays under a prompt until a key
    """

    def __init__(self, kind: str, width: int, height: int, pixels: bool, motion: bool, hour: float = 6.6,
                 cloud: float = 0.0, biome: int = GRASS, seed: int = 1, place: bool = False,
                 caption: str = "", sub: str = "", flat: bool = False, design: dict | None = None,
                 family: str = ""):
        self.kind, self.width, self.height = kind, width, height
        self.family, self.seed = family, seed       # for a wonder: which kind of sight, and which one of them
        self.flat = flat
        self.design = design_of(design)
        self.pixels, self.caption, self.sub = pixels, caption, sub
        self.hold = kind == "splash"
        moving = pixels and motion
        self.frames = {"splash": 16, "launch": 11, "landing": 14, "wonder": 14}[kind] if moving else 1
        # A still picture stays a moment, and so does a landing once it is down. A wonder stays longest.
        self.linger = 0 if self.hold else 12 if kind == "wonder" else 8 if not moving else \
            4 if kind == "landing" else 0
        self.index = 0
        self.backdrop = Backdrop(width, height, hour, cloud, biome, seed, place, flat) if pixels else None

    @property
    def over(self) -> bool:
        return not self.hold and self.index >= self.frames + self.linger

    def advance(self, frames: int = 1) -> None:
        """Move on by so many frames. However many are skipped, the last pose is always shown."""
        pose = self.frames - 1
        if self.index < pose:
            self.index = min(pose, self.index + frames)
        elif not self.hold:
            self.index = min(self.frames + self.linger, self.index + frames)

    def _eased(self) -> float:
        """How far through its move the balloon is, from 0 to 1, slowing as it arrives."""
        if self.frames <= 1:
            return 1.0
        t = min(1.0, self.index / (self.frames - 1))
        return 1.0 - (1.0 - t) * (1.0 - t)

    def draw(self, screen: Screen) -> None:
        w, h = screen.width, screen.height
        if not self.pixels:
            self._card(screen)
            return
        back = self.backdrop
        back.draw(screen)
        r = max(3.0, back.h / 7.0)
        ground = back.ground_y[int(w * 0.34)] - r * 1.75
        t = self._eased()
        scale = max(1, (w - 8) // 60)
        title_y = max(2, int(back.h * 0.14))
        if self.kind == "wonder":
            self._wonder(screen)
            r = max(2.0, back.h / 11.0)                 # small, and off to one side: the sight is the picture
            cx, cy = w * (0.14 + 0.06 * t), back.h * (0.2 if self.family in ("aurora", "alpenglow") else 0.3)
            if self.family in ("aurora", "lightning"):
                cy = back.h * 0.45
        elif self.kind == "splash":
            rest = title_y + 7 * scale + 3 + r          # it comes to rest under the title, clear of it
            cx, cy = w * 0.74, back.h + r + (rest - back.h - r) * t
        elif self.kind == "launch":
            cx, cy = w * 0.34 + w * 0.1 * t, ground + (-r * 2.5 - ground) * (t * t)
        else:
            cx, cy = w * 0.24 + w * 0.1 * t, -r * 1.5 + (ground + r * 1.5) * t
        night = back.hour < 5.5 or back.hour >= 20.5
        if self.kind == "wonder" and self.flat:
            night = False                               # in 256 colours a balloon dimmed for the night is a grey smudge
        draw_balloon(screen, cx, cy, r, (0.5, 0.52, 0.66) if night else (1.0, 1.0, 1.0),
                     flame=self.kind != "landing" or t < 0.9, colours=design_rgb(self.design),
                     pattern=self.design["pattern"])
        if self.kind == "splash":
            self._title(screen, max(2, int(w * 0.06)), title_y, "FAIR WINDS", scale)
            row = (title_y + 7 * scale) // 2 + 2
            self._over(screen, max(2, int(w * 0.06)), row, "Read the wind. Carry the post. Finish the map.", "bright")
            if self.index >= self.frames - 1:
                self._over(screen, max(2, int(w * 0.06)), h - 2, "Press any key", "accent")
        else:
            self._over(screen, 2, h - 3, self.caption, "bright")
            self._over(screen, 2, h - 2, self.sub, "accent")

    # -- wonders: one picture for each kind, painted over the sky and land of the hour ----------

    def _wonder(self, screen: Screen) -> None:
        """Paint the wonder over the backdrop. Each kind has its own hand; the frame number moves what moves."""
        back, n, family = self.backdrop, self.index, self.family
        w, h, hz = back.w, back.h, back.horizon
        seed = (self.seed * 2654435761 + 97) & 0xFFFFFFFF

        def under(x: int, y: int):
            x, y = min(w - 1, max(0, x)), min(h - 1, max(0, y))
            return (back.tops if y % 2 == 0 else back.bottoms)[y // 2][x]

        def put(x: int, y: int, c) -> None:
            if 0 <= x < w and 0 <= y < h:
                screen.pixel(x, y, (int(c[0]), int(c[1]), int(c[2])))

        def ridge(x: int, top: float, amp: float, scale: float, key: int) -> float:
            return top - amp * _noise(seed, key, x / scale, key * 1.7) - amp * 0.4 * _noise(seed, key + 1, x / (scale * 0.3), 2.2)

        if family == "aurora":
            # Curtains of green that fade to violet at the foot, hung from a line that wanders across the sky.
            for x in range(w):
                band = _smooth(_noise(seed, 3, x / (w * 0.09) + n * 0.07, 0.4), 0.3, 0.72)
                band = max(band, 0.7 * _smooth(_noise(seed, 5, x / (w * 0.21) - n * 0.05, 3.3), 0.4, 0.8))
                if band <= 0.02:
                    continue
                top = h * 0.07 + h * 0.2 * _noise(seed, 4, x / (w * 0.3), 1.1)
                length = h * (0.24 + 0.2 * _noise(seed, 6, x / (w * 0.13), 5.5))
                for y in range(int(top), int(min(hz - 2, top + length))):
                    f = (y - top) / length
                    glow = band * (1.0 - f) ** 1.3 * (0.75 + 0.25 * _lattice(seed, 8, x, n // 2))
                    colour = _mix((96, 246, 150), (150, 96, 226), f)
                    put(x, y, _mix(under(x, y), colour, min(0.88, glow)))
        elif family == "salt":
            # The ground turned to a white pan that holds the sky, cracked into plates that shrink toward the horizon.
            for x in range(w):
                gy = int(back.ground_y[x]) - 2
                for y in range(gy, h):
                    depth = (y - gy) / max(1.0, h - gy)
                    sky = under(x, max(0, gy - int((y - gy) * 1.4) - 2))
                    c = _mix((240, 238, 230), sky, 0.5 - 0.25 * depth)
                    plate = 3 + int(depth * 9)
                    shift = (y // max(2, plate // 2)) * 3
                    if (x + shift) % plate == 0 or (y - gy) % max(2, plate // 2) == 0:
                        c = _mix(c, (150, 150, 150), 0.3 + 0.2 * depth)
                    put(x, y, c)
        elif family == "falls":
            # A dark cliff on the right, and a white thread down it into spray.
            foot = int(h * 0.9)
            lip = {}
            for x in range(int(w * 0.44), w):
                edge = _smooth(x, w * 0.44, w * 0.56)
                top = int(ridge(x, hz - h * 0.3 * edge, h * 0.06, w * 0.12, 21))
                lip[x] = top
                for y in range(top, h):
                    rock = _mix((56, 62, 74), (96, 104, 112), _noise(seed, 23, x / 3.0, y / 9.0))
                    if y < top + 2:
                        rock = (74, 110, 70)                    # turf along the lip
                    put(x, y, rock)
            fx = int(w * 0.68)
            for y in range(lip.get(fx, int(hz * 0.6)) + 1, foot):
                spread = 1 + (y - lip.get(fx, 0)) // max(1, h // 10)
                for dx in range(-spread, spread + 1):
                    streak = _lattice(seed, 25, fx + dx, (y - n * 3) // 3)
                    put(fx + dx, y, _mix((236, 244, 252), (170, 200, 232), streak * (0.3 + 0.4 * abs(dx) / (spread + 1))))
            for x in range(fx - h // 5, fx + h // 5 + 1):
                for y in range(foot - h // 12, min(h, foot + h // 14)):
                    d = ((x - fx) / (h / 5.0)) ** 2 + ((y - foot) / (h / 12.0)) ** 2
                    if d < 1.0:
                        puff = (1.0 - d) * (0.55 + 0.35 * _lattice(seed, 27, x // 2, y // 2 + n))
                        put(x, y, _mix(under(x, y) if x < w * 0.44 else (80, 88, 100), (238, 244, 250), min(0.9, puff)))
        elif family in ("cloudsea", "eye"):
            # A floor of cloud lit from the low sun, with the tops of the hills standing out of it.
            peaks = [(w * (0.2 + 0.3 * i + 0.1 * _lattice(seed, 31, i, 0)), h * (0.1 + 0.08 * _lattice(seed, 32, i, 0)))
                     for i in range(3)]
            lit, shade = _mix((250, 236, 220), under(w // 2, hz - 2), 0.25), (146, 150, 176)
            for x in range(w):
                top = ridge(x, hz - h * 0.02, h * 0.05, w * 0.1, 33) + h * 0.012 * math.sin(x / 5.0 + n * 0.2)
                if family == "cloudsea":
                    for px, ph in peaks:
                        rise = ph * (1.0 - abs(x - px) / (ph * 1.6))
                        if rise > 0:
                            for y in range(int(top - rise), int(top) + 1):
                                snow = y < top - rise * 0.55
                                put(x, y, (228, 226, 232) if snow else (52, 54, 80))
                else:
                    # The wall of the storm on either hand, towering, and nothing but still air between.
                    wall = max(_smooth(w * 0.24 - x, 0, w * 0.2), _smooth(x - w * 0.76, 0, w * 0.2))
                    if wall > 0:
                        for y in range(int(h * 0.04 + (1.0 - wall) * h * 0.5), int(top)):
                            f = _noise(seed, 37, x / (w * 0.06), y / (h * 0.07))
                            put(x, y, _mix(_mix(shade, lit, f * 0.8), under(x, y), 0.25 * (1.0 - wall)))
                for y in range(int(top), h):
                    depth = (y - top) / max(1.0, h - top)
                    f = _noise(seed, 35, x / (w * 0.08) + n * 0.03, y / (h * 0.045))
                    c = _mix(lit, shade, min(1.0, 0.15 + 0.6 * f + 0.3 * depth))
                    put(x, y, c)
        elif family == "lightning":
            # A bank of cloud standing off to the right, and a bolt out of it on some frames.
            strike = n % 5 in (1, 2)
            cx, cy = w * 0.72, h * 0.24
            for x in range(int(w * 0.4), w):
                for y in range(0, int(hz * 0.78)):
                    d = ((x - cx) / (w * 0.3)) ** 2 + ((y - cy) / (h * 0.24)) ** 2
                    edge = d + 0.5 * _noise(seed, 41, x / (w * 0.06), y / (h * 0.06)) - 0.25
                    if edge < 1.0:
                        c = _mix((30, 32, 52), (74, 76, 104), _noise(seed, 43, x / 4.0, y / 5.0))
                        if strike:
                            near = max(0.0, 1.0 - abs(x - cx) / (w * 0.16))
                            c = _mix(c, (226, 222, 250), near * 0.6)
                        put(x, y, _mix(under(x, y), c, min(1.0, (1.0 - edge) * 3.0)))
            if strike:
                x, y = int(cx), int(cy + h * 0.14)
                ground = int(back.ground_y[min(w - 1, x)])
                while y < ground:
                    put(x, y, (255, 250, 214))
                    put(x + 1, y, (190, 190, 255))
                    x += int(_lattice(seed, 45 + n // 5, y, 0) * 3) - 1
                    y += 1
        elif family == "alpenglow":
            # A wall of high peaks: sharp, rose on the faces the last sun reaches, blue in their own shadow
            # and below the line the light has already left.
            tops = []
            for x in range(w):
                sharp = abs(_noise(seed, 51, x / (w * 0.11), 0.7) - 0.5) * 2.0          # a ridge that comes to points
                tops.append(hz - h * 0.08 - h * 0.46 * (1.0 - sharp) ** 1.6
                            - h * 0.05 * _noise(seed, 52, x / (w * 0.025), 4.1))
            for x in range(w):
                top = int(tops[x])
                lean = tops[min(w - 1, x + 2)] - tops[max(0, x - 2)]       # rising to the right: a face turned from the sun
                shadow = _smooth(lean, -1.0, 2.5)
                line = hz - h * 0.14 + h * 0.03 * _noise(seed, 53, x / (w * 0.1), 0.0)
                for y in range(max(0, top), int(back.ground_y[x]) + 1):
                    grain = _noise(seed, 55, x / 4.0, y / 6.0)
                    if y < line:
                        lit = _mix((255, 170, 150), (255, 214, 176), grain)
                        dark = _mix((150, 96, 140), (176, 120, 150), grain)
                        c = _mix(lit, dark, shadow * 0.85)
                        c = _mix(c, (104, 84, 136), 0.55 * _smooth(y, line - h * 0.1, line))
                    else:
                        c = _mix((50, 50, 94), (74, 70, 118), grain)
                    put(x, y, c)
        elif family == "sealights":
            # Black water, a dark shore, and a scatter of cold light on the sea that moves.
            shore = int(hz + h * 0.02)
            for x in range(w):
                land = ridge(x, shore, h * 0.07, w * 0.14, 61) if x > w * 0.6 else shore + 1
                for y in range(shore - int(h * 0.1), h):
                    if y < land and x > w * 0.6:
                        continue
                    if x > w * 0.6 and y < shore + (x - w * 0.6) * 0.25:
                        put(x, y, (14, 16, 30))
                        continue
                    if y < shore:
                        continue
                    depth = (y - shore) / max(1.0, h - shore)
                    c = _mix((8, 18, 40), (4, 10, 26), depth)
                    drift = _noise(seed, 63, x / (w * 0.12) + n * 0.06, y / (h * 0.03))
                    if _lattice(seed, 65, x + (n // 2) * (y % 3 - 1), y) < 0.05 + 0.2 * _smooth(drift, 0.5, 0.8):
                        c = _mix((60, 236, 214), (150, 250, 236), _lattice(seed, 67, x, y + n))
                    put(x, y, c)
        elif family == "mist":
            # Low ground at dawn: rank behind rank of dark tree-tops, and white lying in every hollow between.
            floor = int(hz - h * 0.03)
            ranks = 4
            band = max(3.0, (h - floor) / ranks)
            for x in range(w):
                for y in range(floor, h):
                    i = min(ranks - 1, int((y - floor) / band))
                    line = floor + i * band + band * 0.25 * _noise(seed, 71 + i, x / (w * 0.18), i * 2.3)
                    tooth = abs((x + i * 3) % (4 + i) - (2 + i // 2)) * (0.6 + 0.25 * i)
                    crown = line + tooth
                    if y < crown:
                        if i == 0:
                            continue                    # above the first rank: the sky and the far hills
                        prev = floor + (i - 1) * band
                        lay = _smooth((y - prev) / band, 0.2, 0.75)
                        put(x, y, _mix((26, 40, 40), (236, 232, 238), min(0.94, lay + 0.1)))
                        continue
                    lay = _smooth((y - crown) / band, 0.25, 0.9)
                    lay *= 0.75 + 0.25 * _noise(seed, 75, x / (w * 0.1) + n * 0.03, y / 3.0)
                    dark = _mix((22, 38, 38), (40, 58, 50), _noise(seed, 77, x / 3.0, y / 3.0))
                    put(x, y, _mix(dark, (236, 232, 238), min(0.94, lay)))

    def _title(self, screen: Screen, x: int, y: int, text: str, scale: int) -> None:
        for layer, colour in ((1, (40, 24, 72)), (0, None)):
            cx = x
            for ch in text:
                for ry, bits in enumerate(_TITLE_FONT[ch].split()):
                    for rx, bit in enumerate(bits):
                        if bit != "1":
                            continue
                        for dy in range(scale):
                            for dx in range(scale):
                                c = colour or _mix((250, 240, 214), (255, 196, 110), (ry * scale + dy) / (7 * scale))
                                screen.pixel(cx + rx * scale + dx + layer, y + ry * scale + dy + layer, c)
                cx += 6 * scale

    def _over(self, screen: Screen, x: int, row: int, text: str, ink) -> None:
        for i, ch in enumerate(text[:max(0, screen.width - x - 1)]):
            screen.over(x + i, row, ch, ink, 0.62)

    def _card(self, screen: Screen) -> None:
        """The reading for terminals without the picture: the words, in the middle."""
        lines = ["F A I R   W I N D S", "", "Read the wind. Carry the post. Finish the map.", "", "Press any key"] \
            if self.kind == "splash" else [self.caption, self.sub]
        top = max(0, (screen.height - len(lines)) // 2)
        for i, line in enumerate(lines):
            screen.text(max(0, (screen.width - len(line)) // 2), top + i, line, "accent" if i == 0 else "text")


# --------------------------------------------------------------------------
# The session.
# --------------------------------------------------------------------------

_ENTER = "\x1b[0m\x1b[?25l\x1b[2J\x1b[H"
_LEAVE = "\x1b[0m\x1b[?25h\x1b[2J\x1b[H"

class Session:
    """One caller, from the first byte to the last.

    Every way out (quit, end of input, broken output, SIGTERM, the session
    limit) goes through `run`, which always restores the terminal.
    """

    def __init__(self, launch: Launch, inp: Input, out: Output, clock=time.monotonic, game=None):
        self.launch = launch
        self.game = game                      # the plain flight, when there is one
        self.inp, self.out, self.clock = inp, out, clock
        self.keys = KeyDecoder()
        self.width, self.height = launch.width, launch.height
        self.started = clock()
        self.stop: str | None = None          # why the session is ending
        self.resized = False                  # set from a signal handler
        self.terminated = False               # set from a signal handler
        self.view = "front"
        self.notice = ""
        if game is not None and game.opening_notice:
            self.notice, game.opening_notice = game.opening_notice, ""      # said once, on the first screen
        self._warned: set[float] = set()
        self._dirty = True
        self.frame = Screen(self.width, self.height, launch.color_depth, launch.charset)
        self.shown: Screen | None = None      # what the caller's terminal holds; None means unknown
        self._next_frame: float | None = None # when the next frame of moving wind is due
        self.scene: Scene | None = None       # a full-screen scene playing now
        self.scenes_on = False                # launch and landing scenes; main() switches them on
        self._sent: list = []                 # (when, bytes) for what was sent in the last second
        self._wind_frames = 0                 # how many frames of moving wind the next paint carries
        self._painted = 0                     # bytes the last paint sent
        self._scene_at: float | None = None   # the moment the scene's shown frame belongs to
        self._scene_due = 1                   # frames the scene moves on by at its next paint
        self._scene_began = 0.0               # when the scene's first frame went out
        self._after_scene = False             # the next paint puts the game back after a scene
        self._next_poll = self.started + GEOMETRY_POLL

    # -- time ---------------------------------------------------------------

    def remaining(self) -> float | None:
        if self.launch.session_limit is None:
            return None
        return self.launch.session_limit - (self.clock() - self.started)

    def _check_deadline(self) -> None:
        left = self.remaining()
        if left is None:
            return
        if left <= DEADLINE_GRACE:
            self.stop = "deadline"
            return
        for mark in DEADLINE_WARNINGS:
            if left <= mark and mark not in self._warned and self.launch.session_limit > mark:
                self._warned.add(mark)
                minutes = max(1, round(left / 60))
                self.notice = (f"About {minutes} minute{'s' if minutes != 1 else ''} of session time left. "
                               "Nothing is lost when it ends.")
                self._dirty = True

    # -- geometry -----------------------------------------------------------

    def _check_geometry(self) -> None:
        now = self.clock()
        if not self.resized and now < self._next_poll:
            return
        self.resized = False
        self._next_poll = now + GEOMETRY_POLL
        size = current_geometry(self.launch)
        if size != (self.width, self.height):
            self.width, self.height = size
            self.shown = None               # a resize throws the buffer away and repaints everything
            if self.scene is not None:
                self._end_scene()           # and a scene painted for the old size is over
            self._dirty = True

    # -- drawing ------------------------------------------------------------

    def _line(self, row: int, col: int, text: str, role: str = "text") -> str:
        """One run of text onto the frame being drawn."""
        self.frame.text(col, row, text, role)
        return ""

    def _kv(self, row: int, col: int, text: str, wide: int | None = None, label="label", value="value") -> str:
        """A line of names and figures: what stands between backticks is a figure, and is drawn as one.

        wide is how many cells the line may take; what does not fit is left off.
        """
        room = self.width if wide is None else wide
        for n, part in enumerate(text.split("`")):
            part = part[:max(0, room)]
            self.frame.text(col, row, part, value if n % 2 else label)
            col += len(part)
            room -= len(part)
        return ""

    def _kv_right(self, row: int, edge: int, text: str, least: int = 0) -> str:
        """A marked line ending at a column: it measures what will show, not the marks."""
        return self._kv(row, max(least, edge - len(text.replace("`", ""))), text)

    def paint(self) -> None:
        """Draw the current view onto a new frame and send what changed."""
        self.frame = Screen(self.width, self.height, self.launch.color_depth, self.launch.charset)
        if layout_tier(self.width, self.height) == "undersize":
            self._undersize()
        elif self.scene is not None:
            self.scene.draw(self.frame)
        elif self.view == "help":
            self._help()
        elif self.game is not None:
            self.game.draw(self)
        else:
            self._front()
        whole = self.shown is None
        if whole:
            changed = "\x1b[0m\x1b[2J" + self.frame.render(None)
        else:
            changed = self.frame.render(self.shown)
        if changed:
            self.out.write(changed)
            if self.scene is None and not self._after_scene:
                # A scene paces its own frames, and the paint that puts the
                # game back is its last. Counted against the moving wind, they
                # would hold back the whole burst that follows a scene.
                now = self.clock()
                self._sent = [(when, size) for when, size in self._sent if when > now - 1.0]
                self._sent.append((now, len(changed)))
        self._after_scene = False
        self._painted = len(changed)
        if self._wind_frames and self.game is not None and not whole:
            # This paint was the wind moving, by so many frames. A whole
            # repaint (a resize, Ctrl-L) says nothing about what wind costs.
            self.game.map.tune(len(changed) // self._wind_frames)
        elif self.game is not None:
            self.game.map.settled()             # any other paint has taken up whatever a change of share cost
        self._wind_frames = 0
        self.shown = self.frame
        self._dirty = False

    def _dot(self) -> str:
        return " · " if self.launch.charset == "utf-8" and self.launch.unicode_style else " - "

    def _undersize(self) -> list[str]:
        lines = [f"Fair Winds needs a terminal of {MIN_WIDTH} x {MIN_HEIGHT} or larger.",
                 f"Yours is {self.width} x {self.height}.",
                 "Enlarge the window, or press Q to leave."]
        top = max(0, (self.height - len(lines)) // 2)
        return [self._line(top + i, max(0, (self.width - len(t)) // 2), t, "accent" if i == 0 else "text")
                for i, t in enumerate(lines)]

    def _front(self) -> list[str]:
        launch, dot = self.launch, self._dot()
        left = self.remaining()
        host = "standalone" if launch.host == "standalone" else f"NetBBS{dot}{launch.node_name}"
        rows = [
            ("Host", host),
            ("Terminal", f"{self.width} x {self.height}{dot}{launch.color_depth}{dot}{launch.charset}{dot}{launch.transport}"),
            ("Layout", layout_tier(self.width, self.height)),
            ("Pilot", "visiting: nothing is kept" if launch.guest else "registered"),
            ("Session", "no limit" if left is None else f"about {max(1, round(left / 60))} min left"),
        ]
        out = [self._line(0, 1, "FAIR WINDS", "accent"),
               self._line(0, self.width - len(VERSION) - 3, "v" + VERSION, "dim"),
               self._line(2, 1, f"Welcome aboard, {launch.handle}."),
               self._line(3, 1, "The balloon is still in the shed. This build only proves that the door"),
               self._line(4, 1, "opens and closes cleanly.")]
        for i, (label, value) in enumerate(rows):
            out.append(self._line(6 + i, 1, label, "dim"))
            out.append(self._line(6 + i, 13, value))
        if self.notice:
            out.append(self._line(self.height - 3, 1, self.notice, "warn"))
        out += self._keys(self.height - 1, [("?", "Help"), ("Q", f"Return to {launch.node_name}")])
        return out

    def _help(self) -> list[str]:
        text = ["Fair Winds is a balloon navigation game. A balloon cannot steer: it can only",
                "climb or sink, and every altitude blows a different way.",
                "",
                "Keys that work everywhere:",
                "  ?  this help            Q  leave, or go back",
                "  B or Esc  go back       < and >  turn a page",
                "",
                "Leaving never loses anything. The game is saved as you play."]
        if self.game is not None:
            text = self.game.help_text()
        out = [self._line(0, 1, "FAIR WINDS", "accent"), self._line(0, 13, "Help", "dim")]
        # On a tall screen a blank row sets each heading apart; at 24 rows every row is needed.
        roomy = self.height >= 2 + len(text) + sum(t.startswith("# ") for t in text) + 2
        row = 2
        for t in text:
            if t.startswith("# "):
                if roomy and row > 2:
                    row += 1
                self._line(row, 1, t[2:], "accent")
            else:
                self._kv(row, 1, t, self.width - 2, "text", "cool")
            row += 1
        out += self._keys(self.height - 1, [("B", "Back"), ("Q", "Back")])
        return out

    def _keys(self, row: int, keys, gap: int = 3) -> list[str]:
        out, col = [], 1
        for key, label in keys:
            out.append(self._line(row, col, "[", "dim"))
            out.append(self._line(row, col + 1, key, "accent"))
            out.append(self._line(row, col + 1 + len(key), "]", "dim"))
            out.append(self._line(row, col + 3 + len(key), label))
            col += len(key) + len(label) + 3 + gap
        return out

    # -- input --------------------------------------------------------------

    def _handle(self, key: str) -> None:
        self._dirty = True
        if key in ("CTRL-L", "CTRL-R"):
            # Redraw: only differences are sent, so if a line was garbled on
            # the way, or the host wrote over the screen, this puts it right.
            self.shown = None
            if self.game is not None:
                self.game.frames_left = 0       # any key ends a burst, this one too
            return
        self.notice = ""
        lower = key.lower() if len(key) == 1 else key
        if self.view == "help":
            if lower in ("q", "b", "ESC", "?", "ENTER", "CTRL-C"):
                self.view = "front"
            return
        if self.game is not None and layout_tier(self.width, self.height) != "undersize":
            answer = self.game.key(key)
            if answer == "quit":
                self.stop = "quit"
            elif answer == "help":
                self.view = "help"
            return
        if lower in ("q", "CTRL-C"):
            self.stop = "quit"
        elif lower in ("?", "F1"):
            self.view = "help"

    def step(self) -> None:
        """One turn of the loop: time, size, paint, then wait briefly for keys."""
        if self.terminated:
            self.stop = "terminated"
            return
        self._check_deadline()
        if self.stop:
            return
        self._check_geometry()
        if self.game is not None and self.game.pulse(self.clock()):
            self.scene = None                   # the pilot was taken over: that is all there is to show
            self._dirty = True
        if self.scene is not None:
            self._scene_step()
            return
        if self._dirty:
            self.paint()
        moving = self.game is not None and self.view == "front" and self.game.frames_left > 0
        if moving:
            # Frames are due at fixed moments, counted from when the burst began.
            # Whatever makes a frame late (a slow paint, bytes that were not a
            # key) the next one is still due when it was due.
            now = self.clock()
            if self._next_frame is None:
                self._next_frame = now + FRAME_SECONDS
            wait = max(0.01, self._next_frame - now)
        else:
            self._next_frame = None
            wait = IDLE_TIMEOUT
        data = self.inp.read(ESCAPE_TIMEOUT if self.keys.pending else wait)
        if data is None:
            keys = self.keys.flush() if self.keys.pending else []
        elif data == b"":
            self.stop = "eof"
            return
        else:
            keys = self.keys.feed(data)
        if moving and not keys:
            self._frames_due()
        if keys:
            self._next_frame = None             # a key starts whatever moves next afresh
            self._wind_frames = 0               # and the paint it causes is the key's, not the wind's
        for key in keys:
            if self.stop:
                break
            self._handle(key)
            if self.game is not None and self.game.typed_ahead:
                # A turn was flown. Keys typed before its result was seen are
                # dropped, so a held key never flies turns unseen.
                self.game.typed_ahead = False
                self.keys.flush()
                break
        if self.game is not None and self.game.pending_scene is not None and not self.stop:
            self._take_scene()

    # -- scenes ---------------------------------------------------------------

    def start_splash(self) -> None:
        """The title, at the door: a picture where the terminal can show one, the words where it cannot."""
        if layout_tier(self.width, self.height) == "undersize":
            return
        pixels = self.game.map.pixels if self.game is not None else want_pixels(self.launch, {})
        motion = self.game.motion if self.game is not None else True
        self.scene = Scene("splash", self.width, self.height, pixels, motion, seed=self.width * 31 + self.height,
                           flat=self.launch.color_depth != "truecolor")
        self._next_frame = self._scene_at = None
        self._dirty = True

    def _take_scene(self) -> None:
        """Play the scene the last turn earned, if scenes are on and this terminal can show pictures."""
        asked, self.game.pending_scene = self.game.pending_scene, None
        if asked is None or not self.scenes_on or not self.game.map.pixels:
            return
        if layout_tier(self.width, self.height) == "undersize" or self.view != "front":
            return
        self.scene = Scene(asked["kind"], self.width, self.height, True, self.game.motion, hour=asked["hour"],
                           cloud=asked["cloud"], biome=asked["biome"], seed=asked["seed"], place=asked["place"],
                           caption=asked["caption"], sub=asked["sub"],
                           flat=self.launch.color_depth != "truecolor", design=asked.get("design"),
                           family=asked.get("family", ""))
        self._next_frame = self._scene_at = None
        self._dirty = True

    def _end_scene(self) -> None:
        self.scene, self._next_frame, self._scene_at, self._dirty = None, None, None, True
        self._after_scene = True
        self._sent.clear()

    def _scene_step(self) -> None:
        """One step while a scene plays: paint, wait for its next frame, and end it on any key."""
        scene = self.scene
        if self._dirty:
            whole = self.shown is None
            self.paint()
            # A frame that costs more than a line should carry seven times a
            # second is shown less often, evenly, and moves further each time.
            stride = 1
            if not whole and self._painted > SCENE_FRAME_BYTES:
                stride = min(SCENE_STRIDE_MAX, -(-self._painted // SCENE_FRAME_BYTES))
            if self._scene_at is None:
                self._scene_at = self._scene_began = self.clock()
            self._scene_due = stride
            self._next_frame = self._scene_at + stride * SCENE_FRAME_SECONDS
        resting = scene.hold and scene.index >= scene.frames - 1
        if resting:
            wait = IDLE_TIMEOUT
        else:
            now = self.clock()
            if self._next_frame is None:
                self._next_frame = now + SCENE_FRAME_SECONDS
            wait = max(0.01, self._next_frame - now)
        data = self.inp.read(ESCAPE_TIMEOUT if self.keys.pending else wait)
        if data == b"":
            self.stop = "eof"
            return
        if data is None:
            keys = self.keys.flush() if self.keys.pending else []
        else:
            keys = self.keys.feed(data)
        if keys:
            # Any key ends a scene, and the scene uses it up: it is never also a move.
            self.keys.flush()
            if "CTRL-C" in keys:
                self.stop = "quit"
            elif not scene.hold and self.clock() - self._scene_began < SCENE_GRACE_SECONDS:
                return                          # a held Enter is not a wish to skip what it began
            self._end_scene()
            return
        if resting:
            return
        now = self.clock()
        if now < self._next_frame - 1e-6:
            return
        late = int((now - self._next_frame) / SCENE_FRAME_SECONDS)
        self._scene_at = self._next_frame + late * SCENE_FRAME_SECONDS
        self._next_frame = self._scene_at + SCENE_FRAME_SECONDS
        scene.advance(self._scene_due + late)
        self._scene_due = 1
        self._dirty = True
        if scene.over:
            self._end_scene()

    def _frames_due(self) -> None:
        """Move the wind by as many frames as are due, and paint once if the line can take it.

        Late frames are skipped, never queued. And the bytes sent in any one
        second are capped: a pipe to the host takes a whole burst without
        blocking, so how long a write took says nothing about how far behind
        the caller's line is. A cap on what is sent is the only brake there is.
        """
        now = self.clock()
        if now < self._next_frame - 1e-6:
            return                              # woken early, by bytes that were not a key
        due = min(self.game.frames_left, 1 + int((now - self._next_frame) / FRAME_SECONDS))
        for _ in range(due):
            self.game.tick()
        self._next_frame += due * FRAME_SECONDS
        self._wind_frames += due                # every frame the next paint will carry, held ones too
        recent = sum(size for when, size in self._sent if when > now - 1.0)
        if recent < BURST_BYTES_PER_SECOND or self.game.frames_left == 0:
            self._dirty = True                  # the last frame is always painted: it is the one that stays

    def run(self) -> str:
        try:
            self.out.write(_ENTER)
            while not self.stop:
                self.step()
            if self.stop == "deadline":
                # Written straight out, past the buffer: the session is over and nothing is drawn after it.
                self.out.write("\x1b[0m\x1b[2J\x1b[1;2HSession time is up. Nothing is lost.")
        except SessionClosed:
            self.stop = "closed"
        finally:
            try:
                self.out.write(_LEAVE)
            except SessionClosed:
                pass
        return self.stop or "quit"


def diagnose(message: str) -> None:
    """A bounded line for the SysOp. Callers never see standard error."""
    sys.stderr.write(("fair_winds: " + clean_text(message, DIAGNOSTIC_LIMIT)) + "\n")
    sys.stderr.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fair_winds.py", description="Fair Winds, a door game for NetBBS.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--world-map", metavar="NODE_ID", help="print the continent of a node id and exit")
    parser.add_argument("--tempo", metavar="N", type=int, choices=TEMPOS,
                        help="open N world days in each real day, from today on, in every world; and exit")
    parser.add_argument("--backup", metavar="FOLDER",
                        help="copy every world's records to FOLDER and exit; safe while callers are playing")
    parser.add_argument("--guestbook", action="store_true",
                        help="list every line in every station's visitors' book and exit")
    parser.add_argument("--guestbook-remove", metavar="LINE",
                        help="remove one line, named as --guestbook lists it (world:number), and exit")
    args = parser.parse_args(argv)
    if args.guestbook or args.guestbook_remove is not None:
        try:
            if args.guestbook_remove is not None:
                print(guestbook_remove(data_dir(os.environ), args.guestbook_remove))
            else:
                lines = guestbook_lines(data_dir(os.environ))
                print("\n".join(lines) if lines else "no lines")
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"guestbook: {error}", file=sys.stderr)
            return 1
        return 0
    if args.tempo is not None:
        try:
            lines = set_tempo(data_dir(os.environ), args.tempo)
        except (OSError, sqlite3.Error, StoreError) as error:
            print(f"tempo not changed: {error}", file=sys.stderr)
            return 1
        print("\n".join(lines) if lines else "no world to change")
        return 0
    if args.backup is not None:
        try:
            names = backup_worlds(data_dir(os.environ), Path(args.backup))
        except (OSError, sqlite3.Error) as error:
            print(f"backup failed: {error}", file=sys.stderr)
            return 1
        print("\n".join(names) if names else "no world to back up")
        return 0
    if args.world_map is not None:
        print(world_map(World(world_seed(args.world_map))))
        return 0

    out = Output(sys.stdout.fileno())
    try:
        launch = load_launch(os.environ)
    except LaunchError as error:
        diagnose(f"cannot start: {error}")
        try:
            out.write("Fair Winds could not start: the BBS sent launch details it cannot use.\r\n"
                      "Please tell the SysOp.\r\n")
        except SessionClosed:
            pass
        return 2

    local = LocalTerminal()
    if launch.host == "standalone":
        local.enter()
    try:
        game = PlainFlight(launch, data_dir(os.environ), os.environ)
    except StoreError as error:
        # The records are there and cannot be used. Nothing is written over them.
        local.leave()
        diagnose(f"cannot open the world's records: {error}")
        try:
            out.write("Fair Winds cannot read this world's records, and has left them untouched.\r\n"
                      "Please tell the SysOp.\r\n")
        except SessionClosed:
            pass
        return 3
    try:                                    # from here the pilot is held: given up whatever happens next
        session = Session(launch, Input(sys.stdin.fileno(), console=local.console), out, game=game)

        def on_terminate(signum, frame):
            session.terminated = True

        def on_resize(signum, frame):
            session.resized = True

        signal.signal(signal.SIGTERM, on_terminate)
        signal.signal(signal.SIGINT, on_terminate)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, on_terminate)      # a dropped line: leave properly, so the pilot is free
        for name in ("SIGUSR1", "SIGWINCH"):     # NetBBS signals a resize; so does a local terminal
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), on_resize)
        session.scenes_on = os.environ.get("DOOR_SCENES", "").strip() != "0"
        if os.environ.get("DOOR_SPLASH", "").strip() != "0":
            session.start_splash()              # with the scenes off it is still there, and holds still
        session.run()
    finally:
        game.close()
        local.leave()
    return 0


if __name__ == "__main__":
    sys.exit(main())
