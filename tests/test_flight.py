"""S04: the turn engine."""
import dataclasses
import json
import math
import unittest

import support  # noqa: F401

import fair_winds as fw


class StillFrame:
    """Weather held still, so a rule can be tested on its own."""

    def __init__(self, wind=(0.0, 0.0), surface=None, squall=0.0, fog=0.0, icing_layer=5, by_layer=None):
        self._wind, self._surface, self._squall, self._fog = wind, surface, squall, fog
        self.icing_layer, self._by_layer = icing_layer, by_layer or {}

    def wind(self, x, y, layer):
        return self._by_layer.get(layer, self._wind)

    def surface_speed(self, x, y):
        if self._surface is not None:
            return self._surface
        vx, vy = self.wind(x, y, 1)
        return math.hypot(vx, vy)

    def squall(self, x, y):
        return self._squall

    def fog(self, x, y):
        return self._fog


class StillWeather:
    def __init__(self, **kw):
        self._frame = StillFrame(**kw)

    def frame(self, day, minute, lead=0.0, coverage=1.0):
        return self._frame


WORLD = fw.World(fw.world_seed("flight-node"))
WEATHER = fw.Weather(WORLD)


def find_cell(test):
    for k in range(fw.CHART_W * fw.CHART_H):
        if test(k):
            return (k % fw.CHART_W) * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
    raise AssertionError("no such place in this world")


def lowland():
    """Open lowland with lowland all round, away from any town."""
    def test(k):
        x, y = k % fw.CHART_W, k // fw.CHART_W
        if not (10 < x < fw.CHART_W - 10 and 10 < y < fw.CHART_H - 10):
            return False
        near = [WORLD.chart_elevation[(y + dy) * fw.CHART_W + x + dx] for dx in range(-6, 7) for dy in range(-6, 7)]
        if not all(30 <= m < 350 for m in near):
            return False
        px, py = x * 10 + 5.0, y * 10 + 5.0
        return all(math.hypot(px - t["x"], py - t["y"]) > 60 for t in WORLD.towns + WORLD.stations)
    return find_cell(test)


def flight(**weather):
    return fw.Flight(WORLD, StillWeather(**weather) if weather else WEATHER, "node/12")


def aloft(x, y, **changes):
    values = dict(x=x, y=y, layer=1, status="aloft", turn=8)
    values.update(changes)
    return fw.FlightState(**values)


