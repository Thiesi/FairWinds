"""S05: the plain flight, the whole turn on an 80 x 24 text screen."""
import dataclasses
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import support                                             # noqa: E402
from support import Door, Terminal, door_info

import fair_winds as fw


class Out:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


class Keys:
    """Input that hands over what it was given, then nothing."""

    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def read(self, timeout):
        return self.chunks.pop(0) if self.chunks else None


def launch(**changes):
    return fw.parse_door_info(json.dumps(door_info(**changes)), {})


class PlainCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.folder = Path(self._dir.name)
        self.clock = support.Calendar()

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def game(self, **changes):
        return support.new_game(launch(**changes), self.folder, now=self.clock)

    def session(self, game, width=80, height=24, keys=()):
        session = fw.Session(dataclasses.replace(game.launch, width=width, height=height), Keys(*keys), Out(), game=game)
        return session

    def screen(self, game, width=80, height=24, view="front"):
        session = self.session(game, width, height)
        session.view = view
        session.paint()
        term = Terminal(width, height)
        term.feed(session.out.text)
        self.assertEqual(term.out_of_bounds, [])
        self.assertNotIn((height - 1, width - 1), term.written)
        return term.text()

    def press(self, game, *keys):
        for key in keys:
            game.key(key)

    def save_file(self, user_id=12):
        return support.stored(self.folder, user_id)


class NewGame(PlainCase):
    def test_starts_at_the_start_town_with_post_for_three_places(self):
        game = self.game()
        start = game.world.towns[game.world.start_town]
        self.assertEqual((game.state.x, game.state.y, game.state.day, game.state.status), (start["x"], start["y"], 1, "ground"))
        self.assertEqual(len(game.targets), 3)
        self.assertNotIn(("town", game.world.start_town), game.targets)
        self.assertEqual(len(set(game.targets)), 3)
        distances = [d for d, _, _ in game.bearings()]
        self.assertEqual(distances, sorted(distances))
        self.assertLess(distances[0], 150, "something is within a day's reach")

    def test_first_screen(self):
        game = self.game()
        text = self.screen(game)
        self.assertIn("FAIR WINDS  Day 1  06:00", text)
        self.assertIn("Nearest post:", text)
        self.assertIn("@", text)
        self.assertGreaterEqual(text.count("#"), 3 + 1, "three in the list, and at least one on the map")
        self.assertIn("Launch into layer 1 Surface: 2 fuel.", text)
        self.assertIn("[Enter] Launch", text)
        self.assertIn("Return to Test Node", text)
        for name in (game.place_of(t)["name"][:14] for t in game.targets):
            self.assertIn(name, text)

    def test_a_world_per_node_and_a_game_per_pilot(self):
        a = self.game()
        self.press(a, "ENTER")
        b = self.game(user_id=13)
        self.assertEqual(b.state.turn, 0, "another pilot starts their own game")
        c = self.game(node_id="other-node")
        self.assertNotEqual(c.world.seed, a.world.seed)
        self.assertEqual(len(list(self.folder.glob("fair_winds-*.db"))), 2, "one set of records per world")
        self.assertEqual(self.save_file(13)["state"]["turn"], 0, "and in it a record per pilot")


