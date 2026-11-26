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

**What the caller is told.** The screen always names the day being flown, in
its heading, and says when the next one opens and how many opened days still
wait: `Day 42 opens at midnight. Two more days wait.` A pilot who has flown
every opened day reads `Every day that has opened is flown. Day 42 opens at
midnight: come back then.`, has no flying keys, and may look at the day to
come: its map and its forecast are shown, and nothing can be done with them
until it opens. The time is given as `at midnight`, as `at 12:00`, or with its
date when it is more than a day off.

**As built (S11).** The calendar is four values in `meta`: `epoch_date`,
`epoch_day`, `tempo` and `high_day`, with the node's `timezone` beside them. A
world's calendar begins on the local date the world is first opened. The day
is `epoch_day` plus `tempo` for each local date since `epoch_date`, plus the
slot of the day the wall clock is in; `high_day` is written once for each day
that opens, by whichever caller sees it first. Reading the day takes no lock.
A session looks at the calendar when it opens, at each day's end, every 20
seconds while it idles, and at any key pressed while waiting, so a day that
opens is shown without the caller doing anything.

The logbook day is the day in the pilot's flight state: `logbook_day` is not a
separate field. A day is in progress once a turn of it has been flown, and
until the pilot stays the night, walks out or is picked up. Days lapse only
when no day is in progress, at the opening of a session and at each day's end;
the pilot is told how many went by.

The node's zone is looked up by its IANA name. A host without data for that
name uses its own zone and says so on standard error; a door started from a
shell uses the host's zone. `python fair_winds.py --tempo N` sets the tempo of
every world in the data directory from today on: today stays today, the
calendar is counted afresh from this date so that the slot the clock is in is
today, and the next day opens at the next slot, never at the moment of the
change. An empty zone name means UTC. It will move into the SysOp tool.

A pilot from before the calendar counted days of their own (a new day at a key
press). On their first call under the calendar a day number ahead of the
world's becomes today, once; nothing else of theirs changes.

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
the Gyre's bands coverage is capped (with the Gyre's own slice). The weather
is given the coverage by its caller; the game works it out from the towns and
the stations as they stand (section 7).

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
that forced climbs would cost, and it ends early where the flight would: at
ground above the
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
leads; and the keys. The tracks of the choices on offer are drawn on the map,
six turns ahead, the chosen one bright. S05 drew them as the layer's digit;
the owner's play-test found the digits out of place, and they are now a dot
for each turn, a heavier one on the hour, and an arrowhead where the track
ends, in the colour of the choice (sinking cool, holding amber, climbing
pink). Without UTF-8 the dots are `*` and the head one of `< > ^ v`: CP437
draws its own arrows where the control codes are, so they cannot be sent.

Wherever a name and its figure stand on one line (the stores, the heading, the
status line, the post office, the loft) the name is drawn in one ink and the
figure in another, brighter one. The help page has headings, and every key and
map sign in it is in a colour of its own.

**Keys.** W and S or the arrows step through sink, hold and climb. Enter or
Space flies the choice. J chooses a jettison, L a landing, P releases a pibal
at once. N stays the night. Keys typed before a turn's result is on screen are
dropped, so a held key flies one turn.

**The post.** S05 had post for the three nearest places not visited lately,
towns and stations alike, and landing at any of them delivered it. S12 put
contracts in its place (section 6); what S05 found still holds. One fixed
destination is the wrong game: with three layers the winds span less than half
the compass on most days, so a single town is often simply upwind. Measured
with a search that flies on the true winds: one of three places can be reached
in a day on about three days in four, in 6 to 42 turns; one fixed nearest town
on fewer than half. S12's contracts keep this shape: several destinations a
day, and the wind picks.

**Placeholders, each replaced by the slice named.** Walking out and the packet
boat happen at a key press (S16). The pilot's record holds the plain flight's own fields (the post
aboard, the last places delivered to, deliveries, days flown, distance, the
log) as one JSON text; S11 to S13 give them columns and tables of their own.

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

**As built (S12).** The tables are `PRICES`, `STATION_PRICE`, `CARGO_KINDS`,
`LATE_SHARE`, `DAMAGED_SHARE`, `LIFT`, `OFFER_REACH`, `TOWN_OFFERS`,
`STATION_OFFERS`, `START_POSTMARKS` and `RELIEF`.

| Cargo | Weight | Pays | Due | Forfeit if lost | Share of a town's offers |
| --- | --- | --- | --- | --- | --- |
| Mailbag | 1 | 12 + 0.20 a km | 3 days | 5 | 50 |
| Parcel | 3 | 20 + 0.40 a km | 4 days | 15 | 32 |
| Instruments | 2 | 28 + 0.55 a km | 4 days | 25 | 18 |

One more day is allowed for every 300 km. A balloon lifts 8. Cargo delivered
after its day pays half. Instruments shaken by a rough landing pay half. Cargo
lost (a hard landing loses half of it by keyed draws, a ditching all of it)
costs its forfeit, and never more than the pilot has.

| At a town | Postmarks |
| --- | --- |
| A unit of fuel | 1 |
| A sack of ballast | 4 |
| A pibal | 6 |
| A point of wear mended | 1 |

A weather station asks half as much again, rounded up. Nothing is filled or
mended for nothing: not by a delivery, not by a night.

A town's post office offers three to five contracts on a world day, a station
two mailbags. Each is for a different one of the six nearest places, so the
day's wind has several to choose among. The offers are keyed draws on the
world, the place and the day: the same for every caller, and each pilot may
carry each once. A contract taken can be put back at the same counter, unless
the cargo has been damaged since. Cargo carried into a town on foot is
delivered there, late or not. A new
pilot has 40 postmarks and a first round already aboard: three mailbags for
the three nearest places, due in five days. Frontier freight comes with the
masts (S15), and the licence does not yet shape the offers (S13).

**The relief run.** A pilot at a town or station who has less than 10 fuel, or
a balloon too worn to fly, and cannot pay to put that right, may ask once a
day: the postmaster brings the fuel to 25, the wear down to 70 and the ballast
to one sack, and gives them a mailbag for the nearest place that pays 8. A
balloon down in the wild that cannot launch (under 2 fuel, or too worn) is
stranded, and its pilot walks out to a town. So there is always a way to fly
on. A scripted pilot flies ten world days in each of three worlds in the
tests and must be able to launch, or be walking out, every morning.

**The post office screen.** O opens it on the ground at a town or station:
the day's offers with distance, bearing, weight, pay and day due; what is
aboard; and the stores. Digits take a contract or put it back; F buys ten
fuel, A as much as fits and can be paid for, S a sack, P a pibal, M mends as
much as the postmarks pay for, R asks for the relief run when it is due. From
30 rows up, where the terminal shows pixels, the place itself is painted over
the counter (the station vista of S09). The map marks with `#` the places
there is cargo aboard for; with nothing aboard it names the nearest offices.

**The last stretch.** From the owner's play-test: whole flights being in the
wind's hands is the game, but being 16 km from a destination and carried past
it with no layer going the right way was only frustrating. Four moves answer
that, and none of them steers a flight.

