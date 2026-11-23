"""The post (S12): contracts, cargo, postmarks, the stores and the post office screen."""

import dataclasses
import json
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from support import Terminal                              # noqa: E402
from test_plain import Keys, Out, launch                  # noqa: E402


class PostCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.folder = Path(self._dir.name)
        self.clock = support.Calendar()

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
        self.assertEqual(term.out_of_bounds, [], (width, height))
        self.assertNotIn((height - 1, width - 1), term.written)
        return term.text()

    def empty(self, game):
        """Take everything off the balloon, as if it had all been delivered."""
        for item in list(game.manifest):
            game._unload(item)

    def land_at(self, game, ref, kind="clean", **changes):
        """Put the balloon down at a place, in a landing of the class asked for."""
        place = game.place_of(ref)
        for day in range(game.state.day, game.state.day + 200):
            for turn in (8, 20, 30, 40):
                state = dataclasses.replace(game.state, x=place["x"], y=place["y"], day=day, turn=turn, status="aloft",
                                            layer=game.engine.lowest_open(place["x"], place["y"]), **changes)
                if "land" in game.engine.actions(state) and game.engine.landing_class(state) == kind:
                    game.today = max(game.today, day)
                    game.state = state
                    game._choose_default()
                    game.key("l")
                    game.key("ENTER")
                    return
        self.fail(f"no {kind} landing there in two hundred days")


