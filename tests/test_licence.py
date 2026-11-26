"""The licence and the balloon (S13): grades, points, gear and what it does, and the envelope's design."""

import dataclasses
import json
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
from test_post import PostCase                            # noqa: E402


class LicenceCase(PostCase):
    def rich(self, game, postmarks=5000, points=5000):
        game.postmarks, game.points = postmarks, points
        return game

    def aloft(self, game, **changes):
        """The balloon in the air at its start, mid-morning, with a full day ahead."""
        low = game.engine.lowest_open(game.state.x, game.state.y)
        changes.setdefault("layer", low)
        game.state = dataclasses.replace(game.state, status="aloft", turn=12, **changes)
        game._choose_default()
        return game


class Grades(LicenceCase):
    def test_the_ladder(self):
        self.assertEqual(len(fw.GRADES), 7)
        self.assertEqual([name for name, _ in fw.GRADES][0::6], ["Fledgling", "Commodore of the Post"])
        needs = [need for _, need in fw.GRADES]
        self.assertEqual(needs, sorted(set(needs)))
        self.assertEqual(needs[0], 0)
        for i, need in enumerate(needs):
            self.assertEqual(fw.grade_of(need), i)
            self.assertEqual(fw.grade_of(need - 1), max(0, i - 1))

    def test_points_come_from_deliveries_clean_landings_and_distance(self):
        game = self.game()
        self.assertEqual((game.score(), game.grade()), (0, 0))
        item = game.manifest[0]
        game.key("ENTER")                                # a real launch: the flight has somewhere it began
        self.land_at(game, tuple(item["dest"]), fuel=30)
        self.assertEqual(game.points, fw.POINTS["mail"] + fw.POINTS["clean"])
        game.total_km = 250.0
        self.assertEqual(game.score(), game.points + 25, "a point for every ten kilometres")
        game.state = dataclasses.replace(game.state, distance=49.0)
        self.assertEqual(game.score(), game.points + 29)
        self.assertEqual(support.stored(self.folder)["points"], game.points)

    def test_hopping_up_and_down_earns_nothing(self):
        game = self.game()
        self.empty(game)
        home = game.here()
        for _ in range(4):
            game.key("ENTER")
            self.assertEqual(game.state.status, "aloft")
            self.land_at(game, home, fuel=30)            # down again where it went up
            self.assertEqual(game.state.status, "ground")
            game.state = dataclasses.replace(game.state, turn=0)
        self.assertEqual(game.points, 0)
        game.key("ENTER")
        far = min((ref for ref in fw.places_near(game.world, game.state.x, game.state.y, skip=home)),
                  key=lambda ref: 0)
        self.land_at(game, far, fuel=30)
        self.assertEqual(game.points, fw.POINTS["clean"], "a flight that went somewhere and came down clean does")
        game.close()
        again = self.game()
        self.assertEqual(again.lift_off, game.lift_off, "and where a flight began is kept with it")

    def test_a_rough_landing_earns_no_landing_point(self):
        game = self.game()
        self.empty(game)
        self.land_at(game, ("town", 1), kind="rough", fuel=30)
        self.assertEqual(game.points, 0)

    def test_every_grade_is_reached_by_playing(self):
        """A script carries post, the best its licence allows, until it is Commodore: no grade is out of reach."""
        game = self.game()
        seen, flights = [0], 0
        while game.grade() < len(fw.GRADES) - 1 and flights < 400:
            self.empty(game)
            home = ("town", game.world.start_town)
            if game.here() != home:
                place = game.place_of(home)
                game.state = dataclasses.replace(game.state, x=place["x"], y=place["y"], status="ground", layer=0, turn=0)
            game.state = dataclasses.replace(game.state, fuel=60, wear=0)
            game.taken = []
            taken = [o for o in game.offers() if game.take(o).startswith("Aboard")]
            self.assertTrue(taken, "there is always something a pilot of any grade may carry")
            before = game.grade()
            self.land_at(game, tuple(taken[0]["dest"]), fuel=40)
            game._night()
            flights += 1
            if game.grade() != before:
                self.assertEqual(game.grade(), before + 1, "one grade at a time")
                seen.append(game.grade())
                self.assertIn(f"The Post makes you a", " ".join(game.heard + game.log))
        self.assertEqual(seen, list(range(len(fw.GRADES))), f"after {flights} flights")
        self.assertIn("Commodore of the Post", self.loft_text(game))

    def loft_text(self, game):
        place = game.place_of(("town", game.world.start_town))
        game.state = dataclasses.replace(game.state, x=place["x"], y=place["y"], status="ground", layer=0)
        game.loft = True
        return self.screen(game)

    def test_a_promotion_is_said_once(self):
        game = self.game()
        game.points = fw.GRADES[1][1] - 1
        game._promote()
        self.assertEqual(game._grade_seen, 0)
        game.points += 1
        game._promote()
        self.assertIn("The Post makes you an Aeronaut.", game.log[-1])
        said = list(game.log)
        game._promote()
        self.assertEqual(game.log, said)
        game.save()
        game.close()
        again = self.game()
        self.assertEqual(again._grade_seen, 1, "and not again at the next call")

    def test_cargo_is_entrusted_by_grade(self):
        game = self.game()
        self.empty(game)
        offers = {}
        for day in range(1, 40):
            for offer in fw.contracts_at(game.world, game.here(), day):
                offers.setdefault(offer["kind"], offer)
        self.assertEqual(set(offers), set(fw.CARGO_KINDS))
        self.assertIn("Aboard", game.take(offers["mail"]))
        self.assertEqual(game.take(offers["parcel"]), "That is entrusted to a licence of Aeronaut or better.")
        self.assertEqual(game.take(offers["fragile"]), "That is entrusted to a licence of Pilot or better.")
        game.points = fw.GRADES[1][1]
        self.assertIn("Aboard", game.take(offers["parcel"]))
        game.points = fw.GRADES[2][1]
        self.assertIn("Aboard", game.take(offers["fragile"]))

    def test_a_town_always_has_a_mailbag_for_a_fledgling(self):
        world = self.game().world
        for index in range(len(world.towns)):
            for day in range(1, 60):
                self.assertEqual(fw.contracts_at(world, ("town", index), day)[0]["kind"], "mail")

    def test_a_pilot_from_before_the_licence_is_credited_with_their_deliveries(self):
        game = self.game()
        game.deliveries = 9
        game.save()
        game.close()
        import sqlite3
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        career = json.loads(db.execute("SELECT career FROM pilots").fetchone()[0])
        for key in ("points", "gear", "design"):
            del career[key]
        db.execute("UPDATE pilots SET career=?", (json.dumps(career),))
        db.commit()
        db.close()
        again = self.game()
        self.assertTrue(again.saved_ok)
        self.assertEqual((again.points, again.grade()), (9 * fw.POINTS["mail"], 1))
        self.assertEqual(again.gear, {track: 0 for track in fw.TRACKS})
        self.assertEqual(again.design, fw.FIRST_DESIGN)