class Flying(PlainCase):
    def test_choosing_previews_and_enter_flies(self):
        game = self.game()
        self.press(game, "ENTER")
        self.assertEqual((game.state.status, game.state.turn, game.state.fuel, game.choice), ("aloft", 1, 58, "hold"))
        self.assertIn("Hold in layer 1 Surface: 1 fuel.", self.screen(game))
        self.press(game, "w")
        self.assertEqual(game.choice, "climb")
        self.assertEqual(game.state.turn, 1, "looking costs nothing")
        self.assertIn("Climb to layer 2 Low: 3 fuel.", self.screen(game))
        self.press(game, "w")
        self.assertEqual(game.choice, "climb", "there is no choice above climbing")
        self.press(game, "s")
        self.assertEqual(game.choice, "hold")
        self.press(game, "UP", "ENTER")
        self.assertEqual((game.state.layer, game.state.turn, game.state.fuel), (2, 2, 55))
        self.press(game, "DOWN", " ")
        self.assertEqual((game.state.layer, game.state.turn, game.state.fuel), (1, 3, 55))

    def test_the_screen_shows_the_turn(self):
        game = self.game()
        self.press(game, "ENTER", "w", "ENTER")
        text = self.screen(game)
        self.assertIn("Day 1  06:30", text)
        self.assertIn(">2 Low", text)
        self.assertIn("Fuel  55", text)
        self.assertIn("~ forecast, not read", text)
        self.assertRegex(text, r"06:15  Layer 2: [\d.]+ km, wind [NESW]{1,3}\.")
        self.assertNotRegex(text, r" on an? [NESW]", "no article to get wrong before a compass point")
        self.assertRegex(text, r" 3 Mid    ~")
        self.assertRegex(text, r">2 Low      *[A-Z]", "your own layer is read, not forecast")
        self.assertIn("[W/S] Layer", text)

    def test_jettison_pibal_and_land_keys(self):
        game = self.game()
        self.press(game, "ENTER", "j")
        self.assertEqual(game.choice, "jettison")
        self.assertIn("Drop a sack for layer 2 Low: 0 fuel.", self.screen(game))
        self.press(game, "ENTER")
        self.assertEqual((game.state.layer, game.state.ballast, game.state.fuel), (2, 4, 58))
        turn = game.state.turn
        self.press(game, "p")
        self.assertEqual((game.state.pibals, game.state.turn), (2, turn), "a pibal takes no time")
        text = self.screen(game)
        self.assertIn("Pibal read: 4 turns", text)
        self.assertNotRegex(text, r" 3 Mid    ~", "every layer is read now")
        self.press(game, "s", "ENTER")
        if "land" in game.engine.actions(game.state):
            self.press(game, "l")
            self.assertEqual(game.choice, "land")
            self.assertIn("Land here:", self.screen(game))

    def test_keys_that_mean_nothing_do_nothing(self):
        game = self.game()
        before = (game.state, game.choice)
        self.press(game, "x", "TAB", "j", "p", "LEFT", "5")
        self.assertEqual((game.state, game.choice), before)
        self.assertEqual(game.key("q"), "quit")
        self.assertEqual(game.key("Q"), "quit")
        self.assertEqual(game.key("?"), "help")

    def test_a_held_key_never_flies_turns_unseen(self):
        game = self.game()
        session = self.session(game, keys=[b"\r\r\r\r\r\r"])
        session.step()
        self.assertEqual(game.state.turn, 1, "one Enter, one turn: the rest were typed before the result showed")
        session.inp.chunks.append(b"\r")
        session.step()
        self.assertEqual(game.state.turn, 2)
        session.inp.chunks.append(b"wwww\r\r")
        session.step()
        self.assertEqual(game.state.turn, 3, "looking first is fine; the turn is still one")

    def test_a_whole_day_and_the_next_dawn(self):
        game = self.game()
        self.press(game, "ENTER")
        turns = 0
        while game.phase() == "aloft" and turns < 200:
            offered = game.engine.actions(game.state)
            self.assertTrue(offered)
            self.press(game, "ENTER")
            if game.choice not in offered and game.phase() == "aloft":
                self.press(game, "s", "ENTER")
            turns += 1
            self.screen(game)
        self.assertNotEqual(game.phase(), "aloft", "the day ends one way or another")
        self.assertLessEqual(game.state.turn, fw.DAY_TURNS)
        day = game.state.day
        text = self.screen(game)
        phase = game.phase()
        if phase == "stranded":
            self.assertIn("[W] Walk out", text)
            self.press(game, "w")
            self.assertEqual(game.state.day, day + 2)
        elif phase == "ditched":
            self.assertIn("Wait for the boat", text)
            self.press(game, "n")
            self.assertEqual(game.state.day, day + 1)
        else:
            self.assertRegex(text, r"\[N\] (Next day|Night)")
            self.press(game, "n")
            self.assertEqual(game.state.day, day + 1)
        self.assertEqual((game.state.turn, game.state.status, game.state.distance), (0, "ground", 0.0))
        self.assertEqual(game.days_flown, 1)
        self.clock.days(3)
        game._day_may_have_opened()
        self.assertTrue("launch" in game.offered() or game.at_office() or game.phase() == "stranded",
                        "and the balloon can fly again, or there is somewhere to turn")


