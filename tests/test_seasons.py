"""Seasons (S22): the regime, the node's goal, the race of the last week, medals, and what is never reset."""

import dataclasses
import json
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


class SeasonCase(RescueCase):
    def db(self):
        return sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))

    def to_day(self, day, *games):
        """Move the calendar on to a world day, and have every game look."""
        self.clock.days(day - games[0].today)
        for game in games:
            game._day_may_have_opened()
        self.assertEqual(games[0].today, day)

    def flight(self, game, km, day=None):
        """A flight of so many km in a straight line that has just ended, on the day the calendar has come to."""
        day = day or game.today
        game.lift_off = [100.0, 100.0]
        game.state = dataclasses.replace(game.state, x=100.0 + km, y=100.0, day=day, status="ground", layer=0)
        game._race(day)


class Calendar(unittest.TestCase):
    def test_a_season_is_twenty_eight_days(self):
        self.assertEqual(fw.SEASON_DAYS, 28)
        self.assertEqual([fw.season_of(d) for d in (1, 28, 29, 56, 57)], [(1, 1), (1, 28), (2, 1), (2, 28), (3, 1)])
        self.assertEqual((fw.season_span(1), fw.season_span(3)), ((1, 28), (57, 84)))
        self.assertEqual([fw.in_race(d) for d in (21, 22, 28, 29, 49, 50)], [False, True, True, False, False, True])

    def test_each_season_turns_the_prevailing_flow_as_the_weather_says(self):
        world = fw.World(fw.world_seed("test-node"))
        weather = fw.Weather(world)
        regimes = [weather._season(n) for n in range(6)]
        self.assertGreater(len({json.dumps(r) for r in regimes}), 4, "seasons are not all alike")
        for layers, pulse, cold in regimes:
            self.assertEqual(len(layers), fw.LAYERS)
            for li, (tau, speed, sign) in enumerate(layers):
                self.assertTrue(0.8 * fw.WIND_BASE[li] - 1e-9 <= speed <= 1.2 * fw.WIND_BASE[li] + 1e-9,
                                "a layer's speed is within a fifth of its base")
                self.assertIn(sign, (-1.0, 1.0))
            self.assertEqual(layers[0][2], layers[1][2], "the two lowest layers run the same way: trades or not")
            self.assertTrue(all(layer[2] == 1.0 for layer in layers[2:]), "and only they ever run as trades")
            self.assertTrue(0.85 <= pulse <= 1.15)
        # The regime of a day is the regime of its season: the same within one, another in the next.
        mid = [weather._regime(day, 720) for day in (10, 20, 38, 48)]
        self.assertEqual(mid[0], mid[1])
        self.assertEqual(mid[2], mid[3])
        self.assertNotEqual(mid[0], mid[2])
        # And the wind itself turns with it, at the same place and hour.
        calm = [weather.frame(day, 720).wind(800.0, 500.0, 5) for day in (10, 38, 66, 94)]
        self.assertGreater(len({(round(vx), round(vy)) for vx, vy in calm}), 2)


