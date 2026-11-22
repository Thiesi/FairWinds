# Fair Winds: systems

The specification the slices in [design.md](design.md) implement. Every number
here is a starting value. A slice that changes a rule or a number changes this
file in the same PR, so the file always describes the game as built.

## 1. Units and conventions

- Distance in km, speed in km/h, time in minutes. One **turn** is 15 minutes.
- World coordinates: `x` east, `y` south, origin at the north-west corner. The
  world is 1600 by 1000 km.
- A wind vector points where the air is going. Wind is *named* by where it
  comes from, as in meteorology: a vector pointing east is a west wind.
- A **world day** is the game's calendar day, numbered from 1.
- A **pilot** is one `(node_id, user_id)` pair.
- All randomness comes from the keyed source in section 14. Nothing calls an
  unseeded generator.

## 2. The calendar

The unit of play is the world day. A call, a session and a connection mean
nothing to the game.

**World days.** A world keeps `epoch_date`, the node-local date on which it was
created, and `tempo`, the number of world days per real day (default 1; 2, 3 or
4 allowed). With tempo 1, world day `N` is the number of local dates since the
epoch, plus one, in the node's `timezone`. With tempo `k`, each local date holds
`k` equal slots and a day opens at the start of each slot. Counting local dates
makes daylight-saving changes harmless.

The world also stores `high_day`, the highest day it has ever opened. The
current day is `max(computed day, high_day)`, so a clock set backwards never
repeats a day.

**The logbook.** Each pilot has `logbook_day`, the next world day they will fly.

- A pilot may fly `logbook_day` when `logbook_day <= today`. They can never fly
  a day that has not opened.
- Flying a day to its end advances `logbook_day` by one. A day is flown at most
  once.
- At most three opened days wait. On every launch, if `logbook_day < today - 2`,
  it becomes `today - 2`. Missed days beyond that lapse without penalty.
- Days are flown in order, oldest first, each in its own weather. A pilot who is
  up to date flies today's weather, which is the weather the board is talking
  about.
- A new pilot starts with `logbook_day = today`.
- A flying day in progress keeps its day through a turnover.

**What a flying day is.** Dawn at 06:00, dusk at 20:00, a hard stop at 24:00:
56 daylight turns and 16 night turns at most. The day ends when the pilot
chooses to stay the night after landing, or at 24:00. A pilot may land, do
business and launch again on the same day while time remains. A flying day can
be left at any turn and resumed later; nothing is lost and nothing advances.

**Costs paid in days.** Walking out after stranding advances `logbook_day` by
two. Being picked up at sea advances it by one. If that puts the logbook ahead
of today, the pilot waits for the calendar.

**What the calendar does not limit.** Trials, Daily Drift practice, the chart,
the almanac, the album, the rigging loft and guestbooks are always open. The
scored Daily Drift attempt is one per world day.

**Why pace at all.** The map is shared and finite. Without a calendar one
caller could chart a continent in a weekend and leave nothing for anyone else.

**What the caller is told.** The briefing always names the day being flown, how
many days are waiting, and when the next one opens:
`Logbook: Day 39. Two more days are waiting. Day 42 opens at midnight.`

## 3. The world

**Seed.** `world_seed = sha256("fair-winds/1/" + node_id + "/" + generation)`,
truncated to 64 bits. `generation` starts at 0 and changes only if a SysOp
starts a new world. A standalone launch uses a fixed development `node_id`.

**Elevation.** Value-noise fBm shaped into one continent: a large-scale field,
a ridged field for mountain chains, a detail field, and a falloff toward the
world's edge so that land covers 38 to 52 percent of the world. Heights run from
the sea floor to about 4,000 m. The pitch page's script holds a working
prototype of this shape; port its idea, not its constants.

**Moisture and biomes.** A second field gives moisture. Biomes come from
elevation and moisture: sea, shore, grassland, forest, dry plain, hills, high
mountain, snow. Biome sets map colour, station vista and surface behaviour.

**Tiles.** The world is baked at 2.5 km per sample into tiles of 32 by 32
samples (20 by 13 tiles, rounded up). A tile stores elevation and biome per
sample. Tiles are baked on first need and cached in the world's data directory;
a cache is keyed by seed and bake version and is rebuilt if either differs or
its checksum fails. The chart uses a 12.5 km reading derived from the same
tiles. Rendering only ever reads tiles.