class Gear(LicenceCase):
    def test_every_track_and_tier_is_in_the_table(self):
        self.assertEqual(set(fw.TRACKS), {"envelope", "burner", "tanks", "instruments", "wireless", "night", "oxygen", "camera"})
        for track in fw.TRACKS:
            tiers = fw.UPGRADES[track]["tiers"]
            self.assertNotIn("price", tiers[0], "the first tier is what a balloon starts with")
            for tier in tiers[1:]:
                self.assertGreater(tier["price"], 0)
                self.assertIn(tier["grade"], range(len(fw.GRADES)))
            for tier in tiers:
                self.assertTrue(fw.gear_text(track, tier))
        self.assertEqual([t["tank"] for t in fw.UPGRADES["tanks"]["tiers"]], [60, 90, 130])
        self.assertEqual([t["horizon"] for t in fw.UPGRADES["instruments"]["tiers"]], [6, 10, 16])

    def test_buying_needs_the_grade_the_postmarks_and_sometimes_other_gear(self):
        game = self.game()
        self.assertEqual(game.fit("tanks"), "Tanks: sold to Aeronauts.")
        game.points = fw.GRADES[1][1]
        self.assertEqual(game.fit("tanks"), "Tanks: not enough postmarks.")
        game.postmarks = 100
        self.assertEqual(game.fit("tanks"), "Fitted: Tanks, hold 90 fuel.")
        self.assertEqual((game.gear["tanks"], game.postmarks), (1, 0))
        self.assertEqual(support.stored(self.folder)["gear"]["tanks"], 1)
        self.rich(game)
        self.assertEqual(game.fit("oxygen"), "Oxygen: needs Envelope III.")
        game.fit("envelope")
        game.fit("envelope")
        self.assertIn("Fitted: Oxygen", game.fit("oxygen"))
        self.assertEqual(game.fit("oxygen"), "Oxygen: nothing more to fit.")
        self.assertEqual(game.fit("envelope"), "Envelope: nothing more to fit.")

    def test_the_envelope_lifts_more_and_climbs_higher(self):
        game = self.rich(self.game())
        self.assertEqual((game.lift(), game.state.ceiling, game.engine.top_layer(game.state)), (8, 3, 3))
        game.fit("envelope")
        self.assertEqual((game.lift(), game.engine.top_layer(game.state)), (11, 4))
        game.fit("envelope")
        self.assertEqual((game.lift(), game.engine.top_layer(game.state)), (14, 5))
        self.empty(game)
        for _ in range(14):
            game._load_cargo({"contract": None, "kind": "mail", "dest": ["town", 1], "origin": None, "weight": 1,
                              "payout": 1, "due": 9})
        self.assertEqual(game.weight(), 14)
        offer = game.offers()[0]
        self.assertIn("Too heavy: the balloon lifts 14", game.take(offer))
        self.aloft(game, layer=3)
        self.assertIn("climb", game.offered(), "above the first envelope's ceiling")

    def test_the_burner_makes_a_climb_cheaper(self):
        game = self.rich(self.game())
        costs = []
        for _ in range(3):
            self.aloft(game, layer=1)
            costs.append(game.offered()["climb"])
            game.state = dataclasses.replace(game.state, status="ground", layer=0, turn=0)
            game.fit("burner")
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertEqual(len(set(costs)), 3, f"each tier is cheaper: {costs}")

    def test_the_tanks_hold_more(self):
        game = self.rich(self.game())
        game.key("o")
        for tank in (60, 90, 130):
            self.assertEqual(game.tank(), tank)
            game.buy("fuel")
            self.assertEqual(game.state.fuel, tank)
            self.assertIn(f"Fuel {tank}/{tank}", self.screen(game))
            game.fit("tanks")

    def test_the_instruments_look_further_ahead_and_read_more_layers(self):
        game = self.rich(self.game())
        lengths, reads = [], []
        for _ in range(3):
            self.aloft(game, layer=2, ceiling=5)
            lengths.append(len(game.preview("hold")["points"]))
            winds = game.layer_winds()
            reads.append(sorted(layer for layer, wind in winds.items() if wind[2]))
            game.state = dataclasses.replace(game.state, status="ground", layer=0, turn=0)
            game.fit("instruments")
        self.assertEqual(lengths, [6, 10, 16])
        self.assertEqual(reads[0], [2])
        self.assertEqual(reads[1], [1, 2, 3])
        self.assertEqual(reads[2], list(range(1, fw.LAYERS + 1)))

    def test_a_layer_the_instruments_read_is_previewed_from_the_truth(self):
        game = self.rich(self.game())
        home = (game.state.x, game.state.y)
        spots = [(home[0] + dx, home[1] + dy) for dx in range(-300, 301, 50) for dy in range(-300, 301, 50)]
        spot = min((p for p in spots if game.engine.lowest_open(*p) <= 2), key=lambda p: game.coverage(*p))
        away = dict(x=spot[0], y=spot[1])                # far enough from any place for a forecast to err
        self.aloft(game, layer=2, **away)
        self.assertLess(game.coverage(game.state.x, game.state.y), 0.6)
        guessed = game.preview("climb")["points"][0]
        game.state = dataclasses.replace(game.state, status="ground", layer=0, turn=0, x=home[0], y=home[1])
        game.fit("instruments")
        self.aloft(game, layer=2, **away)
        frame = game.weather.frame(game.state.day, game.state.minute)
        vx, vy = frame.wind(game.state.x, game.state.y, 3)
        true = (game.state.x + vx * fw.TURN_HOURS, game.state.y + vy * fw.TURN_HOURS)
        read = game.preview("climb")["points"][0]
        self.assertAlmostEqual(read[0], true[0], places=6)
        self.assertAlmostEqual(read[1], true[1], places=6)
        self.assertNotAlmostEqual(guessed[0], true[0], places=6, msg="before, it was a forecast")

    def test_the_wireless_sharpens_the_forecast_further_from_a_place(self):
        game = self.rich(self.game())
        x, y = game.state.x + 200.0, game.state.y
        near = min(((x - p["x"]) ** 2 + (y - p["y"]) ** 2) ** 0.5 for p in game.world.towns + game.world.stations)
        before = game.coverage(x, y)
        game.fit("wireless")
        after = game.coverage(x, y)
        self.assertGreater(after, before)
        self.assertAlmostEqual(after, max(0.0, 1.0 - near / 300.0))

    def test_night_gear(self):
        game = self.rich(self.game())
        night = dict(turn=60)                            # 21:00
        self.aloft(game, layer=1)
        game.state = dataclasses.replace(game.state, **night)
        before = game.offered()["hold"]
        game.state = dataclasses.replace(game.state, status="ground", layer=0, turn=0)
        game.fit("night")
        self.assertTrue(game.state.night_gear)
        self.aloft(game, layer=1)
        game.state = dataclasses.replace(game.state, **night)
        self.assertLess(game.offered()["hold"], before, "no night fuel penalty")

    def test_oxygen_opens_the_top_layer(self):
        game = self.rich(self.game())
        game.fit("envelope")
        game.fit("envelope")
        self.assertEqual(game.engine.top_layer(game.state), fw.LAYERS - 1)
        game.fit("oxygen")
        self.assertEqual(game.engine.top_layer(game.state), fw.LAYERS)

    def test_gear_survives_the_night_and_the_next_call(self):
        game = self.rich(self.game())
        for track in fw.TRACKS:
            game.fit(track)
        fitted = (game.state.ceiling, game.state.climb_cost, game.state.night_gear, game.state.reads)
        game.key("n")
        self.assertEqual((game.state.ceiling, game.state.climb_cost, game.state.night_gear, game.state.reads), fitted)
        game.close()
        again = self.game()
        self.assertEqual(again.gear, game.gear)
        self.assertEqual((again.state.ceiling, again.state.climb_cost, again.state.night_gear, again.state.reads), fitted)
        self.assertEqual((again.tank(), again.lift()), (90, 11))

    def test_gear_that_is_not_in_the_table_is_a_damaged_record(self):
        game = self.game()
        game.gear["tanks"] = 9
        game.save()
        game.close()
        again = self.game()
        self.assertFalse(again.saved_ok)


