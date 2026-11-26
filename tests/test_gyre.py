"""The Gyre (S23): its bands, the forecast that fails toward it, the wall, the eye and the hall."""

import dataclasses
import math
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from test_flight import WORLD, aloft, flight              # noqa: E402
from test_plain import launch                             # noqa: E402
from test_rescue import RescueCase                        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GYRE = WORLD.gyre


def way_in(world, weather, ceiling=5, days=range(1, 61), from_km=None, engine=None):
    """A day and a start just outside the inner band from which climbing to the top and holding reaches the eye.

    engine: the pilot's own, where the flight is to be flown again by that pilot: squalls and rough air are
    drawn for each pilot.
    """
    engine = engine or fw.Flight(world, weather, "probe")
    gyre = world.gyre
    from_km = from_km or fw.GYRE_INNER_KM + 5.0
    for day in days:
        for k in range(24):
            angle = k * math.pi / 12
            x, y = gyre["x"] + from_km * math.cos(angle), gyre["y"] + from_km * math.sin(angle)
            if not (0 <= x < fw.WORLD_W and 0 <= y < fw.WORLD_H) or world.sample(x, y)[1] < fw.SHORE:
                continue
            state = fw.FlightState(x=x, y=y, day=day, turn=4, fuel=130, ballast=5, ceiling=ceiling, climb_cost=1)
            actions = []
            for _ in range(64):
                offered = engine.actions(state)
                action = "launch" if state.status == "ground" else \
                    "climb" if state.layer < ceiling and "climb" in offered else "hold"
                if action not in offered:
                    break
                actions.append(action)
                state = engine.step(state, action).state
                if state.status != "aloft" or engine.gyre_km(state.x, state.y) <= fw.GYRE_EYE_KM:
                    break
            if state.status == "aloft" and engine.gyre_km(state.x, state.y) <= fw.GYRE_EYE_KM:
                return day, x, y, actions
    return None


class Bands(RescueCase):
    def test_the_bands(self):
        self.assertEqual((fw.GYRE_OUTER_KM, fw.GYRE_INNER_KM, fw.GYRE_WALL_KM, fw.GYRE_EYE_KM), (300.0, 150.0, 60.0, 30.0))
        self.assertEqual(fw.GYRE_INNER_KM, fw.GYRE_RADIUS, "the inner band is the storm's own radius")
        self.assertEqual(fw.WONDER_KINDS["eye"]["km"], fw.GYRE_EYE_KM, "the fortieth wonder is seen from inside the eye")
        engine = fw.Flight(WORLD, fw.Weather(WORLD), "x")
        self.assertEqual(engine.gyre_km(GYRE["x"] + 30.0, GYRE["y"] + 40.0), 50.0)
        self.assertEqual([engine.in_wall(GYRE["x"] + d, GYRE["y"]) for d in (10.0, 30.0, 30.1, 60.0, 60.1)],
                         [False, False, True, True, False])

    def test_the_forecast_fails_toward_the_centre(self):
        game = self.game()
        gyre = game.world.gyre
        spots = [(gyre["x"] + d, gyre["y"]) for d in (fw.GYRE_OUTER_KM + 60.0, 220.0, 100.0)]
        # A post of the Post's own at each spot: outside the bands that is a forecast as good as the truth.
        game.stations = list(game.world.stations) + [{"name": f"S{i}", "x": x, "y": y} for i, (x, y) in enumerate(spots)]
        cover = [game.coverage(x, y) for x, y in spots]
        self.assertEqual(cover, [1.0, fw.GYRE_OUTER_COVER, 0.0], "full outside, half at most in the outer band, none within")

        def error(x, y, share):
            total = 0.0
            for minute in (480, 720, 960):
                for layer in (1, 2, 3):
                    tx, ty = game.weather.frame(3, minute).wind(x, y, layer)
                    fx, fy = game.weather.frame(3, minute, 180.0, share).wind(x, y, layer)
                    total += math.hypot(tx - fx, ty - fy)
            return total
        x, y = spots[2]
        errors = [error(x, y, share) for share in cover]
        self.assertEqual(errors[0], 0.0)
        self.assertTrue(errors[0] < errors[1] < errors[2], errors)
        self.assertEqual(game.coverage(gyre["x"] + fw.GYRE_INNER_KM, gyre["y"]), 0.0)
        self.assertLessEqual(game.coverage(gyre["x"] + fw.GYRE_INNER_KM + 1.0, gyre["y"]), fw.GYRE_OUTER_COVER)

    def test_nothing_is_planted_inside_the_inner_band(self):
        game = self.game()
        gyre = game.world.gyre
        game.kits = 1
        for km, allowed in ((20.0, False), (fw.GYRE_INNER_KM - 1.0, False), (fw.GYRE_INNER_KM + 2.0, True)):
            self.down(game, gyre["x"] + km, gyre["y"])
            why = game.cannot_plant()
            self.assertEqual("Nothing stands this near the Gyre" in why, not allowed, (km, why))
        self.down(game, gyre["x"] + 100.0, gyre["y"])
        game.key("k")
        self.assertEqual((game.kits, len(game.stations)), (1, 3))
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        self.assertIsNone(fw.plant_station(db, game.world, gyre["x"] + 100.0, gyre["y"], 12, "Mara", 1),
                          "nor by any other way into the records")
        self.assertIsNotNone(fw.plant_station(db, game.world, gyre["x"] + fw.GYRE_INNER_KM + 2.0, gyre["y"], 12, "Mara", 1))
        db.close()