**Settlements.** 14 to 18 towns placed by the generator: ports on the coast,
others on lowland and in valleys, at least 80 km apart, every one reachable.
Towns have names from the name generator, a post office, stores and a rigging
loft. Three starting weather stations stand near the pilot start town.

**Features.** The generator labels 200 to 260 features: peaks, ranges, passes,
lakes, bays, capes, plains, forests. Each has a footprint of chart cells and a
seed for its name candidates. Features are unnamed until charted (section 7).

**Local winds.** The generator defines 12 to 20 recurring local winds (section
4). They are the almanac's content.

**Wonders.** Forty sites or conditions (section 9).

**The Gyre.** A permanent cyclone centred inland, at least 300 km from the
start town, over land that is hard to reach.

## 4. Weather

Weather is a pure function. It is never stored and nothing advances while
nobody plays.

`wind(seed, day, minute, x, y, layer) -> (vx, vy)` is the sum of:

1. **Prevailing flow.** A base vector per layer that turns slowly with the
   season's regime and with latitude.
2. **Travelling systems.** Highs and lows spawn at the upwind edge in numbered
   slots, about one every 1.5 world days, decided by the keyed source. Each has
   a path, a speed of 250 to 500 km per day, a radius of 200 to 320 km, a
   strength and a life of 3 to 6 days. The systems alive at a moment are found
   by checking the last few slots. Lows turn anticlockwise with inflow, highs
   clockwise with outflow. A system's wind peaks at its radius and falls off
   quickly beyond it.
3. **The Gyre.** The same shape, larger and stronger, fixed in place, pulsing
   with the season.
4. **Local winds.** Sea breeze onshore in the afternoon in layers 1 and 2 near
   coasts; downslope wind at dawn in layer 1 near high ground; acceleration
   through passes in layers 1 to 3.
5. **Noise.** The curl of a low-resolution field that drifts with time.

A layer is **blocked** where terrain reaches into it. Wind there falls to a
fraction and a balloon cannot be in that layer at that place.

| Layer | Name | Band | Typical speed | Needs |
| --- | --- | --- | --- | --- |
| 1 | Surface | 0 to 0.5 km | 5 to 20 km/h | |
| 2 | Low | 0.5 to 1.5 km | 15 to 40 km/h | |
| 3 | Mid | 1.5 to 2.75 km | 25 to 55 km/h | |
| 4 | Upper | 2.75 to 4.25 km | 35 to 70 km/h | Envelope II |
| 5 | High | 4.25 to 6 km | 50 to 100 km/h | Envelope III |
| 6 | Jet | 6 to 8 km | 80 to 160 km/h | Envelope III and oxygen |

**Other fields**, from the same systems: pressure (for isobars), cloud decks by
layer, squalls near lows and fronts, fog in valleys at dawn, the icing level,
and surface wind for landing.

**Forecast.** `forecast(..., lead)` is the same function with the systems'
positions and strengths perturbed. The perturbation is drawn from the keyed
source per system and day, so every pilot sees the same forecast and it
converges on the truth as the lead shrinks to zero. Its size is
`lead_error(lead) * (1 - coverage(x, y))`. Coverage is the best nearby station:
1 at the station, falling to 0 at 150 km, scaled by the station's stock. Towns
and masts count as full stations. Inside the Gyre's bands coverage is capped.

**What the pilot knows.** The true wind in the current layer at the current
place. Forecast wind everywhere else, out to the instrument's horizon: 6, 10 or
16 turns. A pibal shows true wind in every layer at the current place for four
turns.

**Cost.** One wind sample must stay under 60 microseconds. Screens sample wind
on a coarse grid (10 km) and interpolate.

## 5. Flight

**State.** Position, layer (0 on the ground), fuel, ballast, pibals, wear,
cargo, the world day and the turn number.

**A turn.** The pilot picks an action. The action applies, the balloon drifts
for 15 minutes on the true wind of the layer it is now in, hazards resolve, the
turn is committed, and only then is the result shown.

