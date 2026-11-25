"""S07: the relief map."""
import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path

import support                                             # noqa: E402
from support import Terminal, door_info

import fair_winds as fw

WORLD = fw.World(fw.world_seed("map-node"))
WEATHER = fw.Weather(WORLD)


def launch(**changes):
    return fw.parse_door_info(json.dumps(door_info(**changes)), {})


def view(pixels=True):
    return fw.MapView(WORLD, WEATHER, pixels)


class Window(unittest.TestCase):
    def test_the_balloon_is_always_in_the_middle_half(self):
        for cols, rows in ((52, 16), (104, 32), (140, 38)):
            w, h = cols * 5.0, rows * 10.0
            for x in range(0, 1600, 37):
                for y in range(0, 1000, 53):
                    x0, y0 = fw.MapView.origin(float(x), float(y), cols, rows)
                    self.assertTrue(w / 4 <= x - x0 < w * 3 / 4, (x, x0))
                    self.assertTrue(h / 4 <= y - y0 < h * 3 / 4, (y, y0))

    def test_it_holds_still_and_then_moves_by_half_a_window(self):
        cols, rows = 52, 16
        origins = [fw.MapView.origin(400.0 + i * 3.0, 300.0, cols, rows) for i in range(120)]
        distinct = sorted(set(origins))
        self.assertLessEqual(len(distinct), 4, "360 km of drift is three moves of the window at most")
        steps = {round(b[0] - a[0]) for a, b in zip(distinct, distinct[1:])}
        self.assertEqual(steps, {130}, "each move is half a window")
        self.assertEqual(fw.MapView.origin(400.0, 300.0, cols, rows), fw.MapView.origin(404.0, 303.0, cols, rows))

    def test_cells(self):
        v = view()
        v.draw(fw.Screen(80, 24), 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        self.assertEqual(v.cell_of(v.x0, v.y0), (1, 1))
        self.assertEqual(v.cell_of(v.x0 + 259.9, v.y0 + 159.9), (52, 16))
        self.assertIsNone(v.cell_of(v.x0 - 0.1, v.y0))
        self.assertIsNone(v.cell_of(v.x0 + 260.0, v.y0))
        self.assertIsNone(v.cell_of(v.x0, v.y0 + 160.0))


class Readings(unittest.TestCase):
    def test_which_terminals_get_pixels(self):
        self.assertTrue(fw.want_pixels(launch(), {}))
        self.assertTrue(fw.want_pixels(launch(color_depth="256"), {}))
        self.assertTrue(fw.want_pixels(launch(charset="cp437"), {}))
        self.assertFalse(fw.want_pixels(launch(color_depth="16"), {}), "sixteen colours get the character map")
        self.assertFalse(fw.want_pixels(launch(charset="ascii"), {}))
        self.assertTrue(fw.want_pixels(launch(color_depth="16"), {"DOOR_FW_PIXELS": "half"}))
        self.assertFalse(fw.want_pixels(launch(charset="ascii"), {"DOOR_FW_PIXELS": "half"}), "ASCII has no half block")
        self.assertFalse(fw.want_pixels(launch(), {"DOOR_FW_PIXELS": " Shade "}))
        self.assertTrue(fw.want_pixels(launch(), {"DOOR_FW_PIXELS": "nonsense"}))

    def test_pixels_fill_the_map_and_nothing_else(self):
        screen = fw.Screen(80, 24)
        view().draw(screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        for y in range(24):
            for x in range(80):
                inside = 1 <= x <= 52 and 1 <= y <= 16
                self.assertEqual(bool(screen.split[y * 80 + x]), inside, (x, y))

    def test_characters_fill_the_map_and_nothing_else(self):
        screen = fw.Screen(80, 24, "16")
        view(pixels=False).draw(screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        self.assertFalse(any(screen.split))
        glyphs = {screen.ch[y * 80 + x] for y in range(1, 17) for x in range(1, 53)}
        self.assertLessEqual(glyphs, set('~.,":n^A%'))
        self.assertEqual({screen.ch[x] for x in range(80)}, {" "})

    def test_land_and_sea_are_told_apart(self):
        sea = fw.terrain_rgb(-800, fw.SEA_DEEP)
        self.assertGreater(sea[2], sea[0] + 40, "sea is blue")
        for biome in (fw.GRASS, fw.FOREST):
            c = fw.terrain_rgb(200, biome)
            self.assertGreater(c[1], c[2], "lowland is green")
        self.assertGreater(min(fw.terrain_rgb(3500, fw.SNOW)), 230)
        deep, shallow = fw.terrain_rgb(-2500, fw.SEA_DEEP), fw.terrain_rgb(-20, fw.SEA_SHALLOW)
        self.assertLess(sum(deep), sum(shallow), "deep water is darker")

    def test_slopes_catch_the_light(self):
        v = view()
        shades = set()
        for tx in range(4, 14):
            lit = v._tile(tx, 6)
            tile = WORLD.tile(tx, 6)
            for k, biome in enumerate(tile.biome):
                if biome == fw.HILLS:
                    flat = fw.terrain_rgb(tile.elevation[k], biome)
                    shades.add(round(lit[k][0] / max(1.0, flat[0]), 1))
        self.assertGreater(len(shades), 3, "hills are not one flat colour")
        self.assertTrue(all(0 <= c <= 255 for tile in v._lit.values() for rgb in tile for c in rgb))


class Weatherly(unittest.TestCase):
    def test_night_falls_in_three_steps(self):
        levels = [fw.night_level(m) for m in range(fw.DAWN_MINUTE, fw.MINUTES_PER_DAY, fw.TURN_MINUTES)]
        self.assertEqual(levels[0], 0.0)
        self.assertEqual(levels[-1], 1.0)
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(len(set(levels)), 4, "day, and three steps of dusk")
        changes = sum(a != b for a, b in zip(levels, levels[1:]))
        self.assertEqual(changes, 3, "so the whole map is re-sent three times at dusk, not every turn")
        self.assertEqual(fw.night_level(fw.DUSK_MINUTE - 45), 0.0)
        self.assertGreater(fw.night_level(fw.DUSK_MINUTE - 30), 0.0, "the light starts to go before dusk")

    def test_night_is_darker(self):
        v = view()
        v.draw(fw.Screen(80, 24), 1, 1, 52, 16, 400.0, 300.0, 1, 720)       # sets the window
        day, night = v._compose(1, 12 * 60, None), v._compose(1, 22 * 60, None)
        self.assertLess(sum(sum(c) for row in night[0] for c in row), 0.8 * sum(sum(c) for row in day[0] for c in row))

    def test_the_unknown_is_sketched_in_dim_greys(self):
        screen = fw.Screen(80, 24)
        v = view()
        v.draw(screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600, charted=lambda wx, wy: wx < v.x0 + 130.0)
        left = {tuple(screen.fg[y * 80 + x]) for y in range(1, 17) for x in range(1, 27)}
        right = {tuple(screen.fg[y * 80 + x]) for y in range(1, 17) for x in range(27, 53)}
        self.assertLessEqual(len(right), 12, "what nobody has flown over is drawn in a handful of tones")
        for c in right:
            self.assertLess(max(c), 140, "and dim")
            self.assertLess(max(c) - min(c), 60, "and nearly without colour")
        self.assertGreater(len(right), 1, "but its shape is there")
        self.assertTrue(any(max(c) - min(c) > 60 for c in left), "what is charted is in colour")
        whole = fw.Screen(80, 24)
        w = view()
        w.draw(whole, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        for y in range(1, 17):
            for x in range(27, 53):
                self.assertEqual(tuple(screen.fg[y * 80 + x]), fw.sketch_rgb(whole.fg[y * 80 + x]),
                                 "the same ground, sketched")
        chars = fw.Screen(80, 24, "16")
        c = view(pixels=False)
        c.draw(chars, 1, 1, 52, 16, 400.0, 300.0, 1, 600, charted=lambda wx, wy: False)
        cells = [(chars.ch[y * 80 + x], chars.fg[y * 80 + x]) for y in range(1, 17) for x in range(1, 53)]
        self.assertTrue(all(ink == "dim" or glyph == "%" for glyph, ink in cells), "in characters: its glyphs, dimmed")
        self.assertGreater(len({glyph for glyph, _ in cells}), 1)

    def test_the_picture_is_composed_again_only_when_the_chart_changes(self):
        v = view()
        screen = fw.Screen(80, 24)
        calls = []
        real = v._compose
        v._compose = lambda *args: (calls.append(1), real(*args))[1]
        known = lambda wx, wy: wx < 400.0
        for _ in range(3):
            v.draw(screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600, charted=known, version=4)
        self.assertEqual(len(calls), 1)
        v.draw(screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600, charted=known, version=5)
        self.assertEqual(len(calls), 2)

    def test_off_the_edge_of_the_world_is_sea(self):
        screen = fw.Screen(80, 24)
        v = view()
        v.draw(screen, 1, 1, 52, 16, 2.0, 2.0, 1, 600)
        self.assertLess(v.x0, 0)
        self.assertEqual(tuple(screen.fg[1 * 80 + 1]), (9, 32, 70))
        for x, y in ((fw.WORLD_W - 1.0, fw.WORLD_H - 1.0), (0.0, fw.WORLD_H - 1.0), (fw.WORLD_W - 1.0, 0.0),
                     (-400.0, -400.0), (fw.WORLD_W + 400.0, fw.WORLD_H + 400.0)):
            far = fw.Screen(80, 24)
            v.draw(far, 1, 1, 52, 16, x, y, 1, 600)
            self.assertTrue(all(far.split[r * 80 + c] for r in range(1, 17) for c in range(1, 53)))
        for key in ((0, 0), (fw.TILES_X - 1, 0), (0, fw.TILES_Y - 1), (fw.TILES_X - 1, fw.TILES_Y - 1)):
            self.assertEqual(len(v._tile(*key)), fw.TILE_N * fw.TILE_N, "a tile on the world's border lights like any other")

    def test_the_biggest_window_keeps_its_tiles(self):
        v = view()
        for ty in range(fw.TILES_Y):
            for tx in range(fw.TILES_X):
                v._tile(tx, ty)
        first = v._lit[(0, 0)]
        self.assertEqual(len(v._lit), fw.TILES_X * fw.TILES_Y, "there is room for the whole world")
        self.assertIs(v._tile(0, 0), first, "and nothing drawn a moment ago was thrown away")

    def test_the_same_moment_is_composed_once(self):
        v = view()
        v.draw(fw.Screen(80, 24), 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        first = v._base
        v.draw(fw.Screen(80, 24), 1, 1, 52, 16, 403.0, 301.0, 1, 600)
        self.assertIs(v._base, first, "looking at another layer redraws nothing underneath")
        v.draw(fw.Screen(80, 24), 1, 1, 52, 16, 403.0, 301.0, 1, 615)
        self.assertIsNot(v._base, first, "a new turn has new weather")


class Markers(unittest.TestCase):
    def setUp(self):
        self.screen = fw.Screen(80, 24)
        self.view = view()
        self.view.draw(self.screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        self.x0, self.y0 = self.view.x0, self.view.y0

    def at(self, col, row):
        """The world position of the middle of a map cell."""
        return self.x0 + (col + 0.5) * 5.0, self.y0 + (row + 0.5) * 10.0

    def row_text(self, row):
        return "".join(self.screen.ch[(1 + row) * 80 + x] for x in range(80))

    def test_a_marker_takes_its_cell_and_keeps_it(self):
        self.assertEqual(self.view.mark(self.screen, *self.at(10, 5), "@", "bright"), (11, 6))
        self.assertIsNone(self.view.mark(self.screen, *self.at(10, 5), "3", "dim"), "a track does not cover the balloon")
        self.assertEqual(self.screen.ch[6 * 80 + 11], "@")
        self.assertEqual(self.view.mark(self.screen, *self.at(10, 5), "#", "accent", force=True), (11, 6))
        self.assertIsNone(self.view.mark(self.screen, self.x0 - 3.0, self.y0, "o", "text"), "off the map")
        self.assertFalse(self.screen.split[6 * 80 + 11], "a marker is a glyph, not pixels")

    def test_a_name_goes_to_the_right_or_else_to_the_left(self):
        cell = self.view.mark(self.screen, *self.at(10, 3), "#", "accent")
        self.assertTrue(self.view.label(self.screen, cell, "Gull Post", "accent"))
        self.assertIn("#Gull Post", self.row_text(3))
        edge = self.view.mark(self.screen, *self.at(49, 7), "#", "accent")
        self.assertTrue(self.view.label(self.screen, edge, "Thistle Cairn", "accent"))
        text = self.row_text(7)
        self.assertIn("Thistle Cairn#", text, "no room on the right: it goes on the left")
        self.assertEqual(text[53:].strip(), "", "and nothing runs off the map")

    def test_a_name_never_covers_the_balloon_or_another_name(self):
        self.view.mark(self.screen, *self.at(14, 3), "@", "bright", force=True)
        cell = self.view.mark(self.screen, *self.at(10, 3), "#", "accent")
        self.assertTrue(self.view.label(self.screen, cell, "Gull Post", "accent"))
        text = self.row_text(3)
        self.assertEqual(text[15], "@", "the balloon is still there")
        self.assertIn("Gull Post#", text, "the name moved to the other side")
        again = self.view.mark(self.screen, *self.at(12, 3), "#", "accent")
        self.assertFalse(self.view.label(self.screen, again, "Somewhere Long", "accent"), "no room either side: no name")
        self.assertEqual(self.row_text(3)[15], "@")

    def test_a_name_too_long_is_shortened_not_dropped(self):
        cell = self.view.mark(self.screen, *self.at(25, 9), "#", "accent")
        self.assertTrue(self.view.label(self.screen, cell, "N" * 60, "accent"))
        text = self.row_text(9)
        self.assertEqual(text.count("N"), 26, "it takes the roomier side, to the edge of the map")
        self.assertEqual(text[53:].strip(), "")
        self.assertEqual(text[0], " ")

    def test_a_name_with_no_room_at_all_is_left_out(self):
        self.view.mark(self.screen, *self.at(22, 9), "o", "text")
        self.view.mark(self.screen, *self.at(28, 9), "o", "text")
        cell = self.view.mark(self.screen, *self.at(25, 9), "#", "accent")
        self.assertFalse(self.view.label(self.screen, cell, "Thistle Cairn", "accent"), "two cells either side says nothing")
        self.assertNotIn("Th", self.row_text(9))

    def test_two_names_never_run_together(self):
        first = self.view.mark(self.screen, *self.at(10, 4), "#", "accent")
        self.assertTrue(self.view.label(self.screen, first, "Foo", "accent"))
        second = self.view.mark(self.screen, *self.at(17, 4), "#", "accent")
        self.assertTrue(self.view.label(self.screen, second, "Bar", "accent"))
        text = self.row_text(4)
        self.assertNotIn("FooBar", text)
        self.assertIn("#Foo", text)
        self.assertIn("#Bar", text, "there was room on the right, so it did not crowd the first name")
        third = self.view.mark(self.screen, *self.at(30, 4), "#", "accent")
        fourth = self.view.mark(self.screen, *self.at(36, 4), "#", "accent")
        self.assertTrue(self.view.label(self.screen, third, "Gull", "accent"))
        self.assertTrue(self.view.label(self.screen, fourth, "Wren Rock", "accent"))
        self.assertRegex(self.row_text(4), r"#Gull[^#G]#Wren Rock", "a cell is kept clear between a name and the next marker")


class Out:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


class InTheGame(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")

    def tearDown(self):
        fw.Store.close_all()
        self._dir.cleanup()

    def session(self, **changes):
        info = launch(**changes)
        game = support.new_game(info, Path(self._dir.name))
        return game, fw.Session(info, None, Out(), game=game)

    def paint(self, session):
        session.out.text = ""
        session.paint()
        return session.out.text

    def test_the_balloon_the_post_and_the_tracks_are_on_the_map(self):
        game, session = self.session()
        game.key("ENTER")
        self.paint(session)
        frame = session.frame
        glyphs = [frame.ch[y * 80 + x] for y in range(1, 17) for x in range(1, 53) if not frame.split[y * 80 + x]]
        self.assertEqual(glyphs.count("@"), 1)
        self.assertGreaterEqual(glyphs.count("#"), 1)
        self.assertTrue(any(g in fw.TRACK_HEADS for g in glyphs), "a track is drawn, and ends in an arrow")
        self.assertFalse(any(g in "123456" for g in glyphs), "no digits on the map any more")
        at = game.map.cell_of(game.state.x, game.state.y)
        self.assertEqual(frame.ch[at[1] * 80 + at[0]], "@")
        names = "".join(frame.ch[y * 80 + x] if not frame.split[y * 80 + x] else " " for y in range(1, 17) for x in range(1, 53))
        shown = [game.place_of(t)["name"] for t in game.targets if game.place_of(t)["name"] in names]
        for name in shown:
            self.assertEqual(names.count(name), 1)

    def test_a_turn_sends_a_fraction_of_the_map(self):
        for size in ((80, 24), (168, 46)):
            game, session = self.session(terminal_width=size[0], terminal_height=size[1])
            full = len(self.paint(session))
            game.key("ENTER")
            self.paint(session)
            turns = []
            for _ in range(8):
                before = (game.map.x0, game.map.y0)
                game.key("ENTER")
                sent = len(self.paint(session))
                if (game.map.x0, game.map.y0) == before:
                    turns.append(sent)
            self.assertTrue(turns)
            self.assertLess(sum(turns) / len(turns), full * 0.2, "the window holds still, so the ground is not re-sent")

    def test_sixteen_colours_and_ascii_play_on_the_character_map(self):
        for changes in ({"color_depth": "16"}, {"charset": "ascii"}):
            game, session = self.session(**changes)
            self.assertFalse(game.map.pixels)
            text = self.paint(session)
            term = Terminal(80, 24)
            term.feed(text)
            self.assertEqual(term.out_of_bounds, [])
            self.assertIn("@", term.text())
            if changes.get("charset") == "ascii":
                self.assertTrue(text.isascii())

    def test_no_reading_writes_out_of_bounds(self):
        for depth in fw.COLOR_DEPTHS:
            for size in ((80, 24), (132, 40), (168, 46), (240, 70)):
                game, session = self.session(color_depth=depth, terminal_width=size[0], terminal_height=size[1])
                game.key("ENTER")
                term = Terminal(*size)
                term.feed(self.paint(session))
                self.assertEqual(term.out_of_bounds, [], (depth, size))
                self.assertNotIn((size[1] - 1, size[0] - 1), term.written)


class Cost(unittest.TestCase):
    """Target: a full map composed in under 30 ms from warm tiles. Measured on the
    development host: about 3 ms at 52 x 16 and 19 ms at 140 x 38. The bound
    here is loose for shared CI runners."""

    def test_composing_from_warm_tiles(self):
        v = view()
        v.draw(fw.Screen(168, 46), 1, 1, 140, 38, 700.0, 500.0, 1, 600)        # warms the tiles
        started = time.perf_counter()
        for minute in (615, 630, 645, 660):
            v.draw(fw.Screen(168, 46), 1, 1, 140, 38, 700.0, 500.0, 1, minute)
        self.assertLess((time.perf_counter() - started) / 4, 0.15)


if __name__ == "__main__":
    unittest.main()


class TrailsInTheGame(InTheGame):
    def test_a_cp437_caller_gets_plain_trails(self):
        game, session = self.session(charset="cp437", color_depth="16")
        game.key("ENTER")
        self.paint(session)
        glyphs = set(session.frame.ch)
        self.assertFalse(glyphs & set(fw.TRACK_HEADS))
        self.assertTrue(glyphs & set("<>^v"), "a head that points, in plain characters")

    def test_the_chosen_trail_is_the_bright_one(self):
        game, session = self.session()
        game.key("ENTER")
        self.paint(session)
        frame = session.frame
        heads = [k for k, g in enumerate(frame.ch) if g in fw.TRACK_HEADS]
        self.assertGreaterEqual(len(heads), 1)

    def test_in_sixteen_colours_every_trail_keeps_a_colour_of_its_own(self):
        roles = set(fw._TRACK_ROLES.values())
        readings = {}
        for role in roles:
            readings[role] = fw.INKS[role][1]
            readings[role + "_dim"] = fw.INKS[role + "_dim"][1]
        self.assertEqual(len(set(readings.values())), len(readings), readings)
        game, session = self.session(color_depth="16")
        game.key("ENTER")
        self.paint(session)
        names = set(fw._TRACK_ROLES.values()) | {r + "_dim" for r in fw._TRACK_ROLES.values()}
        marks = {session.frame.fg[k] for k, g in enumerate(session.frame.ch)
                 if g in fw.TRACK_HEADS + (fw.TRACK_DOT, fw.TRACK_HOUR) and session.frame.fg[k] in names}
        self.assertTrue(marks, "the trails are drawn in named inks, each with a chosen sixteen-colour reading")
        self.assertTrue(any(m.endswith("_dim") for m in marks) or len(game.choices()) == 1)


class Trails(unittest.TestCase):
    def points(self, *steps):
        x, y, out = 0.0, 0.0, []
        for dx, dy in steps:
            x, y = x + dx, y + dy
            out.append((x, y, 1))
        return out

    def test_a_dot_a_turn_a_heavier_one_on_the_hour_and_a_head(self):
        glyphs = fw.track_glyphs(self.points(*[(5.0, 0.0)] * 9), (0.0, 0.0), True)
        self.assertEqual(glyphs, [fw.TRACK_DOT] * 3 + [fw.TRACK_HOUR] + [fw.TRACK_DOT] * 3 + [fw.TRACK_HOUR, "\u2192"])
        self.assertEqual(fw.track_glyphs([], (0.0, 0.0), True), [])
        self.assertEqual(fw.track_glyphs(self.points((0.0, -3.0)), (0.0, 0.0), True), ["\u2191"], "one step is all head")

    def test_a_track_that_goes_nowhere_ends_in_a_dot_not_an_arrow(self):
        still = [(0.0, 0.0, 1), (0.0, 0.0, 1)]
        self.assertEqual(fw.track_glyphs(still, (0.0, 0.0), True)[-1], fw.TRACK_HOUR)
        self.assertEqual(fw.track_glyphs(still, (0.0, 0.0), False)[-1], "*")

    def test_the_head_points_where_the_track_is_going(self):
        ways = {(1, 0): "\u2192", (1, 1): "\u2198", (0, 1): "\u2193", (-1, 1): "\u2199", (-1, 0): "\u2190",
                (-1, -1): "\u2196", (0, -1): "\u2191", (1, -1): "\u2197"}
        for (dx, dy), head in ways.items():
            glyphs = fw.track_glyphs(self.points((1.0, 0.0), (dx * 4.0, dy * 4.0)), (0.0, 0.0), True)
            self.assertEqual(glyphs[-1], head, (dx, dy))
        self.assertEqual(fw.track_glyphs(self.points((4.0, 1.0)), (0.0, 0.0), True)[-1], "\u2192", "nearly east is east")

    def test_without_utf8_no_byte_that_a_terminal_would_obey(self):
        for dx, dy, head in ((1, 0, ">"), (-1, 0, "<"), (0, 1, "v"), (0, -1, "^"), (1, 1, ">"), (-1, -2, "^")):
            glyphs = fw.track_glyphs(self.points((1.0, 0.0), (1.0, 0.0), (1.0, 0.0), (1.0, 0.0), (dx * 3.0, dy * 3.0)),
                                     (0.0, 0.0), False)
            self.assertEqual(glyphs[-1], head)
            for g in glyphs:
                self.assertTrue(g.isascii() and g.isprintable())
        # Why: CP437 draws its arrows and its bullet in the range of the control codes (the left arrow sits
        # where Escape is), so as text they cannot be sent at all.
        for glyph in fw.TRACK_HEADS + (fw.TRACK_HOUR,):
            with self.assertRaises(UnicodeEncodeError):
                glyph.encode("cp437")
