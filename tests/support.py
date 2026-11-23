"""Shared test helpers: a small terminal, and a real door process over pipes."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOOR = ROOT / "fair_winds.py"
sys.path.insert(0, str(ROOT))

_SEQUENCE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class Terminal:
    """Just enough of an ANSI terminal to check where a door writes.

    Tracks the cursor, the text on screen, every cell written since the last
    clear, and any attempt to move or write outside the screen.
    """

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[" "] * width for _ in range(height)]
        self.written = set()
        self.row = self.col = 0
        self.out_of_bounds = []
        self.cursor_hidden = False

    def feed(self, text):
        i = 0
        while i < len(text):
            match = _SEQUENCE.match(text, i)
            if match:
                self._sequence(match.group(1), match.group(2))
                i = match.end()
                continue
            ch = text[i]
            i += 1
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.row += 1
            elif ch == "\x1b":
                self.out_of_bounds.append("unparsed escape")
            else:
                if 0 <= self.row < self.height and 0 <= self.col < self.width:
                    self.cells[self.row][self.col] = ch
                    self.written.add((self.row, self.col))
                else:
                    self.out_of_bounds.append(("write", self.row, self.col))
                self.col += 1

    def _sequence(self, params, final):
        if final == "H":
            parts = [int(p) if p else 1 for p in params.split(";")] if params else [1, 1]
            row, col = (parts + [1])[:2]
            if not (1 <= row <= self.height and 1 <= col <= self.width):
                self.out_of_bounds.append(("move", row, col))
            self.row, self.col = row - 1, col - 1
        elif final == "J" and params == "2":
            self.cells = [[" "] * self.width for _ in range(self.height)]
            self.written = set()
        elif final in ("h", "l") and params == "?25":
            self.cursor_hidden = final == "l"

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.cells)


def door_info(**changes):
    info = {"door_api": 5, "handle": "Mara", "user_id": 12, "node_id": "node-abc", "node_name": "Test Node",
            "terminal_width": 80, "terminal_height": 24, "color_depth": "truecolor", "charset": "utf-8",
            "quadrants": True, "unicode_style": True, "transport": "ssh", "timezone": "Europe/Berlin",
            "datetime_format": "%d.%m.%Y %H:%M"}
    info.update(changes)
    return {k: v for k, v in info.items() if v is not ...}


class Door:
    """The real door as a child process, talking over real pipes."""

    def __init__(self, info=None, raw=None, hosted=True, environ=None):
        self._dir = tempfile.TemporaryDirectory(prefix="fair-winds-test-")
        self.info_path = Path(self._dir.name) / "door_info.json"
        self.size = (80, 24)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DOOR_", "NETBBS_"))}
        env["PYTHONIOENCODING"] = "utf-8"
        env["DOOR_FW_DATA_DIR"] = str(Path(self._dir.name) / "data")
        env["DOOR_SPLASH"] = "0"            # straight to the game, unless a test asks for the splash
        env["DOOR_SCENES"] = "0"            # and no scenes to wait out
        env["DOOR_FW_MOTION"] = "off"       # and no moving wind to race against
        if hosted:
            self.write_info(info if info is not None else door_info(), raw)
            env["NETBBS_DOOR_INFO"] = str(self.info_path)
        env.update(environ or {})
        self.proc = subprocess.Popen([sys.executable, str(DOOR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=self._dir.name)
        self._out, self._err = bytearray(), bytearray()
        self._lock = threading.Lock()
        for stream, sink in ((self.proc.stdout, self._out), (self.proc.stderr, self._err)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()

    def write_info(self, info, raw=None):
        """Replace the launch file whole, as NetBBS does."""
        if isinstance(info, dict):
            self.size = (info.get("terminal_width", 80), info.get("terminal_height", 24))
        temp = self.info_path.with_suffix(".tmp")
        temp.write_text(raw if raw is not None else json.dumps(info), encoding="utf-8")
        temp.replace(self.info_path)

    def _pump(self, stream, sink):
        while True:
            data = stream.read1(4096)
            if not data:
                return
            with self._lock:
                sink += data

    @property
    def output(self):
        with self._lock:
            return self._out.decode("utf-8", "replace")

    @property
    def errors(self):
        with self._lock:
            return self._err.decode("utf-8", "replace")

    def send(self, data):
        self.proc.stdin.write(data if isinstance(data, bytes) else data.encode())
        self.proc.stdin.flush()

    def _seen(self, text, size):
        """Is the text on the caller's screen now? Only changed cells are sent,
        so the byte stream alone no longer spells it. Escape sequences, and
        text from before a screen was entered, are looked for in the bytes."""
        output = self.output
        if "\x1b" in text or "\x1b" not in output:
            return text in output
        if text in output:
            return True
        term = Terminal(*size)
        term.feed(output)
        return text in term.text()

    def wait_for(self, text, timeout=10, size=None):
        size = size or self.size
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._seen(text, size):
                return True
            if self.proc.poll() is not None:
                time.sleep(0.1)
                return self._seen(text, size)
            time.sleep(0.02)
        return False

    def wait_exit(self, timeout=10):
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        time.sleep(0.1)     # let the pumps drain
        return code

    def screen(self, width=None, height=None):
        width, height = width or self.size[0], height or self.size[1]
        term = Terminal(width, height)
        term.feed(self.output)
        return term

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self._dir.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_games = []


def new_game(*args, **kwargs):
    """A game as a new session would open it: whichever session came before has ended."""
    import fair_winds as fw
    for game in _games:
        game.close()
    del _games[:]
    game = fw.PlainFlight(*args, **kwargs)
    _games.append(game)
    return game


def stored(folder, user_id=12):
    """What the database holds for a pilot: the career, with the flight under "state"."""
    import json
    import sqlite3
    found = []
    for path in sorted(Path(folder).glob("fair_winds-*.db")):
        db = sqlite3.connect(str(path))
        try:
            row = db.execute("SELECT p.career, f.state, f.seq FROM pilots p JOIN flights f USING (user_id)"
                             " WHERE p.user_id=?", (user_id,)).fetchone()
        finally:
            db.close()
        if row:
            found.append(dict(json.loads(row[0]), state=json.loads(row[1]), seq=row[2]))
    assert len(found) == 1, found
    return found[0]
