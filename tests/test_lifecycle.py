"""S01: the real door over real pipes, from the first byte to the last."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from support import DOOR, ROOT, Door, door_info

LEAVE = "\x1b[0m\x1b[?25h\x1b[2J\x1b[H"


class Lifecycle(unittest.TestCase):
    def assert_clean_exit(self, door, code=0):
        self.assertEqual(door.wait_exit(), code)
        self.assertTrue(door.output.endswith(LEAVE), "the terminal is restored on the way out")
        self.assertEqual(door.errors, "")

    def test_quit(self):
        with Door() as door:
            self.assertTrue(door.wait_for("Return to Test Node"))
            screen = door.screen()
            self.assertIn("Mara", screen.text())
            self.assertIn("80 x 24", screen.text())
            self.assertTrue(screen.cursor_hidden)
            door.send("q")
            self.assert_clean_exit(door)
            self.assertFalse(door.screen().cursor_hidden)

    def test_capital_q_and_ctrl_c_quit_too(self):
        for key in ("Q", b"\x03"):
            with self.subTest(key=key), Door() as door:
                self.assertTrue(door.wait_for("Return to"))
                door.send(key)
                self.assert_clean_exit(door)

    def test_end_of_input(self):
        with Door() as door:
            self.assertTrue(door.wait_for("Return to"))
            started = time.monotonic()
            door.proc.stdin.close()
            self.assert_clean_exit(door)
            self.assertLess(time.monotonic() - started, 3)

    def test_broken_output(self):
        # A plain child here: the Door helper keeps a reader on stdout, and
        # closing a pipe under a blocked reader hangs on Windows.
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            info = os.path.join(folder, "door_info.json")
            with open(info, "w", encoding="utf-8") as f:
                json.dump(door_info(), f)
            env = dict(os.environ, NETBBS_DOOR_INFO=info)
            proc = subprocess.Popen([sys.executable, str(DOOR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            try:
                seen = b""
                while b"Return to" not in seen:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    self.assertTrue(chunk, "the door closed before drawing")
                    seen += chunk
                proc.stdout.close()
                proc.stdin.write(b"?")      # forces a repaint into the closed pipe
                proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertNotIn(b"Traceback", proc.stderr.read())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdin.close()
                proc.stderr.close()

    @unittest.skipIf(os.name == "nt", "Windows cannot deliver SIGTERM to a child")
    def test_sigterm(self):
        with Door() as door:
            self.assertTrue(door.wait_for("Return to"))
            door.proc.send_signal(signal.SIGTERM)
            self.assert_clean_exit(door)

    def test_session_limit(self):
        with Door(door_info(session_limit_seconds=4)) as door:
            self.assertTrue(door.wait_for("Return to"))
            started = time.monotonic()
            self.assertEqual(door.wait_exit(timeout=8), 0)
            self.assertLess(time.monotonic() - started, 4, "leaves before the host's limit")
            self.assertIn("Session time is up", door.output)
            self.assertTrue(door.output.endswith(LEAVE))

    def test_session_limit_warning(self):
        with Door(door_info(session_limit_seconds=61.5)) as door:
            self.assertTrue(door.wait_for("of session time left", timeout=6))
            door.send("q")
            self.assert_clean_exit(door)

    def test_help_and_back(self):
        with Door() as door:
            self.assertTrue(door.wait_for("Return to"))
            door.send("?")
            self.assertTrue(door.wait_for("A balloon cannot steer"))
            door.send("b")
            time.sleep(0.5)
            self.assertIn("Nearest post", door.screen().text())
            self.assertIsNone(door.proc.poll(), "B in help goes back, it does not quit")
            door.send("q")
            self.assert_clean_exit(door)

    def test_split_arrow_is_not_a_quit_or_a_stray_key(self):
        with Door() as door:
            self.assertTrue(door.wait_for("Return to"))
            door.send(b"\x1b")
            time.sleep(0.01)
            door.send(b"[A")
            time.sleep(0.4)
            self.assertIsNone(door.proc.poll())
            self.assertIn("Nearest post", door.screen().text())
            door.send("q")
            self.assert_clean_exit(door)

    def test_undersize_then_resize(self):
        with Door(door_info(terminal_width=60, terminal_height=20)) as door:
            self.assertTrue(door.wait_for("needs a terminal of 80 x 24"))
            self.assertIn("Yours is 60 x 20.", door.screen(60, 20).text())
            self.assertEqual(door.screen(60, 20).out_of_bounds, [])
            door.write_info(door_info(terminal_width=132, terminal_height=40))
            if hasattr(signal, "SIGUSR1"):
                door.proc.send_signal(signal.SIGUSR1)
            self.assertTrue(door.wait_for("132 x 40", timeout=5), "the launch file is read again")
            self.assertIn("medium", door.screen(132, 40).text())
            door.send("q")
            self.assert_clean_exit(door)

    def test_quit_while_undersize(self):
        with Door(door_info(terminal_width=40, terminal_height=12)) as door:
            self.assertTrue(door.wait_for("Enlarge the window"))
            door.send("q")
            self.assert_clean_exit(door)

    def test_never_out_of_bounds_and_never_the_last_cell(self):
        for width, height in ((80, 24), (81, 25), (132, 40), (132, 50), (168, 46), (240, 70), (30, 8), (5, 2), (1, 1)):
            with self.subTest(size=(width, height)):
                info = door_info(terminal_width=width, terminal_height=height, session_limit_seconds=61.5,
                                 handle="M" * 200, node_name="N" * 200)
                with Door(info) as door:
                    self.assertTrue(door.wait_for("\x1b[0m", timeout=6))
                    if width >= 80 and height >= 24:
                        door.wait_for("of session time left", timeout=6)
                    door.send("?")
                    time.sleep(0.4)
                    door.send("bq")
                    self.assertEqual(door.wait_exit(), 0)
                    screen = door.screen(width, height)
                    # Replay everything, checking each frame as it was drawn.
                    self.assertEqual(screen.out_of_bounds, [])
                    for frame in door.output.split("\x1b[2J"):
                        term = type(screen)(width, height)
                        term.feed(frame)
                        self.assertEqual(term.out_of_bounds, [])
                        self.assertNotIn((height - 1, width - 1), term.written)

    def test_sixteen_colours_use_only_plain_ansi(self):
        with Door(door_info(color_depth="16", charset="ascii")) as door:
            self.assertTrue(door.wait_for("Return to"))
            door.send("q")
            self.assert_clean_exit(door)
            self.assertNotIn("38;2;", door.output)
            self.assertNotIn("38;5;", door.output)
            self.assertTrue(door.output.isascii())

    def test_standalone_over_pipes(self):
        with Door(hosted=False) as door:
            self.assertTrue(door.wait_for("standalone"))
            door.send("q")
            self.assert_clean_exit(door)


class LaunchFailures(unittest.TestCase):
    def check(self, door, expect):
        self.assertEqual(door.wait_exit(), 2)
        self.assertIn("could not start", door.output)
        self.assertNotIn("\x1b", door.output, "no screen was entered")
        self.assertIn(expect, door.errors)
        self.assertNotIn("Traceback", door.errors)
        self.assertNotIn("door_info.json", door.errors, "the diagnostic never names the file")
        self.assertLess(len(door.errors), 400)

    def test_malformed_metadata(self):
        with Door(raw="{not json") as door:
            self.check(door, "not valid JSON")

    def test_unsupported_api(self):
        for api in (4, 6):
            with self.subTest(api=api), Door(door_info(door_api=api)) as door:
                self.check(door, f"door_api {api} is not supported")

    def test_wrong_field_type(self):
        with Door(door_info(terminal_width="wide")) as door:
            self.check(door, "'terminal_width' has the wrong type")

    def test_missing_file(self):
        with Door(environ={"NETBBS_DOOR_INFO": os.path.join(str(ROOT), "no-such-file.json")}) as door:
            self.check(door, "cannot be read")


class CommandLine(unittest.TestCase):
    def test_version(self):
        done = subprocess.run([sys.executable, str(DOOR), "--version"], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0)
        self.assertRegex(done.stdout.strip(), r"^\d+\.\d+\.\d+")


class Profile(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / "profiles" / "netbbs.json").read_text(encoding="utf-8"))

    def test_shape(self):
        profile = self.value["profile"]
        self.assertEqual((profile["adapter"], profile["endpoint"], profile["encoding"]), ("native", "stdio", "utf-8"))
        self.assertEqual((profile["width"], profile["height"]), (0, 0), "follow the caller's geometry")
        self.assertTrue(profile["resize_signal"])
        self.assertTrue(all(name.startswith("DOOR_") for name in profile["environment"]))
        self.assertEqual(self.value["args"], ["{install_dir}/fair_winds.py"])

    def test_accepted_by_netbbs(self):
        try:
            from netbbs.doors.profiles import DoorProfile
        except ImportError:
            self.skipTest("NetBBS is not installed here; CI installs a pinned copy")
        # The shipped paths are a POSIX install; give the parser paths that
        # are absolute on whatever platform runs this test.
        value = dict(self.value["profile"], install_dir=str(ROOT),
                     environment={"DOOR_FW_DATA_DIR": str(ROOT / "data")})
        profile = DoorProfile.from_json(json.dumps(value))
        self.assertTrue(profile.resize_signal)


if __name__ == "__main__":
    unittest.main()
