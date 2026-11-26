"""The Trials (S20): the first flight that teaches, twelve set flights, medals, and what they leave alone."""

import dataclasses
import json
import math
import subprocess
import sys
import time
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


class TrialCase(RescueCase):
    def open(self, game, index):
        """Choose a trial on the Trials' screen and begin it. -1 is the first flight."""
        if not game.trials:                               # a trial just flown leaves its screen open
            game.key("r")
        self.assertTrue(game.trials)
        game.trial_at = index + 1
        game.key("ENTER")
        self.assertEqual(game.drift["trial"], index)

    def fly(self, game, actions):
        for action in actions:
            if game.drift is None:
                return
            if action == "pibal":
                game.key("p")
            else:
                self.assertIn(action, game.offered(), action)
                game.choice = action
                game.key("ENTER")

    def gold(self, game, index):
        self.open(game, index)
        self.fly(game, fw.trial_course(game.world, game.weather, index)["gold"])
        self.assertIsNone(game.drift)


class Courses(unittest.TestCase):
    def test_every_trial_in_every_world_tried_has_a_flight_to_gold(self):
        for node in NODES:
            world = fw.World(fw.world_seed(node))
            weather = fw.Weather(world)
            engine = fw.Flight(world, weather, "trial")
            for index in range(-1, len(fw.TRIALS)):
                spec = fw.TUTORIAL if index < 0 else fw.TRIALS[index]
                course = fw.trial_course(world, weather, index)
                self.assertEqual(tuple(course["gold"]), spec["script"], (node, spec["name"], "flown as written"))
                state = fw.trial_state(spec, course["x"], course["y"], course["day"])
                for action in course["gold"]:
                    self.assertIn(action, engine.actions(state), (node, spec["name"], action))
                    state = engine.step(state, action).state
                km = math.hypot(state.x - course["mx"], state.y - course["my"])
                trip = math.hypot(course["mx"] - course["x"], course["my"] - course["y"])
                self.assertEqual((fw.trial_medal(km, state.status, trip), state.status), (3, "ground"),
                                 (node, spec["name"]))
                self.assertLessEqual(state.turn - spec["start"], len(spec["script"]) + fw.TRIAL_SPARE_TURNS)
                if index >= 0:
                    self.assertEqual(fw.trial_medal(trip, "ground", trip), 0,
                                     (node, spec["name"], "coming down where it began wins nothing"))
                self.assertGreaterEqual(world.sample(course["mx"], course["my"])[1], fw.SHORE)
                if index >= 0:
                    self.assertGreaterEqual(math.hypot(course["mx"] - course["x"], course["my"] - course["y"]),
                                            fw.TRIAL_FAR_KM, "the mark is not on the doorstep")
                self.assertGreaterEqual(course["day"], fw.TRIAL_DAY, "weather of its own, far from the calendar's")

    def test_there_are_twelve_and_each_is_its_own(self):
        self.assertEqual(len(fw.TRIALS), 12)
        self.assertEqual(len({t["name"] for t in fw.TRIALS}), 12)
        self.assertEqual(len({(t["script"], json.dumps(t["balloon"], sort_keys=True), t["start"]) for t in fw.TRIALS}), 12)
        for trial in fw.TRIALS + (fw.TUTORIAL,):
            self.assertEqual((trial["script"][0], trial["script"][-1]), ("launch", "land"))
            self.assertLessEqual(len(trial["name"]), 16)
            self.assertLessEqual(len(trial["about"]), 44, "what it is about fits its line at 80 columns")
            self.assertTrue(set(trial["script"]) <= set(fw.ACTIONS))

    def test_the_same_trial_is_the_same_in_another_process(self):
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
                "w = fw.World(fw.world_seed('test-node'))\n"
                "print(json.dumps([fw.trial_course(w, fw.Weather(w), i) for i in (-1, 0, 10)]))\n")
        out = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        world = fw.World(fw.world_seed("test-node"))
        weather = fw.Weather(world)
        self.assertEqual(json.loads(out.stdout), [fw.trial_course(world, weather, i) for i in (-1, 0, 10)])

    def test_medals_by_distance_and_only_on_land(self):
        self.assertEqual([fw.trial_medal(km, "ground") for km in (0.0, 6.0, 6.1, 15.0, 15.1, 30.0, 30.1)],
                         [3, 3, 2, 2, 1, 1, 0])
        self.assertEqual(fw.trial_limits(200.0), (30.0, 15.0, 6.0), "a long trial: the distances of the table")
        self.assertEqual(fw.trial_limits(20.0), (15.0, 8.0, 3.0), "a short one: a share of the way")
        self.assertEqual(fw.trial_limits(10.0), (7.5, 4.0, 3.0))
        for trip in (10.0, 12.0, 25.0, 39.0, 60.0, 300.0):
            bronze, silver, gold = fw.trial_limits(trip)
            self.assertTrue(gold <= silver <= bronze < trip, trip)
        self.assertEqual(fw.trial_medal(1.0, "ditched"), 0)
        self.assertEqual(fw.trial_medal(1.0, "aloft"), 0)
        self.assertEqual(fw.trial_medal(1.0, "stranded"), 3, "down on land out of fuel is still down on land")
        self.assertEqual(fw.TRIAL_PAY, (0, 5, 12, 25))

    def test_the_help_says_how_medals_are_won(self):
        text = " ".join(fw.TRIAL_HELP)
        for word in ("within 6 km of it for", "within 15 km for silver", "within 30 km for bronze", "short trial",
                     "eight turns more", "cost no day", "5, 12 or 25"):
            self.assertIn(word, text)
        self.assertLessEqual(len(fw.TRIAL_HELP), 21)
        self.assertTrue(all(len(line.replace("`", "")) <= 78 and line.count("`") % 2 == 0 for line in fw.TRIAL_HELP))


