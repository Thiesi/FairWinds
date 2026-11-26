"""Wonders (S21): the forty of a world, when each shows, taking one for the album, the album, and the pictures."""

import collections
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
YEAR = 365


def when_it_shows(weather, wonder, days=range(1, YEAR + 1)):
    """The first day and minute of a year, on a turn of the flying day, at which a wonder is to be seen."""
    low, high = fw.WONDER_KINDS[wonder["kind"]]["hours"]
    first = max(fw.DAWN_MINUTE, int(low * 60))
    first += (-(first - fw.DAWN_MINUTE)) % fw.TURN_MINUTES
    for day in days:
        for minute in range(first, min(int(high * 60), fw.MINUTES_PER_DAY - fw.TURN_MINUTES), fw.TURN_MINUTES):
            if fw.wonder_shows(weather, wonder, day, minute):
                return day, minute
    return None


class WonderCase(RescueCase):
    def put(self, game, wonder, away=0.0, layer=None):
        """Stand the balloon in the air where and when a wonder shows. Returns False if it never does in a year."""
        found = when_it_shows(game.weather, wonder)
        if found is None:
            return False
        day, minute = found
        game.today = max(game.today, day)
        x, y = wonder["x"] + away, wonder["y"]
        low, high = fw.WONDER_KINDS[wonder["kind"]]["layers"]
        open_layer = game.engine.lowest_open(x, y)
        want = max(low, open_layer)
        if wonder["kind"] in ("falls", "sealights"):
            want = open_layer
        if wonder["kind"] == "cloudsea":
            want = max(want, game.weather.frame(day, minute).cloud(wonder["x"], wonder["y"])[1] + 1)
        game.state = dataclasses.replace(game.state, x=x, y=y, day=day, turn=(minute - fw.DAWN_MINUTE) // fw.TURN_MINUTES,
                                         status="aloft", layer=layer or want, ceiling=6, oxygen=True, fuel=50)
        game._choose_default()
        return True


class TheForty(unittest.TestCase):
    def test_every_world_has_forty_of_nine_kinds_named_and_the_same_every_time(self):
        for node in NODES:
            world = fw.World(fw.world_seed(node))
            weather = fw.Weather(world)
            wonders = fw.wonders_of(world, weather)
            self.assertEqual(len(wonders), fw.WONDER_COUNT, node)
            self.assertEqual([w["id"] for w in wonders], list(range(40)))
            counts = collections.Counter(w["kind"] for w in wonders)
            self.assertEqual(dict(counts), {kind: spec["count"] for kind, spec in fw.WONDER_KINDS.items()}, node)
            self.assertEqual(len({w["name"] for w in wonders}), 40, "no two share a name")
            self.assertEqual((wonders[-1]["name"], wonders[-1]["x"], wonders[-1]["y"]),
                             ("The Eye of the Gyre", world.gyre["x"], world.gyre["y"]))
            again = fw.World(fw.world_seed(node))
            self.assertEqual([(w["name"], w["x"], w["y"]) for w in fw.wonders_of(again, fw.Weather(again))],
                             [(w["name"], w["x"], w["y"]) for w in wonders])

    def test_every_wonder_is_to_be_seen_on_some_day_of_a_year(self):
        for node in NODES[:3]:
            world = fw.World(fw.world_seed(node))
            weather = fw.Weather(world)
            for wonder in fw.wonders_of(world, weather):
                self.assertIsNotNone(when_it_shows(weather, wonder), (node, wonder["name"]))

    def test_one_worlds_wonders_are_where_they_were(self):
        # The records know a wonder by its number. If this fails the generator has changed, and albums already
        # begun now show other sights.
        world = fw.World(fw.world_seed("test-node"))
        wonders = fw.wonders_of(world, fw.Weather(world))
        self.assertEqual([(w["name"], w["cell"]) for w in (wonders[0], wonders[7], wonders[20], wonders[38])],
                         GOLDEN)

    def test_each_kind_keeps_its_hours_and_its_weather(self):
        world = fw.World(fw.world_seed("test-node"))
        weather = fw.Weather(world)
        by_kind = {w["kind"]: w for w in fw.wonders_of(world, weather)}
        for day in (3, 40, 200):
            self.assertFalse(fw.wonder_shows(weather, by_kind["aurora"], day, 12 * 60), "no lights at noon")
            self.assertFalse(fw.wonder_shows(weather, by_kind["salt"], day, 18 * 60))
            self.assertFalse(fw.wonder_shows(weather, by_kind["falls"], day, 22 * 60), "falls are seen by day")
            self.assertTrue(fw.wonder_shows(weather, by_kind["falls"], day, 10 * 60))
            self.assertFalse(fw.wonder_shows(weather, by_kind["sealights"], day, 10 * 60))
            self.assertTrue(fw.wonder_shows(weather, by_kind["sealights"], day, 22 * 60))
            self.assertTrue(fw.wonder_shows(weather, by_kind["eye"], day, 9 * 60), "the eye is always there")
            for minute in (20 * 60 + 45, 22 * 60):
                frame = weather.frame(day, minute)
                self.assertEqual(fw.wonder_shows(weather, by_kind["aurora"], day, minute),
                                 frame.cloud(by_kind["aurora"]["x"], by_kind["aurora"]["y"])[0] < 0.3)
            frame = weather.frame(day, 7 * 60)
            self.assertEqual(fw.wonder_shows(weather, by_kind["mist"], day, 7 * 60),
                             frame.fog(by_kind["mist"]["x"], by_kind["mist"]["y"]) > fw.FOG_THRESHOLD)
            self.assertEqual(fw.wonder_shows(weather, by_kind["cloudsea"], day, 7 * 60),
                             frame.cloud(by_kind["cloudsea"]["x"], by_kind["cloudsea"]["y"])[0] >= 0.6)


