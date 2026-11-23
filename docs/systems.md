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

**Elevation.** Value-noise fBm shaped into one continent: a large-scale field
(260 km, five octaves), a ridged field for mountain chains (170 km, four), a
detail field (42 km, three), and a falloff from a centre and radii drawn from
the seed. The world's edge is always sea. The sea level is then set per world
so that land covers a share drawn between 41 and 49 percent of the chart grid.

The shaped height `e` becomes metres as `4000 * e * e` on land, capped at
4,500 m, and `5000 * e` under water, down to 3,000 m. Squaring keeps most land
low, so the surface layer is open over lowland and only hills and ranges block
it: hills from 500 m, high mountain from 1,500 m, snow from 2,800 m.

The generator uses only `+ - * /` and comparisons on doubles, never the maths
library, so every platform bakes byte-identical tiles. A pinned fingerprint in
the tests holds Linux and Windows to the same bytes.

**Moisture and biomes.** A second field (300 km, three octaves) gives
moisture. Biomes come from elevation and moisture: deep sea (below -200 m),
shallow sea, shore (below 30 m), dry plain (moisture under 0.42), forest (over
0.58), grassland, hills, high mountain, snow. Biome sets map colour, station
vista and surface behaviour.

**Tiles.** The world is baked at 2.5 km per sample into tiles of 32 by 32
samples (20 by 13 tiles, rounded up). A tile stores elevation in metres and
biome per sample. Tiles are baked on first need and cached in
`terrain-<seed>-b<bake version>` under the world's data directory, one file per
tile, written whole or not at all. Each file carries its bake version, seed and
position and a checksum; a file that fails any of them is rebuilt and replaced,
never trusted. A cache that cannot be written only costs speed. Rendering only
ever reads tiles. A tile bakes in about 5 ms and reads in under 1 ms on the
development host.

**The chart grid.** The same shape sampled at 10 km, 160 by 100 cells, is built
once per world with the index (about 0.15 s) and cached beside the tiles. It
sets the sea level, and it is the grid that settlements, features and charting
(section 7) use.

**Settlements.** 14 to 18 towns placed by the generator on the mainland, the
largest connected land: up to six ports on the coast first, then others on
shore, grassland, forest and dry plain, at least 80 km apart. Towns have names
from the name generator, a post office, stores and a rigging loft. The first
port is the start town. Three starting weather stations stand 40 to 120 km from
it.

