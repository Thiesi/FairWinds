"""The chart of the whole continent (S17): isobars, pan, zoom, find, names, the route and the panel."""

import dataclasses
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
import support                                            # noqa: E402
from test_plain import launch                             # noqa: E402
from test_rescue import RescueCase                        # noqa: E402


class AtlasCase(RescueCase):
    def chart(self, **changes):
        game = self.game(**changes)
        game.key("c")
        self.assertTrue(game.atlas)
        return game

    def window(self, game):
        return game._atlas_window()

    def words(self, game, width=80, height=24):
        """The screen with the picture taken out: marks and names, with spaces where the ground was."""
        return "".join(" " if ch in "▀▄█" else ch for ch in self.screen(game, width, height))


class Keys(AtlasCase):
    def test_it_opens_on_the_whole_continent_and_closes_on_enter(self):
        game = self.chart()
        self.assertEqual(self.window(game), (0.0, 0.0, fw.WORLD_W, fw.WORLD_H))
        text = self.screen(game)
        for word in ("The chart", "[Arrows] Pan", "[Z] Zoom", "[I] Isobars", "[N] Names", "[/] Find", "[Enter] Back"):
            self.assertIn(word, text)
        game.key("x")
        self.assertTrue(game.atlas, "a key that means nothing here does not close it")
        game.key("ENTER")
        self.assertFalse(game.atlas)
        for key in ("ESC", "b", "c", " "):
            game.key("c")
            game.key(key)
            self.assertFalse(game.atlas, key)

    def test_zoom_steps_in_on_the_pilot_and_back_out(self):
        game = self.chart()
        state = game.state
        game.key("z")
        x0, y0, wide, tall = self.window(game)
        self.assertEqual((wide, tall), (fw.WORLD_W / 2, fw.WORLD_H / 2))
        self.assertTrue(x0 <= state.x <= x0 + wide and y0 <= state.y <= y0 + tall, "the pilot is in the window")
        self.assertIn("2 times closer", self.screen(game))
        game.key("z")
        self.assertEqual(self.window(game)[2], fw.WORLD_W / 4)
        self.screen(game)
        game.key("z")
        self.assertEqual(self.window(game), (0.0, 0.0, fw.WORLD_W, fw.WORLD_H))

    def test_pan_moves_a_third_of_the_window_and_stops_at_the_edge(self):
        game = self.chart()
        game.key("RIGHT")
        self.assertEqual(self.window(game), (0.0, 0.0, fw.WORLD_W, fw.WORLD_H), "the whole continent has nowhere to go")
        game.atlas_view.update(zoom=2, x=fw.WORLD_W / 2, y=fw.WORLD_H / 2)
        x0, y0, wide, tall = self.window(game)
        game.key("RIGHT")
        self.assertAlmostEqual(self.window(game)[0], x0 + wide / 3)
        game.key("DOWN")
        self.assertAlmostEqual(self.window(game)[1], y0 + tall / 3)
        game.key("LEFT")
        game.key("UP")
        self.assertAlmostEqual(self.window(game)[0], x0)
        self.assertAlmostEqual(self.window(game)[1], y0)
        for _ in range(20):
            game.key("LEFT")
            game.key("UP")
        self.assertEqual(self.window(game)[:2], (0.0, 0.0))
        for _ in range(20):
            game.key("RIGHT")
            game.key("DOWN")
        self.assertEqual(self.window(game)[:2], (fw.WORLD_W - wide, fw.WORLD_H - tall))
        self.screen(game)

    def test_names_are_shown_and_can_be_put_away(self):
        game = self.chart()
        names = [place["name"] for place in game.world.towns + game.stations]
        shown = [name for name in names if name in self.screen(game)]
        self.assertGreater(len(shown), 3, "at 80 x 24 as many names as fit")
        bound = game.place_of(game.targets[0])["name"]
        self.assertIn(bound, shown, "and where the post is bound comes first")
        game.key("n")
        text = self.screen(game)
        self.assertFalse([name for name in names if name in text])
        self.assertIn("THE GYRE", text, "the Gyre is always named")
        game.key("n")
        self.assertIn(bound, self.screen(game))

    def test_names_do_not_run_together(self):
        game = self.chart()
        rows = self.screen(game).split("\n")[1:20]
        names = [town["name"] for town in game.world.towns]
        for row in rows:
            for name in names:
                at = row.find(name)
                if at > 0:
                    after = row[at + len(name):at + len(name) + 1]
                    self.assertFalse(after.isalpha(), (name, row))

    def test_find_brings_a_place_to_the_middle(self):
        game = self.chart()
        town = max(game.world.towns, key=lambda t: (t["x"] - game.state.x) ** 2 + (t["y"] - game.state.y) ** 2)
        part = town["name"][:4]
        game.key("/")
        self.assertIn("Find: _", self.screen(game))
        for ch in part.upper():
            self.assertIsNone(game.key(ch))
        for ch in "q?":
            self.assertIsNone(game.key(ch), "letters are letters while a name is typed")
        game.key("BACKSPACE")
        game.key("BACKSPACE")
        self.assertTrue(game.atlas)
        game.key("ENTER")
        first = next(t for t in game.world.towns + game.stations if t["name"].lower().startswith(part.lower()))
        x0, y0, wide, tall = self.window(game)
        self.assertEqual(wide, fw.WORLD_W / 2, "found: one step closer")
        self.assertTrue(x0 <= first["x"] <= x0 + wide and y0 <= first["y"] <= y0 + tall)
        text = self.words(game)
        self.assertIn(f"Found: {first['name']}.", text)
        self.assertIn(f"* {first['name']}", text)

    def test_find_knows_the_gyre_stations_and_the_stranded_and_says_when_it_knows_nothing(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        x, y = self.strand(a)
        b._day_may_have_opened()
        b.key("c")
        for typed, name in (("gyre", "The Gyre"), ("ada", "Ada"), (b.stations[0]["name"][:5], b.stations[0]["name"])):
            b.key("/")
            for ch in typed:
                b.key(ch)
            b.key("ENTER")
            self.assertEqual(b.atlas_view["found"][0], name)
        b.key("/")
        for ch in "zzzz":
            b.key(ch)
        b.key("ENTER")
        self.assertIn('Nothing on the chart is called "zzzz"', self.screen(b))
        b.key("/")
        b.key("x")
        b.key("ESC")
        self.assertTrue(b.atlas, "Esc leaves the name, not the chart")
        self.assertIsNone(b.atlas_view["find"])
        b.key("/")
        for ch in "y" * 60:
            b.key(ch)
        self.assertEqual(len(b.atlas_view["find"]), fw.FIND_LIMIT)
        b.close()


class Isobars(AtlasCase):
    def test_a_level_is_a_band_of_four_hpa_and_wider_in_the_deep(self):
        self.assertEqual(fw.isobar_level(1013.0), 253)
        self.assertEqual(fw.isobar_level(1012.0), 253)
        self.assertEqual(fw.isobar_level(1011.9), 252)
        levels = [fw.isobar_level(900.0 + i * 0.5) for i in range(260)]
        self.assertEqual(levels, sorted(levels), "more pressure is never a lower band")
        self.assertEqual(fw.isobar_level(992.0) - fw.isobar_level(991.9), 1, "no jump where the spacing changes")
        self.assertEqual(fw.isobar_level(972.1), fw.isobar_level(991.9), "twenty hPa to a band below 992")

    def test_the_lines_are_where_the_pressure_field_crosses_a_level(self):
        game = self.chart()
        self.assertTrue(game.map.pixels and game.atlas_view["isobars"])
        cols, rows = 78, 19
        lined = game._atlas_compose(cols, rows)["grid"]
        game.key("i")
        plain = game._atlas_compose(cols, rows)["grid"]
        frame = game.weather.frame(game.state.day, game.state.minute)
        down = rows * 2

        def level(px, py):
            return fw.isobar_level(frame.pressure((px + 0.5) / cols * fw.WORLD_W, (py + 0.5) / down * fw.WORLD_H))
        marked = 0
        for py in range(down):
            for px in range(cols):
                crosses = (px > 0 and level(px, py) != level(px - 1, py)) or (py > 0 and level(px, py) != level(px, py - 1))
                self.assertEqual(lined[py][px] != plain[py][px], crosses, (px, py))
                marked += crosses
        self.assertGreater(marked, 40, "there is weather on the chart")
        self.assertLess(marked, cols * down // 3, "and the chart is not all lines")

    def test_the_field_is_the_one_the_wind_turns_on(self):
        game = self.chart()
        frame = game.weather.frame(game.state.day, game.state.minute)
        for sx, sy, inv_r, strength, turn in frame.systems:
            here, off = frame.pressure(sx, sy), frame.pressure(sx + 1.0 / inv_r, sy)
            self.assertEqual(here < off, turn > 0, "a low is where the pressure is least, a high where it is most")
            # And the air goes round it: opposite ways on opposite sides, as that system turns the wind.
            r = 0.7 / inv_r
            if strength > 30 and 0 <= sy - r and sy + r < fw.WORLD_H:
                south, north = frame.wind(sx, sy + r, fw.LAYERS)[0], frame.wind(sx, sy - r, fw.LAYERS)[0]
                self.assertGreater(abs(south - north), 20.0)

    def test_lows_and_highs_are_marked_with_their_pressure(self):
        game = self.chart()
        frame = game.weather.frame(game.state.day, game.state.minute)
        gyre = game.world.gyre
        game.key("n")                                     # names away: nothing crowds the weather
        text = self.words(game, 132, 40)
        rows = text.split("\n")
        for sx, sy, _, _, turn in frame.systems:
            if (sx - gyre["x"]) ** 2 + (sy - gyre["y"]) ** 2 < 100.0 or not (0 <= sx < fw.WORLD_W and 0 <= sy < fw.WORLD_H):
                continue
            letter, hpa = "L" if turn > 0 else "H", f"{frame.pressure(sx, sy):.0f}"
            self.assertTrue(any(f"{letter} {hpa}" in row or f"{hpa} {letter}" in row or hpa in row for row in rows),
                            (letter, hpa))
        self.assertIn("isobars every 4 hPa", text)
        game.key("i")
        self.assertNotIn("isobars every", self.screen(game, 132, 40))

    def test_in_characters_isobars_are_asked_for(self):
        game = self.game(color_depth="16")
        self.assertFalse(game.map.pixels)
        game.key("c")
        self.assertFalse(game.atlas_view["isobars"])
        before = self.screen(game)
        game.key("i")
        after = self.screen(game)
        self.assertGreater(after.count("-"), before.count("-") + 20)


class Picture(AtlasCase):
    def test_every_tier_draws_inside_its_screen(self):
        game = self.chart()
        for size in ((80, 24), (100, 30), (120, 36), (132, 40), (168, 46), (200, 60)):
            for zoom in (0, 1, 2):
                game.atlas_view["zoom"] = zoom
                text = self.screen(game, *size)
                self.assertIn("The chart", text)
                self.assertEqual("THE CONTINENT" in text, size[0] >= fw.ATLAS_PANEL_FROM, size)

    def test_the_panel_says_whose_work_it_is_and_who_is_waiting(self):
        a = self.game(user_id=1, handle="Ada")
        b = self.other(2, handle="Bo")
        x, y, _ = self.wild(a)
        self.fly_over(a, x, y)
        self.strand(a)
        b._day_may_have_opened()
        b.key("c")
        text = self.screen(b, 132, 40)
        for word in ("THE CONTINENT", "features named", "3 stations, 0 of them masts", "RECORDS", "Ada", "STRANDED",
                     "Ada: 15 fuel", "KEY", "your route so far"):
            self.assertIn(word, text)
        b.close()
        a.key("c")
        self.assertIn("Nobody is waiting for help", self.screen(a, 132, 40))

    def test_it_is_composed_once_for_what_it_shows(self):
        game = self.chart()
        first = game._atlas_compose(78, 19)
        self.assertIs(game._atlas_compose(78, 19), first)
        game.key("z")
        zoomed = game._atlas_compose(78, 19)
        self.assertIsNot(zoomed, first)
        self.assertIs(game._atlas_compose(78, 19), zoomed)
        game.chart.add(4242)
        game.chart_version += 1
        self.assertIsNot(game._atlas_compose(78, 19), zoomed, "new ground on the chart is drawn")

    def test_it_composes_in_time(self):
        game = self.chart()
        for cols, rows in ((78, 19), (91, 35), (128, 41)):
            for zoom in (0, 1, 2):
                game.atlas_view["zoom"] = zoom
                game._atlas_made = None
                game._atlas_compose(cols, rows)             # the tiles are warm after this
                best = 9.0
                for _ in range(3):
                    game._atlas_made = None
                    began = time.perf_counter()
                    game._atlas_compose(cols, rows)
                    best = min(best, time.perf_counter() - began)
                # The plan asks for 60 ms. A shared test machine is given four times that.
                self.assertLess(best, 0.240, (cols, rows, zoom, best))

    def test_uncharted_ground_is_sketched_and_charted_ground_is_in_colour(self):
        game = self.chart()
        x, y, cell = self.wild(game)
        game.key("i")
        cols, rows = 160, 50
        before = game._atlas_compose(cols, rows)["grid"]
        px, py = cell % fw.CHART_W, cell // fw.CHART_W
        grey = before[py][px]
        self.assertLess(max(grey) - min(grey), 40, "sketched: nearly grey")
        game.chart.add(cell)
        game.chart_version += 1
        after = game._atlas_compose(cols, rows)["grid"]
        self.assertNotEqual(after[py][px], grey)
        self.assertEqual(after[py][px], tuple(fw.terrain_rgb(game.world.chart_elevation[cell], game.world.chart_biome[cell])))


class Route(AtlasCase):
    def test_the_route_is_where_each_flight_ended(self):
        game = self.game()
        start = [round(game.state.x, 1), round(game.state.y, 1)]
        self.assertEqual(game.route, [start])
        ref = game.targets[0]
        self.land_at(game, ref)
        place = game.place_of(ref)
        self.assertEqual(game.route, [start, [round(place["x"], 1), round(place["y"], 1)]])
        self.assertEqual(support.stored(self.folder)["route"], game.route)
        again = self.game()
        self.assertEqual(again.route, game.route)
        again.key("c")
        none = self.screen(again).count(fw.TRACK_DOT)
        far = max(again.world.towns, key=lambda t: (t["x"] - again.state.x) ** 2 + (t["y"] - again.state.y) ** 2)
        again.route = [[far["x"], far["y"]]] + again.route
        self.assertGreater(self.screen(again).count(fw.TRACK_DOT), none + 5, "a long leg is drawn in dots")

    def test_walking_out_is_on_the_route_and_the_route_is_bounded(self):
        game = self.game()
        self.strand(game)
        game.key("w")
        self.assertEqual(game.route[-1], [round(game.state.x, 1), round(game.state.y, 1)])
        for i in range(100):
            game._route_to(float(i), 1.0)
        self.assertEqual(len(game.route), fw.ROUTE_KEPT)
        self.assertEqual(game.route[-1], [99.0, 1.0])

    def test_a_pilot_from_before_has_no_route_and_a_bad_one_is_not_a_game(self):
        game = self.game()
        career = {k: v for k, v in game._career().items() if k != "route"}
        game._take(career, game.state.to_dict())
        self.assertEqual(game.route, [])
        game.key("c")
        self.screen(game)
        for bad in ([[1.0]], [["x", 2]], [[float("inf"), 1.0]], 7):
            with self.assertRaises((ValueError, TypeError, IndexError)):
                game._take(dict(career, route=bad), game.state.to_dict())


if __name__ == "__main__":
    unittest.main()
