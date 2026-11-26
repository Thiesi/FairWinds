"""Capture game screens as HTML, to look at them.

Drives the game in-process, takes the frame it would send, and writes one
HTML page per capture into a fixed folder, `.test-tmp/captures`, replaced on
every run. Each page draws the cells exactly: one glyph, one foreground, one
background, with a split cell shown as its two pixels.

    python scripts/capture.py            # every size and colour depth
    python scripts/capture.py 168x46 truecolor

A browser can turn a page into a picture:
    msedge --headless=new --user-data-dir=<dir> --screenshot=out.png --window-size=W,H <page>

The colours shown are what the caller's terminal would be asked for: a
256-colour or sixteen-colour capture is drawn from the codes actually sent,
with the standard xterm palette, not from the truecolor the art was made in.
"""
import html
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fair_winds as fw  # noqa: E402

OUT = ROOT / ".test-tmp" / "captures"
SIZES = ((80, 24), (132, 40), (168, 46))
DEPTHS = ("truecolor", "256", "16")
CELL_W, CELL_H = 9, 18
DEFAULT_FG, DEFAULT_BG = (204, 204, 204), (10, 13, 20)


def xterm(n: int):
    if n < 16:
        return fw.ANSI16[n]
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return v, v, v
    n -= 16
    steps = (0, 95, 135, 175, 215, 255)
    return steps[n // 36], steps[n // 6 % 6], steps[n % 6]


def colour(code: str, default):
    """The RGB a terminal shows for one of the buffer's colour codes."""
    if not code or code in ("39", "49"):
        return default
    parts = code.split(";")
    if parts[0] in ("38", "48"):
        if parts[1] == "2":
            return tuple(int(v) for v in parts[2:5])
        return xterm(int(parts[2]))
    if parts[0] == "1":                         # bold: a bright foreground
        return fw.ANSI16[8 + int(parts[1][1])]
    return fw.ANSI16[int(parts[0][1])]


def page(screen: "fw.Screen", title: str) -> str:
    cells = screen.cells()
    rows = []
    for y in range(screen.height):
        spans = []
        for x in range(screen.width):
            ch, fg, bg = cells[y * screen.width + x]
            f, b = colour(fg, DEFAULT_FG), colour(bg, DEFAULT_BG)
            if ch == fw.HALF_BLOCK:
                style, ch = f"background:linear-gradient(rgb{f} 50%,rgb{b} 50%)", " "
            elif ch == fw.LOWER_BLOCK:
                style, ch = f"background:linear-gradient(rgb{b} 50%,rgb{f} 50%)", " "
            else:
                style = f"color:rgb{f};background:rgb{b}"
            spans.append(f'<span style="{style}">{html.escape(ch)}</span>')
        rows.append("".join(spans))
    return (f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<style>body{{margin:0;background:rgb{DEFAULT_BG}}}'
            f'pre{{margin:0;font:{CELL_H - 3}px/{CELL_H}px Consolas,"Cascadia Mono","DejaVu Sans Mono",monospace}}'
            f'span{{display:inline-block;width:{CELL_W}px;height:{CELL_H}px;text-align:center;vertical-align:top}}'
            f'</style><pre>' + "\n".join(rows) + "</pre>")


def frontier(game) -> None:
    """A mast, a station gone silent and a kit to plant: the pilot down in the wild among them."""
    import dataclasses
    spots = []
    for cell in sorted(game.world._land):
        x, y = (cell % fw.CHART_W + 0.5) * fw.CHART_KM, (cell // fw.CHART_W + 0.5) * fw.CHART_KM
        if all((p["x"] - x) ** 2 + (p["y"] - y) ** 2 >= 62 ** 2 for p in game.world.towns + game.stations) \
                and all((sx - x) ** 2 + (sy - y) ** 2 >= 62 ** 2 for sx, sy in spots) \
                and all((sx - x) ** 2 + (sy - y) ** 2 <= 110 ** 2 for sx, sy in spots):
            spots.append((x, y))
        if len(spots) == 3:
            break
    for i, (x, y) in enumerate(spots):
        game.kits = 1
        game.state = dataclasses.replace(game.state, x=x, y=y, layer=0, status="ground")
        if i < 2:
            game.key("k")
    game.crates = fw.MAST_CRATES
    game.state = dataclasses.replace(game.state, x=spots[0][0], y=spots[0][1])
    game._visit()
    game._own_db() if game.store is None else game.store.db.execute(
        "UPDATE stations SET stock_day=stock_day-20 WHERE id=4")
    game._see_stations()
    db = game._own_db() if game.store is None else game.store.db
    fw.call_for_help(db, 77, "Juno", spots[2][0] + 22.0, spots[2][1] - 14.0, 1, 15, 0)
    fw.sign_book(db, 3, 77, "Juno", 1, "Came in on the evening wind. The mast is a fine thing.")
    fw.sign_book(db, 3, 78, "Bram", 2, "Left two crates. Who brings the rest?")
    game._see_calls()
    game.state = dataclasses.replace(game.state, x=spots[2][0], y=spots[2][1])
    game._choose_default()


def drifters(game) -> None:
    """A few pilots on today's Drift table."""
    rows = [(game.today, 70 + i, who, "{}", 1, score, km, turns, 9, float(i))
            for i, (who, score, km, turns) in enumerate((("Juno", 912, 3.4, 15), ("Bram", 871, 7.9, 16),
                                                         ("Quill", 640, 31.0, 22), ("Nib", 0, 12.0, 9)))]
    game.store.db.executemany("INSERT INTO drifts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def medals(game) -> None:
    """A pilot part way through the Trials."""
    game.medals = [3, 3, 2, 1, 0, 3, 0, 2, 0, 0, 0, 0]
    game.taught = True


def almanac(game) -> None:
    """The almanac with a few winds entered and one part way."""
    for i, (wind, who) in enumerate(zip(game.winds[:5], ("Juno", "Bram", "Mara", "Juno", "Quill"))):
        game.almanac[wind["id"]] = (70 + i, who, 3 + i * 2)
    game.flown = {game.winds[6]["id"]: 2}
    game.pages = True


def book(game) -> None:
    """The visitors' book at a mast, with a line half written."""
    import dataclasses
    frontier(game)
    mast = game.stations[3]
    game.state = dataclasses.replace(game.state, x=mast["x"], y=mast["y"])
    game.key("b")
    game.key("w")
    for ch in "Fair winds from the first to land here":
        game.key(ch)


def fly(width: int, height: int, depth: str, charset: str = "utf-8", turns=("ENTER", "w", "ENTER", "ENTER", "w"),
        view: str = "front", setup=None):
    """A game a few turns into its first flight, and the frame it shows."""
    info = {"door_api": 5, "handle": "Mara", "user_id": 12, "node_id": "capture-node", "node_name": "Test Node",
            "terminal_width": width, "terminal_height": height, "color_depth": depth, "charset": charset,
            "transport": "ssh"}
    launch = fw.parse_door_info(json.dumps(info), {})
    with tempfile.TemporaryDirectory(prefix="fair-winds-capture-") as folder:
        game = fw.PlainFlight(launch, Path(folder))
        if setup is not None:
            setup(game)
        for key in turns:
            game.key(key)

        class Quiet:
            def write(self, text):
                pass
        session = fw.Session(launch, None, Quiet(), game=game)
        session.view = view
        session.paint()
        game.close()                      # the folder cannot be removed while its records are open
        return session.frame


SCENES = {
    "splash": dict(kind="splash", hour=6.6, seed=7, steps=99),
    "launch-noon": dict(kind="launch", hour=12.5, cloud=0.3, biome=fw.GRASS, seed=11, place=True, steps=5,
                        caption="Day 3, 12:30. Up and away from Gull Post.", sub=""),
    "landing-dusk": dict(kind="landing", hour=19.7, cloud=0.55, biome=fw.FOREST, seed=23, place=True, steps=99,
                         caption="Down at Thistle Cairn: a clean landing.", sub="The post for Thistle Cairn is delivered. That makes 4."),
    "landing-night": dict(kind="landing", hour=22.5, cloud=0.1, biome=fw.DRY, seed=31, place=False, steps=99,
                          caption="Down in the wild: a rough landing.", sub=""),
}


def scene(name: str, width: int, height: int, depth: str):
    """A scene at one of its frames, as the frame it shows."""
    spec = dict(SCENES[name])
    steps = spec.pop("steps")
    pixels = depth != "16"
    playing = fw.Scene(spec.pop("kind"), width, height, pixels, True, flat=depth != "truecolor", **spec)
    playing.advance(steps)
    screen = fw.Screen(width, height, depth)
    playing.draw(screen)
    return screen


def main(argv) -> int:
    sizes = [tuple(int(v) for v in a.split("x")) for a in argv if "x" in a] or SIZES
    depths = [a for a in argv if a in DEPTHS] or DEPTHS
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for width, height in sizes:
        for depth in depths:
            name = f"flight-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth), name), encoding="utf-8")
            print(name, f"{width * CELL_W}x{height * CELL_H} px")
            name = f"office-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("o", "2", "f")), name), encoding="utf-8")
            name = f"atlas-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("ENTER", "w", "ENTER", "ENTER", "ENTER",
                                                                         "ENTER", "ENTER", "ENTER", "c")), name),
                                    encoding="utf-8")
            name = f"frontier-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=(), setup=frontier), name), encoding="utf-8")
            name = f"chart-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("c",), setup=frontier), name), encoding="utf-8")
            name = f"chart-near-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("c", "z", "z"), setup=frontier), name),
                                    encoding="utf-8")
            name = f"drift-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("t",), setup=drifters), name), encoding="utf-8")
            name = f"drift-flying-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("t", "p", "ENTER", "ENTER", "ENTER")), name),
                                    encoding="utf-8")
            name = f"trials-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("r", "s", "s"), setup=medals), name),
                                    encoding="utf-8")
            name = f"tutorial-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("r", "ENTER", "w", "s", "ENTER")), name),
                                    encoding="utf-8")
            name = f"almanac-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=(), setup=almanac), name), encoding="utf-8")
            name = f"book-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=(), setup=book), name), encoding="utf-8")
            name = f"help-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, view="help"), name), encoding="utf-8")
            name = f"loft-{width}x{height}-{depth}.html"
            (OUT / name).write_text(page(fly(width, height, depth, turns=("g", "p", "p", "z", "z", "z")), name),
                                    encoding="utf-8")
            for which in SCENES:
                name = f"scene-{which}-{width}x{height}-{depth}.html"
                (OUT / name).write_text(page(scene(which, width, height, depth), name), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
