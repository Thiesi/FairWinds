# Fair Winds: flight plan

This is the build plan. [systems.md](systems.md) is the specification the
slices implement: the calendar, the world, the weather, the turn, the shared
world, presentation and storage, with starting numbers. The
[pitch](pitch/fair-winds.html) holds the mockups the presentation slices aim at.

## Product

A balloon cannot steer. It can only climb or sink, and every altitude blows a
different way. Fair Winds is a turn-based navigation game built on that one
fact: each turn the caller sees where the layers within reach would carry the
balloon and picks one. The balloon carries mail across a continent that is
unique to each BBS, starts out blank, and is charted by the node's callers
together. Everyone flies the same weather, and the weather follows the calendar.

There is no combat and no way to harm another caller. Callers chart the map for
each other, plant weather stations that sharpen everyone's forecast, raise
mooring masts together and fly supplies to the stranded. Competition is in the
standings only. At the centre of every continent stands the Gyre, a permanent
storm nobody has entered. Reaching its eye is the long goal.

A turn takes seconds. A flying day takes five to ten minutes. The game is meant
to be the showcase door for NetBBS: it has to be beautiful on a wide truecolor
terminal, fully playable on 80x24 in sixteen colours, and worth opening again
tomorrow.

Art direction: weather-chart blues and relief greens by day, long gradient
skies at dawn and dusk, warm burner light, white wind. No emoji, font-dependent
icons, blinking text or colour-only meaning. Wind always has a glyph, a compass
point and a number.

## Resolved scope (P0)

- **Distribution:** one `fair_winds.py`, Python 3.11+, standard library only,
  BSD-2-Clause. Separate tests, docs and profile JSON. No companion process, no
  network service, no NetBBS imports, no new API requirement.
- **Host:** native stdio, UTF-8 with CP437 and ASCII readings, `resize_signal:
  true`, geometry 0x0 (follow the caller), documented absolute interpreter and
  install paths. NetBBS handles Telnet, SSH and web framing; the door only reads
  and writes the child terminal stream. Minimum terminal 80x24; below it the
  door asks for a bigger window and waits.
- **API:** `door_api` 5 for a hosted launch. Reject any other version clearly.
  Ignore unknown additive fields; treat documented optional fields as optional.
  Malformed supplied metadata is an error, never a silent standalone fallback.
  Standalone play (`python fair_winds.py`, no `NETBBS_DOOR_INFO`) is for
  development and uses a local world.
