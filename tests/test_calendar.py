"""The calendar (S11): world days, the logbook, the three-day window, tempo and a clock that misbehaves."""

import dataclasses
import datetime
import os
import subprocess
import sys
import tempfile
import unittest
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from support import Terminal                              # noqa: E402
from test_plain import Keys, Out, launch                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
D = datetime.datetime


def has_zone(name):
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        return False


class Days(unittest.TestCase):
    EPOCH = datetime.date(2027, 3, 1)

    def test_one_day_for_each_local_date(self):
        self.assertEqual(fw.world_day(D(2027, 3, 1, 0, 0), self.EPOCH, 1, 1), 1)
        self.assertEqual(fw.world_day(D(2027, 3, 1, 23, 59, 59), self.EPOCH, 1, 1), 1)
        self.assertEqual(fw.world_day(D(2027, 3, 2, 0, 0), self.EPOCH, 1, 1), 2)
        self.assertEqual(fw.world_day(D(2027, 4, 9, 13, 0), self.EPOCH, 1, 1), 40)
        self.assertEqual(fw.day_opens(40, self.EPOCH, 1, 1), D(2027, 4, 9, 0, 0))

    def test_tempo_two_and_four_open_days_at_the_documented_hours(self):
        hours = {2: (0, 12), 4: (0, 6, 12, 18), 3: (0, 8, 16)}
        for tempo, starts in hours.items():
            for slot, hour in enumerate(starts):
                self.assertEqual(fw.day_opens(1 + slot, self.EPOCH, 1, tempo), D(2027, 3, 1, hour, 0))
                self.assertEqual(fw.world_day(D(2027, 3, 1, hour, 0), self.EPOCH, 1, tempo), 1 + slot)
                if hour:
                    self.assertEqual(fw.world_day(D(2027, 3, 1, hour - 1, 59, 59), self.EPOCH, 1, tempo), slot)
            self.assertEqual(fw.world_day(D(2027, 3, 2, 0, 0), self.EPOCH, 1, tempo), 1 + tempo)

    def test_every_day_opens_when_it_says_and_not_a_second_before(self):
        for tempo in fw.TEMPOS:
            for day in range(1, 60):
                opens = fw.day_opens(day, self.EPOCH, 1, tempo)
                self.assertEqual(fw.world_day(opens, self.EPOCH, 1, tempo), day)
                self.assertEqual(fw.world_day(opens - datetime.timedelta(seconds=1), self.EPOCH, 1, tempo), day - 1)

    @unittest.skipUnless(has_zone("Europe/Berlin"), "no time zone data on this host")
    def test_daylight_saving_changes_never_repeat_or_skip_a_day(self):
        zone = zoneinfo.ZoneInfo("Europe/Berlin")
        start = D(2027, 3, 20, 12, 0, tzinfo=zone).timestamp()
        epoch = datetime.date(2027, 3, 20)
        seen = []
        for hour in range(24 * 20):                      # across the night the clocks go forward (28 March)
            local = fw.local_time(start + hour * 3600, "Europe/Berlin")
            seen.append(fw.world_day(local, epoch, 1, 1))
        self.assertEqual(sorted(set(seen)), list(range(1, 21)) + [21])
        self.assertEqual(seen, sorted(seen))
        start = D(2027, 10, 25, 12, 0, tzinfo=zone).timestamp()
        epoch = datetime.date(2027, 10, 25)
        seen = [fw.world_day(fw.local_time(start + q * 900, "Europe/Berlin"), epoch, 1, 1) for q in range(96 * 12)]
        self.assertEqual(seen, sorted(seen), "the hour that happens twice (31 October) is one day")
        self.assertEqual(sorted(set(seen)), list(range(1, 14)))
        counts = [seen.count(day) for day in range(2, 13)]            # the whole dates between the two ends
        self.assertEqual(sorted(set(counts)), [96, 100], "each a whole local date; one of them 25 hours long")
        self.assertEqual(counts.count(100), 1)

    @unittest.skipUnless(has_zone("Pacific/Auckland"), "no time zone data on this host")
    def test_the_day_opens_at_the_nodes_midnight(self):
        zone = zoneinfo.ZoneInfo("Pacific/Auckland")
        before = D(2027, 6, 1, 23, 59, tzinfo=zone).timestamp()
        self.assertEqual(fw.local_time(before, "Pacific/Auckland").date(), datetime.date(2027, 6, 1))
        self.assertEqual(fw.local_time(before + 60, "Pacific/Auckland").date(), datetime.date(2027, 6, 2))
        self.assertEqual(fw.local_time(before + 60, "UTC").date(), datetime.date(2027, 6, 1))

    def test_a_zone_this_host_does_not_know_falls_back_to_its_own(self):
        local = fw.local_time(1_800_000_000.0, "Nowhere/Atall")
        self.assertEqual(local, D.fromtimestamp(1_800_000_000.0))
        self.assertEqual(fw.local_time(1_800_000_000.0, "local"), D.fromtimestamp(1_800_000_000.0))
        self.assertEqual(fw.local_time(1_800_000_000.0, "UTC"), D(2027, 1, 15, 8, 0))

    def test_when_a_day_opens_in_words(self):
        now = D(2027, 3, 1, 15, 0)
        fmt = "%d.%m.%Y %H:%M"
        self.assertEqual(fw.when_text(D(2027, 3, 2, 0, 0), now, fmt), "at midnight")
        self.assertEqual(fw.when_text(D(2027, 3, 1, 18, 0), now, fmt), "at 18:00")
        self.assertEqual(fw.when_text(D(2027, 3, 4, 0, 0), now, fmt), "on 04.03.2027 at midnight")
        self.assertEqual(fw.when_text(D(2027, 3, 4, 6, 0), now, "%Y-%m-%d %H:%M"), "on 2027-03-04 at 06:00")
        self.assertIn("at midnight", fw.when_text(D(2027, 3, 4, 0, 0), now, "%"), "a format that cannot be used")


class CalendarCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.folder = Path(self._dir.name)
        self.clock = support.Calendar()                  # 15 January 2027, 08:00 UTC

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def game(self, **changes):
        changes.setdefault("timezone", "UTC")
        return support.new_game(launch(**changes), self.folder, now=self.clock)

    def screen(self, game, width=80, height=24):
        session = fw.Session(dataclasses.replace(game.launch, width=width, height=height), Keys(), Out(), game=game)
        session.paint()
        term = Terminal(width, height)
        term.feed(session.out.text)
        self.assertEqual(term.out_of_bounds, [])
        return term.text()

    def fly_the_day_out(self, game):
        """Launch, come down where the balloon stands, and stay the night: the day is flown."""
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        game._choose_default()
        game.key("n")


class StoredCalendar(CalendarCase):
    def store(self):
        return fw.Store(self.folder, 77, "node")

    def local(self):
        return fw.local_time(self.clock(), "UTC")

    def test_the_calendar_begins_the_day_the_world_is_first_asked(self):
        store = self.store()
        today, cal = store.today(self.local(), "UTC")
        self.assertEqual((today, cal["epoch_date"], cal["tempo"], cal["high_day"]), (1, "2027-01-15", 1, 1))
        self.assertFalse(store.db.in_transaction)
        self.clock.days(3)
        self.assertEqual(store.today(self.local())[0], 4)
        self.assertEqual(self.store().today(self.local())[0], 4, "every caller sees the same day")
        meta = dict(store.db.execute("SELECT key, value FROM meta"))
        self.assertEqual((meta["high_day"], meta["timezone"]), ("4", "UTC"))

    def test_a_clock_set_backwards_never_repeats_a_day_and_never_skips_one(self):
        store = self.store()
        store.today(self.local())
        self.clock.days(5)
        self.assertEqual(store.today(self.local())[0], 6)
        self.clock.days(-3)                              # somebody sets the clock back three days
        self.assertEqual(store.today(self.local())[0], 6, "day 6 is not unopened")
        self.clock.days(2)
        self.assertEqual(store.today(self.local())[0], 6, "nor does day 5 come round again")
        self.clock.days(1)
        self.assertEqual(store.today(self.local())[0], 6)
        self.clock.days(1)
        self.assertEqual(store.today(self.local())[0], 7, "and when the clock is past it, the next day opens: none skipped")

    def test_reading_the_day_writes_only_when_a_day_opens(self):
        store = self.store()
        store.today(self.local())
        writes = []
        real = store._write
        store._write = lambda work: (writes.append(1), real(work))[1]
        for _ in range(5):
            self.clock.hours(1)
            store.today(self.local())
        self.assertEqual(writes, [])
        self.clock.days(1)
        store.today(self.local())
        self.assertEqual(writes, [1])

    def test_a_change_of_tempo_keeps_today_and_opens_days_faster_from_now(self):
        store = self.store()
        store.today(self.local())
        self.clock.days(9)                               # day 10, 08:00
        self.assertEqual(store.today(self.local())[0], 10)
        cal = store.set_tempo(2, self.local())
        self.assertEqual((cal["tempo"], cal["high_day"]), (2, 10))
        self.assertEqual(store.today(self.local())[0], 10, "today is still today")
        self.clock.hours(4)                              # noon
        self.assertEqual(store.today(self.local())[0], 11)
        self.clock.hours(12)                             # midnight
        self.assertEqual(store.today(self.local())[0], 12)
        cal = store.set_tempo(1, self.local())
        self.assertEqual(store.today(self.local())[0], 12)
        self.clock.hours(12)
        self.assertEqual(store.today(self.local())[0], 12, "back at one a day: no day at noon")
        self.clock.hours(12)
        self.assertEqual(store.today(self.local())[0], 13)

    def test_a_change_of_tempo_at_any_hour_opens_no_day_early(self):
        for hour, old, new in ((15, 1, 2), (15, 1, 4), (23, 1, 4), (23, 4, 2), (13, 2, 3), (3, 4, 1), (0, 1, 4)):
            cal = {"epoch_date": "2027-01-01", "epoch_day": 1, "tempo": old, "high_day": 1}
            local = datetime.datetime(2027, 1, 20, hour, 30)
            today = fw.calendar_day(cal, local)
            after = fw.retimed(dict(cal, high_day=today), new, local)
            case = f"{old} to {new} at {hour}:30"
            self.assertEqual(fw.calendar_day(after, local), today, f"{case}: today stays today")
            opens = fw.calendar_opens(after, today + 1)
            self.assertGreater(opens, local, f"{case}: the next day opens later, not already")
            self.assertLessEqual(opens - local, datetime.timedelta(days=1) / new, f"{case}: at the next slot")
            self.assertEqual(fw.calendar_day(after, opens - datetime.timedelta(seconds=1)), today, case)
            self.assertEqual(fw.calendar_day(after, opens), today + 1, case)
            self.assertIsNotNone(fw.read_calendar(after), f"{case}: and it reads back")

    def test_a_change_of_zone_is_recorded_at_once(self):
        store = self.store()
        store.today(self.local(), "UTC")
        store.today(self.local(), "Europe/Berlin")
        self.assertEqual(dict(store.db.execute("SELECT key, value FROM meta"))["timezone"], "Europe/Berlin")
        writes = []
        real = store._write
        store._write = lambda work: (writes.append(1), real(work))[1]
        store.today(self.local(), "Europe/Berlin")
        store.today(self.local())
        self.assertEqual(writes, [], "and then it is a read again")

    def test_the_command_line_sets_the_tempo_of_every_world(self):
        for node in ("one", "two"):
            game = support.new_game(launch(node_id=node), self.folder)
            game.close()
        env = dict(os.environ, DOOR_FW_DATA_DIR=str(self.folder))
        done = subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), "--tempo", "4"],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("tempo 4, day 1 today, day 2 opens", lines[0])
        game = support.new_game(launch(node_id="one"), self.folder)
        self.assertEqual(game.calendar["tempo"], 4)
        bad = subprocess.run([sys.executable, str(ROOT / "fair_winds.py"), "--tempo", "7"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(bad.returncode, 0)


class Logbook(CalendarCase):
    def test_a_new_pilot_starts_on_today(self):
        first = self.game()
        self.assertEqual((first.today, first.state.day), (1, 1))
        self.clock.days(39)
        late = self.game(user_id=50)
        self.assertEqual((late.today, late.state.day), (40, 40), "whoever joins on day 40 flies day 40")

    def test_a_day_flown_cannot_be_flown_again(self):
        game = self.game()
        self.fly_the_day_out(game)
        self.assertEqual((game.state.day, game.state.turn, game.phase()), (2, 0, "wait"))
        self.assertEqual(game.offered(), {})
        for key in ("ENTER", "l", " ", "n", "w", "p", "j", "s"):
            game.key(key)
        self.assertEqual((game.state.day, game.state.turn, game.state.status), (2, 0, "ground"), "no key flies it")
        text = self.screen(game)
        self.assertIn("Every day that has opened is flown. Day 2 opens at midnight: come back then.", text)
        self.assertNotIn("Launch", text)
        self.assertNotIn("Next day", text)
        # reconnecting, on another transport, as often as one likes
        for transport in ("ssh", "telnet", "web"):
            again = self.game(transport=transport)
            self.assertEqual((again.state.day, again.phase(), again.offered()), (2, "wait", {}))
            again.key("ENTER")
            self.assertEqual(again.state.turn, 0)
        self.assertEqual(support.stored(self.folder)["state"]["day"], 2)

    def test_a_day_never_opens_early(self):
        game = self.game()                               # 08:00
        self.fly_the_day_out(game)
        self.clock.hours(15).now += 59 * 60 + 59         # 23:59:59
        game.key("ENTER")
        self.assertEqual((game.phase(), game.state.turn), ("wait", 0))
        self.assertTrue(self.game().waiting(), "nor for a new session")
        game = self.game()
        self.clock.now += 1                              # midnight
        game.key("ENTER")
        self.assertEqual((game.phase(), game.state.turn), ("ground", 0), "the key that found the day open shows it")
        self.assertIn("launch", game.offered())
        game.key("ENTER")
        self.assertEqual((game.state.day, game.state.turn), (2, 1))

    def test_an_idle_session_sees_the_day_open(self):
        game = self.game()
        self.fly_the_day_out(game)
        ticks = support.Calendar(0.0)
        session = fw.Session(game.launch, Keys(None, None, None, None), Out(), clock=ticks, game=game)
        session.inp.clock = ticks
        session.step()
        self.assertIn("come back then", session.out.text)
        self.clock.days(1)
        session.out.text = ""
        session.step()
        self.assertEqual(session.out.text, "", "not looked at yet")
        ticks.now += fw.CALENDAR_CHECK_SECONDS + 1
        session.step()
        self.assertEqual(game.phase(), "ground")
        self.assertIn("Launch", session.out.text, "the screen changed by itself")

    def test_four_missed_days_leave_exactly_three(self):
        game = self.game()
        self.fly_the_day_out(game)                       # the logbook stands at day 2
        game.close()
        self.clock.days(5)                               # today is day 6: days 2 to 6 opened, four of them missed
        game = self.game()
        self.assertEqual((game.today, game.state.day), (6, 4), "days 4, 5 and 6 wait: exactly three")
        self.assertIn("2 days went by unflown. Day 4.", " ".join(game.log))
        self.assertIn("Day 7 opens at midnight. Two more days wait.", self.screen(game))
        self.assertEqual(support.stored(self.folder)["state"]["day"], 4)
        flown = []
        for _ in range(5):
            if game.waiting():
                break
            flown.append(game.state.day)
            self.fly_the_day_out(game)
        self.assertEqual(flown, [4, 5, 6], "oldest first, each once")
        self.assertEqual((game.state.day, game.phase()), (7, "wait"))

    def test_missed_days_cost_nothing(self):
        game = self.game()
        before = (game.state.fuel, game.state.ballast, game.state.wear, game.deliveries, game.targets)
        game.close()
        self.clock.days(30)
        game = self.game()
        self.assertEqual((game.state.fuel, game.state.ballast, game.state.wear, game.deliveries, game.targets), before)
        self.assertEqual(game.state.day, 29)

    def test_a_flight_begun_before_midnight_keeps_its_day(self):
        game = self.game()
        game.key("ENTER")
        game.key("ENTER")
        self.clock.days(1)
        game.key("ENTER")
        self.assertEqual((game.state.day, game.state.turn), (1, 3), "the turnover does not touch a day in progress")
        again = self.game()
        self.assertEqual((again.state.day, again.state.turn), (1, 3))
        self.clock.days(6)                               # a week away, mid-flight
        again = self.game()
        self.assertEqual((again.today, again.state.day, again.state.turn), (8, 1, 3), "it is still that day, in its weather")
        again.key("ENTER")
        self.assertEqual((again.state.day, again.state.turn), (1, 4))
        again.state = dataclasses.replace(again.state, status="ground", layer=0)
        again._choose_default()
        again.key("n")
        self.assertEqual(again.state.day, 6, "when it ends, the three newest days are what wait")
        self.assertIn("went by unflown", " ".join(again.log))

    def test_two_flights_in_one_day_are_one_day(self):
        game = self.game()
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        game._choose_default()
        self.assertEqual(game.phase(), "ground")
        game.key("ENTER")
        self.assertEqual((game.state.day, game.state.status), (1, "aloft"), "land, and launch again while time remains")

    def test_walking_out_is_paid_in_days_and_then_the_calendar_is_waited_for(self):
        game = self.game()
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, status="stranded", layer=0, fuel=0)
        game.key("w")
        self.assertEqual((game.state.day, game.phase()), (3, "wait"))
        self.assertIn("Day 3 opens on 17.01.2027 at midnight.", " ".join(game.log))
        self.clock.days(1)
        game.key("ENTER")
        self.assertEqual(game.phase(), "wait", "day 2 opening is not day 3 opening")
        self.clock.days(1)
        game.key("ENTER")
        self.assertEqual(game.phase(), "ground")

    def test_a_pilot_who_is_behind_wakes_straight_into_the_next_day(self):
        game = self.game()
        self.clock.days(2)
        game.close()
        game = self.game()
        self.assertEqual((game.today, game.state.day), (3, 1))
        self.fly_the_day_out(game)
        self.assertEqual((game.state.day, game.phase()), (2, "ground"), "no waiting: day 2 opened long ago")
        self.assertIn("Day 2. Dawn.", " ".join(game.log))
        self.assertIn("Day 4 opens at midnight. One more day waits.", self.screen(game))

    def test_the_briefing_always_says_which_day_and_when_the_next_opens(self):
        game = self.game()
        text = self.screen(game)
        self.assertIn("Day 1  06:00", text)
        self.assertIn("Day 2 opens at midnight.   Mara  0 delivered  0 km", text)
        self.assertNotIn("more day", text)
        for size in ((132, 40), (168, 46)):
            wide = self.screen(game, *size)
            self.assertIn("Day 2 opens at midnight.", wide)
            self.assertIn(f"{size[0]} x {size[1]} truecolor", wide, "and what the door makes of the terminal, where there is room")
        game.key("ENTER")
        self.assertIn("Day 2 opens at midnight.", self.screen(game), "aloft too")
        game.store.set_tempo(2, game._local())
        game._look_at_the_calendar()
        self.assertIn("Day 2 opens at 12:00.", self.screen(game))

    def test_a_long_handle_does_not_push_the_briefing_off(self):
        game = self.game(handle="A" * 40)
        self.clock.days(2)
        game._look_at_the_calendar()
        self.assertIn("Day 4 opens at midnight. Two more days wait.", self.screen(game))

    def test_a_pilot_from_before_the_calendar_is_brought_to_today_once(self):
        game = self.game()
        game.key("ENTER")
        game.state = dataclasses.replace(game.state, day=253, turn=0, status="ground", layer=0)
        game._dated = False
        game.save()
        stores = (game.state.fuel, game.deliveries, game.targets)
        game.close()
        self.clock.days(2)
        game = self.game()
        self.assertEqual((game.today, game.state.day), (3, 3), "day 253 of their own counting is today")
        self.assertEqual((game.state.fuel, game.deliveries, game.targets), stores, "and nothing else changes")
        self.assertIn("launch", game.offered(), "they are not made to wait 250 days")
        self.fly_the_day_out(game)
        self.assertEqual((game.state.day, game.phase()), (4, "wait"))
        game.close()
        self.assertEqual(self.game().state.day, 4, "once: the next call leaves the logbook alone")

    def test_a_visit_has_a_calendar_of_its_own(self):
        guest = fw.PlainFlight(launch(user_id=2 ** 62 + 9, timezone="UTC"), self.folder, now=self.clock)
        self.assertEqual((guest.today, guest.state.day), (1, 1))
        self.fly_the_day_out(guest)
        self.assertEqual(guest.phase(), "wait")
        self.clock.days(1)
        guest.key("ENTER")
        self.assertEqual(guest.phase(), "ground")
        self.assertEqual(list(self.folder.glob("*.db")), [])

    def test_a_clock_set_back_does_not_reopen_a_flown_day(self):
        game = self.game()
        self.clock.days(1)
        game._look_at_the_calendar()
        self.fly_the_day_out(game)                       # day 1
        self.fly_the_day_out(game)                       # day 2
        self.assertEqual((game.state.day, game.phase()), (3, "wait"))
        game.close()
        self.clock.days(-10)
        game = self.game()
        self.assertEqual((game.today, game.state.day, game.phase()), (2, 3, "wait"))
        self.assertIn("Day 3 opens at midnight", self.screen(game).replace("on 17.01.2027 ", ""))


class RealDoor(unittest.TestCase):
    def test_the_door_says_when_the_next_day_opens(self):
        with support.Door() as door:
            self.assertTrue(door.wait_for("Day 2 opens at midnight."))
            door.send("q")
            self.assertEqual(door.wait_exit(), 0)


if __name__ == "__main__":
    unittest.main()
