"""The Daily Drift (S19): the day's course, the standard balloon, one scored attempt, practice, the table."""

import dataclasses
import json
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
from test_plain import launch                             # noqa: E402
from test_rescue import RescueCase                        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORLD = fw.World(fw.world_seed("test-node"))
WEATHER = fw.Weather(WORLD)


class DriftCase(RescueCase):
    def fly_out(self, game, land_after=3):
        """Fly the Drift that is under way: launch, hold a few turns, land as soon as it can be done."""
        game.key("ENTER")                                 # launch
        for _ in range(fw.DRIFT_TURNS + 2):
            if game.drift is None:
                return
            turns = game.state.turn - fw.DRIFT_START_TURN
            if turns >= land_after and "land" in game.offered():
                game.key("l")
            game.key("ENTER")
        self.assertIsNone(game.drift, "a Drift ends")

    def main(self, game):
        return (game.state, json.dumps(game._career(), sort_keys=True), game.gear, sorted(game.chart))


class Course(unittest.TestCase):
    def test_the_same_day_gives_the_same_course_in_another_process(self):
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
                "w = fw.World(fw.world_seed('test-node'))\n"
                "print(json.dumps([fw.drift_course(w, fw.Weather(w), d) for d in (1, 2, 17)]))\n")
        out = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [fw.drift_course(WORLD, WEATHER, d) for d in (1, 2, 17)])

    def test_every_day_has_a_start_in_a_town_and_a_mark_on_land_in_reach(self):
        starts = set()
        for day in range(1, 41):
            course = fw.drift_course(WORLD, WEATHER, day)
            self.assertEqual(course["day"], day)
            self.assertTrue(any(t["name"] == course["start"] and (t["x"], t["y"]) == (course["x"], course["y"])
                                for t in WORLD.towns))
            self.assertGreaterEqual(WORLD.sample(course["mx"], course["my"])[1], fw.SHORE, "the mark is on land")
            far = math.hypot(course["mx"] - course["x"], course["my"] - course["y"])
            self.assertLess(far, 200.0, day)
            starts.add(course["start"])
        self.assertGreater(len(starts), 5, "the start moves about")
        self.assertNotEqual(fw.drift_course(WORLD, WEATHER, 3), fw.drift_course(WORLD, WEATHER, 4))

    def test_the_mark_can_be_come_down_near(self):
        # A plain scripted flight, the one the course is laid by, ends within the mark's ring on most days.
        engine = fw.Flight(WORLD, WEATHER, "drift")
        near = 0
        for day in range(1, 21):
            course = fw.drift_course(WORLD, WEATHER, day)
            state = fw.drift_state(course["x"], course["y"], day)
            for action in fw.DRIFT_SCOUT:
                offered = engine.actions(state)
                action = action if action in offered else "hold" if "hold" in offered else None
                if action is None:
                    break
                state = engine.step(state, action).state
                if state.status != "aloft":
                    break
            near += math.hypot(state.x - course["mx"], state.y - course["my"]) <= fw.DRIFT_MARK_KM[1] + 0.01
        self.assertGreaterEqual(near, 18)

    def test_the_standard_balloon(self):
        state = fw.drift_state(10.0, 20.0, 7)
        self.assertEqual((state.fuel, state.ballast, state.pibals, state.ceiling, state.reads, state.turn, state.day),
                         (40, 3, 1, 3, 0, fw.DRIFT_START_TURN, 7))
        self.assertEqual(state.minute, 9 * 60)

    def test_the_score(self):
        self.assertEqual(fw.drift_score(0.0, 0, 0), 1000)
        self.assertEqual(fw.drift_score(10.0, 12, 20), 1000 - 80 - 48 + 40)
        self.assertEqual(fw.drift_score(500.0, 24, 0), 0, "never below nothing")
        self.assertEqual(fw.drift_score(0.0, 5, 30, ditched=True), 0, "water scores nothing")
        self.assertGreater(fw.drift_score(5.0, 10, 10), fw.drift_score(6.0, 10, 10))
        self.assertGreater(fw.drift_score(5.0, 10, 10), fw.drift_score(5.0, 11, 10))
        self.assertGreater(fw.drift_score(5.0, 10, 11), fw.drift_score(5.0, 10, 10))

    def test_the_help_shows_the_formula_and_the_tie_break(self):
        text = " ".join(fw.DRIFT_HELP)
        for word in ("1000 points", "less 8 for each km", "less 4 for each turn", "plus 2 for each unit of fuel",
                     "water scores nothing", "nearer the mark, then by fewer turns", "then by who finished first",
                     "once a world day", "never on the table"):
            self.assertIn(word.lower(), text.lower())
        self.assertLessEqual(len(fw.DRIFT_HELP), 21)
        self.assertTrue(all(len(line.replace("`", "")) <= 78 and line.count("`") % 2 == 0 for line in fw.DRIFT_HELP))
        self.assertEqual((fw.DRIFT_SCORE["full"], fw.DRIFT_SCORE["km"], fw.DRIFT_SCORE["turn"], fw.DRIFT_SCORE["fuel"]),
                         (1000, 8, 4, 2), "the help says what the table says")


