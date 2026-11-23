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

VERSION = "0.1.0.dev14"
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
PLACE_RADIUS_KM = 12.5                      # this close to a town or station is at it

ACTIONS = ("launch", "hold", "climb", "sink", "jettison", "land", "pibal")


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
        self._places = [(t["x"], t["y"], "town", i) for i, t in enumerate(world.towns)]
        self._places += [(s["x"], s["y"], "station", i) for i, s in enumerate(world.stations)]

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
        x, y = state.x + vx * TURN_HOURS, state.y + vy * TURN_HOURS
        stranded = False
        need = self.lowest_open(x, y)
        if need > layer:
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
                   paid for), "fuel" (nothing left to hold with), or None
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
        read = layer == state.layer or action == "launch" or state.pibal_until >= state.turn
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
            nx, ny = x + vx * TURN_HOURS, y + vy * TURN_HOURS
            need = self.lowest_open(nx, ny)
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
                        c = _VEIL[1] if (px + py * 3) % 7 == 0 else _VEIL[0]
                else:
                    c = _OFF_WORLD
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
             x: float, y: float, day: int, minute: int, charted=None) -> None:
        """Draw the ground and the weather. Markers go on afterwards with `mark` and `label`."""
        self.left, self.top, self.cols, self.rows = left, top, cols, rows
        self.x0, self.y0 = self.origin(x, y, cols, rows)
        self._taken = set()
        if self.pixels:
            key = (day, minute, self.x0, self.y0, cols, rows, charted is not None)
            if key != self._base_key or charted is not None:
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
                if charted is not None and not charted(wx, wy):
                    glyph, role = ("?" if (c + r * 3) % 7 == 0 else " "), "dim"
                elif frame.squall(wx, wy) > SQUALL_THRESHOLD:
                    glyph, role = "%", "warn"
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
_TRACK_ROLES = {"sink": "cool", "hold": "accent", "climb": "warm", "launch": "accent", "jettison": "warm"}


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
# The world's records (S10): one SQLite file for each world, shared by every
# caller of that node. Writes are short transactions that take the write
# lock at once; none is open while a caller thinks. A process killed at any
# instant leaves the turn before or the turn after, never half of one.
# --------------------------------------------------------------------------