class GettingThere(PlainCase):
    def over_a_target(self, game, **changes):
        """Put the balloon over a destination on a morning calm enough to land."""
        ref = game.targets[0]
        place = game.place_of(ref)
        for day in range(1, 80):
            state = dataclasses.replace(game.state, x=place["x"], y=place["y"], day=day, turn=10, status="aloft",
                                        layer=game.engine.lowest_open(place["x"], place["y"]), fuel=20, **changes)
            if "land" in game.engine.actions(state) and game.engine.landing_class(state) == "clean":
                game.state = state
                game._choose_default()
                return ref
        self.fail("never calm enough to land in eighty days")

    def test_delivering_the_post(self):
        game = self.game()
        ref = self.over_a_target(game, wear=30, ballast=1, pibals=0)
        item = next(item for item in game.manifest if tuple(item["dest"]) == ref)
        before = game.postmarks
        self.press(game, "l", "ENTER")
        self.assertEqual(game.deliveries, 1)
        self.assertEqual(game.state.status, "ground")
        self.assertEqual(game.postmarks, before + item["payout"], "a delivery pays what it promised")
        self.assertNotIn(ref, game.targets, "that post is off the manifest")
        self.assertEqual(len(game.manifest), 2)
        self.assertEqual(len(game.state.cargo), 2)
        self.assertEqual((game.state.fuel, game.state.ballast, game.state.pibals, game.state.wear), (20, 1, 0, 30),
                         "and nothing is filled for nothing: the stores sell it")
        text = self.screen(game)
        self.assertIn(f"mailbag, {item['payout']} postmarks. That makes 1.", text)
        self.assertIn("1 delivered", text)
        self.assertIn("[O] Post", text)
        self.assertEqual(self.save_file()["deliveries"], 1)
        self.assertEqual(self.save_file()["postmarks"], game.postmarks)

    def test_landing_where_there_is_no_post_changes_nothing(self):
        game = self.game()
        start = game.world.towns[game.world.start_town]
        for day in range(1, 80):
            state = dataclasses.replace(game.state, day=day, turn=10, status="aloft", fuel=20, wear=30,
                                        layer=game.engine.lowest_open(start["x"], start["y"]))
            if "land" in game.engine.actions(state) and game.engine.landing_class(state) == "clean":
                break
        game.state = state
        game._choose_default()
        targets, postmarks = list(game.targets), game.postmarks
        self.press(game, "l", "ENTER")
        self.assertEqual((game.deliveries, game.targets, game.postmarks), (0, targets, postmarks),
                         "no delivery, and the post is the same post")
        self.assertEqual((game.state.fuel, game.state.wear), (20, 30), "and no free fill for dropping in")
        self.assertIn("Nothing aboard was for", self.screen(game))
        self.press(game, "n")
        self.assertEqual((game.state.fuel, game.state.wear), (20, 30), "a night fills nothing either")

    def test_distance_is_counted_once(self):
        game = self.game()
        self.press(game, "ENTER", "ENTER", "ENTER")
        first_leg = game.state.distance
        self.assertGreater(first_leg, 0)
        self.assertIn(f"{first_leg:.0f} km", self.screen(game))
        # Land, and fly a second leg the same day.
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        game._choose_default()
        self.assertEqual(game.total_km, 0.0, "nothing joins the total until the day ends")
        self.press(game, "ENTER", "ENTER")
        today = game.state.distance
        self.assertGreater(today, first_leg)
        self.assertIn(f"0 delivered   {today:.0f} km", self.screen(game), "two legs, each counted once")
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        self.press(game, "n")
        self.assertAlmostEqual(game.total_km, today)
        self.assertEqual(game.state.distance, 0.0)
        self.assertIn(f"0 delivered   {today:.0f} km", self.screen(game), "and the same total the next morning")
        self.assertAlmostEqual(self.save_file()["total_km"], today)

    def test_a_day_nothing_was_flown_on_is_not_a_flying_day(self):
        game = self.game()
        self.clock.days(2)
        self.press(game, "n", "n")
        self.assertEqual((game.state.day, game.days_flown), (3, 0))
        self.clock.days(1)
        self.press(game, "ENTER", "ENTER")
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        game._choose_default()
        self.press(game, "n")
        self.assertEqual(game.days_flown, 1)

    def test_a_second_flight_the_same_day(self):
        game = self.game()
        self.over_a_target(game)
        self.press(game, "l", "ENTER")
        self.assertEqual(game.phase(), "ground")
        self.assertIn("[Enter] Launch", self.screen(game))
        turn = game.state.turn
        self.press(game, "ENTER")
        self.assertEqual((game.state.status, game.state.turn), ("aloft", turn + 1))

    def test_stranded_and_walking_out(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, x=game.state.x + 40.0, status="stranded", fuel=0, turn=30)
        game.postmarks = 3
        self.assertEqual(game.phase(), "stranded")
        self.assertIn("costs two days", self.screen(game))
        self.press(game, "ENTER")
        self.assertEqual((game.state.day, game.state.status, game.state.fuel), (3, "ground", 0))
        self.assertIsNotNone(game.engine.place(game.state.x, game.state.y), "walked out to a town")
        self.assertIn("Two days on foot", " ".join(game.log))
        self.assertEqual(len(game.manifest), 3, "the post came along")
        self.assertTrue(game.relief_due(), "and nobody is left there without fuel or the means to buy it")

    def test_down_in_the_wild_and_unable_to_fly_is_stranded(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, x=game.state.x - 60.0, fuel=1, turn=40)
        self.assertIsNone(game.here())
        self.assertEqual(game.phase(), "stranded", "one unit of fuel launches nothing, and no night adds any")
        game.state = dataclasses.replace(game.state, fuel=30, wear=fw.WEAR_GROUNDED)
        self.assertEqual(game.phase(), "stranded")
        self.assertIn("too worn to fly. Walking out", self.screen(game))
        self.press(game, "w")
        self.assertIsNotNone(game.here())

    def test_ditched_and_the_packet_boat(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, x=5.0, y=5.0, status="ditched", turn=30)
        self.assertIn("packet boat", self.screen(game))
        self.press(game, "n")
        self.assertEqual((game.state.day, game.state.status), (2, "ground"))
        place = game.engine.place(game.state.x, game.state.y)
        self.assertEqual(place[0], "town")
        self.assertTrue(game.world.towns[place[1]]["port"] or not any(t["port"] for t in game.world.towns))

    def test_a_worn_out_balloon_rests_until_it_is_mended(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, wear=100)
        self.assertEqual(game.phase(), "rest")
        self.assertIn("too worn to fly. The stores at the post office mend it: O.", self.screen(game))
        self.press(game, "n")
        self.assertEqual(game.state.wear, 100, "a night mends nothing")
        self.clock.days(1)
        game._day_may_have_opened()
        self.press(game, "o", "m")
        self.assertEqual((game.state.wear, game.postmarks), (60, 0), "the stores mend what the postmarks pay for")

    def test_a_night_in_the_wild_fills_nothing(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, x=game.state.x - 60.0, fuel=30, turn=40)
        self.assertIsNone(game.engine.place(game.state.x, game.state.y))
        self.press(game, "n")
        self.assertEqual((game.state.fuel, game.state.day, game.state.turn), (30, 2, 0))


