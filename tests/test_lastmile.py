"""The last stretch: the trail rope, the mail drop, an hour's wait, and cargo sent on by road."""

import dataclasses
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from test_post import PostCase                            # noqa: E402


class Case(PostCase):
    def aloft(self, game, layer=None, **changes):
        low = game.engine.lowest_open(changes.get("x", game.state.x), changes.get("y", game.state.y))
        game.state = dataclasses.replace(game.state, status="aloft", turn=12, layer=low if layer is None else layer,
                                         **changes)
        game._choose_default()
        return game

    def bag(self, game, dest, kind="mail", payout=50, due=9):
        return game._load_cargo({"contract": None, "kind": kind, "dest": list(dest), "origin": None,
                                 "weight": 1, "payout": payout, "due": due})


class Rope(Case):
    def test_the_rope_turns_the_course_thirty_degrees_and_slows_it(self):
        for vx, vy in ((10.0, 0.0), (0.0, -8.0), (-3.0, 4.0), (5.0, 5.0)):
            speed = math.hypot(vx, vy)
            for side in ("left", "right"):
                rx, ry = fw.roped(vx, vy, side)
                self.assertAlmostEqual(math.hypot(rx, ry), speed * fw.ROPE_SPEED)
                cos = (rx * vx + ry * vy) / (speed * speed * fw.ROPE_SPEED)
                self.assertAlmostEqual(cos, fw.ROPE_COS)
        east_left, east_right = fw.roped(10.0, 0.0, "left"), fw.roped(10.0, 0.0, "right")
        self.assertLess(east_left[1], 0, "heading east, left is north: up the map")
        self.assertGreater(east_right[1], 0)

    def test_the_rope_is_offered_in_the_lowest_layer_only(self):
        game = self.aloft(self.game())
        offered = game.offered()
        self.assertEqual((offered["left"], offered["right"]), (offered["hold"], offered["hold"]), "it costs a hold")
        game.state = dataclasses.replace(game.state, layer=game.state.layer + 1)
        self.assertNotIn("left", game.offered(), "the rope does not reach the ground from higher up")
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        self.assertNotIn("left", game.offered())
        game = self.aloft(game)
        game.state = dataclasses.replace(game.state, fuel=0)
        self.assertNotIn("left", game.offered(), "nor without the fuel to hold")

    def test_a_turn_on_the_rope_goes_where_the_rope_says(self):
        game = self.aloft(self.game())
        before = game.state
        frame = game.weather.frame(before.day, before.minute)
        vx, vy = frame.wind(before.x, before.y, before.layer)
        game.key("a")
        self.assertEqual(game.choice, "left")
        game.key("ENTER")
        rx, ry = fw.roped(vx, vy, "left")
        self.assertAlmostEqual(game.state.x, before.x + rx * fw.TURN_HOURS)
        self.assertAlmostEqual(game.state.y, before.y + ry * fw.TURN_HOURS)
        self.assertEqual((game.state.wear, game.state.turn), (before.wear + fw.ROPE_WEAR, before.turn + 1))
        self.assertIn("The rope is out: bearing left of the wind.", " ".join(game.heard))
        held = self.aloft(self.game(user_id=40))
        held.key("ENTER")
        self.assertAlmostEqual(math.hypot(held.state.x - before.x, held.state.y - before.y),
                               math.hypot(vx, vy) * fw.TURN_HOURS, msg="a plain hold goes with the wind")
        self.assertEqual(held.state.wear, before.wear)

    def test_left_and_right_end_either_side_of_the_wind(self):
        game = self.aloft(self.game())
        tracks = {action: game.preview(action)["points"] for action in ("left", "hold", "right")}
        self.assertTrue(all(tracks.values()))
        ends = {action: points[-1][:2] for action, points in tracks.items()}
        self.assertNotEqual(ends["left"], ends["right"])
        spread = math.hypot(ends["left"][0] - ends["right"][0], ends["left"][1] - ends["right"][1])
        run = math.hypot(ends["hold"][0] - game.state.x, ends["hold"][1] - game.state.y)
        self.assertGreater(spread, run * 0.4, "enough to choose between: about a kilometre aside for every two ahead")
        first = tracks["left"][0]
        frame = game.weather.frame(game.state.day, game.state.minute)
        rx, ry = fw.roped(*frame.wind(game.state.x, game.state.y, game.state.layer), "left")
        self.assertAlmostEqual(first[0], game.state.x + rx * fw.TURN_HOURS, msg="the track's first step is the true one")

    def test_the_screen_shows_the_rope(self):
        game = self.aloft(self.game())
        text = self.screen(game)
        self.assertIn("[A/D] Rope", text)
        game.key("d")
        text = self.screen(game)
        self.assertIn("Rope out, bear right in layer", text)
        game.key("RIGHT")
        game.key("LEFT")
        self.assertEqual(game.choice, "left")
        game.key("w")
        self.assertNotIn(game.choice, ("left", "right"), "W and S go back to the layers")
        for size in ((80, 24), (132, 40), (168, 46)):
            self.screen(game, *size)

    def test_the_rope_can_close_a_gap_the_wind_alone_would_not(self):
        """The case that prompted it: sixteen kilometres off, and every layer carries the balloon past."""
        game = self.aloft(self.game())
        state = game.state
        frame = game.weather.frame(state.day, state.minute)
        vx, vy = frame.wind(state.x, state.y, state.layer)
        speed = math.hypot(vx, vy)
        # A place sixteen km off, twenty-five degrees to the left of where the wind is going.
        c, s_ = math.cos(math.radians(25)), math.sin(math.radians(25))
        tx = state.x + (vx * c + vy * s_) / speed * 16.0
        ty = state.y + (-vx * s_ + vy * c) / speed * 16.0

        def nearest(action):
            best = math.hypot(tx - state.x, ty - state.y)
            for x, y, _ in game.preview(action)["points"]:
                best = min(best, math.hypot(tx - x, ty - y))
            return best
        self.assertLess(nearest("left"), nearest("hold"))
        self.assertLess(nearest("left"), fw.PLACE_RADIUS_KM, "on the rope it comes within reach of the place")