APPLICATION_ID = 0x46574442          # "FWDB": says the file is a Fair Winds world
SCHEMA_VERSION = 1
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
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
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

    def commit(self, user_id: int, handle: str, career: dict, state: dict, action: str = "") -> int:
        """Write the pilot as they now stand, and with an action a line in the day's journal.

        One transaction: the flight, the journal row and the career go
        together or not at all. Raises LeaseLost, having written nothing, if
        the pilot is no longer this session's.
        """
        def work():
            db, now = self.db, self.clock()
            row = db.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,)).fetchone()
            if row is None or row[0] != self.token:
                raise LeaseLost()
            row = db.execute("SELECT seq FROM flights WHERE user_id=?", (user_id,)).fetchone()
            seq = row[0] if row else 0
            text = json.dumps(state)
            db.execute("INSERT INTO pilots VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
                       " handle=excluded.handle, career=excluded.career",
                       (user_id, handle, now, json.dumps(career)))
            if action:
                seq += 1
                db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (user_id, seq, state["day"], state["turn"], action, text, now))
                db.execute("DELETE FROM turns WHERE user_id=? AND day<?", (user_id, state["day"]))
            db.execute("INSERT INTO flights VALUES (?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
                       " seq=excluded.seq, state=excluded.state", (user_id, seq, text))
            db.execute("UPDATE sessions SET heartbeat=? WHERE user_id=?", (now, user_id))
            return seq
        return self._write(work)

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
        if cal is not None and calendar_day(cal, local) == cal["high_day"]                 and (not zone or meta.get("timezone") == zone):
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
        finally:
            db.close()
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
        self.visited = [("town", self.world.start_town)]
        self.targets = self._next_targets()
        self.deliveries, self.days_flown, self.total_km = 0, 0, 0.0
        self.log = []
        self._say(f"You are at {start['name']}. There is post for {self._names()}.")
        self._say("Read the winds and fly to whichever the day favours.")

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
        return self.state.day > self.today

    def offered(self, state: FlightState | None = None) -> dict:
        """What the pilot may do now: nothing, on a day that has not opened."""
        state = state or self.state
        return {} if state.day > self.today else self.engine.actions(state)

    def briefing(self) -> str:
        """When the next day opens, and how many opened days still wait. The day being flown heads the screen."""
        local = self._local()
        if self.waiting():
            opens = when_text(calendar_opens(self.calendar, self.state.day), local, self.launch.datetime_format)
            return f"Day {self.state.day} opens {opens}."
        more = self.today - self.state.day
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
        except (ValueError, KeyError, TypeError):
            diagnose(f"the record of pilot {self.launch.user_id} cannot be used")
            return "damaged"

    def _take(self, career: dict, state: dict):
        """Set the game from stored data. Raises ValueError, KeyError or TypeError if it is not a game."""
        flight = FlightState.from_dict(state)
        targets = [self._ref(t) for t in career["targets"]][:POST_CHOICES]
        visited = [self._ref(v) for v in career["visited"]][-4:]
        if not targets:
            raise ValueError("no post aboard")
        deliveries, days_flown, total_km = int(career["deliveries"]), int(career["days_flown"]), float(career["total_km"])
        self._dated = bool(career.get("dated", False))
        log = [str(line) for line in career.get("log", [])][-3:]
        self.state, self.targets, self.visited = flight, targets, visited
        self.deliveries, self.days_flown, self.total_km, self.log = deliveries, days_flown, total_km, log

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
        except (ValueError, KeyError, TypeError):
            # It is left where it is, and both the pilot and the SysOp are told.
            diagnose(f"the old save file {self.legacy.name} cannot be used; it is left in place")
            self.opening_notice = "An older saved game could not be read. This is a new one; the file is kept."
            return False
        self._imported = True
        return True

    def _career(self) -> dict:
        return {"targets": [list(t) for t in self.targets], "visited": [list(v) for v in self.visited],
                "deliveries": self.deliveries, "days_flown": self.days_flown, "total_km": self.total_km,
                "log": self.log[-3:], "dated": self._dated}

    def save(self, action: str = "") -> None:
        """Write the game as it stands; with an action, as a committed turn in the day's journal."""
        if self.store is None or self.ended:
            return
        try:
            self.store.commit(self.launch.user_id, self.launch.handle, self._career(), self.state.to_dict(), action)
            self.saved_ok = True
        except LeaseLost:
            self.ended = "taken"
        except sqlite3.Error as error:
            diagnose(f"a turn could not be saved: {error}")
            self.saved_ok = False       # play on; say so on the screen

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
        if not self._look_at_the_calendar():
            return False
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
        if self.store is not None:
            try:
                if self.busy is None and not self.ended:
                    self.store.release(self.launch.user_id)
            except sqlite3.Error:
                pass
            self.store.close()
            self.store = None

    # -- the post ------------------------------------------------------------

    def _ref(self, value) -> tuple[str, int]:
        kind, index = value
        group = self.world.towns if kind == "town" else self.world.stations
        if kind not in ("town", "station") or not 0 <= int(index) < len(group):
            raise ValueError("no such place")
        return kind, int(index)

    def place_of(self, ref: tuple[str, int]) -> dict:
        return (self.world.towns if ref[0] == "town" else self.world.stations)[ref[1]]

    def _next_targets(self) -> list[tuple[str, int]]:
        """Post for the nearest places not visited lately: the day's wind picks among them."""
        here = self.state
        refs = [("town", i) for i in range(len(self.world.towns))]
        refs += [("station", i) for i in range(len(self.world.stations))]
        at = self.engine.place(here.x, here.y)
        refs = [r for r in refs if r != at and r not in self.visited[-2:]]
        refs.sort(key=lambda r: ((self.place_of(r)["x"] - here.x) ** 2 + (self.place_of(r)["y"] - here.y) ** 2, r))
        return refs[:POST_CHOICES]

    def _names(self) -> str:
        names = [self.place_of(t)["name"] for t in self.targets]
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]

    def bearings(self, x: float | None = None, y: float | None = None) -> list[tuple[float, str, dict]]:
        """Distance, direction and place for each destination, nearest first."""
        x = self.state.x if x is None else x
        y = self.state.y if y is None else y
        out = []
        for ref in self.targets:
            place = self.place_of(ref)
            dx, dy = place["x"] - x, place["y"] - y
            out.append((math.sqrt(dx * dx + dy * dy), bearing(dx, dy), place))
        return sorted(out, key=lambda item: item[0])

    def coverage(self, x: float, y: float) -> float:
        """How well the nearest town or starting station covers this place, 0 to 1."""
        best = 0.0
        for place in self.world.towns + self.world.stations:
            d = math.sqrt((x - place["x"]) ** 2 + (y - place["y"]) ** 2)
            best = max(best, 1.0 - d / STATION_REACH_KM)
        return best

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
        return self.engine.preview(self.state, action, PLAIN_HORIZON, self.coverage(self.state.x, self.state.y))

    def phase(self) -> str:
        state = self.state
        if state.status == "aloft":
            return "aloft"
        if state.status in ("stranded", "ditched"):
            return state.status
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
        phase = self.phase()
        offered = self.offered()
        if phase in ("aloft", "ground"):
            options = self.choices()
            if k in ("w", "UP") and self.choice in options:
                self.choice = options[min(len(options) - 1, options.index(self.choice) + 1)]
            elif k in ("s", "DOWN") and self.choice in options:
                self.choice = options[max(0, options.index(self.choice) - 1)]
            elif k in ("w", "s", "UP", "DOWN") and options:
                self.choice = options[0]
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
        elif phase == "stranded" and k in ("w", "ENTER", " "):
            self._walk_out()
        elif phase == "ditched" and k in ("n", "ENTER", " "):
            self._picked_up()
        return None

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
        before = self.state
        result = self.engine.step(before, action)
        self.state = result.state
        self.heard = []
        stamp = clock_text(before.minute)
        for event in result.events:
            line = self._describe(event)
            if line:
                self._say(f"{stamp}  {line}")
        landed = before.status == "aloft" and self.state.status != "aloft"
        if landed:
            self._landed()
        self.save(action)                 # before anything is shown
        if self.ended:
            return
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
                        and ("delivered" in line or "no post" in line or "stranded" in line)), "")
        return {"kind": kind, "hour": minute / 60.0, "cloud": frame.cloud(state.x, state.y)[0],
                "biome": self.world.sample(state.x, state.y)[1], "place": place is not None,
                "seed": int(state.x) * 7919 + int(state.y) * 104729 + 17, "caption": caption, "sub": sub}

    def _describe(self, event: tuple) -> str:
        code = event[0]
        if code == "launch":
            return f"Up and away into layer {event[1]}, {LAYER_NAMES[event[1] - 1]}."
        if code in ("hold", "climb", "sink", "jettison"):
            return ""                      # the drift line says it
        if code == "drift":
            return f"Layer {self.state.layer if self.state.layer else '-'}: {event[1]:g} km, wind {event[2]}."
        if code == "forced_climb":
            return f"High ground: forced up into layer {event[1]} for {event[2]} fuel."
        if code == "forced_jettison":
            return f"High ground and no fuel to spare: a sack goes over the side for layer {event[1]}."
        if code == "ridge":
            return "High ground ahead rises above your ceiling. The balloon is held in its lee."
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
            place = event[2]
            where = "in the wild"
            if place:
                group = self.world.towns if place[0] == "town" else self.world.stations
                where = f"at {group[place[1]]['name']}"
            return f"Down {where}: a {event[1]} landing."
        if code == "stranded":
            return "No fuel, and nobody near. You are stranded."
        return ""

    def _landed(self) -> None:
        place = self.engine.place(self.state.x, self.state.y)
        if self.state.status != "ground" or place is None:
            return
        if place not in self.targets:
            self._say(f"There was no post for {self.place_of(place)['name']}. A night here fills the balloon.")
            return
        self.deliveries += 1
        self._say(f"The post for {self.place_of(place)['name']} is delivered. That makes {self.deliveries}.")
        if not self.visited or self.visited[-1] != place:
            self.visited.append(place)
        del self.visited[:-4]
        self.targets = self._next_targets()
        self._say(f"New post aboard, for {self._names()}.")
        # Until towns have stores (S12), a delivery fills the balloon up.
        self.state = dataclasses.replace(self.state, fuel=FUEL_START, ballast=BALLAST_START,
                                         pibals=PIBALS_START, wear=0)

    def _new_day(self, days: int, x: float | None = None, y: float | None = None, refill: bool = False) -> None:
        state = self.state
        stores = dict(fuel=FUEL_START, ballast=BALLAST_START, pibals=PIBALS_START, wear=0) if refill else {}
        self.total_km += state.distance        # today's distance joins the total as the day ends
        if state.turn > 0:
            self.days_flown += 1              # a day nothing was flown on is not a flying day
        self.state = dataclasses.replace(state, day=state.day + days, turn=0, layer=0, status="ground",
                                         distance=0.0, pibal_until=-1,
                                         x=state.x if x is None else x, y=state.y if y is None else y, **stores)
        self._choose_default()

    def _dawn(self) -> str:
        """What to say of the day the logbook has come to: it is here, or when it will be."""
        self._look_at_the_calendar()
        self._catch_up()                  # days that lapsed while this one was in progress
        if not self.waiting():
            return f"Day {self.state.day}. Dawn."
        opens = when_text(calendar_opens(self.calendar, self.state.day), self._local(), self.launch.datetime_format)
        return f"Day {self.state.day} opens {opens}."

    def _night(self) -> None:
        at_place = self.engine.place(self.state.x, self.state.y) is not None
        self._new_day(1, refill=at_place)
        self._say(self._dawn())
        self.save("night")                # once, with the new day and its log together

    def _nearest(self, towns) -> dict:
        return min(towns, key=lambda t: ((t["x"] - self.state.x) ** 2 + (t["y"] - self.state.y) ** 2, t["cell"]))

    def _walk_out(self) -> None:
        town = self._nearest(self.world.towns)
        self._new_day(2, town["x"], town["y"], refill=True)
        self._say(f"Two days on foot to {town['name']}. {self._dawn()}")
        self.targets = self._next_targets()
        self.save("walk")

    def _picked_up(self) -> None:
        ports = [t for t in self.world.towns if t["port"]] or self.world.towns
        town = self._nearest(ports)
        self._new_day(1, town["x"], town["y"], refill=True)
        self._say(f"The packet boat lands you at {town['name']}. {self._dawn()}")
        self.targets = self._next_targets()
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

    def draw(self, s: "Session") -> list[str]:
        if self.busy is not None or self.ended:
            return self._draw_lease(s)
        state, launch = self.state, self.launch
        phase = self.phase()
        # The layout follows the terminal: a bigger window is a bigger map.
        # At 80 x 24 the map is 52 x 16 with the panel beside it and seven rows below.
        self.tier = layout_tier(s.width, s.height)
        panel = PANEL_SMALL if self.tier == "small" else PANEL_WIDE
        self.map_cols, self.map_rows = s.width - panel - 2, s.height - 8
        self.panel_col, base, wide = s.width - panel + 1, s.height - 7, s.width - 2
        out = [s._line(0, 1, "FAIR WINDS", "accent"),
               s._line(0, 13, f"Day {state.day}  {clock_text(state.minute)}", "text")]
        distance, toward, nearest = self.bearings()[0]
        post = f"Nearest post: {nearest['name']}, {distance:.0f} km {toward}"
        out.append(s._line(0, max(32, s.width - 1 - len(post)), post, "text"))
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
        status = f"{self.briefing()}   {launch.handle}  {self.deliveries} delivered  {self.total_km + state.distance:.0f} km"
        terminal = (f"   {s.width} x {s.height} {launch.color_depth} {layout_tier(s.width, s.height)}"
                    f" {'standalone' if launch.host == 'standalone' else launch.transport}")
        if len(status) + len(terminal) <= wide:
            status += terminal            # what the door makes of this terminal, where there is room to say it
        out.append(s._line(base + 5, 1, status[:wide], "dim"))
        out += s._keys(base + 6, self._key_help(phase))
        return out

    def _key_help(self, phase: str):
        leave = ("Q", f"Return to {self.launch.node_name}")
        offered = self.offered(self.state)
        if phase == "aloft":
            keys = [("W/S", "Layer"), ("Enter", "Fly it")]
            if "jettison" in offered:
                keys.append(("J", "Jettison"))
            if "pibal" in offered:
                keys.append(("P", "Pibal"))
            if "land" in offered:
                keys.append(("L", "Land"))
            return keys + [("?", "Help"), ("Q", "Quit")]
        if phase == "ground":
            return [("Enter", "Launch"), ("N", "Night here"), ("?", "Help"), leave]
        if phase == "stranded":
            return [("W", "Walk out"), ("?", "Help"), leave]
        if phase == "ditched":
            return [("N", "Wait for the boat"), ("?", "Help"), leave]
        if phase == "wait":
            return [("?", "Help"), leave]
        return [("N", "Next day"), ("?", "Help"), leave]

    def _sentence(self, phase: str) -> tuple[str, str]:
        state = self.state
        if phase == "stranded":
            return "Stranded with no fuel. Walking out to the nearest town costs two days.", "warn"
        if phase == "ditched":
            return "Adrift. The packet boat will take a day to bring you to port.", "warn"
        if phase == "wait":
            opens = when_text(calendar_opens(self.calendar, state.day), self._local(), self.launch.datetime_format)
            return f"Every day that has opened is flown. Day {state.day} opens {opens}: come back then.", "accent"
        if phase == "rest":
            if state.grounded:
                return "The balloon is too worn to fly. A night at a town mends it.", "warn"
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
                "jettison": "Drop a sack for"}[choice]
        text = f"{verb} layer {layer} {LAYER_NAMES[layer - 1]}: {look['cost']} fuel."
        if look["points"]:
            ex, ey, _ = look["points"][-1]
            left, _, place = self.bearings(ex, ey)[0]
            minutes = len(look["points"]) * TURN_MINUTES
            text += f" In {minutes} min: {left:.0f} km from {place['name']}."
        stop = look["stop"]
        if stop == "ridge":
            return text + " High ground you cannot climb stops you.", "warn"
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
        read = layer == here or (here == 0 and layer == low) or state.pibal_until >= state.turn
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
        view.draw(screen, 1, 1, self.map_cols, self.map_rows, state.x, state.y, state.day, state.minute)
        if self._show_wind():
            if self.motion:
                self.frames_left = max(self.frames_left, LOOK_FRAMES)
            else:
                for _ in range(STREAK_TRAIL):     # no motion: a new still pattern for the new wind
                    view.advance()
        view.draw_wind(screen)
        # The balloon first: nothing may cover it, and names keep clear of it.
        view.mark(screen, state.x, state.y, "@", "bright", force=True)
        wanted, others = [], []
        for kind, group, glyph, ink in (("town", self.world.towns, "o", "text"),
                                        ("station", self.world.stations, "+", "cool")):
            for i, place in enumerate(group):
                (wanted if (kind, i) in self.targets else others).append((place, glyph, ink))
        cells = []
        for place, _, _ in wanted:
            cell = view.mark(screen, place["x"], place["y"], "#", "accent")
            cells.append((cell, place))
        for place, glyph, ink in others:
            view.mark(screen, place["x"], place["y"], glyph, ink)
        if state.status in ("aloft", "ground") and self.offered(state):
            order = [a for a in self.choices() if a != self.choice]
            if self.choice in ("sink", "hold", "climb", "launch", "jettison"):
                order.insert(0, self.choice)        # the chosen track first: where tracks share a cell, it shows
            for action in order:
                look = self.preview(action)
                ink = _TRACK_ROLES[action] if action == self.choice else "dim"
                for px, py, layer in look["points"]:
                    view.mark(screen, px, py, str(layer), ink)
        for cell, place in cells:                    # names last, where there is room left for them
            if cell is not None:
                view.label(screen, cell, place["name"], "accent")
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
            read = layer == here or (here == 0 and layer == low) or state.pibal_until >= state.turn
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
                read = layer == here or (here == 0 and layer == low) or state.pibal_until >= state.turn
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
        out.append(s._line(row, col, f"Fuel {state.fuel:>3}   Sacks {state.ballast}", "text"))
        out.append(s._line(row + 1, col, f"Pibals {state.pibals}   Wear {state.wear}", "text"))
        if state.pibal_until >= state.turn:
            out.append(s._line(row + 2, col, f"Pibal read: {state.pibal_until - state.turn + 1} turns", "cool"))
        left = max(0, DUSK_MINUTE - state.minute)
        light = f"Dusk in {left // 60}h{left % 60:02d}" if left else "Night"
        out.append(s._line(row + 3, col, light, "text" if left > 90 else "warn"))
        # The ground wind is the lowest layer's wind, known only as well as that layer is.
        lowest = self.layer_winds()[low]
        ground = math.sqrt(lowest[0] * lowest[0] + lowest[1] * lowest[1])
        out.append(s._line(row + 4, col, f"Ground wind {'' if lowest[2] else '~'}{ground:.0f} km/h", "text"))
        cloud, _ = frame.cloud(state.x, state.y)
        sky = "clear" if cloud < 0.2 else "broken cloud" if cloud < 0.6 else "overcast"
        if frame.fog(state.x, state.y) > FOG_THRESHOLD:
            sky = "fog on the ground"
        out.append(s._line(row + 5, col, f"Sky {sky}", "text"))
        out.append(s._line(row + 6, col, f"Forecast {'sharp' if cover > 0.66 else 'fair' if cover > 0.33 else 'vague'}", "dim"))
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

        bar(row + 1, "Fuel", state.fuel, FUEL_START, (255, 176, 84), f"{state.fuel}")
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
        s._line(row + 1, col, f"Sky {sky}. Forecast {'sharp' if reach > 0.66 else 'fair' if reach > 0.33 else 'vague'}.",
                "text")

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