class Saving(PlainCase):
    def test_every_turn_is_saved_and_a_new_session_resumes_on_it(self):
        game = self.game()
        self.press(game, "ENTER", "w", "ENTER", "ENTER")
        self.assertEqual(fw.FlightState.from_dict(self.save_file()["state"]), game.state)
        again = self.game()
        self.assertEqual(again.state, game.state)
        self.assertEqual((again.targets, again.manifest, again.postmarks, again.log),
                         (game.targets, game.manifest, game.postmarks, game.log))
        self.assertEqual(self.screen(again), self.screen(game), "the screen is the same screen")
        self.press(game, "ENTER")
        self.press(again, "ENTER")
        self.assertEqual(again.state, game.state, "and the next turn ends the same way")

    def test_looking_is_not_saved_flying_is(self):
        game = self.game()
        start = self.save_file()
        self.assertEqual(start["state"]["turn"], 0, "the game is saved from the start, so a bad folder shows at once")
        self.press(game, "ENTER")
        first = self.save_file()
        self.assertNotEqual(first, start)
        self.press(game, "w", "s", "w")
        self.assertEqual(self.save_file(), first)

    def test_a_guest_is_a_visit(self):
        game = self.game(user_id=2 ** 62 + 5)
        self.press(game, "ENTER", "ENTER")
        self.assertEqual(game.state.turn, 2)
        self.assertEqual(list(self.folder.glob("pilot-*")) + list(self.folder.glob("*.db")), [])
        self.assertIn("You are visiting: nothing is kept", self.screen(game))
        self.assertEqual(self.game(user_id=2 ** 62 + 5).state.turn, 0)

    def test_a_folder_that_cannot_be_written_does_not_trap_the_caller(self):
        blocker = self.folder / "in-the-way"
        blocker.write_text("not a folder", encoding="utf-8")
        game = fw.PlainFlight(launch(), blocker)
        self.assertFalse(game.saved_ok)
        self.assertIn("cannot be saved here", self.screen(game), "said before the first turn, not after it")
        self.press(game, "ENTER", "ENTER")
        self.assertEqual(game.state.turn, 2, "the game plays on")
        session = self.session(game)
        session.notice = "About 1 minute of session time left."
        session.paint()
        self.assertIn("of session time left", session.out.text, "a session notice is not hidden by the save warning")

    def test_a_record_that_cannot_be_read_is_never_written_over(self):
        game = self.game()
        self.press(game, "ENTER")
        game.close()
        path = next(self.folder.glob("fair_winds-*.db"))
        for bad in ("", "{", "[]", '{"targets": [["nowhere", 99]], "visited": []}'):
            db = sqlite3.connect(str(path))
            db.execute("UPDATE pilots SET career=? WHERE user_id=12", (bad,))
            db.commit()
            db.close()
            fresh = self.game()
            text = self.screen(fresh)
            self.assertIn("could not be read. This game is not saved. Tell the SysOp.", text)
            self.press(fresh, "ENTER", "ENTER")
            self.assertEqual(fresh.state.turn, 2, "the game plays on")
            self.assertIn("cannot be saved here", self.screen(fresh))
            fresh.close()
            db = sqlite3.connect(str(path))
            self.assertEqual(db.execute("SELECT career FROM pilots WHERE user_id=12").fetchone()[0], bad,
                             "the record is exactly as it was")
            db.close()

    def test_a_save_file_from_before_the_database_is_taken_over_once(self):
        game = self.game()
        self.press(game, "ENTER", "ENTER", "ENTER")
        state, targets = game.state, list(game.targets)
        old = self.folder / f"pilot-{game.world.seed:016x}-12.json"
        old.write_text(json.dumps(dict(game._career(), version=1, seed=game.world.seed, state=game.state.to_dict())),
                       encoding="utf-8")
        game.close()
        for path in self.folder.glob("fair_winds-*"):
            path.unlink()                                # the installation as it was before the database
        again = self.game()
        self.assertEqual((again.state, again.targets), (state, targets), "the pilot flies on where they were")
        self.assertFalse(old.exists())
        self.assertTrue(old.with_name(old.name + ".imported").exists(), "the old file is kept under another name")
        self.assertEqual(self.save_file()["state"]["turn"], 3, "and the database has the pilot now")
        self.press(again, "ENTER")
        third = self.game()
        self.assertEqual(third.state.turn, 4, "the database is what is read from then on")

    def test_an_old_save_file_that_cannot_be_opened_is_waited_for(self):
        game = self.game()
        self.press(game, "ENTER", "ENTER")
        seed, text = game.world.seed, json.dumps(dict(game._career(), version=1, seed=game.world.seed,
                                                      state=game.state.to_dict()))
        game.close()
        for path in self.folder.glob("fair_winds-*"):
            path.unlink()
        old = self.folder / f"pilot-{seed:016x}-12.json"
        old.mkdir()                                      # there, and not a file that opens
        fresh = self.game()
        self.assertIn("This game is not saved", self.screen(fresh))
        self.press(fresh, "ENTER")
        fresh.close()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        self.assertEqual(db.execute("SELECT count(*) FROM pilots").fetchone()[0], 0, "no new career shuts the old one out")
        db.close()
        old.rmdir()
        old.write_text(text, encoding="utf-8")           # the SysOp has put it right
        again = self.game()
        self.assertEqual(again.state.turn, 2, "and the pilot flies on where they were")

    def test_an_earlier_imported_file_is_never_written_over(self):
        game = self.game()
        self.press(game, "ENTER")
        old = self.folder / f"pilot-{game.world.seed:016x}-12.json"
        old.write_text(json.dumps(dict(game._career(), version=1, seed=game.world.seed, state=game.state.to_dict())),
                       encoding="utf-8")
        aside = old.with_name(old.name + ".imported")
        aside.write_text("an earlier one", encoding="utf-8")
        game.close()
        for path in self.folder.glob("fair_winds-*"):
            path.unlink()
        again = self.game()
        self.assertEqual(again.state.turn, 1)
        self.assertEqual(aside.read_text(encoding="utf-8"), "an earlier one")
        self.assertTrue(old.exists(), "the newer file stays where it is")

    def test_an_old_save_file_that_cannot_be_used_is_left_alone(self):
        seed = fw.world_seed("relink-test")
        probe = self.game()
        seed = probe.world.seed
        probe.close()
        for path in self.folder.glob("fair_winds-*"):
            path.unlink()
        old = self.folder / f"pilot-{seed:016x}-12.json"
        for bad in ("{ not a save", "[]", json.dumps({"version": 99, "seed": seed})):
            old.write_text(bad, encoding="utf-8")
            fresh = self.game()
            self.assertEqual((fresh.state.turn, fresh.saved_ok), (0, True), "a new pilot, and saved")
            self.assertEqual(old.read_text(encoding="utf-8"), bad)
            self.assertIn("An older saved game could not be read. This is a new one; the file is kept.", self.screen(fresh))
            self.assertFalse(old.with_name(old.name + ".imported").exists())
            fresh.close()
            for path in self.folder.glob("fair_winds-*"):
                path.unlink()

    def test_a_missing_save_is_simply_a_new_pilot(self):
        game = self.game()
        self.assertNotIn("could not be read", self.screen(game))
        self.assertEqual(list(self.folder.glob("*.bad")), [])


