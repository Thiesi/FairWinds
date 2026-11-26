"""Stranding, rescue and the visitors' books (S16)."""

import dataclasses
import os
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
from test_stations import StationCase                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class RescueCase(StationCase):
    def strand(self, game, x=None, y=None, **changes):
        """Down in the wild and unable to fly, with the call for help out."""
        if x is None:
            x, y = self.spot(game)
        changes.setdefault("fuel", 0)
        game.state = dataclasses.replace(game.state, x=x, y=y, layer=0, status="stranded", turn=30, **changes)
        game._sync_call()
        return x, y

    def arrive(self, game, x, y, **changes):
        self.down(game, x, y, **changes)
        game._rescue()


class Stranded(RescueCase):
    def test_a_stranded_pilot_is_on_every_map(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        x, y = self.strand(a)
        self.assertTrue(any("Your call for 15 fuel is on every pilot's map" in line for line in a.heard))
        self.assertEqual(self.rows("SELECT user_id, handle, fuel, repair, by_user FROM calls"), [(1, "Ada", 15, 0, None)])
        self.assertEqual(a.calls, [], "a pilot's own call is not one to answer")
        self.assertEqual(b.calls, [])
        self.assertTrue(b._day_may_have_opened(), "the screen has something new to show")
        self.assertEqual([(c["handle"], c["fuel"]) for c in b.calls], [("Ada", 15)])
        self.down(b, x - 40.0, y)
        text = self.screen(b, 132, 40)
        self.assertIn("!", text)
        self.assertIn("Ada: 15 fuel", text)
        b.key("c")
        self.assertIn("!", self.screen(b))
        b.close()

    def test_flying_the_tank_dry_in_the_wild_puts_the_call_out(self):
        game = self.game()
        x, y = self.spot(game)
        game.state = dataclasses.replace(game.state, x=x, y=y, status="aloft", fuel=0, turn=20,
                                         layer=game.engine.lowest_open(x, y))
        game._choose_default()
        game.key("l")
        game.key("ENTER")
        self.assertEqual(game.phase(), "stranded")
        self.assertTrue(game._called)
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(1,)])
        text = self.screen(game)
        self.assertIn("or wait for help", text)
        self.assertIn("[W] Walk out", text)

    def test_too_worn_to_fly_asks_for_patches(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        x, y = self.strand(a, fuel=30, wear=fw.WEAR_GROUNDED)
        self.assertEqual(self.rows("SELECT fuel, repair FROM calls"), [(0, 1)])
        self.assertEqual(fw.need_text(0, 1), "patches")
        self.assertEqual(fw.need_text(15, 1), "15 fuel and patches")
        fuel = b.state.fuel
        self.arrive(b, x, y)
        self.assertEqual(b.state.fuel, fuel, "patches cost the rescuer no fuel")
        a._day_may_have_opened()
        self.assertEqual((a.state.wear, a.state.fuel, a.phase()), (fw.RELIEF["wear"], 30, "ground"))
        b.close()

    def test_walking_out_costs_two_days_of_the_calendar_and_takes_the_call_down(self):
        a = self.game(user_id=1)
        b = self.other(2)
        self.strand(a)
        day = a.state.day
        a.key("ENTER")
        a.key(" ")
        self.assertEqual(a.state.day, day, "only W walks out")
        a.key("w")
        self.assertEqual((a.state.day, a.state.status), (day + 2, "ground"))
        self.assertTrue(a.waiting(), "those days have not opened yet")
        self.assertEqual(a.phase(), "wait")
        self.assertIn(f"Day {day + 2} opens", a.briefing())
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(0,)])
        b._day_may_have_opened()
        self.assertEqual(b.calls, [])
        self.days(2, a)
        self.assertFalse(a.waiting())
        b.close()


