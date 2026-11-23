"""Scenes (S09): the splash, and the pictures for a launch and a landing."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fair_winds as fw                                   # noqa: E402
from support import Door, door_info, stored  # noqa: E402
#                       # noqa: E402
from test_wind import GameCase                            # noqa: E402


def brightness(colour):
    return sum(colour) / 3


class Sky(unittest.TestCase):
    def test_the_sky_follows_the_hour(self):
        night, dawn, noon, dusk = (fw.sky_stops(h) for h in (1.0, 6.2, 12.0, 19.6))
        self.assertLess(brightness(night[0]), 30)
        self.assertGreater(noon[0][2], noon[0][0], "a blue noon")
        self.assertGreater(dawn[3][0], dawn[3][2], "a warm horizon at dawn")
        self.assertGreater(dusk[3][0], dusk[3][2], "and at dusk")
        self.assertEqual(fw.sky_stops(0.0), fw.sky_stops(24.0))
        self.assertEqual(fw.sky_stops(30.0), fw.sky_stops(6.0))
        for h in range(0, 240):                          # no jumps: each ten minutes is near the last
            a, b = fw.sky_stops(h / 10), fw.sky_stops((h + 1) / 10)
            for i in range(4):
                self.assertLess(max(abs(a[i][k] - b[i][k]) for k in range(3)), 30)

    def test_a_backdrop_fills_the_screen_and_is_the_same_twice(self):
        one = fw.Backdrop(80, 24, 19.7, 0.5, fw.FOREST, 23, True)
        two = fw.Backdrop(80, 24, 19.7, 0.5, fw.FOREST, 23, True)
        self.assertEqual((len(one.tops), len(one.tops[0]), len(one.bottoms)), (24, 80, 24))
        self.assertEqual(one.tops, two.tops)
        self.assertEqual(one.bottoms, two.bottoms)
        other = fw.Backdrop(80, 24, 19.7, 0.5, fw.FOREST, 24, True)
        self.assertNotEqual(one.tops, other.tops, "another place is another picture")
        for row in one.tops + one.bottoms:
            for c in row:
                self.assertTrue(all(0 <= v <= 255 for v in c))

    def test_the_ground_is_one_tone_where_colours_are_few(self):
        deep = fw.Backdrop(80, 24, 12.0, seed=5)
        flat = fw.Backdrop(80, 24, 12.0, seed=5, flat=True)
        self.assertGreater(len({row[40] for row in deep.bottoms[20:]}), 1, "truecolor fades")
        self.assertEqual(len({row[40] for row in flat.bottoms[20:]}), 1)
        screen = fw.Screen(80, 24, "256")
        scene = fw.Scene("splash", 80, 24, True, True, flat=True)
        scene.draw(screen)
        self.assertNotIn("\x1b[48;5;16m", screen.render(None).split("Press")[0][-3000:], "no black at the foot")

    def test_night_is_darker_than_day_and_has_stars(self):
        day = fw.Backdrop(80, 24, 12.0, seed=5)
        night = fw.Backdrop(80, 24, 23.0, seed=5)
        mean = lambda b: sum(brightness(c) for row in b.tops for c in row) / (80 * 24)
        self.assertLess(mean(night) * 2, mean(day))
        sky = [c for row in night.tops[:8] for c in row]
        self.assertTrue(any(brightness(c) > 120 for c in sky), "stars")

    def test_a_town_lights_its_windows_from_dusk(self):
        lamp = (255, 214, 124)
        count = lambda b: sum(c == lamp for row in b.tops + b.bottoms for c in row)
        self.assertEqual(count(fw.Backdrop(132, 40, 12.0, seed=11, place=True)), 0)
        self.assertGreater(count(fw.Backdrop(132, 40, 19.7, seed=11, place=True)), 0)
        self.assertGreater(count(fw.Backdrop(132, 40, 23.0, seed=11, place=True)), 0)
        self.assertEqual(count(fw.Backdrop(132, 40, 23.0, seed=11, place=False)), 0, "the wild has no roofs")


class Playing(unittest.TestCase):
    def test_a_launch_and_a_landing_end_by_themselves_and_the_splash_holds(self):
        for kind in ("launch", "landing"):
            scene = fw.Scene(kind, 80, 24, True, True)
            n = 0
            while not scene.over:
                scene.advance()
                n += 1
                self.assertLess(n, 40)
            self.assertLess(n * fw.SCENE_FRAME_SECONDS, 3.0, "under three seconds")
        splash = fw.Scene("splash", 80, 24, True, True)
        splash.advance(500)
        self.assertFalse(splash.over)
        self.assertEqual(splash.index, splash.frames - 1)

    def test_however_many_frames_are_skipped_the_last_pose_is_shown(self):
        scene = fw.Scene("landing", 80, 24, True, True)
        scene.advance(scene.frames - 2)
        scene.advance(5)
        self.assertEqual(scene.index, scene.frames - 1)
        self.assertFalse(scene.over)
        scene.advance(50)
        self.assertTrue(scene.over)

    def test_the_balloon_moves_and_the_words_are_there(self):
        scene = fw.Scene("landing", 80, 24, True, True, hour=19.7, place=True, caption="Down at Gull Post.", sub="Delivered.")
        first = fw.Screen(80, 24)
        scene.draw(first)
        scene.advance(scene.frames)
        last = fw.Screen(80, 24)
        scene.draw(last)
        self.assertNotEqual(first.fg, last.fg)
        rows = ["".join(last.ch[r * 80:(r + 1) * 80]) for r in range(24)]
        self.assertIn("Down at Gull Post.", rows[21])
        self.assertIn("Delivered.", rows[22])

    def test_the_splash_title_and_prompt(self):
        scene = fw.Scene("splash", 132, 40, True, True)
        early = fw.Screen(132, 40)
        scene.draw(early)
        self.assertNotIn("Press any key", "".join(early.ch), "not until the balloon is up")
        scene.advance(99)
        screen = fw.Screen(132, 40)
        scene.draw(screen)
        text = "".join(screen.ch)
        self.assertIn("Press any key", text)
        self.assertIn("Read the wind. Carry the post. Finish the map.", text)

    def test_without_pixels_the_splash_is_words(self):
        scene = fw.Scene("splash", 80, 24, False, False)
        self.assertIsNone(scene.backdrop)
        screen = fw.Screen(80, 24, "16", "cp437")
        scene.draw(screen)
        text = "".join(screen.ch)
        self.assertIn("F A I R   W I N D S", text)
        self.assertIn("Press any key", text)

    def test_a_scene_fits_every_layout(self):
        for size in ((80, 24), (132, 40), (168, 46), (250, 70), (80, 60)):
            for kind in ("splash", "launch", "landing"):
                scene = fw.Scene(kind, size[0], size[1], True, True, hour=21.0, cloud=0.8, place=True,
                                 caption="x" * 300, sub="y" * 300)
                while True:
                    scene.draw(fw.Screen(size[0], size[1]))
                    if scene.over or (scene.hold and scene.index == scene.frames - 1):
                        break
                    scene.advance()


class InSession(GameCase):
    def play(self, session, clock, steps=80):
        for _ in range(steps):
            if session.scene is None:
                return
            session.step()
        self.fail("the scene did not end")

    def test_a_launch_plays_its_scene_after_the_turn_is_saved(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.scenes_on = True
        session.step()
        self.assertIsNotNone(session.scene)
        self.assertEqual(session.scene.kind, "launch")
        self.assertIn("Up and away", session.scene.caption)
        saved = stored(self._dir.name)
        self.assertEqual(saved["state"]["status"], "aloft", "saved before the scene began")
        self.play(session, clock)
        self.assertIsNone(session.scene)
        self.assertEqual(game.state.turn, 1)
        session.out.text = ""
        session.step()
        self.assertTrue(session.out.text, "the game is painted again when the scene ends")

    def test_any_key_ends_a_scene_and_is_not_also_a_move(self):
        game, session, clock = self.start(keys=[b"\r", None, None, None, b"\r"])
        session.scenes_on = True
        for _ in range(4):
            session.step()
        self.assertIsNotNone(session.scene)
        session.step()                                   # the second Enter arrives during the scene
        self.assertIsNone(session.scene)
        self.assertEqual(game.state.turn, 1, "it ended the scene and held nothing")

    def test_a_key_in_the_first_moment_of_a_scene_does_not_skip_it(self):
        game, session, clock = self.start(keys=[b"\r", b"\r", b"\r"])   # Enter held down
        session.scenes_on = True
        session.step()
        session.step()
        session.step()
        self.assertIsNotNone(session.scene, "auto-repeat is not a wish to skip")
        self.assertEqual(game.state.turn, 1, "and it is still not a move")
        game, session, clock = self.start(keys=[b"\r", b"\x03"], user_id=77)
        session.scenes_on = True
        session.step()
        session.step()
        self.assertEqual(session.stop, "quit", "Ctrl-C is never held off")

    def test_no_scene_of_a_turn_that_was_not_saved(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.scenes_on = True
        game.save = lambda action="": setattr(game, "saved_ok", False)
        session.step()
        self.assertIsNone(session.scene)
        self.assertIsNone(game.pending_scene)

    def test_the_wind_moves_after_a_scene_as_after_any_turn(self):
        game, session, clock = self.start(keys=[b"\r"], terminal_width=168, terminal_height=46)
        session.scenes_on = True
        session.step()
        self.play(session, clock)
        self.assertEqual(game.frames_left, fw.BURST_FRAMES, "the burst waited for the scene")
        self.assertEqual(session._sent, [], "and the scene's bytes are not held against it")
        writes = []
        real = session.out.write
        session.out.write = lambda text: (writes.append(len(text)), real(text))
        for _ in range(fw.BURST_FRAMES + 3):
            session.step()
        self.assertEqual(game.frames_left, 0)
        self.assertGreaterEqual(len(writes), fw.BURST_FRAMES - 2, "nearly every frame was painted, not one lump")

    def test_a_stranding_and_a_ditching_are_said_under_the_picture(self):
        game, session, clock = self.start()
        game.heard = []
        game._say("21:15  " + game._describe(("land", "hard", None)))
        game._say("21:15  " + game._describe(("stranded",)))
        asked = game._scene_for("landing")
        self.assertEqual(asked["caption"], "Down in the wild: a hard landing.")
        self.assertIn("stranded", asked["sub"])
        game.heard = []
        game._say("09:30  " + game._describe(("ditch",)))
        asked = game._scene_for("landing")
        self.assertTrue(asked["caption"].startswith("Down on the water"))
        self.assertEqual(asked["sub"], "")
        game.heard = []
        game._say("23:45  " + game._describe(("midnight",)))
        self.assertTrue(game._scene_for("landing")["caption"].startswith("Midnight"))

    def test_a_landing_caption_is_the_landing_however_much_else_was_said(self):
        game, session, clock = self.start()
        game.heard = []
        game._say("17:45  Down at Thistle Cairn: a clean landing.")
        game._say("Delivered to Thistle Cairn: mailbag, 36 postmarks. That makes 4.")
        game._say("New post aboard, for " + ", ".join(["Somewhere With A Long Name"] * 5) + ".")
        self.assertFalse(any("Down at" in line for line in game.log), "the log has moved on")
        asked = game._scene_for("landing")
        self.assertEqual(asked["caption"], "Down at Thistle Cairn: a clean landing.")
        self.assertEqual(asked["sub"], "Delivered to Thistle Cairn: mailbag, 36 postmarks. That makes 4.")

    def test_ctrl_c_and_a_lost_caller_still_end_the_session_from_a_scene(self):
        game, session, clock = self.start(keys=[b"\r", None, None, None, b"\x03"])
        session.scenes_on = True
        for _ in range(5):
            session.step()
        self.assertEqual(session.stop, "quit")
        game, session, clock = self.start(keys=[b"\r", b""], user_id=78)
        session.scenes_on = True
        session.step()
        session.step()
        self.assertEqual(session.stop, "eof")

    def test_no_scenes_unless_they_are_on_and_the_terminal_shows_pictures(self):
        game, session, clock = self.start(keys=[b"\r"])
        session.step()
        self.assertIsNone(session.scene, "off unless main switches them on")
        self.assertIsNone(game.pending_scene, "and the request does not wait around")
        game, session, clock = self.start(keys=[b"\r"], color_depth="16")
        session.scenes_on = True
        session.step()
        self.assertIsNone(session.scene, "no picture to show in sixteen colours")

    def test_a_landing_plays_its_scene_with_what_happened(self):
        game, session, clock = self.start()
        session.scenes_on = True
        game.key("ENTER")
        game.pending_scene = None
        for _ in range(200):
            if "land" in game.engine.actions(game.state):
                game.key("l")
                game.key("ENTER")
                break
            game.key("ENTER")
        self.assertNotEqual(game.state.status, "aloft")
        asked = game.pending_scene
        self.assertEqual(asked["kind"], "landing")
        self.assertTrue(asked["caption"])
        self.assertNotEqual(asked["caption"], "Down.", "it says what the logbook said")
        self.assertTrue(0.0 <= asked["hour"] <= 24.0)
        session._take_scene()
        self.assertEqual(session.scene.kind, "landing")

    def test_the_splash_holds_for_a_key_and_then_the_game_is_there(self):
        game, session, clock = self.start(keys=[None] * 40 + [b" "])
        session.start_splash()
        for _ in range(40):
            session.step()
        self.assertIsNotNone(session.scene, "it holds")
        self.assertIn("Press any key", "".join(session.shown.ch))
        before = len(session.out.text)
        session.step()                                   # the key
        self.assertIsNone(session.scene)
        self.assertEqual(game.state.turn, 0, "the key was the splash's")
        session.step()
        self.assertGreater(len(session.out.text), before)

    def test_a_still_splash_sends_nothing(self):
        game, session, clock = self.start(keys=[None] * 60)
        session.start_splash()
        for _ in range(30):
            session.step()
        session.out.text = ""
        for _ in range(20):
            session.step()
        self.assertEqual(session.out.text, "")

    def test_a_resize_ends_a_scene(self):
        game, session, clock = self.start(keys=[None] * 10)
        session.start_splash()
        session.step()
        real = fw.current_geometry
        try:
            fw.current_geometry = lambda launch: (100, 30)
            session.resized = True
            session.step()
        finally:
            fw.current_geometry = real
        self.assertIsNone(session._scene_at, "nothing of the scene's timing is left")
        self.assertIsNone(session.scene)
        self.assertEqual((session.width, session.height), (100, 30))

    def test_no_splash_on_a_screen_too_small(self):
        game, session, clock = self.start(terminal_width=60, terminal_height=20)
        session.start_splash()
        self.assertIsNone(session.scene)

    def test_dear_frames_are_shown_less_often_and_the_scene_still_ends_on_time(self):
        def run(width, height):
            game, session, clock = self.start(keys=[b"\r"], terminal_width=width, terminal_height=height,
                                              user_id=width)      # a pilot of their own: both launch
            session.scenes_on = True
            session.step()
            began = clock.now
            session.out.text = ""
            session.step()                               # the backdrop
            sizes, last = [], len(session.out.text)
            for _ in range(80):
                if session.scene is None:
                    break
                session.step()
                if len(session.out.text) != last:
                    sizes.append(len(session.out.text) - last)
                    last = len(session.out.text)
            return sizes, clock.now - began
        small, small_time = run(80, 24)
        wide, wide_time = run(168, 46)
        self.assertLess(len(wide), len(small), "fewer, bigger frames on the wide screen")
        self.assertLess(sum(wide) / wide_time, fw.BURST_BYTES_PER_SECOND, "and under the cap on bytes a second")
        self.assertAlmostEqual(wide_time, small_time, delta=0.5)


class RealDoor(unittest.TestCase):
    def test_the_door_opens_on_the_splash_and_a_key_goes_to_the_game(self):
        with Door(environ={"DOOR_SPLASH": "1", "DOOR_SCENES": "1"}) as door:
            door.wait_for("Press any key", timeout=15)
            door.send(b" ")
            door.wait_for("Enter", timeout=15)
            door.send(b"q")
            self.assertEqual(door.wait_exit(), 0)

    def test_the_sysop_can_switch_the_splash_off(self):
        with Door(environ={"DOOR_SPLASH": "0", "DOOR_SCENES": "1"}) as door:
            door.wait_for("Enter", timeout=15)
            self.assertNotIn("Press any key", door.screen().text())
            door.send(b"q")
            self.assertEqual(door.wait_exit(), 0)

    def test_with_the_scenes_off_the_splash_is_still_there(self):
        with Door(environ={"DOOR_SPLASH": "1", "DOOR_SCENES": "0"}) as door:
            door.wait_for("Press any key", timeout=15)
            door.send(b"q")                              # the splash's key, not the game's
            door.wait_for("Enter", timeout=15)
            door.send(b"q")
            self.assertEqual(door.wait_exit(), 0)


if __name__ == "__main__":
    unittest.main()
