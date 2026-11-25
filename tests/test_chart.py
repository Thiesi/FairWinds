"""The chart (S14): what is seen from the basket, whose it is, features found and named, and the atlas."""

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
from test_post import PostCase                            # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class ChartCase(PostCase):
    def other(self, user_id, **changes):
        """Another pilot of the same world, in a session of their own that stays open."""
        changes.setdefault("timezone", "UTC")
        return fw.PlainFlight(launch(user_id=user_id, **changes), self.folder, now=self.clock)

    def wild(self, game, clear=True):
        """A spot of unknown land, far from every place, under a clear sky if asked."""
        world = game.world
        for cell in sorted(world._land - game.known - game.chart):
            x, y = (cell % fw.CHART_W + 0.5) * fw.CHART_KM, (cell // fw.CHART_W + 0.5) * fw.CHART_KM
            near = fw.cells_within(x, y, 30.0)
            if any(c in game.known or c in game.chart for c in near) or len(near) < 20:
                continue
            if clear and game.weather.frame(game.state.day, game.state.minute).cloud(x, y)[0] >= fw.CLOUD_BLOCKS:
                continue
            return x, y, cell
        self.fail("no unknown land")

    def fly_over(self, game, x, y, layer=3):
        """One turn that ends over a place at a height, as the engine would leave it."""
        game.state = dataclasses.replace(game.state, x=x, y=y, layer=layer, status="aloft", turn=12, ceiling=5)
        seen = game.seen_cells(x, y, layer, game.state.day, game.state.minute)
        game.save("hold", seen)
        return seen


class Sight(ChartCase):
    def test_how_far_is_seen_grows_with_height(self):
        game = self.game()
        x, y, _ = self.wild(game)
        counts = []
        for layer in range(1, 7):
            cells = fw.cells_within(x, y, fw.SIGHT_KM[layer - 1])
            counts.append(len(cells))
            for cell in cells:
                cx, cy = (cell % fw.CHART_W + 0.5) * fw.CHART_KM, (cell // fw.CHART_W + 0.5) * fw.CHART_KM
                self.assertLessEqual((cx - x) ** 2 + (cy - y) ** 2, fw.SIGHT_KM[layer - 1] ** 2 + 1e-6)
        self.assertEqual(fw.SIGHT_KM, (10.0, 20.0, 30.0, 40.0, 50.0, 50.0))
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], 6)
        self.assertGreater(counts[4], 60)
        self.assertEqual(game.seen_cells(x, y, 0, 1, 600), [], "nothing is charted from the ground")

    def test_cloud_below_the_balloon_hides_the_ground(self):
        game = self.game()
        real = fw.WeatherFrame.cloud
        x, y, cell = self.wild(game, clear=False)
        try:
            fw.WeatherFrame.cloud = lambda self, cx, cy: (0.9, 3)      # a deck in layer 3, everywhere
            self.assertEqual(game.seen_cells(x, y, 4, 1, 600), [], "above the deck: nothing")
            self.assertEqual(game.seen_cells(x, y, 5, 1, 600), [])
            self.assertTrue(game.seen_cells(x, y, 3, 1, 600), "in it or under it: the ground is in sight")
            self.assertTrue(game.seen_cells(x, y, 2, 1, 600))
            fw.WeatherFrame.cloud = lambda self, cx, cy: (fw.CLOUD_BLOCKS - 0.05, 3)
            self.assertTrue(game.seen_cells(x, y, 5, 1, 600), "broken cloud lets the ground through")
            fw.WeatherFrame.cloud = lambda self, cx, cy: ((0.9, 3) if cx < x else (0.0, 3))
            half = game.seen_cells(x, y, 5, 1, 600)
            self.assertTrue(half)
            self.assertTrue(all((c % fw.CHART_W + 0.5) * fw.CHART_KM >= x for c in half), "cell by cell")
        finally:
            fw.WeatherFrame.cloud = real

    def test_the_ground_around_every_place_was_known_from_the_start(self):
        game = self.game()
        for place in game.world.towns + game.world.stations:
            self.assertIn(fw.cell_at(place["x"], place["y"]), game.known)
            self.assertTrue(game.is_charted(place["x"], place["y"]))
        self.assertEqual(game.chart, set())
        self.assertGreater(game.charted_share(), 0.02)
        self.assertLess(game.charted_share(), 0.2, "and most of the land is still to chart")
        self.assertTrue(game.is_charted(-50.0, 10.0), "off the chart there is nothing to chart")

    def test_a_turn_of_a_real_flight_charts_what_it_saw(self):
        game = self.game()
        place = game.world.towns[game.world.start_town]
        game.state = dataclasses.replace(game.state, x=place["x"] + 45.0, y=place["y"], status="aloft",
                                         layer=game.engine.lowest_open(place["x"] + 45.0, place["y"]) + 1,
                                         ceiling=5, turn=12)
        game._choose_default()
        game.key("ENTER")
        self.assertTrue(game.chart, "the turn put cells on the chart")
        stored = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        rows = stored.execute("SELECT cell, user_id, day FROM charted").fetchall()
        stored.close()
        self.assertEqual({r[0] for r in rows}, game.chart)
        self.assertEqual({(r[1], r[2]) for r in rows}, {(12, 1)}, "in this pilot's name, on this day")
        self.assertEqual(game.chart & game.known, set(), "what was known already is not charted again")