| Move | Key | Where | What it does | What it costs |
| --- | --- | --- | --- | --- |
| Trail rope | A / D | aloft, in the lowest layer | bears 30 degrees left or right of the wind for the turn, at three quarters of its speed | a hold's fuel and 1 wear |
| Mail drop | M | aloft, no more than one layer above the lowest, within 30 km of where a mailbag is bound | delivers the mailbags for that place without landing; it takes no turn | pays 60% |
| Send on by road | W at the post office | at any town or station | hands over cargo bound elsewhere | pays half its payout times the part of the way it was carried: nothing at the counter it came from |
| Wait | H | on the ground | an hour passes and the wind changes | nothing but the hour |

A landing pays in full, a drop 60%, the road at most 50%: nearly making it is
worth something, and making it is worth more. Parcels and instruments are not
dropped. A mailbag is dropped only once it has come at least half its way, so
one for the next village is flown there. The road's "part of the way" is
measured from the office the cargo came from to the office handing it on, not
from where the balloon stood, and an office never sends on what it gave out. A
day only waited out is not a flying day. A drop earns the mailbag's licence
points; the road earns postmarks only. The tracks on the map show where each
side of the rope would lead. Waiting is a small move: nobody should be asked
to sit out more than an hour or so now and then, and nothing in the game needs
it.

Measured with the scripted pilot over eight worlds and ten days each
(deliveries for each flying day): 0.36 before, 0.45 with the rope, 0.82 with
the rope and the mail drop.

**Not yet tuned.** The amounts are a first table. Measured with a scripted
pilot that steers by the forecast track and takes the nearest contracts: about
one delivery in four flying days, and it lives on the relief run. A person
reads the wind better than that script, but how much better, and whether a
casual caller gets by, is for the play-test after S13.

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

**As built (S13).** The tables are `GRADES`, `POINTS`, `CARGO_GRADE`,
`UPGRADES`, `PATTERNS` and `ENVELOPE_COLOURS`.

| Grade | Points | Entrusted with | Sold |
| --- | --- | --- | --- |
| Fledgling | 0 | mailbags | |
| Aeronaut | 60 | parcels | Envelope II, Burner II, Tanks II |
| Pilot | 160 | instruments | Instruments II, Wireless, Night gear |
| Navigator | 360 | | Envelope III, Burner III, Tanks III |
| Wind-reader | 700 | | Instruments III, Oxygen |
| Master | 1200 | | |
| Commodore of the Post | 2000 | | |

Points: 8 for a mailbag delivered, 14 for a parcel, 20 for instruments, 3 for
a clean landing that ends a flight at least 25 km from where it launched, and
one for every ten km flown. A flight already in the air when this rule
arrived has no launch point on record, and its landing earns no landing
points. Charting will add to them
(S14). A town's first offer of the day is always a mailbag, so there is
something for a pilot of any grade; an offer above the pilot's grade is shown
and refused with the grade it wants.

| Gear | I | II | III |
| --- | --- | --- | --- |
| Envelope | lifts 8, layer 3 | 150: lifts 11, layer 4 | 450: lifts 14, layer 5 |
| Burner | a climb costs 3 | 120: costs 2 | 380: costs 1 |
| Tanks | 60 fuel | 100: 90 fuel | 320: 130 fuel |
| Instruments | tracks 6 turns, reads its own layer | 140: 10 turns, and the layers next to it | 420: 16 turns, every layer |

| Gear | Price | Effect |
| --- | --- | --- |
| Wireless | 200 | Forecasts sharpened by places up to 300 km off, not 150 |
| Night gear | 180 | Night landings a class better; no night fuel penalty |
| Oxygen | 300, and Envelope III | Layer 6 |

"Preview reach" in the table above this section is built as what the
instruments read: a layer read is known true, and its track starts from the
truth; any other layer is a forecast. The camera is sold from S21, with the
wonders. The engine waits for its own slice and is not sold yet, because it
would do nothing.

The gear a pilot owns is in their record as a tier for each track. The
figures the engine flies by (ceiling, climb cost, night gear, oxygen, layers
read) are set from it whenever it changes and whenever the pilot is loaded, so
a change to the table reaches every balloon.

**The rigging loft** is in towns: G from the ground or from the post office.
It shows the licence, each track with what is fitted and what the next tier
would cost and do, or why it cannot be bought (the grade it is sold to, other
gear it needs, not enough postmarks), and the envelope: P steps through the
patterns, Z, X, C and V through the eight colours for each of the four places.
From 100 columns the balloon is drawn as designed. The design is in the launch
and landing scenes. Every colour has a different reading in sixteen colours,
and the design is always named in words.

**Other pilots on the map.** Each other pilot of the world is drawn where
their last committed turn left them, as `Q` in the first colour of their
envelope, at most 24 of them: the latest to have flown. A session looks again every 20 seconds and at
each turn. Names, and who is flying today, come with the shared world (S14).

**Not yet tuned.** Thresholds and prices are a first table, as in S12.

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

**As built (S14).** From the owner's play-test: when the wind will not take a
pilot where they meant to go, going somewhere else should be worth something.
The chart is the first answer: a flight blown off course is a surveying flight.

- The ground within 30 km of every town and station is known from the start,
  about a twentieth of the land. Everything else is uncharted until somebody
  has seen it from the basket.
- Uncharted ground is drawn as a dim, grey sketch of itself (in characters:
  its glyphs, dimmed), not as a blank. The land's shape shows, and what has
  been flown over stands out from it in colour. The owner called the map
  beautiful; a black veil over nine tenths of it would have thrown that away.
- Each turn that ends aloft charts the cells in sight from the layer it was
  flown in, from points along the turn no further apart than that sight, by
  the weather at its start. Every cell in sight is charted, water too, so lakes and islands
  can be found; only land pays.
- First over a cell is settled where it is written: the cell is inserted if
  nobody has it, inside the turn's own transaction. Two pilots over the same
  ground are each credited with what was theirs. A turn that cannot be written
  earns nothing and charts nothing: what it would have paid is taken back, so
  the ground is still there for whoever is written down as first.
- Pay: a postmark for every 2 cells of land a pilot is first to chart, a
  licence point for every 5. It is said once, when the flight is down.
- A feature is found by the turn that brings it to 60 percent charted, counting
  what was known from the start; at least one of its cells has to be charted
  by somebody. Finding one pays 25 postmarks and 10 points.
  The finder is shown three names and picks with 1, 2 or 3; any other key
  takes the first, and the key is used up. Until chosen, and if the finder
  leaves, the first name stands. Names are a word from the generator's list
  and a word for the kind of feature (`FEATURE_WORDS`), drawn by key from the
  world's seed: the same three for a feature every time, and no caller's text.
- Found features are named on the map where they lie. `C` opens the chart of
  the whole continent: what is charted in colour, the rest sketched, towns,
  the pilot, other pilots, the share of the land charted, how many features
  are found, and who has been first over the most cells.
