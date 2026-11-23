"""S02: the world generator and the terrain cache."""
import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import DOOR, ROOT

import fair_winds as fw

# Pinned so that Linux and Windows, and every later change, must agree on
# the same bytes. Changing the generator means changing BAKE_VERSION and
# these, on purpose.
GOLDEN_NODE = "golden-node"
GOLDEN_TILE = "8fe333df5bf3bfa26e01c29ca0bcdf5e2428a5f62b9ba04813a2289b4036bd73"
GOLDEN_INDEX = "87aef29ca9363f5213d61536b8159fbf66c21ff7b5e21d8723c92bd702008703"

_FINGERPRINT = """
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import fair_winds as fw
world = fw.World(fw.world_seed(sys.argv[2]))
print(hashlib.sha256(world._tile_bytes(world.tile(8, 6))).hexdigest())
print(hashlib.sha256(repr((world.sea_level, world.towns, world.stations, world.gyre, world.features,
                           world.chart_elevation, bytes(world.chart_biome))).encode()).hexdigest())
"""


def fingerprint(node_id):
    done = subprocess.run([sys.executable, "-c", _FINGERPRINT, str(ROOT), node_id],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def cell_distance(a, b):
    return ((a % fw.CHART_W - b % fw.CHART_W) ** 2 + (a // fw.CHART_W - b // fw.CHART_W) ** 2) ** 0.5


class Determinism(unittest.TestCase):
    def test_seed(self):
        self.assertEqual(fw.world_seed("a"), fw.world_seed("a"))
        self.assertNotEqual(fw.world_seed("a"), fw.world_seed("b"))
        self.assertNotEqual(fw.world_seed("a", 0), fw.world_seed("a", 1))
        self.assertLess(fw.world_seed("a"), 2 ** 64)

    def test_draws_are_keyed(self):
        self.assertEqual(fw.draw(7, "town", 3), fw.draw(7, "town", 3))
        self.assertNotEqual(fw.draw(7, "town", 3), fw.draw(7, "town", 4))
        self.assertNotEqual(fw.draw(7, "town", 3), fw.draw(7, "station", 3))
        self.assertNotEqual(fw.draw(7, "town", 3), fw.draw(8, "town", 3))
        self.assertTrue(all(0.0 <= fw.draw_unit(1, "x", i) < 1.0 for i in range(200)))

    def test_two_processes_agree_and_match_the_pinned_bytes(self):
        first, second = fingerprint(GOLDEN_NODE), fingerprint(GOLDEN_NODE)
        self.assertEqual(first, second, "two processes bake the same world")
        self.assertEqual(first, [GOLDEN_TILE, GOLDEN_INDEX], "every platform bakes the same bytes")

    def test_different_seeds_differ(self):
        a, b = fw.World(fw.world_seed("one")), fw.World(fw.world_seed("two"))
        self.assertNotEqual(a.tile(8, 6), b.tile(8, 6))
        self.assertNotEqual(a.chart_elevation, b.chart_elevation)
        self.assertNotEqual([t["cell"] for t in a.towns], [t["cell"] for t in b.towns])

    def test_no_maths_library_in_the_generator(self):
        # sin, pow, exp and friends may differ in the last bit between
        # platforms, which would break byte-identical tiles.
        source = DOOR.read_text(encoding="utf-8")
        start, end = source.index("# The world: one continent per seed"), source.index("def world_map(")
        self.assertNotIn("math.", source[start:end])
        self.assertNotIn("**0.5", source[start:end].replace(" ", ""))


class Shape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worlds = [fw.World(fw.world_seed(f"survey-{i}")) for i in range(50)]

    def test_documented_ranges_hold_for_fifty_seeds(self):
        for world in self.worlds:
            with self.subTest(seed=f"{world.seed:016x}"):
                self.assertTrue(0.38 <= world.land_fraction <= 0.52, world.land_fraction)
                self.assertTrue(14 <= len(world.towns) <= 18, len(world.towns))
                self.assertTrue(200 <= len(world.features) <= 260, len(world.features))
                self.assertEqual(len(world.stations), 3)

    def test_towns_are_on_the_mainland_and_apart(self):
        for world in self.worlds:
            land = world._components(lambda k: world.chart_biome[k] >= fw.SHORE)
            main = set(max(land, key=len))
            cells = [t["cell"] for t in world.towns]
            with self.subTest(seed=f"{world.seed:016x}"):
                self.assertEqual(len(set(t["name"] for t in world.towns + world.stations)),
                                 len(world.towns) + len(world.stations), "names are unique")
                for town in world.towns:
                    self.assertIn(town["cell"], main, "every town can be reached overland")
                    self.assertLess(world.chart_biome[town["cell"]], fw.MOUNTAIN)
                    self.assertGreaterEqual(world.sample(town["x"], town["y"])[0], -200, "not out at sea")
                for i, a in enumerate(cells):
                    for b in cells[i + 1:]:
                        self.assertGreaterEqual(cell_distance(a, b), fw.TOWN_SPACING, "at least 80 km apart")
                self.assertTrue(world.towns[world.start_town]["port"] or not any(t["port"] for t in world.towns))

    def test_stations_stand_near_the_start_town(self):
        for world in self.worlds:
            start = world.towns[world.start_town]["cell"]
            for station in world.stations:
                self.assertTrue(4 <= cell_distance(station["cell"], start) <= 12)
                self.assertGreaterEqual(world.chart_biome[station["cell"]], fw.SHORE)

    def test_the_gyre_is_inland_and_far_from_the_start(self):
        for world in self.worlds:
            start = world.towns[world.start_town]["cell"]
            self.assertGreaterEqual(world.chart_biome[world.gyre["cell"]], fw.SHORE)
            self.assertGreaterEqual(cell_distance(world.gyre["cell"], start), 30, "300 km or more")

    def test_the_edge_of_the_world_is_sea(self):
        for world in self.worlds[:10]:
            for k, biome in enumerate(world.chart_biome):
                x, y = k % fw.CHART_W, k // fw.CHART_W
                if x in (0, fw.CHART_W - 1) or y in (0, fw.CHART_H - 1):
                    self.assertLess(biome, fw.SHORE)

    def test_features_do_not_overlap_within_a_kind_and_stay_on_the_chart(self):
        world = self.worlds[0]
        for kind in {f["kind"] for f in world.features}:
            seen = set()
            for feature in (f for f in world.features if f["kind"] == kind):
                self.assertTrue(feature["cells"])
                self.assertTrue(all(0 <= k < fw.CHART_W * fw.CHART_H for k in feature["cells"]))
                self.assertFalse(seen & set(feature["cells"]))
                seen |= set(feature["cells"])
        self.assertEqual([f["id"] for f in world.features], list(range(len(world.features))))

    def test_heights_and_biomes(self):
        self.assertEqual((fw.metres(0.0), fw.metres(1.0), fw.metres(5.0), fw.metres(-9.0)), (0, 4000, 4500, -3000))
        self.assertEqual(fw.biome_of(-500, 0.5), fw.SEA_DEEP)
        self.assertEqual(fw.biome_of(-5, 0.5), fw.SEA_SHALLOW)
        self.assertEqual(fw.biome_of(10, 0.5), fw.SHORE)
        self.assertEqual((fw.biome_of(200, 0.3), fw.biome_of(200, 0.5), fw.biome_of(200, 0.7)),
                         (fw.DRY, fw.GRASS, fw.FOREST))
        self.assertEqual((fw.biome_of(500, 0.5), fw.biome_of(1500, 0.5), fw.biome_of(2800, 0.5)),
                         (fw.HILLS, fw.MOUNTAIN, fw.SNOW))

    def test_tiles_agree_with_the_chart(self):
        world = self.worlds[0]
        land = total = 0
        for ty in range(0, 12, 3):
            for tx in range(0, fw.TILES_X, 3):
                tile = world.tile(tx, ty)
                land += sum(1 for b in tile.biome if b >= fw.SHORE)
                total += len(tile.biome)
        self.assertAlmostEqual(land / total, world.land_fraction, delta=0.12)
        self.assertEqual(world.sample(-1, 5), (-3000, fw.SEA_DEEP))
        self.assertEqual(world.sample(5, 1000.0), (-3000, fw.SEA_DEEP))
        with self.assertRaises(IndexError):
            world.tile(fw.TILES_X, 0)


class Cache(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.folder = Path(self._dir.name)
        self.seed = fw.world_seed("cache-node")
        self.fresh = fw.World(self.seed)

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def world(self):
        return fw.World(self.seed, self.folder)

    def test_a_cached_world_equals_a_fresh_one(self):
        first = self.world()
        tile = first.tile(5, 5)
        again = self.world()
        self.assertEqual(again.tile(5, 5), tile)
        self.assertEqual(tile, self.fresh.tile(5, 5))
        self.assertEqual((again.towns, again.features, again.gyre, again.sea_level),
                         (self.fresh.towns, self.fresh.features, self.fresh.gyre, self.fresh.sea_level))
        self.assertEqual(again.chart_biome, self.fresh.chart_biome)

    def test_a_cached_tile_is_read_not_baked(self):
        self.world().tile(5, 5)
        world = self.world()
        world.bake_tile = None          # reading must not need it
        self.assertEqual(world.tile(5, 5), self.fresh.tile(5, 5))

    def test_damaged_files_are_rebuilt_never_trusted(self):
        self.world().tile(5, 5)
        folder = next(self.folder.iterdir())
        for name in ("t05_05.bin", "index.json"):
            path = folder / name
            good = path.read_bytes()
            damaged = [good[:-9], good[:40], b"", good[:100] + bytes([good[100] ^ 1]) + good[101:], b"x" * len(good)]
            for bad in damaged:
                path.write_bytes(bad)
                world = self.world()
                self.assertEqual(world.tile(5, 5), self.fresh.tile(5, 5))
                self.assertEqual(world.towns, self.fresh.towns)
                self.assertEqual(path.read_bytes(), good, "the rebuilt file replaces the damaged one")

    def test_a_tile_from_another_place_or_world_is_refused(self):
        world = self.world()
        world.tile(5, 5)
        world.tile(6, 5)
        folder = next(self.folder.iterdir())
        (folder / "t05_05.bin").write_bytes((folder / "t06_05.bin").read_bytes())   # valid checksum, wrong tile
        self.assertEqual(self.world().tile(5, 5), self.fresh.tile(5, 5))
        other = fw.World(fw.world_seed("other-node"), self.folder)
        self.assertNotEqual(other.cache_dir, world.cache_dir, "each world has its own folder")

    def test_an_unwritable_cache_only_costs_speed(self):
        blocker = self.folder / "not-a-folder"
        blocker.write_text("in the way", encoding="utf-8")
        world = fw.World(self.seed, blocker)
        self.assertEqual(world.tile(5, 5), self.fresh.tile(5, 5))
        self.assertEqual(world.towns, self.fresh.towns)

    def test_no_stray_files(self):
        world = self.world()
        world.tile(1, 1)
        names = sorted(p.name for p in next(self.folder.iterdir()).iterdir())
        self.assertEqual(names, ["index.json", "t01_01.bin"])


class Speed(unittest.TestCase):
    """Loose bounds for shared CI runners. Measured on the development host:
    a tile bakes in about 5 ms and reads in under 1 ms (targets: 300 ms, 2 ms)."""

    def test_bake_and_read(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            world = fw.World(fw.world_seed("speed"), folder)
            started = time.perf_counter()
            for tx in range(5):
                world.tile(tx, 6)
            bake = (time.perf_counter() - started) / 5
            again = fw.World(fw.world_seed("speed"), folder)
            started = time.perf_counter()
            for tx in range(5):
                again.tile(tx, 6)
            read = (time.perf_counter() - started) / 5
            self.assertLess(bake, 0.3)
            self.assertLess(read, 0.02)


class CommandLine(unittest.TestCase):
    def test_world_map(self):
        done = subprocess.run([sys.executable, str(DOOR), "--world-map", "node-abc"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("the Gyre", done.stdout)
        self.assertIn("towns:", done.stdout)


if __name__ == "__main__":
    unittest.main()
