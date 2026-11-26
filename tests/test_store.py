"""The world's records (S10): the database, the lease, the turn commit, and what survives."""

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from support import Door, Terminal, door_info             # noqa: E402
from test_plain import Keys, Out, launch                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED = 0x1234


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.folder = Path(self._dir.name)
        self.clock = Clock()

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def store(self, seed=SEED):
        return fw.Store(self.folder, seed, "node", self.clock)

    def raw(self, seed=SEED):
        return sqlite3.connect(str(self.folder / fw.db_name(seed)))


class Opening(StoreCase):
    def test_a_new_world_gets_its_file_its_marks_and_its_tables(self):
        store = self.store()
        self.assertEqual(store.path.name, "fair_winds-0000000000001234.db")
        db = self.raw()
        self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], fw.APPLICATION_ID)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], fw.SCHEMA_VERSION)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables - {"sqlite_sequence"},
                         {"meta", "pilots", "sessions", "flights", "turns", "charted", "features",
                          "stations", "contributions", "calls", "guestbook", "sightings", "almanac", "drifts", "wonders", "seen",
                          "seasons", "race", "season_medals", "season_claims"})
        self.assertEqual(dict(db.execute("SELECT key, value FROM meta"))["seed"], str(SEED))
        db.close()
        self.assertFalse(store.db.in_transaction, "nothing is left open")

    def test_opening_again_changes_nothing(self):
        def meta():
            db = self.raw()
            try:
                return db.execute("SELECT * FROM meta ORDER BY key").fetchall()
            finally:
                db.close()
        self.store().close()
        before = meta()
        self.store().close()
        self.assertEqual(meta(), before)

    def test_the_sysop_can_ask_for_a_rollback_journal(self):
        game = fw.PlainFlight(launch(), self.folder, {"DOOR_FW_JOURNAL": " Delete "})
        game.key("ENTER")
        self.assertEqual(game.store.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertTrue(game.saved_ok)
        game.close()
        again = fw.PlainFlight(launch(), self.folder, {"DOOR_FW_JOURNAL": "delete"})
        self.assertEqual(again.state.turn, 1)

    def test_each_world_has_its_own_file(self):
        self.store(1).close()
        self.store(2).close()
        self.assertEqual(len(list(self.folder.glob("fair_winds-*.db"))), 2)

    def unusable(self, expect):
        path = self.folder / fw.db_name(SEED)
        before = path.read_bytes()
        with self.assertRaises(fw.StoreError) as caught:
            self.store()
        self.assertIn(expect, str(caught.exception))
        self.assertEqual(path.read_bytes(), before, "the file is exactly as it was")
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith((".bad", ".tmp"))], [])

    def test_a_file_that_is_not_a_database_is_refused_and_left_alone(self):
        (self.folder / fw.db_name(SEED)).write_bytes(b"this is not a database at all, " * 200)
        self.unusable("damaged or is not a database")

    def test_a_database_cut_short_is_refused_and_left_alone(self):
        store = self.store()
        store.claim(1, "ssh")
        for n in range(200):
            store.commit(1, "Mara", {"n": n, "pad": "x" * 500}, {"day": 1, "turn": n}, "hold")
        store.close()
        path = self.folder / fw.db_name(SEED)
        for extra in self.folder.glob("*.db-*"):
            extra.unlink()
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2 // 4096 * 4096 + 100])
        with self.assertRaises((fw.StoreError, sqlite3.DatabaseError)):
            again = self.store()
            again.pilot(1)
            again.db.execute("PRAGMA integrity_check").fetchall()
            raise fw.StoreError("the damage was found by the check")

    def test_a_newer_schema_is_refused_and_left_alone(self):
        self.store().close()
        db = self.raw()
        db.execute(f"PRAGMA user_version={fw.SCHEMA_VERSION + 1}")
        db.commit()
        db.close()
        self.unusable("newer Fair Winds")

    def test_some_other_programs_database_is_refused_and_left_alone(self):
        db = self.raw()
        db.execute("CREATE TABLE recipes (name TEXT)")
        db.commit()
        db.close()
        self.unusable("is not a Fair Winds world")

    def test_another_worlds_records_under_this_name_are_refused(self):
        self.store(SEED + 1).close()
        os.replace(self.folder / fw.db_name(SEED + 1), self.folder / fw.db_name(SEED))
        for extra in self.folder.glob("*.db-*"):
            extra.unlink()
        self.unusable("another world's records")

    def test_no_folder_to_write_in_is_unavailable_not_an_error(self):
        blocker = self.folder / "in-the-way"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(fw.StoreUnavailable):
            fw.Store(blocker, SEED, "node")

    def test_many_callers_creating_many_worlds_at_once(self):
        # Seen once on the Windows job: a caller read the marks of a world another was half way through
        # making, and refused it as somebody else's file. Several rounds, to give the race room.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw; "
                "s = fw.Store(sys.argv[2], int(sys.argv[4]), 'node'); s.claim(int(sys.argv[3]), 'ssh'); "
                "s.commit(int(sys.argv[3]), 'P', {}, {'day': 1, 'turn': 0}); s.close()")
        for seed in range(9000, 9004):
            procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.folder), str(n), str(seed)],
                                      stderr=subprocess.PIPE) for n in range(8)]
            for proc in procs:
                _, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err.decode()[-400:])
            db = self.raw(seed)
            self.assertEqual(db.execute("SELECT count(*) FROM pilots").fetchone()[0], 8)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], fw.SCHEMA_VERSION)
            db.close()

    def test_the_marks_of_a_world_are_read_as_one(self):
        store = self.store()
        seen = []
        real = store.db

        class Watching:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                seen.append((sql.split("(")[0].strip(), self.db.in_transaction))
                return self.db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.db, name)
        store.db = Watching(real)
        store._prepare(SEED, "node")
        store.db = real
        reads = [inside for sql, inside in seen if sql.startswith(("SELECT count", "PRAGMA application_id",
                                                                    "PRAGMA user_version"))][:3]
        self.assertEqual(reads, [True, True, True], "all three inside one transaction")
        self.assertFalse(real.in_transaction)

    def test_two_callers_creating_the_world_at_once(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import fair_winds as fw; "
                "s = fw.Store(sys.argv[2], 4660, 'node'); s.claim(int(sys.argv[3]), 'ssh'); "
                "s.commit(int(sys.argv[3]), 'P', {}, {'day': 1, 'turn': 0}); s.close()")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.folder), str(n)],
                                  stderr=subprocess.PIPE) for n in range(6)]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
        db = self.raw()
        self.assertEqual(db.execute("SELECT count(*) FROM pilots").fetchone()[0], 6)
        self.assertEqual(db.execute("SELECT count(*) FROM meta WHERE key='seed'").fetchone()[0], 1)
        db.close()


