"""S06: the cell buffer, the colour and charset readings, and the layouts."""
import dataclasses
import json
import random
import re
import tempfile
import unittest
from pathlib import Path

from support import Terminal, door_info

import fair_winds as fw

SGR = re.compile(r"\x1b\[([0-9;]*)m")


def apply(text, width, height, onto=None):
    term = onto or Terminal(width, height)
    term.feed(text)
    return term


def shown(screen):
    """What a frame should look like as plain text."""
    rows = []
    for y in range(screen.height):
        row = "".join(screen._cell(y * screen.width + x)[0] for x in range(screen.width))
        if y == screen.height - 1:
            row = row[:-1]              # the corner is never written
        rows.append(row.rstrip())
    return "\n".join(rows)


def random_frame(rng, width, height, depth="truecolor", charset="utf-8"):
    screen = fw.Screen(width, height, depth, charset)
    for _ in range(width * height // 3):
        x, y = rng.randrange(width), rng.randrange(height)
        ink = rng.choice(list(fw.INKS) + [(rng.randrange(256), rng.randrange(256), rng.randrange(256)), None])
        back = rng.choice([None, None, (rng.randrange(256), rng.randrange(256), rng.randrange(256))])
        screen.put(x, y, rng.choice("abcXYZ#@. "), ink, back)
    return screen


class Rendering(unittest.TestCase):
    def test_an_empty_frame_and_an_unchanged_frame_cost_nothing(self):
        screen = fw.Screen(80, 24)
        self.assertEqual(screen.render(None), "")
        screen.text(3, 4, "hello", "accent")
        same = fw.Screen(80, 24)
        same.text(3, 4, "hello", "accent")
        self.assertEqual(same.render(screen), "")

    def test_only_what_changed_is_sent(self):
        before = fw.Screen(80, 24)
        before.text(0, 0, "FAIR WINDS  Day 1  06:00", "text")
        before.fill(0, 2, 79, 20, (10, 20, 30))
        after = fw.Screen(80, 24)
        after.text(0, 0, "FAIR WINDS  Day 1  06:15", "text")
        after.fill(0, 2, 79, 20, (10, 20, 30))
        change = after.render(before)
        self.assertLess(len(change), 40)
        self.assertIn("\x1b[1;23H", change, "it goes straight to the two digits that differ")
        self.assertIn("15", change)
        self.assertLess(len(change) * 20, len(after.render(None)))

    def test_what_is_sent_reproduces_the_frame(self):
        rng = random.Random(6)          # test data only
        for width, height in ((80, 24), (132, 40), (17, 5), (1, 1), (2, 1)):
            first = random_frame(rng, width, height)
            term = apply(first.render(None), width, height)
            self.assertEqual(term.out_of_bounds, [])
            self.assertEqual(term.text(), shown(first))
            for _ in range(4):
                second = random_frame(rng, width, height)
                apply(second.render(first), width, height, term)
                self.assertEqual(term.out_of_bounds, [])
                self.assertEqual(term.text(), shown(second), "a difference applied on top gives the new frame")
                first = second

    def test_the_corner_is_never_written_and_nothing_goes_out_of_bounds(self):
        for width, height in ((80, 24), (81, 25), (132, 40), (168, 46), (240, 70), (3, 2), (1, 1)):
            screen = fw.Screen(width, height)
            screen.fill(0, 0, width - 1, height - 1, (40, 40, 40), "#", "bright")
            screen.text(-5, 0, "clipped-on-the-left", "text")
            screen.text(width - 3, height - 1, "clipped-on-the-right", "text")
            screen.put(width, 0, "x")
            screen.put(0, height, "x")
            screen.pixel(width + 2, 0, (1, 2, 3))
            term = apply(screen.render(None), width, height)
            self.assertEqual(term.out_of_bounds, [], (width, height))
            self.assertNotIn((height - 1, width - 1), term.written)
            self.assertEqual(len(term.written), width * height - 1, "every other cell is written")

    def test_a_short_gap_is_written_through(self):
        screen = fw.Screen(40, 3)
        screen.text(2, 1, "Return to Test Node", "text")
        self.assertIn("Return to Test Node", screen.render(None), "words stay whole in the bytes")
        far = fw.Screen(40, 3)
        far.text(0, 1, "a", "text")
        far.text(30, 1, "b", "text")
        self.assertEqual(far.render(None).count("H"), 2, "a long gap is jumped over")

    def test_colour_is_set_only_when_it_changes(self):
        screen = fw.Screen(40, 2)
        screen.text(0, 0, "one colour all the way along", "accent")
        self.assertEqual(len(SGR.findall(screen.render(None))), 2, "once to set it, once to reset at the end")


class Colours(unittest.TestCase):
    def frame(self, depth, charset="utf-8"):
        rng = random.Random(16)         # test data only
        screen = random_frame(rng, 60, 20, depth, charset)
        for x in range(60):
            for py in range(10):
                screen.pixel(x, py, (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        return screen

    def codes(self, text):
        return {int(p) for group in SGR.findall(text) for p in group.split(";") if p}

    def test_sixteen_colours_use_only_what_every_ansi_terminal_takes(self):
        text = self.frame("16").render(None)
        allowed = {0, 1, 39, 49} | set(range(30, 38)) | set(range(40, 48))
        self.assertLessEqual(self.codes(text), allowed, "bold for bright, eight backgrounds, nothing else")
        for group in SGR.findall(text):
            self.assertTrue(group.startswith("0"), "every change starts from a reset, so bold never leaks")
        self.assertNotIn("38;", text)
        self.assertNotIn("48;", text)

    def test_256_colours(self):
        text = self.frame("256").render(None)
        self.assertIn("38;5;", text)
        self.assertIn("48;5;", text)
        self.assertNotIn("38;2;", text)
        self.assertNotIn("48;2;", text)

    def test_truecolor(self):
        screen = fw.Screen(10, 2)
        screen.put(1, 0, "x", (1, 2, 3), (4, 5, 6))
        self.assertIn("38;2;1;2;3;48;2;4;5;6", screen.render(None))

    def test_named_inks_have_a_chosen_sixteen_colour_reading(self):
        expect = {"accent": "1;33", "dim": "36", "text": "37", "warn": "1;31", "bright": "1;37",
                  "sea": "34", "land": "32", "cool": "1;36", "warm": "1;35"}
        self.assertEqual(set(expect), set(fw.INKS))
        for name, code in expect.items():
            screen = fw.Screen(4, 1, "16")
            screen.put(0, 0, "x", name)
            self.assertIn(f"\x1b[0;{code};49m", screen.render(None), name)

    def test_nearest_colours(self):
        self.assertEqual(fw.nearest16((0, 0, 0)), 0)
        self.assertEqual(fw.nearest16((250, 250, 250)), 15)
        self.assertEqual(fw.nearest16((250, 250, 250), 8), 7, "among the eight backgrounds")
        self.assertEqual(fw.nearest16((240, 30, 30)), 9)
        self.assertEqual(fw.nearest256((0, 0, 0)), 16)
        self.assertEqual(fw.nearest256((255, 255, 255)), 231)
        self.assertEqual(fw.nearest256((128, 128, 128)), 244, "a grey takes the grey ramp")
        self.assertEqual(fw.nearest256((255, 0, 0)), 196)
        forest, deep = fw.nearest256((52, 112, 72)), fw.nearest256((9, 32, 70))
        self.assertLess(forest, 232, "a dark green stays a green, not a grey")
        self.assertLess(deep, 232, "and deep water stays blue")
        self.assertTrue(all(16 <= fw.nearest256((r, g, b)) <= 255 for r in (0, 90, 200) for g in (10, 130) for b in (0, 255)))

    def test_ascii_frames_are_ascii(self):
        for depth in fw.COLOR_DEPTHS:
            self.assertTrue(self.frame(depth, "ascii").render(None).isascii())


class Hardening(unittest.TestCase):
    def test_colours_out_of_range_are_clamped_never_sent(self):
        screen = fw.Screen(6, 2)
        screen.put(0, 0, "x", (-3, 300, 12.9), (999, -1, 0))
        screen.pixel(1, 0, (-50, 128.7, 400))
        screen.pixel(1, 1, (0, 0, 0))
        screen.pixel(2, 2, (300, 300, 300))
        screen.over(2, 1, "@", "bright", -5.0)       # a darkening that would overshoot
        text = screen.render(None)
        self.assertIn("38;2;0;255;12;48;2;255;0;0", text)
        for group in SGR.findall(text):
            for part in group.split(";"):
                self.assertTrue(part.isdigit() and 0 <= int(part) <= 255, group)
        for depth in ("256", "16"):
            odd = fw.Screen(4, 1, depth)
            odd.put(0, 0, "x", (-9, 900, 40), (500, 500, -500))
            self.assertNotIn("-", odd.render(None))

    def test_only_one_cell_glyphs_reach_the_terminal(self):
        screen = fw.Screen(20, 2)
        for i, ch in enumerate(["\x1b", "\r", "\n", "\x07", "\t", "ab", "", "\u4e2d", "\u0301", "\u200b", "\x9b"]):
            screen.put(i, 0, ch, "text")
        screen.text(0, 1, "a\x1b[2Jb\rc", "text")
        text = screen.render(None)
        term = apply(text, 20, 2)
        self.assertEqual(term.out_of_bounds, [])
        self.assertEqual(term.text().split("\n")[0], "?" * 11)
        self.assertEqual(term.text().split("\n")[1], "a?[2Jb?c", "an escape in text is drawn, not obeyed")
        self.assertEqual(text.count("\x1b"), len(re.findall(r"\x1b\[[0-9;]*[Hm]", text)), "only our own sequences")
        self.assertEqual(fw.safe_glyph("é"), "é")
        self.assertEqual(fw.safe_glyph(fw.HALF_BLOCK), fw.HALF_BLOCK)
        self.assertEqual(fw.safe_glyph("~"), "~")

    def test_ascii_callers_get_ascii_glyphs(self):
        screen = fw.Screen(8, 1, "truecolor", "ascii")
        screen.text(0, 0, fw.HALF_BLOCK + "\u00b7é\u2592x", "text")
        text = screen.render(None)
        self.assertTrue(text.isascii())
        self.assertEqual(apply(text, 8, 1).text(), "#-?:x")
        same = fw.Screen(8, 1, "truecolor", "utf-8")
        same.text(0, 0, "é", "text")
        self.assertIn("é", same.render(None), "a caller who can show it gets it")

    def test_sixteen_colours_end_with_bold_reset(self):
        screen = fw.Screen(10, 1, "16")
        screen.text(0, 0, "bold", "accent")
        text = screen.render(None)
        self.assertTrue(text.endswith("\x1b[0m"))
        self.assertEqual(SGR.findall(text), ["0;1;33;49", "0"])
        after = fw.Screen(10, 1, "16")
        after.text(0, 0, "bold", "accent")
        after.text(5, 0, "dim", "dim")
        self.assertEqual(SGR.findall(after.render(screen)), ["0;36;49", "0"], "the next frame starts from a reset too")

    def test_a_frame_works_out_its_cells_once(self):
        screen = fw.Screen(30, 5)
        screen.text(0, 0, "abc", "text")
        first = screen.cells()
        self.assertIs(screen.cells(), first)
        screen.put(1, 1, "z", "text")
        self.assertIsNot(screen.cells(), first, "drawing again throws the worked-out cells away")
        self.assertEqual(screen.cells()[30 + 1][0], "z")


class Pixels(unittest.TestCase):
    def test_two_pixels_to_a_cell(self):
        screen = fw.Screen(4, 2)
        screen.pixel(1, 0, (200, 0, 0))
        screen.pixel(1, 1, (0, 0, 200))
        self.assertEqual(screen._cell(1), (fw.HALF_BLOCK, "38;2;200;0;0", "48;2;0;0;200"))
        screen.pixel(2, 3, (9, 9, 9))
        self.assertEqual(screen._cell(4 + 2), (fw.HALF_BLOCK, "38;2;0;0;0", "48;2;9;9;9"), "the other half starts black")

    def test_a_cell_of_one_colour_is_a_space(self):
        screen = fw.Screen(4, 2)
        screen.pixel(0, 0, (5, 6, 7))
        screen.pixel(0, 1, (5, 6, 7))
        self.assertEqual(screen._cell(0), (" ", "", "48;2;5;6;7"))

    def test_sixteen_colours_put_the_bright_half_in_front(self):
        screen = fw.Screen(4, 1, "16")
        screen.pixel(0, 0, (255, 255, 85))      # bright yellow over dark blue
        screen.pixel(0, 1, (0, 0, 170))
        self.assertEqual(screen._cell(0), (fw.HALF_BLOCK, "1;33", "44"))
        screen.pixel(1, 0, (0, 0, 170))         # the other way up: the block turns over
        screen.pixel(1, 1, (255, 255, 85))
        self.assertEqual(screen._cell(1), (fw.LOWER_BLOCK, "1;33", "44"))

    def test_ascii_draws_pixels_as_shades(self):
        screen = fw.Screen(4, 1, "truecolor", "ascii")
        screen.pixel(0, 0, (0, 0, 0))
        screen.pixel(0, 1, (0, 0, 0))
        screen.pixel(1, 0, (255, 255, 255))
        screen.pixel(1, 1, (255, 255, 255))
        self.assertEqual((screen._cell(0)[0], screen._cell(1)[0]), (" ", "@"))

    def test_a_glyph_over_pixels_takes_the_cell(self):
        screen = fw.Screen(4, 1)
        screen.pixel(0, 0, (200, 100, 0))
        screen.pixel(0, 1, (100, 100, 200))
        screen.over(0, 0, "@", "bright", 0.5)
        self.assertEqual(screen._cell(0), ("@", "38;2;255;255;255", "48;2;75;50;50"))
        screen.put(1, 0, " ", None, (1, 1, 1))
        screen.over(1, 0, "o", "accent")
        self.assertEqual(screen._cell(1)[2], "48;2;1;1;1", "over plain cells it keeps their background")


class Out:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


class SessionPainting(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        launch = fw.parse_door_info(json.dumps(door_info()), {})
        self.game = fw.PlainFlight(launch, Path(self._dir.name))
        self.session = fw.Session(launch, None, Out(), game=self.game)

    def tearDown(self):
        self._dir.cleanup()

    def paint(self):
        self.session.out.text = ""
        self.session.paint()
        return self.session.out.text

    def test_the_first_paint_clears_and_later_ones_send_differences(self):
        first = self.paint()
        self.assertTrue(first.startswith("\x1b[0m\x1b[2J"))
        self.assertEqual(self.paint(), "", "nothing changed, nothing sent")
        self.game.key("ENTER")
        turn = self.paint()
        self.assertNotIn("\x1b[2J", turn, "a turn never clears the screen")
        self.assertLess(len(turn), len(first) * 0.8)
        term = apply(first, 80, 24)
        apply(turn, 80, 24, term)
        self.assertIn("Day 1  06:15", term.text())
        self.assertEqual(term.out_of_bounds, [])

    def test_looking_at_another_layer_is_a_small_change(self):
        self.game.key("ENTER")
        full = self.paint()
        self.game.key("w")
        look = self.paint()
        self.assertGreater(len(look), 0)
        self.assertLess(len(look), len(full) / 3)

    def test_a_resize_repaints_everything(self):
        self.paint()
        self.session.width, self.session.height = 132, 40
        self.session.shown = None           # what _check_geometry does on a new size
        big = self.paint()
        self.assertTrue(big.startswith("\x1b[0m\x1b[2J"))
        term = apply(big, 132, 40)
        self.assertEqual(term.out_of_bounds, [])
        self.assertNotIn((39, 131), term.written)

    def test_a_bigger_terminal_is_a_bigger_map(self):
        def layout(width, height):
            self.session.width, self.session.height, self.session.shown = width, height, None
            lines = apply(self.paint(), width, height).text().split("\n")
            you = next((row, line.index("@")) for row, line in enumerate(lines) if "@" in line)
            return lines, you
        for width, height in ((80, 24), (132, 40), (168, 46)):
            lines, (row, col) = layout(width, height)
            cols, rows = width - 28, height - 8
            self.assertTrue(1 + rows // 4 <= row <= 1 + rows * 3 // 4, "the balloon is in the middle half of the map")
            self.assertTrue(1 + cols // 4 <= col <= 1 + cols * 3 // 4)
            self.assertIn("LAYER", lines[1][width - 26:])
            self.assertIn("[Enter] Launch", lines[height - 1])
            self.assertEqual((self.game.map.cols, self.game.map.rows), (cols, rows), "the map takes what the window gives")

    def test_ctrl_l_redraws_the_whole_screen(self):
        first = self.paint()
        self.assertEqual(self.paint(), "")
        for key in ("CTRL-L", "CTRL-R"):
            self.session._handle(key)
            again = self.paint()
            self.assertTrue(again.startswith("\x1b[0m\x1b[2J"), "a clear, then everything")
            self.assertEqual(apply(again, 80, 24).text(), apply(first, 80, 24).text())
        self.assertEqual(self.game.state.turn, 0, "a redraw is not a move")
        self.session.notice = "About 1 minute of session time left."
        self.session._handle("CTRL-L")
        self.assertTrue(self.session.notice, "and it does not dismiss a notice")

    def test_every_depth_and_charset_says_the_same_thing(self):
        # The map is drawn differently for each reading. Everything written
        # beside and below it must read the same in all of them.
        texts = set()
        for depth in fw.COLOR_DEPTHS:
            for charset in fw.CHARSETS:
                self.session.launch = dataclasses.replace(self.session.launch, color_depth=depth, charset=charset)
                self.session.shown = None
                lines = apply(self.paint(), 80, 24).text().split("\n")
                texts.add("\n".join([lines[0]] + [line[54:] for line in lines[1:17]] + lines[17:]))
        self.assertEqual(len(texts), 1, "colour and charset change how it is drawn, never what it says")


if __name__ == "__main__":
    unittest.main()