class Contracts(PostCase):
    def test_the_same_town_on_the_same_day_offers_the_same_to_everyone(self):
        world = self.game().world
        for ref in (("town", 0), ("town", 3), ("station", 0)):
            for day in (1, 2, 40):
                self.assertEqual(fw.contracts_at(world, ref, day), fw.contracts_at(world, ref, day))
        other = self.game(user_id=99).world
        self.assertEqual(fw.contracts_at(world, ("town", 0), 7), fw.contracts_at(other, ("town", 0), 7))
        days = [json.dumps(fw.contracts_at(world, ("town", 0), day)) for day in range(1, 30)]
        self.assertGreater(len({d.split('"kind"', 1)[1] for d in days}), 5, "and another day is another set")

    def test_a_town_offers_three_to_five_each_for_a_different_place_nearby(self):
        world = self.game().world
        counts = set()
        for index, town in enumerate(world.towns):
            near = fw.places_near(world, town["x"], town["y"], skip=("town", index))[:fw.OFFER_REACH]
            for day in range(1, 40):
                offers = fw.contracts_at(world, ("town", index), day)
                counts.add(len(offers))
                dests = [tuple(o["dest"]) for o in offers]
                self.assertEqual(len(set(dests)), len(dests), "several destinations: the wind picks")
                self.assertTrue(all(d in near for d in dests))
                self.assertNotIn(("town", index), dests)
                for i, offer in enumerate(offers):
                    self.assertEqual(offer["contract"], ["town", index, day, i])
                    self.assertIn(offer["kind"], fw.CARGO_KINDS)
        self.assertEqual(counts, {3, 4, 5})

    def test_a_station_has_a_little_mail(self):
        world = self.game().world
        for index in range(len(world.stations)):
            offers = fw.contracts_at(world, ("station", index), 5)
            self.assertEqual((len(offers), {o["kind"] for o in offers}), (fw.STATION_OFFERS, {"mail"}))

    def test_payout_and_deadline_come_from_the_table(self):
        world = self.game().world
        seen = set()
        for day in range(1, 60):
            for offer in fw.contracts_at(world, ("town", 1), day):
                kind = fw.CARGO_KINDS[offer["kind"]]
                here, there = world.towns[1], fw.PlainFlight.place_of(self.game(), tuple(offer["dest"]))
                km = math.hypot(there["x"] - here["x"], there["y"] - here["y"])
                self.assertEqual(offer["payout"], int(kind["base"] + kind["per_km"] * km + 0.5))
                self.assertEqual(offer["due"], day + kind["days"] + int(km // 300))
                self.assertEqual(offer["weight"], kind["weight"])
                seen.add(offer["kind"])
        self.assertEqual(seen, set(fw.CARGO_KINDS), "every kind turns up")

    def test_prices_and_payouts_are_data(self):
        game = self.game()
        world, before = game.world, fw.contracts_at(game.world, ("town", 0), 3)
        real_kinds, real_prices = fw.CARGO_KINDS, fw.PRICES
        try:
            fw.CARGO_KINDS = {code: dict(kind, base=kind["base"] + 100) for code, kind in real_kinds.items()}
            fw.PRICES = dict(real_prices, fuel=7)
            after = fw.contracts_at(world, ("town", 0), 3)
            self.assertEqual([o["payout"] for o in after], [o["payout"] + 100 for o in before])
            self.assertEqual((fw.price_of("fuel", ("town", 0)), fw.price_of("fuel", ("station", 0))), (7, 11))
        finally:
            fw.CARGO_KINDS, fw.PRICES = real_kinds, real_prices
        self.assertEqual((fw.price_of("fuel", ("town", 0)), fw.price_of("sack", ("station", 0))), (1, 6))


class Carrying(PostCase):
    def test_a_new_pilot_has_a_first_round_aboard(self):
        game = self.game()
        self.assertEqual(game.postmarks, fw.START_POSTMARKS)
        self.assertEqual([item["kind"] for item in game.manifest], ["mail"] * 3)
        home = ("town", game.world.start_town)
        near = fw.places_near(game.world, game.state.x, game.state.y, skip=home)[:3]
        self.assertEqual(game.targets, near, "for the three nearest places")
        self.assertEqual({item["due"] for item in game.manifest}, {1 + fw.FIRST_ROUND_DAYS})
        self.assertEqual(len(game.state.cargo), 3)

    def test_taking_and_putting_back(self):
        game = self.game()
        self.empty(game)
        offers = game.offers()
        game.key("o")
        self.assertTrue(game.hub)
        game.key("1")
        self.assertIn("Aboard:", game.hub_note)
        self.assertEqual((len(game.manifest), game.weight()), (1, offers[0]["weight"]))
        self.assertEqual(game.manifest[0]["payout"], offers[0]["payout"])
        self.assertEqual(support.stored(self.folder)["manifest"], game.manifest, "saved at once")
        game.key("1")
        self.assertIn("Put back:", game.hub_note)
        self.assertEqual((game.manifest, game.state.cargo, game.taken), ([], (), []))
        game.key("1")
        self.assertEqual(len(game.manifest), 1, "and it can be taken again")

    def test_the_balloon_lifts_only_so_much(self):
        game = self.game()
        self.empty(game)
        for _ in range(fw.LIFT):
            game._load_cargo({"contract": None, "kind": "mail", "dest": ["town", 1], "origin": None, "weight": 1,
                              "payout": 1, "due": 9})
        game.key("o")
        game.key("1")
        self.assertIn("Too heavy", game.hub_note)
        self.assertEqual(game.weight(), fw.LIFT)

    def test_a_contract_is_carried_once(self):
        game = self.game()
        self.empty(game)
        offer = game.offers()[0]
        game.take(offer)
        game._unload(game.manifest[0])                   # delivered, say, and the pilot is back the same day
        self.assertEqual(game.take(offer), "You have carried that one already.")
        self.assertIn("carried", self.screen_of_hub(game))
        game.key("ENTER")
        game.key("n")
        self.assertEqual(game.taken, [], "yesterday's are forgotten: today's offers are other offers")

    def screen_of_hub(self, game):
        game.hub = True
        return self.screen(game)

    def test_a_delivery_pays_what_it_promised(self):
        game = self.game()
        self.empty(game)
        offer = game.offers()[0]
        game.take(offer)
        before = game.postmarks
        self.land_at(game, tuple(offer["dest"]), fuel=30)
        self.assertEqual((game.postmarks, game.deliveries, game.manifest), (before + offer["payout"], 1, []))
        self.assertIn(f"{offer['payout']} postmarks. That makes 1.", " ".join(game.log))

    def test_late_cargo_pays_what_the_table_says(self):
        game = self.game()
        self.empty(game)
        offer = game.offers()[0]
        game.take(offer)
        before = game.postmarks
        game.state = dataclasses.replace(game.state, day=offer["due"] + 1)
        self.land_at(game, tuple(offer["dest"]), fuel=30)
        self.assertEqual(game.postmarks, before + int(offer["payout"] * fw.LATE_SHARE))
        self.assertIn("late: less is paid", " ".join(game.log))

    def test_instruments_shaken_by_a_rough_landing_pay_less(self):
        game = self.game()
        self.empty(game)
        game._load_cargo({"contract": None, "kind": "fragile", "dest": ["town", 1], "origin": None, "weight": 2,
                          "payout": 80, "due": 400})
        game._load_cargo({"contract": None, "kind": "mail", "dest": ["town", 1], "origin": None, "weight": 1,
                          "payout": 20, "due": 400})
        before = game.postmarks
        self.land_at(game, ("town", 1), kind="rough", fuel=30)
        self.assertEqual(game.postmarks, before + int(80 * fw.DAMAGED_SHARE) + 20, "the mailbag does not mind")
        self.assertIn("damaged", " ".join(game.log))

    def test_cargo_lost_in_the_sea_costs_what_the_table_says(self):
        game = self.game()
        self.empty(game)
        for kind in ("mail", "parcel"):
            game._load_cargo({"contract": None, "kind": kind, "dest": ["town", 1], "origin": None,
                              "weight": 1, "payout": 50, "due": 9})
        game.postmarks = 100
        game.state = dataclasses.replace(game.state, x=5.0, y=5.0, status="aloft", layer=1, turn=20, fuel=30)
        game._choose_default()
        game.key("l")
        game.key("ENTER")
        self.assertEqual(game.state.status, "ditched")
        forfeit = fw.CARGO_KINDS["mail"]["lost"] + fw.CARGO_KINDS["parcel"]["lost"]
        self.assertEqual((game.manifest, game.postmarks), ([], 100 - forfeit))
        self.assertIn(f"Lost: 2 pieces of cargo. {forfeit} postmarks forfeit.", " ".join(game.heard))

    def test_a_forfeit_never_takes_more_than_there_is(self):
        game = self.game()
        game.postmarks = 3
        game.state = dataclasses.replace(game.state, cargo=())
        game._lost_cargo()
        self.assertEqual((game.postmarks, game.manifest), (0, []))

    def test_with_nothing_aboard_the_nearest_offices_are_named(self):
        game = self.game()
        self.empty(game)
        self.assertEqual(len(game.targets), 3)
        self.assertNotIn(game.here(), game.targets)
        self.assertIn("Nearest office:", self.screen(game))
        game.key("ENTER")
        self.assertEqual(game.state.status, "aloft")
        self.screen(game)

    def test_cargo_and_papers_that_disagree_are_not_played_on(self):
        game = self.game()
        game.key("ENTER")
        game.close()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        career = json.loads(db.execute("SELECT career FROM pilots").fetchone()[0])
        career["manifest"] = career["manifest"][:1]
        db.execute("UPDATE pilots SET career=?", (json.dumps(career),))
        db.commit()
        db.close()
        again = self.game()
        self.assertFalse(again.saved_ok)
        self.assertIn("could not be read", again.opening_notice)

    def test_a_pilot_from_before_the_post_office_keeps_their_post_as_mailbags(self):
        game = self.game()
        game.key("ENTER")
        targets = game.targets
        game.close()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        career = json.loads(db.execute("SELECT career FROM pilots").fetchone()[0])
        state = json.loads(db.execute("SELECT state FROM flights").fetchone()[0])
        old = {"targets": [list(t) for t in targets], "visited": [["town", 0]], "deliveries": 7, "days_flown": 3,
               "total_km": 500.0, "log": career["log"], "dated": True}
        state["cargo"] = []
        db.execute("UPDATE pilots SET career=?", (json.dumps(old),))
        db.execute("UPDATE flights SET state=?", (json.dumps(state),))
        db.commit()
        db.close()
        again = self.game()
        self.assertTrue(again.saved_ok)
        self.assertEqual((again.targets, again.deliveries, again.postmarks), (targets, 7, fw.START_POSTMARKS))
        self.assertEqual(([item["kind"] for item in again.manifest], len(again.state.cargo)), (["mail"] * 3, 3))
        again.key("ENTER")
        self.assertEqual(support.stored(self.folder)["postmarks"], fw.START_POSTMARKS)


class Stores(PostCase):
    def test_buying(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, fuel=20, ballast=2, pibals=0)
        game.key("o")
        game.key("f")
        self.assertEqual((game.state.fuel, game.postmarks), (30, 30))
        game.key("s")
        game.key("p")
        self.assertEqual((game.state.ballast, game.state.pibals, game.postmarks), (3, 1, 20))
        game.key("a")
        self.assertEqual((game.state.fuel, game.postmarks), (50, 0), "as much as the postmarks buy")
        self.assertIn("That is all the postmarks would buy.", game.hub_note)
        game.key("f")
        self.assertIn("Not enough postmarks", game.hub_note)
        self.assertEqual(support.stored(self.folder)["postmarks"], 0)
        self.assertEqual(support.stored(self.folder)["state"]["fuel"], 50)

    def test_no_more_than_fits(self):
        game = self.game()
        game.key("o")
        for key, word in (("f", "fuel"), ("a", "fuel"), ("s", "sacks"), ("p", "pibals")):
            game.key(key)
            self.assertEqual(game.hub_note, f"No room for more {word}.")
        game.key("m")
        self.assertEqual(game.hub_note, "Nothing to mend.")
        self.assertEqual(game.postmarks, fw.START_POSTMARKS)

    def test_a_station_asks_half_as_much_again(self):
        game = self.game()
        station = game.world.stations[0]
        game.state = dataclasses.replace(game.state, x=station["x"], y=station["y"], fuel=20, wear=10)
        self.assertEqual(game.here(), ("station", 0))
        game.key("o")
        game.key("f")
        self.assertEqual((game.state.fuel, game.postmarks), (30, 40 - 20))
        game.key("m")
        self.assertEqual((game.state.wear, game.postmarks), (0, 0))
        self.assertIn("a weather station", self.screen(game))

    def test_the_post_office_is_only_at_a_place_on_the_ground(self):
        game = self.game()
        game.key("ENTER")
        game.key("o")
        self.assertFalse(game.hub, "not aloft")
        game.state = dataclasses.replace(game.state, x=game.state.x - 60.0, status="ground", layer=0)
        game.key("o")
        self.assertFalse(game.hub, "not in the wild")
        self.assertEqual(game.offers(), [])

    def test_leaving_the_post_office(self):
        for key in ("ENTER", "ESC", "b", "o", " "):
            game = self.game(user_id=hash(key) % 1000 + 100)
            game.key("o")
            self.assertTrue(game.hub)
            game.key(key)
            self.assertFalse(game.hub)
            self.assertEqual(game.state.turn, 0, "the key that closes the door launches nothing")
        game.key("o")
        self.assertEqual(game.key("q"), "quit")


class Relief(PostCase):
    def test_nobody_who_can_pay_is_offered_it(self):
        game = self.game()
        self.assertFalse(game.relief_due())
        game.state = dataclasses.replace(game.state, fuel=0)
        self.assertFalse(game.relief_due(), "forty postmarks buy the fuel")
        game.postmarks = 24
        self.assertTrue(game.relief_due())
        game.state = dataclasses.replace(game.state, fuel=fw.RELIEF["fuel"])
        self.assertFalse(game.relief_due(), "enough fuel to fly a run")

    def test_the_relief_run(self):
        game = self.game()
        self.empty(game)
        game.postmarks = 0
        game.state = dataclasses.replace(game.state, fuel=0, ballast=0, wear=fw.WEAR_GROUNDED)
        self.assertEqual(game.phase(), "rest")
        text = self.screen(game)
        self.assertIn("[O] Post", text)
        game.key("o")
        self.assertIn("R  Relief run", self.screen(game))
        game.key("r")
        self.assertEqual((game.state.fuel, game.state.ballast, game.state.wear),
                         (fw.RELIEF["fuel"], fw.RELIEF["sack"], fw.RELIEF["wear"]))
        self.assertEqual(([item["payout"] for item in game.manifest], game.postmarks), ([fw.RELIEF["payout"]], 0))
        self.assertFalse(game.relief_due(), "once a day")
        self.assertNotIn("Relief run", self.screen(game))
        game.key("ENTER")
        self.assertIn("launch", game.offered(), "and the pilot can fly")
        game.state = dataclasses.replace(game.state, fuel=0)
        self.assertFalse(game.relief_due())
        game.key("n")
        self.clock.days(1)
        game._day_may_have_opened()
        self.assertTrue(game.relief_due(), "tomorrow, if it comes to that again")

    def test_the_relief_key_does_nothing_for_a_pilot_who_can_pay(self):
        game = self.game()
        game.key("o")
        game.key("r")
        self.assertEqual((game.state.fuel, len(game.manifest), game.hub_note), (60, 3, ""))


class Screen(PostCase):
    def test_the_post_office_fits_every_layout(self):
        game = self.game()
        game.key("o")
        game.key("1")
        for size in ((80, 24), (100, 30), (132, 40), (168, 46), (250, 70), (80, 60)):
            text = self.screen(game, *size)
            self.assertIn("THE POST OFFICE", text)
            self.assertIn("ABOARD", text)
            self.assertIn("STORES", text)
            self.assertIn("To the balloon", text)
            self.assertIn("postmarks", text)
        for changes in ({"color_depth": "16"}, {"charset": "cp437", "color_depth": "16"}, {"charset": "ascii"}):
            other = self.game(user_id=300 + len(changes), **changes)
            other.key("o")
            self.assertIn("THE POST OFFICE", self.screen(other))

    def test_the_place_looks_in_where_there_are_rows_to_spare(self):
        game = self.game()
        game.key("o")
        session = fw.Session(dataclasses.replace(game.launch, width=132, height=40), Keys(), Out(), game=game)
        session.paint()
        self.assertIn("▀", session.out.text, "a picture of the place over the counter")
        self.assertIsNotNone(game._vista)
        kept = game._vista[1]
        session.paint()
        self.assertIs(game._vista[1], kept, "painted once, not at every key")
        small = fw.Session(game.launch, Keys(), Out(), game=game)
        small.paint()
        self.assertNotIn("▀", small.out.text, "at 80 by 24 the counter takes every row")

    def test_a_long_manifest_is_summed_up(self):
        game = self.game()
        for _ in range(4):
            game._load_cargo({"contract": None, "kind": "mail", "dest": ["town", 2], "origin": None, "weight": 1,
                              "payout": 1, "due": 9})
        game.key("o")
        self.assertIn("and 5 more", self.screen(game))

    def test_the_session_plays_the_post_office(self):
        game = self.game()
        session = fw.Session(game.launch, Keys(b"o", b"1", b"f", b"\r", b"\r"), Out(), game=game)
        for _ in range(3):
            session.step()
        self.assertTrue(game.hub)
        session.step()
        self.assertFalse(game.hub)
        session.step()
        self.assertEqual(game.state.status, "aloft")


class Career(PostCase):
    """A pilot played by a script for ten world days: it must never reach a state it cannot fly on from."""

    def shop(self, game):
        self.assertTrue(game.at_office())
        if game.relief_due():
            game.relief()
        if game.state.grounded:
            game.mend()
        game.buy("fuel")
        for offer in sorted(game.offers(), key=lambda o: -o["payout"] / o["weight"]):
            if game.held(offer) is None and offer["contract"] not in game.taken \
                    and game.weight() + offer["weight"] <= fw.LIFT:
                game.take(offer)

    def best(self, game):
        """The layer choice whose forecast track ends nearest any place the pilot is bound for."""
        best, best_km = None, None
        for action in game.choices():
            look = game.preview(action)
            if not look["points"]:
                continue
            ex, ey, _ = look["points"][-1]
            km = game.bearings(ex, ey)[0][0]
            if best_km is None or km < best_km:
                best, best_km = action, km
        return best

    def fly_a_day(self, game):
        for _ in range(fw.DAY_TURNS + 5):
            phase = game.phase()
            if phase == "ground" and game.state.turn == 0:
                if game.at_office():
                    self.shop(game)
                if "launch" not in game.offered():
                    break
                game.key("ENTER")
            elif phase == "aloft":
                offered = game.offered()
                here = game.here()
                bound = here is not None and here in game.targets
                low = game.state.fuel < 8 or game.state.minute >= 19 * 60
                if "land" in offered and (bound or (low and here is not None) or game.state.fuel < 4) \
                        and game.engine.landing_class(game.state) != "ditch":
                    game.key("l")
                    game.key("ENTER")
                    continue
                choice = self.best(game)
                if choice is None or choice not in offered:
                    choice = next(iter(game.choices()), None)
                if choice is None:
                    # Nothing to hold or climb on: come down if the ground will have it, else throw a sack and wait.
                    if "land" in offered:
                        game.key("l")
                    elif "jettison" in offered:
                        game.key("j")
                    else:
                        self.fail(f"aloft with nothing to do: {game.state}")
                    game.key("ENTER")
                    continue
                game.choice = choice
                game.key("ENTER")
            else:
                break

    def end_the_day(self, game):
        phase = game.phase()
        if phase == "aloft":
            self.fail("still aloft at the end of the day")
        game.key({"stranded": "w", "ditched": "n"}.get(phase, "n"))

    def career(self, node, user):
        game = self.game(node_id=node, user_id=user)
        for day in range(10):
            before = game.state.day
            self.fly_a_day(game)
            self.end_the_day(game)
            self.assertGreater(game.state.day, before, "the day ended")
            self.clock.days(3)                           # the script never waits for the calendar
            game._day_may_have_opened()
            self.assertFalse(game.waiting())
            self.assertGreaterEqual(game.postmarks, 0)
            # The rule: wherever the pilot stands at dawn, there is a way to fly on.
            if game.phase() == "stranded":
                continue                                 # walking out is the way
            if not game.at_office():
                # Down in the wild with fuel to launch on: fly on, or run dry and walk out.
                self.assertIn("launch", game.offered(), f"day {day}: {game.phase()} in the wild, {game.state.fuel} fuel")
                continue
            self.shop(game)
            self.assertIn("launch", game.offered(),
                          f"day {day}: at {game.here()} with {game.state.fuel} fuel, {game.postmarks} postmarks, "
                          f"wear {game.state.wear}")
        return game

    def test_ten_days_never_reach_a_dead_end(self):
        totals = []
        for node, user in (("relink-test", 12), ("another-node", 5), ("third", 77)):
            game = self.career(node, user)
            totals.append((game.deliveries, game.postmarks))
        self.assertGreater(sum(d for d, _ in totals), 0, f"somebody delivered something: {totals}")

    def test_a_pilot_with_nothing_at_all_still_flies(self):
        game = self.game()
        self.empty(game)
        game.postmarks = 0
        game.state = dataclasses.replace(game.state, fuel=0, ballast=0, pibals=0, wear=fw.WEAR_GROUNDED)
        self.shop(game)
        self.assertIn("launch", game.offered())
        self.assertEqual(game.manifest[0]["payout"], fw.RELIEF["payout"], "with a mailbag to earn the next fuel by")
        self.assertEqual(game.state.fuel, fw.RELIEF["fuel"])


if __name__ == "__main__":
    unittest.main()