- A session takes in what others have charted every 20 seconds and at each of
  its own turns. The map is composed again only when the chart has changed.
- The records gain two tables, `charted` and `features`, and the schema is
  version 2. A world from before is brought forward when it is next opened;
  flights made before the chart charted nothing.
  A build from before this one refuses a world at version 2.
- Measured with the scripted pilot: about 11 postmarks a flying day from
  charting and about one feature found a day. One pilot charted 7 percent of
  the land in seven flying days.

**Stations.** A pilot who lands in the wild with a station kit may plant a
station, at least 60 km from any other. A station has stock from 0 to 14. Stock
falls by one each world day. Any pilot landing there may resupply it for a
reward. At zero it lapses: it stays on the chart, gives no coverage, and can be
restored. Forecast coverage is section 4.

**Masts.** A station becomes a mooring mast when pilots have delivered the
required freight from towns. Contributions are recorded per pilot. A mast gives
full coverage, a lit landing at night and in fog, fuel, and a post office with
contracts. Masts do not lapse.

**As built (S15).** The tables are `STATION_APART_KM`, `STOCK_FULL`,
`STOCK_SHARP`, `KIT_PRICE`, `KIT_WEIGHT`, `PLANT_POINTS`, `RESUPPLY_PAY`,
`RESUPPLY_POINTS`, `CRATE_PRICE`, `CRATE_WEIGHT`, `CRATE_PAY`, `CRATE_POINTS`
and `MAST_CRATES`.

This is the second answer to the owner's question of what there is to do
where the wind has put a pilot down: plant something there.

| | Postmarks | Weight | Points |
| --- | --- | --- | --- |
| A station kit, at a town | costs 50 | 3 | 25 for planting it |
| A day of stock put back | pays 2 | | 1 for every two |
| A crate of mast freight, at a town | costs 10 | 3 | |
| A crate unloaded at a station | pays 25 | | 10 |

- **Three kinds of station.** The world's first three are the Post's own:
  staffed, always in full report, with a post office and stores, as before.
  A planted station is a hut of instruments with nobody in it. A mast is a
  planted station the Post has taken over.
- **A kit** is bought at a town's post office (`K`; `K` again hands it back
  for what it cost). A pilot carries one at most. It counts against the lift.
- **Planting.** `K` on the ground in the wild, at least 60 km from every town
  and every station. The station is named from the generator's word lists by
  a keyed draw on the spot: no caller's text. Two pilots planting within 60
  km of each other at the same moment plant one; the second keeps the kit.
- **Stock.** 14 days when planted or restocked, one less each world day (the
  world's calendar, not the pilot's logbook). Nothing is written as the days
  pass: the records hold the stock on the day it was last put right. With 7
  days or more the station reports in full; below that in proportion; at
  none it is silent. It stays on the map, drawn dim, and can be restored.
- **Restocking.** Landing at a planted station (within 12.5 km) sees to it:
  the stock is full again and each day put back pays. It is settled where it
  is written, so two pilots landing together are not both paid for the same
  days.
- **Coverage.** The best of every town, mast and station within reach: 1 at
  the place, falling to 0 at 150 km (300 with the wireless), times the
  station's report. Every pilot's forecast is sharper around a station
  another pilot planted; a session takes in new stations every 20 seconds.
- **Mast freight.** Crates are bought at a town (`T`; `Y` hands one back).
  They are bound for no station in particular: whichever planted station
  the pilot lands at takes as many as it still wants and pays for each. The
  wind picks the station, as it picks the town.
- **Raising.** Four crates raise a mast, from however many pilots; who gave
  how many is recorded (`contributions`). The landing that brings the fourth
  raises it, once, inside its own write. A station never takes more crates
  than it wants; the rest stay aboard.
- **A mast** is a place of the Post from then on: full report for good, a lit
  field (night and fog do not worsen a landing there), fuel, sacks, pibals
  and mending at the station's price, a post office with two mailbags a day,
  and a destination for the post of the towns around it. Its post office
  names the pilots whose freight raised it. Being out of fuel there is not a
  stranding. A planted station gives none of that: a pilot down there with
  no fuel is stranded, as anywhere in the wild.
- **Lost at sea.** A ditching loses the kit and the crates with the post.
- The map draws a station as `+` (dim when silent) and a mast as `Y`; the
  chart of the continent shows them too.
- The records gain two tables, `stations` and `contributions`, and the schema
  is version 3. A world from before is brought forward when it is next
  opened. A build from before this one refuses a world at version 3, so there
  is no going back to an older build without a copy from before:
  `--backup FOLDER` makes one, and is worth running before a new build is
  installed.
- A visitor, or a caller whose records cannot be kept, plants and raises for
  that visit only.

**Since built.** Nothing may be planted in the Gyre's inner band and
coverage is capped in its bands (S23). Guestbooks came with S16.

**Not yet tuned.** Whether fourteen days is the right life for a station on
a node with one caller, and four crates the right price of a mast, is for
play.

**Rescue.** A stranded pilot is shown on every chart with what they need. Any
pilot may fly it in. Both are rewarded, once. The stranded pilot resumes from
that spot with the delivered fuel.

**Guestbooks.** Each station and mast has one. A pilot may leave one line of up
to 60 characters per station per world day. Text is stripped of control
characters and escape sequences and shown as plain text. `DOOR_FW_GUESTBOOK`
may be `on` (default), `canned` (phrases from a list only) or `off`. The SysOp
tool lists and removes lines.

**As built (S16).** The tables are `RESCUE_FUEL`, `RESCUE_PAY`,
`RESCUE_POINTS`, `GUESTBOOK_LIMIT`, `GUESTBOOK_SHOWN` and `CANNED`.

- **Stranded** is down in the wild and unable to launch: under 2 fuel, or too
  worn to fly. A planted station is the wild; a town, a first station and a
  mast are not. The moment a pilot is stranded their call for help is written
  (`calls`): where they are, and what they want. It is fuel up to 15, or
  patches, or both.
- **On every map.** Each other pilot sees the stranded pilot as `!` with
  their name and what they are waiting for, on the map and on the chart of
  the continent. A session looks every 20 seconds.
- **Walking out** is `W` and nothing else (Enter is the key that flies, and
  must not cost two days by habit). It costs two world days through the
  calendar: the logbook moves on two days, and if they have not opened the
  pilot waits for them. The call is taken down.
- **Waiting** is doing nothing. The pilot may leave the door; the call stays.
- **Rescue.** A pilot who lands within 12.5 km of a stranded pilot, with the
  fuel they want and 2 to launch again, reaches them: the fuel comes out of
  the rescuer's tank, patches cost nothing. The rescuer is given 40
  postmarks and 20 licence points by the Post. The stranded pilot gets the
  fuel (and, if they were too worn to fly, wear brought down to 70), keeps
  the spot and the days walking would have cost, and flies on. They find out
  the next time their session looks, or at their next call.