| Action | Fuel | Effect |
| --- | --- | --- |
| Launch | 2 | Ground to layer 1 |
| Hold | 1 by day, 0 from 11:00 to 15:00, 2 at night | Stay in the layer |
| Climb | 3, plus 1 into layer 5, plus 2 into layer 6 | Up one layer |
| Sink | 0 | Down one layer |
| Jettison | 0, one ballast sack | Up one layer at once, no fuel |
| Pibal | 0, one pibal, no turn | True wind in every layer here, four turns |
| Land | 0 | From layer 1 to the ground, if the surface allows |

Starting stores: 60 fuel, 5 sacks, 3 pibals. Fuel, sacks and pibals are bought
at towns and masts.

**Preview.** Before committing, the pilot steps through the layers within one
step. For each, the screen draws the forecast track for the instrument's
horizon and the cost. This is the core of the interface.

**Terrain.** If the drift would carry the balloon into a blocked layer, it
climbs one layer automatically at double cost. With no fuel and no ballast it
comes down on the high ground: a hard landing and a stranding.

**Landing.** By surface wind: up to 15 km/h is clean; up to 30 is rough (wear,
and a check on fragile cargo); above 30 is refused unless fuel is gone, and is
then hard (heavy wear, half the cargo lost). A night landing away from a lit
mast is one class worse. Fog is one class worse without a mast.

**Out of fuel.** The balloon sinks one layer a turn and lands where it is.

**Stranded.** Down in the wild with no fuel. The pilot may walk out to the
nearest town (two days, section 2) or wait for a rescue (section 7).

**At sea.** A landing on water is a ditching: the packet boat returns the pilot
to the nearest port, cargo is lost, one day passes.

**Wear.** 0 to 100. Rough and hard landings, squalls and icing add to it. At 100
the balloon is grounded until repaired at a town or mast. Nothing is ever
destroyed and no career ends.

**Hazards.** Squall: each turn inside one moves the balloon a layer up or down
by the keyed source and adds wear. Icing: above the icing level without an
upgraded envelope, holding costs double. Rotor: in the lee of a ridge in layers
1 and 2, a keyed chance of losing a layer. Every hazard is visible on the map or
named in the forecast before the pilot enters it.

## 6. Economy and progress

All amounts are data tables in the source, not logic.

**Postmarks** are the currency. Sources: mail and parcel deliveries, charting,
first namings, wonders, station resupply, rescues, the Daily Drift, season
goals. Sinks: fuel, ballast, pibals, repairs, upgrades, station kits, mast
materials.

**Contracts.** Each town offers three to five a day, generated from the town,
the day and the pilot's licence: mailbags (light, common), parcels (heavier),
fragile instruments (damaged by rough landings), and frontier freight for masts.
Each has a destination, a weight, a payout and a deadline in world days.
Deadlines are world dates, so waiting costs.

**No dead ends.** A pilot with no postmarks and no fuel can always take a short
subsidised mail run from a town. A scripted career must never reach a state from
which it cannot earn fuel.

**Licence.** Seven grades: Fledgling, Aeronaut, Pilot, Navigator, Wind-reader,
Master, Commodore of the Post. Earned by logbook points from distance,
deliveries, charting and clean landings. A grade opens contract tiers and
upgrade tiers.

**Upgrades.**

| Track | Tiers | Effect |
| --- | --- | --- |
| Envelope | I to III | Lift (cargo weight) and the highest layer |
| Burner | I to III | Fuel cost of climbing |
| Tanks | I to III | Fuel capacity: 60, 90, 130 |
| Instruments | I to III | Forecast horizon: 6, 10, 16 turns; preview reach: 1, 2, all layers |
| Wireless | one | Live reports from stations in range |
| Night gear | one | Night landings one class better; no night fuel penalty |
| Oxygen | one | Layer 6 |
| Camera | I to II | Wonder capture range |
| Engine | one, late | One crab action every eight turns: nudge the drift by 45 degrees |

**Envelope design.** A pilot picks four colours and a pattern (stripes, halves,
chevrons, rings). It shows in scenes and as the pilot's marker on other
callers' maps. Each design has a sixteen-colour reading.

## 7. The shared world