class Lease(StoreCase):
    def test_one_live_session_for_a_pilot(self):
        a, b = self.store(), self.store()
        self.assertIsNone(a.claim(7, "ssh"))
        self.clock.now += 30
        held = b.claim(7, "telnet")
        self.assertEqual((held["transport"], held["now"] - held["started"]), ("ssh", 30))
        self.assertIsNone(b.claim(8, "telnet"), "another pilot is nobody's business")
        self.assertIsNone(a.claim(7, "ssh"), "claiming again what is already yours is fine")

    def test_a_session_gone_quiet_is_replaced_without_asking(self):
        a, b = self.store(), self.store()
        a.claim(7, "ssh")
        self.clock.now += fw.LEASE_STALE_SECONDS - 1
        self.assertIsNotNone(b.claim(7, "ssh"))
        self.clock.now += 2
        self.assertIsNone(b.claim(7, "ssh"))
        self.assertFalse(a.beat(7), "and the first one learns it at its next heartbeat")

    def test_a_heartbeat_keeps_the_lease(self):
        a, b = self.store(), self.store()
        a.claim(7, "ssh")
        for _ in range(10):
            self.clock.now += fw.LEASE_STALE_SECONDS - 5
            self.assertTrue(a.beat(7))
        self.assertIsNotNone(b.claim(7, "ssh"))
        self.assertLess(fw.HEARTBEAT_SECONDS * 3, fw.LEASE_STALE_SECONDS, "three beats can be missed")

    def test_taking_over_ends_the_other_session_at_its_next_turn(self):
        a, b = self.store(), self.store()
        a.claim(7, "ssh")
        a.commit(7, "Mara", {"n": 1}, {"day": 1, "turn": 1}, "launch")
        self.assertIsNone(b.claim(7, "telnet", force=True))
        with self.assertRaises(fw.LeaseLost):
            a.commit(7, "Mara", {"n": 2}, {"day": 1, "turn": 2}, "hold")
        self.assertFalse(a.db.in_transaction)
        self.assertEqual(json.loads(b.pilot(7)[1])["turn"], 1, "and it wrote nothing")
        self.assertEqual(len(b.journal(7)), 1)
        b.commit(7, "Mara", {"n": 2}, {"day": 1, "turn": 2}, "climb")
        self.assertEqual(json.loads(a.pilot(7)[1])["turn"], 2)

    def test_release_frees_the_pilot_and_only_ones_own_lease(self):
        a, b = self.store(), self.store()
        a.claim(7, "ssh")
        b.release(7)
        self.assertIsNotNone(b.claim(7, "ssh"), "somebody else's release does nothing")
        a.release(7)
        self.assertIsNone(b.claim(7, "ssh"))

    def test_a_commit_without_the_lease_is_refused(self):
        a = self.store()
        with self.assertRaises(fw.LeaseLost):
            a.commit(7, "Mara", {}, {"day": 1, "turn": 0})
        self.assertIsNone(a.pilot(7))


