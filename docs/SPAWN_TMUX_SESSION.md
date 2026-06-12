# Spawn → tmux session (Windows) — spec, plan & handoff

**Branch:** `claude/lab/spawn-tmux-session` (off `v0.10.2`)
**Status:** implemented + **validated end-to-end on metal** (workstation-lc, 2026-06-12). Ready for Lab review/integration.

## Problem

On Windows the cell is launched from an **admin PowerShell console**. Running
`claude` directly there triggers the Ink-TUI input bug — the SGR `m` terminator
leaks into stdin, so **Enter inserts a literal `m`** and the app is unusable.

The pre-existing rescue (`_relaunch_in_windows_terminal`) bounces into Windows
Terminal, but on a box running a terminal multiplexer it pops a *detached*
window that **escapes the supervised session**.

### Ground-truth corrections (from on-metal testing, override the old docstrings)

1. The bug is **PowerShell-host-specific**, not "conhost vs Windows Terminal."
   Launching from **cmd works**; from **PowerShell breaks**. The old WT-relaunch
   only "worked" because it escaped *into a fresh console*, not because it
   reached WT.
2. **tmux fixes it.** claude rendered inside a tmux pane works from *both*
   PowerShell and cmd — tmux's own PTY answers the TUI's terminal queries, so the
   leak never reaches the app. tmux is therefore both the **rendering fix** and
   the **supervision substrate** (sidecar/watchdog `send-keys`).

## Design

New `_launch_via_tmux(binary, argv, cwd, session_name)` in
`commands/spawn.py`, called from `ClaudeMembrane.pre_launch` **ahead of** the
WT-relaunch fallback. win32-only + requires `tmux` on PATH; otherwise returns
False and the existing WT path (and `SWARPH_WIN_ACK`) is untouched. Linux/mac
peers are entirely unaffected.

- **session name** = the operator-typed `swarph spawn <name>` positional
  (`effective_role or cell.role`), so spawn / attach / `send-keys` all key off
  one string.
- **create**: `tmux new-session -d -s <name> -c <cwd> -e SWARPH_SPAWN=1 swarph
  spawn <name>` — the session command re-enters spawn so the membrane
  env-scrub + starter-injection apply once inside the pane.
- **attach (interactive)**: **blocking `subprocess.run([tmux, "attach", "-t",
  <name>])`** in the launching console. The PowerShell window *becomes* the
  cell's viewport. (PowerShell renders the TUI's UTF-8 better than cmd's cp1252.)
- **headless** (watchdog A2 respawn / CI / piped): create detached, **no attach**
  — sidecar/watchdog reach it via `send-keys`.

### Two load-bearing implementation lessons

- **Loop safety.** The session command re-enters `swarph spawn <name>`, but that
  inner process runs with `$TMUX` set, so it skips both the tmux decision *and*
  (via a new `$TMUX` guard added to `_relaunch_in_windows_terminal`) the WT
  relaunch — falling straight through to `launch()`'s in-place `execve`.
  `SWARPH_SPAWN=1` in the session env is belt-and-suspenders. No recursion.
- **Attach must be a blocking child, NOT `os.execv`.** This path is win32-only,
  and on Windows `os.exec*` is emulated as spawn-and-exit, so the parent
  PowerShell regains the console and fights the attaching tmux → claude renders
  but input is garbled (observed). A blocking `subprocess.run` keeps ONE console
  shared `PowerShell → swarph → tmux`, identical to a manual `tmux attach`.

## Dependency (Windows)

**psmux** — Windows-native tmux. **Soft/optional**: swarph calls `tmux`
generically; absent → WT-relaunch fallback. Any tmux-compatible CLI on PATH
satisfies it; psmux is the Windows-native one.

- Upstream: https://github.com/psmux/psmux
- Install (friendly): `winget install psmux`
- Install (scripted, unambiguous — `psmux.TerminalMap` shares the prefix):
  `winget install -e --id marlocarlo.psmux`

> **For Lab to decide:** bless `marlocarlo.psmux` as the official Windows
> multiplexer, or document "any `tmux` on PATH." Code is agnostic either way.

## Tests

`tests/test_spawn_tmux_session.py` (decision matrix: skip-gates, create-vs-attach,
interactive-vs-headless, failure→fallback) + `test_spawn_windows_relaunch.py`
(`$TMUX` skips the WT relaunch). **23/23 green**; full `test_spawn_command.py`
52/52 green.

## Recommended next step — generalize beyond Windows (Lab to own)

The single-command UX (`swarph spawn <name>` → create session + launch claude +
attach) should be **the behavior on every OS**, not just Windows. The branch
gates `_launch_via_tmux` to win32 **only to avoid changing the Linux peers
(lab-ovh) without review** — the logic itself is OS-agnostic.

To generalize (Lab's call, since it changes lab-ovh's own cells and needs a
Linux box to validate):

- Drop / widen the `sys.platform != "win32"` guard in `_launch_via_tmux`.
- **Attach per-OS**: keep the blocking `subprocess.run` on Windows (os.exec* is
  broken there); on Linux/mac use `os.execv` for a true in-place replacement
  (cleaner — no intermediate process). A small platform branch.
- Confirm the create command (`-e`, `-c`) against the tmux build on each peer.
- Decide the "already inside tmux" policy uniformly (current: in-place exec — the
  loop-breaker — which is correct for the `tmux new` → `swarph spawn` workflow).

### ✅ DONE — generalized to all-OS (lab, 2026-06-12)

All four points above shipped. `_launch_via_tmux` is now the single-command UX on
every OS:

- **win32 guard dropped.** The `$TMUX` loop-breaker + `SWARPH_SPAWN` guard +
  tmux-on-PATH check stay (all OS-agnostic).
- **Per-OS attach.** POSIX (Linux/mac) → `os.execv` (true in-place replace);
  Windows → the blocking `subprocess.run` (lc's `c86beb5`, now the Windows branch
  of a two-branch split).
- **Create command confirmed on a Linux box.** Live on lab-ovh (**tmux 3.5a**):
  the `new-session -d -s -c -e CMD` argv returns rc 0, has-session +
  exact-match `=` prefix behave, and the `-e SWARPH_SPAWN` env **actually lands
  in the pane** (the loop-breaker mechanism, verified on real tmux). Graceful
  degradation if a peer's tmux predates `-e` (< 3.0): create fails → return
  False → fall back to the standard launch, no half-state.
- **In-tmux policy is uniform + composes with capture-at-birth.** `$TMUX` set →
  exec in place (never re-create), so the `claude-tmux@.service` template's inner
  `swarph spawn %i` falls straight through.

**Validation:** 822 tests green (all-OS decision matrix incl. win32-vs-POSIX
attach, the test harness now mocks `os.execv` with a sentinel so the POSIX path
can never fire a real exec) + the live lab-ovh tmux smoke above. **Residual
(named):** the interactive `os.execv` attach on POSIX is the one path not
live-tested — it needs an interactive TTY (a non-interactive shell/CI can't
provide one) — but it's unit-covered and a stdlib primitive; same "validate on
metal" class lc closed for Windows.

## Open items (NOT blockers for this branch)

- **Mouse-tracking leak** — under psmux, claude's SGR-1006 mouse codes leak/mangle
  into stdin (host-independent: PowerShell/cmd/git-bash identical). This is a
  **separate psmux bug**, not a spawn bug — the rendering + attach here are
  correct. Track as its own psmux ticket.
- **Runtime hint** for missing psmux on Windows — docs-only for now; could add a
  one-line "`winget install psmux` for a supervised session" on the WT-fallback
  path (gated to avoid noise).