**Charting.** The chart grid is 10 km cells, 160 by 100. Each turn the balloon
charts the land cells within sight: 10 km from layer 1, 20 from layer 2, 30 from
layer 3, 40 from layer 4, 50 above. A cloud deck between the balloon and the
ground blocks sight. A cell records who charted it first and on which day.
Charted percentage counts land cells only.

**Names.** A feature is discovered when 60 percent of its cells are charted.
The pilot whose turn crosses that line is offered three names composed from the
generator's word lists and picks one. If they decline, the first is used. Caller
text never enters a name.

**Stations.** A pilot who lands in the wild with a station kit may plant a
station, at least 60 km from any other. A station has stock from 0 to 14. Stock
falls by one each world day. Any pilot landing there may resupply it for a
reward. At zero it lapses: it stays on the chart, gives no coverage, and can be
restored. Forecast coverage is section 4.

**Masts.** A station becomes a mooring mast when pilots have delivered the
required freight from towns. Contributions are recorded per pilot. A mast gives
full coverage, a lit landing at night and in fog, fuel, and a post office with
contracts. Masts do not lapse.

**Rescue.** A stranded pilot is shown on every chart with what they need. Any
pilot may fly it in. Both are rewarded, once. The stranded pilot resumes from
that spot with the delivered fuel.

**Guestbooks.** Each station and mast has one. A pilot may leave one line of up
to 60 characters per station per world day. Text is stripped of control
characters and escape sequences and shown as plain text. `DOOR_FW_GUESTBOOK`
may be `on` (default), `canned` (phrases from a list only) or `off`. The SysOp
tool lists and removes lines.

**Other pilots.** The map and chart show where other pilots are moored, camped,
stranded, or were last seen aloft, with their envelope colours. Positions are
read from the world when a screen is drawn. Nobody needs to be online together.

**The almanac.** A local wind is entered when one pilot has flown through it
while it was blowing on three different world days. It gets a name from the
generator and that pilot's credit. An entered wind is drawn on every pilot's
forecast when its conditions hold.

**The Gazette.** A record of the week: stations planted, features named,
almanac entries, records, the season's progress. It is a screen in the door and,
if the SysOp allows, a weekly board post (section 13).

## 8. Always open: Daily Drift and Trials

Neither costs calendar time. Neither writes to the shared map.

**Daily Drift.** One course per world day, the same for every pilot on the
node: a start, a target, a standard balloon and that day's weather. The aim is
to land as near the target as possible, in few turns, with fuel to spare. The
formula and tie-break are shown in help. One scored attempt per world day;
practice is unlimited and unranked. The standings table is the top ten.

**Trials.** A tutorial flight and twelve challenge flights with fixed weather
and bronze, silver and gold thresholds: threading a pass, a night run on the
jet, a squall line, a flight with no ballast, a long drift on one tank. Attempts
are unlimited. The tutorial teaches preview, commit, ballast, the pibal and
landing by having the caller do each once.

## 9. Wonders, seasons and the Gyre

**Wonders.** Forty. Each is a place, or a condition, or both: an aurora north
of a pass on a clear night above 3 km; a salt flat at noon; a waterfall from
layer 1; a cloud sea at dawn from above the deck; lightning seen from a safe
distance. A wonder is captured with a free action when its conditions hold. The
first pilot is credited. Each capture shows a full-screen scene. Scenes come
from about eight parametric families, not forty drawings. The album shows what
has been seen, by whom first, and a hint for each wonder not yet seen; hints
sharpen as the node's chart grows.

**Seasons.** 28 world days. Each has a weather regime (a turn of the prevailing
flow and a pulse of the Gyre), a node goal generated to be reachable by one
active pilot and faster with more (raise a chain of masts through a pass, chart
a region, supply a frontier town), and a race in its last seven days (the
longest straight-line flight in one flying day). At the close, medals go to at
most three pilots. Nothing a pilot owns is reset. The map is never reset.

**The Gyre.** Three bands around the centre. In the outer band forecast
coverage is capped at half. In the inner band it is capped at zero and nothing
can be planted. The eyewall needs layer 5 or above, or the engine. Inside is the
eye: calm air, the fortieth wonder, and a place in the hall for the first pilot
on the node to arrive. What a later version puts there is out of scope.