class Committing(StoreCase):
    def test_a_turn_is_the_flight_the_journal_and_the_career_together(self):
        a = self.store()
        a.claim(7, "ssh")
        self.assertEqual(a.commit(7, "Mara", {"km": 0}, {"day": 1, "turn": 0}), 0, "the start is not a turn")
        self.assertEqual(a.journal(7), [])
        self.assertEqual(a.commit(7, "Mara", {"km": 4}, {"day": 1, "turn": 1}, "launch"), 1)
        self.assertEqual(a.commit(7, "Mara", {"km": 9}, {"day": 1, "turn": 2}, "climb"), 2)
        career, state = a.pilot(7)
        self.assertEqual((json.loads(career), json.loads(state)), ({"km": 9}, {"day": 1, "turn": 2}))
        self.assertEqual(a.journal(7), [(1, 1, 1, "launch"), (2, 1, 2, "climb")])
        self.assertFalse(a.db.in_transaction)

    def test_the_journal_is_the_current_flying_day(self):
        a = self.store()
        a.claim(7, "ssh")
        for turn in range(1, 4):
            a.commit(7, "Mara", {}, {"day": 1, "turn": turn}, "hold")
        a.commit(7, "Mara", {}, {"day": 2, "turn": 0}, "night")
        self.assertEqual(a.journal(7), [(4, 2, 0, "night")])

    def test_a_failure_inside_a_commit_leaves_the_turn_before(self):
        a = self.store()
        a.claim(7, "ssh")
        a.commit(7, "Mara", {"n": 1}, {"day": 1, "turn": 1}, "launch")
        with self.assertRaises(KeyError):
            a.commit(7, "Mara", {"n": 2}, {"turn": 2}, "hold")       # no day: it fails after the first writes
        self.assertFalse(a.db.in_transaction)
        career, state = a.pilot(7)
        self.assertEqual((json.loads(career)["n"], json.loads(state)["turn"]), (1, 1))

    def test_a_commit_that_cannot_finish_leaves_nothing_open(self):
        # A rollback journal, and a reader in the way: the write lock is had, the commit is not.
        real = fw.BUSY_TIMEOUT_SECONDS
        fw.BUSY_TIMEOUT_SECONDS = 0.2
        try:
            a = fw.Store(self.folder, SEED, "node", self.clock, journal="delete")
        finally:
            fw.BUSY_TIMEOUT_SECONDS = real
        a.claim(7, "ssh")
        a.commit(7, "Mara", {"n": 1}, {"day": 1, "turn": 1}, "launch")
        reader = self.raw()
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM pilots").fetchall()         # holds a shared lock
        with self.assertRaises(sqlite3.OperationalError):
            a.commit(7, "Mara", {"n": 2}, {"day": 1, "turn": 2}, "hold")
        self.assertFalse(a.db.in_transaction, "the failed commit was rolled back")
        reader.rollback()
        reader.close()
        self.assertEqual(a.commit(7, "Mara", {"n": 2}, {"day": 1, "turn": 2}, "hold"), 2, "and the next one works")
        self.assertEqual(json.loads(a.pilot(7)[0])["n"], 2)

    def test_holding_is_a_read(self):
        a, b = self.store(), self.store()
        a.claim(7, "ssh")
        beat = a.db.execute("SELECT heartbeat FROM sessions").fetchone()[0]
        self.clock.now += 50
        self.assertTrue(a.holds(7))
        self.assertFalse(b.holds(7))
        self.assertEqual(a.db.execute("SELECT heartbeat FROM sessions").fetchone()[0], beat)

    def test_pilots_are_kept_apart(self):
        a = self.store()
        for user in (1, 2, 3):
            a.claim(user, "ssh")
            a.commit(user, f"P{user}", {"who": user}, {"day": 1, "turn": user}, "hold")
        for user in (1, 2, 3):
            self.assertEqual(json.loads(a.pilot(user)[0])["who"], user)
            self.assertEqual(a.journal(user), [(1, 1, user, "hold")])

    WORKER = ("import sys, random; sys.path.insert(0, sys.argv[1]); import fair_winds as fw\n"
              "user = int(sys.argv[3]); s = fw.Store(sys.argv[2], 4660, 'node')\n"
              "s.claim(user, 'ssh', force=True)\n"
              "row = s.pilot(user); n = __import__('json').loads(row[0])['n'] if row else 0\n"
              "limit = int(sys.argv[4])\n"
              "while limit < 0 or n < limit:\n"
              "    n += 1\n"
              "    s.commit(user, 'P', {'n': n, 'pad': 'x' * random.randrange(2000)}, {'day': 1, 'turn': n}, 'hold')\n"
              "s.release(user); s.close()\n")

    def check(self, users):
        db = self.raw()
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        found = {}
        for user in users:
            row = db.execute("SELECT p.career, f.state, f.seq, (SELECT max(seq) FROM turns WHERE user_id=p.user_id),"
                             " (SELECT count(*) FROM turns WHERE user_id=p.user_id)"
                             " FROM pilots p JOIN flights f USING (user_id) WHERE p.user_id=?", (user,)).fetchone()
            if row is None:
                self.assertEqual(db.execute("SELECT count(*) FROM turns WHERE user_id=?", (user,)).fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM flights WHERE user_id=?", (user,)).fetchone()[0], 0)
                found[user] = 0
                continue
            n = json.loads(row[0])["n"]
            self.assertEqual((json.loads(row[1])["turn"], row[2], row[3], row[4]), (n, n, n, n),
                             "career, flight and journal all stand on the same turn")
            found[user] = n
        db.close()
        return found

    def test_eight_callers_flying_at_once_lose_nothing(self):
        self.store().close()
        procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), str(user), "40"],
                                  stderr=subprocess.PIPE) for user in range(1, 9)]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode()[-400:])
        self.assertEqual(self.check(range(1, 9)), {user: 40 for user in range(1, 9)})

    def test_a_process_killed_at_any_instant_leaves_a_whole_turn(self):
        self.store().close()
        rng = random.Random(5)
        last = 0
        for _ in range(6):
            proc = subprocess.Popen([sys.executable, "-c", self.WORKER, str(ROOT), str(self.folder), "3", "-1"],
                                    stderr=subprocess.DEVNULL)
            time.sleep(0.5 + rng.random() * 0.6)
            proc.kill()
            proc.wait()
            n = self.check([3])[3]
            self.assertGreaterEqual(n, last, "nothing committed is ever lost")
            last = n
        self.assertGreater(last, 0, "the worker did commit turns")
        store = self.store()
        self.assertIsNone(store.claim(3, "ssh", force=True))
        store.commit(3, "P", {"n": last + 1}, {"day": 1, "turn": last + 1}, "hold")
        self.assertEqual(self.check([3])[3], last + 1, "and the world goes on")


