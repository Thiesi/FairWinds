"""S05: the plain flight, the whole turn on an 80 x 24 text screen."""
import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path

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

    def tearDown(self):
        self._dir.cleanup()

    def game(self, **changes):
        return fw.PlainFlight(launch(**changes), self.folder)

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

    def save_file(self):
        files = [p for p in self.folder.glob("pilot-*.json")]
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding="utf-8"))


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
        self.assertEqual(len(list(self.folder.glob("pilot-*.json"))), 1)


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
        self.assertRegex(text, r"06:15  Layer 2: [\d.]+ km on a \w+ wind\.")
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
            self.assertRegex(text, r"\[N\] (Next day|Night here)")
            self.press(game, "n")
            self.assertEqual(game.state.day, day + 1)
        self.assertEqual((game.state.turn, game.state.status, game.state.distance), (0, "ground", 0.0))
        self.assertIn("launch", game.engine.actions(game.state), "and the balloon can fly again")
        self.assertEqual(game.days_flown, 1)


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
        old = list(game.targets)
        self.press(game, "l", "ENTER")
        self.assertEqual(game.deliveries, 1)
        self.assertEqual(game.state.status, "ground")
        self.assertNotIn(ref, game.targets, "the next post is for somewhere else")
        self.assertEqual(len(game.targets), 3)
        self.assertNotEqual(game.targets, old)
        self.assertEqual((game.state.fuel, game.state.ballast, game.state.pibals, game.state.wear), (60, 5, 3, 0),
                         "a town or station fills the balloon up, until towns have stores")
        text = self.screen(game)
        self.assertIn("is delivered. That makes 1.", text)
        self.assertIn("1 delivered", text)
        self.assertEqual(self.save_file()["deliveries"], 1)

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
        self.assertEqual(game.phase(), "stranded")
        self.assertIn("costs two days", self.screen(game))
        self.press(game, "ENTER")
        self.assertEqual((game.state.day, game.state.status, game.state.fuel), (3, "ground", 60))
        self.assertIsNotNone(game.engine.place(game.state.x, game.state.y), "walked out to a town")
        self.assertIn("Two days on foot", self.screen(game))

    def test_ditched_and_the_packet_boat(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, x=5.0, y=5.0, status="ditched", turn=30)
        self.assertIn("packet boat", self.screen(game))
        self.press(game, "n")
        self.assertEqual((game.state.day, game.state.status), (2, "ground"))
        place = game.engine.place(game.state.x, game.state.y)
        self.assertEqual(place[0], "town")
        self.assertTrue(game.world.towns[place[1]]["port"] or not any(t["port"] for t in game.world.towns))

    def test_a_worn_out_balloon_rests(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, wear=100)
        self.assertEqual(game.phase(), "rest")
        self.assertIn("too worn to fly", self.screen(game))
        self.press(game, "n")
        self.assertEqual((game.state.wear, game.state.day), (0, 2), "a night at a town mends it")

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
        self.assertEqual((again.targets, again.visited, again.log), (game.targets, game.visited, game.log))
        self.assertEqual(self.screen(again), self.screen(game), "the screen is the same screen")
        self.press(game, "ENTER")
        self.press(again, "ENTER")
        self.assertEqual(again.state, game.state, "and the next turn ends the same way")

    def test_looking_is_not_saved_flying_is(self):
        game = self.game()
        self.assertEqual(list(self.folder.glob("pilot-*.json")), [], "nothing is written until something happens")
        self.press(game, "ENTER")
        first = self.save_file()
        self.press(game, "w", "s", "w")
        self.assertEqual(self.save_file(), first)

    def test_a_guest_is_a_visit(self):
        game = self.game(user_id=2 ** 62 + 5)
        self.press(game, "ENTER", "ENTER")
        self.assertEqual(game.state.turn, 2)
        self.assertEqual(list(self.folder.glob("pilot-*")), [])
        self.assertIn("You are visiting: nothing is kept", self.screen(game))
        self.assertEqual(self.game(user_id=2 ** 62 + 5).state.turn, 0)

    def test_a_folder_that_cannot_be_written_does_not_trap_the_caller(self):
        blocker = self.folder / "in-the-way"
        blocker.write_text("not a folder", encoding="utf-8")
        game = fw.PlainFlight(launch(), blocker)
        self.press(game, "ENTER", "ENTER")
        self.assertEqual(game.state.turn, 2, "the game plays on")
        self.assertFalse(game.saved_ok)
        self.assertIn("cannot be saved here", self.screen(game))

    def test_a_damaged_or_foreign_save_starts_a_new_game(self):
        game = self.game()
        self.press(game, "ENTER")
        path = next(self.folder.glob("pilot-*.json"))
        good = path.read_text(encoding="utf-8")
        for bad in ("", "{", "[]", good[:40], good.replace('"version": 1', '"version": 99'),
                    good.replace(f'"seed": {game.world.seed}', '"seed": 1'),
                    good.replace('"targets": [[', '"targets": [["nowhere", 99], [')):
            path.write_text(bad, encoding="utf-8")
            fresh = self.game()
            self.assertEqual((fresh.state.turn, fresh.state.status), (0, "ground"))
            self.assertEqual(len(fresh.targets), 3)
        self.assertEqual(sorted(p.name for p in self.folder.glob("pilot-*")), [path.name], "no stray files")


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
        self.assertIn("saved every turn", text)
        self.assertLessEqual(len(fw.PLAIN_HELP), 21, "help fits the screen above the key row")
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