## 10. Presentation

**Readings.** Colour: truecolor, 256, 16, from `color_depth`. Charset: UTF-8
with half blocks, CP437 with shade blocks, ASCII, from `charset`. Quarter blocks
are used only when `quadrants` is true and only for fine art. Limited palettes
get an art-directed reading, never a nearest-colour fallback.

**Layout tiers.** By the caller's geometry, changing live on resize.

| Tier | From | What it adds |
| --- | --- | --- |
| Small | 80 by 24 | Map, the layer table, stores, two log lines, keys |
| Medium | 132 by 40 | Relief map, the sky column, layer rows, a longer log |
| Wide | 168 by 46 | Forecast strip, pilots in the air, cargo |

A terminal 200 columns or wider may later show the map and chart side by side.

**Screens.** Briefing, flight, station, post office, stores, rigging loft,
chart, almanac, album, guestbook, Daily Drift, Trials, gazette, hall, help.
The pitch page shows flight, station, chart, a wonder, and the small tier.

**The cell buffer.** A grid of glyph, foreground and background. Output is the
changed runs only, with SGR sequences kept minimal. The bottom-right cell is
never written. A resize throws the buffer away and repaints.

**The map.** Composed in order: terrain from tiles with hillshade; the uncharted
veil; cloud and squall; wind streaks; tracks; markers; labels. A half-block cell
holds two pixels. A glyph over the map takes the cell: its background becomes
the darkened mean of the two pixels beneath.

**Wind.** A few hundred particles advected on the interpolated wind of the
viewed layer, drawn as short fading streaks. Animation runs for a burst after
each turn and then holds still. With motion off, with `DOOR_SCENES=0`, or with
keys typed ahead, a still streak pattern is drawn instead. If output backs up,
frames are dropped, never queued.

**Scenes.** Splash, launch, landing, station vista, wonders. A scene plays only
after what it shows has been saved, never waits for a key, ends on any key and
consumes it. `DOOR_SPLASH=0` removes the splash. `DOOR_SCENES=0` removes the
rest.

**Budgets.** S08 measures and fixes these. Targets: under 40 KB a second while
animating, under a quarter of one core, and a one-hour session inside the
shipped profile's CPU limit with margin.

## 11. Input

An incremental decoder with bounded reads. Arrow keys and letters; escape
sequences may arrive split. No key-up, no chords, no timing. Keys typed while a
scene or a turn result is drawn are discarded, so a held key never commits
turns unseen. The key for an action is the first letter of its label. `B` and
Esc go back, `<` and `>` page, `/` finds, `?` shows help.

## 12. Storage

One SQLite file, `fair_winds.db`, under `DOOR_FW_DATA_DIR` or the
installation's `data` directory, with an `application_id` of its own and a
`user_version` for the schema. WAL mode. Writes use short immediate
transactions. No transaction is open while a caller thinks.

| Table | Holds |
| --- | --- |
| `meta` | Node id, seed, generation, epoch date, tempo, high day, maintenance flag |
| `pilots` | Identity, logbook day, licence, postmarks, upgrades, envelope design, home |
| `sessions` | One lease per pilot with a heartbeat |
| `flights` | The flight in progress: state after the last committed turn |
| `turns` | The journal of committed turns for the current flying day |
| `cargo`, `contracts` | What is carried and what is on offer |
| `charted` | Chart cells: first pilot and day |
| `features` | Discovery, chosen name, pilot, day |
| `stations`, `contributions` | Stations, stock, mast progress, who gave what |
| `strandings` | Who is down, where, what they need |
| `guestbook` | Lines per station |
| `almanac`, `wonders` | Entries and first sightings |
| `drift`, `trials` | Scores |
| `seasons`, `hall` | Season state, medals, firsts |
| `outbox` | Outbound request identifiers and receipts |

The terrain tile cache is separate files beside the database. It can always be
rebuilt and need not be backed up.

**The turn commit.** One transaction writes the new flight state, the journal
row, any chart cells, and any world change the turn caused. A process killed at
any instant leaves either the turn before or the turn after.