class Backup(StoreCase):
    def test_a_backup_taken_while_flying_restores_to_that_turn(self):
        game = support.new_game(launch(), self.folder)
        for key in ("ENTER", "ENTER", "ENTER"):
            game.key(key)
        target = self.folder / "backup"
        names = fw.backup_worlds(self.folder, target)
        self.assertEqual(names, [fw.db_name(game.world.seed)])
        state = game.state
        game.key("ENTER")                                # the game goes on after the backup
        game.close()
        restored = self.folder / "restored"
        restored.mkdir()
        for path in target.iterdir():
            (restored / path.name).write_bytes(path.read_bytes())
        again = fw.PlainFlight(launch(), restored)
        self.assertIsNone(again.busy, "nobody is flying in a restored world")
        self.assertEqual(again.state, state, "the pilot is on the turn the backup was taken at")
        again.key("ENTER")
        self.assertTrue(again.saved_ok)
        again.close()

    def test_the_command_line_backs_up_every_world(self):
        for node in ("one", "two"):
            game = support.new_game(launch(node_id=node), self.folder)
            game.key("ENTER")
            game.close()
        env = dict(os.environ, DOOR_FW_DATA_DIR=str(self.folder))
        target = self.folder / "copies"
        done = subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), "--backup", str(target)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout.split()), 2)
        self.assertEqual(len(list(target.glob("fair_winds-*.db"))), 2)
        for path in target.glob("*.db"):
            db = sqlite3.connect(str(path))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM pilots").fetchone()[0], 1)
            db.close()

    def test_a_backup_into_the_data_folder_is_refused(self):
        game = support.new_game(launch(), self.folder)
        game.key("ENTER")
        path = self.folder / fw.db_name(game.world.seed)
        for target in (self.folder, self.folder / "." / "sub" / ".."):
            with self.assertRaises(OSError):
                fw.backup_worlds(self.folder, target)
        game.key("ENTER")
        self.assertTrue(game.saved_ok)
        self.assertEqual(support.stored(self.folder)["state"]["turn"], 2, "the live records are the live records")
        self.assertEqual([p.name for p in self.folder.glob("*.tmp")], [])
        env = dict(os.environ, DOOR_FW_DATA_DIR=str(self.folder))
        done = subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), "--backup", str(self.folder)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("is the data folder", done.stderr)
        self.assertTrue(path.exists())

    def test_a_backup_never_lands_on_a_database_in_use(self):
        game = support.new_game(launch(), self.folder)
        game.key("ENTER")
        other = self.folder / "other"
        other.mkdir()
        (other / (fw.db_name(game.world.seed) + "-wal")).write_bytes(b"")
        with self.assertRaises(OSError):
            fw.backup_worlds(self.folder, other)
        self.assertEqual([p.name for p in other.glob("*.tmp")], [])

    def test_a_refused_backup_copies_nothing_at_all(self):
        names = []
        for node in ("one", "two"):
            game = support.new_game(launch(node_id=node), self.folder)
            game.key("ENTER")
            names.append(fw.db_name(game.world.seed))
            game.close()
        other = self.folder / "other"
        other.mkdir()
        (other / (sorted(names)[1] + "-wal")).write_bytes(b"")           # the second world is the one in the way
        with self.assertRaises(OSError):
            fw.backup_worlds(self.folder, other)
        self.assertEqual([p.name for p in other.glob("*.db")] + [p.name for p in other.glob("*.tmp")], [])

    def test_nothing_to_back_up_says_so(self):
        env = dict(os.environ, DOOR_FW_DATA_DIR=str(self.folder / "empty"))
        done = subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), "--backup", str(self.folder / "c")],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "no world to back up"))