class Costs(unittest.TestCase):
    def setUp(self):
        self.x, self.y = lowland()
        self.flight = flight(wind=(0.0, 0.0))

    def test_starting_stores(self):
        state = fw.FlightState(x=self.x, y=self.y)
        self.assertEqual((state.fuel, state.ballast, state.pibals, state.wear), (60, 5, 3, 0))
        self.assertEqual((state.status, state.layer, state.minute), ("ground", 0, 360))

    def test_launch(self):
        state = fw.FlightState(x=self.x, y=self.y)
        self.assertEqual(self.flight.actions(state), {"launch": 2})
        after = self.flight.step(state, "launch").state
        self.assertEqual((after.status, after.layer, after.fuel, after.turn), ("aloft", 1, 58, 1))

    def test_hold_by_the_clock(self):
        def cost(hour, **changes):
            state = aloft(self.x, self.y, turn=(hour * 60 - fw.DAWN_MINUTE) // 15, **changes)
            return self.flight.actions(state)["hold"]
        self.assertEqual([cost(h) for h in (6, 10)], [1, 1])
        self.assertEqual([cost(h) for h in (11, 13, 14)], [0, 0, 0], "midday sun lifts for nothing")
        self.assertEqual([cost(h) for h in (15, 19)], [1, 1])
        self.assertEqual([cost(h) for h in (20, 23)], [2, 2], "night costs double")
        self.assertEqual(cost(22, night_gear=True), 1)

    def test_icing_doubles_the_hold(self):
        cold = flight(wind=(0.0, 0.0), icing_layer=2)
        self.assertEqual(cold.actions(aloft(self.x, self.y, layer=2))["hold"], 2)
        self.assertEqual(cold.actions(aloft(self.x, self.y, layer=1))["hold"], 1, "below the icing level")
        self.assertEqual(cold.actions(aloft(self.x, self.y, layer=2, iceproof=True))["hold"], 1)
        self.assertEqual(cold.actions(aloft(self.x, self.y, layer=2, turn=24))["hold"], 2, "even at midday")

    def test_climb_sink_and_the_ceiling(self):
        state = aloft(self.x, self.y, layer=2)
        offered = self.flight.actions(state)
        self.assertEqual((offered["climb"], offered["sink"], offered["jettison"]), (3, 0, 0))
        up = self.flight.step(state, "climb").state
        self.assertEqual((up.layer, up.fuel), (3, 57))
        self.assertNotIn("climb", self.flight.actions(up), "envelope I stops at layer 3")
        self.assertNotIn("jettison", self.flight.actions(up))
        down = self.flight.step(state, "sink").state
        self.assertEqual((down.layer, down.fuel), (1, 60))
        self.assertNotIn("sink", self.flight.actions(down), "there is nothing below layer 1 but the ground")

    def test_the_high_layers_cost_more_and_need_the_gear(self):
        high = aloft(self.x, self.y, layer=4, ceiling=6)
        self.assertEqual(self.flight.actions(high)["climb"], 4, "into layer 5")
        five = aloft(self.x, self.y, layer=5, ceiling=6)
        self.assertNotIn("climb", self.flight.actions(five), "layer 6 needs oxygen")
        self.assertEqual(self.flight.actions(dataclasses.replace(five, oxygen=True))["climb"], 5, "into layer 6")
        self.assertEqual(self.flight.actions(aloft(self.x, self.y, layer=2, climb_cost=2))["climb"], 2)

    def test_jettison_is_a_free_climb_that_spends_a_sack(self):
        state = aloft(self.x, self.y, fuel=0)
        after = self.flight.step(state, "jettison").state
        self.assertEqual((after.layer, after.ballast, after.fuel), (2, 4, 0))
        self.assertNotIn("jettison", self.flight.actions(aloft(self.x, self.y, ballast=0)))

    def test_a_pibal_takes_no_time_and_lasts_four_turns(self):
        state = aloft(self.x, self.y)
        result = self.flight.step(state, "pibal")
        self.assertEqual((result.state.turn, result.state.pibals, result.state.pibal_until), (8, 2, 11))
        self.assertNotIn("pibal", self.flight.actions(result.state), "one reading at a time")
        later = dataclasses.replace(result.state, turn=12)
        self.assertIn("pibal", self.flight.actions(later))
        self.assertNotIn("pibal", self.flight.actions(aloft(self.x, self.y, pibals=0)))

    def test_what_is_not_on_offer_is_refused(self):
        state = aloft(self.x, self.y, layer=3)
        for action in ("climb", "launch", "fly", ""):
            with self.assertRaises(ValueError):
                self.flight.step(state, action)
        self.assertEqual(set(fw.ACTIONS), {"launch", "hold", "climb", "sink", "jettison", "land", "pibal"})

    def test_wear_grounds_the_balloon(self):
        worn = fw.FlightState(x=self.x, y=self.y, wear=100)
        self.assertTrue(worn.grounded)
        self.assertEqual(self.flight.actions(worn), {})
        self.assertEqual(self.flight.actions(fw.FlightState(x=self.x, y=self.y, fuel=1)), {}, "no fuel to launch")


class Drift(unittest.TestCase):
    def setUp(self):
        self.x, self.y = lowland()

    def test_fifteen_minutes_on_the_wind_of_the_new_layer(self):
        engine = flight(by_layer={1: (8.0, 0.0), 2: (0.0, -40.0)})
        held = engine.step(aloft(self.x, self.y), "hold")
        self.assertAlmostEqual(held.state.x, self.x + 2.0)
        self.assertAlmostEqual(held.state.y, self.y)
        self.assertEqual(held.events, (("hold", 1, 1), ("drift", 2.0, "W")))
        climbed = engine.step(aloft(self.x, self.y), "climb")
        self.assertAlmostEqual(climbed.state.y, self.y - 10.0, msg="it drifts on the layer it climbed into")
        self.assertEqual(climbed.events[1], ("drift", 10.0, "S"))
        self.assertAlmostEqual(climbed.state.distance, 10.0)

    def test_midnight_ends_the_day(self):
        engine = flight(wind=(0.0, 0.0))
        state = aloft(self.x, self.y, turn=fw.DAY_TURNS - 1, night_gear=True)
        result = engine.step(state, "hold")
        self.assertEqual(result.state.turn, fw.DAY_TURNS)
        self.assertEqual(result.state.status, "ground")
        self.assertIn(("midnight",), result.events)
        self.assertEqual(engine.actions(result.state), {}, "nothing more today")


class Terrain(unittest.TestCase):
    def setUp(self):
        self.engine = flight(wind=(0.0, 0.0))
        # Lowland with high ground 10 km to the east.
        def test(k):
            x = k % fw.CHART_W
            if x >= fw.CHART_W - 2:
                return False
            px, py = x * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            return self.engine.lowest_open(px, py) == 1 and self.engine.lowest_open(px + 10.0, py) == 2
        self.x, self.y = find_cell(test)
        self.east = flight(wind=(40.0, 0.0))

    def test_lowest_open_layer_follows_the_ground(self):
        heights = {1: 0, 2: 0, 3: 0, 4: 0}
        for k, m in enumerate(WORLD.chart_elevation):
            px, py = (k % fw.CHART_W) * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            low = self.engine.lowest_open(px, py)
            real = WORLD.sample(px, py)[0]
            self.assertEqual(low, 1 + sum(real >= b for b in (500, 1500, 2750)))
            heights[low] += 1
        self.assertGreater(heights[1], 0)
        self.assertGreater(heights[2], 0)

    def test_a_blocked_layer_cannot_be_entered(self):
        hill = aloft(self.x + 10.0, self.y, layer=2)
        self.assertNotIn("sink", self.engine.actions(hill), "layer 1 is full of hill here")
        self.assertIn("land", self.engine.actions(hill), "the lowest open layer is where you land from")
        ground = fw.FlightState(x=self.x + 10.0, y=self.y)
        self.assertEqual(self.engine.step(ground, "launch").state.layer, 2, "a launch from a hill starts above it")

    def test_high_ground_ahead_forces_a_climb_at_double_cost(self):
        result = self.east.step(aloft(self.x, self.y), "hold")
        self.assertEqual(result.state.layer, 2)
        self.assertEqual(result.state.fuel, 60 - 1 - 6)
        self.assertIn(("forced_climb", 2, 6), result.events)
        self.assertAlmostEqual(result.state.x, self.x + 10.0)

    def test_ballast_goes_when_the_fuel_cannot_pay(self):
        result = self.east.step(aloft(self.x, self.y, fuel=4), "hold")
        self.assertEqual((result.state.layer, result.state.fuel, result.state.ballast), (2, 3, 4))
        self.assertIn(("forced_jettison", 2), result.events)

    def test_with_neither_it_comes_down_on_the_high_ground(self):
        result = self.east.step(aloft(self.x, self.y, fuel=1, ballast=0, cargo=((1, False, "ok"), (2, True, "ok"))), "hold")
        self.assertEqual((result.state.status, result.state.layer, result.state.fuel), ("stranded", 0, 0))
        self.assertEqual(result.state.wear, 30, "a hard landing")
        self.assertEqual([e[0] for e in result.events][-3:], ["high_ground", "land", "stranded"])
        self.assertIn(("high_ground",), result.events)
        self.assertIn(("stranded",), result.events)
        self.assertEqual(self.east.actions(result.state), {}, "stranded: nothing to do but walk or wait")

    def test_ground_above_the_ceiling_holds_the_balloon_in_its_lee(self):
        def test(k):
            x = k % fw.CHART_W
            if x >= fw.CHART_W - 2:
                return False
            px, py = x * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            return self.engine.lowest_open(px, py) == 2 and self.engine.lowest_open(px + 10.0, py) >= 3
        x, y = find_cell(test)
        result = self.east.step(aloft(x, y, layer=2, ceiling=2), "hold")
        self.assertEqual((result.state.x, result.state.y, result.state.layer), (x, y, 2))
        self.assertEqual(result.state.wear, fw.RIDGE_WEAR)
        self.assertEqual(result.events[-1][0], "ridge")
        self.assertEqual(result.state.fuel, 59, "the turn is still paid for")


class Landing(unittest.TestCase):
    def setUp(self):
        self.x, self.y = lowland()
        self.cargo = ((1, False, "ok"), (2, True, "ok"), (3, False, "ok"), (4, True, "ok"))

    def land(self, **kw):
        state = aloft(self.x, self.y, **kw.pop("state", {}))
        engine = flight(**kw)
        return engine, engine.step(state, "land")

    def test_clean(self):
        engine, result = self.land(wind=(15.0, 0.0), state={"cargo": self.cargo})
        self.assertEqual(engine.landing_class(aloft(self.x, self.y)), "clean")
        self.assertEqual((result.state.status, result.state.layer, result.state.wear), ("ground", 0, 0))
        self.assertEqual(result.state.cargo, self.cargo)
        self.assertEqual(result.events, (("land", "clean", None),))
        self.assertEqual(result.state.turn, 9, "landing takes the turn")
        self.assertEqual((result.state.x, result.state.y), (self.x, self.y), "and there is no drift")

    def test_rough_damages_fragile_cargo(self):
        _, result = self.land(wind=(15.1, 0.0), state={"cargo": self.cargo})
        self.assertEqual(result.events[0], ("land", "rough", None))
        self.assertEqual(result.state.wear, 10)
        self.assertEqual([c[2] for c in result.state.cargo], ["ok", "damaged", "ok", "damaged"])

    def test_hard_is_refused_while_the_balloon_can_stay_up(self):
        engine = flight(wind=(30.1, 0.0))
        self.assertEqual(engine.landing_class(aloft(self.x, self.y)), "hard")
        self.assertNotIn("land", engine.actions(aloft(self.x, self.y)))
        self.assertIn("land", engine.actions(aloft(self.x, self.y, fuel=0, ballast=0)))
        self.assertIn("land", flight(wind=(30.0, 0.0)).actions(aloft(self.x, self.y)), "thirty is still only rough")

    def test_hard_loses_cargo_and_wears_the_balloon(self):
        cargo = tuple((i, i % 2 == 0, "ok") for i in range(40))
        _, result = self.land(wind=(45.0, 0.0), state={"cargo": cargo, "fuel": 0, "ballast": 0})
        self.assertEqual(result.events[0], ("land", "hard", None))
        self.assertEqual(result.state.wear, 30)
        self.assertTrue(8 <= len(result.state.cargo) <= 32, "about half is lost")
        self.assertTrue(all(c[2] == "damaged" for c in result.state.cargo if c[1]))
        self.assertEqual(result.state.status, "stranded", "down in the wild with no fuel")
        _, again = self.land(wind=(45.0, 0.0), state={"cargo": cargo, "fuel": 0, "ballast": 0})
        self.assertEqual(again.state.cargo, result.state.cargo, "which half is fixed, not rolled")

    def test_wear_stops_at_grounded(self):
        _, result = self.land(wind=(20.0, 0.0), state={"wear": 95})
        self.assertEqual(result.state.wear, 100)
        self.assertTrue(result.state.grounded)

    def test_night_and_fog_each_make_it_one_class_worse(self):
        night = {"turn": (21 * 60 - fw.DAWN_MINUTE) // 15}
        self.assertEqual(flight(wind=(5.0, 0.0)).landing_class(aloft(self.x, self.y, **night)), "rough")
        self.assertEqual(flight(wind=(5.0, 0.0)).landing_class(aloft(self.x, self.y, night_gear=True, **night)), "clean")
        self.assertEqual(flight(wind=(5.0, 0.0), fog=0.5).landing_class(aloft(self.x, self.y)), "rough")
        self.assertEqual(flight(wind=(5.0, 0.0), fog=0.5).landing_class(aloft(self.x, self.y, **night)), "hard")
        self.assertEqual(flight(wind=(5.0, 0.0), fog=0.3).landing_class(aloft(self.x, self.y)), "clean")

    def test_a_town_is_lit(self):
        town = WORLD.towns[WORLD.start_town]
        engine = flight(wind=(5.0, 0.0), fog=0.9)
        state = aloft(town["x"], town["y"], turn=(22 * 60 - fw.DAWN_MINUTE) // 15, fuel=0)
        self.assertEqual(engine.place(town["x"], town["y"]), ("town", WORLD.start_town))
        self.assertEqual(engine.landing_class(state), "clean", "neither night nor fog matters at a town")
        landed = engine.step(dataclasses.replace(state, layer=engine.lowest_open(state.x, state.y)), "land")
        self.assertEqual(landed.state.status, "ground", "out of fuel at a town is not stranded")
        self.assertEqual(landed.events[0], ("land", "clean", ("town", WORLD.start_town)))
        self.assertIsNone(engine.place(self.x, self.y))

    def test_water_is_a_ditching(self):
        engine = flight(wind=(0.0, 0.0))
        x, y = find_cell(lambda k: WORLD.chart_biome[k] == fw.SEA_DEEP and 5 < k % fw.CHART_W < 150
                         and 5 < k // fw.CHART_W < 95)
        state = aloft(x, y, cargo=self.cargo)
        self.assertEqual(engine.landing_class(state), "ditch")
        result = engine.step(state, "land")
        self.assertEqual((result.state.status, result.state.cargo), ("ditched", ()))
        self.assertEqual(result.events, (("ditch",),))
        self.assertEqual(engine.actions(result.state), {})


class OutOfFuel(unittest.TestCase):
    def test_it_sinks_a_layer_a_turn_and_lands_where_it_is(self):
        x, y = lowland()
        engine = flight(wind=(4.0, 0.0))
        state = aloft(x, y, layer=3, fuel=0, ballast=0)
        seen = []
        while state.status == "aloft":
            offered = engine.actions(state)
            self.assertTrue(offered, "there is always something to do")
            self.assertTrue(all(cost == 0 for cost in offered.values()))
            self.assertNotIn("hold", offered)
            action = "sink" if "sink" in offered else "land"
            seen.append(action)
            state = engine.step(state, action).state
        self.assertEqual(seen, ["sink", "sink", "land"])
        self.assertEqual(state.status, "stranded")

    def test_midday_sun_still_holds_an_empty_balloon_up(self):
        x, y = lowland()
        engine = flight(wind=(0.0, 0.0))
        self.assertEqual(engine.actions(aloft(x, y, fuel=0, ballast=0, turn=24)).get("hold"), 0)


class Hazards(unittest.TestCase):
    def setUp(self):
        self.x, self.y = lowland()

    def test_a_squall_shoves_and_wears(self):
        engine = flight(wind=(0.0, 0.0), squall=1.0)
        moved = set()
        for turn in range(8, 40):
            result = engine.step(aloft(self.x, self.y, layer=2, turn=turn), "hold")
            self.assertEqual(result.state.wear, fw.SQUALL_WEAR)
            self.assertEqual(result.events[-1][0], "squall")
            moved.add(result.state.layer)
        self.assertEqual(moved, {1, 3}, "up on some turns, down on others")
        calm = flight(wind=(0.0, 0.0), squall=0.4).step(aloft(self.x, self.y, layer=2), "hold")
        self.assertEqual((calm.state.layer, calm.state.wear), (2, 0), "at the threshold nothing happens")

    def test_a_squall_respects_ground_and_ceiling(self):
        engine = flight(wind=(0.0, 0.0), squall=1.0)
        for turn in range(8, 30):
            self.assertIn(engine.step(aloft(self.x, self.y, layer=1, turn=turn), "hold").state.layer, (1, 2))
            self.assertIn(engine.step(aloft(self.x, self.y, layer=3, turn=turn), "hold").state.layer, (2, 3))

    def test_the_same_turn_always_ends_the_same_way(self):
        engine = flight(wind=(0.0, 0.0), squall=1.0)
        state = aloft(self.x, self.y, layer=2)
        self.assertEqual(engine.step(state, "hold"), engine.step(state, "hold"))
        other = fw.Flight(WORLD, StillWeather(wind=(0.0, 0.0), squall=1.0), "node/13")
        differs = sum(engine.step(aloft(self.x, self.y, layer=2, turn=t), "hold").state.layer
                      != other.step(aloft(self.x, self.y, layer=2, turn=t), "hold").state.layer for t in range(8, 60))
        self.assertGreater(differs, 5, "another pilot has other luck")

    def test_rotor_in_the_lee_of_a_ridge(self):
        engine = flight(wind=(0.0, 0.0))
        # Over lowland, with ground close upwind (to the west) that reaches layer 1.
        def test(k):
            x = k % fw.CHART_W
            if x < 3:
                return False
            px, py = x * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            return (engine.lowest_open(px, py) == 1 and engine.lowest_open(px + 1.0, py) == 1
                    and WORLD.sample(px - fw.ROTOR_REACH_KM + 1.0, py)[0] >= 500)
        x, y = find_cell(test)
        lee = flight(wind=(4.0, 0.0))                                 # a west wind
        outcomes = [lee.step(aloft(x, y, turn=t), "hold") for t in range(8, 68)]
        rough = [r for r in outcomes if r.events[-1][0] == "rotor"]
        self.assertTrue(8 <= len(rough) <= 34, len(rough))
        self.assertTrue(all(r.state.layer == 1 and r.state.wear == fw.ROTOR_WEAR for r in rough),
                        "at the lowest layer it only wears")
        if WORLD.sample(x + fw.ROTOR_REACH_KM, y)[0] < 500:
            upwind = flight(wind=(-4.0, 0.0))                         # an east wind: the ridge is downwind
            self.assertFalse(any(upwind.step(aloft(x, y, turn=t), "hold").events[-1][0] == "rotor"
                                 for t in range(8, 68)))
        high = flight(wind=(4.0, 0.0))
        self.assertFalse(any(high.step(aloft(x, y, layer=3, turn=t), "hold").events[-1][0] == "rotor"
                             for t in range(8, 68)), "layer 3 is above it")

    def test_rotor_drops_a_layer_where_there_is_one_below(self):
        engine = flight(wind=(0.0, 0.0))
        seen = None
        for k in range(fw.CHART_W * fw.CHART_H):
            x = k % fw.CHART_W
            if x < 3:
                continue
            px, py = x * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            if engine.lowest_open(px, py) == 1 and WORLD.sample(px - fw.ROTOR_REACH_KM + 1.0, py)[0] >= 1500:
                seen = (px, py)
                break
        if seen is None:
            self.skipTest("this world has no lowland that close behind a range")
        lee = flight(wind=(4.0, 0.0))
        dropped = [r for r in (lee.step(aloft(seen[0], seen[1], layer=2, turn=t), "hold") for t in range(8, 68))
                   if r.events[-1][0] == "rotor"]
        self.assertTrue(dropped)
        self.assertTrue(all(r.state.layer == 1 for r in dropped))


def autopilot(engine, source, target, day):
    """Fly toward a town by always taking the layer that closes the distance most."""
    state = fw.FlightState(x=source["x"], y=source["y"], day=day)
    script = []
    while True:
        offered = engine.actions(state)
        distance = math.hypot(target["x"] - state.x, target["y"] - state.y)
        if not offered or state.status != "aloft" and state.turn > 0:
            return state, script
        if state.status == "ground":
            action = "launch"
        elif distance <= fw.PLACE_RADIUS_KM:
            action = "land" if "land" in offered else "sink" if "sink" in offered else "hold"
        else:
            frame = engine.weather.frame(state.day, state.minute)
            best = None
            for name, change in (("hold", 0), ("climb", 1), ("sink", -1)):
                if name not in offered:
                    continue
                vx, vy = frame.wind(state.x, state.y, state.layer + change)
                left = math.hypot(target["x"] - state.x - vx * 0.25, target["y"] - state.y - vy * 0.25)
                score = left + offered[name] * 0.8
                if best is None or score < best[0]:
                    best = (score, name)
            action = best[1] if best else sorted(offered)[0]
        script.append(action)
        state = engine.step(state, action).state


class WholeFlights(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = fw.Flight(WORLD, WEATHER, "flight-node/12")
        start = WORLD.towns[WORLD.start_town]
        others = sorted((t for t in WORLD.towns if t is not start),
                        key=lambda t: math.hypot(t["x"] - start["x"], t["y"] - start["y"]))
        cls.start, cls.near = start, others[:3]

    def delivery(self):
        for day in range(1, 61):
            for target in self.near:
                state, script = autopilot(self.engine, self.start, target, day)
                if state.status == "ground" and self.engine.place(state.x, state.y) == ("town", WORLD.towns.index(target)):
                    return day, target, state, script
        self.fail("no town could be reached from the start town in sixty days")

    def test_a_scripted_delivery_between_two_towns_succeeds(self):
        day, target, state, script = self.delivery()
        self.assertGreater(state.distance, 50.0)
        self.assertGreaterEqual(state.fuel, 0)
        self.assertLessEqual(state.turn, fw.DAY_TURNS)
        self.assertEqual(script[0], "launch")
        self.assertEqual(script[-1], "land")

    def test_a_scripted_flight_replays_to_the_same_state(self):
        day, target, final, script = self.delivery()
        world = fw.World(fw.world_seed("flight-node"))
        engine = fw.Flight(world, fw.Weather(world), "flight-node/12")     # nothing shared with the first run
        state = fw.FlightState(x=self.start["x"], y=self.start["y"], day=day)
        events = []
        for action in script:
            result = engine.step(state, action)
            events.append(result.events)
            state = result.state
        self.assertEqual(state, final)
        again = fw.FlightState(x=self.start["x"], y=self.start["y"], day=day)
        for action, expected in zip(script, events):
            result = self.engine.step(again, action)
            self.assertEqual(result.events, expected)
            again = result.state

    def test_a_disconnect_in_the_middle_changes_nothing(self):
        day, target, final, script = self.delivery()
        state = fw.FlightState(x=self.start["x"], y=self.start["y"], day=day)
        for i, action in enumerate(script):
            if i % 5 == 0:
                # Hang up: all that survives is what was written down.
                saved = json.dumps(state.to_dict())
                world = fw.World(fw.world_seed("flight-node"))
                engine = fw.Flight(world, fw.Weather(world), "flight-node/12")
                state = fw.FlightState.from_dict(json.loads(saved))
            else:
                engine = self.engine
            before = state
            state = engine.step(state, action).state
            self.assertEqual(engine.step(before, action).state, state, "trying the turn again gives the same turn")
        self.assertEqual(state, final)

    def test_state_survives_being_written_down(self):
        state = fw.FlightState(x=1.25, y=2.5, day=7, turn=3, layer=2, status="aloft", fuel=11,
                               cargo=((4, True, "damaged"), (9, False, "ok")), oxygen=True, distance=12.75)
        self.assertEqual(fw.FlightState.from_dict(json.loads(json.dumps(state.to_dict()))), state)
        self.assertEqual(fw.FlightState.from_dict({"x": 1.0, "y": 2.0, "unknown": 1}), fw.FlightState(x=1.0, y=2.0))


class Preview(unittest.TestCase):
    def setUp(self):
        self.x, self.y = lowland()

    def test_in_still_weather_the_preview_is_the_flight(self):
        engine = flight(by_layer={1: (12.0, 4.0), 2: (-20.0, 8.0), 3: (0.0, -30.0)})
        state = aloft(self.x, self.y, layer=2)
        for action in ("hold", "climb", "sink"):
            look = engine.preview(state, action, 6)
            self.assertEqual(look["cost"], engine.actions(state)[action])
            flown = engine.step(state, action).state
            points = [(flown.x, flown.y, flown.layer)]
            for _ in range(5):
                flown = engine.step(flown, "hold").state
                points.append((flown.x, flown.y, flown.layer))
            self.assertEqual(len(look["points"]), 6)
            for got, want in zip(look["points"], points):
                self.assertAlmostEqual(got[0], want[0])
                self.assertAlmostEqual(got[1], want[1])
                self.assertEqual(got[2], want[2])
            self.assertFalse(look["blocked"])

    def test_only_what_the_pilot_can_see_is_shown_as_true(self):
        engine = fw.Flight(WORLD, WEATHER, "flight-node/12")
        found = False
        for day in range(1, 40):
            state = aloft(self.x, self.y, layer=2, day=day)
            truth = WEATHER.frame(day, state.minute)
            hold = engine.preview(state, "hold", 4)["points"][0]
            vx, vy = truth.wind(state.x, state.y, 2)
            self.assertAlmostEqual(hold[0], state.x + vx * 0.25, msg="your own layer is known")
            blind = engine.preview(state, "climb", 4)["points"]
            sure = engine.preview(dataclasses.replace(state, pibal_until=state.turn + 3), "climb", 4)["points"]
            ux, uy = truth.wind(state.x, state.y, 3)
            self.assertAlmostEqual(sure[0][0], state.x + ux * 0.25, msg="a pibal reads the layer above")
            self.assertEqual(blind[0], sure[0], "with no lead the first step is the truth either way")
            far = engine.preview(state, "hold", 12, coverage=0.0)["points"]
            near = engine.preview(state, "hold", 12, coverage=1.0)["points"]
            found = found or far[-1] != near[-1]
        self.assertTrue(found, "without a station the far end of a track is a guess")

    def test_what_cannot_be_done_has_no_track(self):
        engine = flight(wind=(1.0, 0.0))
        state = aloft(self.x, self.y, layer=3)
        self.assertEqual(engine.preview(state, "climb", 6)["points"], [])
        self.assertEqual(engine.preview(state, "land", 6)["cost"], None)

    def test_a_track_stops_at_ground_it_cannot_cross_and_at_midnight(self):
        engine = flight(wind=(0.0, 0.0))
        def test(k):
            x = k % fw.CHART_W
            if x >= fw.CHART_W - 2:
                return False
            px, py = x * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            return engine.lowest_open(px, py) == 1 and engine.lowest_open(px + 10.0, py) == 2
        x, y = find_cell(test)
        east = flight(wind=(40.0, 0.0))
        over = east.preview(aloft(x, y), "hold", 3)
        self.assertTrue(over["blocked"])
        self.assertEqual(over["points"][0][2], 2, "the track shows the climb it would force")
        stopped = east.preview(aloft(x, y, ceiling=1), "hold", 3)
        self.assertEqual((stopped["points"], stopped["blocked"]), ([], True))
        late = flight(wind=(1.0, 0.0)).preview(aloft(self.x, self.y, turn=fw.DAY_TURNS - 2, night_gear=True), "hold", 8)
        self.assertEqual(len(late["points"]), 2)


if __name__ == "__main__":
    unittest.main()