class Loft(LicenceCase):
    def test_the_loft_is_in_towns_and_reached_from_the_ground_or_the_post_office(self):
        game = self.game()
        game.key("g")
        self.assertTrue(game.loft)
        game.key("ENTER")
        self.assertTrue(game.loft, "Enter does not leave the loft")
        game.key("b")
        self.assertFalse(game.loft)
        game.key("o")
        self.assertIn("[G] Rigging loft", self.screen(game))
        game.key("g")
        self.assertEqual((game.hub, game.loft), (False, True))
        game.key("ESC")
        station = game.world.stations[0]
        game.state = dataclasses.replace(game.state, x=station["x"], y=station["y"])
        game.key("g")
        self.assertFalse(game.loft, "a weather station has no loft")
        game.key("o")
        self.assertNotIn("Rigging loft", self.screen(game))
        game.key("g")
        self.assertFalse(game.loft)

    def test_the_loft_fits_every_layout(self):
        game = self.rich(self.game(), postmarks=260, points=400)
        game.key("g")
        game.key("1")
        for size in ((80, 24), (100, 30), (132, 40), (168, 46), (250, 70), (80, 60)):
            text = self.screen(game, *size)
            self.assertIn("LICENCE  Navigator", text)
            self.assertIn("1  Envelope    II     lifts 11, reaches layer 4", text)
            self.assertIn("next      III    450 pm  not enough postmarks", text)
            self.assertIn("next      fit    300 pm  sold to Wind-readers", text)
            self.assertIn("ENVELOPE  stripes in teal, cream, coral, cream", text)
            self.assertIn("[1-8] Fit the next", text)
        for changes in ({"color_depth": "16"}, {"charset": "cp437", "color_depth": "16"}, {"charset": "ascii"}):
            other = self.game(user_id=400 + len(changes), **changes)
            other.key("g")
            self.assertIn("ENVELOPE  stripes in teal, cream, coral, cream", self.screen(other))

    def test_keys_in_the_loft(self):
        game = self.rich(self.game())
        game.key("g")
        game.key("3")
        self.assertEqual(game.gear["tanks"], 1)
        self.assertIn("Fitted: Tanks", game.hub_note)
        game.key("9")
        game.key("w")
        self.assertEqual(sum(game.gear.values()), 1, "other keys do nothing")
        self.assertEqual(game.key("q"), "quit")