class Goal(SeasonCase):
    def test_a_season_has_one_goal_set_once_and_the_same_for_everyone(self):
        a = self.game(user_id=1)
        b = self.other(2)
        kind = a.season_now["kind"]
        self.assertIn(kind, fw.SEASON_GOALS)
        self.assertEqual((a.season_now["target"], a.season_now["season"]), (fw.SEASON_GOALS[kind]["target"], 1))
        self.assertEqual((b.season_now["kind"], b.season_now["target"]), (kind, a.season_now["target"]))
        self.assertEqual(self.rows("SELECT season, kind, closed FROM seasons"), [(1, kind, 0)])
        db = self.db()
        self.assertEqual(fw.begin_season(db, a.world.seed, 1, len(a.winds)), (kind, a.season_now["target"]))
        db.close()
        b.close()

    def test_goals_differ_by_season_and_pass_over_what_the_world_has_no_room_for(self):
        game = self.game()
        db = self.db()
        kinds = [fw.begin_season(db, game.world.seed, n, len(game.winds))[0] for n in range(1, 13)]
        self.assertGreater(len(set(kinds)), 2)
        db.execute("DELETE FROM seasons")
        db.executemany("INSERT INTO almanac VALUES (?, 9, 'Bo', 1)", [(i,) for i in range(len(game.winds))])
        db.executemany("INSERT INTO wonders VALUES (?, 9, 'Bo', 1)", [(i,) for i in range(fw.WONDER_COUNT)])
        full = [fw.begin_season(db, game.world.seed, n, len(game.winds))[0] for n in range(1, 13)]
        self.assertFalse({"winds", "wonders"} & set(full), "no goal that cannot be met any more")
        db.close()

    def test_every_goal_is_within_one_pilots_reach_and_two_pilots_add_up(self):
        # What one active pilot does in a season, from the measurements in docs/systems.md: charts about twenty
        # cells a flying day, can buy two kits, carry four crates in two flights, and so on.
        within = {"chart": 20 * 14, "plant": 3, "mast": 1, "wonders": 6, "winds": 3}
        for kind, spec in fw.SEASON_GOALS.items():
            self.assertLessEqual(spec["target"], within[kind], kind)
        game = self.game()
        db = self.db()
        db.executemany("INSERT INTO charted VALUES (?, ?, ?)",
                       [(9000 + i, 1 if i < 30 else 2, 3) for i in range(50)] + [(9100, 1, 40)])
        db.executemany("INSERT INTO pilots VALUES (?, ?, 1.0, '{}')", ((1, "Ada"), (2, "Bo")))
        db.commit()
        total, hands = fw.goal_progress(db, "chart", 1, 28)
        self.assertEqual((total, hands), (50, [(1, "Ada", 30), (2, "Bo", 20)]), "what each did, and the sum of it")
        self.assertEqual(fw.goal_progress(db, "chart", 29, 56)[0], 1, "a cell counts in the season it was charted in")
        db.execute("INSERT INTO stations VALUES (3, 'A Hut', 1.0, 1.0, 1, 'Ada', 5, 14, 5, 4, 9)")
        db.execute("INSERT INTO stations VALUES (4, 'B Hut', 90.0, 1.0, 2, 'Bo', 30, 14, 30, 0, NULL)")
        db.executemany("INSERT INTO contributions VALUES (3, ?, ?, ?)", ((1, "Ada", 1), (2, "Bo", 3)))
        db.execute("INSERT INTO wonders VALUES (0, 2, 'Bo', 6)")
        db.execute("INSERT INTO almanac VALUES (0, 1, 'Ada', 7)")
        self.assertEqual(fw.goal_progress(db, "plant", 1, 28), (1, [(1, "Ada", 1)]))
        self.assertEqual(fw.goal_progress(db, "mast", 1, 28), (1, [(2, "Bo", 3), (1, "Ada", 1)]),
                         "a mast is the work of everyone whose crates are in it")
        self.assertEqual(fw.goal_progress(db, "wonders", 1, 28), (1, [(2, "Bo", 1)]))
        self.assertEqual(fw.goal_progress(db, "winds", 1, 28), (1, [(1, "Ada", 1)]))
        db.close()
        game.close()

    def test_a_goal_that_was_met_pays_everyone_who_had_a_hand_in_it_once(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        c = self.other(3, handle="Cy")
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, ?, ?)", [(9000 + i, 1 if i < 30 else 2, 3) for i in range(50)])
        db.commit()
        db.close()
        was = [g.postmarks for g in (a, b, c)]
        self.to_day(28 + fw.SEASON_GRACE, a, b, c)
        self.assertEqual([g.postmarks for g in (a, b, c)], was, "not until every day of it has lapsed")
        self.to_day(29 + fw.SEASON_GRACE, a, b, c)
        self.assertEqual(self.rows("SELECT closed, met FROM seasons WHERE season=1"), [(1, 1)])
        self.assertEqual([g.postmarks - w for g, w in zip((a, b, c), was)], [fw.GOAL_PAY, fw.GOAL_PAY, 0],
                         "the two who charted, and not the one who did not")
        self.assertTrue(any("Season 1 is over and its goal was met" in line for line in a.log))
        self.assertEqual(support.stored(self.folder, 1)["postmarks"], a.postmarks)
        for game in (a, b, c):
            game._day_may_have_opened()
        again = self.game(user_id=1, handle="Ada")
        self.assertEqual(again.postmarks, was[0] + fw.GOAL_PAY, "and not again at the next call")
        self.assertEqual(self.rows("SELECT count(*) FROM season_claims WHERE season=1"), [(3,)])
        for game in (b, c):
            game.close()

    def test_what_is_owed_does_not_depend_on_when_it_is_asked_for(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, 1, 3)", [(9000 + i,) for i in range(45)])
        db.commit()
        db.close()
        was = (a.postmarks, b.postmarks)
        self.to_day(29 + fw.SEASON_GRACE, a)              # a settles it and is paid; b has not looked yet
        self.assertEqual(self.rows("SELECT user_id FROM season_hands WHERE season=1"), [(1,)])
        db = self.db()
        db.execute("INSERT INTO charted VALUES (9999, 2, 20)")     # b, whose logbook stood on day 20, charts now
        db.commit()
        db.close()
        b._day_may_have_opened()
        self.assertEqual((a.postmarks - was[0], b.postmarks - was[1]), (fw.GOAL_PAY, 0),
                         "a hand put to it after it was settled is owed nothing")
        self.assertEqual(self.rows("SELECT count(*) FROM season_claims WHERE season=1"), [(2,)])
        b.close()

    def test_a_pilot_long_away_is_still_given_what_old_seasons_owe(self):
        a = self.game(user_id=1, handle="Ada")
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, 1, 3)", [(9000 + i,) for i in range(45)])
        db.executemany("INSERT INTO race VALUES (1, ?, ?, ?, 25)", ((1, "Ada", 200.0), (2, "Bo", 100.0)))
        db.commit()
        db.close()
        was = a.postmarks
        a.close()
        self.clock.days(28 * 9 + fw.SEASON_GRACE + 1)     # nine seasons go by with nobody calling
        back = self.game(user_id=1, handle="Ada")
        self.assertEqual(fw.season_of(back.today)[0], 10)
        self.assertEqual(back.postmarks, was + fw.GOAL_PAY + fw.RACE_PAY[0])
        closed = dict(self.rows("SELECT season, closed FROM seasons"))
        self.assertEqual([closed.get(n) for n in range(1, 11)], [1] * 9 + [0], "every season between is settled")
        self.assertEqual(self.rows("SELECT count(*) FROM season_claims WHERE user_id=1"), [(9,)])

    def test_the_look_is_cheap_once_the_day_is_settled_and_backs_off_when_it_cannot_write(self):
        game = self.game()
        self.to_day(40, game)
        writes, reads = [], []
        real_write, real_read = game.store.write, game.store.read
        game.store.write = lambda *args, **kwargs: writes.append(1) or real_write(*args, **kwargs)
        game.store.read = lambda *args, **kwargs: reads.append(1) or real_read(*args, **kwargs)
        for _ in range(5):
            game._see_season()
        self.assertEqual(len(writes), 0, "nothing is written again for a day already settled")
        self.assertEqual(len(reads), 5, "and only the season under way is read")

        def broken(*args, **kwargs):
            writes.append(1)
            raise sqlite3.OperationalError("database is locked")
        game.store.write = broken
        self.clock.days(1)
        game._look_at_the_calendar()
        for _ in range(5):
            game._see_season()
        self.assertEqual(len(writes), 1, "a settling that cannot be written is not tried again at every look")
        self.clock.now += fw.SEASON_RETRY_SECONDS + 1
        game.store.write = real_write
        game._see_season()
        self.assertEqual(game._season_done, (fw.season_of(game.today)[0], game.today))

    def test_a_goal_that_was_not_met_pays_nobody(self):
        a = self.game(user_id=1)
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, 1, 3)", [(9000 + i,) for i in range(39)])
        db.commit()
        db.close()
        was = a.postmarks
        self.to_day(29 + fw.SEASON_GRACE, a)
        self.assertEqual(self.rows("SELECT closed, met FROM seasons WHERE season=1"), [(1, 0)])
        self.assertEqual(a.postmarks, was)