class FirstFlight(TrialCase):
    def test_it_teaches_by_having_each_thing_done_once_and_is_soon_over(self):
        game = self.game()
        began = time.monotonic()
        self.open(game, -1)
        self.assertIn("W and S look at the layer above", " ".join(game.log))
        said = []
        for keys, lesson in ((("w", "s"), "Enter flies what you are looking at"), (("ENTER",), "P sends up a pibal"),
                             (("p",), "A sack over the side"), (("j", "ENTER"), "Sinking is free"),
                             (("s", "ENTER"), "You land from the lowest layer")):
            for key in keys:
                game.key(key)
            said.append(any(lesson in line for line in game.heard))
            self.assertTrue(said[-1], (keys, game.heard))
            self.screen(game)
        self.assertEqual(game.drift["step"], 5)
        game.key("l")
        game.key("ENTER")
        self.assertIsNone(game.drift)
        self.assertTrue(game.taught)
        self.assertIn("every lesson done", game.trial_note)
        self.assertLess(time.monotonic() - began, 240.0, "under four minutes of scripted play")
        self.assertTrue(support.stored(self.folder)["taught"])
        self.assertIn("flown", self.screen(game))
        again = self.game()
        self.assertTrue(again.taught)

    def test_the_lessons_are_preview_commit_pibal_ballast_and_landing(self):
        self.assertEqual([step[0] for step in fw.TUTOR_STEPS], ["look", "launch", "pibal", "jettison", "sink", "land"])
        self.assertTrue(all(len(step[1]) < 230 for step in fw.TUTOR_STEPS))

    def test_lessons_are_passed_in_whatever_order_and_only_by_doing_them(self):
        game = self.game()
        self.open(game, -1)
        game.key("ENTER")                                 # launched without looking first
        self.assertEqual(game.drift["done"], {"launch"})
        self.assertTrue(any("Press W or S" in line for line in game.heard), "the lesson still open is asked for")
        game.key("w")
        self.assertEqual(game.drift["done"], {"launch", "look"})
        self.assertTrue(any("P sends up a pibal" in line for line in game.heard))
        game.choice = "hold"
        game.key("ENTER")
        self.assertEqual(game.drift["step"], 2, "a hold is no lesson")
        game.key("w")
        self.assertEqual(game.drift["step"], 2, "and a lesson is passed once")
        game.choice = "land"
        game.key("ENTER")
        self.assertIsNone(game.drift)
        self.assertFalse(game.taught)
        self.assertIn("3 of the lessons still to do", game.trial_note)

    def test_the_first_flight_leaves_room_to_look_about(self):
        game = self.game()
        self.open(game, -1)
        self.assertEqual(game.drift["turns"], len(fw.TUTORIAL["script"]) + fw.TUTORIAL_SPARE_TURNS)
        self.assertGreaterEqual(game.drift["turns"], 24)

    def test_a_new_pilot_is_shown_the_way_to_it(self):
        game = self.game()
        self.assertIn("R teaches", " ".join(game.log))
        self.assertIn("[R] Learn", self.screen(game))
        game.taught = True
        self.assertNotIn("[R] Learn", self.screen(game))


