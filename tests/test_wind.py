"""S08: wind on the map, moving in short bursts."""
import json
import tempfile
import unittest
from pathlib import Path

from support import Terminal, door_info

import fair_winds as fw

WORLD = fw.World(fw.world_seed("wind-node"))
WEATHER = fw.Weather(WORLD)


def launch(**changes):
    return fw.parse_door_info(json.dumps(door_info(**changes)), {})


class Out:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


class Keys:
    """Input that hands over what it was given. A read that gets nothing has waited its whole timeout."""

    def __init__(self, *chunks):
        self.chunks, self.waits, self.clock = list(chunks), [], None

    def read(self, timeout):
        self.waits.append(timeout)
        data = self.chunks.pop(0) if self.chunks else None
        if data is None and self.clock is not None:
            self.clock.now += timeout
        return data


class Clock:
    """Time that moves only when told to, or by a fixed cost per paint."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Streaks(unittest.TestCase):
    def view(self, pixels=True, wind=(40.0, 0.0)):
        v = fw.MapView(WORLD, WEATHER, pixels)
        self.screen = fw.Screen(80, 24, "truecolor" if pixels else "16")
        v.draw(self.screen, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        v.set_wind("test", lambda wx, wy: wind)
        return v

    def test_the_wind_is_taken_once_per_key(self):
        v = self.view()
        self.assertFalse(v.set_wind("test", lambda wx, wy: (0.0, 0.0)), "the same wind again changes nothing")
        self.assertTrue(v.set_wind("other", lambda wx, wy: (0.0, 9.0)))
        self.assertEqual(v._wind_at(10.0, 10.0), (0.0, 9.0))
        self.assertEqual(len(v._wind), (52 * 5 // 20 + 2) * (16 * 10 // 20 + 2), "sampled every 20 km")

    def test_the_wind_is_blended_between_samples(self):
        v = self.view()
        v._wind_key = None
        v.set_wind("slope", lambda wx, wy: (wx - v.x0, 0.0))
        for px in (0.0, 3.0, 10.5, 40.0):
            self.assertAlmostEqual(v._wind_at(px, 7.0)[0], px * 5.0)

    def test_streaks_run_with_the_wind(self):
        v = self.view(wind=(40.0, 0.0))
        self.assertEqual(len(v._streaks), max(fw.STREAKS_MIN, 52 * 16 // fw.STREAK_CELLS_EACH))
        before = [(s[0], s[1], s[2]) for s in v._streaks]
        v.advance()
        moved = 0
        for (x, y, age), streak in zip(before, v._streaks):
            if streak[2] == age + 1:                       # not one that just started again
                self.assertAlmostEqual(streak[0] - x, 40.0 * fw.STREAK_PX_PER_KMH)
                self.assertEqual(streak[1], y)
                moved += 1
        self.assertGreater(moved, len(before) // 2)
        for _ in range(200):
            v.advance()
        for x, y, age, trail in v._streaks:
            self.assertTrue(0 <= x < 52 and 0 <= y < 32, "a streak that leaves starts again inside")
            self.assertLessEqual(len(trail), fw.STREAK_TRAIL)
            self.assertLessEqual(age, fw.STREAK_LIFE)
            self.assertTrue(all(0 <= px < 52 and 0 <= py < 32 for px, py in trail))

    def test_the_first_picture_already_shows_the_flow(self):
        v = self.view()
        self.assertGreater(sum(len(s[3]) > 1 for s in v._streaks), len(v._streaks) // 2)

    def test_streaks_brighten_the_ground_and_stay_on_the_map(self):
        v = self.view()
        plain = fw.Screen(80, 24)
        v.draw(plain, 1, 1, 52, 16, 400.0, 300.0, 1, 600)
        v.draw_wind(self.screen)
        changed = 0
        for k in range(80 * 24):
            x, y = k % 80, k // 80
            if self.screen.fg[k] != plain.fg[k] or self.screen.bg[k] != plain.bg[k]:
                changed += 1
                self.assertTrue(1 <= x <= 52 and 1 <= y <= 16, "wind is drawn on the map and nowhere else")
                for mine, theirs in ((self.screen.fg[k], plain.fg[k]), (self.screen.bg[k], plain.bg[k])):
                    self.assertTrue(all(a >= b for a, b in zip(mine, theirs)), "it only ever lightens")
        self.assertGreater(changed, 10)

    def test_the_same_window_seeds_the_same_streaks(self):
        a, b = self.view(), self.view()
        self.assertEqual(a._streaks, b._streaks, "decoration, but not chance: the same window, the same picture")

    def test_the_character_map_shows_wind_as_lines(self):
        for wind, glyph in (((40.0, 0.0), "-"), ((0.0, 30.0), "|"), ((25.0, 25.0), "\\"), ((25.0, -25.0), "/"),
                            ((-25.0, 25.0), "/"), ((-25.0, -25.0), "\\")):
            v = self.view(pixels=False, wind=wind)
            before = list(self.screen.ch)
            v.draw_wind(self.screen)
            drawn = {self.screen.ch[k] for k in range(80 * 24) if self.screen.ch[k] != before[k]}
            self.assertEqual(drawn, {glyph}, wind)
            again = list(self.screen.ch)
            v.advance()
            v.draw_wind(self.screen)
            self.assertEqual(self.screen.ch, again, "the lines hold still")
        calm = self.view(pixels=False, wind=(1.0, 1.0))
        before = list(self.screen.ch)
        calm.draw_wind(self.screen)
        self.assertEqual(self.screen.ch, before, "a calm says nothing")


class GameCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")

    def tearDown(self):
        self._dir.cleanup()

    def start(self, environ=None, keys=(), **changes):
        info = launch(**changes)
        game = fw.PlainFlight(info, Path(self._dir.name), environ or {})
        clock = Clock()
        session = fw.Session(info, Keys(*keys), Out(), clock=clock, game=game)
        session.inp.clock = clock
        return game, session, clock

    def paint(self, session):
        session.out.text = ""
        session.paint()
        return session.out.text


class Motion(GameCase):
    def test_who_gets_moving_wind(self):
        self.assertTrue(fw.want_motion(launch(), {}))
        self.assertTrue(fw.want_motion(launch(), {"DOOR_FW_MOTION": "burst"}))
        self.assertFalse(fw.want_motion(launch(), {"DOOR_FW_MOTION": " OFF "}))
        self.assertFalse(fw.want_motion(launch(), {"DOOR_SCENES": "0"}), "scenes off means motion off")
        self.assertTrue(fw.want_motion(launch(), {"DOOR_SCENES": "1"}))
        game, _, _ = self.start(color_depth="16")
        self.assertFalse(game.motion, "the character map's wind holds still")

    def test_a_turn_starts_a_burst_and_then_it_holds_still(self):
        game, session, _ = self.start()
        self.paint(session)
        game.key("ENTER")
        self.assertEqual(game.frames_left, fw.BURST_FRAMES)
        self.paint(session)
        frames = 0
        while game.frames_left > 0:
            game.tick()
            self.assertGreater(len(self.paint(session)), 0, "each frame moves the wind")
            frames += 1
        self.assertEqual(frames, fw.BURST_FRAMES)
        self.assertEqual(self.paint(session), "", "then nothing moves and nothing is sent")
        game.tick()
        self.assertEqual(self.paint(session), "", "and a tick after the burst does nothing")

    def test_looking_at_another_layer_shows_that_layers_wind(self):
        game, session, _ = self.start()
        game.key("ENTER")
        self.paint(session)
        self.assertEqual(game.shown_layer(), 1)
        while game.frames_left:
            game.tick()
        self.paint(session)
        game.key("w")
        self.assertEqual(game.frames_left, 0, "a key ends whatever was moving")
        self.assertEqual(game.shown_layer(), 2)
        self.paint(session)
        self.assertEqual(game.frames_left, fw.LOOK_FRAMES, "the new layer's wind moves for a moment")
        self.assertEqual(game.map._wind_key[0][2:], (2, False), "layer 2, as a forecast: it has not been read")

    def test_any_key_ends_a_burst(self):
        game, session, _ = self.start()
        game.key("ENTER")
        self.assertGreater(game.frames_left, 0)
        game.key("x")
        self.assertEqual(game.frames_left, 0)

    def test_motion_off_shows_a_still_pattern(self):
        for environ in ({"DOOR_FW_MOTION": "off"}, {"DOOR_SCENES": "0"}):
            game, session, _ = self.start(environ)
            first = self.paint(session)
            self.assertIn("38;2;", first)
            self.assertEqual(self.paint(session), "")
            game.key("ENTER")
            self.assertEqual(game.frames_left, 0)
            self.assertGreater(len(self.paint(session)), 0)
            self.assertEqual(self.paint(session), "", "still, turn after turn")
            game.key("w")
            self.paint(session)
            self.assertEqual(game.frames_left, 0)

    def test_the_balloon_and_the_post_sit_on_top_of_the_wind(self):
        game, session, _ = self.start()
        game.key("ENTER")
        for _ in range(fw.BURST_FRAMES):
            game.tick()
            self.paint(session)
            frame = session.frame
            at = game.map.cell_of(game.state.x, game.state.y)
            self.assertEqual(frame.ch[at[1] * 80 + at[0]], "@")
            term = Terminal(80, 24)
            term.feed(session.frame.render(None))
            self.assertEqual(term.out_of_bounds, [])


class Loop(GameCase):
    def burst_steps(self, game, session, limit=60):
        steps = 0
        while game.frames_left > 0 and steps < limit:
            session.step()
            steps += 1
        return steps

    def test_while_the_wind_moves_the_loop_runs_at_the_frame_rate(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.step()                                  # paints, reads Enter, flies the turn
        self.assertEqual(game.frames_left, fw.BURST_FRAMES)
        began = clock.now
        steps = self.burst_steps(game, session)
        self.assertEqual(steps, fw.BURST_FRAMES, "one frame a step")
        self.assertAlmostEqual(clock.now - began, fw.BURST_FRAMES * fw.FRAME_SECONDS, places=6)
        waits = session.inp.waits[1:1 + fw.BURST_FRAMES]
        self.assertTrue(all(abs(w - fw.FRAME_SECONDS) < 1e-6 for w in waits), waits)
        session.step()
        self.assertEqual(session.inp.waits[-1], fw.IDLE_TIMEOUT, "then back to idling")

    def test_a_slow_paint_skips_frames_and_the_burst_ends_on_time(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.step()
        began = clock.now
        real_paint = session.paint

        def slow_paint():
            real_paint()
            clock.now += fw.FRAME_SECONDS * 3.2          # this frame took more than three slots
        session.paint = slow_paint
        steps = self.burst_steps(game, session)
        self.assertLess(steps, fw.BURST_FRAMES / 2, "it skipped ahead")
        self.assertEqual(game.frames_left, 0)
        self.assertLess(clock.now - began, (fw.BURST_FRAMES + 5) * fw.FRAME_SECONDS, "about when it would have: one slow frame late at most")
        self.assertTrue(all(w >= 0.01 for w in session.inp.waits))

    def test_no_more_than_the_byte_budget_goes_out_in_a_second(self):
        # A pipe takes a whole burst without blocking, so timing a write says
        # nothing. Count what is sent instead.
        game, session, clock = self.start(keys=[b"\r"])
        session.step()
        session.out.text = ""
        sent = []
        real_write = session.out.write

        def write(text):
            sent.append((clock.now, len(text)))
            real_write(text)
        session.out.write = write
        real_render = fw.Screen.render
        try:
            fw.Screen.render = lambda self, previous=None: (real_render(self, previous) or "") + "x" * 20000
            steps = self.burst_steps(game, session)
            session.step()                               # the frame that stays
        finally:
            fw.Screen.render = real_render
        self.assertEqual(game.frames_left, 0)
        self.assertLess(len(sent), fw.BURST_FRAMES, "frames were held back")
        for when, _ in sent:
            window = sum(size for at, size in sent if when - 1.0 < at <= when)
            self.assertLess(window, fw.BURST_BYTES_PER_SECOND + 21000, "never more than the budget and one frame")
        self.assertGreaterEqual(len(sent), 2, "but the wind still moved, and its last frame was sent")

    def test_bytes_that_are_not_a_key_do_not_stall_the_wind(self):
        focus = b"\x1b[I"                                # a terminal's focus report: a sequence, no key
        game, session, clock = self.start(keys=[b"\r"] + [focus, None] * 40)
        session.step()
        began = clock.now
        steps = self.burst_steps(game, session, limit=200)
        self.assertEqual(game.frames_left, 0, "the burst ran to its end")
        self.assertAlmostEqual(clock.now - began, fw.BURST_FRAMES * fw.FRAME_SECONDS, places=6)

    def test_a_key_during_a_burst_is_obeyed_at_once(self):
        game, session, clock = self.start(keys=[b"\r", None, None, b"w"])
        for _ in range(4):
            session.step()
        self.assertEqual(game.choice, "climb")
        self.assertEqual(game.state.turn, 1, "looking is still not flying")

    def test_ctrl_l_ends_a_burst_and_redraws(self):
        game, session, clock = self.start(keys=[b"\r", None, b"\x0c"])
        for _ in range(3):
            session.step()
        self.assertEqual(game.frames_left, 0)
        session.out.text = ""
        session.step()
        self.assertTrue(session.out.text.startswith("\x1b[0m\x1b[2J"))

    def test_help_stops_the_wind(self):
        game, session, clock = self.start(keys=[b"\r", b"?"])
        session.step()
        session.step()
        self.assertEqual(session.view, "help")
        session.step()
        self.assertEqual(session.inp.waits[-1], fw.IDLE_TIMEOUT)

    def test_a_still_screen_sends_nothing_however_long_it_idles(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.step()
        self.burst_steps(game, session)
        session.step()
        session.out.text = ""
        for _ in range(200):
            session.step()
        self.assertEqual(session.out.text, "")
        self.assertTrue(all(w == fw.IDLE_TIMEOUT for w in session.inp.waits[-200:]))


class Budget(GameCase):
    """Targets (docs/systems.md): under 40 KB a second while the wind moves, and
    under a quarter of one core. Measured on the development host at seven
    frames a second: 8 KB/s and 4% at 80 x 24, about 37 KB/s and 15% at
    168 x 46 in truecolor. The bounds here are loose for shared CI runners."""

    def burst(self, **changes):
        game, session, _ = self.start(**changes)
        self.paint(session)
        game.key("ENTER")
        self.paint(session)
        sizes = []
        for _ in range(3):
            game.key("ENTER")
            self.paint(session)
            while game.frames_left > 0:
                game.tick()
                sizes.append(len(self.paint(session)))
        return sum(sizes) / len(sizes)

    def test_bytes_while_the_wind_moves(self):
        small = self.burst()
        wide = self.burst(terminal_width=168, terminal_height=46)
        self.assertLess(small / fw.FRAME_SECONDS, 16 * 1024)
        self.assertLess(wide / fw.FRAME_SECONDS, 48 * 1024)
        self.assertLess(self.burst(terminal_width=168, terminal_height=46, color_depth="256") / fw.FRAME_SECONDS, 28 * 1024)


if __name__ == "__main__":
    unittest.main()