class Shared(ChartCase):
    def test_what_one_pilot_charts_the_next_sees(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y, cell = self.wild(a)
        self.assertFalse(b.is_charted(x, y))
        seen = self.fly_over(a, x, y)
        self.assertIn(cell, seen)
        self.assertTrue(a.is_charted(x, y))
        self.assertFalse(b.is_charted(x, y), "not until it looks")
        version = b.chart_version
        self.assertTrue(b._day_may_have_opened(), "the next look: the screen has to change")
        self.assertTrue(b.is_charted(x, y))
        self.assertGreater(b.chart_version, version, "and the map is composed again")
        self.assertFalse(b._day_may_have_opened(), "and then it is quiet again")
        c = self.other(3)
        self.assertTrue(c.is_charted(x, y), "a pilot who arrives later has it from the start")

    def test_the_first_over_a_cell_is_the_one_credited(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y, _ = self.wild(a)
        self.fly_over(a, x, y)
        first, pay = a.charted, a.postmarks
        self.assertGreater(first, 0)
        self.fly_over(b, x, y)                           # b has not looked: it thinks the ground is new
        self.assertEqual((b.charted, b.postmarks), (0, fw.START_POSTMARKS), "second over it earns nothing")
        self.assertEqual((a.charted, a.postmarks), (first, pay))
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        self.assertEqual({row[0] for row in db.execute("SELECT DISTINCT user_id FROM charted")}, {1})
        db.close()

    def test_land_pays_and_water_does_not(self):
        game = self.game()
        x, y, _ = self.wild(game)
        before = game.postmarks
        seen = self.fly_over(game, x, y)
        land = sum(1 for c in seen if c in game.world._land and c not in game.known)
        self.assertEqual(game.charted, land)
        self.assertEqual(game.postmarks, before + land // fw.CHART_PER_POSTMARK)
        self.assertEqual(game.points, land // fw.CHART_PER_POINT)
        sea = next(c for c in range(fw.CHART_W * fw.CHART_H)
                   if not any(n in game.world._land for n in fw.cells_within((c % fw.CHART_W + 0.5) * 10, (c // fw.CHART_W + 0.5) * 10, 50)))
        marks, cells = game.postmarks, game.charted
        self.fly_over(game, (sea % fw.CHART_W + 0.5) * 10, (sea // fw.CHART_W + 0.5) * 10, layer=1)
        self.assertEqual((game.postmarks, game.charted), (marks, cells), "open sea is charted for nothing")
        self.assertIn(sea, game.chart)

    def test_postmarks_and_points_come_out_right_over_many_small_steps(self):
        game = self.game()
        land = sorted(game.world._land - game.known)[:23]
        before = game.postmarks
        for cell in land:
            game.save("hold", [cell])
        self.assertEqual(game.charted, 23)
        self.assertEqual(game.postmarks, before + 23 // fw.CHART_PER_POSTMARK)
        self.assertEqual(game.points, 23 // fw.CHART_PER_POINT)
        self.assertEqual(support.stored(self.folder)["charted"], 23)

    def test_the_share_charted_is_what_the_grid_says(self):
        game = self.game()
        land = game.world._land
        self.assertEqual(game.charted_share(), len(land & game.known) / len(land))
        some = sorted(land - game.known)[:400]
        game.save("hold", some)
        self.assertAlmostEqual(game.charted_share(), (len(land & game.known) + 400) / len(land))
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        rows = {row[0] for row in db.execute("SELECT cell FROM charted")}
        db.close()
        self.assertEqual(len(rows | game.known) and len((rows | game.known) & land) / len(land), game.charted_share())
        self.assertIn(f"{game.charted_share() * 100:.0f}% charted", self.screen(game, 132, 40))

    def test_ground_somebody_else_charted_first_is_on_this_pilots_map_at_once(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y, cell = self.wild(a)
        self.fly_over(a, x, y)
        seen = self.fly_over(b, x, y)                    # b had not looked, and learns from its own turn
        self.assertTrue(b.is_charted(x, y))
        self.assertLessEqual(set(seen), b.chart | b.known)
        version = b.chart_version
        self.fly_over(b, x, y)
        self.assertEqual(b.chart_version, version, "and the same ground is not sent in again")

    def test_a_fast_turn_charts_what_it_passed_over(self):
        game = self.game()
        x, y, _ = self.wild(game)
        game.state = dataclasses.replace(game.state, x=x - 60.0, y=y, layer=1, status="aloft", turn=12)
        game._choose_default()
        real = fw.WeatherFrame.wind
        try:
            fw.WeatherFrame.wind = lambda self, wx, wy, layer: (240.0, 0.0)     # sixty km in the turn
            game.key("ENTER")
        finally:
            fw.WeatherFrame.wind = real
        self.assertAlmostEqual(game.state.x, x)
        middle = fw.cell_at(x - 30.0, y)
        self.assertTrue(middle in game.chart or middle in game.known, "the ground under the middle of the turn")

    def test_the_share_is_counted_once_for_each_state_of_the_chart(self):
        game = self.game()
        first = game.charted_share()
        kept = game._share
        for _ in range(5):
            game.charted_share()
        self.assertIs(game._share, kept)
        game.save("hold", sorted(game.world._land - game.known)[:50])
        self.assertGreater(game.charted_share(), first)

    def test_charting_is_said_once_when_the_flight_is_down(self):
        game = self.game()
        x, y, _ = self.wild(game)
        self.fly_over(game, x, y)
        self.assertNotIn("Charted", " ".join(game.log), "not every turn")
        cells, pay = game.flight_cells, game.flight_pay
        self.assertGreater(cells, 0)
        game._say_charted()
        self.assertIn(f"Charted on the way: {cells} cells of land nobody had seen, {pay} postmark", " ".join(game.log))
        self.assertEqual((game.flight_cells, game.flight_pay), (0, 0))

    def test_a_visit_charts_for_itself_and_leaves_no_mark(self):
        guest = fw.PlainFlight(launch(user_id=2 ** 62 + 4, timezone="UTC"), self.folder, now=self.clock)
        x, y, _ = self.wild(guest)
        self.fly_over(guest, x, y)
        self.assertTrue(guest.is_charted(x, y))
        self.assertGreater(guest.charted, 0)
        self.assertEqual(list(self.folder.glob("*.db")), [])
        self.assertFalse(self.game().is_charted(x, y))


class Features(ChartCase):
    def feature(self, game, kind=None):
        """A feature nobody has found, none of it known from the start."""
        for feature in game.world.features:
            if kind and feature["kind"] != kind:
                continue
            if 2 <= len(feature["cells"]) <= 40 and not any(c in game.known for c in feature["cells"]):
                return feature
        self.fail("no such feature")

    def test_names_are_made_of_the_word_lists_and_nothing_else(self):
        game = self.game(handle="<script>`\x1b[2J</script>", node_name="\x1b]0;x\x07")
        firsts = set(fw._NAME_FIRST)
        for feature in game.world.features:
            names = fw.feature_names(game.world, feature)
            self.assertEqual(len(names), 3)
            self.assertEqual(len(set(names)), 3)
            self.assertEqual(names, fw.feature_names(game.world, feature), "the same three every time")
            for name in names:
                first, last = name.split(" ", 1)
                self.assertIn(first, firsts)
                self.assertIn(" " + last, fw.FEATURE_WORDS[feature["kind"]])
                self.assertTrue(name.isascii() and name.replace(" ", "").isalpha())
        self.assertEqual({f["kind"] for f in game.world.features}, set(fw.FEATURE_WORDS))

    def test_a_feature_is_found_when_most_of_it_is_charted(self):
        game = self.game()
        feature = self.feature(game)
        cells = feature["cells"]
        need = -(-len(cells) * 6 // 10)                  # sixty in a hundred, rounded up
        before = game.postmarks
        game.save("hold", cells[:need - 1])
        self.assertNotIn(feature["id"], game.found)
        self.assertEqual(game.naming, [])
        game.save("hold", cells[need - 1:need])
        self.assertIn(feature["id"], game.found)
        names = fw.feature_names(game.world, feature)
        self.assertEqual(game.naming, [{"id": feature["id"], "kind": feature["kind"], "names": names}])
        self.assertEqual(game.found[feature["id"]], (names[0], 12, "Mara", 1), "until chosen, the first name stands")
        land = sum(1 for c in cells[:need] if c in game.world._land)
        self.assertEqual(game.postmarks, before + land // fw.CHART_PER_POSTMARK + fw.DISCOVERY_PAY)
        self.assertIn("nobody had named", " ".join(game.log))

    def test_the_finder_picks_one_of_three_names(self):
        game = self.game()
        feature = self.feature(game)
        game.save("hold", feature["cells"])
        names = fw.feature_names(game.world, feature)
        text = self.screen(game)
        self.assertIn(f"Name the {feature['kind']}:   1 {names[0]}   2 {names[1]}   3 {names[2]}", text)
        self.assertIn("[1-3] Name it", text)
        turn = game.state.turn
        game.key("2")
        self.assertEqual(game.naming, [])
        self.assertEqual(game.found[feature["id"]][0], names[1])
        self.assertEqual(game.state.turn, turn, "the key was the name's")
        self.assertIn(f"It is {names[1]} on every chart now.", " ".join(game.log))
        other = self.other(2)
        self.assertEqual(other.found[feature["id"]], (names[1], 12, "Mara", 1), "with the finder's name beside it")

    def test_any_other_key_takes_the_first_and_is_not_a_move(self):
        game = self.game()
        fresh = [f for f in game.world.features
                 if 2 <= len(f["cells"]) <= 40 and not any(c in game.known for c in f["cells"])]
        for feature, key in zip(fresh, ("ENTER", " ", "x", "9", "0", "ESC", "w")):
            game.save("hold", feature["cells"])
            self.assertTrue(game.naming, key)
            before = game.state
            owed = len(game.naming)                      # a peak inside a range: two may be found at once
            self.assertIsNone(game.key(key))
            self.assertEqual(len(game.naming), owed - 1, "one key, one name")
            while game.naming:
                game.key(key)
            self.assertEqual(game.found[feature["id"]][0], fw.feature_names(game.world, feature)[0])
            self.assertEqual(game.state, before, "the keys were the names', not moves")

    def test_leaving_with_a_name_owed_leaves_the_first_name(self):
        game = self.game()
        feature = self.feature(game)
        game.save("hold", feature["cells"])
        self.assertEqual(game.key("q"), "quit")
        game.close()
        again = self.game()
        self.assertEqual(again.found[feature["id"]][0], fw.feature_names(again.world, feature)[0])
        self.assertEqual(again.naming, [], "and it is not asked again")

    def test_a_name_is_chosen_once(self):
        game = self.game()
        feature = self.feature(game)
        game.save("hold", feature["cells"])
        names = fw.feature_names(game.world, feature)
        game.key("3")
        self.assertFalse(game.store.name_feature(feature["id"], 12, names[0]), "not twice")
        other = self.other(2)
        self.assertFalse(other.store.name_feature(feature["id"], 2, names[0]), "and not by somebody else")
        self.assertEqual(self.other(3).found[feature["id"]][0], names[2])

    def test_two_pilots_crossing_the_line_together_find_it_once(self):
        a = self.game(user_id=1)
        b = self.other(2)
        feature = self.feature(a)
        cells = feature["cells"]
        half = len(cells) // 2
        a.save("hold", cells[:half])
        b.save("hold", cells[half:])                     # b does not know of a's half: between them it is found
        finders = [g for g in (a, b) if g.naming]
        self.assertEqual(len(finders), 1, "found by the turn that crossed the line, once")
        self.assertIs(finders[0], b)
        a.save("hold", cells)
        self.assertEqual(a.naming, [], "and not again by the other")
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        self.assertEqual(db.execute("SELECT count(*), min(user_id) FROM features").fetchone(), (1, 2))
        db.close()
        self.assertEqual(a.postmarks - fw.START_POSTMARKS < fw.DISCOVERY_PAY, True)

    WORKER = ("import sys, json; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3]); cells = json.loads(sys.argv[4])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[5]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found: (got.extend(found), {})[1], {'day': 1, 'turn': 1}, 'hold',\n"
              "         cells, {7: (cells, len(cells), 'Gull Tarn')})\n"
              "print(len(got)); s.release(user); s.close()\n")

    def test_eight_processes_charting_the_same_feature_at_once_find_it_once(self):
        game = self.game()
        seed = game.world.seed
        game.close()
        cells = list(range(5000, 5030))
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user),
                                   json.dumps(cells), str(seed)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for user in range(1, 9)]
        found = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            found += int(out.decode().strip())
        self.assertEqual(found, 1, "exactly one of the eight is the finder")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        self.assertEqual(db.execute("SELECT count(*) FROM features").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM charted").fetchone()[0], len(cells), "each cell once")
        self.assertEqual(db.execute("SELECT count(DISTINCT user_id) FROM charted WHERE cell=5000").fetchone()[0], 1)
        db.close()

    def test_a_feature_partly_known_from_the_start_needs_only_the_rest(self):
        game = self.game()
        feature = next(f for f in game.world.features
                       if 0 < sum(c in game.known for c in f["cells"]) < len(f["cells"]) * 0.6 and len(f["cells"]) >= 5)
        known = [c for c in feature["cells"] if c in game.known]
        rest = [c for c in feature["cells"] if c not in game.known]
        need = -(-len(feature["cells"]) * 6 // 10) - len(known)
        game.save("hold", rest[:need - 1])
        self.assertNotIn(feature["id"], game.found)
        game.save("hold", rest[need - 1:need])
        self.assertIn(feature["id"], game.found)

    def test_found_names_are_on_the_map(self):
        game = self.game()
        feature = self.feature(game)
        game.save("hold", feature["cells"])
        game.key("1")
        name = game.found[feature["id"]][0]
        middle = feature["cells"][len(feature["cells"]) // 2]
        game.state = dataclasses.replace(game.state, x=(middle % fw.CHART_W + 0.5) * 10 + 30.0,
                                         y=(middle // fw.CHART_W + 0.5) * 10 + 20.0, status="aloft", layer=3, ceiling=5)
        game._choose_default()
        self.assertIn(name[:8], self.screen(game, 132, 40))


class Records(ChartCase):
    def test_a_world_from_before_the_chart_is_brought_forward_and_keeps_its_pilots(self):
        game = self.game()
        game.key("ENTER")
        turn = game.state.turn
        game.close()
        path = str(next(self.folder.glob("fair_winds-*.db")))
        db = sqlite3.connect(path)
        db.execute("DROP TABLE charted")
        db.execute("DROP TABLE features")
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
        again = self.game()
        self.assertTrue(again.saved_ok)
        self.assertEqual(again.state.turn, turn)
        db = sqlite3.connect(path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], fw.SCHEMA_VERSION)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        self.assertLessEqual({"charted", "features"}, tables)
        x, y, _ = self.wild(again)
        self.fly_over(again, x, y)
        self.assertGreater(again.charted, 0)

    def test_a_pilot_from_before_the_chart_has_charted_nothing_yet(self):
        game = self.game()
        game.save()
        game.close()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        career = json.loads(db.execute("SELECT career FROM pilots").fetchone()[0])
        del career["charted"]
        db.execute("UPDATE pilots SET career=?", (json.dumps(career),))
        db.commit()
        db.close()
        again = self.game()
        self.assertEqual((again.saved_ok, again.charted), (True, 0))

    def test_a_commit_that_fails_charts_nothing(self):
        game = self.game()
        store = game.store
        with self.assertRaises(KeyError):
            store.commit(12, "Mara", {}, {"turn": 1}, "hold", [5000, 5001])       # no day: it fails after the cells
        self.assertEqual(store.chart()[0], set())
        self.assertFalse(store.db.in_transaction)

    def test_a_turn_that_is_not_written_earns_nothing_and_charts_nothing(self):
        game = self.game()
        feature = next(f for f in game.world.features
                       if 2 <= len(f["cells"]) <= 40 and not any(c in game.known for c in f["cells"]))
        before = (game.charted, game.postmarks, game.points, set(game.chart), dict(game.found), list(game.log))
        real = game.store.db

        class Failing:
            """The database, with a COMMIT that fails: a full disk at the worst moment."""
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("database or disk is full")
                return self.db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.db, name)
        game.store.db = Failing(real)
        game.save("hold", feature["cells"])
        game.store.db = real
        self.assertFalse(game.saved_ok)
        self.assertEqual((game.charted, game.postmarks, game.points, game.chart, game.found, game.log), before,
                         "nothing the turn would have earned stands")
        self.assertEqual(game.naming, [], "and no name is offered for a feature the records do not have")
        self.assertEqual(real.execute("SELECT count(*) FROM charted").fetchone()[0], 0)
        self.assertFalse(real.in_transaction)
        other = self.other(2)
        other.save("hold", feature["cells"])
        self.assertTrue(other.naming, "so the next pilot over it is first, once")
        game.save("hold", feature["cells"])
        self.assertTrue(game.saved_ok)
        self.assertEqual(game.charted, 0, "and this one, coming back, is second")

    def test_a_real_world_at_schema_one_keeps_its_pilots(self):
        seed = fw.world_seed("relink-test")
        path = self.folder / fw.db_name(seed)
        db = sqlite3.connect(str(path))
        for statement in fw.MIGRATIONS[1]:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        probe = fw.PlainFlight(launch(user_id=2 ** 62 + 9, timezone="UTC", node_id="relink-test"), None, now=self.clock)
        probe.key("ENTER")
        probe.key("ENTER")
        career = {k: v for k, v in probe._career().items() if k != "charted"}
        db.execute("INSERT INTO pilots VALUES (12, 'Mara', 1.0, ?)", (json.dumps(career),))
        db.execute("INSERT INTO flights VALUES (12, 2, ?)", (json.dumps(probe.state.to_dict()),))
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.assertEqual(game.state.turn, 2)
        self.assertEqual((game.charted, game.chart), (0, set()), "nothing flown before the chart is charted after the fact")
        game.key("ENTER")
        self.assertEqual(game.state.turn, 3)
        db = sqlite3.connect(str(path))
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        db.close()

    def test_who_charted_most(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        a.save("hold", [5000, 5001, 5002])
        b.save("hold", [5002, 5003])
        self.assertEqual(a.store.charters(), [("Ada", 3), ("Bo", 1)])


class Atlas(ChartCase):
    def test_c_opens_the_chart_and_any_key_closes_it(self):
        game = self.game()
        game.key("c")
        self.assertTrue(game.atlas)
        text = self.screen(game)
        self.assertIn("The chart", text)
        self.assertIn("of the land charted", text)
        self.assertIn("You were first over 0 cells of land.", text)
        self.assertIn(f"0 of {len(game.world.features)} features are found and named.", text)
        turn = game.state.turn
        game.key("ENTER")
        self.assertFalse(game.atlas)
        self.assertEqual(game.state.turn, turn, "the key that closes it flies nothing")
        game.key("ENTER")
        game.key("c")
        self.assertTrue(game.atlas, "aloft too")

    def test_the_chart_fits_every_layout_and_reading(self):
        game = self.game()
        game.save("hold", sorted(game.world._land - game.known)[:300])
        while game.naming:
            game.key("ENTER")                            # names first: they are owed before anything else
        game.key("c")
        for size in ((80, 24), (100, 30), (132, 40), (168, 46), (250, 70), (80, 60)):
            text = self.screen(game, *size)
            self.assertIn("Most cells charted: Mara", text)
            self.assertIn("@", text)
        for changes in ({"color_depth": "16"}, {"charset": "cp437", "color_depth": "16"}, {"charset": "ascii"},
                        {"color_depth": "256"}):
            other = self.game(user_id=600 + len(changes) + len(str(changes)), **changes)
            other.key("c")
            self.assertIn("of the land charted", self.screen(other))

    def test_what_is_charted_is_in_colour_and_the_rest_is_sketched(self):
        game = self.game()
        session = fw.Session(dataclasses.replace(game.launch, width=132, height=40), None, type("O", (), {"write": lambda s, t: None})(),
                             game=game)
        game.key("c")
        session.paint()
        frame = session.frame
        inks = [c for k, c in enumerate(frame.fg) if frame.split[k]] + [c for k, c in enumerate(frame.bg) if frame.split[k]]
        vivid = [c for c in inks if max(c) - min(c) > 60]
        dull = [c for c in inks if max(c) - min(c) <= 60]
        self.assertTrue(vivid, "the ground around the places, in colour")
        self.assertGreater(len(dull), len(vivid) * 3, "and most of the continent still a sketch")

    def test_a_handle_on_the_board_cannot_carry_a_mark_or_a_control_code(self):
        game = self.game(handle="A`b\x1b[2Jc")
        game.save("hold", [5000])
        game.key("c")
        text = self.screen(game)
        self.assertNotIn("`", text)
        self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main()
