"""The almanac (S18): the world's local winds, flying them into the almanac, and the almanac screen."""

import dataclasses
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from test_plain import launch                             # noqa: E402
from test_rescue import RescueCase                        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NODES = ("test-node", "relink-test", "a", "b", "zeta")


class AlmanacCase(RescueCase):
    def blowing_turn(self, game, wind, day):
        """A turn of the day at which the wind blows, leaving room to fly."""
        for turn in range(0, 56):
            if fw.wind_blows(game.weather, wind, day, fw.DAWN_MINUTE + turn * fw.TURN_MINUTES):
                return turn
        return None

    def fly(self, game, wind, layer=None):
        """One turn held in the wind on the day the calendar has come to. False if it does not blow that day."""
        day = game.today
        turn = self.blowing_turn(game, wind, day)
        if turn is None:
            return False
        low = game.engine.lowest_open(wind["x"], wind["y"])
        game.state = dataclasses.replace(game.state, x=wind["x"], y=wind["y"], day=day, turn=turn, status="aloft",
                                         layer=layer or low, fuel=50, ceiling=5)
        game._choose_default()
        game.choice = "hold"
        game.key("ENTER")
        return True

    def fly_days(self, game, wind, days, others=()):
        """Fly the wind on so many different days it blows on."""
        flown = 0
        for _ in range(60):
            if flown == days:
                return
            self.days(1, game, *others)
            flown += bool(self.fly(game, wind))
        self.fail("the wind did not blow often enough")

    def a_wind(self, game, kind="sea"):
        return next(w for w in game.winds if w["kind"] == kind)


class Generator(unittest.TestCase):
    def test_every_world_has_twelve_to_twenty_well_apart_and_named(self):
        for node in NODES:
            world = fw.World(fw.world_seed(node))
            winds = fw.local_winds(world, fw.Weather(world))
            self.assertTrue(fw.WIND_COUNT[0] <= len(winds) <= fw.WIND_COUNT[1], (node, len(winds)))
            self.assertEqual([w["id"] for w in winds], list(range(len(winds))))
            names = [w["name"] for w in winds]
            self.assertEqual(len(set(names)), len(names), "no two share a name")
            for i, wind in enumerate(winds):
                self.assertIn(wind["kind"], fw.WIND_KINDS)
                self.assertTrue(wind["name"].startswith("The ") and
                                wind["name"].endswith(fw.WIND_KINDS[wind["kind"]]["words"]), wind["name"])
                self.assertLess(world.chart_elevation[wind["cell"]], fw.LAYER_BLOCK_M[0], "low enough to fly in")
                self.assertIn(wind["cell"], wind["cells"])
                for other in winds[:i]:
                    self.assertGreaterEqual((wind["x"] - other["x"]) ** 2 + (wind["y"] - other["y"]) ** 2,
                                            fw.WIND_APART_KM ** 2)
            again = fw.World(fw.world_seed(node))
            self.assertEqual([(w["name"], w["cell"], w["kind"]) for w in fw.local_winds(again, fw.Weather(again))],
                             [(w["name"], w["cell"], w["kind"]) for w in winds], "the same every time")

    def test_one_worlds_winds_are_where_they_were(self):
        # The records know a wind by its number. If this fails, the generator has changed, and the winds
        # entered in every world already begun now belong to other places.
        world = fw.World(fw.world_seed("test-node"))
        winds = fw.local_winds(world, fw.Weather(world))
        self.assertEqual([(w["name"], w["cell"], w["kind"]) for w in winds[:4]],
                         [("The Mallow Funnel", 11414, "pass"), ("The Alder Breeze", 10867, "sea"),
                          ("The Flint Drain", 12847, "fall"), ("The Osier Rush", 5385, "pass")])

    def test_each_wind_comes_back_when_its_conditions_hold(self):
        for node in NODES[:3]:
            world = fw.World(fw.world_seed(node))
            weather = fw.Weather(world)
            for wind in fw.local_winds(world, weather):
                days = [day for day in range(1, 29)
                        if any(fw.wind_blows(weather, wind, day, minute) for minute in range(360, 1200, 30))]
                self.assertGreaterEqual(len(days), fw.WIND_DAYS + 1, (node, wind["name"], "it has to be findable"))
                for day in (3, 11, 20):
                    for minute in range(360, 1260, 45):
                        frame = weather.frame(day, minute)
                        blows = fw.wind_blows(weather, wind, day, minute)
                        if wind["kind"] == "sea":
                            self.assertEqual(blows, frame.sea_breeze >= fw.SEA_BREEZE_AT)
                        elif wind["kind"] == "fall":
                            self.assertEqual(blows, frame.katabatic >= fw.DOWNSLOPE_AT)
                        else:
                            vx, vy = frame.wind(wind["x"], wind["y"], 1)
                            self.assertEqual(blows, (vx * vx + vy * vy) ** 0.5 >= fw.PASS_KMH)

    def test_a_sea_breeze_keeps_its_hours_and_a_downslope_wind_its_dawn(self):
        world = fw.World(fw.world_seed("test-node"))
        weather = fw.Weather(world)
        sea = next(w for w in fw.local_winds(world, weather) if w["kind"] == "sea")
        fall = next(w for w in fw.local_winds(world, weather) if w["kind"] == "fall")
        for day in (1, 9, 30):
            self.assertTrue(fw.wind_blows(weather, sea, day, 15 * 60))
            self.assertFalse(fw.wind_blows(weather, sea, day, 8 * 60))
            self.assertFalse(fw.wind_blows(weather, sea, day, 21 * 60))
            self.assertTrue(fw.wind_blows(weather, fall, day, 6 * 60 + 15))
            self.assertFalse(fw.wind_blows(weather, fall, day, 12 * 60))