class Drop(Case):
    def over(self, game, ref, km):
        place = game.place_of(ref)
        return self.aloft(game, x=place["x"] + km, y=place["y"])

    def test_mail_is_dropped_within_thirty_km_flying_low(self):
        game = self.game()
        self.empty(game)
        ref = ("town", 2)
        item = self.bag(game, ref, payout=50)
        self.bag(game, ref, kind="parcel")
        self.bag(game, ("town", 3))
        self.over(game, ref, fw.DROP_KM + 2)
        self.assertEqual(game.droppable(), [])
        self.over(game, ref, fw.DROP_KM - 2)
        self.assertEqual([i["id"] for i in game.droppable()], [item["id"]], "the mailbag for here, not the parcel")
        self.assertIn("[M] Drop", self.screen(game))
        before, turn = game.postmarks, game.state.turn
        game.key("m")
        self.assertEqual(game.postmarks, before + int(50 * fw.DROP_SHARE))
        self.assertEqual((game.deliveries, game.points, game.state.turn), (1, fw.POINTS["mail"], turn), "and no turn passes")
        self.assertEqual(len(game.manifest), 2)
        self.assertEqual(len(game.state.cargo), 2)
        self.assertIn("Dropped over", " ".join(game.log))
        self.assertEqual(support.stored(self.folder)["postmarks"], game.postmarks, "saved at once")
        game.key("m")
        self.assertEqual(game.postmarks, before + int(50 * fw.DROP_SHARE), "nothing to drop twice")

    def test_not_from_high_up_and_not_from_the_ground(self):
        game = self.game()
        self.empty(game)
        ref = ("town", 2)
        self.bag(game, ref)
        self.over(game, ref, 5.0)
        low = game.engine.lowest_open(game.state.x, game.state.y)
        game.state = dataclasses.replace(game.state, layer=low + fw.DROP_ABOVE)
        self.assertTrue(game.droppable())
        game.state = dataclasses.replace(game.state, layer=low + fw.DROP_ABOVE + 1, ceiling=5)
        self.assertEqual(game.droppable(), [])
        game.key("m")
        self.assertEqual(len(game.manifest), 1)
        game.state = dataclasses.replace(game.state, status="ground", layer=0)
        self.assertEqual(game.droppable(), [])

    def test_a_late_bag_dropped_pays_less_again(self):
        game = self.game()
        self.empty(game)
        ref = ("town", 2)
        self.bag(game, ref, payout=50, due=1)
        self.over(game, ref, 5.0)
        game.state = dataclasses.replace(game.state, day=3)
        game.today = 3
        before = game.postmarks
        game.key("m")
        self.assertEqual(game.postmarks, before + int(int(50 * fw.DROP_SHARE) * fw.LATE_SHARE))

    def test_landing_still_pays_more_than_dropping(self):
        self.assertLess(fw.DROP_SHARE, 1.0)
        self.assertGreater(fw.DROP_SHARE, fw.FORWARD_SHARE)
        self.assertGreater(fw.DROP_KM, fw.PLACE_RADIUS_KM)