class Rescue(RescueCase):
    def test_a_rescue_rewards_both_and_only_once(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        c = self.other(3, handle="Cy")
        x, y = self.strand(a)
        for game in (b, c):
            game._day_may_have_opened()
        was = (b.state.fuel, b.postmarks, b.points)
        self.arrive(b, x + 5.0, y)
        self.assertEqual((b.state.fuel, b.postmarks, b.points),
                         (was[0] - fw.RESCUE_FUEL, was[1] + fw.RESCUE_PAY, was[2] + fw.RESCUE_POINTS))
        self.assertTrue(any("You reach Ada with 15 fuel" in line for line in b.heard), b.heard)
        saved = support.stored(self.folder, 2)
        self.assertEqual((saved["state"]["fuel"], saved["postmarks"]), (b.state.fuel, b.postmarks))
        self.assertEqual(self.rows("SELECT by_user, by_handle FROM calls"), [(2, "Bo")])
        self.assertEqual(b.calls, [], "answered: nobody else need come")
        paid = c.postmarks
        self.arrive(c, x, y)
        self.assertEqual(c.postmarks, paid)
        self.assertEqual(a.state.fuel, 0, "the stranded pilot has not looked yet")
        self.assertTrue(a._day_may_have_opened())
        self.assertEqual((a.state.fuel, a.state.status, a.phase()), (fw.RESCUE_FUEL, "ground", "ground"))
        self.assertTrue(any("Bo reached you with 15 fuel" in line for line in a.heard), a.heard)
        self.assertEqual(support.stored(self.folder, 1)["state"]["fuel"], fw.RESCUE_FUEL)
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(0,)])
        a._day_may_have_opened()
        self.assertEqual(a.state.fuel, fw.RESCUE_FUEL, "and what was brought is taken once")
        self.assertIn("launch", a.offered())
        for game in (b, c):
            game.close()

    def test_the_second_of_two_rescuers_is_told_so(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2)
        c = self.other(3)
        x, y = self.strand(a)
        for game in (b, c):
            game._day_may_have_opened()
            self.down(game, x, y)
        b._rescue()
        real = c._see_calls
        c._see_calls = lambda: False                      # c lands in the same instant: it has not heard
        was = (c.state.fuel, c.postmarks)
        c._rescue()
        c._see_calls = real
        self.assertEqual((c.state.fuel, c.postmarks), was)
        self.assertTrue(any("Somebody reached Ada before you" in line for line in c.heard))
        for game in (b, c):
            game.close()

    WORKER = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3])\n"
              "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(user, 'ssh', force=True)\n"
              "got = []\n"
              "s.commit(user, 'P%d' % user, lambda new, found, done: (got.append(done), {})[1], {'day': 1, 'turn': 1},\n"
              "         'rescue', deed=lambda db: fw.answer_call(db, 1, user, 'P%d' % user))\n"
              "print(0 if got[0] is None else 1)\n"
              "s.release(user); s.close()\n")

    def test_eight_processes_reaching_one_pilot_at_once(self):
        game = self.game(user_id=1)
        self.strand(game)
        seed = game.world.seed
        game.close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), str(seed)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for user in range(101, 109)]
        answered = 0
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            answered += int(out.decode().strip())
        self.assertEqual(answered, 1, "one of the eight is the rescuer")
        self.assertEqual(self.rows("SELECT count(*), count(by_user) FROM calls"), [(1, 1)])

    def test_a_rescuer_with_no_fuel_to_spare_cannot_help(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2)
        x, y = self.strand(a)
        self.arrive(b, x, y, fuel=fw.RESCUE_FUEL + fw.COST_LAUNCH - 1)
        self.assertEqual(b.state.fuel, fw.RESCUE_FUEL + fw.COST_LAUNCH - 1)
        self.assertTrue(any("more than you can spare" in line for line in b.heard))
        self.assertEqual(self.rows("SELECT by_user FROM calls"), [(None,)])
        self.arrive(b, x, y, fuel=fw.RESCUE_FUEL + fw.COST_LAUNCH)
        self.assertEqual(b.state.fuel, fw.COST_LAUNCH, "enough left to launch again")
        b.close()

    def test_too_far_off_is_no_rescue(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y = self.strand(a)
        was = b.postmarks
        self.arrive(b, x + fw.PLACE_RADIUS_KM + 1.0, y)
        self.assertEqual(b.postmarks, was)
        b.close()

    def test_help_that_came_while_the_pilot_made_up_their_mind_is_not_walked_away_from(self):
        a = self.game(user_id=1)
        b = self.other(2, handle="Bo")
        x, y = self.strand(a)
        self.arrive(b, x, y)
        day = a.state.day
        a.key("w")
        self.assertEqual((a.state.day, a.state.x, a.state.fuel), (day, x, fw.RESCUE_FUEL))
        b.close()

    def test_a_rescuer_in_the_instant_of_walking_out_is_not_paid_for_nothing(self):
        a = self.game(user_id=1)
        b = self.other(2, handle="Bo")
        x, y = self.strand(a)
        b._day_may_have_opened()
        self.down(b, x, y)
        real = fw.end_call
        day = a.state.day

        def late(db, user_id):
            # Bo's write lands after Ada pressed W and before her walk is written.
            fw.answer_call(db, 1, 2, "Bo")
            return real(db, user_id)
        fw.end_call = late
        try:
            a.key("w")
        finally:
            fw.end_call = real
        self.assertEqual((a.state.day, a.state.x, a.state.fuel, a.state.status), (day, x, fw.RESCUE_FUEL, "ground"))
        self.assertTrue(any("Bo reached you" in line for line in a.heard))
        self.assertEqual(support.stored(self.folder, 1)["state"]["fuel"], fw.RESCUE_FUEL)
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(0,)])
        b.close()

    def test_a_call_that_now_wants_more_than_can_be_spared_is_left(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y = self.strand(a, fuel=10)                    # wants 5
        b._day_may_have_opened()
        self.down(b, x, y, fuel=8)                        # can spare 6
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.execute("UPDATE calls SET fuel=15")            # stranded again since, and dry this time
        db.commit()
        db.close()
        real = b._see_calls
        b._see_calls = lambda: False
        b._rescue()
        b._see_calls = real
        self.assertEqual(b.state.fuel, 8)
        self.assertEqual(self.rows("SELECT by_user FROM calls"), [(None,)])
        b.close()

    def test_a_walk_that_cannot_be_written_did_not_happen(self):
        a = self.game(user_id=1)
        x, y = self.strand(a)
        was = (a.state, list(a.manifest), a.days_flown)
        real = a.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        a.store.commit = broken
        a.key("w")
        a.store.commit = real
        self.assertEqual((a.state, a.manifest, a.days_flown), was)
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(1,)])

    def test_rescued_while_away(self):
        a = self.game(user_id=1, handle="Ada")
        x, y = self.strand(a)
        b = self.other(2, handle="Bo")
        self.arrive(b, x, y)
        b.close()
        again = self.game(user_id=1, handle="Ada")
        self.assertEqual((again.state.fuel, again.state.status), (fw.RESCUE_FUEL, "ground"))
        self.assertTrue(any("Bo reached you" in line for line in again.log), again.log)
        self.assertIn("Bo reached you", self.screen(again))

    def test_a_rescue_that_cannot_be_written_did_not_happen(self):
        a = self.game(user_id=1)
        b = self.other(2)
        x, y = self.strand(a)
        b._day_may_have_opened()
        self.down(b, x, y)
        was, real = (b.state.fuel, b.postmarks, b.points), b.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        b.store.commit = broken
        b._rescue()
        b.store.commit = real
        self.assertEqual((b.state.fuel, b.postmarks, b.points), was)
        self.assertEqual(self.rows("SELECT by_user FROM calls"), [(None,)])
        b.close()

    def test_a_visitor_strands_and_walks_out_alone(self):
        game = fw.PlainFlight(launch(user_id=5, timezone="UTC"), None, now=self.clock)
        self.strand(game)
        self.assertTrue(game._called)
        game.key("w")
        self.assertFalse(game._called)
        self.assertEqual(game.state.status, "ground")
        game.close()