class Race(SeasonCase):
    def test_the_longest_flight_of_the_last_week_stands(self):
        game = self.game(handle="Ada")
        self.to_day(21, game)
        self.flight(game, 300.0)
        self.assertEqual(self.rows("SELECT count(*) FROM race"), [(0,)], "day 21 is not yet the race")
        self.to_day(22, game)
        self.flight(game, 80.0)
        self.assertTrue(any("your longest in this season's race" in line for line in game.log))
        self.flight(game, 60.0)
        self.flight(game, 140.4)
        self.assertEqual(self.rows("SELECT season, user_id, handle, round(km, 1), day FROM race"),
                         [(1, 1 if False else 12, "Ada", 140.4, 22)])
        self.to_day(28, game)
        self.flight(game, 139.0)
        self.assertEqual(self.rows("SELECT round(km, 1) FROM race"), [(140.4,)], "a shorter one changes nothing")

    def test_a_flight_ending_through_the_game_is_entered(self):
        game = self.game()
        self.to_day(24, game)
        ref = game.targets[0]
        start = (game.state.x, game.state.y)
        game.state = dataclasses.replace(game.state, day=24)
        game.lift_off = list(start)
        self.land_at(game, ref)
        place = game.place_of(ref)
        km = ((place["x"] - start[0]) ** 2 + (place["y"] - start[1]) ** 2) ** 0.5
        rows = self.rows("SELECT round(km, 1) FROM race")
        self.assertEqual(rows, [(round(km, 1),)])

    def test_the_race_is_scored_once_and_at_most_three_take_medals(self):
        games = [self.game(user_id=1, handle="Ada")] + [self.other(n, handle=h) for n, h in
                                                        ((2, "Bo"), (3, "Cy"), (4, "Di"), (5, "Ed"))]
        self.to_day(25, *games)
        for game, km in zip(games, (210.0, 340.0, 90.0, 340.0, 15.0)):
            self.flight(game, km)
        self.to_day(27, *games)
        self.flight(games[2], 400.0)                      # Cy flies again, later in the week
        was = [g.postmarks for g in games]
        self.to_day(29 + fw.SEASON_GRACE, *games)
        medals = self.rows("SELECT place, handle, round(km) FROM season_medals WHERE season=1 ORDER BY place")
        self.assertEqual(medals, [(0, "Cy", 400.0), (1, "Bo", 340.0), (2, "Di", 340.0)],
                         "the longest; of two alike, the one flown first, then the lower number")
        self.assertEqual(len(medals), 3, "no more than three")
        paid = [g.postmarks - w for g, w in zip(games, was)]
        self.assertEqual([paid[2], paid[1], paid[3]], list(fw.RACE_PAY))
        self.assertTrue(paid[0] < fw.RACE_PAY[2] and paid[4] < fw.RACE_PAY[2], "fourth and fifth have no medal")
        self.assertTrue(any("You took gold in the race of season 1" in line for line in games[2].log))
        db = self.db()
        self.assertFalse(fw.close_season(db, games[0].world.seed, 1, len(games[0].winds)), "settled already")
        self.assertEqual(fw.race_fly(db, 9, "Late", 999.0, 26), -1, "and a closed race takes no more flights")
        db.close()
        late = games[4]
        self.flight(late, 999.0, day=26)                  # Ed was stranded since the race, and lands only now
        self.assertTrue(any("came too late" in line for line in late.log), late.log)
        self.assertEqual(self.rows("SELECT handle FROM season_medals WHERE season=1 ORDER BY place"),
                         [("Cy",), ("Bo",), ("Di",)])
        self.assertEqual(self.rows("SELECT count(*) FROM season_medals"), [(3,)])
        for game in games:
            game._day_may_have_opened()
        self.assertEqual([g.postmarks - w for g, w in zip(games, was)], paid, "nothing is paid twice")
        for game in games[1:]:
            game.close()

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[3]), 'node')\n"
              "print(int(s.write(lambda db: fw.close_season(db, int(sys.argv[3]), 1, 14)))); s.close()\n")

    def test_eight_processes_settling_a_season_at_once(self):
        game = self.game()
        seed = game.world.seed
        db = self.db()
        db.executemany("INSERT INTO race VALUES (1, ?, ?, ?, 25)", [(n, f"P{n}", 100.0 + n) for n in range(1, 7)])
        db.commit()
        db.close()
        game.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        settled = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            settled += int(out.decode().strip())
        self.assertEqual(settled, 1)
        self.assertEqual(self.rows("SELECT place, user_id FROM season_medals ORDER BY place"), [(0, 6), (1, 5), (2, 4)])

    def test_a_season_nobody_looked_at_is_settled_all_the_same(self):
        game = self.game()
        self.to_day(3 * 28 + 10, game)
        closed = dict(self.rows("SELECT season, closed FROM seasons"))
        self.assertEqual((closed.get(1), closed.get(2), closed.get(3), closed.get(4)), (1, 1, 1, 0))