GOLDEN = [("The Tarn Lights", 3884), ("The Rook Mirror", 8497), ("Vane Storm Light", 3477), ("Pike Mist", 4607)]


class Taking(WonderCase):
    def test_every_wonder_of_a_world_is_taken_by_a_balloon_that_is_there_when_it_shows(self):
        game = self.game()
        was = game.postmarks
        for wonder in game.wonders:
            self.assertTrue(self.put(game, wonder), wonder["name"])
            self.assertIs(game.wonder_here(), wonder, wonder["name"])
            turn = game.state.turn
            game.key("f")
            self.assertIn(wonder["id"], game.album, wonder["name"])
            self.assertEqual(game.state.turn, turn, "it takes no turn")
            self.assertEqual(game.pending_scene["family"], wonder["kind"])
            self.assertEqual(game.pending_scene["caption"], wonder["name"])
            game.pending_scene = None
        self.assertEqual(len(game.album), 40)
        self.assertEqual(game.postmarks, was + 40 * fw.WONDER_PAY[1])
        self.assertEqual(self.rows("SELECT count(*), count(DISTINCT wonder) FROM wonders"), [(40, 40)])
        game.key("u")
        self.assertIn("You have seen 40 of 40", self.screen(game))

    def test_the_first_on_the_node_is_credited_once_and_everyone_has_an_album(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        wonder = next(w for w in a.wonders if w["kind"] == "falls")
        was = (a.postmarks, a.points, b.postmarks, b.points)
        self.put(a, wonder)
        self.assertIn("[F] Wonder", self.screen(a))
        a.key("f")
        self.assertEqual((a.postmarks - was[0], a.points - was[1]), (fw.WONDER_PAY[1], fw.WONDER_POINTS[1]))
        self.assertTrue(any(wonder["name"] in line and "Nobody here had seen it" in line for line in a.heard))
        self.assertEqual(support.stored(self.folder, 1)["postmarks"], a.postmarks)
        self.assertIsNone(a.wonder_here(), "in the album: there is nothing more to take")
        self.assertNotIn("[F] Wonder", self.screen(a))
        a.key("f")
        self.assertEqual(a.postmarks - was[0], fw.WONDER_PAY[1])
        self.put(b, wonder)
        b.key("f")                                        # b has not looked: the records know who was first
        self.assertEqual((b.postmarks - was[2], b.points - was[3]), (fw.WONDER_PAY[0], fw.WONDER_POINTS[0]))
        self.assertTrue(any("In your album" in line for line in b.heard))
        self.assertEqual(self.rows("SELECT user_id, handle FROM wonders"), [(1, "Ada")])
        self.assertEqual(self.rows("SELECT count(*) FROM seen"), [(2,)])
        self.assertEqual(b.sighted[wonder["id"]][:2], (1, "Ada"), "who was first comes back with the write")
        self.assertIn("First seen by Ada", b.pending_scene["sub"], "so the picture names them at once")
        b.close()

    def test_a_dawn_wonder_is_flown_to_from_where_the_pilot_slept(self):
        # A day begins where the balloon came down the day before. Down by a mist site in the evening,
        # up at 06:00 into the lowest layer, and the mist is there to be taken on the first turn.
        game = self.game()
        took = 0
        for wonder in (w for w in game.wonders if w["kind"] in ("mist", "falls")):
            low, _ = fw.WONDER_KINDS[wonder["kind"]]["hours"]
            dawn = max(fw.DAWN_MINUTE, int(low * 60))
            day = next((d for d in range(1, YEAR + 1) if fw.wonder_shows(game.weather, wonder, d, dawn)
                        and fw.wonder_shows(game.weather, wonder, d, dawn + fw.TURN_MINUTES)), None)
            if day is None:
                continue
            game.today = max(game.today, day)
            game.state = dataclasses.replace(game.state, x=wonder["x"], y=wonder["y"], day=day, layer=0,
                                             turn=(dawn - fw.DAWN_MINUTE) // fw.TURN_MINUTES, status="ground", fuel=40)
            game._choose_default()
            if "launch" not in game.offered():
                continue
            game.key("ENTER")                             # a real turn: launched, and drifted a quarter of an hour
            self.assertEqual(game.state.status, "aloft")
            if game.wonder_here() is wonder:
                self.assertTrue(any("something to see" in line for line in game.heard), "and it is pointed out")
                game.key("f")
                self.assertIn(wonder["id"], game.album)
                took += 1
                game.pending_scene = None
        self.assertGreaterEqual(took, 5, "most of the ten low wonders are taken on the first turn of such a morning")

    def test_what_is_in_view_is_worked_out_once_for_a_turn(self):
        game = self.game()
        falls = next(w for w in game.wonders if w["kind"] == "falls")
        self.put(game, falls)
        calls = []
        real = game.wonder_in_view
        game.wonder_in_view = lambda w, state=None: calls.append(w["id"]) or real(w, state)
        for _ in range(5):
            self.assertIs(game.wonder_here(), falls)
        self.assertLessEqual(len(calls), 40)
        game.state = dataclasses.replace(game.state, x=game.state.x + 300.0)
        self.assertIsNone(game.wonder_here(), "and again when the balloon has moved")

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found, done: (got.append(done), {})[1], {'day': 3, 'turn': 1},\n"
              "         'wonder', deed=lambda db: fw.see_wonder(db, 7, user, 'P%d' % user, 3))\n"
              "print(int(got[0]['first']), int(got[0]['new']))\n"
              "s.release(user); s.close()\n")

    def test_eight_pilots_taking_one_wonder_at_once(self):
        game = self.game()
        seed = game.world.seed
        game.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for user in range(101, 109)]
        first = new = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            a, b = out.decode().split()
            first, new = first + int(a), new + int(b)
        self.assertEqual((first, new), (1, 8), "one is first; every one of them has it")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        done = fw.see_wonder(db, 7, 999, "Late", 4)
        db.close()
        self.assertEqual((done["first"], done["by"][0] in range(101, 109)), (False, True), "and a later one is told who")
        self.assertEqual(self.rows("SELECT count(*) FROM wonders"), [(1,)])

    def test_only_from_where_and_when_it_shows(self):
        game = self.game()
        falls = next(w for w in game.wonders if w["kind"] == "falls")
        lights = next(w for w in game.wonders if w["kind"] == "aurora")
        self.put(game, falls)
        here = game.state
        reach = fw.WONDER_KINDS["falls"]["km"]
        self.assertTrue(game.wonder_in_view(falls))
        self.assertFalse(game.wonder_in_view(falls, dataclasses.replace(here, x=here.x + reach + 1.0)), "too far")
        self.assertFalse(game.wonder_in_view(falls, dataclasses.replace(here, layer=here.layer + 1)), "too high")
        self.assertFalse(game.wonder_in_view(falls, dataclasses.replace(here, status="ground", layer=0)), "not aloft")
        self.assertFalse(game.wonder_in_view(falls, dataclasses.replace(here, turn=66)), "falls are seen by day")
        self.put(game, lights)
        self.assertTrue(game.wonder_in_view(lights))
        low = fw.WONDER_KINDS["aurora"]["layers"][0]
        if game.engine.lowest_open(game.state.x, game.state.y) < low:
            self.assertFalse(game.wonder_in_view(lights, dataclasses.replace(game.state, layer=low - 1)))
        game.key("t")
        self.assertFalse(game.drift_lobby, "aloft: no Drift")
        game.drift = {"course": {"mx": 0.0, "my": 0.0}, "scored": False}
        self.assertIsNone(game.wonder_here(), "nor is a wonder taken on a balloon that is not the pilot's")
        game.drift = None

    def test_a_cloud_sea_is_seen_from_above_the_deck(self):
        game = self.game()
        sea = next(w for w in game.wonders if w["kind"] == "cloudsea")
        self.put(game, sea)
        deck = game.weather.frame(game.state.day, game.state.minute).cloud(sea["x"], sea["y"])[1]
        self.assertTrue(game.wonder_in_view(sea))
        self.assertEqual(game.state.layer, max(deck + 1, game.engine.lowest_open(sea["x"], sea["y"])))
        self.assertFalse(game.wonder_in_view(sea, dataclasses.replace(game.state, layer=deck)), "in the cloud")

    def test_the_camera_sees_from_half_as_far_again(self):
        game = self.game()
        salt = next(w for w in game.wonders if w["kind"] == "salt")
        reach = fw.WONDER_KINDS["salt"]["km"]
        self.put(game, salt, away=reach * 1.3)
        self.assertEqual(game.gear["camera"], 0)
        self.assertFalse(game.wonder_in_view(salt))
        game.gear["camera"] = 1
        self.assertTrue(game.wonder_in_view(salt))
        self.assertEqual(fw.UPGRADES["camera"]["tiers"][1]["range"], 1.5)
        self.assertEqual(fw.gear_text("camera", fw.UPGRADES["camera"]["tiers"][1]), "wonders from half as far again")

    def test_a_wonder_coming_into_view_is_pointed_out_once(self):
        game = self.game()
        falls = next(w for w in game.wonders if w["kind"] == "falls")
        self.put(game, falls)
        game.heard = []
        game._point_out()
        game._point_out()
        self.assertEqual(sum("something to see" in line for line in game.heard), 1)
        game.state = dataclasses.replace(game.state, x=game.state.x + 300.0)
        game._point_out()
        self.put(game, falls)
        game._point_out()
        self.assertEqual(sum("something to see" in line for line in game.heard), 2, "and again when it comes back")

    def test_a_wonder_that_cannot_be_written_is_not_in_the_album(self):
        game = self.game()
        falls = next(w for w in game.wonders if w["kind"] == "falls")
        self.put(game, falls)
        was, real = (game.postmarks, game.points), game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        game.store.commit = broken
        game.key("f")
        game.store.commit = real
        self.assertEqual((game.postmarks, game.points, game.album, game.sighted), was + (set(), {}))
        self.assertIsNone(game.pending_scene, "no picture of what was not kept")

    def test_a_visitor_and_a_world_from_before(self):
        guest = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        falls = next(w for w in guest.wonders if w["kind"] == "falls")
        self.put(guest, falls)
        guest.key("f")
        self.assertIn(falls["id"], guest.album)
        guest.close()
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in range(1, 7):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=6")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.execute("INSERT INTO drifts VALUES (1, 9, 'Bo', '{}', 1, 700, 3.0, 9, 5, 1.0)")
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        falls = next(w for w in game.wonders if w["kind"] == "falls")
        self.put(game, falls)
        game.key("f")
        self.assertEqual(self.rows("PRAGMA user_version"), [(fw.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT count(*) FROM wonders"), [(1,)])
        self.assertEqual(self.rows("SELECT handle FROM drifts"), [("Bo",)])


class Album(WonderCase):
    def test_a_hint_says_more_as_the_chart_grows(self):
        game = self.game()
        wonder = next(w for w in game.wonders if w["kind"] == "salt")
        game.chart.discard(fw.cell_at(wonder["x"], wonder["y"]))
        real = game.charted_share
        try:
            game.charted_share = lambda: 0.05
            vague = game.wonder_hint(wonder)
            self.assertEqual(vague, "A dry plain that shines at noon.")
            game.charted_share = lambda: fw.WONDER_HINTS[0]
            where = game.wonder_hint(wonder)
            self.assertTrue(where.startswith("A dry plain that shines at noon, ") and " of " in where or "at " in where)
            self.assertNotIn("11:00", where)
            game.charted_share = lambda: fw.WONDER_HINTS[1]
            how = game.wonder_hint(wonder)
            self.assertIn("between 11:00 and 13:30 under a clear sky", how)
            self.assertTrue(len(vague) < len(where) < len(how))
            game.charted_share = lambda: 0.0
            game.chart.add(fw.cell_at(wonder["x"], wonder["y"]))
            self.assertEqual(game.wonder_hint(wonder), how, "a place somebody has charted is told in full")
            self.assertEqual(game.wonder_hint(game.wonders[-1]), "The still middle of the great storm.")
        finally:
            game.charted_share = real

    def test_the_album_shows_all_forty_across_its_pages(self):
        game = self.game()
        game.key("u")
        self.assertTrue(game.album_open)
        pages = game._album_pages()
        self.assertEqual(sum(len(page) for page in pages), len(fw.WONDER_KINDS))
        seen = 0
        for number in range(len(pages)):
            text = self.screen(game)
            self.assertIn(f"Page {number + 1} of {len(pages)}", text)
            body = text.split("\n")[2:21]
            titles = [spec["title"].upper() for spec in fw.WONDER_KINDS.values()]
            seen += sum(1 for line in body if line.strip() and not any(line.strip().startswith(t) for t in titles))
            self.assertTrue(all(len(line) <= 79 for line in text.split("\n")))
            for size in ((100, 30), (132, 40)):
                self.screen(game, *size)
            game.key("s")
        self.assertEqual(seen, 40, "every wonder has its line, on some page, at 80 x 24")
        self.assertEqual(game.album_page, len(pages) - 1, "the last page is the last")
        for _ in range(5):
            game.key("w")
        self.assertEqual(game.album_page, 0)
        game.key("ENTER")
        self.assertTrue(game.album_open, "Enter does not leave the album")
        game.key("b")
        self.assertFalse(game.album_open)

    def test_what_is_seen_is_named_and_what_another_saw_is_told_in_full(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        wonder = a.wonders[0]
        self.put(a, wonder)
        a.key("f")
        a.pending_scene = None
        a.key("u")
        text = self.screen(a)
        self.assertIn(f"* {wonder['name']}", text)
        self.assertIn("First: Ada", text)
        self.assertIn("You have seen 1 of 40; the node 1", text)
        self.assertIn("LIGHTS  after 20:30 under a clear sky, from layer 2 or above", text, "one seen: how is told")
        self.assertIn("MIRRORS  nobody knows yet how", text)
        b.key("u")
        text = self.screen(b)
        self.assertIn(f"- {wonder['name']}", text)
        self.assertIn("Seen by Ada", text)
        self.assertIn("You have seen 0 of 40; the node 1", text)
        b.close()

    def test_the_help_names_the_keys(self):
        text = " ".join(fw.PLAIN_HELP)
        self.assertIn("`U` album", text)
        self.assertIn("`F` a wonder", text)


class Pictures(unittest.TestCase):
    def frame(self, family, width=80, height=24, steps=3, depth="truecolor"):
        scene = fw.Scene("wonder", width, height, True, True, hour=12.0, seed=9, family=family, caption="A name",
                         sub="First seen by Mara on Day 2.", flat=depth != "truecolor")
        scene.advance(steps)
        screen = fw.Screen(width, height, depth)
        scene.draw(screen)
        return scene, screen

    def test_every_kind_has_a_picture_of_its_own_at_every_size(self):
        plain = {}
        for size in ((80, 24), (132, 40), (168, 46)):
            back = fw.Screen(*size)
            fw.Backdrop(size[0], size[1], 12.0, seed=9).draw(back)
            pictures = {}
            for family in fw.WONDER_KINDS:
                scene, screen = self.frame(family, *size)
                self.assertEqual(scene.family, family)
                pictures[family] = (tuple(screen.fg), tuple(screen.bg))
                changed = sum(1 for k in range(len(screen.fg)) if (screen.fg[k], screen.bg[k]) != (back.fg[k], back.bg[k]))
                self.assertGreater(changed, size[0] * size[1] // 12, (family, size, "the wonder is most of the picture"))
            self.assertEqual(len(set(pictures.values())), len(fw.WONDER_KINDS), "no two kinds look alike")
            plain[size] = pictures

    def test_the_name_and_who_saw_it_first_are_on_the_picture(self):
        _, screen = self.frame("aurora")
        rows = ["".join(screen.ch[r * 80:(r + 1) * 80]) for r in range(24)]
        self.assertIn("A name", rows[21])
        self.assertIn("First seen by Mara on Day 2.", rows[22])

    def test_what_moves_moves_and_the_picture_ends(self):
        _, one = self.frame("aurora", steps=1)
        _, two = self.frame("aurora", steps=7)
        self.assertNotEqual(one.fg, two.fg, "the lights shimmer")
        scene = fw.Scene("wonder", 80, 24, True, True, family="salt")
        self.assertFalse(scene.hold)
        for _ in range(200):
            if scene.over:
                break
            scene.advance()
        self.assertTrue(scene.over)
        self.assertGreater(scene.frames + scene.linger, 20, "a wonder stays a moment longer than a landing")

    def test_in_256_colours_and_without_pixels(self):
        for family in fw.WONDER_KINDS:
            self.frame(family, depth="256")
        scene = fw.Scene("wonder", 80, 24, False, True, family="mist", caption="Kestrel Mist", sub="")
        screen = fw.Screen(80, 24)
        scene.draw(screen)
        self.assertIn("Kestrel Mist", "".join(screen.ch))


if __name__ == "__main__":
    unittest.main()
