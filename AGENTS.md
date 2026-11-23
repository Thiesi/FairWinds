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

**Status:** P0 is complete. S01 (host adapter and terminal lifecycle), S02
(world generator and terrain cache), S03 (weather) and S04 (turn engine) are
built. The door opens, shows what the host told it and closes cleanly, and
`--world-map <node id>` prints a continent. The engine can fly a whole day, but
no screen shows it yet. The next slice is S05, the plain flight, which ends in
the owner's play-test.

For S05's tuning: with the starting balloon, the towns nearest the start town
are often 150 km and more away, and a simple scripted pilot reaches one only on
some days. Whether that is a good first flight is for the play-test to judge.

P1 ends in a playtest by the owner (S05). Do not start P2 until the owner has
said the turn is fun.

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
