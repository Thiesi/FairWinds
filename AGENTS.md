# Working in this repository

Guidance for anyone, human or agent, working on Fair Winds. The README says
what the door is. This file says how to work here.

## Start here

1. Read [docs/design.md](docs/design.md): the product, the resolved scope and
   the slices, each with its acceptance criteria and blockers.
2. Read [docs/systems.md](docs/systems.md): the rules and starting numbers the
   slices implement.
3. Open [docs/pitch/fair-winds.html](docs/pitch/fair-winds.html) in a browser to
   see the target screens. Its script is a working prototype of the terrain
   shape, the wind field and the half-block map. Port ideas from it, not
   constants.
4. Check the phase trackers on GitHub (issues #1 to #7) for what is done.

**Status:** P0 and P1 are built: S01 (host adapter and terminal lifecycle), S02
(world generator and terrain cache), S03 (weather), S04 (turn engine) and S05
(the plain flight). P2 is built too: S06 (the cell buffer, the colour and
charset readings, layouts that fill the terminal), S07 (the relief map), S08
(wind on the map, moving in bursts, and the sounding panel) and S09 (the
splash, and the launch and landing scenes). The game can be played:
`python fair_winds.py` in a terminal, or as a door under NetBBS. Of P3, S10
(the world database, the pilot's lease and the atomic turn commit), S11 (the
calendar: world days, the three-day window, tempo), S12 (contracts, cargo,
postmarks, the stores and the post office screen) and S13 (the licence, the
gear and the envelope designer) are built. P3 is built, and the owner is
play-testing it: what they have said so far, and what was changed for it, is
on the P3 tracker (issue #4). From that play-test came the last-stretch moves
(trail rope, mail drop, cargo sent on by road, a wait) and a pass over how the
screen reads. Of P4, S14 (the chart: what is seen from the basket is charted
for everyone, features are found and named) and S15 (stations planted in the
wild, kept stocked, and raised into mooring masts with freight from the
towns) and S16 (a stranded pilot's call for help on every map, rescue by
landing beside them, and the stations' visitors' books) are built at the
owner's asking, ahead of the play-test's verdict. S17 (the chart of the whole
continent: isobars, pan, zoom, find, names, the route so far) is built too.
S18 (the almanac: each world's local winds, entered by flying them on three
days) is built as well, which completes P4. Of P5, S19 (the Daily Drift: one
course a world day on a standard balloon, one scored attempt, a table) is
built, and so is S20 (the Trials: a first flight that teaches, and twelve
set flights with medals) and S21 (forty wonders in every world, taken for an
album when the balloon is there as they show, each with a picture) and S22
(the 28-day season: a goal for the node, a race in its last week, medals,
and nothing reset) and S23 (the Gyre: bands where the forecast fails, a wall
only layer 5 crosses, the eye and the hall). That completes P5. The plan
calls for the owner's second play-test here, before P6 (the host, the
SysOp's tools, acceptance and release).

To look at a screen, run `python scripts/capture.py`: it writes each size and
colour depth as an HTML page under `.test-tmp/captures`, which headless Edge
can turn into a picture. Look at the pictures before saying a screen is right.
Pictures kept as evidence go on an `evidence/<topic>` branch.

Every screen draws onto a `Screen` and the session sends only what changed.
Draw with `Session._line` or straight onto `session.frame`; never write escape
sequences yourself. Tests that drive the real door must look at the screen
(`Door.wait_for`, `Door.screen`), not at the byte stream, which no longer
spells whole lines.

**The play-test moved.** The owner played the plain flight on 2026-11-23 and
found it cannot be judged alone: it needs a real session, in a nearly finished
game. So the build carries on through P2 and P3, and the owner's play-tests come
after S13 (a whole career with its presentation) and after S23. Keep the turn
cheap to change until then: the engine must stay ignorant of the screens, and
every number must stay data.

Tuning already done from measurement: squalls were made
narrow and rarer (they sat over a start town all day), a place counts as
reached within 12.5 km (was 7.5), and the post is for three places at once,
because one fixed town is upwind on most days. `docs/systems.md` section 5a
has the numbers.


## What the code is

One file, `fair_winds.py`: Python 3.11 or newer, standard library only. Tests
live in `tests/` and run with `python -m unittest discover -s tests -v`. No
package, no build step, no third-party runtime dependency. NetBBS bundles a
tagged release of the single file, so keep it self-contained and keep LF line
endings.

Lunar Lander (`Thiesi/LunarLander`) is the closest sibling and the model for
host handling, the cell buffer, captures, the NetBBS smoke test and release
packaging. Read it; never import from it.

## Rules that are easy to break

- **The calendar, never the call.** Nothing may depend on how many times a
  caller connects. A flight is saved every turn and resumes exactly.
- **No rerolls.** Every outcome comes from the keyed source in
  [systems.md](docs/systems.md#14-determinism). Commit a turn before showing its
  result. Never call an unseeded random generator.
- **Weather is computed, never stored.** No background process, no tick.
- **Numbers are data.** Costs, prices and thresholds live in tables. When a
  slice changes a rule or a number, it changes `docs/systems.md` in the same PR.
- **Colour never carries meaning alone.** Every screen must read in sixteen
  colours and in ASCII.
- **Caller text is untrusted.** Sanitize it before it meets an escape sequence.
  Feature names are composed from word lists and never contain caller text.

## Local disk hygiene

Scratch from test runs and captures once grew to millions of files on the
development host and took it down. These rules keep it bounded.

- **One fixed folder per kind of scratch, never one per run.** Use
  `.test-tmp/<kind>` in the checkout, or `fair-winds/<kind>` in the system temp
  folder, reused by every run. Delete it when you are done.
- **The system temp folder is not storage.** Evidence worth keeping goes on an
  `evidence/<topic>` branch or under `docs/`.
- **Tests clean up after themselves.** A test that creates a world creates it
  in a temporary directory and removes it.
- **Run the full suite one at a time.** Never start a full run while another
  session's run is going, and never kill another session's processes.
- **One worktree per active branch.** Remove it once its PR merges.
- **Keep the checkout outside OneDrive** or any other synced folder.

## Git

- Branch from `origin/main`. Merge through a PR, after the review cycle below.
- Never force-push, `--force-with-lease` included. Reconcile with a merge. If a
  pushed branch must be redone, use a new branch name.
- Never use a bare `git stash`: the stash is shared by every worktree.
- One topic per PR. Stack PRs that depend on unmerged work and say so at the
  top of the body.
- Captures for a PR go on an orphan `evidence/<topic>` branch, linked from the
  PR body. They are not committed to the feature branch.

## Review

The owner's standing instruction (2026-11-23):

1. Open the PR ready for review, not as a draft. That starts Claude's automated
   review. If none arrives, or to ask again, comment on the PR tagging `@claude`
   with the word "review".
2. Triage the findings. Fix what a player, or a SysOp deploying the game, would
   actually notice: anything that makes the game worse to play or to run. A
   point of pure code correctness with no real-world effect may be declined;
   reply on the PR saying why.
3. Push the fixes, ask for a re-review, and triage again the same way.
4. When the review is settled and every check is green, merge the PR and move
   on to the next slice. No further go-ahead is needed.

A failed or skipped reviewer run is not approval.

The review workflows (`claude.yml`, `claude-code-review.yml`) are installed.
`acceptance.yml` runs the suite on Linux and Windows against a pinned NetBBS.

## Windows development notes

- Use the system `python`. Do not rely on a venv copied from another path.
- In Git Bash a Python child reads stdin as cp1252. Pass UTF-8 through files or
  read `sys.stdin.buffer`. Long heredocs with apostrophes can fail; write the
  content to a file instead.
- Headless Edge can screenshot an HTML capture:
  `msedge --headless=new --screenshot=<png> <file-url>`. It needs its own
  `--user-data-dir`.
- Windows has no `SIGUSR1`, PTYs or POSIX resource limits. Resize is polled
  there. Production is NetBSD and Linux.