class Medals(TrialCase):
    def test_the_way_to_gold_wins_gold_and_pays_once(self):
        game = self.game()
        was = game.postmarks
        self.gold(game, 0)
        self.assertEqual(game.medals[0], 3)
        self.assertEqual(game.postmarks, was + fw.TRIAL_PAY[3])
        self.assertIn("Gold, and 25 postmarks", game.trial_note)
        saved = support.stored(self.folder)
        self.assertEqual((saved["medals"][0], saved["postmarks"]), (3, game.postmarks))
        text = self.screen(game)
        self.assertIn("1 gold", text)
        self.gold(game, 0)
        self.assertEqual(game.postmarks, was + fw.TRIAL_PAY[3], "flown again: nothing more")
        self.assertIn("no better than your best", game.trial_note)
        again = self.game()
        self.assertEqual(again.medals[0], 3)

    def test_every_trial_is_won_through_the_game_as_it_is_played(self):
        game = self.game()
        for index in range(len(fw.TRIALS)):
            self.gold(game, index)
            self.assertEqual(game.medals[index], 3, fw.TRIALS[index]["name"])
        self.assertEqual(game.medals, [3] * 12)
        self.assertIn("12 gold", self.screen(game))

    def test_a_better_medal_pays_the_difference_and_a_worse_one_changes_nothing(self):
        game = self.game()
        game.medals = [1] + [0] * 11
        was = game.postmarks
        self.gold(game, 0)
        self.assertEqual(game.postmarks, was + fw.TRIAL_PAY[3] - fw.TRIAL_PAY[1])
        self.open(game, 0)
        self.fly(game, ("launch", "land"))                # down on the doorstep: far from the mark
        self.assertEqual(game.medals[0], 3)
        self.assertEqual(game.postmarks, was + fw.TRIAL_PAY[3] - fw.TRIAL_PAY[1])

    def test_a_medal_that_cannot_be_written_does_not_stand(self):
        import sqlite3
        game = self.game()
        was, real = game.postmarks, game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        self.open(game, 0)
        game.store.commit = broken
        self.fly(game, fw.trial_course(game.world, game.weather, 0)["gold"])
        game.store.commit = real
        self.assertEqual((game.medals[0], game.postmarks), (0, was))
        self.assertIn("could not be written down", game.trial_note)
        game.saved_ok = True
        self.gold(game, 0)
        self.assertEqual((game.medals[0], game.postmarks), (3, was + fw.TRIAL_PAY[3]))

    def test_coming_down_where_it_began_wins_nothing(self):
        game = self.game()
        for index in range(len(fw.TRIALS)):
            self.open(game, index)
            self.assertTrue(any("Gold within" in line for line in game.log), "the log says what wins what")
            if "land" not in game.offered():
                game.key("ENTER")
            for _ in range(40):
                if game.drift is None:
                    break
                if "land" in game.offered():
                    game.choice = "land"
                elif "sink" in game.offered():
                    game.choice = "sink"
                else:
                    game.choice = "hold"
                game.key("ENTER")
            self.assertEqual(game.medals[index], 0, fw.TRIALS[index]["name"])

    def test_a_trial_that_could_not_be_laid_out_has_no_medal(self):
        game = self.game()
        town = game.world.towns[0]
        game.world.__dict__.setdefault("_trials", {})[5] = {
            "day": 600, "start": town["name"], "x": town["x"], "y": town["y"], "mx": town["x"], "my": town["y"],
            "gold": []}
        self.open(game, 5)
        self.fly(game, ("launch", "land"))
        self.assertEqual(game.medals[5], 0)
        self.assertIn("cannot be laid out in this world", game.trial_note)
        del game.world.__dict__["_trials"][5]

    def test_time_runs_out_and_leaving_takes_two_presses(self):
        game = self.game()
        self.open(game, 0)
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, fuel=300)
        limit = len(fw.TRIALS[0]["script"]) + fw.TRIAL_SPARE_TURNS
        for _ in range(limit + 2):
            if game.drift is None:
                break
            game.choice = "hold"
            game.key("ENTER")
        self.assertIsNone(game.drift, "the balloon comes down when the trial's turns are spent")
        self.open(game, 1)
        game.key("x")
        self.assertIsNotNone(game.drift)
        game.key("x")
        self.assertIsNone(game.drift)
        self.assertIn("Left", game.trial_note)
        self.assertEqual(game.medals[1], 0)

    def test_each_trial_has_its_own_balloon(self):
        game = self.game()
        for index, want in ((3, {"fuel": 6}), (4, {"ballast": 0}), (9, {"night_gear": True}), (10, {"ceiling": 5})):
            self.open(game, index)
            for field, value in want.items():
                self.assertEqual(getattr(game.state, field), value, fw.TRIALS[index]["name"])
            self.assertEqual(game.state.turn, fw.TRIALS[index]["start"])
            text = self.screen(game)
            self.assertIn(fw.TRIALS[index]["name"][:16], text)
            self.assertIn("the mark", text)
            self.assertNotIn("Day 5", text, "the trial's own day is nobody's business")
            self.assertNotIn("more days wait", text)
            self.assertRegex(text.split("\n")[0], r"turns left\. Mark \d+ km [NSEW]+ *$")
            game.key("x")
            game.key("x")


