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

**Status:** P0 is complete. S01 (host adapter and terminal lifecycle) is built:
the door opens, shows what the host told it and closes cleanly. The next slices
are S02 to S04. There is no game yet.

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

- Branch from `origin/main`. Merge through a PR, and only when the owner asks.
- Never force-push, `--force-with-lease` included. Reconcile with a merge. If a
  pushed branch must be redone, use a new branch name.
- Never use a bare `git stash`: the stash is shared by every worktree.
- One topic per PR. Stack PRs that depend on unmerged work and say so at the
  top of the body.
- Captures for a PR go on an orphan `evidence/<topic>` branch, linked from the
  PR body. They are not committed to the feature branch.

## Review

Open a PR and wait for Claude's completed written review. Fix what matters,
explain what you dismiss, and ask for `@claude review` again after fixes. A
failed or skipped reviewer run is not approval.

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
