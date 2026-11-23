#!/usr/bin/env python3
"""Fair Winds: a cooperative, turn-based balloon navigation door game.

One file, standard library only, Python 3.11 or newer. Runs as a native stdio
door under NetBBS (launch details in the JSON named by NETBBS_DOOR_INFO), or
standalone in a terminal for development.

This build is slice S01 of docs/design.md: the host adapter and the terminal
lifecycle. It opens, shows what the host told it, and closes cleanly. The game
arrives in the slices that follow.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0.dev1"
DOOR_API = 5
MIN_WIDTH, MIN_HEIGHT = 80, 24
MAX_DIMENSION = 1000
# NetBBS gives a bundled door's guest call an identity no account can have.
GUEST_ID_FLOOR = 2 ** 62
# Leave before the host's wall-clock limit, so the host never has to stop us.
DEADLINE_GRACE = 2.0
DEADLINE_WARNINGS = (300.0, 60.0)
GEOMETRY_POLL = 1.0
ESCAPE_TIMEOUT = 0.05
IDLE_TIMEOUT = 0.25
DIAGNOSTIC_LIMIT = 300

COLOR_DEPTHS = ("truecolor", "256", "16")
CHARSETS = ("utf-8", "cp437", "ascii")


# --------------------------------------------------------------------------
# Launch: one host-neutral record. Nothing after start-up knows which host
# it is on (docs/systems.md, section 13).
# --------------------------------------------------------------------------

class LaunchError(Exception):
    """The host supplied launch details the door cannot use."""


@dataclass
class Launch:
    host: str                     # "netbbs" or "standalone"
    handle: str
    user_id: int | None
    node_id: str | None
    node_name: str
    width: int
    height: int
    color_depth: str
    charset: str
    quadrants: bool
    unicode_style: bool
    transport: str
    timezone: str
    datetime_format: str
    session_limit: float | None   # seconds of wall clock for this launch
    outbound: dict | None
    guest: bool
    info_path: Path | None = None


def clean_text(value: str, limit: int) -> str:
    """Printable text only: no control characters, so no escape sequences."""
    kept = "".join(ch for ch in value if ch.isprintable())
    return " ".join(kept.split())[:limit]


def _field(info: dict, name: str, kind, default):
    """A documented optional field: absent means unknown, a wrong type is an error."""
    if name not in info or info[name] is None:
        return default
    value = info[name]
    if isinstance(value, bool) and kind is not bool:
        raise LaunchError(f"'{name}' has the wrong type")
    if not isinstance(value, kind):
        raise LaunchError(f"'{name}' has the wrong type")
    return value


def _dimension(info: dict, name: str, default: int) -> int:
    value = _field(info, name, int, default)
    if not 1 <= value <= MAX_DIMENSION:
        raise LaunchError(f"'{name}' is out of range")
    return value


def _guest_ids(environ) -> set[int]:
    ids = set()
    for part in environ.get("DOOR_FW_GUEST_IDS", "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def parse_door_info(text: str, environ, info_path: Path | None = None) -> Launch:
    """NetBBS's door_info.json into a launch record."""
    try:
        info = json.loads(text)
    except ValueError:
        raise LaunchError("the launch file is not valid JSON") from None
    if not isinstance(info, dict):
        raise LaunchError("the launch file is not a JSON object")
    api = info.get("door_api")
    if isinstance(api, bool) or not isinstance(api, int):
        raise LaunchError("the launch file has no usable 'door_api'")
    if api != DOOR_API:
        raise LaunchError(f"door_api {api} is not supported; this door needs door_api {DOOR_API}")

    user_id = _field(info, "user_id", int, None)
    if user_id is not None and user_id < 0:
        raise LaunchError("'user_id' is out of range")
    node_id = _field(info, "node_id", str, None)
    if node_id is not None:
        node_id = clean_text(node_id, 128) or None
    color_depth = _field(info, "color_depth", str, "16")
    if color_depth not in COLOR_DEPTHS:
        raise LaunchError("'color_depth' is not one of truecolor, 256, 16")
    charset = _field(info, "charset", str, "utf-8")
    if charset not in CHARSETS:
        raise LaunchError("'charset' is not one of utf-8, cp437, ascii")
    limit = _field(info, "session_limit_seconds", (int, float), None)
    if limit is not None and limit <= 0:
        raise LaunchError("'session_limit_seconds' is out of range")
    guest = (user_id is None or node_id is None or user_id >= GUEST_ID_FLOOR
             or user_id in _guest_ids(environ))
    return Launch(
        host="netbbs",
        handle=clean_text(_field(info, "handle", str, ""), 32) or "Pilot",
        user_id=user_id,
        node_id=node_id,
        node_name=clean_text(_field(info, "node_name", str, ""), 48) or "the BBS",
        width=_dimension(info, "terminal_width", MIN_WIDTH),
        height=_dimension(info, "terminal_height", MIN_HEIGHT),
        color_depth=color_depth,
        charset=charset,
        quadrants=_field(info, "quadrants", bool, False),
        unicode_style=_field(info, "unicode_style", bool, True),
        transport=clean_text(_field(info, "transport", str, "unknown"), 16) or "unknown",
        timezone=clean_text(_field(info, "timezone", str, "UTC"), 64) or "UTC",
        datetime_format=_field(info, "datetime_format", str, "%d.%m.%Y %H:%M"),
        session_limit=float(limit) if limit is not None else None,
        outbound=_field(info, "outbound", dict, None),
        guest=guest,
        info_path=info_path,
    )