class Wait(Case):
    def test_an_hour_on_the_ground_costs_only_the_hour(self):
        game = self.game()
        before = game.state
        self.assertIn("[H] Wait", self.screen(game, 132, 40))
        game.key("h")
        self.assertEqual(game.state.turn, before.turn + fw.WAIT_TURNS)
        self.assertEqual((game.state.fuel, game.state.wear, game.state.x, game.state.status),
                         (before.fuel, before.wear, before.x, "ground"))
        self.assertIn("07:00  An hour on the ground.", " ".join(game.log))
        self.assertEqual(support.stored(self.folder)["state"]["turn"], fw.WAIT_TURNS)
        self.assertIn("launch", game.offered())

    def test_the_wind_is_another_wind_an_hour_on(self):
        game = self.game()
        seen = set()
        for _ in range(8):
            winds = game.layer_winds()
            seen.add(tuple(round(v, 3) for wind in winds.values() if wind for v in wind[:2]))
            game.key("h")
        self.assertGreater(len(seen), 4)

    def test_waiting_stops_at_midnight_and_not_aloft(self):
        game = self.game()
        game.state = dataclasses.replace(game.state, turn=fw.DAY_TURNS - 2)
        game.key("h")
        self.assertEqual(game.state.turn, fw.DAY_TURNS)
        game.key("h")
        self.assertEqual(game.state.turn, fw.DAY_TURNS)
        self.assertNotIn("[H] Wait", self.screen(game, 132, 40))
        flying = self.aloft(self.game(user_id=41))
        turn = flying.state.turn
        flying.key("h")
        self.assertEqual(flying.state.turn, turn)