class Book(RescueCase):
    def at_station(self, game, index=0):
        place = game.stations[index]
        self.down(game, place["x"], place["y"])
        return place

    def write(self, game, text):
        game.key("b")
        game.key("w")
        for ch in text:
            game.key(ch)
        game.key("ENTER")

    def test_a_line_is_written_and_read_by_everyone(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        place = self.at_station(a)
        self.assertIn("[B] Book", self.screen(a))
        a.key("b")
        text = self.screen(a)
        self.assertIn(f"The visitors' book at {place['name']}", text)
        self.assertIn("Nobody has signed it yet", text)
        a.key("w")
        for ch in "Quiet here? Q and ? are letters":
            self.assertIsNone(a.key(ch), "no key leaves or opens the help while a line is written")
        self.assertIn("> Quiet here? Q and ? are letters_", self.screen(a))
        a.key("BACKSPACE")
        a.key("ENTER")
        self.assertIsNone(a.writing)
        self.assertEqual(a.book["lines"], [(a.today, "Ada", "Quiet here? Q and ? are letter")])
        self.assertIn("Signed.", self.screen(a))
        a.key("ENTER")
        self.assertIsNone(a.book)
        self.at_station(b)
        b.key("b")
        self.assertIn("Ada: Quiet here? Q and ? are letter", self.screen(b))
        b.close()

    def test_one_line_a_station_a_world_day(self):
        game = self.game()
        self.at_station(game)
        self.write(game, "first")
        game.key("w")
        for ch in "second":
            game.key(ch)
        game.key("ENTER")
        self.assertIn("signed this book today", game.book["note"])
        self.assertEqual(len(game.book["lines"]), 1)
        game.key("ENTER")
        self.at_station(game, 1)
        self.write(game, "another station")
        self.assertEqual(game.book["note"], "Signed.")
        game.key("ENTER")
        self.days(1, game)
        self.at_station(game)
        self.write(game, "next day")
        self.assertEqual([line[2] for line in game.book["lines"]], ["next day", "first"])

    def test_a_line_is_bounded_and_plain(self):
        game = self.game()
        self.at_station(game)
        game.key("b")
        game.key("w")
        for ch in "x" * 80:
            game.key(ch)
        self.assertEqual(len(game.writing), fw.GUESTBOOK_LIMIT)
        for key in ("\x07", "UP", "F1", "\x1b"):
            game.key(key)
        self.assertEqual(game.writing, "x" * fw.GUESTBOOK_LIMIT, "nothing that is not printable is taken")
        game.key("ESC")
        self.assertIsNone(game.writing)
        self.assertEqual(game.book["lines"], [])
        game._sign("  red\x1b[31m  alert\x07\r\n" + "y" * 100)
        stored = self.rows("SELECT text FROM guestbook")[0][0]
        self.assertTrue(stored.startswith("red[31m alert"))
        self.assertTrue(stored.isprintable())
        self.assertEqual(len(stored), fw.GUESTBOOK_LIMIT)

    def test_an_empty_line_is_not_signed(self):
        game = self.game()
        self.at_station(game)
        self.write(game, "   ")
        self.assertEqual(self.rows("SELECT count(*) FROM guestbook"), [(0,)])
        self.assertEqual(game.book["note"], "Nothing written.")

    def test_a_line_keeps_to_one_cell_a_character(self):
        self.assertEqual(fw.book_text("Gr\u00fc\u00dfe aus K\u00f6ln"), "Gr\u00fc\u00dfe aus K\u00f6ln", "letters of a language stay")
        self.assertEqual(fw.book_text("z\u0301\u0302\u0303a\u0336lgo"), "zalgo", "marks stacked on a letter go")
        self.assertEqual(fw.book_text("wide \u6f22\u5b57 \uff37 here"), "wide here", "and what is two cells wide")
        self.assertEqual(fw.book_text("x" * 200), "x" * fw.GUESTBOOK_LIMIT)
        game = self.game()
        self.at_station(game)
        game.key("b")
        game.key("w")
        for key in ("a", "\u0301", "\u6f22", " ", "\u00fc"):
            game.key(key)
        self.assertEqual(game.writing, "a \u00fc")

    def test_nothing_is_signed_from_afar(self):
        game = self.game()
        self.at_station(game)
        game.key("b")
        game.state = dataclasses.replace(game.state, x=game.state.x + 100.0)
        game._sign("left behind")
        self.assertEqual(self.rows("SELECT count(*) FROM guestbook"), [(0,)])

    def test_what_is_in_the_records_is_cleaned_again_when_shown(self):
        game = self.game()
        self.at_station(game)
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.execute("INSERT INTO guestbook (station, user_id, handle, day, text) VALUES (0, 9, ?, 1, ?)",
                   ("Mal\x1b[2Jory", "wipe\x1b[2J\x1b[H" + "z" * 300))
        db.commit()
        db.close()
        game.key("b")
        text = self.screen(game)
        self.assertIn("Mal[2Jory: wipe[2J[H", text)
        self.assertLessEqual(max(len(line) for line in text.split("\n")), 80)

    def test_canned_lines_only(self):
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC"), self.folder, {"DOOR_FW_GUESTBOOK": "canned"},
                              now=self.clock)
        self.assertEqual(game.book_mode, "canned")
        self.at_station(game)
        game.key("b")
        text = self.screen(game)
        for i, phrase in enumerate(fw.CANNED):
            self.assertIn(f"{i + 1}  {phrase}", text)
        game.key("w")
        self.assertIsNone(game.writing, "nothing is typed in this mode")
        game.key("3")
        self.assertEqual(game.book["lines"], [(game.today, game.launch.handle, fw.CANNED[2])])
        self.assertTrue(all(len(p) <= fw.GUESTBOOK_LIMIT and p.isprintable() for p in fw.CANNED))
        game.close()

    def test_off_means_no_book(self):
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC"), self.folder, {"DOOR_FW_GUESTBOOK": " OFF "},
                              now=self.clock)
        self.at_station(game)
        self.assertIsNone(game.book_place())
        game.key("b")
        self.assertIsNone(game.book)
        self.assertNotIn("Book", self.screen(game))
        game.close()

    def test_on_unless_told_otherwise(self):
        self.assertEqual(self.game().book_mode, "on")
        odd = fw.PlainFlight(launch(user_id=13, timezone="UTC"), self.folder, {"DOOR_FW_GUESTBOOK": "maybe"},
                             now=self.clock)
        self.assertEqual(odd.book_mode, "on")
        odd.close()

    def test_every_station_and_mast_has_one_and_a_town_none(self):
        game = self.game()
        self.assertIsNone(game.book_place(), "the start town keeps no book")
        game.key("b")
        self.assertIsNone(game.book)
        station = self.plant(game)
        self.assertEqual(game.book_place(), 3)
        self.write(game, "planted today")
        game.key("ENTER")
        self.raise_mast(game, station)
        self.assertEqual(game.book_place(), 3)
        game.key("b")
        self.assertIn("planted today", self.screen(game))
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, status="aloft", layer=1)
        self.assertIsNone(game.book_place(), "and only from the ground")

    def test_leaving_the_place_closes_the_book(self):
        game = self.game()
        self.at_station(game)
        game.key("b")
        game.key("w")
        game.state = dataclasses.replace(game.state, x=game.state.x + 100.0)
        self.screen(game)
        self.assertEqual((game.book, game.writing), (None, None))