class Wall(unittest.TestCase):
    def test_nothing_below_its_layer_gets_through_the_wall(self):
        engine = flight(wind=(-60.0, 0.0))                # fifteen km a turn, straight at the middle
        x, y = GYRE["x"] + fw.GYRE_WALL_KM + 10.0, GYRE["y"]
        for layer in range(1, fw.GYRE_WALL_LAYER):
            if layer < engine.lowest_open(x, y) or layer < engine.lowest_open(x - 15.0, y):
                continue
            state = aloft(x, y, layer=layer, ceiling=6, oxygen=True)
            result = engine.step(state, "hold")
            self.assertEqual((result.state.x, result.state.y), (x, y), layer)
            self.assertEqual(result.state.wear, state.wear + fw.GYRE_WEAR)
            self.assertIn(("wall",), result.events)
            look = engine.preview(state, "hold", 6)
            self.assertEqual((look["stop"], look["points"]), ("wall", []), "and the track says so")
        high = aloft(x, y, layer=fw.GYRE_WALL_LAYER, ceiling=6, oxygen=True)
        through = engine.step(high, "hold")
        self.assertEqual(through.state.x, x - 15.0)
        self.assertNotIn(("wall",), through.events)
        self.assertTrue(engine.in_wall(through.state.x, through.state.y))

    def test_a_fast_wind_does_not_carry_a_balloon_over_the_wall_in_one_bound(self):
        engine = flight(wind=(-560.0, 0.0))               # 140 km in a turn: from outside the wall to past it
        x, y = GYRE["x"] + fw.GYRE_WALL_KM + 5.0, GYRE["y"]
        for layer in range(1, fw.GYRE_WALL_LAYER):
            if layer < max(engine.lowest_open(x, y), engine.lowest_open(x - 140.0, y)):
                continue
            state = aloft(x, y, layer=layer, ceiling=4)
            self.assertFalse(engine.in_wall(x - 140.0, y), "it would end clear of the wall on the far side")
            result = engine.step(state, "hold")
            self.assertEqual((result.state.x, result.state.y), (x, y), layer)
            self.assertIn(("wall",), result.events)
            self.assertEqual(engine.preview(state, "hold", 4)["stop"], "wall")
        into = flight(wind=(-140.0, 0.0))                 # thirty-five km: from outside straight into the eye
        result = into.step(aloft(x, y, layer=min(4, max(1, into.lowest_open(x, y))), ceiling=4), "hold")
        self.assertNotIn(("eye",), result.events)
        self.assertGreater(into.gyre_km(result.state.x, result.state.y), fw.GYRE_WALL_KM)
        self.assertTrue(engine.wall_stops(x, y, x - 140.0, y))
        self.assertFalse(engine.wall_stops(x, y, x, y + 300.0), "a move that stays clear of it is not stopped")
        self.assertTrue(engine.wall_stops(GYRE["x"] - 200.0, GYRE["y"] + 40.0, GYRE["x"] + 200.0, GYRE["y"] + 40.0),
                        "nor is the wall crossed by a move that only grazes it and ends far beyond")

    def test_a_balloon_down_in_the_wall_is_not_trapped_there(self):
        out = flight(wind=(60.0, 0.0))                    # blowing outward
        x, y = GYRE["x"] + 45.0, GYRE["y"]
        low = out.lowest_open(x, y)
        if low >= fw.GYRE_WALL_LAYER:
            self.skipTest("the ground there fills every layer below the wall's")
        ground = dataclasses.replace(aloft(x, y, layer=0, ceiling=3), status="ground")
        up = out.step(ground, "launch")
        self.assertNotIn(("wall",), up.events, "a launch from inside the wall is not thrown back")
        self.assertEqual(up.state.wear, ground.wear)
        self.assertGreater(out.gyre_km(up.state.x, up.state.y), 45.0, "and the wind carries it out")
        inward = flight(wind=(-80.0, 0.0))
        held = inward.step(aloft(x, y, layer=low, ceiling=3), "hold")
        self.assertIn(("wall",), held.events, "but not into the eye")
        self.assertEqual(held.state.x, x)
        still = flight(wind=(0.0, 20.0))
        self.assertNotIn(("wall",), still.step(aloft(x, y, layer=low, ceiling=3), "hold").events, "about in it: yes")

    def test_nothing_below_the_walls_layer_leaves_the_eye_and_what_is_paid_for_is_only_what_was_done(self):
        out = flight(wind=(80.0, 0.0))
        x, y = GYRE["x"] + 20.0, GYRE["y"]
        low = out.lowest_open(x, y)
        if low < fw.GYRE_WALL_LAYER:
            held = out.step(aloft(x, y, layer=low, ceiling=6, oxygen=True, fuel=40), "hold")
            self.assertIn(("wall",), held.events)
            self.assertEqual((held.state.x, held.state.layer), (x, low))
            self.assertEqual(held.state.wear, 0, "held in the still air: no wear")
            self.assertFalse(any(event[0] in ("forced_climb", "ridge") for event in held.events),
                             "thrown back before any ground ahead is met")
        high = out.step(aloft(x, y, layer=fw.GYRE_WALL_LAYER, ceiling=6, oxygen=True), "hold")
        self.assertNotIn(("wall",), high.events)

    def test_no_town_and_no_first_station_stands_inside_the_inner_band(self):
        for node in ("test-node", "relink-test", "a", "b", "zeta"):
            world = fw.World(fw.world_seed(node))
            gyre = world.gyre
            for place in world.towns + world.stations:
                self.assertGreater(math.hypot(place["x"] - gyre["x"], place["y"] - gyre["y"]), fw.GYRE_INNER_KM,
                                   (node, place["name"]))

    def test_a_first_balloon_never_gets_in(self):
        engine = flight(wind=(-60.0, 0.0))
        x, y = GYRE["x"] + fw.GYRE_WALL_KM + 10.0, GYRE["y"]
        low = max(engine.lowest_open(x, y), engine.lowest_open(x - 15.0, y))
        state = aloft(x, y, layer=min(3, max(1, low)), ceiling=3, fuel=60)
        if low > 3:
            self.skipTest("the ground at the wall is above a first balloon's ceiling: it cannot be there at all")
        for _ in range(20):
            if "hold" not in engine.actions(state):
                break
            state = engine.step(state, "hold").state
            self.assertGreater(engine.gyre_km(state.x, state.y), fw.GYRE_WALL_KM, "held outside, turn after turn")
        self.assertGreaterEqual(state.wear, fw.GYRE_WEAR * 5)
        self.assertEqual(engine.top_layer(state), 3)
        self.assertLess(engine.top_layer(state), fw.GYRE_WALL_LAYER, "its ceiling is under the wall's layer for good")

    def test_into_the_eye_is_an_event_once(self):
        engine = flight(wind=(-60.0, 0.0))
        x, y = GYRE["x"] + fw.GYRE_EYE_KM + 10.0, GYRE["y"]
        state = aloft(x, y, layer=fw.GYRE_WALL_LAYER, ceiling=6, oxygen=True)
        result = engine.step(state, "hold")
        self.assertIn(("eye",), result.events)
        again = engine.step(result.state, "hold")
        self.assertNotIn(("eye",), again.events, "already inside")

    def test_with_the_real_weather_no_first_balloon_reaches_the_eye_and_a_late_one_does(self):
        weather = fw.Weather(WORLD)
        self.assertIsNone(way_in(WORLD, weather, ceiling=3, days=range(1, 13)), "a first balloon cannot")
        self.assertIsNone(way_in(WORLD, weather, ceiling=4, days=range(1, 13)), "nor one that tops out a layer short")
        self.assertIsNotNone(way_in(WORLD, weather, ceiling=5), "a balloon that reaches the wall's layer can")

    def test_every_world_tried_has_a_way_in(self):
        for node in ("relink-test", "a", "zeta"):
            world = fw.World(fw.world_seed(node))
            self.assertIsNotNone(way_in(world, fw.Weather(world)), node)