class Flying(DriftCase):
    def test_the_lobby_shows_the_course_the_rules_and_the_table(self):
        game = self.game()
        game.key("t")
        self.assertTrue(game.drift_lobby)
        text = self.screen(game)
        self.assertIn("then fewer turns, then first.", text, "the rules in short are not cut off at 80 columns")
        self.assertIn("for every pilot.", text)
        course = fw.drift_course(game.world, game.weather, game.today)
        for word in ("The Daily Drift", f"from {course['start']}", "40 fuel, 3 sacks, 1 pibal", "Score: 1000",
                     "Nobody has flown it yet today", "You have not flown it today", "[Enter] Fly it for the table",
                     "[P] Practise", "[?] The rules", "[B] Back"):
            self.assertIn(word, text)
        self.assertEqual(game.key("?"), "help")
        self.assertIs(game.help_text(), fw.DRIFT_HELP)

        class Quiet:
            def write(self, text):
                pass
        session = fw.Session(dataclasses.replace(game.launch, width=80, height=24), None, Quiet(), game=game)
        session.view = "help"
        session.paint()
        shown = "\n".join("".join(session.frame.ch[r * 80:(r + 1) * 80]) for r in range(24))
        self.assertIn("THE SCORE", shown)
        self.assertIn("less 8 for each km", shown)
        self.assertIn("then by who finished first", shown)
        game.key("b")
        self.assertFalse(game.drift_lobby)
        self.assertIs(game.help_text(), fw.PLAIN_HELP)

    def test_practice_touches_neither_the_table_nor_the_pilot(self):
        game = self.game()
        before = self.main(game)
        for _ in range(2):
            game.key("t")
            game.key("p")
            self.assertEqual((game.drift["scored"], game.state.fuel, game.state.turn), (False, 40, fw.DRIFT_START_TURN))
            self.assertIn("The Drift:", self.screen(game))
            self.fly_out(game)
            self.assertTrue(game.drift_lobby)
            self.assertIn("Practice:", game.drift_note)
            self.assertIn("That would score", self.screen(game))
            game.key("b")
        self.assertEqual(self.main(game), before, "the pilot's own flight, post, gear and chart are as they were")
        self.assertEqual(self.rows("SELECT count(*) FROM drifts"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM charted"), [(0,)])
        saved = support.stored(self.folder)
        self.assertEqual((saved["state"]["turn"], saved["state"]["fuel"]), (before[0].turn, before[0].fuel))

    def test_the_scored_drift_is_flown_once_and_pays(self):
        game = self.game(handle="Ada")
        before = self.main(game)
        was = game.postmarks
        game.key("t")
        game.key("ENTER")
        self.assertTrue(game.drift["scored"])
        self.assertEqual(self.rows("SELECT user_id, done FROM drifts"), [(12, 0)], "begun: it is on record at once")
        self.fly_out(game)
        row = self.rows("SELECT done, score, turns, fuel FROM drifts")[0]
        self.assertEqual(row[0], 1)
        self.assertEqual(game.postmarks, was + row[1] // fw.DRIFT_PAY_PER)
        self.assertIn(f"Scored {row[1]}", game.drift_note)
        text = self.screen(game)
        self.assertIn(f"You scored {row[1]} today", text)
        self.assertIn("Ada", text)
        self.assertNotIn("Fly it for the table", text)
        self.assertEqual(support.stored(self.folder)["postmarks"], game.postmarks)
        state, career, gear, chart = self.main(game)
        self.assertEqual((state, gear, chart), (before[0], before[2], before[3]))
        game.key("ENTER")
        self.assertIsNone(game.drift, "there is no second attempt")
        self.assertIn("You have flown today's Drift", game.drift_note)
        self.assertEqual(self.rows("SELECT count(*) FROM drifts"), [(1,)])
        self.assertEqual(game.postmarks, was + row[1] // fw.DRIFT_PAY_PER)

    def test_no_reconnect_gives_a_second_attempt(self):
        game = self.game()
        game.key("t")
        game.key("ENTER")
        game.key("ENTER")                                 # launched
        game.key("ENTER")
        part = game.state
        self.assertEqual(part.turn, fw.DRIFT_START_TURN + 2)
        again = self.game()                               # the call drops; the pilot calls back
        self.assertIsNone(again.drift)
        self.assertEqual(again.state.turn, 0, "their own flight is where it was")
        again.key("t")
        self.assertIn("Your Drift waits part way", self.screen(again))
        self.assertIn("[Enter] Take it up", self.screen(again))
        again.key("ENTER")
        self.assertEqual(again.state, part, "taken up exactly where it was left, not from the start")
        again.key("x")                                    # a scored Drift is parked by one X: nothing is lost
        self.assertIn("waits as you left it", again.drift_note)
        again.key("ENTER")
        self.assertEqual(again.state, part)
        self.fly_out_from_air(again)
        third = self.game()
        third.key("t")
        third.key("ENTER")
        self.assertIsNone(third.drift)
        self.assertEqual(self.rows("SELECT count(*), sum(done) FROM drifts"), [(1, 1)])

    def fly_out_from_air(self, game):
        for _ in range(fw.DRIFT_TURNS + 2):
            if game.drift is None:
                return
            if "land" in game.offered():
                game.key("l")
            game.key("ENTER")
        self.fail("the Drift did not end")

    def test_a_drift_whose_last_turn_was_written_but_not_scored_is_scored_on_return(self):
        game = self.game()
        was = game.postmarks
        game.key("t")
        game.key("ENTER")
        game.key("ENTER")
        real = game._drift_end
        game._drift_end = lambda left=False: None         # the process dies as the balloon touches down
        for _ in range(fw.DRIFT_TURNS + 2):
            if game.state.status != "aloft":
                break
            if "land" in game.offered():
                game.key("l")
            game.key("ENTER")
        game._drift_end = real
        self.assertEqual(self.rows("SELECT done FROM drifts"), [(0,)])
        game.close()
        again = self.game()
        again.key("t")
        again.key("ENTER")
        self.assertIsNone(again.drift)
        self.assertEqual(self.rows("SELECT done FROM drifts"), [(1,)])
        self.assertGreaterEqual(again.postmarks, was)
        self.assertIn("Scored", again.drift_note)

    def test_time_runs_out_and_the_balloon_comes_down(self):
        game = self.game()
        game.key("t")
        game.key("p")
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, fuel=400)
        for _ in range(fw.DRIFT_TURNS + 2):
            if game.drift is None:
                break
            game.choice = "hold"
            game.key("ENTER")
        self.assertIsNone(game.drift)
        self.assertIn(f"in {fw.DRIFT_TURNS} turns", game.drift_note)

    def test_the_table_is_the_same_for_everyone_and_orders_level_scores(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        day = a.today
        rows = [(day, 1, "Ada", "{}", 1, 700, 12.0, 10, 5, 3.0), (day, 2, "Bo", "{}", 1, 700, 9.0, 12, 5, 4.0),
                (day, 3, "Cy", "{}", 1, 700, 9.0, 11, 5, 5.0), (day, 4, "Di", "{}", 1, 700, 9.0, 11, 5, 2.0),
                (day, 5, "Ed", "{}", 1, 910, 30.0, 20, 5, 9.0), (day, 6, "Flo", "{}", 0, None, None, None, None, 1.0),
                (day + 1, 7, "Gus", "{}", 1, 999, 1.0, 1, 1, 1.0)]
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.executemany("INSERT INTO drifts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        db.commit()
        db.close()
        for game in (a, b):
            game.key("t")
            names = [row[0] for row in game._drift_seen["table"]]
            self.assertEqual(names, ["Ed", "Di", "Cy", "Bo", "Ada"],
                             "the score; then nearer the mark; then fewer turns; then first to finish")
            text = self.screen(game)
            self.assertNotIn("Flo", text, "an attempt part way is not on the table")
            self.assertNotIn("Gus", text, "nor is another day's")
        self.assertIn("You scored 700 today", self.screen(a))
        b.close()

    def test_only_the_top_ten_are_shown(self):
        game = self.game()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.executemany("INSERT INTO drifts VALUES (?, ?, ?, '{}', 1, ?, 1.0, 1, 1, 1.0)",
                       [(game.today, 100 + i, f"P{i}", 900 - i) for i in range(15)])
        db.commit()
        db.close()
        game.key("t")
        self.assertEqual(len(game._drift_seen["table"]), fw.DRIFT_TOP)
        self.screen(game)
        self.screen(game, 132, 40)

    def test_a_drift_charts_nothing_delivers_nothing_and_is_not_on_the_route(self):
        game = self.game()
        route, deliveries, charted = list(game.route), game.deliveries, game.charted
        game.key("t")
        game.key("ENTER")
        self.assertEqual(game.targets, [])
        self.assertEqual(game.droppable(), [])
        self.assertFalse(game.at_office())
        for key in ("o", "g", "k", "m", "h", "n", "b", "t"):
            game.key(key)
        self.assertTrue(game.drift and not game.hub and not game.loft and game.book is None)
        self.assertEqual(game.state.turn, fw.DRIFT_START_TURN, "no key that is not flying does anything")
        game.key("ENTER")
        game.key("ENTER")
        self.assertEqual(game.state.distance, 0.0, "the km of a Drift are not logbook km")
        self.assertEqual(game.score(), game.points + int(game.total_km * fw.POINTS["km"]))
        text = self.screen(game)
        self.assertTrue(all(len(line) <= 80 for line in text.split(chr(10))))
        self.assertIn("X", self.screen(game))
        self.assertIn("the mark", self.screen(game))
        self.fly_out(game)
        self.assertEqual((game.route, game.deliveries, game.charted), (route, deliveries, charted))
        self.assertEqual(self.rows("SELECT count(*) FROM charted"), [(0,)])
        self.assertIsNone(game.pending_scene)

    def test_nothing_of_the_pilots_own_is_written_while_a_drift_is_flown(self):
        game = self.game()
        seq = support.stored(self.folder)["seq"]
        game.key("t")
        game.key("p")
        game.key("ENTER")
        game.save("hold")
        self.assertFalse(game._day_may_have_opened())
        self.assertFalse(game._deed("x", lambda db: 1, lambda done: None))
        self.clock.days(2)
        game.pulse(self.clock())
        self.assertEqual(game.state.day, game.drift["course"]["day"], "a day opening does not move the Drift's day")
        saved = support.stored(self.folder)
        self.assertEqual((saved["seq"], saved["state"]["turn"], saved["state"]["fuel"]), (seq, 0, 60))
        game.key("x")
        self.assertIsNotNone(game.drift, "a practice is not thrown away by one slip of the finger")
        self.assertTrue(any("X again leaves it" in line for line in game.heard))
        game.key("s")
        game.key("x")
        self.assertIsNotNone(game.drift, "another key in between and it asks again")
        game.key("x")
        self.assertEqual((game.state.turn, game.state.fuel), (0, 60))
        self.assertEqual(game.drift_note, "Practice left.")

    def test_a_turn_that_cannot_be_written_is_not_flown(self):
        game = self.game()
        game.key("t")
        game.key("ENTER")
        game.key("ENTER")
        game.key("ENTER")
        kept = game.state
        real = game.store.write

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        game.store.write = broken
        game.key("ENTER")
        game.store.write = real
        self.assertIsNone(game.drift, "the Drift is parked")
        self.assertIn("could not be written", game.drift_note)
        game.key("ENTER")
        self.assertEqual(game.state, kept, "and taken up from the last turn on record, not the one that was lost")

    def test_a_turn_no_row_took_is_not_flown_either(self):
        game = self.game()
        game.key("t")
        game.key("ENTER")
        game.key("ENTER")
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.execute("DELETE FROM drifts")
        db.commit()
        db.close()
        game.key("ENTER")
        self.assertIsNone(game.drift)
        self.assertIn("could not be written", game.drift_note)

    def test_the_key_after_a_slip_of_x_flies_as_it_was_pressed(self):
        game = self.game()
        game.key("t")
        game.key("p")
        game.key("ENTER")
        turn = game.state.turn
        game.key("x")
        game.key("ENTER")
        self.assertEqual(game.state.turn, turn + 1)
        self.assertFalse(game.drift.get("leaving"))

    def test_a_drift_is_not_written_by_a_session_that_has_lost_the_pilot(self):
        game = self.game()
        game.key("t")
        game.key("ENTER")
        game.key("ENTER")
        text = self.rows("SELECT state FROM drifts")[0][0]
        other = fw.Store(self.folder, game.world.seed, "test-node")
        other.claim(12, "ssh", force=True)
        real = game._mine
        game._mine = lambda write=True: True               # the look before the turn still saw the pilot as ours
        game.key("ENTER")
        game._mine = real
        self.assertEqual(game.ended, "taken")
        self.assertEqual(self.rows("SELECT state FROM drifts")[0][0], text, "nothing was written")
        other.close()

    def test_a_day_that_opened_while_the_screen_stood_open_is_shown_before_it_is_flown(self):
        game = self.game()
        game.key("t")
        day = game._drift_seen["day"]
        self.clock.days(1)
        game.key("ENTER")
        self.assertIsNone(game.drift)
        self.assertEqual(game._drift_seen["day"], day + 1)
        self.assertIn("A new day has opened", self.screen(game))
        game.key("ENTER")
        self.assertEqual(game.drift["course"]["day"], day + 1)

    def test_not_from_the_air_and_the_drift_screen_fits(self):
        game = self.game()
        game.key("ENTER")
        game.key("t")
        self.assertFalse(game.drift_lobby, "the pilot's own flight is finished first")
        other = self.game(user_id=40, color_depth="16")
        other.key("t")
        for size in ((80, 24), (100, 30), (132, 40)):
            self.screen(other, *size)
        other.key("p")
        for size in ((80, 24), (132, 40)):
            text = self.screen(other, *size)
            self.assertIn("[X] Leave it", text)

    def test_a_visitor_flies_it_for_the_visit(self):
        game = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        game.key("t")
        game.key("ENTER")
        self.fly_out(game)
        self.assertIn("Scored", game.drift_note)
        game.key("ENTER")
        self.assertIsNone(game.drift)
        game.close()

    def test_a_world_from_before_is_brought_forward(self):
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in (1, 2, 3, 4, 5):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=5")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.execute("INSERT INTO almanac VALUES (2, 9, 'Bo', 3)")
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.assertIn(2, game.almanac)
        game.key("t")
        game.key("ENTER")
        self.assertEqual(self.rows("PRAGMA user_version"), [(fw.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT count(*) FROM drifts"), [(1,)])


if __name__ == "__main__":
    unittest.main()