class Design(LicenceCase):
    def test_four_colours_and_a_pattern_are_chosen_and_kept(self):
        game = self.game()
        self.assertEqual(game.design, {"pattern": "stripes", "colours": [0, 1, 2, 1]})
        game.key("g")
        seen = []
        for _ in range(len(fw.PATTERNS)):
            game.key("p")
            seen.append(game.design["pattern"])
        self.assertEqual(sorted(seen), sorted(fw.PATTERNS))
        game.key("z")
        game.key("v")
        game.key("v")
        self.assertEqual(game.design["colours"], [1, 1, 2, 3])
        self.assertIn("in cream, cream, coral, navy", game.hub_note)
        for _ in range(len(fw.ENVELOPE_COLOURS)):
            game.key("x")
        self.assertEqual(game.design["colours"][1], 1, "the colours go round")
        self.assertEqual(support.stored(self.folder)["design"], game.design)
        game.close()
        self.assertEqual(self.game().design, game.design)

    def test_every_colour_reads_differently_in_sixteen(self):
        readings = [fw.nearest16(rgb) for _, rgb in fw.ENVELOPE_COLOURS]
        self.assertEqual(len(set(readings)), len(readings), readings)
        names = [name for name, _ in fw.ENVELOPE_COLOURS]
        self.assertEqual(len(set(names)), len(names))

    def test_a_design_that_cannot_be_used_is_the_first_design(self):
        for bad in (None, {}, {"pattern": "plaid", "colours": [0, 1, 2, 3]}, {"pattern": "rings", "colours": [0, 1]},
                    {"pattern": "rings", "colours": [0, 1, 2, 99]}, "stripes", {"pattern": "rings", "colours": "abcd"}):
            self.assertEqual(fw.design_of(bad), fw.FIRST_DESIGN)
        self.assertEqual(fw.design_of({"pattern": "rings", "colours": [7, 6, 5, 4]})["colours"], [7, 6, 5, 4])

    def colours_drawn(self, **design):
        screen = fw.Screen(40, 24)
        fw.draw_balloon(screen, 20, 20, 9.0, flame=False, colours=fw.design_rgb(fw.design_of(design)),
                        pattern=design["pattern"])
        return screen

    def test_each_pattern_paints_a_different_balloon_in_the_pilots_colours(self):
        pictures = {}
        for pattern in fw.PATTERNS:
            screen = self.colours_drawn(pattern=pattern, colours=[3, 4, 5, 6])
            pictures[pattern] = (tuple(screen.fg), tuple(screen.bg))
            reds = [c for c in screen.fg + screen.bg if c is not None and isinstance(c, tuple)]
            self.assertTrue(reds)
        self.assertEqual(len(set(pictures.values())), len(fw.PATTERNS))
        default = self.colours_drawn(pattern="stripes", colours=[0, 1, 2, 1])
        plain = fw.Screen(40, 24)
        fw.draw_balloon(plain, 20, 20, 9.0, flame=False)
        self.assertEqual((default.fg, default.bg), (plain.fg, plain.bg), "the first design is the balloon of the title")

    def test_the_design_is_shown_in_scenes(self):
        game = self.game()
        game.design = {"pattern": "rings", "colours": [3, 4, 5, 6]}
        game.key("ENTER")
        asked = game.pending_scene
        self.assertEqual(asked["design"], game.design)
        mine = fw.Scene("launch", 80, 24, True, True, design=asked["design"])
        other = fw.Scene("launch", 80, 24, True, True)
        a, b = fw.Screen(80, 24), fw.Screen(80, 24)
        mine.draw(a)
        other.draw(b)
        self.assertNotEqual((a.fg, a.bg), (b.fg, b.bg))
        session = fw.Session(game.launch, Keys(), Out(), game=game)
        session.scenes_on = True
        session._take_scene()
        self.assertEqual(session.scene.design, game.design)

    def test_the_loft_shows_the_balloon_where_there_is_room(self):
        game = self.game()
        game.key("g")
        wide = fw.Session(dataclasses.replace(game.launch, width=132, height=40), Keys(), Out(), game=game)
        wide.paint()
        self.assertIn("▀", wide.out.text)
        small = fw.Session(game.launch, Keys(), Out(), game=game)
        small.paint()
        self.assertNotIn("▀", small.out.text)


