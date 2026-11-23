"""S01: the incremental key decoder."""
import unittest

import support  # noqa: F401  (puts the repository root on the path)

import fair_winds as fw


def decode(*chunks, flush=False):
    decoder = fw.KeyDecoder()
    keys = []
    for chunk in chunks:
        keys += decoder.feed(chunk)
    if flush:
        keys += decoder.flush()
    return keys, decoder


class Keys(unittest.TestCase):
    def test_plain_characters(self):
        self.assertEqual(decode(b"wS?q ")[0], ["w", "S", "?", "q", " "])

    def test_arrows_and_navigation(self):
        keys, _ = decode(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1b[5~\x1b[6~\x1b[11~\x1b[1~\x1b[4~")
        self.assertEqual(keys, ["UP", "DOWN", "RIGHT", "LEFT", "HOME", "END", "PGUP", "PGDN", "F1", "HOME", "END"])
        self.assertEqual(decode(b"\x1bOA\x1bOB\x1bOC\x1bOD\x1bOP")[0], ["UP", "DOWN", "RIGHT", "LEFT", "F1"])
        self.assertEqual(decode(b"\x1b[1;5A")[0], ["UP"], "a modified arrow is still an arrow")

    def test_a_sequence_split_anywhere_is_one_key(self):
        for sequence, key in ((b"\x1b[A", "UP"), (b"\x1bOB", "DOWN"), (b"\x1b[5~", "PGUP"), (b"\x1b[11~", "F1")):
            for cut in range(1, len(sequence)):
                keys, decoder = decode(sequence[:cut], sequence[cut:], b"q")
                self.assertEqual(keys, [key, "q"], (sequence, cut))
                self.assertFalse(decoder.pending)
            keys, _ = decode(*[bytes([b]) for b in sequence])
            self.assertEqual(keys, [key])

    def test_a_lone_escape_needs_the_timeout(self):
        keys, decoder = decode(b"\x1b")
        self.assertEqual(keys, [])
        self.assertTrue(decoder.pending)
        self.assertEqual(decoder.flush(), ["ESC"])
        self.assertFalse(decoder.pending)

    def test_escape_then_a_key(self):
        self.assertEqual(decode(b"\x1bq")[0], ["ESC", "q"])
        self.assertEqual(decode(b"\x1b\x1b", flush=True)[0], ["ESC", "ESC"])

    def test_enter_in_every_spelling(self):
        self.assertEqual(decode(b"\r")[0], ["ENTER"])
        self.assertEqual(decode(b"\n")[0], ["ENTER"])
        self.assertEqual(decode(b"\r\n")[0], ["ENTER"])
        self.assertEqual(decode(b"\r\x00")[0], ["ENTER"])
        self.assertEqual(decode(b"\r", b"\n")[0], ["ENTER"], "CR and LF in separate reads")
        self.assertEqual(decode(b"\r\r")[0], ["ENTER", "ENTER"])
        self.assertEqual(decode(b"\n\n")[0], ["ENTER", "ENTER"])
        self.assertEqual(decode(b"\rq\n")[0], ["ENTER", "q", "ENTER"])

    def test_control_keys(self):
        self.assertEqual(decode(b"\x7f\x08\t\x03\x00")[0], ["BACKSPACE", "BACKSPACE", "TAB", "CTRL-C"])

    def test_utf8(self):
        self.assertEqual(decode("é€😀".encode())[0], ["é", "€", "😀"])
        euro = "€".encode()
        self.assertEqual(decode(euro[:1], euro[1:2], euro[2:])[0], ["€"], "split across reads")

    def test_bad_bytes_are_dropped_not_fatal(self):
        self.assertEqual(decode(b"\xffq")[0], ["q"])
        self.assertEqual(decode(b"\x80\xbfq")[0], ["q"])
        self.assertEqual(decode(b"\xe2q")[0], ["q"], "a broken character does not eat the next key")
        self.assertEqual(decode(b"\xe2\x82", flush=True)[0], [], "an unfinished character is dropped at the timeout")

    def test_unknown_sequences_are_ignored(self):
        self.assertEqual(decode(b"\x1b[99~\x1b[Zq")[0], ["q"])
        self.assertEqual(decode(b"\x1bOZq")[0], ["q"])

    def test_a_runaway_sequence_stays_bounded(self):
        keys, decoder = decode(b"\x1b[" + b"1;" * 500)
        self.assertEqual(keys.count("UP"), 0)
        self.assertLessEqual(len(decoder._buf), fw.KeyDecoder.MAX_SEQUENCE + 1)
        decoder.flush()
        self.assertEqual(decoder.feed(b"q"), ["q"])

    def test_flush_keeps_what_followed_a_held_escape(self):
        decoder = fw.KeyDecoder()
        decoder.feed(b"\x1b")
        self.assertEqual(decoder.flush(), ["ESC"])
        self.assertEqual(decoder.feed(b"[A"), ["[", "A"], "after the timeout these are ordinary keys")


if __name__ == "__main__":
    unittest.main()
