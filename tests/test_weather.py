"""S03: the weather model."""
import math
import random
import re
import subprocess
import sys
import time
import unittest

from support import DOOR, ROOT

import fair_winds as fw

GOLDEN_NODE = "golden-node"
GOLDEN_WEATHER = "59834eae411c2721b49983923f5c2b4d656cf9bd76f0a7efcf9c8ad66c2fc3cc"

# Documented bounds (docs/systems.md, section 4).
STEP_BOUND = 6.0        # km/h between one turn and the next, same place
SPACE_BOUND = 35.0      # km/h between places 10 km apart, in open air

_FINGERPRINT = """
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import fair_winds as fw
weather = fw.Weather(fw.World(fw.world_seed(sys.argv[2])))
out = []
for day in (1, 17, 29, 200):
    for minute in (0, 375, 900):
        frame = weather.frame(day, minute)
        for x, y in ((120.0, 130.0), (640.5, 512.25), (1333.0, 777.0)):
            out.append([frame.wind(x, y, layer) for layer in range(1, 7)])
            out.append((frame.pressure(x, y), frame.cloud(x, y), frame.squall(x, y), frame.fog(x, y)))
            out.append(weather.forecast(day, minute, x, y, 3, 240, 0.25))
print(hashlib.sha256(repr(out).encode()).hexdigest())
"""