def standalone_launch(environ) -> Launch:
    """No host: a development launch in whatever terminal this is."""
    size = shutil.get_terminal_size((MIN_WIDTH, MIN_HEIGHT))
    truecolor = environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    return Launch(
        host="standalone",
        handle=clean_text(environ.get("USER") or environ.get("USERNAME") or "", 32) or "Pilot",
        user_id=1, node_id="standalone", node_name="the shell",
        width=max(1, min(MAX_DIMENSION, size.columns)), height=max(1, min(MAX_DIMENSION, size.lines)),
        color_depth="truecolor" if truecolor else "256", charset="utf-8",
        quadrants=False, unicode_style=True, transport="local", timezone="UTC",
        datetime_format="%d.%m.%Y %H:%M", session_limit=None, outbound=None, guest=False)


def load_launch(environ) -> Launch:
    """The launch record for this run.

    A launch file that was supplied but cannot be used is an error, never a
    quiet fall back to standalone play.
    """
    if "NETBBS_DOOR_INFO" not in environ:
        return standalone_launch(environ)
    name = environ["NETBBS_DOOR_INFO"]
    if not name:
        raise LaunchError("NETBBS_DOOR_INFO is empty")
    path = Path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise LaunchError("the launch file cannot be read") from None
    return parse_door_info(text, environ, path)


def current_geometry(launch: Launch) -> tuple[int, int]:
    """The caller's size now. A file caught mid-change keeps the last size."""
    if launch.host == "standalone":
        size = shutil.get_terminal_size((launch.width, launch.height))
        return max(1, min(MAX_DIMENSION, size.columns)), max(1, min(MAX_DIMENSION, size.lines))
    try:
        info = json.loads(launch.info_path.read_text(encoding="utf-8"))
        width, height = info.get("terminal_width"), info.get("terminal_height")
        if all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_DIMENSION
               for v in (width, height)):
            return width, height
    except (OSError, ValueError, AttributeError):
        pass
    return launch.width, launch.height


def layout_tier(width: int, height: int) -> str:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return "undersize"
    if width >= 168 and height >= 46:
        return "wide"
    if width >= 132 and height >= 40:
        return "medium"
    return "small"


# --------------------------------------------------------------------------
# Keys: an incremental decoder. Escape sequences may arrive split across
# reads; nothing depends on key-up, chords or timing beyond a lone Escape.
# --------------------------------------------------------------------------