- **Once.** A call is answered inside the rescuer's own write, by the first
  to arrive; a second is told somebody was there before them and keeps their
  fuel. What was brought is taken inside the stranded pilot's own write,
  which removes the call. Walking out takes the call down in the same write
  as the walk: if help came first, even in that instant, the fuel is taken
  and nobody walks.
- **The visitors' books.** `B` on the ground at a station: one of the first
  three, a planted one, or a mast. Towns keep none. The book shows its
  latest 12 lines, newest first, with the day and the pilot. `W` writes a
  line: every printable key is part of it, `Q` and `?` included; Backspace,
  Enter to sign, Esc to leave it. One line for each pilot, station and world
  day.
- **What may be in a line.** Printable characters only, runs of space made
  one, 60 characters at most: so no control characters and no escape
  sequences. Accented letters stay; marks that stack on a letter and
  characters two cells wide are dropped, so a line cannot push the screen's
  edge about. It is cleaned when it is written and again when it is shown,
  and it is shown as plain text, never as markup.
- **`DOOR_FW_GUESTBOOK`.** `on` (the default, and what any other value
  means): as above. `canned`: nothing is typed; the pilot signs with one of
  eight lines from `CANNED`, by its number. `off`: there are no books, no
  key for them, and nothing is read from them.
- **The SysOp.** `python fair_winds.py --guestbook` lists every line of every
  world in the data directory, each beginning with its name
  (`world:number`); `--guestbook-remove world:number` removes one. Both are
  safe while callers play. The pilot whose line was removed may sign that
  book again the same day.
- The records gain `calls` and `guestbook`, and the schema is version 4.
- A visitor's call and lines are that visit's only.

**Not yet built here.** The packet boat is still a key press. Other pilots
are one mark, `Q`, wherever they were last: moored, camped and aloft are not
told apart yet.

**Not yet tuned.** Landing within 12.5 km of a stranded pilot is as hard as
landing at a town. Whether rescue should reach further (a drop from the air,
as mail has) is for play.

**Other pilots.** The map and chart show where other pilots are moored, camped,
stranded, or were last seen aloft, with their envelope colours. Positions are
read from the world when a screen is drawn. Nobody needs to be online together.

**The chart, as built (S17).** `C` from the flying screen, in the air or on
the ground. The tables are `ATLAS_ZOOMS`, `ISOBAR_HPA`, `ISOBAR_DEEP`,
`ATLAS_PANEL`, `ATLAS_PANEL_FROM`, `ROUTE_KEPT` and `FIND_LIMIT`.

- **The ground.** Charted land in colour, the rest sketched, as on the map.
  The whole continent is drawn from the chart grid (10 km cells); zoomed in,
  from the map's own shaded relief, a sample to a pixel.
- **Zoom.** `Z` steps through the whole continent, a half of it across, a
  quarter of it across, and back. The first step in is centred on the pilot.
  `-` steps back out.
- **Pan.** The arrows move the window a third of itself, never past the edge
  of the world. On the whole continent there is nowhere to go, and the
  chart says so.