PLAIN_HELP = (
    "A balloon cannot steer. It can only climb or sink, and every layer of air",
    "blows a different way. Each turn is fifteen minutes. Pick the layer whose",
    "wind takes you where you want to go, and carry the post to any place marked #.",
    "",
    "  W / S or arrows   look at the layer above or below; its track is drawn",
    "  Enter or Space    fly the chosen layer for one turn",
    "  J  drop a sack of ballast: up one layer for no fuel",
    "  P  release a pibal: true winds in every layer for four turns",
    "  L  land (from the lowest layer the ground leaves open)",
    "  N  stay the night   ?  this help   Ctrl-L  redraw   Q  leave (saved)",
    "",
    "The map: @ you, # places with post waiting, o towns, + weather stations,",
    "~ sea  . shore  , grass  \" forest  : dry plain  n hills  ^ mountain  A snow,",
    "% squall. Digits are tracks: where each layer would carry you, by forecast.",
    "A wind marked ~ is a forecast: you have not read that layer. Your own layer",
    "is always true. Climbing costs fuel, sinking is free, holding costs 1 (0 from",
    "11:00 to 15:00, 2 after 20:00). Land in 15 km/h or less for a clean landing.",
    "Within 12 km of a place counts as being there: two or three map cells.",
    "A world day opens at the same hour for everyone and is flown once; up to",
    "three wait for you. Leaving never loses anything: every turn is saved.",
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

    def draw(self, screen: Screen) -> None:
        for r in range(len(self.tops)):
            screen.pixel_row(0, r, self.tops[r], self.bottoms[r])


def draw_balloon(screen: Screen, cx: float, cy: float, r: float, light=(1.0, 1.0, 1.0), flame: bool = True) -> None:
    """The balloon, in pixels: a striped envelope lit from the right, ropes and a basket."""
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
            col = BALLOON_COLOURS[gore % 4]
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
                 caption: str = "", sub: str = "", flat: bool = False):
        self.kind, self.width, self.height = kind, width, height
        self.pixels, self.caption, self.sub = pixels, caption, sub
        self.hold = kind == "splash"
        moving = pixels and motion
        self.frames = {"splash": 16, "launch": 11, "landing": 14}[kind] if moving else 1
        # A still picture stays a moment, and so does a landing once it is down.
        self.linger = 0 if self.hold else 8 if not moving else 4 if kind == "landing" else 0
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
        if self.kind == "splash":
            rest = title_y + 7 * scale + 3 + r          # it comes to rest under the title, clear of it
            cx, cy = w * 0.74, back.h + r + (rest - back.h - r) * t
        elif self.kind == "launch":
            cx, cy = w * 0.34 + w * 0.1 * t, ground + (-r * 2.5 - ground) * (t * t)
        else:
            cx, cy = w * 0.24 + w * 0.1 * t, -r * 1.5 + (ground + r * 1.5) * t
        night = back.hour < 5.5 or back.hour >= 20.5
        draw_balloon(screen, cx, cy, r, (0.5, 0.52, 0.66) if night else (1.0, 1.0, 1.0),
                     flame=self.kind != "landing" or t < 0.9)
        if self.kind == "splash":
            self._title(screen, max(2, int(w * 0.06)), title_y, "FAIR WINDS", scale)
            row = (title_y + 7 * scale) // 2 + 2
            self._over(screen, max(2, int(w * 0.06)), row, "Read the wind. Carry the post. Finish the map.", "bright")
            if self.index >= self.frames - 1:
                self._over(screen, max(2, int(w * 0.06)), h - 2, "Press any key", "accent")
        else:
            self._over(screen, 2, h - 3, self.caption, "bright")
            self._over(screen, 2, h - 2, self.sub, "accent")

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
            text = PLAIN_HELP
        out = [self._line(0, 1, "FAIR WINDS", "accent"), self._line(0, 13, "Help", "dim")]
        out += [self._line(2 + i, 1, t) for i, t in enumerate(text)]
        out += self._keys(self.height - 1, [("B", "Back"), ("Q", "Back")])
        return out

    def _keys(self, row: int, keys) -> list[str]:
        out, col = [], 1
        for key, label in keys:
            out.append(self._line(row, col, "[", "dim"))
            out.append(self._line(row, col + 1, key, "accent"))
            out.append(self._line(row, col + 1 + len(key), "]", "dim"))
            out.append(self._line(row, col + 3 + len(key), label))
            col += len(key) + len(label) + 6
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
                           flat=self.launch.color_depth != "truecolor")
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
    args = parser.parse_args(argv)
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