_CSI_FINAL = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
_CSI_TILDE = {"1": "HOME", "7": "HOME", "4": "END", "8": "END", "5": "PGUP", "6": "PGDN", "11": "F1"}
_SS3 = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END", "P": "F1"}


class KeyDecoder:
    """Bytes in, key names out: a character, or UP, ENTER, ESC and so on."""

    MAX_SEQUENCE = 16

    def __init__(self):
        self._buf = bytearray()
        self._after_cr = False

    @property
    def pending(self) -> bool:
        """True while bytes are held back waiting for the rest of a key."""
        return bool(self._buf)

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        keys: list[str] = []
        while self._buf:
            used = self._one(keys)
            if used == 0:
                break
            del self._buf[:used]
        return keys

    def flush(self) -> list[str]:
        """No more bytes came in time: a held Escape was a key of its own."""
        keys: list[str] = []
        if self._buf and self._buf[0] == 0x1B:
            keys.append("ESC")
            del self._buf[:1]
            keys += self.feed(b"")
        self._buf.clear()
        return keys

    def _one(self, keys: list[str]) -> int:
        """Decode one key from the front of the buffer; 0 means wait for more."""
        buf = self._buf
        b = buf[0]
        if self._after_cr:
            self._after_cr = False
            if b in (0x0A, 0x00):       # CR LF and CR NUL are one Enter
                return 1
        if b == 0x1B:
            return self._escape(keys)
        if b == 0x0D:
            keys.append("ENTER")
            self._after_cr = True
            return 1
        if b == 0x0A:
            keys.append("ENTER")
            return 1
        if b in (0x7F, 0x08):
            keys.append("BACKSPACE")
            return 1
        if b == 0x09:
            keys.append("TAB")
            return 1
        if b == 0x00:
            return 1
        if b < 0x20:
            keys.append("CTRL-" + chr(b + 0x40))
            return 1
        if b < 0x7F:
            keys.append(chr(b))
            return 1
        return self._utf8(keys)

    def _escape(self, keys: list[str]) -> int:
        buf = self._buf
        if len(buf) == 1:
            return 0
        second = buf[1]
        if second == 0x5B:              # CSI: parameters, then one final byte
            for i in range(2, len(buf)):
                if 0x40 <= buf[i] <= 0x7E:
                    params, final = buf[2:i].decode("ascii", "replace"), chr(buf[i])
                    key = _CSI_TILDE.get(params.split(";")[0]) if final == "~" else (
                        _CSI_FINAL.get(final) if not params or params.startswith("1;") else None)
                    if key:
                        keys.append(key)
                    return i + 1
                if i >= self.MAX_SEQUENCE:
                    return i + 1        # runaway sequence: drop it, stay bounded
            return 0 if len(buf) <= self.MAX_SEQUENCE else len(buf)
        if second == 0x4F:              # SS3: application-mode arrows and F1
            if len(buf) < 3:
                return 0
            key = _SS3.get(chr(buf[2]))
            if key:
                keys.append(key)
            return 3
        keys.append("ESC")              # Escape followed by an ordinary key
        return 1

    def _utf8(self, keys: list[str]) -> int:
        buf = self._buf
        b = buf[0]
        need = 2 if 0xC2 <= b <= 0xDF else 3 if 0xE0 <= b <= 0xEF else 4 if 0xF0 <= b <= 0xF4 else 0
        if need == 0:
            return 1                    # not the start of a character: drop it
        if len(buf) < need:
            if all(0x80 <= c <= 0xBF for c in buf[1:]):
                return 0
            return 1
        try:
            ch = bytes(buf[:need]).decode("utf-8")
        except UnicodeDecodeError:
            return 1
        if ch.isprintable():
            keys.append(ch)
        return need


# --------------------------------------------------------------------------
# Terminal streams.
# --------------------------------------------------------------------------

