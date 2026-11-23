"""S01: launch metadata into the host-neutral launch record."""
import json
import tempfile
import unittest
from pathlib import Path

from support import door_info

import fair_winds as fw


def parse(environ=None, **changes):
    return fw.parse_door_info(json.dumps(door_info(**changes)), environ or {})


class LaunchRecord(unittest.TestCase):
    def test_full_file(self):
        launch = parse(session_limit_seconds=1800, outbound={"label": "Fair Winds.door"})
        self.assertEqual((launch.host, launch.handle, launch.user_id, launch.node_id), ("netbbs", "Mara", 12, "node-abc"))
        self.assertEqual((launch.width, launch.height), (80, 24))
        self.assertEqual((launch.color_depth, launch.charset, launch.quadrants), ("truecolor", "utf-8", True))
        self.assertEqual(launch.session_limit, 1800.0)
        self.assertEqual(launch.outbound, {"label": "Fair Winds.door"})
        self.assertFalse(launch.guest)

    def test_only_the_version_is_required(self):
        launch = fw.parse_door_info('{"door_api": 5}', {})
        self.assertEqual((launch.width, launch.height), (80, 24))
        self.assertEqual((launch.color_depth, launch.charset), ("16", "utf-8"))
        self.assertFalse(launch.quadrants)
        self.assertIsNone(launch.session_limit)
        self.assertIsNone(launch.outbound)
        self.assertTrue(launch.guest, "no identity is a visit")

    def test_unknown_fields_are_ignored(self):
        self.assertEqual(parse(something_new={"a": 1}, war_dialer_owner=True).handle, "Mara")

    def test_null_counts_as_absent(self):
        self.assertEqual(parse(color_depth=None).color_depth, "16")

    def test_other_api_versions_are_refused(self):
        for api in (4, 6, 0, "5", 5.0, True, None, ...):
            with self.subTest(api=api), self.assertRaises(fw.LaunchError):
                parse(door_api=api)

    def test_malformed_files_are_errors(self):
        for text in ("", "{", "[]", '"door"', "5", "null"):
            with self.subTest(text=text), self.assertRaises(fw.LaunchError):
                fw.parse_door_info(text, {})

    def test_wrong_types_are_errors(self):
        bad = [("terminal_width", "80"), ("terminal_width", 0), ("terminal_height", 5000), ("terminal_width", True),
               ("color_depth", "blue"), ("color_depth", 256), ("charset", "latin-1"), ("user_id", -1),
               ("user_id", "12"), ("user_id", True), ("quadrants", "yes"), ("handle", 7),
               ("session_limit_seconds", 0), ("session_limit_seconds", "60"), ("outbound", []), ("node_id", 4)]
        for name, value in bad:
            with self.subTest(field=name, value=value), self.assertRaises(fw.LaunchError):
                parse(**{name: value})

    def test_names_cannot_carry_escape_sequences(self):
        launch = parse(handle="Ma\x1b[2Jra\r\n\x07", node_name="No\x1bde")
        self.assertEqual(launch.handle, "Ma[2Jra")
        self.assertNotIn("\x1b", launch.node_name)
        self.assertEqual(parse(handle="\x1b\x07").handle, "Pilot")

    def test_guest_calls(self):
        self.assertTrue(parse(user_id=2 ** 62).guest, "a bundled door's guest call")
        self.assertTrue(parse(user_id=2 ** 62 + 99).guest)
        self.assertFalse(parse(user_id=2 ** 62 - 1).guest)
        self.assertTrue(parse({"DOOR_FW_GUEST_IDS": "3, 12 ,x"}).guest, "listed by the SysOp")
        self.assertFalse(parse({"DOOR_FW_GUEST_IDS": "3,4"}).guest)
        self.assertTrue(parse(user_id=...).guest)
        self.assertTrue(parse(node_id=...).guest)


class LoadLaunch(unittest.TestCase):
    def test_no_host_is_standalone(self):
        launch = fw.load_launch({"USER": "dev"})
        self.assertEqual((launch.host, launch.handle), ("standalone", "dev"))
        self.assertFalse(launch.guest)

    def test_a_supplied_file_never_falls_back_to_standalone(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            missing = Path(folder) / "nope.json"
            for value in ("", str(missing), folder):
                with self.subTest(value=value), self.assertRaises(fw.LaunchError):
                    fw.load_launch({"NETBBS_DOOR_INFO": value})
            broken = Path(folder) / "broken.json"
            broken.write_bytes(b"\xff\xfe{")
            with self.assertRaises(fw.LaunchError):
                fw.load_launch({"NETBBS_DOOR_INFO": str(broken)})

    def test_errors_do_not_name_the_file(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            path = Path(folder) / "secret-name.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(fw.LaunchError) as caught:
                fw.load_launch({"NETBBS_DOOR_INFO": str(path)})
            self.assertNotIn("secret-name", str(caught.exception))

    def test_geometry_is_read_again_and_survives_a_bad_file(self):
        with tempfile.TemporaryDirectory(prefix="fair-winds-test-") as folder:
            path = Path(folder) / "door_info.json"
            path.write_text(json.dumps(door_info()), encoding="utf-8")
            launch = fw.load_launch({"NETBBS_DOOR_INFO": str(path)})
            path.write_text(json.dumps(door_info(terminal_width=132, terminal_height=40)), encoding="utf-8")
            self.assertEqual(fw.current_geometry(launch), (132, 40))
            for text in ("{", json.dumps(door_info(terminal_width="x")), json.dumps(door_info(terminal_height=0))):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(fw.current_geometry(launch), (80, 24))
            path.unlink()
            self.assertEqual(fw.current_geometry(launch), (80, 24))


class Layout(unittest.TestCase):
    def test_tiers(self):
        cases = {(79, 24): "undersize", (80, 23): "undersize", (80, 24): "small", (131, 50): "small",
                 (132, 39): "small", (132, 40): "medium", (167, 60): "medium", (168, 45): "medium",
                 (168, 46): "wide", (240, 70): "wide"}
        for size, tier in cases.items():
            self.assertEqual(fw.layout_tier(*size), tier, size)


if __name__ == "__main__":
    unittest.main()
