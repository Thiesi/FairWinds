"""S08, second half: the sounding panel."""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from support import Terminal, door_info

import fair_winds as fw


class Out:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


class PanelCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")

    def tearDown(self):
        self._dir.cleanup()

    def start(self, width=132, height=40, **changes):
        info = fw.parse_door_info(json.dumps(door_info(terminal_width=width, terminal_height=height, **changes)), {})
        game = fw.PlainFlight(info, Path(self._dir.name))
        return game, fw.Session(info, None, Out(), game=game)

    def lines(self, session):
        session.shown = None
        session.out.text = ""
        session.paint()
        term = Terminal(session.width, session.height)
        term.feed(session.out.text)
        self.assertEqual(term.out_of_bounds, [])
        self.assertNotIn((session.height - 1, session.width - 1), term.written)
        return term.text().split("\n")

    def panel(self, session, game):
        return [line[game.panel_col:] for line in self.lines(session)]


class Layout(PanelCase):
    def test_the_small_tier_keeps_its_table(self):
        game, session = self.start(80, 24)
        lines = self.lines(session)
        self.assertEqual((game.tier, game.panel_col, game.map_cols), ("small", 55, 52))
        self.assertIn("LAYER      WIND   FUEL", lines[1])
        self.assertNotIn("SOUNDING", "\n".join(lines))

    def test_from_132_by_40_up_the_panel_is_the_sounding(self):
        for size, tier in (((132, 40), "medium"), ((168, 46), "wide"), ((240, 70), "wide")):
            game, session = self.start(*size)
            panel = self.panel(session, game)
            self.assertEqual(game.tier, tier)
            self.assertEqual((game.panel_col, game.map_cols), (size[0] - 39, size[0] - 42))
            self.assertTrue(panel[1].startswith("SOUNDING"))
            text = "\n".join(panel)
            for heading in ("STORES", "POST FOR", "Ground wind", "Sky "):
                self.assertIn(heading, text)
            self.assertEqual("FORECAST" in text, tier == "wide", "only the wide tier looks ahead by the hour")

    def test_the_map_and_the_panel_never_overlap(self):
        game, session = self.start(168, 46)
        session.paint()
        frame = session.frame
        for y in range(1, 1 + game.map_rows):
            for x in range(168):
                inside_map = 1 <= x <= game.map_cols
                if frame.split[y * 168 + x] and not inside_map:
                    self.assertTrue(game.panel_col <= x < game.panel_col + fw.SKY_COLS, (x, y))
        self.assertGreater(game.panel_col, game.map_cols + 1, "a clear column between them")