**Features.** The generator labels 200 to 260 features, each a footprint of
chart cells: lakes (water that does not reach the world's edge), islands, up to
forty peaks (local summits above 1,500 m), and stretches of plain, forest, dry
plain, hills and range, cut where they cross an 80 km block so that no feature
is too large to chart in a few flights. The largest are kept. Features are
unnamed until charted (section 7). Passes, bays and capes are left for S14.

**Local winds.** Sea breezes, downslope winds and pass winds come from the
shape of the land (section 4). S18 picks 12 to 20 of the places where they
recur and makes them the almanac's content.

**Wonders.** Forty sites or conditions (section 9).

**The Gyre.** A permanent cyclone centred on the mainland, at least 300 km
from the start town, at the place farthest from every town.

`python fair_winds.py --world-map <node id>` prints a node's continent as
characters, with its towns, stations and the Gyre.

## 4. Weather

Weather is a pure function of the seed, the world day and the minute. It is
never stored and nothing advances while nobody plays. Like the terrain it uses
no maths library apart from the square root, so every platform agrees.

`Weather.frame(day, minute)` gathers everything that does not depend on the
place, once per turn. `frame.wind(x, y, layer)` is then the sum of:

1. **Prevailing flow.** Each season draws a regime: a lean for the whole
   column, and for each layer a swing to one side or the other, alternating
   with height, so neighbouring layers blow different ways. Speeds are drawn
   within 20 percent of the layer's base. In about a third of seasons the two
   lowest layers run the other way, as trades. The flow veers a little through
   the day and with latitude. A season's first day eases from the old regime
   to the new one, and a layer that turns round passes through calm, so the
   wind never jumps at midnight.
2. **Travelling systems.** Highs and lows are drawn in numbered slots, one
   every 1.5 world days, of which 85 percent hold a system. Each has a life of
   3 to 6 days and a radius of 200 to 320 km, enters at the west edge, and
   crosses to the east edge in exactly its life, on a course that drifts and
   wobbles. It fades in over its first half day and out over its last. Lows
   turn anticlockwise with inflow, highs clockwise with outflow. A system's
   wind is `2q / (1 + q^6)` of its strength at `q` radii from its centre: calm
   at the centre, strongest at about three quarters of a radius, gone by two
   and a half. Higher layers feel systems more.
3. **The Gyre.** The same shape, fixed in place, 150 km in radius and far
   stronger, pulsing by season.
4. **Local winds.** Sea breeze in layers 1 and 2 near a coast: onshore from
   11:00 to 19:00, strongest at 15:00, and a weaker offshore breeze at night.
   Downslope wind in layer 1 from 03:30 to 08:30 near steep ground. Wind in
   layers 1 to 3 runs 1.6 times as fast through a pass: low ground with high
   ground within 30 km on both sides.
5. **Noise.** Two drifting fields of small eddies per layer.

A layer is **blocked** where terrain reaches into it: the wind there falls to
15 percent and a balloon cannot be in that layer at that place. Terrain at or
above 500 m fills layer 1, 1,500 m layer 2 and 2,750 m layer 3. The weather
reads terrain from the chart grid.

| Layer | Name | Band | Base speed | Never above | Needs |
| --- | --- | --- | --- | --- | --- |
| 1 | Surface | 0 to 0.5 km | 9 km/h | 35 km/h | |
| 2 | Low | 0.5 to 1.5 km | 24 km/h | 60 km/h | |
| 3 | Mid | 1.5 to 2.75 km | 36 km/h | 80 km/h | |
| 4 | Upper | 2.75 to 4.25 km | 48 km/h | 100 km/h | Envelope II |
| 5 | High | 4.25 to 6 km | 68 km/h | 140 km/h | Envelope III |
| 6 | Jet | 6 to 8 km | 105 km/h | 200 km/h | Envelope III and oxygen |

Mean speeds over a year sit near the base. The caps are reached only around
the Gyre.

**Continuity.** At one place the wind changes by less than 6 km/h from one
turn to the next, across midnight and across a season's turn as well. Between
places 10 km apart in open air it differs by less than 35 km/h. The edge of
blocked terrain and the mouth of a pass are sharp on purpose.

**Other fields**, from the same frame: pressure in hPa (for isobars); cloud
cover from 0 to 1 with its deck in layer 3 near a low and layer 4 elsewhere;
squalls as a narrow broken ring about two thirds of a radius out from each
strong low (a weak low has none); fog from 04:30 to 08:30 over calm
ground below 250 m; the icing level, layer 5, or layer 4 in a cold season; and
the surface wind for landing.

**Forecast.** `frame(day, minute, lead, coverage)` is what a forecast made
`lead` minutes earlier expects. Each system is misplaced by up to
`lead_error * (1 - coverage)` in a direction drawn once per system, so every
pilot sees the same forecast and it closes on the truth as the lead shrinks.
`lead_error` is 400 km per day of lead, capped at 400 km. A forecast also
knows less of the small eddies the further ahead it looks, and nothing of them
beyond three hours without a station. With no lead, or at a station, the
forecast is the truth. Measured with no station: about 0.5 km/h of error half
an hour ahead, 6 km/h at three hours, 11 km/h at a day.

Coverage is the best nearby station: 1 at the station, falling to 0 at 150 km,
scaled by the station's stock. Towns and masts count as full stations. Inside
the Gyre's bands coverage is capped. Stations arrive in S15; until then the
caller of the weather passes the coverage in.

**What the pilot knows.** The true wind in the current layer at the current
place. Forecast wind everywhere else, out to the instrument's horizon: 6, 10 or
16 turns. A pibal shows true wind in every layer at the current place for four
turns.

**Cost.** One wind sample takes about 20 microseconds on the development host
(target: under 60). Screens sample wind on a coarse grid (10 km) and
interpolate.

## 5. Flight

**State.** Position, layer (0 on the ground), status (ground, aloft, stranded,
ditched), fuel, ballast, pibals, wear, cargo, the world day and the turn
number, and what the balloon is fitted with: its ceiling, oxygen, night gear,
an ice-proof envelope, its burner's climb cost. A state is never changed; a
turn makes a new one, and a state can be written down and read back exactly.

**A turn.** The pilot picks an action from those on offer. The action applies,
the balloon drifts for 15 minutes on the true wind of the layer it is now in,
the ground ahead and the hazards resolve, and the new state is returned for the
caller to commit before anything is shown. Turns are numbered from dawn at
06:00; turn 72 is midnight.

| Action | Fuel | Effect |
| --- | --- | --- |
| Launch | 2 | From the ground into the lowest open layer |
| Hold | 1 by day, 0 from 11:00 to 15:00, 2 from 20:00 | Stay in the layer |
| Climb | 3, plus 1 into layer 5, plus 2 into layer 6 | Up one layer |
| Sink | 0 | Down one layer |
| Jettison | 0, one ballast sack | Up one layer, no fuel |
| Pibal | 0, one pibal, no turn | True wind in every layer here, four turns |
| Land | 0 | From the lowest open layer to the ground; judged by the weather at the moment it is chosen; takes the turn, no drift |

Starting stores: 60 fuel, 5 sacks, 3 pibals. The first envelope reaches layer
3. Fuel, sacks and pibals are bought at towns and masts.

**The lowest open layer** is the lowest one the ground here does not fill: 1
over lowland, 2 over hills, 3 over high mountain, 4 over the highest peaks. A
balloon launches into it, lands from it and cannot sink below it.

**Preview.** Before committing, the pilot steps through the layers within one
step. For each, `preview` returns the forecast track for the instrument's
horizon (the action, then holding) and the cost. This is the core of the
interface.

The pilot knows the true wind only where they have read it: in their own
layer, in the layer a launch enters (they are standing in it), and in every
layer while a pibal's reading lasts. There a track starts
from the truth and looks ahead turn by turn. Any other layer is forecast from a
bulletin two hours old, so it is a guess from its first turn. That gap is what
a pibal is for. At a station the forecast is exact either way.

A track follows fuel and ballast as the flight would, so it never shows
somewhere the balloon cannot get to. It reports the fuel and the ballast sacks
that forced climbs would cost, and it ends early where the flight would: at ground above the
ceiling, at a forced climb that cannot be paid for, or where the fuel for
holding runs out.

**Terrain.** If the drift carries the balloon to ground that fills its layer,
it climbs over at double the climb cost per layer, spending a ballast sack for
any layer the fuel cannot pay for. With neither it comes down on the high
ground: a hard landing and a stranding. Ground that rises above the envelope's
ceiling cannot be crossed at all: the balloon is held in its lee for the turn,
does not move, and takes 2 wear. A previewed track shows a forced climb and
stops at ground it cannot cross.

**Landing.** By surface wind: up to 15 km/h is clean; up to 30 is rough (10
wear, and fragile cargo is damaged); above 30 is hard (30 wear, about half the
cargo lost, the fragile survivors damaged). At night away from a town or
station it is one class worse, unless the balloon has night gear. In fog away
from a town or station it is one class worse again. A hard landing is refused
while the pilot has any other way to stay up. A town or station counts as
reached within 12.5 km of it.

**Out of fuel.** Only free actions remain: sinking, landing, a jettison, and
holding in the midday sun. So the balloon sinks a layer a turn and lands where
it is.

**Midnight.** A balloon still aloft at turn 72 is put down where it is.

**Stranded.** Down in the wild with no fuel. The pilot may walk out to the
nearest town (two days, section 2) or wait for a rescue (section 7). Out of
fuel at a town or station is not a stranding.

**At sea.** A landing on water is a ditching: the packet boat returns the pilot
to the nearest port, cargo is lost, one day passes.

**Wear.** 0 to 100. Rough and hard landings, squalls, rotor and ridges add to
it. At 100 the balloon cannot launch until repaired at a town or mast. Nothing
is ever destroyed and no career ends.

**Hazards.** Each is a keyed draw for the pilot, the day and the turn.

- *Squall.* In layers 1 to 4, where the squall field is above 0.4: the balloon
  is shoved up or down a layer, within the ground and its ceiling, and takes up
  to 6 wear.
- *Rotor.* In layers 1 and 2, with ground within 15 km upwind that reaches the
  balloon's layer: a 35 percent chance each turn of 2 wear, and of dropping a
  layer where there is one below. (The plan had rotor only drop a layer; real
  terrain almost never puts lowland that close behind a range, so it could not
  happen.)
- *Icing.* At or above the icing level without an ice-proof envelope, holding
  costs double, and at least 2.

Every hazard is visible on the map or named in the forecast before the pilot
enters it.

## 5a. The plain flight

S05 puts the whole turn on an 80 by 24 text screen, to find out whether it is
fun before anything is drawn. It is a stand-in: S06 to S13 replace its screen,
its save and its placeholder rules, and keep its findings.

**The screen.** A character map 260 km wide and 160 km tall centred on the
balloon; a table of the layers with each one's wind, marked `~` where it is a
forecast and not a reading; stores, daylight, ground wind and sky; the last
three log lines; one sentence saying what the chosen action costs and where it
leads; and the keys. Digits on the map are the tracks of the choices on offer,
six turns ahead, the chosen one bright.

**Keys.** W and S or the arrows step through sink, hold and climb. Enter or
Space flies the choice. J chooses a jettison, L a landing, P releases a pibal
at once. N stays the night. Keys typed before a turn's result is on screen are
dropped, so a held key flies one turn.

**The post.** There is post for the three nearest places not visited lately,
towns and stations alike, and landing at any of them delivers it. One fixed
destination is the wrong game: with three layers the winds span less than half
the compass on most days, so a single town is often simply upwind. Measured
with a search that flies on the true winds: one of three places can be reached
in a day on about three days in four, in 6 to 42 turns; one fixed nearest town
on fewer than half. S12's contracts keep this shape: several destinations a
day, and the wind picks.

**Placeholders, each replaced by the slice named.** A delivery fills the balloon
up and mends it for nothing, and so does a night at a town or station; landing
where there is no post changes nothing (S12). A new day starts when the pilot
asks, with no calendar (S11). Walking out and the packet boat happen at a key
press (S16). The save is one JSON file per pilot, written whole and synced
after every turn. A save that cannot be read is moved aside to `<save>.bad`,
never overwritten, and the pilot is told on the first screen; there is one such
file per pilot, so a SysOp who wants one should collect it before it happens
again. If it cannot even be moved aside, the game is played unsaved and the
file is left alone (S10). Forecast coverage counts towns and the three
starting stations (S15).

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

| Tier | From | The panel |
| --- | --- | --- |
| Small | 80 by 24 | 26 columns: a table of the layers within reach, stores as numbers, the post |
| Medium | 132 by 40 | 40 columns: the sounding, stores as bars, the landing named, the post |
| Wide | 168 by 46 | The same, and the forecast for the layer in view, hour by hour for six hours |

Within a tier the layout follows the terminal cell for cell: the panel keeps
its width, the log and keys keep their rows, and the map takes everything
else. 80 by 24 gives a map of 52 by 16 cells, 132 by 40 one of 90 by 32, and
168 by 46 one of 126 by 38, so a bigger window shows more of the world.

**The sounding.** All six layers from the jet down, two rows each: the layer's
name, its wind as the pilot knows it (`~` marks a forecast), the fuel to get
there, and under that a bar of its speed against the most that layer ever
blows, with a word where there is one to say: `you`, `cloud deck`, `icing`,
`out of reach`, `needs oxygen`, or `ground` where the land fills the layer.
Beside the rows, where the map is drawn in pixels, stands a slice of sky eight
cells wide: darker with height, the ground filling the layers it reaches, the
cloud deck in its layer, and the balloon hanging in the layer it is in. On the
character map there is no sky column and the rows start at the panel's edge.

Stores are bars for fuel, wear and daylight, and a row of marks for ballast
sacks and pibals. While aloft the panel names the landing the ground wind
would give now. Bars and marks use only the full block, the middle dot and the
black square, which CP437 has; an ASCII caller gets `#` and `-`.

**Screens.** Briefing, flight, station, post office, stores, rigging loft,
chart, almanac, album, guestbook, Daily Drift, Trials, gazette, hall, help.
The pitch page shows flight, station, chart, a wonder, and the small tier.

**The cell buffer.** Every screen is drawn onto a `Screen`: a grid of cells,
each a glyph with a foreground and a background. `render(previous)` returns
only what turns the last frame into this one: it jumps to each run of changed
cells, writes through gaps of up to four unchanged cells where that is cheaper
than a jump, sets a colour only when it changes, and returns nothing at all
for an unchanged frame. The bottom-right cell is never written. A resize
throws the buffer away and repaints after a clear.

Colours are given once, as RGB or as a named ink, and read three ways.
Truecolor sends them as they are. 256 colours takes the nearest of the colour
cube and the grey ramp. Sixteen colours uses a chosen reading for each named
ink (a nearest colour would turn half of them grey) and the nearest colour
otherwise, with only what every ANSI terminal takes: bold for a bright
foreground, the eight plain backgrounds, and a reset before every change so
bold never leaks.

A cell can hold two square pixels, upper and lower. UTF-8 and CP437 draw them
with a half block. Sixteen colours has only eight backgrounds, so the block is
turned over where that puts the brighter half in front. ASCII draws a pixel
cell as one character from a ramp of shades. A glyph placed over pixels takes
the cell, on the darkened mean of the two.

Measured on the plain flight: a full 80 by 24 frame is about 3.3 KB, looking
at another layer about 220 bytes, and a turn about 1.6 KB (most of it the map
moving under the balloon). A paint takes 10 ms at 80 by 24 and 34 ms at 168 by
46.

**The map.** A window on the world: each cell is 5 km wide and 10 km tall, so
half a cell is a square pixel of 5 km. Where the terminal has colour enough and
a half block (truecolor or 256 colours, UTF-8 or CP437) the map is relief in
pixels. Sixteen colours and plain ASCII get the character map, a reading made
for them. `DOOR_FW_PIXELS` overrides: `half` forces pixels, `shade` characters.

The relief is composed in order: terrain from the tiles, coloured by biome and
height and lit from the north-west; the veil over what nobody has charted;
cloud and squall, sampled every fourth pixel and blended between; and night,
which darkens it in three steps (a third at 19:30, two thirds at 20:00, full at
20:45), because every change of light re-sends the whole map. The lit colours
of a tile are worked out once and kept. A
moment's map is composed once and reused while the pilot looks at other
layers. Then, on top: the balloon, places, tracks and names. A marker takes
its cell on the darkened mean of the two pixels beneath. A name goes to the
right of its marker if it fits, else to the left, else shortened on the roomier
side down to four letters, else it is left out. It never runs off the map,
never covers a marker, the balloon or another name, and keeps a cell clear of
the next thing along its row.

**The window holds still.** It moves only when the balloon leaves its middle
half, and then by half a window, so the balloon is always in the middle half.
A window that followed the balloon would be a whole screen to send every turn.
Cloud is blended in steps for the same reason: drifting a little, it does not
re-send every cell under it.

Measured: a map of 140 by 38 cells composes in 19 ms from warm tiles (52 by
16 in 3 ms). A truecolor frame at 168 by 46 is 139 KB once, and a turn then
sends about 2.6 KB; at 80 by 24 it is 23 KB and 1.4 KB. In 256 colours a turn
is 0.9 to 1.4 KB. Looking at another layer is about 370 bytes at any size.

**256 colours.** The nearest palette entry by distance alone turns dark greens
and deep blues grey, because each axis of the colour cube rounds to the same
step. The match looks at the cube entries around a colour and counts a loss of
saturation against them, and it uses the grey ramp only for greys.

**Wind.** The map shows the wind of the layer the pilot is choosing, as far as
they know it: the truth where they have read it, the forecast otherwise. It is
sampled every 20 km over the window and blended between. One streak for every
fifty map cells is carried along it, a head and a fading tail of five pixels
that lighten the ground beneath; a streak that leaves the map, or grows old,
starts again somewhere else. The streaks are seeded from the window, so the
same window always starts with the same picture.

The wind moves at seven frames a second in short bursts: ten frames after a
turn and six after looking at another layer. Then it holds still, and a still
frame sends nothing. Any key ends a burst at once. With `DOOR_FW_MOTION=off` or
`DOOR_SCENES=0` nothing moves: each new wind is drawn as a new still pattern.
The character map shows wind as short lines along it (`-`, `|`, `/`, `\`),
which always hold still.

Frames are due at fixed moments counted from when the burst began. A frame
that is late skips the frames whose moments have passed; none is queued, and
the burst ends when it would have. And while the wind moves, about 44 KB is
the most that goes out in any one second (a frame is painted while the second's
total is under that, so it can end a frame over). When it is reached the wind
advances unpainted, except for a burst's last frame, which is the one that
stays. Everything sent counts, a full frame too: after a first draw, a resize,
Ctrl-L or a move of the window, the wind holds still for up to a second and
then shows where it has got to, because that frame is what the line is busy
with. The cap is there
because a pipe to the host takes a whole burst without blocking, so the time a
write took says nothing about how far behind the caller's line is.

**Scenes.** Splash, launch, landing, station vista, wonders. A scene plays only
after what it shows has been saved, never waits for a key, ends on any key and
consumes it. `DOOR_SPLASH=0` removes the splash. `DOOR_SCENES=0` removes the
rest.

**Budgets.** Targets: under 40 KB a second while the wind moves, under a
quarter of one core, and a one-hour session inside the shipped profile's CPU
limit with margin. Measured in process on the development host, in truecolor:

| | 80 by 24 | 132 by 40 | 168 by 46 |
| --- | --- | --- | --- |
| Streaks | 16 | 66 | 106 |
| A frame | 0.9 KB | 3.9 KB | 5.6 KB |
| While moving | 6 KB/s | 27 KB/s | 38 KB/s |
| CPU while moving | 4% of a core | 9% | 15% |
| A whole burst | 8 KB | 38 KB | 54 KB |

In 256 colours a frame is less than half as large. A burst lasts about a
second and a half, so a turn with its burst costs about 0.25 s of CPU at the
widest layout: two hundred turns an hour is under a minute, against a profile
limit of 900 s. These are the door's own figures. What each transport adds,
and how a real line behaves, is S26's to measure on a live node.

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
| `DOOR_FW_MOTION` | `auto` or `burst` (the wind moves in short bursts) or `off` (it holds still) |
| `DOOR_FW_PIXELS` | `auto` (relief in pixels where the terminal can show it), `half` (always pixels) or `shade` (always the character map) |
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