class Screens(PlainCase):
    def test_never_out_of_bounds_through_a_flight(self):
        game = self.game()
        for size in ((80, 24), (132, 40), (240, 70)):
            self.screen(game, *size)
        self.press(game, "ENTER")
        for step in range(30):
            if game.phase() != "aloft":
                break
            self.press(game, ("w", "s", "j", "p", "l")[step % 5])
            self.screen(game)
            self.press(game, "ENTER")
            text = self.screen(game)
            self.assertEqual(len(text.split("\n")), 24)
        self.screen(game, 132, 40)

    def test_long_names_are_clipped(self):
        game = self.game(handle="M" * 200, node_name="N" * 200)
        text = self.screen(game)
        self.assertTrue(all(len(line) <= 79 for line in text.split("\n")[-1:]), "the last row leaves the corner alone")

    def test_help_explains_every_key(self):
        game = self.game()
        text = self.screen(game, view="help")
        for key in ("W / S", "Enter", "J ", "P ", "L ", "N ", "?", "Q "):
            self.assertIn(key, text)
        for glyph in ("@", "#", "o", "+", "~", "%"):
            self.assertIn(glyph, text)
        self.assertIn("every turn is saved", text)
        self.assertIn("A world day opens at the same hour for everyone", text)
        self.assertLessEqual(len(fw.PLAIN_HELP), 21, "help fits the screen above the key row")
        self.assertIn("Within 12 km of a place counts", text)
        self.assertTrue(all(len(line) <= 78 for line in fw.PLAIN_HELP))

    def test_sixteen_colours_and_ascii(self):
        game = self.game(color_depth="16", charset="ascii")
        self.press(game, "ENTER")
        session = self.session(game)
        session.paint()
        self.assertTrue(session.out.text.isascii())
        self.assertNotIn("38;2;", session.out.text)
        self.assertNotIn("38;5;", session.out.text)

    def test_the_log_wraps(self):
        game = self.game()
        game._say("word " * 40)
        self.assertTrue(all(len(line) <= 78 for line in game.log))
        self.assertEqual(len(game.log), 3)