class SessionClosed(Exception):
    """The caller is gone: input ended or output broke."""


class Output:
    def __init__(self, fd: int):
        self._fd = fd

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        try:
            while data:
                data = data[os.write(self._fd, data):]
        except OSError:
            raise SessionClosed() from None


class Input:
    """Timed reads from the caller.

    A thread does the blocking read, so the same code serves pipes, sockets
    and consoles on POSIX and Windows, where select() does not cover them all.
    """

    def __init__(self, fd: int, console: bool = False):
        self._queue: queue.Queue[bytes] = queue.Queue()
        target = self._read_console if console else self._read_fd
        threading.Thread(target=target, args=(fd,), name="fw-input", daemon=True).start()

    def _read_fd(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 256)
            except OSError:
                data = b""
            self._queue.put(data)
            if not data:
                return

    def _read_console(self, fd: int) -> None:      # Windows console, standalone only
        import msvcrt
        special = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",
                   "G": b"\x1b[H", "O": b"\x1b[F", "I": b"\x1b[5~", "Q": b"\x1b[6~", ";": b"\x1b[11~"}
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                self._queue.put(special.get(msvcrt.getwch(), b""))
            else:
                self._queue.put(ch.encode("utf-8", "replace"))

    def read(self, timeout: float) -> bytes | None:
        """Bytes, b"" at end of input, or None when nothing came in time."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class LocalTerminal:
    """Standalone play in a real terminal: raw keys in, restored on the way out."""

    def __init__(self):
        self._restore = None
        self.console = False

    def enter(self) -> None:
        if not sys.stdin.isatty():
            return
        if os.name == "nt":
            self.console = True
            try:
                import ctypes
                kernel = ctypes.windll.kernel32
                handle = kernel.GetStdHandle(-11)
                mode = ctypes.c_uint()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel.SetConsoleMode(handle, mode.value | 0x0004)   # ANSI sequences
                    self._restore = lambda: kernel.SetConsoleMode(handle, mode.value)
            except (OSError, AttributeError):
                pass
            return
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def leave(self) -> None:
        if self._restore:
            try:
                self._restore()
            except Exception:
                pass
            self._restore = None


# --------------------------------------------------------------------------
# The session.
# --------------------------------------------------------------------------

_ENTER = "\x1b[0m\x1b[?25l\x1b[2J\x1b[H"
_LEAVE = "\x1b[0m\x1b[?25h\x1b[2J\x1b[H"

_INK = {
    "accent": {"truecolor": "38;2;255;196;110", "256": "38;5;215", "16": "1;33"},
    "dim": {"truecolor": "38;2;112;126;148", "256": "38;5;103", "16": "36"},
    "text": {"truecolor": "38;2;206;214;226", "256": "38;5;252", "16": "37"},
    "warn": {"truecolor": "38;2;255;150;130", "256": "38;5;210", "16": "1;31"},
}


class Session:
    """One caller, from the first byte to the last.

    Every way out (quit, end of input, broken output, SIGTERM, the session
    limit) goes through `run`, which always restores the terminal.
    """

    def __init__(self, launch: Launch, inp: Input, out: Output, clock=time.monotonic):
        self.launch = launch
        self.inp, self.out, self.clock = inp, out, clock
        self.keys = KeyDecoder()
        self.width, self.height = launch.width, launch.height
        self.started = clock()
        self.stop: str | None = None          # why the session is ending
        self.resized = False                  # set from a signal handler
        self.terminated = False               # set from a signal handler
        self.view = "front"
        self.notice = ""
        self._warned: set[float] = set()
        self._dirty = True
        self._next_poll = self.started + GEOMETRY_POLL

    # -- time ---------------------------------------------------------------

    def remaining(self) -> float | None:
        if self.launch.session_limit is None:
            return None
        return self.launch.session_limit - (self.clock() - self.started)

    def _check_deadline(self) -> None:
        left = self.remaining()
        if left is None:
            return
        if left <= DEADLINE_GRACE:
            self.stop = "deadline"
            return
        for mark in DEADLINE_WARNINGS:
            if left <= mark and mark not in self._warned and self.launch.session_limit > mark:
                self._warned.add(mark)
                minutes = max(1, round(left / 60))
                self.notice = (f"About {minutes} minute{'s' if minutes != 1 else ''} of session time left. "
                               "Nothing is lost when it ends.")
                self._dirty = True

    # -- geometry -----------------------------------------------------------

    def _check_geometry(self) -> None:
        now = self.clock()
        if not self.resized and now < self._next_poll:
            return
        self.resized = False
        self._next_poll = now + GEOMETRY_POLL
        size = current_geometry(self.launch)
        if size != (self.width, self.height):
            self.width, self.height = size
            self._dirty = True              # a resize repaints everything

    # -- drawing ------------------------------------------------------------

    def ink(self, role: str) -> str:
        return f"\x1b[0;{_INK[role][self.launch.color_depth]}m"

    def _line(self, row: int, col: int, text: str, role: str = "text") -> str:
        """One run of text, clipped so the bottom-right cell is never written."""
        if not 0 <= row < self.height or col >= self.width:
            return ""
        room = self.width - col - (1 if row == self.height - 1 else 0)
        text = text[:max(0, room)]
        if not text:
            return ""
        return f"\x1b[{row + 1};{col + 1}H{self.ink(role)}{text}"

    def paint(self) -> None:
        parts = ["\x1b[0m\x1b[2J"]
        if layout_tier(self.width, self.height) == "undersize":
            parts += self._undersize()
        elif self.view == "help":
            parts += self._help()
        else:
            parts += self._front()
        parts.append("\x1b[0m")
        self.out.write("".join(parts))
        self._dirty = False

    def _dot(self) -> str:
        return " · " if self.launch.charset == "utf-8" and self.launch.unicode_style else " - "

    def _undersize(self) -> list[str]:
        lines = [f"Fair Winds needs a terminal of {MIN_WIDTH} x {MIN_HEIGHT} or larger.",
                 f"Yours is {self.width} x {self.height}.",
                 "Enlarge the window, or press Q to leave."]
        top = max(0, (self.height - len(lines)) // 2)
        return [self._line(top + i, max(0, (self.width - len(t)) // 2), t, "accent" if i == 0 else "text")
                for i, t in enumerate(lines)]

    def _front(self) -> list[str]:
        launch, dot = self.launch, self._dot()
        left = self.remaining()
        host = "standalone" if launch.host == "standalone" else f"NetBBS{dot}{launch.node_name}"
        rows = [
            ("Host", host),
            ("Terminal", f"{self.width} x {self.height}{dot}{launch.color_depth}{dot}{launch.charset}{dot}{launch.transport}"),
            ("Layout", layout_tier(self.width, self.height)),
            ("Pilot", "visiting: nothing is kept" if launch.guest else "registered"),
            ("Session", "no limit" if left is None else f"about {max(1, round(left / 60))} min left"),
        ]
        out = [self._line(0, 1, "FAIR WINDS", "accent"),
               self._line(0, self.width - len(VERSION) - 3, "v" + VERSION, "dim"),
               self._line(2, 1, f"Welcome aboard, {launch.handle}."),
               self._line(3, 1, "The balloon is still in the shed. This build only proves that the door"),
               self._line(4, 1, "opens and closes cleanly.")]
        for i, (label, value) in enumerate(rows):
            out.append(self._line(6 + i, 1, label, "dim"))
            out.append(self._line(6 + i, 13, value))
        if self.notice:
            out.append(self._line(self.height - 3, 1, self.notice, "warn"))
        out += self._keys(self.height - 1, [("?", "Help"), ("Q", f"Return to {launch.node_name}")])
        return out

    def _help(self) -> list[str]:
        text = ["Fair Winds is a balloon navigation game. A balloon cannot steer: it can only",
                "climb or sink, and every altitude blows a different way.",
                "",
                "Keys that work everywhere:",
                "  ?  this help            Q  leave, or go back",
                "  B or Esc  go back       < and >  turn a page",
                "",
                "Leaving never loses anything. The game is saved as you play."]
        out = [self._line(0, 1, "FAIR WINDS", "accent"), self._line(0, 13, "Help", "dim")]
        out += [self._line(2 + i, 1, t) for i, t in enumerate(text)]
        out += self._keys(self.height - 1, [("B", "Back"), ("Q", "Back")])
        return out

    def _keys(self, row: int, keys) -> list[str]:
        out, col = [], 1
        for key, label in keys:
            out.append(self._line(row, col, "[", "dim"))
            out.append(self._line(row, col + 1, key, "accent"))
            out.append(self._line(row, col + 1 + len(key), "]", "dim"))
            out.append(self._line(row, col + 3 + len(key), label))
            col += len(key) + len(label) + 6
        return out

    # -- input --------------------------------------------------------------

    def _handle(self, key: str) -> None:
        self.notice = ""
        self._dirty = True
        lower = key.lower() if len(key) == 1 else key
        if self.view == "help":
            if lower in ("q", "b", "ESC", "?", "ENTER", "CTRL-C"):
                self.view = "front"
            return
        if lower in ("q", "CTRL-C"):
            self.stop = "quit"
        elif lower in ("?", "F1"):
            self.view = "help"

    def step(self) -> None:
        """One turn of the loop: time, size, paint, then wait briefly for keys."""
        if self.terminated:
            self.stop = "terminated"
            return
        self._check_deadline()
        if self.stop:
            return
        self._check_geometry()
        if self._dirty:
            self.paint()
        data = self.inp.read(ESCAPE_TIMEOUT if self.keys.pending else IDLE_TIMEOUT)
        if data is None:
            keys = self.keys.flush() if self.keys.pending else []
        elif data == b"":
            self.stop = "eof"
            return
        else:
            keys = self.keys.feed(data)
        for key in keys:
            if self.stop:
                break
            self._handle(key)

    def run(self) -> str:
        try:
            self.out.write(_ENTER)
            while not self.stop:
                self.step()
            if self.stop == "deadline":
                self.out.write("\x1b[0m\x1b[2J" + self._line(0, 1, "Session time is up. Nothing is lost.", "accent"))
        except SessionClosed:
            self.stop = "closed"
        finally:
            try:
                self.out.write(_LEAVE)
            except SessionClosed:
                pass
        return self.stop or "quit"


def diagnose(message: str) -> None:
    """A bounded line for the SysOp. Callers never see standard error."""
    sys.stderr.write(("fair_winds: " + clean_text(message, DIAGNOSTIC_LIMIT)) + "\n")
    sys.stderr.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fair_winds.py", description="Fair Winds, a door game for NetBBS.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.parse_args(argv)

    out = Output(sys.stdout.fileno())
    try:
        launch = load_launch(os.environ)
    except LaunchError as error:
        diagnose(f"cannot start: {error}")
        try:
            out.write("Fair Winds could not start: the BBS sent launch details it cannot use.\r\n"
                      "Please tell the SysOp.\r\n")
        except SessionClosed:
            pass
        return 2

    local = LocalTerminal()
    if launch.host == "standalone":
        local.enter()
    session = Session(launch, Input(sys.stdin.fileno(), console=local.console), out)

    def on_terminate(signum, frame):
        session.terminated = True

    def on_resize(signum, frame):
        session.resized = True

    signal.signal(signal.SIGTERM, on_terminate)
    signal.signal(signal.SIGINT, on_terminate)
    for name in ("SIGUSR1", "SIGWINCH"):     # NetBBS signals a resize; so does a local terminal
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_resize)
    try:
        session.run()
    finally:
        local.leave()
    return 0


if __name__ == "__main__":
    sys.exit(main())