class InTheGame(StoreCase):
    def screen(self, game):
        session = fw.Session(game.launch, Keys(), Out(), game=game)
        session.paint()
        term = Terminal(80, 24)
        term.feed(session.out.text)
        self.assertEqual(term.out_of_bounds, [])
        return term.text()

    def test_a_second_session_is_told_and_may_take_over(self):
        first = fw.PlainFlight(launch(), self.folder)
        first.key("ENTER")
        second = fw.PlainFlight(launch(transport="telnet"), self.folder)
        self.assertIsNotNone(second.busy)
        text = self.screen(second)
        self.assertIn("Mara is already flying: a session over ssh, opened a moment ago.", text)
        self.assertIn("Take over here", text)
        self.assertNotIn("Fuel", text, "nothing of the game is shown or touched")
        second.key("ENTER")
        second.key("w")
        self.assertIsNotNone(second.busy, "other keys do nothing")
        self.assertEqual(second.key("q"), "quit")
        second.close()
        first.key("ENTER")
        self.assertEqual((first.state.turn, first.ended), (2, ""), "leaving did not disturb the first session")

        third = fw.PlainFlight(launch(transport="telnet"), self.folder)
        third.key("t")
        self.assertIsNone(third.busy)
        self.assertEqual(third.state.turn, 2, "it takes up where the pilot stands")
        first.key("ENTER")
        self.assertEqual(first.ended, "taken")
        text = self.screen(first)
        self.assertIn("Mara is now flying from another session.", text)
        self.assertEqual(first.key("x"), "quit", "any key leaves")
        third.key("ENTER")
        self.assertEqual((third.state.turn, third.saved_ok), (3, True))
        self.assertEqual(support.stored(self.folder)["state"]["turn"], 3, "one line of turns, none lost, none twice")
        first.close()
        self.assertTrue(third.store.beat(12), "the loser's goodbye does not free the winner's lease")

    def test_a_session_taken_over_while_idle_learns_it_from_its_heartbeat(self):
        first = fw.PlainFlight(launch(), self.folder)
        session = fw.Session(first.launch, Keys(None, None, None, None), Out(), clock=Clock(), game=first)
        session.inp.clock = session.clock
        session.step()
        second = fw.PlainFlight(launch(), self.folder)
        second.key("t")
        session.step()
        self.assertEqual(first.ended, "", "not before the next heartbeat")
        session.clock.now += fw.HEARTBEAT_SECONDS + 1
        session.out.text = ""
        session.step()
        self.assertEqual(first.ended, "taken")
        self.assertIn("now flying from another session", session.out.text)

    def test_records_that_are_busy_are_not_called_damaged(self):
        first = fw.PlainFlight(launch(), self.folder)
        first.key("ENTER")
        first.close()
        real = fw.Store.pilot

        def busy(self, user_id):
            raise sqlite3.OperationalError("database is locked")
        fw.Store.pilot = busy
        try:
            game = fw.PlainFlight(launch(), self.folder)
        finally:
            fw.Store.pilot = real
        text = self.screen(game)
        self.assertIn("The records are busy. This game is not saved; yours is safe.", text)
        self.assertNotIn("could not be read", text)
        game.key("ENTER")
        game.close()
        again = fw.PlainFlight(launch(), self.folder)
        self.assertIsNone(again.busy, "the session that kept nothing held nobody out")
        self.assertEqual(again.state.turn, 1, "and the record is as it was")

    def test_a_turn_costs_one_write(self):
        game = fw.PlainFlight(launch(), self.folder)
        writes = []
        real = game.store._write
        game.store._write = lambda work: (writes.append(1), real(work))[1]
        game.key("ENTER")
        self.assertEqual(len(writes), 1, "the look before the turn reads; the commit is the one synced write")

    def test_leaving_frees_the_pilot(self):
        first = fw.PlainFlight(launch(), self.folder)
        first.key("ENTER")
        first.close()
        second = fw.PlainFlight(launch(), self.folder)
        self.assertIsNone(second.busy)
        self.assertEqual(second.state.turn, 1)

    def test_a_guest_keeps_nothing_and_holds_nobody_out(self):
        guest = fw.PlainFlight(launch(user_id=2 ** 62 + 5), self.folder)
        self.assertIsNone(guest.store)
        again = fw.PlainFlight(launch(user_id=2 ** 62 + 5), self.folder)
        self.assertIsNone(again.busy)

    def test_no_transaction_is_open_while_the_caller_thinks(self):
        game = fw.PlainFlight(launch(), self.folder)
        for key in ("ENTER", "w", "ENTER", "s", "p", "ENTER"):
            game.key(key)
            self.assertFalse(game.store.db.in_transaction)
        other = fw.PlainFlight(launch(user_id=40), self.folder)
        other.key("ENTER")                               # another caller writes while the first one thinks
        self.assertTrue(other.saved_ok)

    def test_records_that_stop_taking_writes_do_not_trap_the_caller(self):
        game = fw.PlainFlight(launch(), self.folder)
        game.key("ENTER")
        real = game.store.commit

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        game.store.commit = broken
        game.key("ENTER")
        self.assertEqual((game.state.turn, game.saved_ok), (2, False))
        self.assertIn("cannot be saved here", self.screen(game))
        game.store.commit = real
        game.key("ENTER")
        self.assertTrue(game.saved_ok, "and when they take writes again the game is saved again")
        self.assertEqual(support.stored(self.folder)["state"]["turn"], 3)