def fingerprint():
    done = subprocess.run([sys.executable, "-c", _FINGERPRINT, str(ROOT), GOLDEN_NODE],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def speed(v):
    return math.hypot(v[0], v[1])


def gap(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class WeatherCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = fw.World(fw.world_seed("weather-node"))
        cls.weather = fw.Weather(cls.world)
        rng = random.Random(20261123)       # test sampling only; the game never does this
        cls.points = [(rng.randint(1, 365), rng.randrange(0, 1440, 15), rng.uniform(50, 1550),
                       rng.uniform(50, 950), rng.randint(1, 6)) for _ in range(4000)]

    def open_air(self, x, y, layer):
        cell = self.weather.cell(x, y)
        if cell is None:
            return False
        if layer > 3:
            return True
        return self.world.chart_elevation[cell] < fw.LAYER_BLOCK_M[layer - 1] and not self.weather.gap[cell]


class Determinism(WeatherCase):
    def test_two_processes_agree_and_match_the_pinned_value(self):
        first, second = fingerprint(), fingerprint()
        self.assertEqual(first, second)
        self.assertEqual(first, GOLDEN_WEATHER, "every platform computes the same weather")

    def test_nothing_is_remembered(self):
        # A weather that has been asked a thousand other things answers the same.
        used = fw.Weather(self.world)
        for day, minute, x, y, layer in self.points[:1000]:
            used.wind(day, minute, x, y, layer)
            used.forecast(day, minute, x, y, layer, 300, 0.3)
        fresh = fw.Weather(self.world)
        for day, minute, x, y, layer in self.points[1000:1200]:
            self.assertEqual(used.wind(day, minute, x, y, layer), fresh.wind(day, minute, x, y, layer))
            self.assertEqual(used.forecast(day, minute, x, y, layer, 300, 0.3),
                             fresh.forecast(day, minute, x, y, layer, 300, 0.3))

    def test_only_the_square_root_comes_from_the_maths_library(self):
        source = DOOR.read_text(encoding="utf-8")
        section = source[source.index("# Weather: a pure function"):source.index("# Flight: the turn engine")]
        self.assertEqual(set(re.findall(r"math\.(\w+)", section)), {"sqrt"})
        for word in ("open(", "write", "sqlite", "time.", "random"):
            self.assertNotIn(word, section, "the weather reads and writes nothing")

    def test_minutes_past_midnight_roll_into_the_next_day(self):
        self.assertIs(self.weather.frame(5, 1440 + 30), self.weather.frame(6, 30))
        self.assertEqual(self.weather.wind(5, 1500, 400, 400, 3), self.weather.wind(6, 60, 400, 400, 3))


class Wind(WeatherCase):
    def test_speeds_stay_inside_each_layers_range_over_a_year(self):
        total, count = [0.0] * 6, [0] * 6
        for day, minute, x, y, layer in self.points:
            s = speed(self.weather.wind(day, minute, x, y, layer))
            self.assertLessEqual(s, fw.WIND_CAP[layer - 1] + 1e-9)
            total[layer - 1] += s
            count[layer - 1] += 1
        means = [t / c for t, c in zip(total, count)]
        self.assertEqual(means, sorted(means), "higher layers are faster on average")
        for mean, base in zip(means, fw.WIND_BASE):
            self.assertTrue(0.6 * base <= mean <= 1.5 * base, (mean, base))

    def test_continuous_from_turn_to_turn(self):
        worst = 0.0
        for day, minute, x, y, layer in self.points:
            worst = max(worst, gap(self.weather.wind(day, minute, x, y, layer),
                                   self.weather.wind(day, minute + fw.TURN_MINUTES, x, y, layer)))
        self.assertLess(worst, STEP_BOUND)

    def test_continuous_across_midnight_and_across_seasons(self):
        for day in (1, 2, 27, 28, 29, 56, 57, 84, 85, 112, 113, 364):
            for layer in range(1, 7):
                for x, y in ((300.0, 300.0), (800.0, 500.0), (1300.0, 700.0)):
                    step = gap(self.weather.wind(day, 1425, x, y, layer), self.weather.wind(day + 1, 0, x, y, layer))
                    self.assertLess(step, STEP_BOUND, (day, layer))

    def test_continuous_from_place_to_place_in_open_air(self):
        worst = 0.0
        for day, minute, x, y, layer in self.points:
            if self.open_air(x, y, layer) and self.open_air(x + 10, y, layer):
                worst = max(worst, gap(self.weather.wind(day, minute, x, y, layer),
                                       self.weather.wind(day, minute, x + 10, y, layer)))
        self.assertLess(worst, SPACE_BOUND)

    def test_layers_blow_different_ways(self):
        # The whole game: on most days the layers above a place offer a choice.
        varied = 0
        days = range(1, 121)
        for day in days:
            frame = self.weather.frame(day, 720)
            names = {fw.compass(*frame.wind(700.0, 500.0, layer)) for layer in range(1, 7)}
            varied += len(names) >= 3
        self.assertGreater(varied, len(days) * 0.9)

    def test_terrain_fills_the_low_layers(self):
        elevation = self.world.chart_elevation
        high = next(k for k, m in enumerate(elevation) if m >= 1500)
        x, y = (high % fw.CHART_W) * 10 + 5.0, (high // fw.CHART_W) * 10 + 5.0
        for day in (3, 40, 90):
            frame = self.weather.frame(day, 600)
            for layer in (1, 2):
                self.assertLessEqual(speed(frame.wind(x, y, layer)), fw.WIND_CAP[layer - 1] * fw.BLOCKED_FACTOR + 1e-9)

    def test_off_the_chart_there_is_still_wind(self):
        self.assertGreater(speed(self.weather.wind(10, 600, -50.0, 500.0, 4)), 0.0)

    def test_sea_breeze_and_downslope_wind_follow_the_clock(self):
        frames = {hour: self.weather.frame(10, hour * 60) for hour in (3, 6, 9, 15, 21)}
        self.assertEqual(frames[15].sea_breeze, 1.0)
        self.assertEqual((frames[3].sea_breeze, frames[21].sea_breeze), (0.0, 0.0))
        self.assertEqual(frames[6].katabatic, 1.0)
        self.assertEqual(frames[15].katabatic, 0.0)
        self.assertGreater(frames[3].land_breeze, 0.9)
        self.assertEqual(frames[15].land_breeze, 0.0)

    def test_coast_and_slope_fields(self):
        weather, world = self.weather, self.world
        coastal = [k for k, v in enumerate(weather.coast) if speed(v) > 0.8]
        self.assertTrue(coastal)
        for k in coastal[::25]:
            x, y = k % fw.CHART_W, k // fw.CHART_W
            vx, vy = weather.coast[k]
            ahead = (min(fw.CHART_H - 1, max(0, round(y + vy * 3))) * fw.CHART_W
                     + min(fw.CHART_W - 1, max(0, round(x + vx * 3))))
            behind = (min(fw.CHART_H - 1, max(0, round(y - vy * 3))) * fw.CHART_W
                      + min(fw.CHART_W - 1, max(0, round(x - vx * 3))))
            self.assertGreaterEqual(world.chart_biome[ahead] >= fw.SHORE, world.chart_biome[behind] >= fw.SHORE,
                                    "the coast vector points inland")
        self.assertEqual(weather.coast[0], (0.0, 0.0), "open sea has no sea breeze")
        self.assertTrue(all(speed(v) <= 1.0 + 1e-9 for v in weather.slope))
        self.assertGreater(sum(weather.gap), 0, "some passes funnel the wind")


class Systems(WeatherCase):
    def test_every_system_crosses_the_continent_in_three_to_six_days(self):
        weather, seen = self.weather, 0
        for slot in range(0, 240):
            system = weather._slot(slot)
            if system is None:
                continue
            seen += 1
            life, radius = system[0], system[1]
            self.assertTrue(3.0 <= life <= 6.0)
            self.assertTrue(200.0 <= radius <= 320.0)
            born = slot * fw.SYSTEM_SLOT_DAYS
            first = [s for s in weather._systems(born + 1e-6, 0.0)][:-1]
            last = [s for s in weather._systems(born + life - 1e-6, 0.0)][:-1]
            self.assertTrue(any(abs(s[0] + radius) < 1.0 for s in first), "enters at the west edge")
            self.assertTrue(any(abs(s[0] - (fw.WORLD_W + radius)) < 1.0 for s in last), "leaves at the east edge")
        self.assertTrue(180 <= seen <= 225, "about one system every day and a half")

    def test_systems_fade_in_and_out(self):
        weather = self.weather
        slot = next(k for k in range(10, 200) if weather._slot(k))
        born = slot * fw.SYSTEM_SLOT_DAYS
        life, radius, strength = weather._slot(slot)[:3]

        def felt(age):
            return min((abs(s[0] - (-radius + (fw.WORLD_W + 2 * radius) * age / life)), s[3])
                       for s in weather._systems(born + age, 0.0)[:-1])[1]
        self.assertLess(felt(0.01), strength * 0.01)
        self.assertAlmostEqual(felt(life / 2), strength, places=6)
        self.assertLess(felt(life - 0.01), strength * 0.01)

    def test_the_gyre_is_always_there_and_is_the_deepest_low(self):
        gyre = self.world.gyre
        for day in (1, 50, 300):
            frame = self.weather.frame(day, 600)
            self.assertEqual(frame.systems[-1][:2], (gyre["x"], gyre["y"]))
            here = frame.pressure(gyre["x"], gyre["y"])
            self.assertLess(here, 940.0)
            self.assertLess(here, min(frame.pressure(x, y) for x in (100.0, 800.0, 1500.0) for y in (100.0, 900.0)
                                      if math.hypot(x - gyre["x"], y - gyre["y"]) > 300))
            ring = speed(frame.wind(gyre["x"] + 115.0, gyre["y"], 4))
            self.assertGreater(ring, 80.0, "the Gyre's ring is fast")
            self.assertLess(speed(frame.wind(gyre["x"], gyre["y"], 4)), ring, "and its eye is calmer")

    def test_seasons_change_the_prevailing_wind(self):
        regimes = {self.weather._season(s)[0] for s in range(12)}
        self.assertEqual(len(regimes), 12)
        self.assertIn(4, {self.weather._icing(1 + s * 28) for s in range(40)})
        self.assertIn(5, {self.weather._icing(1 + s * 28) for s in range(40)})


class Forecast(WeatherCase):
    def error(self, lead, coverage, points=None):
        points = points or self.points[:1200]
        return sum(gap(self.weather.wind(*p), self.weather.forecast(*p, lead, coverage)) for p in points) / len(points)

    def test_no_lead_is_the_truth(self):
        for p in self.points[:300]:
            self.assertEqual(self.weather.forecast(*p, 0, 0.0), self.weather.wind(*p))

    def test_at_a_station_the_forecast_is_the_truth(self):
        for p in self.points[:300]:
            self.assertEqual(self.weather.forecast(*p, 600, 1.0), self.weather.wind(*p))

    def test_error_grows_with_lead_time(self):
        errors = [self.error(lead, 0.0) for lead in (0, 30, 60, 180, 360, 720, 1440)]
        self.assertEqual(errors[0], 0.0)
        self.assertEqual(errors, sorted(errors))
        self.assertGreater(errors[3], 3.0, "three hours ahead is visibly uncertain")
        self.assertLess(errors[1], 1.5, "the next half hour is nearly sure")

    def test_error_shrinks_under_station_coverage(self):
        for lead in (60, 360, 1440):
            errors = [self.error(lead, coverage) for coverage in (0.0, 0.25, 0.5, 0.75, 1.0)]
            self.assertEqual(errors, sorted(errors, reverse=True), lead)
            self.assertEqual(errors[-1], 0.0)

    def test_every_caller_sees_the_same_forecast(self):
        other = fw.Weather(self.world)
        for p in self.points[:200]:
            self.assertEqual(self.weather.forecast(*p, 240, 0.4), other.forecast(*p, 240, 0.4))

    def test_lead_error_table(self):
        self.assertEqual(fw.lead_error_km(0), 0.0)
        self.assertEqual(fw.lead_error_km(-5), 0.0)
        self.assertAlmostEqual(fw.lead_error_km(360), 100.0)
        self.assertEqual(fw.lead_error_km(99999), 400.0)


class OtherFields(WeatherCase):
    def test_ranges(self):
        for day, minute, x, y, _ in self.points[:800]:
            frame = self.weather.frame(day, minute)
            cover, deck = frame.cloud(x, y)
            self.assertTrue(0.0 <= cover <= 1.0)
            self.assertIn(deck, (3, 4))
            self.assertTrue(0.0 <= frame.squall(x, y) <= 1.0)
            self.assertTrue(0.0 <= frame.fog(x, y) <= 1.0)
            self.assertTrue(880.0 < frame.pressure(x, y) < 1050.0)
            self.assertAlmostEqual(frame.surface_speed(x, y), speed(frame.wind(x, y, 1)))

    def test_sky_is_neither_always_clear_nor_always_covered(self):
        covers = [self.weather.frame(day, 720).cloud(x, y)[0]
                  for day in range(1, 30, 3) for x in range(100, 1600, 150) for y in range(100, 1000, 150)]
        clear = sum(c < 0.1 for c in covers) / len(covers)
        self.assertTrue(0.2 < clear < 0.8, clear)

    def test_squalls_ring_the_lows(self):
        hit = 0
        for day in range(1, 40):
            frame = self.weather.frame(day, 900)
            for sx, sy, inv_r, strength, turn in frame.systems[:-1]:
                if turn < 0:
                    self.assertEqual(frame._low(sx, sy), frame._low(sx, sy))
                    continue
                radius = 1.0 / inv_r
                ring = max(frame.squall(sx + radius * 0.67 * dx, sy + radius * 0.67 * dy)
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, -0.7)))
                far = frame.squall(sx + radius * 3.0, sy)
                hit += ring > far
        self.assertGreater(hit, 10)

    def test_fog_is_a_dawn_thing_over_calm_lowland(self):
        dawn = noon = high = 0
        for day in range(1, 12):
            early, midday = self.weather.frame(day, 390), self.weather.frame(day, 720)
            for k, m in enumerate(self.world.chart_elevation):
                if k % 7:
                    continue
                x, y = (k % fw.CHART_W) * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
                f = early.fog(x, y)
                if f > 0.3:
                    dawn += 1
                    if not 0 <= m < 250:
                        high += 1
                noon += midday.fog(x, y) > 0.0
        self.assertGreater(dawn, 0)
        self.assertEqual(noon, 0)
        self.assertEqual(high, 0)


class Compass(unittest.TestCase):
    def test_sixteen_points_named_by_where_the_wind_comes_from(self):
        names = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
        for i, name in enumerate(names):
            angle = math.radians(i * 22.5)                  # bearing the wind comes from
            vx, vy = -math.sin(angle), math.cos(angle)      # so it goes the other way; south is +y
            self.assertEqual(fw.compass(vx, vy), name)
            for nudge in (-9, 9):
                a = math.radians(i * 22.5 + nudge)
                self.assertEqual(fw.compass(-math.sin(a) * 30, math.cos(a) * 30), name)
        self.assertEqual(fw.compass(0.0, 0.0), "calm")
        self.assertEqual(fw.compass(40.0, 0.0), "W", "air going east is a west wind")


class Cost(WeatherCase):
    """Target: under 60 microseconds a sample on the development host, where
    it measures about 20. The bound here is loose for shared CI runners."""

    def test_a_sample_is_cheap(self):
        frame = self.weather.frame(77, 615)
        frame.wind(500.0, 500.0, 3)
        started = time.perf_counter()
        n = 0
        for i in range(6000):
            frame.wind(200.0 + (i % 1200), 150.0 + (i % 700), 1 + i % 6)
            n += 1
        self.assertLess((time.perf_counter() - started) / n, 250e-6)


if __name__ == "__main__":
    unittest.main()
