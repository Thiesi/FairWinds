"""Stations and masts (S15): planting, stock and lapse, forecast coverage, and masts raised together."""

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
from test_chart import ChartCase                          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class StationCase(ChartCase):
    def spot(self, game, least=70.0, skip=0):
        """Land in the wild, at least so far from every town and station."""
        found = 0
        for cell in sorted(game.world._land):
            x, y = (cell % fw.CHART_W + 0.5) * fw.CHART_KM, (cell // fw.CHART_W + 0.5) * fw.CHART_KM
            if all((p["x"] - x) ** 2 + (p["y"] - y) ** 2 >= least * least for p in game.world.towns + game.stations):
                if found == skip:
                    return x, y
                found += 1
        self.fail("no such land")

    def down(self, game, x, y, **changes):
        game.state = dataclasses.replace(game.state, x=x, y=y, layer=0, status="ground", **changes)

    def plant(self, game, x=None, y=None):
        if x is None:
            x, y = self.spot(game)
        game.kits = 1
        self.down(game, x, y)
        game.key("k")
        return game.stations[-1]

    def days(self, n, *games):
        self.clock.days(n)
        for game in games:
            game._day_may_have_opened()

    def rows(self, sql):
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def raise_mast(self, game, station):
        game.crates = fw.MAST_CRATES
        self.down(game, station["x"], station["y"])
        game._visit()
        return game.stations[station["id"]]


class FirstStations(StationCase):
    def test_a_new_world_has_three_and_they_never_lapse(self):
        game = self.game()
        self.assertEqual(len(game.stations), 3)
        self.assertTrue(all(fw.staffed(s) for s in game.stations))
        first = game.stations[0]
        self.assertEqual(game.coverage(first["x"], first["y"]), 1.0)
        self.assertGreater(game.coverage(game.state.x, game.state.y), 0.0, "a lone caller has a forecast")
        self.days(60, game)
        self.assertGreater(game.today, 50)
        self.assertEqual(game.coverage(first["x"], first["y"]), 1.0)
        self.assertEqual(game._station_mark(first), ("+", "cool"))


class Kits(StationCase):
    def test_a_kit_is_bought_in_a_town_and_handed_back(self):
        game = self.game()
        self.empty(game)
        game.postmarks = 60
        game.hub = True
        game.key("k")
        self.assertEqual((game.kits, game.postmarks, game.weight()), (1, 60 - fw.KIT_PRICE, fw.KIT_WEIGHT))
        self.assertEqual(support.stored(self.folder)["kits"], 1)
        self.assertIn("station kit", self.screen(game).lower())
        game.key("k")
        self.assertEqual((game.kits, game.postmarks), (0, 60))
        game.postmarks = fw.KIT_PRICE - 1
        self.assertIn("Not enough", game.kit())
        game.postmarks = 500
        game.crates = 2
        self.assertIn("Too heavy", game.kit())
        self.assertEqual(game.kits, 0)

    def test_neither_kit_nor_crate_is_sold_at_a_station(self):
        game = self.game()
        game.postmarks = 500
        station = game.stations[0]
        self.down(game, station["x"], station["y"])
        self.assertTrue(game.at_office())
        game.hub = True
        game.key("k")
        game.key("t")
        self.assertEqual((game.kits, game.crates, game.postmarks), (0, 0, 500))
        self.assertNotIn("Station kit", self.screen(game))

    def test_the_kit_comes_back_at_the_next_call(self):
        game = self.game()
        self.empty(game)
        game.postmarks = 200
        game.kit()
        game.crate(1)
        again = self.game()
        self.assertEqual((again.kits, again.crates, again.weight() >= fw.KIT_WEIGHT + fw.CRATE_WEIGHT), (1, 1, True))

    def test_a_record_with_impossible_freight_is_not_a_game(self):
        game = self.game()
        career = game._career()
        for bad in ({"kits": 2}, {"kits": -1}, {"crates": -1}, {"crates": 1000}, {"kits": "x"}):
            with self.assertRaises((ValueError, TypeError)):
                game._take(dict(career, **bad), game.state.to_dict())

    def test_a_ditching_loses_the_freight(self):
        game = self.game()
        game.kits, game.crates = 1, 2
        cell = next(k for k in range(fw.CHART_W * fw.CHART_H) if game.world.chart_biome[k] == fw.SEA_DEEP
                    and 5 < k % fw.CHART_W < 150 and 5 < k // fw.CHART_W < 95)
        x, y = (cell % fw.CHART_W + 0.5) * fw.CHART_KM, (cell // fw.CHART_W + 0.5) * fw.CHART_KM
        game.state = dataclasses.replace(game.state, x=x, y=y, status="aloft", layer=1, turn=8)
        game._choose_default()
        game.key("l")
        game.key("ENTER")
        self.assertEqual(game.state.status, "ditched")
        self.assertEqual((game.kits, game.crates), (0, 0))
        self.assertEqual(support.stored(self.folder)["crates"], 0)


class Planting(StationCase):
    def test_a_kit_planted_in_the_wild_is_a_station_on_every_chart(self):
        game = self.game(handle="Zed`Q")
        other = self.other(2)
        points = game.points
        x, y = self.spot(game)
        game.kits = 1
        self.down(game, x, y)
        self.assertIn("[K] Plant station", self.screen(game))
        game.key("k")
        self.assertEqual(len(game.stations), 4)
        station = game.stations[3]
        self.assertEqual((station["id"], station["x"], station["y"], station["stock"]), (3, x, y, fw.STOCK_FULL))
        self.assertEqual((game.kits, game.points), (0, points + fw.PLANT_POINTS))
        self.assertTrue(station["name"].endswith(fw.STATION_WORDS), station["name"])
        self.assertNotIn("Zed", station["name"])
        self.assertTrue(any(station["name"] in line for line in game.heard))
        saved = support.stored(self.folder, 12)
        self.assertEqual((saved["kits"], saved["points"]), (0, points + fw.PLANT_POINTS))
        self.assertEqual(self.rows("SELECT id, user_id, handle FROM stations"), [(3, 12, "Zed`Q")])
        self.assertNotIn("Plant station", self.screen(game))
        self.assertEqual(len(other.stations), 3)
        other._day_may_have_opened()
        self.assertEqual(other.stations[3]["name"], station["name"])
        other.close()

    def test_the_name_is_the_same_for_the_same_spot_and_unlike_any_other(self):
        game = self.game()
        x, y = self.spot(game)
        used = {p["name"] for p in game.world.towns + game.stations}
        name = fw.station_name(game.world, x, y, used)
        self.assertEqual(name, fw.station_name(game.world, x, y, used))
        self.assertNotEqual(name, fw.station_name(game.world, x, y, used | {name}))

    def test_not_within_sixty_km_of_a_town_or_a_station(self):
        game = self.game()
        town = game.world.towns[0]
        game.kits = 1
        self.down(game, town["x"] + 30.0, town["y"])
        self.assertIn(town["name"], game.cannot_plant())
        game.key("k")
        self.assertEqual((game.kits, len(game.stations)), (1, 3))
        self.assertTrue(any("Too close" in line for line in game.heard))
        station = self.plant(game)
        game.kits = 1
        self.down(game, station["x"] + fw.STATION_APART_KM - 1.0, station["y"])
        self.assertIn(station["name"], game.cannot_plant())
        game.key("k")
        self.assertEqual((game.kits, len(game.stations)), (1, 4))

    def test_no_kit_no_station_and_not_from_the_air(self):
        game = self.game()
        x, y = self.spot(game)
        self.down(game, x, y)
        game.key("k")
        self.assertEqual(len(game.stations), 3)
        game.kits = 1
        game.state = dataclasses.replace(game.state, status="aloft", layer=1)
        game.key("k")
        self.assertEqual((game.kits, len(game.stations)), (1, 3))

    def test_two_pilots_planting_side_by_side_plant_one(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y = self.spot(a)
        for game in (a, b):
            game.kits = 1
            self.down(game, x, y)
        a.key("k")
        b.key("k")                                        # b has not looked since: only the records know
        self.assertEqual((a.kits, b.kits), (0, 1), "the second keeps the kit")
        self.assertEqual(self.rows("SELECT count(*) FROM stations"), [(1,)])
        self.assertTrue(any("just planted" in line for line in b.heard))
        self.assertEqual(len(b.stations), 4, "and now it knows")
        b.close()

    def test_a_planting_that_cannot_be_written_did_not_happen(self):
        game = self.game()
        x, y = self.spot(game)
        game.kits = 1
        self.down(game, x, y)
        points, real = game.points, game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        game.store.commit = broken
        game.key("k")
        game.store.commit = real
        self.assertEqual((game.kits, game.points, len(game.stations)), (1, points, 3))
        self.assertFalse(game.saved_ok)
        self.assertEqual(self.rows("SELECT count(*) FROM stations"), [(0,)])

    def test_a_visitor_plants_for_the_visit_only(self):
        game = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        station = self.plant(game)
        self.assertEqual((len(game.stations), game.kits), (4, 0))
        self.assertEqual(game.coverage(station["x"], station["y"]), 1.0)
        game.crates = fw.MAST_CRATES
        game._visit()
        self.assertIsNotNone(game.stations[3]["mast_day"])
        game.close()
        again = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        self.assertEqual(len(again.stations), 3)
        again.close()


class Coverage(StationCase):
    def error(self, game, x, y):
        """How far the forecast is from the truth around a place, two hours ahead."""
        total = 0.0
        for dx in (0.0, 40.0, -40.0):
            for minute in (480, 720, 960):
                tx, ty = game.weather.frame(1, minute).wind(x + dx, y, 2)
                fx, fy = game.weather.frame(1, minute, 120.0, game.coverage(x + dx, y)).wind(x + dx, y, 2)
                total += ((tx - fx) ** 2 + (ty - fy) ** 2) ** 0.5
        return total

    def test_a_planted_station_sharpens_the_forecast_for_every_pilot(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y = self.spot(a, least=200.0)
        self.assertEqual(b.coverage(x, y), 0.0)
        beyond = b.coverage(x + fw.STATION_REACH_KM + 1.0, y)
        before = self.error(b, x, y)
        self.assertGreater(before, 0.5)
        self.plant(a, x, y)
        b._day_may_have_opened()
        self.assertEqual(b.coverage(x, y), 1.0)
        self.assertAlmostEqual(b.coverage(x + 75.0, y), 0.5)
        self.assertEqual(b.coverage(x + fw.STATION_REACH_KM + 1.0, y), beyond, "and no further than its radius")
        self.assertLess(self.error(b, x, y), before * 0.6)
        b.close()

    def test_stock_falls_by_the_world_day_and_at_none_the_station_is_silent(self):
        game = self.game()
        x, y = self.spot(game, least=200.0)
        station = self.plant(game, x, y)
        day = game.today
        self.assertEqual(fw.stock_of(station, day), fw.STOCK_FULL)
        for later, stock in ((1, 13), (7, 7), (10, 4), (14, 0), (40, 0)):
            self.assertEqual(fw.stock_of(station, day + later), stock)
        self.days(7, game)
        self.assertEqual(game.today, day + 7)
        self.assertEqual(game.coverage(x, y), 1.0, "half stocked still reports in full")
        self.days(3, game)
        self.assertAlmostEqual(game.coverage(x, y), 4 / fw.STOCK_SHARP)
        self.days(4, game)
        self.assertEqual(game.coverage(x, y), 0.0)
        self.assertEqual(len(game.stations), 4, "it stays on the chart")
        self.assertEqual(game._station_mark(game.stations[3]), ("+", "dim"))
        self.screen(game)

    def test_a_silent_station_is_restored_by_whoever_lands_there(self):
        a = self.game(user_id=1)
        b = self.other(2)
        station = self.plant(a, *self.spot(a, least=200.0))
        self.days(20, a, b)
        self.assertEqual(b.coverage(station["x"], station["y"]), 0.0)
        postmarks, points = b.postmarks, b.points
        self.down(b, station["x"] + 5.0, station["y"])
        b._visit()
        self.assertEqual(b.postmarks, postmarks + fw.STOCK_FULL * fw.RESUPPLY_PAY)
        self.assertEqual(b.points, points + int(fw.STOCK_FULL * fw.RESUPPLY_POINTS))
        self.assertTrue(any("was silent" in line for line in b.heard))
        self.assertEqual(b.coverage(station["x"], station["y"]), 1.0)
        self.assertEqual(support.stored(self.folder, 2)["postmarks"], b.postmarks)
        a._day_may_have_opened()
        self.assertEqual(a.coverage(station["x"], station["y"]), 1.0)
        b.close()

    def test_stock_is_paid_for_once(self):
        a = self.game(user_id=1)
        b = self.other(2)
        station = self.plant(a, *self.spot(a))
        self.days(5, a, b)
        for game in (a, b):
            self.down(game, station["x"], station["y"])
        was = (a.postmarks, b.postmarks)
        a._visit()
        b._visit()                                        # b still thinks five days are missing
        self.assertEqual((a.postmarks - was[0], b.postmarks - was[1]), (5 * fw.RESUPPLY_PAY, 0))
        self.assertTrue(any("well stocked" in line for line in b.heard))
        b.close()

    def test_landing_there_is_the_visit(self):
        game = self.game()
        station = self.plant(game)
        self.days(4, game)
        was = game.postmarks
        for turn in range(4, 48):
            state = dataclasses.replace(game.state, day=game.today, turn=turn, status="aloft", fuel=40,
                                        layer=game.engine.lowest_open(station["x"], station["y"]))
            if "land" in game.engine.actions(state):
                break
        else:
            self.skipTest("no landing there today")
        game.state = state
        game._choose_default()
        game.key("l")
        game.key("ENTER")
        self.assertEqual(game.state.status, "ground")
        self.assertTrue(any(f"Down at {station['name']}" in line for line in game.heard), game.heard)
        self.assertTrue(any("4 days of stock put back, 8 postmarks" in line for line in game.heard), game.heard)
        self.assertGreaterEqual(game.postmarks, was + 4 * fw.RESUPPLY_PAY)
        self.assertEqual(game.stations[3]["stock_day"], game.today)


class Masts(StationCase):
    def test_crates_are_bought_in_a_town_and_weigh(self):
        game = self.game()
        self.empty(game)
        game.postmarks = 100
        game.hub = True
        game.key("t")
        game.key("t")
        self.assertEqual((game.crates, game.postmarks, game.weight()), (2, 100 - 2 * fw.CRATE_PRICE, 2 * fw.CRATE_WEIGHT))
        self.assertIn("Too heavy", game.crate(1))
        self.assertIn("2 crates of mast freight", self.screen(game))
        game.key("y")
        self.assertEqual((game.crates, game.postmarks), (1, 100 - fw.CRATE_PRICE))

    def test_several_pilots_raise_one_mast_once(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        c = self.other(3, handle="Cy")
        station = self.plant(a)
        for game in (a, b, c):
            game._day_may_have_opened()
            game.crates = 2
            self.down(game, station["x"], station["y"])
        was = [g.postmarks for g in (a, b, c)]
        a._visit()
        self.assertEqual((a.crates, a.postmarks - was[0]), (0, 2 * fw.CRATE_PAY))
        self.assertIsNone(a.stations[3]["mast_day"])
        self.assertTrue(any("2 more will raise it" in line for line in a.heard))
        b._visit()
        self.assertEqual(b.crates, 0)
        self.assertTrue(any("raises the mast" in line for line in b.heard))
        self.assertEqual(b.stations[3]["mast_day"], b.today)
        c._visit()                                        # c had not heard: the mast wants nothing more
        self.assertEqual((c.crates, c.postmarks), (2, was[2]), "the third keeps the crates and is paid nothing")
        self.assertTrue(any("is a mast now" in line and "crates stay aboard" in line for line in c.heard), c.heard)
        self.assertTrue(c.at_office())
        self.assertEqual(self.rows("SELECT freight, mast_day IS NOT NULL FROM stations"), [(fw.MAST_CRATES, 1)])
        self.assertEqual(self.rows("SELECT user_id, handle, crates FROM contributions ORDER BY user_id"),
                         [(1, "Ada", 2), (2, "Bo", 2)])
        self.assertEqual(support.stored(self.folder, 2)["crates"], 0)
        for game in (b, c):
            game.close()

    def test_a_station_takes_only_the_crates_it_still_wants(self):
        game = self.game()
        station = self.plant(game)
        game.crates = fw.MAST_CRATES + 2
        self.down(game, station["x"], station["y"])
        game._visit()
        self.assertEqual(game.crates, 2)
        self.assertIsNotNone(game.stations[3]["mast_day"])

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found, done: (got.append(done), {})[1], {'day': 1, 'turn': 1},\n"
              "         'station', deed=lambda db: fw.visit_station(db, 3, user, 'P%d' % user, 1, 1))\n"
              "done = got[0]\n"
              "print(0 if done is None else done['took'], 0 if done is None else int(done['raised']))\n"
              "s.release(user); s.close()\n")

    def test_eight_processes_unloading_at_once_raise_it_once(self):
        game = self.game()
        self.plant(game)
        seed = game.world.seed
        game.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for user in range(101, 109)]
        took = raised = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            a, b = out.decode().split()
            took, raised = took + int(a), raised + int(b)
        self.assertEqual((took, raised), (fw.MAST_CRATES, 1), "four crates taken of eight, one raising")
        self.assertEqual(self.rows("SELECT freight FROM stations"), [(fw.MAST_CRATES,)])
        self.assertEqual(self.rows("SELECT count(*), sum(crates) FROM contributions"), [(4, 4)])

    def test_a_mast_is_lit_sells_fuel_and_has_a_post_office(self):
        game = self.game(handle="Ada")
        station = self.plant(game)
        x, y = station["x"], station["y"]
        night = dataclasses.replace(game.state, x=x, y=y, status="aloft", layer=game.engine.lowest_open(x, y),
                                    turn=(22 * 60 - fw.DAWN_MINUTE) // 15)
        real = fw.WeatherFrame.surface_speed, fw.WeatherFrame.fog
        try:
            fw.WeatherFrame.surface_speed = lambda self, wx, wy: 5.0
            fw.WeatherFrame.fog = lambda self, wx, wy: 0.9
            self.assertIsNone(game.engine.place(x, y), "a station is no lit field")
            self.assertEqual(game.engine.landing_class(night), "hard")
            self.assertFalse(game.at_office())
            mast = self.raise_mast(game, station)
            self.assertEqual(game.engine.place(x, y), ("station", 3))
            self.assertEqual(game.engine.landing_class(night), "clean", "night and fog do not matter at a mast")
        finally:
            fw.WeatherFrame.surface_speed, fw.WeatherFrame.fog = real
        self.assertTrue(fw.staffed(mast))
        self.assertTrue(game.at_office())
        self.assertFalse(game.at_loft(), "the loft is in towns")
        game.postmarks = 100
        game.state = dataclasses.replace(game.state, fuel=10)
        game.buy("fuel", 10)
        self.assertEqual((game.state.fuel, game.postmarks), (20, 100 - 10 * fw.price_of("fuel", ("station", 3))))
        offers = game.offers()
        self.assertEqual(len(offers), fw.STATION_OFFERS)
        self.assertEqual(offers[0]["origin"], ["station", 3])
        game.key("o")
        text = self.screen(game)
        self.assertIn("a mooring mast", text)
        self.assertIn("freight of Ada", text)
        game.key("ENTER")
        self.assertEqual(game._station_mark(mast), ("Y", "cool"))
        self.assertIsNone(game.hut(), "nothing more to restock or to raise")

    def test_a_mast_never_lapses_and_post_is_sent_there(self):
        a = self.game(user_id=1)
        b = self.other(2)
        station = self.plant(a, *self.spot(a, least=200.0))
        self.raise_mast(a, station)
        self.days(40, a, b)
        self.assertEqual(b.coverage(station["x"], station["y"]), 1.0)
        near = fw.places_near(b.world, station["x"] + 1.0, station["y"], stations=b.stations)
        self.assertEqual(near[0], ("station", 3))
        self.assertNotIn(("station", 3), fw.places_near(b.world, station["x"], station["y"]),
                         "the world alone knows only its first three")
        town = near[1] if near[1][0] == "town" else next(ref for ref in near if ref[0] == "town")
        sent = [offer for day in range(1, 40) for offer in fw.contracts_at(b.world, town, day, b.stations)
                if offer["dest"] == ["station", 3]]
        self.assertTrue(sent, "the nearest town has post for the mast on some day")
        b.close()

    def test_cargo_for_a_mast_is_there_at_the_next_call(self):
        game = self.game()
        station = self.plant(game)
        self.raise_mast(game, station)
        self.empty(game)
        game._load_cargo({"contract": None, "kind": "mail", "dest": ["station", 3], "origin": None, "weight": 1,
                          "payout": 20, "due": game.today + 5})
        game.save()
        again = self.game()
        self.assertEqual(again.manifest[0]["dest"], ["station", 3])
        self.assertEqual(again.place_of(("station", 3))["name"], station["name"])
        self.assertIn(station["name"], self.screen(again))
        was = again.deliveries
        self.land_at(again, ("station", 3))
        self.assertEqual(again.deliveries, was + 1)

    def test_what_was_known_stays_known_when_the_records_go(self):
        game = self.game()
        station = self.plant(game)
        game._drop_store()
        game._see_stations()
        self.assertEqual(game.stations[3]["name"], station["name"])


class Records(StationCase):
    def test_a_world_from_before_the_stations_is_brought_forward(self):
        seed = fw.world_seed("relink-test")
        path = self.folder / fw.db_name(seed)
        db = sqlite3.connect(str(path))
        for step in (1, 2):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=2")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.execute("INSERT INTO charted VALUES (77, 4, 1)")
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.assertIn(77, game.chart)
        self.assertEqual(len(game.stations), 3)
        self.plant(game)
        self.assertEqual(self.rows("PRAGMA user_version"), [(3,)])
        self.assertEqual(self.rows("SELECT count(*) FROM stations"), [(1,)])

    def test_the_help_names_them(self):
        text = " ".join(fw.PLAIN_HELP)
        for word in ("`K` plants a station kit", "`Y` mast", "restocks it", "no fuel there"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