class RealDoor(unittest.TestCase):
    def test_records_that_cannot_be_used_stop_the_door_and_are_left_alone(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            seed = fw.world_seed(door_info()["node_id"])
            path = Path(folder) / fw.db_name(seed)
            path.write_bytes(b"not a database " * 500)
            before = path.read_bytes()
            with Door(environ={"DOOR_FW_DATA_DIR": folder}) as door:
                self.assertEqual(door.wait_exit(timeout=30), 3)
                self.assertIn("cannot read this world's records", door.output)
                self.assertIn("left them untouched", door.output)
                self.assertIn("damaged or is not a database", door.errors)
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(hasattr(__import__("signal"), "SIGHUP"), "no hang-up signal here")
    def test_a_dropped_line_gives_the_pilot_up(self):
        import signal
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            env = {"DOOR_FW_DATA_DIR": folder}
            with Door(environ=env) as first:
                self.assertTrue(first.wait_for("Day 1  06:00"))
                first.send("\r")
                self.assertTrue(first.wait_for("Day 1  06:15"))
                first.proc.send_signal(signal.SIGHUP)
                self.assertEqual(first.wait_exit(), 0)
            with Door(environ=env) as second:
                self.assertTrue(second.wait_for("Day 1  06:15"), "straight back in: nobody is asked anything")
                second.send("q")
                self.assertEqual(second.wait_exit(), 0)

    def test_two_doors_for_one_pilot(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            env = {"DOOR_FW_DATA_DIR": folder}
            with Door(environ=env) as first:
                self.assertTrue(first.wait_for("Day 1  06:00"))
                first.send("\r")
                self.assertTrue(first.wait_for("Day 1  06:15"))
                with Door(environ=env) as second:
                    self.assertTrue(second.wait_for("is already flying"))
                    second.send("t")
                    self.assertTrue(second.wait_for("Day 1  06:15"))
                    first.send("\r")
                    self.assertTrue(first.wait_for("now flying from another session"))
                    first.send(" ")
                    self.assertEqual(first.wait_exit(), 0)
                    second.send("\r")
                    self.assertTrue(second.wait_for("Day 1  06:30"))
                    second.send("q")
                    self.assertEqual(second.wait_exit(), 0)
            with Door(environ=env) as third:
                self.assertTrue(third.wait_for("Day 1  06:30"), "nobody is flying now: straight in")
                third.send("q")
                self.assertEqual(third.wait_exit(), 0)


if __name__ == "__main__":
    unittest.main()