class Entering(AlmanacCase):
    def test_three_days_in_a_wind_enter_it_under_the_pilot_once(self):
        game = self.game(handle="Ada")
        wind = self.a_wind(game)
        self.days(1, game)
        was = (game.postmarks, game.points)
        self.assertTrue(self.fly(game, wind))
        self.assertEqual(game.flown, {wind["id"]: 1})
        self.assertTrue(any("sea breeze blows here that no almanac has. Flown on 1 of 3 days" in line
                            for line in game.heard), game.heard)
        self.fly(game, wind)
        self.assertEqual(game.flown, {wind["id"]: 1}, "a day counts once")
        self.assertFalse(any("almanac" in line for line in game.heard), "and is spoken of once")
        self.fly_days(game, wind, 1)
        self.assertEqual((game.flown[wind["id"]], game.almanac), (2, {}))
        self.assertEqual(game.postmarks - was[0] < fw.ALMANAC_PAY, True, "nothing is paid before the third day")
        paid = game.postmarks
        self.fly_days(game, wind, 1)
        self.assertEqual(game.almanac[wind["id"]][:2], (12, "Ada"))
        self.assertGreaterEqual(game.postmarks, paid + fw.ALMANAC_PAY)
        self.assertTrue(any(f"{wind['name']} enters the almanac in your name" in line for line in game.heard))
        self.assertEqual(self.rows("SELECT wind, user_id, handle FROM almanac"), [(wind["id"], 12, "Ada")])
        self.assertEqual(support.stored(self.folder)["postmarks"], game.postmarks)
        paid = game.postmarks
        self.fly_days(game, wind, 1)
        self.assertLess(game.postmarks - paid, fw.ALMANAC_PAY, "entered once, paid once")
        self.assertEqual(self.rows("SELECT count(*) FROM almanac"), [(1,)])

    def test_not_when_it_is_not_blowing_and_not_from_on_high(self):
        game = self.game()
        wind = self.a_wind(game)
        self.days(1, game)
        low = game.engine.lowest_open(wind["x"], wind["y"])
        game.state = dataclasses.replace(game.state, x=wind["x"], y=wind["y"], day=game.today, turn=4, status="aloft",
                                         layer=low, fuel=50)
        self.assertFalse(fw.wind_blows(game.weather, wind, game.today, game.state.minute), "a sea breeze at 07:00")
        game._choose_default()
        game.key("ENTER")
        self.assertEqual(game.flown, {})
        turn = self.blowing_turn(game, wind, game.today)
        high = dataclasses.replace(game.state, x=wind["x"], y=wind["y"], turn=turn, layer=fw.WIND_LAYERS + 1)
        self.assertIsNone(game.wind_here(high, game.today, high.minute))
        self.assertIs(game.wind_here(dataclasses.replace(high, layer=fw.WIND_LAYERS), game.today, high.minute), wind)
        away = dataclasses.replace(high, layer=1, x=wind["x"] + fw.WIND_REACH_KM + 15.0)
        self.assertIsNone(game.wind_here(away, game.today, away.minute))
        self.assertIsNone(game.wind_here(dataclasses.replace(high, layer=0, status="ground"), game.today, high.minute))

    def test_the_first_to_a_third_day_enters_it_and_everyone_sees_it_blow(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        wind = self.a_wind(a)
        for _ in range(2):
            self.days(1, a, b)
            self.assertTrue(self.fly(a, wind))
            self.assertTrue(self.fly(b, wind))
        self.days(1, a, b)
        self.fly(b, wind)
        self.assertEqual(b.almanac[wind["id"]][:2], (2, "Bo"))
        paid = a.postmarks
        self.fly(a, wind)                                 # a has not looked: the records know it is entered
        self.assertEqual(self.rows("SELECT user_id FROM almanac"), [(2,)])
        self.assertFalse(any("enters the almanac" in line or "no almanac has" in line for line in a.heard), a.heard)
        self.assertTrue(any("in the almanac already: another pilot entered it" in line for line in a.heard), a.heard)
        self.assertLess(a.postmarks - paid, fw.ALMANAC_PAY)
        self.assertEqual(a.almanac[wind["id"]][:2], (2, "Bo"), "and it is taken in at once")
        said = len([line for line in a.log if "almanac already" in line])
        self.fly(a, wind)
        self.assertEqual(len([line for line in a.heard if "almanac already" in line]), 0, "said once")
        del said
        turn = self.blowing_turn(a, wind, a.today)
        a.state = dataclasses.replace(a.state, x=wind["x"] - 40.0, y=wind["y"], day=a.today, turn=turn,
                                      status="ground", layer=0)
        self.assertIn(wind, a.blowing())
        a.others = []                                     # (Bo's balloon is parked on the very spot)
        self.assertIn(wind["name"], self.screen(a, 132, 40))
        a.state = dataclasses.replace(a.state, turn=0)
        self.assertNotIn(wind, a.blowing(), "and not when it does not blow")
        self.assertNotIn(wind["name"], self.screen(a, 132, 40))
        b.close()

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found, done: (got.append(done), {})[1], {'day': 3, 'turn': 1},\n"
              "         'hold', deed=lambda db: fw.fly_wind(db, 0, user, 'P%d' % user, 3))\n"
              "print(int(got[0]['entered']))\n"
              "s.release(user); s.close()\n")

    def test_eight_pilots_on_their_third_day_at_once_enter_it_once(self):
        game = self.game()
        seed = game.world.seed
        game.close()
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for user in range(101, 109):
            db.executemany("INSERT INTO sightings VALUES (0, ?, ?)", ((user, 1), (user, 2)))
        db.commit()
        db.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for user in range(101, 109)]
        entered = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            entered += int(out.decode().strip())
        self.assertEqual(entered, 1)
        self.assertEqual(self.rows("SELECT count(*) FROM almanac"), [(1,)])

    def test_a_flight_that_cannot_be_written_enters_nothing(self):
        game = self.game()
        wind = self.a_wind(game)
        self.fly_days(game, wind, 2)
        self.days(1, game)
        was, real = (game.postmarks, game.points, dict(game.flown)), game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        game.store.commit = broken
        self.fly(game, wind)
        game.store.commit = real
        self.assertEqual((game.postmarks, game.points, game.flown, game.almanac), was + ({},))
        self.assertEqual(self.rows("SELECT count(*) FROM almanac"), [(0,)])

    def test_a_visitor_enters_a_wind_for_the_visit(self):
        game = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        wind = self.a_wind(game)
        self.fly_days(game, wind, 3)
        self.assertIn(wind["id"], game.almanac)
        game.close()

    def test_a_world_from_before_is_brought_forward(self):
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in (1, 2, 3, 4):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=4")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        # A world that has been lived in: a pilot, charted ground, a mast with its givers, a call, a line.
        probe = fw.PlainFlight(launch(user_id=2 ** 62 + 9, timezone="UTC", node_id="relink-test"), None, now=self.clock)
        probe.key("ENTER")
        spot = self.spot(probe)
        db.execute("INSERT INTO pilots VALUES (12, 'Mara', 1.0, ?)", (fw.json.dumps(probe._career()),))
        db.execute("INSERT INTO flights VALUES (12, 1, ?)", (fw.json.dumps(probe.state.to_dict()),))
        db.execute("INSERT INTO charted VALUES (77, 12, 1)")
        db.execute("INSERT INTO stations VALUES (3, 'Larch Hut', ?, ?, 12, 'Mara', 1, 14, 1, 4, 1)", spot)
        db.execute("INSERT INTO contributions VALUES (3, 12, 'Mara', 4)")
        db.execute("INSERT INTO calls (user_id, handle, x, y, day, fuel, repair) VALUES (40, 'Juno', 10.0, 10.0, 1, 15, 0)")
        db.execute("INSERT INTO guestbook (station, user_id, handle, day, text) VALUES (3, 12, 'Mara', 1, 'still here')")
        db.commit()
        db.close()
        probe.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.assertEqual(game.state.turn, 1, "the pilot is where they were")
        self.assertIn(77, game.chart)
        self.assertEqual((game.stations[3]["name"], game.stations[3]["given"]), ("Larch Hut", ["Mara"]))
        self.assertEqual([call["handle"] for call in game.calls], ["Juno"])
        self.assertEqual(game._book_lines(3), [(1, "Mara", "still here")])
        self.assertEqual((game.almanac, game.flown), ({}, {}))
        self.days(1, game)
        self.assertTrue(self.fly(game, self.a_wind(game)))
        self.assertEqual(self.rows("PRAGMA user_version"), [(fw.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT count(*) FROM sightings"), [(1,)])


class Pages(AlmanacCase):
    def test_the_almanac_opens_from_the_air_and_the_ground_and_any_key_closes_it(self):
        game = self.game()
        game.key("v")
        self.assertTrue(game.pages)
        text = self.screen(game)
        self.assertIn("The almanac", text)
        self.assertIn(f"0 of {len(game.winds)} local winds entered", text)
        self.assertIn("Nothing is entered yet", text)
        self.assertIn("Fly one in layer 2 or below while it blows, on 3 days: it is yours to enter.", text)
        self.assertIn("in hills.", text)
        self.assertIn("[B] Back", text)
        turn = game.state.turn
        for key in ("x", "ENTER", " "):
            game.key(key)
            self.assertTrue(game.pages, "no other key leaves it")
        self.assertEqual(game.state.turn, turn, "and none of them is a move")
        game.key("b")
        self.assertFalse(game.pages)
        self.assertEqual(game.state.turn, 0, "the key that closed it did nothing else")
        game.key("ENTER")
        game.key("v")
        self.assertTrue(game.pages, "aloft too")

    def test_it_says_what_is_entered_by_whom_and_what_is_part_way(self):
        game = self.game(handle="Ada")
        wind, other = self.a_wind(game, "sea"), self.a_wind(game, "fall")
        self.fly_days(game, wind, 3)
        self.fly_days(game, other, 2)
        game.key("v")
        text = self.screen(game)
        self.assertIn(f"{wind['name']}, a sea breeze", text)
        self.assertIn("Entered by Ada on Day", text)
        self.assertIn("An unnamed downslope wind", text)
        self.assertIn("flown 2 of 3 days", text)
        self.assertNotIn(other["name"], text, "a wind has no name until it is entered")
        self.assertIn(f"1 of {len(game.winds)} local winds entered", text)

    def test_a_full_almanac_fits_every_screen(self):
        game = self.game()
        game.key("v")
        game.almanac = {wind["id"]: (9, "Somebody With A Long Name", 3) for wind in game.winds}
        for size in ((80, 24), (100, 30), (132, 40), (168, 46)):
            text = self.screen(game, *size)
            self.assertIn(game.winds[0]["name"], text)
            self.assertNotIn("more blow where no pilot", text)
            if size[1] >= 46:
                self.assertIn(game.winds[-1]["name"], text)
            else:
                self.assertTrue(game.winds[-1]["name"] in text or " more" in text)

    def test_an_entered_wind_can_be_found_on_the_chart(self):
        game = self.game()
        wind = self.a_wind(game)
        game.almanac = {wind["id"]: (9, "Bo", 3)}
        game.key("c")
        game.key("/")
        for ch in wind["name"][4:]:
            game.key(ch)
        game.key("ENTER")
        self.assertEqual(game.atlas_view["found"], (wind["name"], wind["x"], wind["y"]))
        other = next(w for w in game.winds if w is not wind)
        self.assertNotIn(other["name"], [place[0] for place in game._atlas_places()])

    def test_the_help_names_the_almanac(self):
        self.assertIn("`V` winds", " ".join(fw.PLAIN_HELP))
        self.assertLessEqual(len(fw.PLAIN_HELP), 21)


if __name__ == "__main__":
    unittest.main()