**The lease.** A pilot has one live session. A second launch finds the lease:
if its heartbeat is stale it is taken over; if it is live the caller is told
and may take over, which ends the other session at its next turn.

**Guests.** A guest call opens the world read-only and keeps its own state in
memory.

## 13. Host integration

**Launch.** `NETBBS_DOOR_INFO` names the JSON. Used fields: `door_api`,
`handle`, `user_id`, `node_id`, `node_name`, `terminal_width`,
`terminal_height`, `color_depth`, `charset`, `quadrants`, `unicode_style`,
`transport`, `timezone`, `datetime_format`, `session_limit_seconds`, `outbound`.

**Environment.** Only `DOOR_*` names reach the door.

| Name | Meaning |
| --- | --- |
| `DOOR_FW_DATA_DIR` | Where the world lives |
| `DOOR_FW_GUEST_IDS` | Comma-separated `user_id`s to treat as guest calls |
| `DOOR_FW_GUESTBOOK` | `on`, `canned` or `off` |
| `DOOR_FW_MOTION` | `auto`, `burst` or `off` |
| `DOOR_FW_PIXELS` | `auto`, `half` or `shade` |
| `DOOR_SPLASH`, `DOOR_SCENES` | `0` switches the splash, or the scenes, off |
| `DOOR_FW_NODE_ID` | Classic hosts: names this BBS and seeds its world (default `classic`). Keep it once set. |
| `DOOR_FW_BBS_NAME` | Classic hosts: the name in "Return to ..." (default "BBS") |
| `DOOR_FW_TIMEZONE` | Classic hosts: the IANA zone whose midnight opens a world day (default UTC) |
| `DOOR_FW_JOURNAL` | `wal` (default) or `delete`, for a data directory where WAL cannot work |

Tempo is a property of the world, set with the SysOp tool, not an environment
variable, so that it cannot change by accident.

**Outbound.** Sent only when the `outbound` block is present, and never for a
guest call. Request names are zero-padded and their identifiers are stored in
`outbox` before the file is renamed into place.

| Request | When | What a bot says |
| --- | --- | --- |
| `rank` | A licence grade is earned | Nib reached Navigator in Fair Winds. |
| `achievement` | An almanac entry, a first naming of a major feature, a first rescue | Vex earned the achievement "Named the Bora" in Fair Winds. |
| `capture` | A wonder is seen first | Mara captured the Aurora at Lake Indra in Fair Winds. |
| `medal` | A season closes | Season 4 of Fair Winds closed: gold Quill, silver Mara, bronze Nib. |
| `season` | A season opens | A new Fair Winds season begins: The Northern Passage, 28 days. |
| standings | The Daily Drift table changes | Answers `!top` |
| board post | Once a week | The Gazette |
| chat line | A squall line reaches a town, at most a few a day | A one-line warning |

**The launch record.** Everything after start-up reads one host-neutral record:
pilot identity, handle, node identity, node name, geometry, colour depth,
charset, quarter blocks, glyph preference, transport, timezone, date format,
session limit, outbound, guest. NetBBS's JSON fills it on a hosted launch, a
drop file on a classic host, and defaults on a standalone launch. No code
outside start-up knows which host it is on.

**Classic hosts.** `fair_winds.py <drop file>` reads DOOR32.SYS or DOOR.SYS.
The mapping, the consequences and what is not supported are in
[design.md](design.md#classic-hosts). In short: record number is the pilot,
80 columns with no resize, CP437 and sixteen colours unless `--encoding` and
`--color` say otherwise, still wind unless `DOOR_FW_MOTION=burst`, the drop
file's minutes as the session limit, and no outbound.

**Session end.** If `session_limit_seconds` is present the door warns five
minutes and one minute before it. Being cut off costs nothing: the flight
resumes at the same turn.

## 14. Determinism

`draw(purpose, *keys)` hashes the world seed, a purpose string and its keys
with SHA-256 and returns a number. Purposes include the world generator, system
spawn slots, forecast perturbation, contracts per town and day, the Daily Drift
course, and per-turn hazards keyed by pilot, world day and turn number.

Consequences the tests hold to: the same turn always has the same result;
reconnecting, relaunching or switching transport changes nothing; two processes
on two platforms agree on world, weather, forecast and course.