class Sounding(PanelCase):
    def test_every_layer_is_listed_from_the_jet_down(self):
        game, session = self.start()
        game.key("ENTER")
        panel = self.panel(session, game)
        rows = [panel[2 + i * 2] for i in range(6)]
        for i, name in enumerate(("6 Jet", "5 High", "4 Upper", "3 Mid", "2 Low", "1 Surface")):
            self.assertIn(name, rows[i])
        self.assertIn(">1 Surface", rows[5], "you are here")
        self.assertIn("you", panel[2 + 5 * 2 + 1])
        for i in (0, 1, 2):
            self.assertIn("out of reach", panel[2 + i * 2 + 1], "the first envelope stops at layer 3")
        self.assertIn("~ forecast, not read", "\n".join(panel))

    def test_marks_costs_and_readings_follow_the_choice(self):
        game, session = self.start()
        game.key("ENTER")
        game.key("w")
        panel = self.panel(session, game)
        self.assertIn("*2 Low", panel[2 + 4 * 2], "the layer being looked at")
        self.assertRegex(panel[2 + 4 * 2], r"\*2 Low\s+~\s*[NESW]+\s+\d+\s+3$", "a forecast, and it costs 3 to climb to")
        self.assertRegex(panel[2 + 5 * 2], r">1 Surface\s+[NESW]+\s+\d+\s+1$", "read, and 1 to hold")
        game.key("ENTER")
        game.key("p")
        panel = self.panel(session, game)
        self.assertNotRegex("\n".join(panel[2:14]), r"(Mid|Low|Surface)\s+~", "a pibal reads every layer within reach")
        self.assertIn("read: 4 turns", "\n".join(panel))

    def test_your_own_layer_still_says_what_is_in_it(self):
        game, session = self.start()
        game.key("ENTER")
        state = game.state
        frame = game.weather.frame(state.day, state.minute)
        real_cloud, real_icing = type(frame).cloud, frame.icing_layer
        try:
            type(frame).cloud = lambda self, x, y: (0.9, 1)
            panel = self.panel(session, game)
            self.assertIn("you, cloud deck", panel[2 + 5 * 2 + 1])
            frame.icing_layer = 1
            panel = self.panel(session, game)
            self.assertIn("you, icing", panel[2 + 5 * 2 + 1], "icing costs fuel, so it is said first")
            self.assertIn("icing", panel[2 + 4 * 2 + 1])
            self.assertNotIn("you", panel[2 + 4 * 2 + 1])
        finally:
            type(frame).cloud, frame.icing_layer = real_cloud, real_icing

    def test_a_step_that_cannot_be_paid_for_says_so(self):
        game, session = self.start()
        game.key("ENTER")
        panel = self.panel(session, game)
        self.assertRegex(panel[2 + 4 * 2], r"\s3$", "climbing to layer 2 is on offer: 3 fuel")
        self.assertRegex(panel[2 + 3 * 2], r"\d$", "layer 3 is two steps away: its row ends with its speed, no cost")
        self.assertNotRegex(panel[2 + 3 * 2], r"--$")
        game.state = dataclasses.replace(game.state, fuel=2, ballast=0)
        game._choose_default()
        panel = self.panel(session, game)
        self.assertRegex(panel[2 + 4 * 2], r"--$", "a step away, and not affordable")

    def test_bars_share_one_scale(self):
        game, session = self.start()
        game.key("ENTER")
        session.paint()
        frame, col = session.frame, game.panel_col + fw.SKY_COLS + 1
        winds = game.layer_winds()
        for layer in range(1, 4):
            row = 2 + (6 - layer) * 2 + 1
            filled = sum(frame.ch[row * 132 + col + 2 + n] == "\u2588" for n in range(10))
            speed = (winds[layer][0] ** 2 + winds[layer][1] ** 2) ** 0.5
            self.assertEqual(filled, max(1, min(10, round(speed / 10))), "ten km/h to a block, whatever the layer")

    def test_the_ground_wind_is_known_only_as_well_as_the_lowest_layer(self):
        game, session = self.start()
        game.key("ENTER")
        text = "\n".join(self.panel(session, game))
        self.assertRegex(text, r"Ground wind \d+ km/h: ", "in the lowest layer it is read, and the landing is named")
        game.key("w")
        game.key("ENTER")
        self.assertEqual(game.state.layer, 2)
        text = "\n".join(self.panel(session, game))
        self.assertRegex(text, r"Ground wind ~\d+ km/h", "from above it is a forecast, like the Surface row")
        self.assertNotRegex(text, r"Ground wind ~?\d+ km/h: ", "and no landing is promised from up here")
        game.key("p")
        text = "\n".join(self.panel(session, game))
        self.assertRegex(text, r"Ground wind \d+ km/h", "a pibal reads it")

    def test_what_the_pilot_knows_of_each_layer(self):
        game, session = self.start()
        game.key("ENTER")
        winds = game.layer_winds()
        self.assertEqual(set(winds), {1, 2, 3, 4, 5, 6})
        self.assertTrue(winds[1][2], "your own layer is read")
        self.assertFalse(winds[2][2])
        truth = game.weather.frame(game.state.day, game.state.minute).wind(game.state.x, game.state.y, 1)
        self.assertEqual(winds[1][:2], truth)
        self.assertNotEqual(winds[3][:2], game.weather.frame(game.state.day, game.state.minute).wind(
            game.state.x, game.state.y, 3), "an unread layer is a forecast")

    def test_ground_fills_the_layers_it_reaches(self):
        game, session = self.start()
        engine = game.engine
        hill = None
        for k, m in enumerate(game.world.chart_elevation):
            x, y = (k % fw.CHART_W) * 10 + 5.0, (k // fw.CHART_W) * 10 + 5.0
            if engine.lowest_open(x, y) == 2:
                hill = (x, y)
                break
        game.state = dataclasses.replace(game.state, x=hill[0], y=hill[1], status="aloft", layer=2, turn=8)
        game._choose_default()
        panel = self.panel(session, game)
        self.assertIn("1 Surface ground", panel[2 + 5 * 2])
        self.assertIsNone(game.layer_winds()[1])
        self.assertIn(">2 Low", panel[2 + 4 * 2])

    def test_the_sky_column_holds_the_balloon_where_it_is(self):
        game, session = self.start()
        game.key("ENTER")
        game.key("w")
        game.key("ENTER")
        self.assertEqual(game.state.layer, 2)
        session.paint()
        frame, col = session.frame, game.panel_col
        sky = [(x, y) for y in range(2, 14) for x in range(col, col + fw.SKY_COLS) if frame.split[y * 132 + x]]
        self.assertEqual(len(sky), fw.SKY_COLS * 12, "two rows a layer, six layers")
        coral = (244, 112, 92)
        rows = {y for x, y in sky if frame.fg[y * 132 + x] == coral or frame.bg[y * 132 + x] == coral}
        self.assertEqual(rows, {10, 11}, "the balloon hangs in layer 2's two rows")
        top, bottom = frame.fg[2 * 132 + col], frame.bg[13 * 132 + col + 7]
        self.assertLess(sum(top), sum(bottom), "the sky is darker high up")

    def test_the_character_map_has_no_sky_column(self):
        game, session = self.start(color_depth="16")
        game.key("ENTER")
        session.paint()
        frame = session.frame
        self.assertFalse(any(frame.split), "no pixels anywhere")
        panel = self.panel(session, game)
        self.assertTrue(panel[2].lstrip().startswith("6 Jet"))
        self.assertLess(len(panel[2]) - len(panel[2].lstrip()), 3, "the layer rows start at the panel's edge")


class Stores(PanelCase):
    def bar(self, session, game, label):
        session.paint()
        frame, col = session.frame, game.panel_col
        row = next(y for y in range(40) if "".join(frame.ch[y * 132 + col + i] for i in range(len(label))) == label)
        return "".join(frame.ch[row * 132 + col + 9 + n] for n in range(14))

    def test_bars_follow_the_stores(self):
        game, session = self.start()
        full = "█"
        self.assertEqual(self.bar(session, game, "Fuel").count(full), 14)
        game.state = dataclasses.replace(game.state, fuel=30, ballast=2, pibals=1, wear=50)
        self.assertEqual(self.bar(session, game, "Fuel").count(full), 7)
        self.assertEqual(self.bar(session, game, "Wear").count(full), 7)
        self.assertEqual(self.bar(session, game, "Ballast").count("■"), 2)
        self.assertEqual(self.bar(session, game, "Pibals").count("■"), 1)
        game.state = dataclasses.replace(game.state, fuel=0, wear=0)
        self.assertEqual(self.bar(session, game, "Fuel").count(full), 0)
        self.assertEqual(self.bar(session, game, "Wear").count(full), 0)
        text = "\n".join(self.panel(session, game))
        self.assertIn("2 sacks", text)
        self.assertIn("1 left", text)

    def test_daylight_runs_down(self):
        game, session = self.start()
        dawn = self.bar(session, game, "Daylight").count("█")
        game.state = dataclasses.replace(game.state, turn=28)          # 13:00, half the daylight gone
        noon = self.bar(session, game, "Daylight").count("█")
        game.state = dataclasses.replace(game.state, turn=60)          # 21:00
        night = self.bar(session, game, "Daylight").count("█")
        self.assertEqual((dawn, noon, night), (14, 7, 0))
        self.assertIn("night", "\n".join(self.panel(session, game)))

    def test_the_landing_is_named_while_aloft(self):
        game, session = self.start()
        self.assertNotIn("landing", "\n".join(self.panel(session, game)), "on the ground there is nothing to land")
        game.key("ENTER")
        self.assertRegex("\n".join(self.panel(session, game)), r"Ground wind \d+ km/h: (a (clean|rough|hard) landing|water below)")

    def test_the_post_is_listed_nearest_first(self):
        game, session = self.start()
        panel = self.panel(session, game)
        at = next(i for i, line in enumerate(panel) if line.startswith("POST FOR"))
        km = [int(line.split(" km ")[0].split()[-1]) for line in panel[at + 1:at + 4]]
        self.assertEqual(km, sorted(km))
        self.assertEqual(len(km), 3)


class ForecastStrip(PanelCase):
    def strip(self, session, game):
        panel = self.panel(session, game)
        at = next(i for i, line in enumerate(panel) if line.startswith("FORECAST"))
        return panel[at:at + 4]

    def test_six_hours_of_the_layer_in_view(self):
        game, session = self.start(168, 46)
        game.key("ENTER")
        head, hours, bearings, speeds = self.strip(session, game)
        self.assertIn("Surface here, hour by hour", head)
        self.assertEqual(hours.split(), ["07:15", "08:15", "09:15", "10:15", "11:15", "12:15"],
                         "each column says the minute it means: the first turn left at 06:15")
        self.assertEqual(len(bearings.split()), 6)
        self.assertTrue(all(v.isdigit() for v in speeds.split()))
        game.key("w")
        self.assertIn("Low here", self.strip(session, game)[0], "it follows the layer being looked at")

    def test_it_stops_at_midnight(self):
        game, session = self.start(168, 46)
        game.state = dataclasses.replace(game.state, status="aloft", layer=1, turn=66, night_gear=True)     # 22:30
        game._choose_default()
        hours = self.strip(session, game)[1].split()
        self.assertEqual(hours, ["23:30"])


class Everywhere(PanelCase):
    def test_no_size_depth_or_charset_writes_out_of_bounds(self):
        for size in ((132, 40), (168, 46), (240, 70), (133, 41), (200, 40), (132, 70)):
            for changes in ({}, {"color_depth": "256"}, {"color_depth": "16"}, {"charset": "ascii"}):
                game, session = self.start(*size, **changes)
                game.key("ENTER")
                lines = self.lines(session)
                self.assertEqual(len(lines), size[1])
                if changes.get("charset") == "ascii":
                    self.assertTrue(session.out.text.isascii())

    def test_looking_at_a_layer_is_still_a_small_change(self):
        game, session = self.start(168, 46)
        game.key("ENTER")
        session.paint()
        session.out.text = ""
        game.key("w")
        game.frames_left = 0
        session.paint()
        self.assertLess(len(session.out.text), 6000)


if __name__ == "__main__":
    unittest.main()