class Eye(RescueCase):
    def late_balloon(self, game):
        game.gear.update(envelope=2, burner=2, tanks=2)
        game._fit()

    def test_a_late_flight_from_the_nearest_mast_reaches_the_eye_and_is_in_the_hall(self):
        game = self.game(handle="Ada")
        found = way_in(game.world, game.weather, engine=game.engine)
        self.assertIsNotNone(found)
        day, x, y, actions = found
        self.assertLessEqual(game.engine.gyre_km(x, y), fw.GYRE_INNER_KM + 5.0, "as near as anything may stand")
        # A mast as near the Gyre as one can be: planted there, raised there. (Its distance from other places
        # is not what is tried here.)
        game.kits = 1
        self.down(game, x, y)
        self.assertNotIn("Gyre", game.cannot_plant())
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        self.assertIsNotNone(fw.plant_station(db, game.world, x, y, 12, "Ada", 1))
        db.execute("UPDATE stations SET freight=?, mast_day=1", (fw.MAST_CRATES,))
        db.commit()
        db.close()
        game._see_stations()
        self.assertEqual(game.here(), ("station", 3), "the balloon stands at the mast")
        self.late_balloon(game)
        game.today = max(game.today, day)
        game.state = dataclasses.replace(game.state, day=day, turn=4, fuel=game.tank(), ballast=5)
        game._choose_default()
        was = (game.postmarks, game.points)
        for action in actions:
            while game.naming:                            # new ground all the way: features are found, and named
                game.key("1")
            self.assertIn(action, game.offered(), action)
            game.choice = action
            game.key("ENTER")
        self.assertLessEqual(game.engine.gyre_km(game.state.x, game.state.y), fw.GYRE_EYE_KM)
        self.assertEqual(game.state.status, "aloft")
        self.assertTrue(game.eye)
        self.assertTrue(any("This is the eye of the Gyre" in line for line in game.heard), game.heard)
        self.assertTrue(any("Your name is in the hall for good" in line for line in game.heard))
        self.assertGreaterEqual(game.postmarks - was[0], fw.EYE_PAY[1])
        self.assertGreaterEqual(game.points - was[1], fw.EYE_POINTS[1])
        self.assertEqual(self.rows("SELECT feat, user_id, handle FROM hall"), [("eye", 12, "Ada")])
        saved = support.stored(self.folder)
        self.assertTrue(saved["eye"])
        eye = game.wonders[-1]
        self.assertIs(game.wonder_here(), eye, "and the fortieth wonder is there to be taken")
        game.key("f")
        self.assertIn(eye["id"], game.album)
        game.pending_scene = None
        game.key("y")
        self.assertIn("The eye of the Gyre: Ada was first in", self.screen(game))

    def test_the_second_pilot_in_is_paid_less_and_is_not_in_the_hall(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        gyre = a.world.gyre
        for game in (a, b):
            self.late_balloon(game)
            game.state = dataclasses.replace(game.state, x=gyre["x"] + fw.GYRE_EYE_KM + 3.0, y=gyre["y"], status="aloft",
                                             layer=5, fuel=60, turn=20)
            game._choose_default()
        was = (a.postmarks, b.postmarks)
        entered = []
        for game in (a, b):
            for _ in range(8):                            # the air is slow here: hold until it carries the balloon in
                game.state = dataclasses.replace(game.state, x=gyre["x"] + fw.GYRE_EYE_KM + 3.0, y=gyre["y"])
                real = game.engine.weather.frame

                class Inward:
                    def __init__(self, frame):
                        self.frame = frame

                    def wind(self, x, y, layer):
                        return (-40.0, 0.0)

                    def __getattr__(self, name):
                        return getattr(self.frame, name)
                game.engine.weather.frame = lambda *args, real=real: Inward(real(*args))
                game.choice = "hold"
                game.key("ENTER")
                game.engine.weather.frame = real
                if game.eye:
                    break
            entered.append(game.eye)
        self.assertEqual(entered, [True, True])
        self.assertGreaterEqual(a.postmarks - was[0], fw.EYE_PAY[1])
        self.assertTrue(fw.EYE_PAY[0] <= b.postmarks - was[1] < fw.EYE_PAY[1])
        self.assertTrue(any("Ada was here first" in line for line in b.heard), b.heard)
        self.assertEqual(self.rows("SELECT user_id FROM hall"), [(1,)])
        self.assertTrue(support.stored(self.folder, 2)["eye"], "and it is in the pilot's record: once is once")
        b.close()

    def test_an_arrival_that_could_not_be_written_is_made_good_by_the_next_turn(self):
        game = self.game(handle="Ada")
        gyre = game.world.gyre
        self.late_balloon(game)
        game.state = dataclasses.replace(game.state, x=gyre["x"] + 5.0, y=gyre["y"], status="aloft", layer=5, fuel=60,
                                         turn=20)
        game._choose_default()
        was, real = game.postmarks, game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        game.store.commit = broken
        game.choice = "hold"
        game.key("ENTER")
        game.store.commit = real
        self.assertEqual((game.eye, game.postmarks), (False, was))
        self.assertEqual(self.rows("SELECT count(*) FROM hall"), [(0,)])
        while game.naming:
            game.key("1")
        low = game.engine.lowest_open(game.state.x, game.state.y)
        game.state = dataclasses.replace(game.state, layer=low)
        game.choice = "land"
        game.key("ENTER")
        self.assertNotEqual(game.state.status, "aloft")
        self.assertTrue(game.eye, "still in the eye, and down in it: the next turn writes it all the same")
        self.assertGreaterEqual(game.postmarks, was + fw.EYE_PAY[1])
        self.assertEqual(self.rows("SELECT handle FROM hall"), [("Ada",)])

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found, done: (got.append(done), {})[1], {'day': 3, 'turn': 1},\n"
              "         'hold', deed=lambda db: fw.enter_hall(db, 'eye', user, 'P%d' % user, 3))\n"
              "print(int(got[0]['first']))\n"
              "s.release(user); s.close()\n")

    def test_eight_pilots_into_the_eye_at_once_and_one_is_first(self):
        game = self.game()
        seed = game.world.seed
        game.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for user in range(101, 109)]
        first = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            first += int(out.decode().strip())
        self.assertEqual(first, 1)
        self.assertEqual(self.rows("SELECT count(*) FROM hall"), [(1,)])

    def test_the_screens_show_the_wall_and_the_hall(self):
        game = self.game()
        gyre = game.world.gyre
        game.key("y")
        self.assertIn("THE HALL", self.screen(game))
        self.assertIn("no pilot has been in it", self.screen(game))
        game.key("ENTER")
        game.key("c")
        for size in ((80, 24), (132, 40)):
            self.screen(game, *size)
        game.key("ENTER")
        self.assertEqual(game._describe(("wall",)), "The wall of the Gyre throws the balloon back. Nothing below "
                                                    "layer 5 gets through it.")
        self.assertIn("eye of the Gyre", game._describe(("eye",)))
        del gyre

    def test_a_pilot_and_a_world_from_before(self):
        game = self.game()
        career = {k: v for k, v in game._career().items() if k != "eye"}
        game._take(career, game.state.to_dict())
        self.assertFalse(game.eye)
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in range(1, 9):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=8")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.execute("INSERT INTO seasons VALUES (1, 'plant', 2, 0, 0)")
        db.commit()
        db.close()
        old = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(old.saved_ok)
        self.assertEqual(old.season_now["kind"], "plant")
        self.assertEqual(old.hall, {})
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], fw.SCHEMA_VERSION)
        self.assertEqual(db.execute("SELECT count(*) FROM hall").fetchone()[0], 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