class NothingReset(SeasonCase):
    def test_a_new_season_takes_nothing_back(self):
        game = self.game()
        game.postmarks, game.points = 777, 400
        game.gear["burner"] = 2
        game.medals = [3, 2] + [0] * 10
        game.taught = True
        game.save("hold", [5000, 5001, 5002])
        game.kits = 1
        game.save()
        station = self.plant(game)
        before = (game.postmarks, game.points, dict(game.gear), list(game.medals), game.charted, sorted(game.chart),
                  len(game.stations), game.deliveries)
        self.to_day(29, game)
        self.assertEqual(fw.season_of(game.today)[0], 2)
        self.assertEqual(game.season_now["season"], 2)
        after = (game.postmarks, game.points, dict(game.gear), list(game.medals), game.charted, sorted(game.chart),
                 len(game.stations), game.deliveries)
        self.assertEqual(after, before)
        self.to_day(40, game)
        again = self.game()
        self.assertEqual((again.postmarks >= before[0], again.gear["burner"], again.medals[:2], again.taught),
                         (True, 2, [3, 2], True))
        self.assertEqual(sorted(again.chart), before[5], "and the map is never reset")
        self.assertEqual(again.stations[3]["name"], station["name"])


class Screen(SeasonCase):
    def test_the_season_screen_says_how_it_stands(self):
        a = self.game(user_id=1, handle="Ada")
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, 1, 3)", [(9000 + i,) for i in range(10)])
        db.commit()
        db.close()
        a.key("y")
        self.assertTrue(a.season_open)
        text = self.screen(a)
        for word in ("Season 1", "day 1 of 28", "THE NODE'S GOAL", "Chart 40 cells nobody has seen", "10 of 40", "Ada 10",
                     "THE RACE", "From day 22: the longest straight line from launch to landing in one flight.",
                     "60 postmarks at the season's end.", "SEASONS PAST", "Nothing a pilot owns is taken back"):
            self.assertIn(word, text)
        self.assertIn(a.season_weather(1)[:40], text)
        a.key("x")
        self.assertFalse(a.season_open)
        self.to_day(23, a)
        self.flight(a, 120.0)
        a.key("y")
        text = self.screen(a)
        self.assertIn("On now, until day 28: the longest straight line from launch to landing.", text)
        self.assertIn("1 Ada 120 km", text)
        a.key("ENTER")
        self.to_day(29 + fw.SEASON_GRACE, a)
        a.key("y")
        text = self.screen(a)
        self.assertIn("Season 2", text)
        self.assertIn("Season 1: chart 40 cells nobody has seen: not met.", text)
        self.assertIn("gold Ada 120 km", text)
        for size in ((100, 30), (132, 40)):
            self.screen(a, *size)

    def test_in_the_days_before_a_season_is_settled_the_older_ones_are_still_shown(self):
        game = self.game(handle="Ada")
        self.to_day(26, game)
        self.flight(game, 150.0)
        self.to_day(29 + fw.SEASON_GRACE, game)           # season 1 is settled
        self.to_day(57, game)                             # the first day of season 3: season 2 is over, not settled
        game.close()
        fresh = self.game(handle="Ada")
        self.assertEqual([(p["season"], p["closed"]) for p in fresh.season_past], [(2, False), (1, True)])
        fresh.key("y")
        text = self.screen(fresh)
        self.assertIn("Season 2:", text)
        self.assertIn("being settled", text)
        self.assertIn("Season 1:", text)
        self.assertIn("gold Ada 150 km", text)
        self.to_day(57 + fw.SEASON_GRACE, fresh)
        self.assertEqual([(p["season"], p["closed"]) for p in fresh.season_past], [(2, True), (1, True)])

    def test_dues_that_could_not_be_read_or_written_are_asked_for_again_and_not_at_every_look(self):
        game = self.game(handle="Ada")
        db = self.db()
        db.execute("UPDATE seasons SET kind='chart', target=40")
        db.executemany("INSERT INTO charted VALUES (?, 12, 3)", [(9000 + i,) for i in range(45)])
        db.commit()
        db.close()
        was = game.postmarks
        self.clock.days(28 + fw.SEASON_GRACE)
        game._look_at_the_calendar()
        real_read, real_commit = game.store.read, game.store.commit

        def no_read(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        game.store.read = no_read
        game._see_season()
        self.assertNotEqual(game._season_done, (fw.season_of(game.today)[0], game.today),
                            "the day is not done until the dues were read as well")
        game.store.read = real_read
        tries = []

        def no_commit(*args, **kwargs):
            tries.append(1)
            raise sqlite3.OperationalError("disk I/O error")
        game.store.commit = no_commit
        self.clock.now += fw.SEASON_RETRY_SECONDS + 1
        for _ in range(5):
            game._see_season()
        self.assertEqual((len(tries), game.postmarks), (1, was), "dues that cannot be written are not retried at once")
        game.store.commit = real_commit
        game.saved_ok = True
        self.clock.now += fw.SEASON_RETRY_SECONDS + 1
        game._see_season()
        self.assertEqual(game.postmarks, was + fw.GOAL_PAY)

    def test_the_weather_line_says_what_the_regime_is(self):
        game = self.game()
        seen = set()
        for season in range(1, 30):
            line = game.season_weather(season)
            layers, pulse, cold = game.weather._season(season - 1)
            self.assertEqual("trades" in line, layers[0][2] < 0)
            self.assertEqual("ice from layer 4" in line, cold)
            self.assertEqual("Gyre strong" in line, pulse > 1.05)
            self.assertLessEqual(len(line), 78)
            seen.add(line)
        self.assertGreater(len(seen), 3)

    def test_the_help_names_it_and_a_visitor_has_no_season(self):
        self.assertIn("`Y` season", " ".join(fw.PLAIN_HELP))
        guest = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        guest.key("y")
        self.assertIn("no goal on record", self.screen(guest))
        guest.close()

    def test_a_world_from_before_is_brought_forward(self):
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in range(1, 8):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=7")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.execute("INSERT INTO wonders VALUES (3, 9, 'Bo', 2)")
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.assertIn(3, game.sighted)
        self.assertEqual(self.rows("PRAGMA user_version"), [(fw.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT count(*) FROM seasons"), [(1,)])


if __name__ == "__main__":
    unittest.main()