class Others(LicenceCase):
    def test_other_pilots_are_on_the_map_in_their_own_colours(self):
        first = self.game(user_id=1)
        first.design = {"pattern": "rings", "colours": [3, 0, 0, 0]}        # navy first
        first.key("ENTER")
        first.key("ENTER")
        where = (first.state.x, first.state.y)
        second = fw.PlainFlight(launch(user_id=2, timezone="UTC"), self.folder, now=self.clock)
        self.assertEqual(second.others, [(where[0], where[1], fw.ENVELOPE_COLOURS[3][1])])
        session = fw.Session(second.launch, Keys(), Out(), game=second)
        session.paint()
        cell = second.map.cell_of(*where)
        self.assertIsNotNone(cell)
        k = cell[1] * 80 + cell[0]
        self.assertEqual(session.frame.ch[k], "Q")
        mine = second.map.cell_of(second.state.x, second.state.y)
        self.assertEqual(session.frame.ch[mine[1] * 80 + mine[0]], "@", "and one's own balloon is still one's own")
        self.assertEqual(first.others, [], "the first has not looked since the second arrived")
        first._day_may_have_opened()
        self.assertEqual(len(first.others), 1)

    def test_in_sixteen_colours_the_marker_is_the_colours_reading(self):
        first = self.game(user_id=1)
        first.design = {"pattern": "stripes", "colours": [2, 0, 0, 0]}      # coral
        first.key("ENTER")
        second = fw.PlainFlight(launch(user_id=2, timezone="UTC", color_depth="16"), self.folder, now=self.clock)
        session = fw.Session(second.launch, Keys(), Out(), game=second)
        session.paint()
        self.assertIn("Q", session.out.text)
        self.assertEqual(fw.nearest16(second.others[0][2]), fw.nearest16(fw.ENVELOPE_COLOURS[2][1]))

    def test_a_visit_and_a_world_of_one_show_nobody(self):
        alone = self.game(user_id=1)
        self.assertEqual(alone.others, [])
        guest = fw.PlainFlight(launch(user_id=2 ** 62 + 3, timezone="UTC"), self.folder, now=self.clock)
        self.assertEqual(guest.others, [])

    def test_somebody_elses_broken_record_is_not_this_pilots_trouble(self):
        first = self.game(user_id=1)
        first.key("ENTER")
        first.close()
        import sqlite3
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        db.execute("UPDATE pilots SET career='{' WHERE user_id=1")
        db.commit()
        db.close()
        second = fw.PlainFlight(launch(user_id=2, timezone="UTC"), self.folder, now=self.clock)
        self.assertEqual(second.others, [])
        self.assertTrue(second.saved_ok)

    def test_the_latest_to_fly_are_the_ones_drawn(self):
        import sqlite3
        for user in (5, 9, 2):
            game = fw.PlainFlight(launch(user_id=user, timezone="UTC"), self.folder, now=self.clock)
            game.key("ENTER")
            game.close()
        db = sqlite3.connect(str(next(self.folder.glob("fair_winds-*.db"))))
        for user, at in ((5, 300.0), (9, 100.0), (2, 200.0)):
            db.execute("UPDATE turns SET at=? WHERE user_id=?", (at, user))
            db.execute("UPDATE flights SET state=json_set(state, '$.x', ?) WHERE user_id=?", (float(user), user))
        db.commit()
        db.close()
        store = fw.Store(self.folder, game.world.seed, "node")
        order = [json.loads(state)["x"] for _, state in store.others(77)]
        self.assertEqual(order, [5.0, 2.0, 9.0], "by when they last flew, not by their number")

    def test_numbers_that_are_not_numbers_in_another_record_break_nothing(self):
        import sqlite3
        first = self.game(user_id=1)
        first.key("ENTER")
        first.close()
        path = str(next(self.folder.glob("fair_winds-*.db")))
        for career, state in (('{"design": {"pattern": "rings", "colours": [Infinity, 0, 0, 0]}}', None),
                              (None, '{"x": Infinity, "y": 5.0}'), (None, '{"x": NaN, "y": NaN}'),
                              ('[1, 2]', None), (None, '"text"')):
            db = sqlite3.connect(path)
            if career is not None:
                db.execute("UPDATE pilots SET career=? WHERE user_id=1", (career,))
            if state is not None:
                db.execute("UPDATE flights SET state=? WHERE user_id=1", (state,))
            db.commit()
            db.close()
            second = fw.PlainFlight(launch(user_id=2, timezone="UTC"), self.folder, now=self.clock)
            self.assertTrue(second.saved_ok)
            for x, y, _ in second.others:
                self.assertTrue(x == x and y == y and abs(x) < 1e9)
            self.assertFalse(second._day_may_have_opened(), "and nothing keeps the screen repainting")
            second.close()

    def test_ones_own_record_with_such_numbers_is_damaged_not_a_crash(self):
        import sqlite3
        game = self.game()
        game.key("ENTER")
        game.close()
        path = str(next(self.folder.glob("fair_winds-*.db")))
        db = sqlite3.connect(path)
        whole = db.execute("SELECT career FROM pilots").fetchone()[0]
        db.close()
        for spoil in (lambda c: c.update(gear=[1, 2]), lambda c: c.update(points=float("inf")),
                      lambda c: c.update(postmarks=float("inf")), lambda c: c.update(lift_off=[float("nan")]),
                      lambda c: c.update(lift_off=[float("inf"), 0.0]), lambda c: c.update(lift_off=[0.0, float("nan")])):
            career = json.loads(whole)
            spoil(career)
            db = sqlite3.connect(path)
            db.execute("UPDATE pilots SET career=?", (json.dumps(career),))
            db.commit()
            db.close()
            again = self.game()
            self.assertFalse(again.saved_ok, "it is said to be unreadable, and it is not written over")
            again.close()

    def test_no_more_than_so_many_are_drawn(self):
        for user in range(1, fw.OTHERS_SHOWN + 6):
            game = fw.PlainFlight(launch(user_id=user, timezone="UTC"), self.folder, now=self.clock)
            game.close()
        last = fw.PlainFlight(launch(user_id=500, timezone="UTC"), self.folder, now=self.clock)
        self.assertEqual(len(last.others), fw.OTHERS_SHOWN)


if __name__ == "__main__":
    unittest.main()