- **Classic hosts:** DOOR32.SYS and DOOR.SYS on other BBS software (Synchronet,
  Mystic, ENiGMA½), as Lunar Lander supports them: the drop file's name as the
  one argument, stdio or the inherited DOOR32 socket, CP437 and sixteen colours
  by default, 80 columns with no live resize. A classic caller gets the whole
  game at the small layout tier. NetBBS-only features (game events, `!top`,
  board posts, chat lines) are absent there. Scoped below under
  [Classic hosts](#classic-hosts) and built in S28 and S29.
- **Pacing:** the unit of play is the world's calendar day, never the call. A
  world day opens at node-local midnight. A pilot flies each world day at most
  once, in order, and can never fly a day that has not opened. Up to three
  opened days wait; older ones lapse. A session boundary means nothing: a flight
  is saved every turn and resumes exactly where it stopped, on any transport,
  minutes or days later. Trials and Daily Drift practice cost no calendar time,
  so there is always something to fly. See [systems.md](systems.md#2-the-calendar).
- **Determinism:** every outcome is a pure function of the world seed, the
  pilot, the world day and the turn number. A turn is committed before its
  result is shown. Hanging up changes nothing and rerolls nothing.
- **Controls:** W/S or up/down step the previewed layer; Enter or Space commits
  the turn; J jettisons ballast; P releases a pibal; L lands; F forecast; C
  chart; ? help; Q moors or quits. No key-up, chord or timing dependency. Keys
  are the first letter of their label. B and Esc go back, `<` `>` page, `/`
  finds, `?` helps, as on NetBBS's own screens.
- **Simulation:** six wind layers; one turn is 15 minutes of flight; climbing
  costs fuel, sinking is free, holding costs a little; terrain blocks low
  layers; landing needs a calm surface. Simulation is independent of rendering
  and terminal size.
- **Weather:** computed, never stored, never simulated in the background:
  prevailing flow per layer and season, travelling highs and lows, the Gyre,
  local daily winds, small-scale noise. The forecast is the same function with
  error that grows with lead time and with distance from the nearest station.
- **World:** one continent of about 1600 by 1000 km per node, generated from a
  seed derived from `node_id`. Terrain is baked once into cached tiles; nothing
  procedural is evaluated per frame.
- **Presentation:** a cell buffer with changed-run output; half-block pixels;
  truecolor with art-directed 256- and 16-colour readings; three layout tiers
  (80x24, 132x40, 168x46 and wider). Animation runs in short bursts at about
  8 frames a second and obeys the caller's motion preference. `DOOR_SPLASH=0`
  and `DOOR_SCENES=0` mean what they mean in the other bundled doors. Never
  write the bottom-right cell. Resize invalidates the diff buffer.
- **Shared world:** asynchronous. Charting, feature names, stations, masts,
  guestbooks, the almanac, rescues and seasons live in one SQLite world per
  node. A single caller on a quiet node must make steady progress alone; other
  callers make things faster and never gate them.
- **Storage:** SQLite under `DOOR_FW_DATA_DIR`, otherwise an installation-local
  `data` directory. Keyed by opaque `node_id` plus numeric `user_id`, never
  handles. WAL, short transactions, a versioned schema with an application ID,
  no lock held while a caller thinks. One live session per pilot.
- **Safety and cleanup:** EOF, broken output, SIGTERM, the session deadline and
  an ordinary quit all end promptly and restore the terminal. Bounded reads,
  sanitized names, bounded errors. Save before celebrating.
- **Guests:** a guest call is a visit: Trials, Daily Drift practice and a
  read-only chart, with nothing written to the world. A call is a guest call
  when its `user_id` is 2^62 or above (what NetBBS gives a bundled door's guest
  call) or is listed in `DOOR_FW_GUEST_IDS` (for an installed copy).
- **Outbound:** optional and off unless the SysOp switches it on. Game events
  (`rank`, `achievement`, `capture`, `medal`, `season`), a standings snapshot, a
  weekly Gazette board post and optional storm warnings in chat. Only what was
  earned live is reported.
- **Release:** a tagged single-file asset, licence, profile and SHA256SUMS,
  matched against the tagged source. NetBBS vendoring is a downstream task; do
  not change its frozen API or edit a vendored copy.

No unresolved product decision blocks implementation. The acceptance matrix at
the end is an evidence gate, not permission to claim testing nobody performed.

## Contract research

Reviewed NetBBS v7.45.0 (`main` at `dcf247479d6f59093fe8e1a8b256bbeaca9b5d6c`)
on 2026-11-22:

- [Developer handbook: native doors, metadata, lifetime and resize](https://github.com/Thiesi/NetBBS/blob/main/docs/NetBBS-Developer-Handbook.md#developing-a-native-door).
  `door_api` is 5. `color_depth` is `truecolor`, `256` or `16`. `charset` is
  `utf-8`, `cp437` or `ascii`. `quadrants` says whether quarter blocks show.
  `unicode_style` is a boolean. `timezone` and `datetime_format` are node-wide.
  `session_limit_seconds` is elapsed wall time for this launch.
- Limits: 300 CPU seconds, 256 MiB, 3600 seconds wall time and five seconds of
  stop grace by default, all changeable in the profile. The CPU ceiling is
  `SIGKILL` with no warning, so the door cannot save there. This is why every
  turn is committed as it is played.
- Resize: a stdio profile with `resize_signal` gets the JSON rewritten and
  `SIGUSR1` on POSIX. Read the file again and redraw; also poll it, since
  notifications coalesce and Windows has no signal.
- Environment: only `DOOR_*` names pass from a profile. All settings are
  `DOOR_FW_*`.
- [Outbound posting, events and standings](https://github.com/Thiesi/NetBBS/blob/main/docs/NetBBS-Developer-Handbook.md#door-outbound-posting).
  The `outbound` block is absent when the hook is off. Requests are JSON files
  renamed into the drop directory; receipts are matched on their `request`
  field. A door posts only while its minimum play level is 0. Event types are a
  fixed set and NetBBS words the sentence. `"rehearsal": true` means nothing is
  published.
- Guests: NetBBS gives a bundled door's guest call a `user_id` of 2^62 or above,
  a throwaway copy of its data and a rehearsal outbound block
  (`netbbs.guest_call`, `netbbs.doors.runtime.run_door`). An installed copy gets
  the shared guest account instead, which is why `DOOR_FW_GUEST_IDS` exists.
- [Lunar Lander](https://github.com/Thiesi/LunarLander) is the closest sibling:
  single file, standard library, SQLite records, half-block scenery, 10 frames a
  second, the same host. Read its `docs/design.md` and `docs/operations.md`
  before S01 and S06. Do not import from it.

The existing API is sufficient. No NetBBS gap issue is required.

## Classic hosts

Scoped on 2026-11-23 against Lunar Lander's shipped classic support (its README,
"Classic BBS setup"), which is the model. Fair Winds differs in three ways: it
has a shared world, a calendar, and far more screens.

**Launch.** `python3 fair_winds.py /path/to/node/DOOR32.SYS`. `DOOR.SYS` works
too. The file is recognised by name without case, or by its contents when
renamed, with CRLF or LF endings. `NETBBS_DOOR_INFO`, when set, wins. A
malformed drop file stops the launch with a message on stderr that never names
the file. A caller without ANSI (DOOR32 emulation 0, DOOR.SYS `NG` or `7E`) is
told the game needs an ANSI terminal and is returned to the BBS.

**What the launch record takes from a drop file.**

| Launch record | DOOR32.SYS | DOOR.SYS | Otherwise |
| --- | --- | --- | --- |
| Pilot identity | User record number | User record number | Required; a launch without one is a visit |
| Handle | Alias | Alias or name | Sanitized, display only |
| Node identity | | | `DOOR_FW_NODE_ID`, default `classic` |
| BBS name | | | `DOOR_FW_BBS_NAME`, default "BBS" |
| Geometry | 80 by 24 | 80 by page length | No live resize |
| Colour depth | | | 16, or `--color 256` or `truecolor` |
| Charset | | | CP437, or `--encoding utf-8` |
| Session limit | Minutes left | Minutes left | Warn, then return before the cutoff |
| Timezone | | | `DOOR_FW_TIMEZONE`, default UTC |
| Transport | Type 2: inherited socket | Stdio | Stdio |
| Outbound | | | Never present |

**Consequences for the game.**

- *Identity.* Pilots are keyed by record number, so an alias change keeps a
  career. Different BBSes' record numbers overlap, so each BBS needs its own
  data directory, shared by all its nodes. The world seed derives from
  `DOOR_FW_NODE_ID`: keep it once set, or the continent changes.
- *Shared world.* Every node of a multi-node BBS opens the same SQLite file.
  That is what S10 builds; a classic host adds no new concurrency. The data
  directory must be on a local disk: SQLite's WAL does not work over a network
  share. A host that cannot guarantee that sets `DOOR_FW_JOURNAL=delete`.
- *Calendar.* The world day turns at midnight in `DOOR_FW_TIMEZONE`. Nothing
  else about pacing changes: a drop file's time limit ends a session, and a
  session means nothing to the game.
- *Layout.* Always the small tier. This is the reason the small tier must carry
  the whole game and never a reduced one (S05, S06, and every screen slice's
  80x24 acceptance).
- *Display.* Sixteen colours sent as every ANSI-BBS terminal takes them: bold
  for a bright foreground and the eight plain backgrounds, never the 90-97 and
  100-107 codes. CP437 shade blocks stand in for half-block pixels. Motion
  defaults to still on a classic host; `DOOR_FW_MOTION=burst` turns it on.
- *Guests.* The drop file has no guest flag. `DOOR_FW_GUEST_IDS` lists the
  record numbers to treat as visits.
- *Gazette, standings, events.* The in-door Gazette, hall and Daily Drift table
  work everywhere. Publishing them is a NetBBS feature.
- *DOOR32 socket.* With communication type 2 the door uses the inherited handle
  (a descriptor on POSIX, Winsock on Windows), filters telnet commands, doubles
  outgoing 0xFF, takes CR NUL or CR LF as one Enter, and closes only its own
  handle on return. This is the path for Synchronet on Windows.

**Not supported.** Non-ANSI terminals. DOS doors, FOSSIL and serial handles: the
door is a native program. DORINFOx.DEF, CHAIN.TXT and other drop files. Live
resize. 40-column terminals.

**Evidence.** A fixture is not a host. S28 and S29 are complete on fixture and
subprocess evidence; a recipe for a named host is marked verified in the README
only after a real launch on that host, and stays marked unverified until then.

## Open questions

None of these blocks a slice. Each is answered in the slice named.

- **Is choosing a layer fun for forty turns?** S05 ends in a human playtest. If
  the answer is no, stop and rework the turn before any art is drawn.
- **Render budget.** How many bytes and how much CPU does animated wind cost on
  each transport? S08 measures it and sets the burst length and particle count.
- **Starting numbers.** Every number in [systems.md](systems.md) is a first
  guess. S05 tunes the turn, S12 the economy, S26 the calendar window.
- **Bundled guest calls on a shared world.** NetBBS copies a bundled door's data
  for a guest call. Fair Winds writes nothing for a guest, so no copy is needed.
  Settle this with NetBBS when it vendors the door (S27).

## Phases and slices

Each P-number is a GitHub milestone with one tracker issue.
[P0 #1](https://github.com/Thiesi/FairWinds/issues/1),
[P1 #2](https://github.com/Thiesi/FairWinds/issues/2),
[P2 #3](https://github.com/Thiesi/FairWinds/issues/3),
[P3 #4](https://github.com/Thiesi/FairWinds/issues/4),
[P4 #5](https://github.com/Thiesi/FairWinds/issues/5),
[P5 #6](https://github.com/Thiesi/FairWinds/issues/6),
[P6 #7](https://github.com/Thiesi/FairWinds/issues/7).
Each slice gets a reviewable PR, or adjacent tightly coupled slices share one PR
with explicit acceptance evidence. Blockers are hard dependencies.

### P0 — Contract and design

**S00 — Resolve the product and host contract.** Build this plan, the systems
specification, the repository and the milestones and trackers.
Why: implementation needs an agreed, executable scope.
Outcome: no unresolved launch, pacing, control, storage or presentation decision.
Acceptance: source-linked contract research; every slice has build, why, outcome,
acceptance and blockers; release and manual boundaries are explicit.
Blockers: none.

### P1 — The wind and the turn

The whole game with no art. P1 exists to prove the turn is fun before anything
is drawn.

**S01 — Host adapter and terminal lifecycle.** Build launch metadata validation,
an incremental key decoder, timed input, resize and deadline handling, terminal
restore on every exit path, standalone launch, and the CI workflow.
Why: nothing else matters if a caller cannot enter or leave reliably.
Outcome: the door launches, shows a placeholder screen and returns cleanly.
Acceptance: real subprocess pipe tests for input, EOF, quit, SIGTERM and
deadline; split escape sequences; optional-field defaults; one host-neutral
launch record that everything after start-up reads, so a drop file can fill it
later without touching the game (S28); `door_api` other than
5 rejected; malformed metadata diagnosed on stderr; undersize terminal handled;
`profiles/netbbs.json` accepted by NetBBS's profile parser; the suite green on
Linux and Windows; scratch kept bounded as [AGENTS.md](../AGENTS.md) requires.
Blockers: S00.

**S02 — World generator and terrain bake.** Build the seed derivation, elevation,
moisture, coast, biomes, settlements and features, and the on-disk tile cache.
Why: the continent is the board, and it must be the same for every caller.
Outcome: any map window reads from cached tiles in milliseconds.
Acceptance: the same seed gives byte-identical tiles across processes and
platforms; different seeds differ; a tile bakes in under 0.3 s and reads in
under 2 ms on the development host; land fraction, town count and feature count
fall inside the documented ranges for 50 seeds; every town is on land and
reachable; a corrupt or stale cache is rebuilt, never trusted.
Blockers: S00.

**S03 — Weather model.** Build the pure wind function, the travelling systems,
the Gyre, local daily winds, cloud, squalls and surface wind, and the forecast
with its error model.
Why: the weather is the opponent and the shared calendar.
Outcome: wind, cloud and forecast for any place, layer, world day and hour.
Acceptance: identical output across processes for the same inputs; continuous in
space and time (no jump above a documented bound between adjacent turns or
cells); speeds inside each layer's range over a simulated year; systems cross
the continent in three to six days; forecast error is zero at lead zero, grows
with lead time and shrinks under station coverage; one wind sample costs under
60 microseconds; no state is read or written.
Blockers: S02.

**S04 — Turn engine.** Build flight state, the actions and their costs, drift,
terrain blocking, landing classes, stores, wear, failure and the keyed random
source.
Why: the turn has to be fair, learnable and impossible to reroll.
Outcome: reproducible flights independent of rendering and of the terminal.
Acceptance: a scripted flight replays to identical state; every action's cost
matches the table; a blocked layer cannot be entered; fuel exhaustion descends
and lands by the documented rule; each landing class is reachable and named;
the same turn replayed after a simulated disconnect gives the same result;
a scripted delivery between two towns succeeds.
Blockers: S03.

**S05 — Plain flight and the fun gate.** Build an 80x24 text interface over the
engine: the layer table, a character map, three previewed tracks, the log, and
one complete flying day from briefing to landing.
Why: decide whether the game works before spending on presentation.
Outcome: a caller can fly a whole day, in standalone or hosted launch.
Acceptance: scripted end-to-end launch, preview, commit, land and quit; resume
after a killed process continues on the same turn; help explains every key;
no stale key commits a turn. Then a human playtest of at least five flying days
by the owner, with the verdict and the retuned numbers recorded in the PR.
P2 does not start until the verdict is yes.
Blockers: S01, S04.

### P2 — Beautiful flight

**S06 — Cell buffer, palettes and layout tiers.** Build the cell grid, changed-run
output, SGR economy, the truecolor, 256- and 16-colour readings, the charset
readings, and the three layout tiers with live resize.
Why: every screen stands on this, and limited terminals need art direction.
Outcome: one drawing API that renders correctly at every tier.
Acceptance: unchanged frames emit nothing; no cursor move out of bounds and no
bottom-right write at any size from 80x24 to 240x70; captures at each colour
depth and charset inspected; a resize repaints fully; a 16-colour reading uses
only the bold attribute and eight backgrounds.
Blockers: S05.

**S07 — Relief map.** Build the map compositor: terrain tiles, hillshade, the
uncharted veil, cloud and squall, markers, labels and the previewed tracks, with
a character-map reading for classic terminals.
Why: the map is where the caller spends the game.
Outcome: a legible, attractive map at every tier.
Acceptance: captures at 80x24, 132x40 and 168x46 in each colour depth inspected
against the pitch; labels never overlap the balloon or run off the map; tracks
stay readable over any terrain; a full map composes in under 30 ms from warm
tiles; scrolling the window re-sends only changed cells.
Blockers: S06, S02.

**S08 — Wind animation and the sounding panel.** Build wind streaks, burst
scheduling, the still pattern for motion off, the sky column, the layer rows,
stores, forecast strip and the wide-tier panels. Measure the render budget.
Why: moving wind is the picture the game is sold on, and it must be affordable.
Outcome: the flight screen of the pitch, within budget.
Acceptance: bytes per second and CPU per second measured over SSH, Telnet and
the web terminal and recorded; defaults chosen so a one-hour session stays
inside the shipped profile's CPU limit with margin; motion off, `DOOR_SCENES=0`
and typed-ahead keys all suppress animation; a slow link drops frames rather
than queueing them.
Blockers: S07.

**S09 — Scenes.** Build the splash, launch and landing scenes, and the station
vista with a sky that follows the hour, the weather and the biome.
Why: arrival and departure are the game's rewards, and the hub needs a face.
Outcome: skippable full-screen scenes consistent with the other bundled doors.
Acceptance: each scene plays only after its result is saved, never waits for a
key, ends on any key and consumes it; `DOOR_SPLASH=0` and `DOOR_SCENES=0`
honoured; captures at each tier inspected; scenes obey the session deadline.
Blockers: S06.

### P3 — Career

**S10 — World database and pilot records.** Build the schema, migrations, the
pilot record, the session lease, the atomic turn commit and mid-flight resume.
Why: a shared world is only as good as its durability.
Outcome: flights, pilots and world state survive restart, crash and concurrency.
Acceptance: node and account isolation; eight concurrent callers flying with no
lost update and no lock held during think time; a second session for the same
pilot is refused or takes over cleanly; `SIGKILL` between any two statements
leaves a consistent world; a corrupt or newer database fails clearly and is
never replaced; read-only storage does not trap a caller; backup instructions
verified by restore.
Blockers: S05.

**S11 — The calendar.** Build the world-day function, the logbook pointer, the
three-day window, turnover during a flight, tempo, and clock-skew handling.
Why: the calendar is the game's pacing and its shared rhythm.
Outcome: play is paced by world days and never by sessions.
Acceptance: a day flown cannot be flown again by reconnecting, relaunching or
switching transport; a day never opens early; four missed days leave exactly
three; a flight begun before midnight keeps its day; daylight-saving changes and
a clock set backwards never repeat or skip a day; tempo 2 and 4 open days at the
documented hours; the briefing always says which day is being flown and when
the next one opens.
Blockers: S10.

**S12 — Towns, stations and the post.** Build the hub screen, the post office
and its contracts, stores, cargo, postmarks and the first economy table.
Why: a flight needs a reason and a reward.
Outcome: the pick up, fly, deliver, resupply loop works end to end.
Acceptance: contracts are generated deterministically per town and day; a
delivery pays what it promised; a late or lost cargo costs what the table says;
a scripted ten-day career never deadlocks (no state from which the pilot cannot
earn fuel); prices and payouts are data, not code.
Blockers: S10, S11.

**S13 — Licence and balloon.** Build the licence ladder, upgrades, the layers
they unlock, and the envelope designer.
Why: long-term progress, and a balloon a caller can call theirs.
Outcome: ranks and upgrades change what a pilot can attempt.
Acceptance: each rank is reachable by scripted play; each upgrade has a
measurable effect covered by a test; an envelope design is stored, shown in
scenes and on other callers' maps, and readable in sixteen colours.
Blockers: S12.

### P4 — Shared world

**S14 — Charting and names.** Build the charted grid, sight by layer and cloud,
feature discovery and naming from generated candidates.
Why: finishing the map together is the game's centre.
Outcome: one caller's flight changes every caller's map.
Acceptance: cells charted by pilot A appear for pilot B on the next read; cloud
below blocks charting; a feature is named exactly once under concurrent
discovery; names are composed from word lists and cannot contain caller text;
charted percentage is consistent with the grid.
Blockers: S10, S07.

**S15 — Stations and masts.** Build planting, upkeep and lapse, forecast
coverage, and community-built mooring masts.
Why: infrastructure is how the frontier moves and how callers help each other.
Outcome: stations improve the forecast for everyone and need care.
Acceptance: a planted station lowers forecast error inside its radius for every
pilot; stock decays by world day and lapses at zero; contributions from several
pilots raise one mast exactly once; a mast permits a night landing and sells
fuel; three starting stations exist in a new world so a lone caller is never
without a forecast.
Blockers: S14, S12.

**S16 — Stranding, rescue and guestbooks.** Build stranding, walking out, rescue
flights, guestbook lines and other pilots on the map.
Why: failure should create something for someone else to do.
Outcome: callers meet each other's traces and can help.
Acceptance: a stranded pilot appears on every chart; a rescue rewards both and
is applied exactly once; walking out costs the documented days through the
calendar; guestbook text is sanitized, bounded and removable by the SysOp;
`DOOR_FW_GUESTBOOK=canned` and `off` are honoured.
Blockers: S15.

**S17 — The chart.** Build the whole-continent screen: charted land, isobars,
the Gyre, sites, camps, the route so far, and the continent, season, gazette
and records panel.
Why: the chart is the shared scoreboard and the planning table.
Outcome: the chart of the pitch, with pan and zoom.
Acceptance: captures at each tier inspected; isobars match the pressure field
the wind is derived from; pan, zoom, find and the name toggle work at 80x24;
the chart composes in under 60 ms from warm tiles.
Blockers: S14, S07.

**S18 — The almanac.** Build recurring local winds, their discovery by flying
them, and the almanac screen.
Why: knowledge of the winds is the game's deepest progress.
Outcome: discovered winds are named, credited and shown in every forecast.
Acceptance: each generated local wind recurs when its conditions hold; three
qualifying flights enter it, once, under the right pilot; an entered wind shows
on the forecast for all pilots; the generator gives each world 12 to 20 of them.
Blockers: S15.

### P5 — Reasons to return

**S19 — Daily Drift.** Build the daily course, the standard balloon, scoring,
one scored attempt per world day, unlimited practice and the standings.
Why: three minutes a day for the caller with three minutes.
Outcome: the same short course for everyone, with a table.
Acceptance: the same world day gives the same course across processes; a second
scored attempt is impossible by any reconnect; the score formula and tie-break
are shown in help; practice never touches the table.
Blockers: S10, S08.

**S20 — Trials.** Build the tutorial flight and twelve challenge flights with
medals and unlimited attempts.
Why: something to fly when the calendar is caught up, and a way to learn.
Outcome: a first-time caller is taught by flying, and a regular has medals to
chase.
Acceptance: every trial has a scripted gold flight; the tutorial completes in
under four minutes of scripted play and teaches preview, commit, ballast, pibal
and landing; trials cost no calendar time and write nothing to the shared map.
Blockers: S19.

**S21 — Wonders.** Build the forty wonders, their conditions, the capture
action, the scene families and the album.
Why: rare sights are the collectible and the best pictures in the game.
Outcome: wonders are found by being in the right place, hour and weather.
Acceptance: every wonder is reachable in a scripted flight on some world day
within a simulated year; first sighting is credited once; each scene family is
inspected at each tier; hints unlock as the documented conditions are met.
Blockers: S09, S14.

**S22 — Seasons.** Build the 28-day season, weather regimes, the node goal, the
closing race, medals and the season archive.
Why: a monthly rhythm and a shared target.
Outcome: each season changes the winds and ends with a result.
Acceptance: regimes change prevailing flow as documented; a goal is generated to
be reachable by one active pilot and faster with more; the race is scored once;
medals name at most three pilots; nothing a pilot owns is reset.
Blockers: S15, S19.

**S23 — The Gyre.** Build the storm's bands, the failing forecast, the eyewall,
the eye and the hall.
Why: the long goal.
Outcome: reaching the eye is possible, hard and recorded for good.
Acceptance: no station can be planted inside the inner band; forecast error
grows as documented toward the centre; a scripted late-game flight from the
nearest possible mast reaches the eye; a first-tier balloon cannot; the first
arrival is recorded exactly once under concurrency.
Blockers: S13, S18, S22.

### P6 — Host, acceptance and release

**S24 — Outbound.** Build game events, the standings snapshot, the weekly
Gazette and optional storm warnings, with persisted request identifiers.
Why: the door should give the board something to talk about.
Outcome: results reach chat and boards exactly as the SysOp allows.
Acceptance: nothing is sent when `outbound` is absent; rehearsal is reported as
rehearsal; only live-earned events are sent; a retried request is never
published twice by the door's own doing; ceilings are respected; a guest call
sends nothing.
Blockers: S13, S19, S22.

**S25 — SysOp tooling.** Build the administration entry point: inspect, backup,
verify, guestbook and name moderation, tempo re-base and season controls.
Why: a shared world needs an operator.
Outcome: a SysOp can look after a world without a database client.
Acceptance: every command is covered by a test against a real world file;
destructive commands back up first and say where; maintenance mode refuses
callers politely; the operations guide matches the commands.
Blockers: S16, S22.

**S26 — Native host acceptance.** Build repeatable NetBBS runtime scenarios and
capture tooling; fix what they find.
Why: fixtures do not establish compatibility with the real host.
Outcome: evidence that separates unit, subprocess, native runtime, transport
and human testing.
Acceptance: the full suite; real `run_door` launches at 80x24, 132x40 and
168x46; quit, deadline, disconnect, resize and eight callers; CPU and bytes
inside the budgets set in S08; a 28-day scripted season on a shared world; the
calendar window retuned from real play; unperformed checks listed as such.
Blockers: S24, S25.

**S27 — Release and SysOp handoff.** Build the README, the operations guide, the
release package and checksums; publish a tagged candidate or stable release
according to the evidence.
Why: SysOps need a deployable, maintainable game.
Outcome: a verified download with safe state operations.
Acceptance: clean-checkout suite; artifact and tag byte identity; LF endings;
checksum verification of the download; a fresh install launches; release notes
name outstanding field gates. Stable v1 needs real terminal and browser play by
people; without it publish an explicitly marked prerelease.
Blockers: S26, S28.

**S28 — Classic hosts: drop files over stdio.** Build DOOR32.SYS and DOOR.SYS
parsing into the launch record, the `--encoding` and `--color` options, the
classic settings, the non-ANSI refusal, the drop file's time limit, and the
README's classic setup with recipes for Synchronet, Mystic and ENiGMA½.
Why: a door that runs only on NetBBS reaches only NetBBS callers.
Outcome: the whole game on a classic host over stdio, at 80 columns in CP437.
Acceptance: fixtures for both formats with CRLF and LF, renamed files and
mixed-case names; each malformed field refused with a bounded message that does
not name the file; `NETBBS_DOOR_INFO` takes precedence; a scripted flying day
over a pipe in CP437 and sixteen colours emits no byte outside CP437, no
90-107 colour code and no cursor move beyond 80 by the page length; record
number keys the pilot and an alias change keeps the career; two nodes of one
BBS share a world; the session ends before the drop file's cutoff and resumes
on the same turn; a guest record number writes nothing; outbound is never
attempted. Recipes are labelled unverified until launched on the real host.
Blockers: S06, S10, S11.

**S29 — Classic hosts: the DOOR32 socket.** Build the inherited-handle transport
for communication type 2 on POSIX and Windows, with telnet filtering.
Why: Synchronet on Windows passes a socket, not stdio.
Outcome: the same game over an inherited socket.
Acceptance: a socketpair harness on POSIX and a Winsock harness on Windows play
a scripted flight; telnet commands and option negotiation are filtered; a
literal 0xFF survives both ways; CR NUL and CR LF are one Enter; a closed peer
ends the session promptly with the turn saved; only the door's own handle is
closed. Shipped as experimental until a real Synchronet launch is recorded.
Blockers: S28.

## Delivery discipline

Fetch and start from current remote `main`. One topic per branch and PR; when
work depends on an unmerged PR, stack it and say so at the top of the body.
Commit, push and open a PR; wait for Claude's completed written review. Triage
findings by caller and SysOp impact, fix the meaningful ones, and request
`@claude review` again after fixes. Merge only after that cycle and the relevant
checks pass, and only when the owner asks. Explain dismissed findings. Never
treat a failed or skipped reviewer run as approval. Never rewrite a pushed
branch. Delete merged branches and task-owned worktrees.

Captures that are evidence for a PR go on an orphan `evidence/<topic>` branch
and are linked from the PR body. They are not committed to the feature branch.

Every slice updates [systems.md](systems.md) where it changes a rule or a
number, in the same PR. The specification describes the game as built.

## Acceptance matrix

Automated: determinism of world, weather and turn; boundary scenarios for every
action and landing class; calendar edge cases; protocol fixtures; real stdin and
stdout subprocesses; storage concurrency and crash consistency; terminal bounds;
NetBBS's actual runtime and profile parser. Captures at 80x24, 132x40 and 168x46
in each colour depth and charset.

Field: a real terminal client over Telnet and SSH; the browser transport; a
real launch on each classic host the README gives a recipe for;
resizing during a flight; ordinary latency; disconnect and relaunch mid-flight;
eight callers; a season on a live node; and human judgement of fun, difficulty
and readability. Record these separately. A virtual terminal does not certify a
browser, and a script does not certify a playtest.

## Out of scope for v1

- Live multiplayer. Callers never need to be online together.
- Play across nodes over NetBBS Link. The door contract gives a door no Link
  access. A later version may let the Gyre's eye connect continents.
- A second continent. When a chart is complete, a later version may sight a far
  shore.
- Non-ANSI terminals, DOS-era drop files other than DOOR32.SYS and DOOR.SYS,
  FOSSIL and serial handles, and 40-column terminals.
- Sound.