class LeftAlone(TrialCase):
    def test_a_trial_costs_no_day_and_writes_nothing_shared(self):
        game = self.game()
        before = (game.state, game.gear, sorted(game.chart), list(game.route), list(game.manifest), game.deliveries)
        seq = support.stored(self.folder)["seq"]
        self.open(game, 2)
        self.assertIs(game.help_text(), fw.TRIAL_HELP)
        for key in ("o", "g", "k", "m", "h", "n", "b", "t", "r"):
            game.key(key)
        self.assertEqual(game.state.turn, fw.TRIALS[2]["start"])
        self.fly(game, ("launch", "hold", "land"))
        self.assertIsNone(game.drift)
        self.assertEqual((game.state, game.gear, sorted(game.chart), game.route, game.manifest, game.deliveries), before)
        self.assertEqual(self.rows("SELECT count(*) FROM charted"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM drifts"), [(0,)])
        self.assertEqual(support.stored(self.folder)["seq"], seq, "no medal: nothing at all is written")
        self.assertIsNone(game.pending_scene)
        journal = game.store.journal(12)
        self.gold(game, 2)
        self.assertEqual(game.store.journal(12), journal, "a medal is no line in the day's journal")
        self.assertEqual(support.stored(self.folder)["seq"], seq, "nor a turn of the pilot's")
        saved = support.stored(self.folder)
        self.assertEqual((saved["state"]["day"], saved["state"]["turn"], saved["state"]["x"]),
                         (before[0].day, before[0].turn, before[0].x), "a medal is written to the pilot and nothing else")
        self.assertEqual(self.rows("SELECT count(*) FROM charted"), [(0,)])

    def test_the_screen_lists_the_first_flight_and_the_twelve(self):
        game = self.game()
        game.key("r")
        text = self.screen(game)
        for spec in (fw.TUTORIAL,) + fw.TRIALS:
            self.assertIn(spec["name"], text)
            self.assertIn(spec["about"], text, "whole, not cut off")
        for word in ("The Trials", "not yet flown", "no medal", "[W/S] Choose", "[Enter] Fly it", "[B] Back",
                     "0 gold  0 silver  0 bronze"):
            self.assertIn(word, text)
        self.assertEqual(game.key("?"), "help")
        self.assertIs(game.help_text(), fw.TRIAL_HELP)
        for _ in range(20):
            game.key("s")
        self.assertEqual(game.trial_at, len(fw.TRIALS))
        self.assertIn(f"> {fw.TRIALS[-1]['name']}", self.screen(game))
        for _ in range(20):
            game.key("w")
        self.assertEqual(game.trial_at, 0)
        for size in ((80, 24), (100, 30), (132, 40)):
            self.screen(game, *size)
        game.key("b")
        self.assertFalse(game.trials)

    def test_not_from_the_air_and_a_visitor_may_fly_them(self):
        game = self.game()
        game.key("ENTER")
        game.key("r")
        self.assertFalse(game.trials)
        guest = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        self.gold(guest, 0)
        self.assertEqual(guest.medals[0], 3)
        guest.close()

    def test_a_pilot_from_before_has_no_medals_and_a_bad_record_is_not_a_game(self):
        game = self.game()
        career = {k: v for k, v in game._career().items() if k not in ("medals", "taught")}
        game._take(career, game.state.to_dict())
        self.assertEqual((game.medals, game.taught), ([0] * 12, False))
        game._take(dict(career, medals=[3, 2]), game.state.to_dict())
        self.assertEqual(game.medals, [3, 2] + [0] * 10)
        for bad in ([4], [-1], ["x"], 7):
            with self.assertRaises((ValueError, TypeError)):
                game._take(dict(career, medals=bad), game.state.to_dict())

    def test_the_help_names_the_trials(self):
        self.assertIn("`R` Trials", " ".join(fw.PLAIN_HELP))
        self.assertLessEqual(len(fw.PLAIN_HELP), 21)


if __name__ == "__main__":
    unittest.main()