class Forward(Case):
    def test_the_road_pays_for_the_part_of_the_way_it_was_carried(self):
        game = self.game()
        self.empty(game)
        home = game.here()
        far = fw.places_near(game.world, game.state.x, game.state.y, skip=home)[4]
        item = self.bag(game, far, payout=100)
        self.assertGreater(item["km"], 50)
        self.assertEqual(game.forwardable(), [], "at the counter it came aboard at, nothing")
        self.assertIn("The road pays nothing", game.forward())
        self.assertEqual(len(game.manifest), 1)
        there = game.place_of(far)
        between = min((ref for ref in fw.places_near(game.world, there["x"], there["y"], skip=far)
                       if ref != home), key=lambda ref: 0)
        place = game.place_of(between)
        game.state = dataclasses.replace(game.state, x=place["x"], y=place["y"])
        left = math.hypot(there["x"] - place["x"], there["y"] - place["y"])
        want = int(100 * fw.FORWARD_SHARE * max(0.0, 1 - left / item["km"]))
        if want <= 0:
            self.skipTest("the nearest other office is no nearer than home in this world")
        self.assertEqual(game.forwardable(), [(game.manifest[0], want)])
        game.key("o")
        self.assertIn(f"W sends on by road what is bound elsewhere: {want} postmarks", self.screen(game))
        before, deliveries, points = game.postmarks, game.deliveries, game.points
        game.key("w")
        self.assertEqual((game.postmarks, game.manifest, game.state.cargo), (before + want, [], ()))
        self.assertEqual((game.deliveries, game.points), (deliveries, points), "paid, but it is not a delivery")
        self.assertIn("Sent on by road", game.hub_note)
        self.assertEqual(support.stored(self.folder)["postmarks"], game.postmarks)

    def test_never_more_than_half_and_never_for_going_backwards(self):
        game = self.game()
        self.empty(game)
        home = game.here()
        near = fw.places_near(game.world, game.state.x, game.state.y, skip=home)
        item = self.bag(game, near[0], payout=100)
        for ref in near[1:]:
            place = game.place_of(ref)
            game.state = dataclasses.replace(game.state, x=place["x"], y=place["y"])
            for _, pay in game.forwardable():
                self.assertLessEqual(pay, int(100 * fw.FORWARD_SHARE))
            there = game.place_of(near[0])
            if math.hypot(there["x"] - place["x"], there["y"] - place["y"]) >= item["km"]:
                self.assertEqual(game.forwardable(), [], "further off than it started earns nothing")

    def test_cargo_for_here_is_delivered_not_sent_on(self):
        game = self.game()
        self.empty(game)
        self.bag(game, game.here(), payout=100)
        self.assertEqual(game.forwardable(), [])

    def test_taking_a_contract_and_sending_it_straight_on_earns_nothing(self):
        game = self.game()
        self.empty(game)
        before = game.postmarks
        game.key("o")
        game.key("1")
        game.key("w")
        self.assertEqual(game.postmarks, before)
        self.assertEqual(len(game.manifest), 1)

    def test_cargo_from_before_this_rule_can_still_be_delivered(self):
        game = self.game()
        game.manifest = [{k: v for k, v in item.items() if k != "km"} for item in game.manifest]
        game.save()
        game.close()
        again = self.game()
        self.assertTrue(again.saved_ok)
        self.assertEqual([item["km"] for item in again.manifest], [None] * 3)
        self.assertEqual(again.forwardable(), [])
        self.land_at(again, tuple(again.manifest[0]["dest"]), fuel=30)
        self.assertEqual(again.deliveries, 1)


class Keys(Case):
    def test_the_key_line_always_fits(self):
        game = self.game()
        self.empty(game)
        ref = ("town", 2)
        self.bag(game, ref)
        place = game.place_of(ref)
        self.aloft(game, x=place["x"] + 14.0, y=place["y"])
        offered = game.offered()
        self.assertIn("left", offered)
        text = self.screen(game)
        line = text.splitlines()[23] if len(text.splitlines()) > 23 else text.splitlines()[-1]
        for want in ("[W/S] Layer", "[A/D] Rope", "[Enter] Fly", "[M] Drop", "[L] Land", "[Q] Quit"):
            self.assertIn(want, line)
        wide = self.screen(game, 132, 40).splitlines()[-1]
        for want in ("[J] Jettison", "[P] Pibal", "[?] Help"):
            self.assertIn(want, wide, "everything, where there is room")

    def test_fitting_drops_the_least_needed_first(self):
        keys = [("Enter", "Launch"), ("O", "Post"), ("H", "Wait"), ("N", "Night"), ("?", "Help"),
                ("Q", "Return to A Node With A Long Name")]
        fit = fw.PlainFlight._fit_keys(keys, 78)
        self.assertIn(("Q", "Leave"), fit)
        self.assertIn(("?", "Help"), fit)
        self.assertEqual(fw.PlainFlight._fit_keys(keys, 200), keys)
        tight = fw.PlainFlight._fit_keys(keys, 40)
        self.assertNotIn(("?", "Help"), tight)
        self.assertIn(("Enter", "Launch"), tight)


if __name__ == "__main__":
    unittest.main()