class SysOp(RescueCase):
    def lines(self):
        game = self.game(handle="Ada")
        place = game.stations[0]
        self.down(game, place["x"], place["y"])
        game.key("b")
        game._sign("first line")
        game.key("ENTER")
        self.days(1, game)
        game.key("b")
        game._sign("second line")
        game.close()
        return fw.guestbook_lines(self.folder)

    def test_the_lines_are_listed_and_one_is_removed(self):
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        name = lines[0].split()[0]
        self.assertRegex(name, r"^[0-9a-f]{16}:\d+$")
        self.assertIn("station 0", lines[0])
        self.assertIn("Ada: first line", lines[0])
        self.assertEqual(fw.guestbook_remove(self.folder, name), f"removed {name}")
        left = fw.guestbook_lines(self.folder)
        self.assertEqual(len(left), 1)
        self.assertIn("second line", left[0])
        for bad in (name, "nonsense", "0123456789abcdef:1", name.split(":")[0] + ":x", name.split(":")[0] + ":999"):
            with self.assertRaises(ValueError):
                fw.guestbook_remove(self.folder, bad)

    def test_from_the_command_line(self):
        self.lines()
        env = dict(os.environ, DOOR_FW_DATA_DIR=str(self.folder))
        run = lambda *args: subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), *args], env=env,   # noqa: E731
                                           capture_output=True, text=True, timeout=60)
        listed = run("--guestbook")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(listed.stdout.strip().split("\n")), 2)
        name = listed.stdout.split()[0]
        gone = run("--guestbook-remove", name)
        self.assertEqual((gone.returncode, gone.stdout.strip()), (0, f"removed {name}"))
        again = run("--guestbook-remove", name)
        self.assertEqual(again.returncode, 1)
        self.assertIn("no line", again.stderr)
        self.assertEqual(len(run("--guestbook").stdout.strip().split("\n")), 1)

    def test_no_books_no_lines(self):
        self.assertEqual(fw.guestbook_lines(self.folder), [])

    def test_a_world_from_before_is_brought_forward(self):
        seed = fw.world_seed("relink-test")
        db = sqlite3.connect(str(self.folder / fw.db_name(seed)))
        for step in (1, 2, 3):
            for statement in fw.MIGRATIONS[step]:
                db.execute(statement)
        db.execute(f"PRAGMA application_id={fw.APPLICATION_ID}")
        db.execute("PRAGMA user_version=3")
        db.executemany("INSERT INTO meta VALUES (?, ?)", (("seed", str(seed)), ("node", "relink-test"), ("created", "1")))
        db.commit()
        db.close()
        game = fw.PlainFlight(launch(user_id=12, timezone="UTC", node_id="relink-test"), self.folder, now=self.clock)
        self.assertTrue(game.saved_ok)
        self.strand(game)
        self.assertEqual(self.rows("PRAGMA user_version"), [(fw.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT count(*) FROM calls"), [(1,)])


if __name__ == "__main__":
    unittest.main()