class RealDoor(unittest.TestCase):
    """The whole thing over real pipes: launch, look, fly, leave, come back."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.env = {"DOOR_FW_DATA_DIR": self._dir.name}

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def test_launch_preview_commit_quit_and_resume(self):
        with Door(environ=self.env) as door:
            self.assertTrue(door.wait_for("Day 1  06:00"))
            door.send("\r")
            self.assertTrue(door.wait_for("Day 1  06:15"))
            door.send("w")
            self.assertTrue(door.wait_for("Climb to layer 2 Low: 3 fuel."))
            door.send("\r")
            self.assertTrue(door.wait_for("Day 1  06:30"))
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)
            self.assertEqual(door.errors, "")
        with Door(environ=self.env) as door:
            self.assertTrue(door.wait_for("Day 1  06:30"), "back on the same turn")
            self.assertIn(">2 Low", door.screen().text())
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)

    def test_a_killed_process_resumes_on_the_same_turn(self):
        with Door(environ=self.env) as door:
            self.assertTrue(door.wait_for("Day 1  06:00"))
            for clock in ("06:15", "06:30", "06:45"):
                door.send("\r")
                self.assertTrue(door.wait_for(f"Day 1  {clock}"))
            door.proc.kill()            # no goodbye: the session limit's CPU ceiling does exactly this
            door.proc.wait()
        with Door(environ=self.env) as door:
            # The killed session never gave the pilot up, and is not yet long enough gone to be sure of.
            self.assertTrue(door.wait_for("is already flying"))
            door.send("t")
            self.assertTrue(door.wait_for("Day 1  06:45"))
            door.send("\r")
            self.assertTrue(door.wait_for("Day 1  07:00"), "and it flies on")
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)

    def test_help_from_the_flight(self):
        with Door(environ=self.env) as door:
            self.assertTrue(door.wait_for("Day 1  06:00"))
            door.send("?")
            self.assertTrue(door.wait_for("A balloon cannot steer"))
            door.send("b")
            time.sleep(0.4)
            self.assertIn("Nearest post", door.screen().text())
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)

    def test_standalone_plays_too(self):
        with Door(hosted=False, environ=self.env) as door:
            self.assertTrue(door.wait_for("Day 1  06:00"))
            door.send("\r")
            self.assertTrue(door.wait_for("Day 1  06:15"))
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)


if __name__ == "__main__":
    unittest.main()