- **Isobars.** `I`. A line wherever the pressure of the true weather at the
  pilot's day and hour passes a multiple of 4 hPa between two neighbouring
  pixels (below 992 hPa, of 20: the Gyre's core is too steep for more). The
  field is `WeatherFrame.pressure`, from the same systems that turn the
  wind. Each low and high is marked `L` or `H` with its pressure. Where the
  map is drawn in characters there is no room for isobars among the glyphs,
  so they start off and `I` asks for them.
- **Names.** `N`. Towns, stations and found features are named beside their
  marks, a space apart and never run together; where not all fit, the places
  the pilot has post for come first. The Gyre is always named.
- **Find.** `/`, a name or part of one, Enter. Towns, stations, found
  features, stranded pilots, winds in the almanac and the Gyre can be found;
  a name that begins with what was typed (a leading "The" aside) comes
  before one that only holds it, and of several the shortest. The place is
  brought to the middle, one step closer if the chart showed the whole
  continent, and marked `*` until the chart is closed. While a name is typed
  every printable key is part of it. Esc leaves the name, not the chart.
- **Sites.** `o` town, `+` station (dim when silent), `Y` mast, `#` where
  the pilot has post for, `Q` another pilot, `!` a stranded pilot with what
  they want, `@` the pilot.
- **The route so far.** Where each of the pilot's last 40 flights, walks and
  boat rides ended, joined by dots, and on to where the balloon is now. The
  dots are drawn last and only where nothing else is: they hide no name. It
  is kept in the pilot's record (`route`); a pilot from before has none
  until they next land.
- **The panel**, from 120 columns: how much of the land is charted, features
  named, stations and masts, who has charted the most, who is stranded and
  how far off, and a key to the marks. Below 120 columns the two lines under
  the map say the first and the fourth of those.
- **Leaving.** Enter, Esc, Space, B or C. Other keys do nothing.
- **Cost.** The ground and isobars are composed once for a window and kept
  until what they show changes. Measured on the development host from warm
  tiles: 4 ms at 80 x 24, 9 ms at 132 x 40, 16 ms at 168 x 46 (the plan
  allows 60).

**Not yet built here.** The season and the gazette have no panel because
there is neither yet. A camp is not told from any other pilot on the ground.
The winds are not drawn on the chart.

**The almanac.** A local wind is entered when one pilot has flown through it
while it was blowing on three different world days. It gets a name from the
generator and that pilot's credit. An entered wind is drawn on every pilot's
forecast when its conditions hold.

**As built (S18).** The tables are `WIND_COUNT`, `WIND_APART_KM`,
`WIND_REACH_KM`, `WIND_LAYERS`, `WIND_DAYS`, `PASS_KMH`, `SEA_BREEZE_AT`,
`DOWNSLOPE_AT`, `ALMANAC_PAY`, `ALMANAC_POINTS` and `WIND_KINDS`.

- **Where they are.** Each world has 12 to 20 local winds, drawn from its
  seed. They are picked by turns from the strongest of three kinds of place,
  each on ground low enough for the lowest layer and at least 110 km from
  every other: gaps between high ground (a pass wind), coasts (a sea
  breeze), steep ground (a downslope wind). They are not stored; the land
  and the seed say where they are.

| Kind | Blows | Named |
| --- | --- | --- |
| Pass wind | whenever the lowest layer runs at 18 km/h or more through the gap | Funnel, Rush, Gate |
| Sea breeze | 13:00 to 17:00, every day | Breeze, Doctor, Onshore |
| Downslope wind | from before dawn until about 07:45, every day | Fall, Drain, Bora |

- **These are the weather's own winds.** The sea breeze, the downslope wind
  and the faster air in a gap are in the wind a balloon flies on (section
  4). The almanac adds nothing to the weather: it names where they come
  back.
- **Flying one.** A turn that ends aloft in layer 2 or below, within 30 km
  of a local wind's middle, counts for that pilot and that flying day if the
  wind was blowing when the turn began: a turn flies on the wind it sets
  out on (section 5), so that is the wind it was in. A day counts once. The first time on a day the log says what
  kind of wind it is and how many of three days it has been flown.
- **Entering one.** The third such day enters it, in the write of that turn:
  once, under the pilot whose third day it was, however many arrive
  together. It pays 30 postmarks and 15 licence points. It has no name
  before: the name is the generator's, kept for that moment.
- **For everyone.** From then on the wind is marked and named on every
  pilot's map and chart at the hours it blows, and can be found on the
  chart by name.
- **The almanac** is `V`, from the air or the ground. It lists each wind
  entered with its kind, where it is, when it blows, who entered it and on
  what day, and whether it blows now; then the winds this pilot is part way
  to entering, unnamed; then how many are still to be found and how. Any
  key closes it.
- The records gain `sightings` and `almanac`, and the schema is version 5.

**Not yet tuned.** A downslope wind blows for under two hours of the flying
day and a pilot has to be there for it on three days. Measured over three
worlds, every wind blows on at least four days in twenty-eight.

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

**As built (S19).** The tables are `DRIFT_START_TURN`, `DRIFT_TURNS`,
`DRIFT_BALLOON`, `DRIFT_SCORE`, `DRIFT_PAY_PER`, `DRIFT_TOP`, `DRIFT_MARK_KM`
and `DRIFT_SCOUT`; the rules a caller reads are `DRIFT_HELP`.

- **Where.** `T` from the pilot's own screen, whenever their own balloon is
  not in the air: on the ground, at rest, or waiting for a day to open.
- **The course** of a world day is a town to start from and a mark to come
  down by. The town is a keyed draw on the day. A plain flight (launch, five
  holds, a climb, five holds) is flown from it on that day's true wind, and
  the mark is set down on land 8 to 20 km from where that flight ends. So
  the mark can always be reached, and not by doing nothing clever. Nothing
  is stored: the same day gives the same course in every process.
- **The standard balloon.** 40 fuel, 3 sacks, 1 pibal, the first envelope,
  burner and instruments, whatever the pilot owns. Hazards are drawn on the
  Drift's own key, so they are the same for everyone too.
- **The clock.** It starts at 09:00 in the weather of the world day it
  belongs to and is over 24 turns later: a balloon still up comes down
  where it is.
- **The score.** 1000, less 8 for each km between the landing and the mark,
  less 4 for each turn, plus 2 for each unit of fuel left; never below
  nothing; water scores nothing. Level scores are placed by the nearer
  landing, then the fewer turns, then whoever finished first. The Drift's
  screen says this in short and `?` there shows it in full.
- **One scored attempt.** Enter begins it, and that moment is written
  (`drifts`): one row for a pilot and a day. Every turn is written to that
  row. Leaving the Drift (`X`), the door, or a dropped call leaves it part
  way; coming back the same day takes it up exactly there. There is no way
  to begin it again. A turn that cannot be written is not flown: the Drift
  is parked where the records have it. When it ends it is scored once, in the same write that
  pays the pilot a postmark for every 25 points. An attempt left unfinished
  when the day is out scores nothing.
- **Practice** (`P`) is the same course as often as wanted, in memory only:
  it writes nothing and is never on the table. `X` leaves a Drift; a
  practice, which would be lost, asks for it twice.
- **The table** is the day's top ten.
- **What it leaves alone.** While a Drift is flown the pilot's own flight is
  set aside, and nothing of it is written: no turn, no chart, no delivery,
  no route, no day of the calendar. The post office, the loft, books, the
  mail drop, waiting and the night are not there.
- The records gain `drifts`, and the schema is version 6.

**Trials.** A tutorial flight and twelve challenge flights with fixed weather
and bronze, silver and gold thresholds: threading a pass, a night run on the
jet, a squall line, a flight with no ballast, a long drift on one tank. Attempts
are unlimited. The tutorial teaches preview, commit, ballast, the pibal and
landing by having the caller do each once.

**As built (S20).** The tables are `TRIALS`, `TUTORIAL`, `TUTOR_STEPS`,
`TRIAL_BALLOON`, `TRIAL_KM`, `TRIAL_PAY`, `TRIAL_SPARE_TURNS`, `TRIAL_DAY`
and `TRIAL_FAR_KM`; the rules a caller reads are `TRIAL_HELP`.

- **Where.** `R` from the pilot's own screen, whenever their own balloon is
  not in the air. `W` and `S` choose, Enter flies.
- **A trial** is a balloon, an hour to start at, and a flight written down in
  the table. Its weather is a fixed day from day 500 on, far from the
  calendar's, the same every time it is flown. The flight is flown from a
  town; where it lands is the mark. Towns and days are tried in a fixed
  order until the flight can be flown exactly as written and comes down on
  land at least 10 km from its start. So every trial has a flight to gold,
  in every world, and the tests fly each of them in five worlds.
- **Medals.** Down on land within 6 km of the mark is gold, within 15 km
  silver, within 30 km bronze; on a short trial, no further than 15, 40 and
  75 percent of the way from the start to the mark (gold never nearer than
  3 km), so that coming down where the flight began wins nothing. The log
  says the three distances when a trial begins. Water wins nothing. A trial
  that could not be laid out in some world (no day in 120 lets its flight be
  flown) has no medal there. A trial allows eight
  turns more than its own flight took; then the balloon comes down where it
  is. The best medal for each trial is kept in the pilot's record. A medal
  is worth 5, 12 or 25 postmarks, paid the first time, and a better one pays
  the difference.

| Trial | Balloon | Starts | The flight it is laid by |
| --- | --- | --- | --- |
| First Hop | standard | 09:00 | four holds |
| Up and Over | standard | 09:00 | up a layer, four holds, down, a hold |
| Top Floor | standard | 10:00 | up two layers, five holds, down two |
| The Long Tank | 6 fuel | 11:00 | twelve holds in the midday sun |
| No Ballast | no sacks | 09:00 | up a layer, six holds, down |
| Sack Race | 8 fuel | 09:00 | two sacks over the side, three holds, down two |
| Rope Trick | standard | 09:00 | the rope left, left, a hold, right, left |
| Dawn Run | standard | 06:00 | eight holds |
| Dusk Dash | standard | 17:00 | up a layer, six holds, down, a hold |
| Night Mail | night gear | 20:30 | six holds |
| The High Road | 60 fuel, layer 5 | 09:00 | up four layers, six holds, down four |
| Long Haul | standard | 09:00 | six holds, up a layer, eight holds, down, three holds |

  The standard balloon has 40 fuel, 3 sacks, 2 pibals and three layers.
- **The first flight** is the tutorial: the same machinery, with six lessons
  said in the log one at a time and passed by doing what each asks: look at
  another layer (the preview), launch (the commit), send up a pibal, drop a
  sack, sink, land. A lesson is passed by doing what it asks, in whatever
  order: the log asks for the first one not yet done. It allows twenty turns
  more than its own flight. Landing with every lesson done marks the pilot
  as taught. Scripted, it is eleven keys. Until then the ground's key line
  offers `R`, and a new pilot's first log line names it.
- **What they leave alone.** As the Drift: the pilot's own flight is set
  aside, and nothing of a trial is written but a better medal, in the
  pilot's own record, with no line in the day's journal: a trial is not a
  turn of theirs. A medal that cannot be written does not stand. No day, no
  chart, no table.
- No schema change: medals and the first flight are in the pilot's record.

**Not yet tuned.** The medal distances and the trials themselves are a first
table: they were laid out by what the engine allows, not by how they feel.
The plan's "squall line" and "threading a pass" are put off, not dropped: a
flight through a squall is not the same flight twice, and a pass needs a
mark set between two features, which this table of flights cannot say.

## 9. Wonders, seasons and the Gyre

**Wonders.** Forty. Each is a place, or a condition, or both: an aurora north
of a pass on a clear night above 3 km; a salt flat at noon; a waterfall from
layer 1; a cloud sea at dawn from above the deck; lightning seen from a safe
distance. A wonder is captured with a free action when its conditions hold. The
first pilot is credited. Each capture shows a full-screen scene. Scenes come
from about eight parametric families, not forty drawings. The album shows what
has been seen, by whom first, and a hint for each wonder not yet seen; hints
sharpen as the node's chart grows.

**As built (S21).** The tables are `WONDER_KINDS`, `WONDER_COUNT`,
`WONDER_APART_KM`, `WONDER_PAY`, `WONDER_POINTS`, `WONDER_HINTS` and
`WONDER_SAMPLE`.

| Kind | In a world | Seen within | From | When |
| --- | --- | --- | --- | --- |
| Lights (aurora) | 5, in the northern third | 40 km | layer 2 or above | after 20:30, cloud under 0.3 there |
| Mirror (salt flat) | 5, on dry plain | 20 km | layer 3 or below | 11:00 to 13:30, cloud under 0.3 |
| Falls | 5, on steep low ground | 12 km | the lowest layer | 07:00 to 19:00 |
| Cloud Sea | 5, over hills | 40 km | above the cloud deck | 06:00 to 09:00, cloud 0.6 or more |
| Storm Light (lightning) | 4, on low land | 40 km | any layer | after 18:00, a squall 25 to 65 km off and none overhead |
| Rose (alpenglow) | 5, at high peaks | 40 km | any layer | 19:00 to 20:30, cloud under 0.4 |
| Fire (sea lights) | 5, in shallow water by a coast | 15 km | the lowest layer | after 20:30 |
| Mist | 5, on low grass or forest | 15 km | layer 2 or below | 06:00 to 08:15, fog lying there |
| The Eye of the Gyre | 1, at its centre | 30 km | any layer | always |

- **Forty.** Each kind's ground is gone through in an order drawn from the
  seed. A place is taken if it is 150 km from the others of its kind (less
  where the land does not allow it) and the sight does show there on at
  least one of thirty sample days: no wonder is one that never comes. They
  are not stored; the records know a wonder by its number, and a test holds
  one world's wonders to where they were. Each is named from the word lists.
- **Taking one.** `F`, when the balloon is aloft where and when a wonder
  shows and it is not in the pilot's album. It takes no turn. The log says
  so when one comes into view, and `[F] Wonder` heads the key line.
- **Credit.** The first pilot on the node to take a wonder is named beside
  it, once, whoever arrives together: 40 postmarks and 20 points. Any other
  pilot taking it for their own album has 12 and 6.
- **The camera** (rigging loft, 160 postmarks, from Pilot) sees every wonder
  from half as far again.
- **The picture.** Taking a wonder plays a scene: the sky and land of the
  hour, the wonder painted over it by its kind's own hand, the pilot's
  balloon small to one side, the wonder's name and who saw it first. Nine
  pictures (the Eye is the Cloud Sea's with the storm's wall on either
  hand). Lights shimmer, water falls, lightning strikes on some frames, the
  sea's light drifts. It stays about four seconds; any key ends it.
- **The album** is `U`. Three kinds to a page, `W` and `S` turn pages. Each
  kind has a heading, and under it its wonders. A wonder in the pilot's
  album is named, with where it is and who saw it first. One another pilot
  has seen is named, with where and by whom. One nobody has seen has a
  hint: what kind of sight; from a tenth of the land charted also where.
  How a kind is seen (its hours, weather and layers) stands in its heading
  once anybody has seen one of that kind, or three tenths of the land are
  charted, or one of its places has been charted.
- The records gain `wonders` and `seen`, and the schema is version 7.

**What "reachable" was tested as.** For every wonder of three worlds there is
a turn of a flying day within a year at which it shows, and in one world a
balloon stood there at that turn takes each of the forty through the game's
own keys. A pilot's day begins wherever they came down the day before, so
the dawn kinds are reached by landing near one in the evening: the tests
launch from the ground at a mist site at 06:00 and take it on the first
turn. No route from a town to each wonder was searched for.

**Not yet built here.** A wonder cannot be looked at again from the album.
In 256 colours the night pictures are coarse.

**Seasons.** 28 world days. Each has a weather regime (a turn of the prevailing
flow and a pulse of the Gyre), a node goal generated to be reachable by one
active pilot and faster with more (raise a chain of masts through a pass, chart
a region, supply a frontier town), and a race in its last seven days (the
longest straight-line flight in one flying day). At the close, medals go to at
most three pilots. Nothing a pilot owns is reset. The map is never reset.

**Seasons, as built (S22).** The tables are `SEASON_DAYS`, `RACE_DAYS`,
`SEASON_GRACE`, `SEASON_GOALS`, `GOAL_PAY`, `RACE_PAY` and `RACE_PLACES`.

- **The season** of a world day is `(day - 1) // 28 + 1`. Its screen is `Y`.
- **The regime** is the weather's own (section 4) and has been since S04:
  for each season a lean of the whole column, a swing and a speed for each
  layer within a fifth of its base, in about a third of seasons the two
  lowest layers running the other way as trades, a pulse of the Gyre from
  0.85 to 1.15, and in about three in ten a cold season with ice from layer
  4. The first day of a season eases from the old regime to the new. The
  season's screen says it in a line.
- **The goal** is one of five, set by the first session to look at the
  season and the same for everyone after. The kinds are gone through in an
  order drawn for the season; one the world has no more room for is passed
  over. Progress is counted from what the records already hold for the days
  of the season, so nothing new is written as pilots play.

| Goal | To do | Counted from | One pilot |
| --- | --- | --- | --- |
| Chart | 200 cells nobody has seen | `charted`, by day | about ten flying days |
| Plant | 2 weather stations | `stations`, by day planted | two kits |
| Mast | 1 mooring mast | `stations`, by day raised | four crates, two flights |
| Wonders | be first to 4 | `wonders`, by day | four sights |
| Winds | enter 2 in the almanac | `almanac`, by day | six flights |

  Each is within what one active pilot does in a season; what several do is
  added together, so more pilots are sooner there.
- **The race** is the season's last seven days. A flight that ends on one of
  them counts by the straight line from where it launched to where it came
  down; each pilot's longest stands (`race`).
- **The close.** A season is settled once it is three days past: by the
  first session to look, in one write, once, however long the node was
  quiet in between. Whether the goal was met is fixed then, and so is who
  had a hand in it (`season_hands`). A pilot whose logbook still stands on a
  day of that season (stranded, or part way through a day) may add to its
  count afterwards; that changes nothing that is owed. A flight begun in
  the race and ended after the season was settled is told it came too
  late. The race's first
  three take gold, silver and bronze (`season_medals`): the longest flight;
  of two alike, the one flown on the earlier day. No more than three pilots
  have a medal of a season. A race that is closed takes no more flights.
- **Dues.** A closed season owes a pilot 60 postmarks if its goal was met
  and they had a hand in it, and 120, 70 or 40 for a medal. It is asked for
  once for each pilot and season (`season_claims`), in the same write that
  pays, at their next call or look, for every closed season however long
  ago it was.
- **Past seasons** are on the season's screen: the last three, with their
  goal, whether it was met, and their medals. One that is over and not yet
  settled is shown as being settled.
- **A node that had seasons before this build** has each of them settled the
  first time it is opened: a goal is drawn for each and counted from the
  records of its days, and pilots who had a hand in one that was met are
  paid for it then. There were no races to give medals for.
- **Nothing is reset.** A new season changes the weather and the goal and
  nothing else: postmarks, licence, gear, medals, albums, stations and the
  chart are as they were.
- A session settles seasons once for each day that opens, reads the season
  under way at each look, and reads a closed season once. If settling
  cannot be written it is tried again after a minute, not at every look.
- The records gain `seasons`, `race`, `season_medals`, `season_claims` and
  `season_hands`, an index on the day a cell was charted, and the schema is
  version 8. A visitor, or a caller whose records cannot
  be kept, has no season.

**Not yet tuned.** The targets and the pay are a first table. The goals the
plan gives as examples ("a chain of masts through a pass", "supply a frontier
town") are not among them: these five are the ones the records can count.

**The Gyre.** Three bands around the centre. In the outer band forecast
coverage is capped at half. In the inner band it is capped at zero and nothing
can be planted. The eyewall needs layer 5 or above, or the engine. Inside is the
eye: calm air, the fortieth wonder, and a place in the hall for the first pilot
on the node to arrive. What a later version puts there is out of scope.

**The Gyre, as built (S23).** The tables are `GYRE_OUTER_KM`,
`GYRE_INNER_KM`, `GYRE_WALL_KM`, `GYRE_EYE_KM`, `GYRE_OUTER_COVER`,
`GYRE_WALL_LAYER`, `GYRE_WEAR`, `EYE_PAY` and `EYE_POINTS`.

| From the centre | Band | What holds there |
| --- | --- | --- |
| 150 to 300 km | outer | a forecast is worth half at most, whatever stations are near |
| 60 to 150 km | inner | nothing is forecast; nothing can be planted (so the nearest a mast can stand is just outside 150 km) |
| 30 to 60 km | the wall | only layer 5 and above gets through |
| under 30 km | the eye | still air; the fortieth wonder; the hall |

- **The failing forecast.** Coverage (section 4) is cut to 0.5 in the outer
  band and to nothing inside the inner one, after every station has been
  counted. A track drawn there is a guess from its first turn.
- **The wall.** A balloon below layer 5 whose turn would carry it into the
  wall is thrown back where it was and takes 4 wear. A track that would run
  into it ends there and says why. A first balloon tops out at layer 3 and
  an Envelope II at layer 4: neither ever gets in. Envelope III does.
- **The way in.** The Gyre turns anticlockwise and draws air inward, so a
  balloon held in layer 5 inside the inner band spirals toward the middle,
  unless the season's flow up there is stronger than the storm on the side
  it came in from. Tried by climbing to layer 5 and holding, from just
  outside the inner band: there is a way in on most days in four of the
  five worlds tried and on about one day in forty in the fifth, whose Gyre
  lies by the coast.
- **The eye.** The turn that first carries a pilot inside 30 km is written
  with their arrival: 50 postmarks and 30 points, once for each pilot. The
  first on the node is written in the hall, once however many arrive
  together, and has 200 and 100.
- **The hall** (`hall`) is what was done first on the node and is kept for
  good. It has one line so far. It is shown on the season's screen.
- **The chart** draws the inner band and the wall as rings of dots.
- The records gain `hall`, and the schema is version 9.

**Not yet built here.** The engine, the plan's other way through the wall,
is not sold. What the eye holds beyond the wonder and the hall is out of
scope.

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
name, its wind as the pilot knows it (`~` marks a forecast), and the fuel to
get there: a number for a step that is on offer, `--` for a step that cannot be
paid for, nothing for a layer more than a step away (or when nothing at all is
on offer, as at midnight). Under that is a bar of its
speed, ten km/h to a block for every layer so that bars can be compared, with a
word where there is one to say: `you`, `icing`, `cloud deck`, `out of reach`,
`needs oxygen`, or `ground` where the land fills the layer. The layer the
balloon is in says what is in it too: `you, icing`.
Beside the rows, where the map is drawn in pixels, stands a slice of sky eight
cells wide: darker with height, the ground filling the layers it reaches, the
cloud deck in its layer, and the balloon hanging in the layer it is in. On the
character map there is no sky column and the rows start at the panel's edge.

Stores are bars for fuel, wear and daylight, and a row of marks for ballast
sacks and pibals. The ground wind is the lowest layer's wind and is known only
as well as that layer is: read from within it or by a pibal, a forecast marked
`~` from above. The landing it would give is named when it can be made, from
the lowest open layer. Bars and marks use only the full block, the middle dot
and the
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
fifty map cells is carried along it: a bright head and a dimmer tail of one
tone, five pixels in all, that lighten the ground beneath. A streak that
leaves the map, or grows old, starts again somewhere else.

A frame costs what its streaks move, so three things keep that cost from
following the wind. Faster wind moves a streak faster but not in proportion:
0.17 of a pixel a frame at 6 km/h, 0.29 at 25, 0.46 at 100. The tail is one
tone and not a fade, so a streak that moves changes three pixels, not all
five. And the map is told what each frame of moving wind came to and draws
fewer streaks, down to a fifth of them, when frames run over 5.5 KB. Three
things are not counted as a frame's cost: the frame after the number of
streaks changed, which carries the change itself; a whole repaint; and a
paint that carried several frames, skipped or held back by the cap, is divided
among them. A paint a key caused is the key's and is not measured. Measured
over SSH on ReLink before these, a burst on a 23 km/h layer cost two to three
times one on a 6 km/h layer, up to 99 KB at 168 by 46, and there it arrived as
a stall and then a lump: the cap below was holding frames back. The streaks are seeded from the window, so the
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

Built so far: the splash, the launch and the landing. Each is a backdrop
painted once (a sky of four tones from its top to the horizon, taken from the
hour; a sun on its arc or a moon and stars; cloud by the cloud cover over the
balloon; two ranges of hills and the ground in the colour of the land there;
at a town or station, roofs and a mast, with windows lit from dusk to dawn)
and the balloon moving over it. That backdrop at a place is the station vista;
the station screen of P3 will stand on it. Below truecolor the ground is one
tone, because a fade to dark there passes through grey and black.

| Scene | Plays | Lasts |
| --- | --- | --- |
| Splash | at the door | the balloon rises for 2.1 s, then it holds under "Press any key" |
| Launch | after a launch is saved | 1.6 s |
| Landing | after a landing is saved | 1.9 s, then the picture stays 0.7 s |

The caption under a launch or landing is the logbook's own line for it. A
scene runs at 7 frames a second. A frame that costs more than 6 KB is followed
by a longer wait, in proportion, up to four frames' worth, and the balloon
moves that much further at the next one, so a wide screen gets fewer frames
and the scene lasts as long. Its last pose is always shown. A resize ends a
scene. A key in the first 0.3 s of a launch or landing scene is used up
without ending it: a held Enter was not a wish to skip what it began. Ctrl-C
is never held off, and the splash, which asks for a key, takes one at once. A turn that could not be saved gets no scene; the screen says
it was not saved. The bytes of a scene, and of the paint that puts the game
back, are not counted against the moving wind's cap, so the burst after a
launch plays as after any turn. The session deadline, a lost caller and Ctrl-C end the session from a
scene as from anywhere else.

Without half blocks (sixteen colours, CP437 without them, ASCII) there are no
launch or landing scenes, and the splash is the title and the prompt as text.
With motion off a scene is one still picture: a launch or landing stays 1.3 s.
`DOOR_SCENES=0` leaves the splash, still.

**Budgets.** Targets: under 40 KB a second while the wind moves, under a
quarter of one core, and a one-hour session inside the shipped profile's CPU
limit with margin. Measured in process on the development host, in truecolor:

| A burst, truecolor | 80 by 24 | 132 by 40 | 168 by 46 |
| --- | --- | --- | --- |
| Streaks | 16 | 66 | 106 |
| In a 6 km/h wind | 3 KB | 11 KB | 20 KB |
| In a 25 km/h wind | 7 KB | 23 KB | 47 KB |
| In a 110 km/h wind | 13 KB | 39 KB | 47 KB |
| Its worst second | 9 KB | 28 KB | 34 KB |
| CPU while moving | 4% of a core | 9% | 15% |

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

One SQLite file for each world, `fair_winds-<seed>.db`, under
`DOOR_FW_DATA_DIR` or the installation's `data` directory. The seed in the
name is the world's, as in the terrain cache beside it, so two nodes that
share a data directory, or a SysOp trying the door from a shell, never meet in
one file. The file carries an `application_id` of its own (`FWDB`) and a
`user_version` for the schema, now 2. WAL mode, or a rollback journal with
`DOOR_FW_JOURNAL=delete`; `synchronous=FULL`, so a turn is on disk before its
result is shown. Every write is one short transaction that takes the write
lock with its first statement and waits up to five seconds for another
caller's. No transaction is open while a caller thinks.

| Table | Holds | Built |
| --- | --- | --- |
| `meta` | Node id, seed, generation, epoch date, tempo, high day, maintenance flag | S10: seed, node, created |
| `pilots` | Identity, logbook day, licence, postmarks, upgrades, envelope design, home | S10: handle, career |
| `sessions` | One lease per pilot with a heartbeat | S10 |
| `flights` | The flight in progress: state after the last committed turn | S10 |
| `turns` | The journal of committed turns for the current flying day | S10 |
| `cargo`, `contracts` | What is carried and what is on offer | |
| `charted` | Chart cells: first pilot and day | S14 |
| `features` | Discovery, chosen name, pilot, day | S14 |
| `stations`, `contributions` | Stations, stock, mast progress, who gave what | |
| `strandings` | Who is down, where, what they need | |
| `guestbook` | Lines per station | |
| `almanac`, `wonders` | Entries and first sightings | |
| `drift`, `trials` | Scores | |
| `seasons`, `hall` | Season state, medals, firsts | |
| `outbox` | Outbound request identifiers and receipts | |

The terrain tile cache is separate files beside the database. It can always be
rebuilt and need not be backed up.

**Opening.** A file that is new and empty is given the schema. An older schema
is brought forward one version at a time inside one transaction; two callers
arriving at once do it once. A file that is damaged, is not a database, is
some other program's, holds another world, or was written by a newer Fair
Winds is refused: the door says so to the caller, writes the reason to
standard error, exits with status 3 and changes nothing. Nothing is ever moved
aside or replaced. A data directory that cannot be written is different: the
caller plays, is told on the first screen that the game cannot be saved, and
nothing is kept. Records that are only out of reach when the pilot is read
(locked by another caller past the five seconds) are treated the same way and
said to be busy, not damaged.

**The turn commit.** One transaction writes the new flight state, the journal
row, any chart cells, and any world change the turn caused. A process killed at
any instant leaves either the turn before or the turn after. The journal holds
the current flying day: a commit on a later day removes the days before it. A
commit that fails (a full disk, a directory gone read-only) does not stop the
game: the screen says the game cannot be saved, and the next commit that
succeeds saves it again.

**The lease.** A pilot has one live session. Opening the door claims the
pilot. A session says it is alive every 20 seconds and with every commit. A
second session finds the lease: if it has not been heard from for 90 seconds
it is replaced without asking; if it is live the caller is told who holds the
pilot (the transport and how long ago it opened) and may press T to take over
or Q to leave. Taking over ends the other session at its next turn or its next
heartbeat, whichever comes first: its commit is refused, having written
nothing, and it shows that the pilot is now flying elsewhere. Leaving the door
gives the pilot up, and so does a line that drops (`SIGHUP`, `SIGTERM`, end
of input). A session that is killed outright does not, so a caller who comes
straight back after one is asked, and takes over. Before a turn is worked out
the lease is read; the commit checks it again inside its transaction, so a
turn costs one synced write.

**Guestbooks.** `python fair_winds.py --guestbook` lists every line callers
have written; `--guestbook-remove world:number` removes one (section 7).

**Backup.** `python fair_winds.py --backup FOLDER` copies every world in the
data directory to FOLDER through SQLite's online backup, checks each copy and
removes the leases from it. It is safe while callers are playing. It refuses
to copy into the data directory itself, or onto a database in use. A refusal
comes before the first copy, so a refused backup has copied nothing. To restore,
stop the door, put the copies in the data directory and remove any `-wal` and
`-shm` files left beside the old ones.

**Save files from before the database.** The plain flight kept one JSON file
per pilot, `pilot-<seed>-<user>.json`. A pilot the database has never seen who
has one is taken over from it, once, and the file is renamed with `.imported`
added, unless a file of that name is already there. One that cannot be read
is left where it is: the pilot starts anew and is told, and the reason goes to
standard error.
One that is there and cannot be opened is waited for: the pilot plays
that call unsaved, so that no new career shuts the old one out, and the file is
taken over once the SysOp has put it right.

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
