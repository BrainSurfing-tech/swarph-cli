# Pluggable liveness for `swarph watchdog` — design

**Date:** 2026-07-04
**Component:** `swarph-cli` — `src/swarph_cli/commands/watchdog.py`
**Status:** design approved; plan + implementation to follow on branch `feat/watchdog-pluggable-liveness`.

## Problem

`swarph watchdog --check` recovers a cell that has gone dormant or throttle-stranded: a stale cursor plus a *live* session triggers an A1 `tmux send-keys` wake; a stale cursor plus a *dead* session triggers the destructive A2 respawn (`swarph spawn <role>`, which kills the tmux session and respawns from cell-yaml).

The liveness decision is made by `_process_alive(tmux_session)` (`watchdog.py:583`), which runs `pgrep -f "claude"` and keeps only PIDs descending from the session's panes (`_pid_under`). **The process name `"claude"` is hardcoded.**

Consequence for non-Claude cells:

- `gpt-ops` runs `node` (codex), `grok-researcher` runs `grok`. Neither has a `claude` process under its panes.
- Whenever such a cell's cursor is stale — the *only* moment recovery matters — `_process_alive` returns `False`, so the watchdog takes the A2 "process dead" branch.
- Under `--no-respawn` that branch is inert: it returns exit 2 and does nothing (fake coverage — logs `a2_dry_run` every 5 minutes but never wakes the cell).
- *Without* `--no-respawn` it is destructive: it kills the live `grok`/`node` session and respawns from cell-yaml.

Measured 2026-07-04: gridiron (Claude) `--check` exited 0 (healthy); gpt-ops and grok-researcher both exited 2 (`a2_respawn_process_dead` under `--no-respawn`). Confirmed no existing override flag (`--model-rung`/`--no-model-rung`/`--stable-model` are the only liveness-adjacent flags, all Claude-specific).

There is a second, related coupling: `_resolve_send_target(name)` (`watchdog.py:641`) picks *where* the A1 wake lands by preferring a pane whose current command looks like `node`/`claude` — so even a cell that passes liveness could have its wake mis-land on a bash/log pane.

## Goal

Let an operator tell the watchdog what "alive" means for a given cell, so the watchdog can recover a non-Claude cell's dormancy without the Claude-coupling — while leaving every existing Claude cron byte-for-byte unchanged.

## Design

### Two new `--check` flags (mutually exclusive)

- **`--process-name NAME`** *(default `"claude"`)* — the string handed to `pgrep -f`, still scoped to the session's pane PIDs via the existing `_pid_under`. The default preserves today's behavior exactly.
- **`--liveness-cmd CMD`** — an escape hatch for the rare cell whose liveness a process name cannot express. Run `CMD`; **exit 0 = alive**, non-zero = dead.
  - **On timeout / `OSError` → assume alive.** This preserves `_process_alive`'s existing fail-safe (a broken detector must never false-fire the destructive A2) and honors the standing heuristic *never gate a destructive or blocking action on an uncertain probe*.
  - Runs with a bounded timeout (the module's existing 5s subprocess pattern).

The two flags are an argparse **mutually-exclusive group** — passing both is a usage error (fail-fast; no ambiguous precedence to reason about).

### Thread the process identity into both coupling points

Two functions carry the Claude assumption; both take exactly one caller, so threading a parameter through is contained:

- `_process_alive(tmux_session, process_name="claude")` — the `pgrep -f` string.
- `_resolve_send_target(name, process_name="claude")` — the preferred-pane-command match honors `process_name`, so the A1 wake lands on the agent pane (not a bash/log pane) for a non-Claude cell too. When `--liveness-cmd` is used there is no process name, so send-target keeps today's `node`/`claude` + active-pane heuristic.

The `--check` handler resolves the liveness signal:

```
if args.liveness_cmd:      liveness = (run args.liveness_cmd, rc == 0, assume-alive on error)
else:                      liveness = _process_alive(tmux_session, args.process_name)
send_target                = _resolve_send_target(session, args.process_name)   # default "claude" under --liveness-cmd
```

Everything downstream of the liveness boolean (the A1/A2 decision matrix, markers, respawn) is unchanged.

### Backward compatibility — the load-bearing invariant

Default `process_name="claude"` and no `--liveness-cmd` → the four live crons (`lab`, `drop-on-meta-edge`, `science-claude`, `gridiron`) are unaffected. A test locks the default `pgrep` argument as `"claude"` so a future refactor cannot silently change it.

## Testing (TDD)

Extend the existing watchdog suite (`tests/test_watchdog.py`; the suite already establishes the subprocess-mock pattern):

- **Compat lock:** default (no new flags) → `_process_alive` issues `pgrep -f claude`.
- **`--process-name grok`** → `_process_alive` issues `pgrep -f grok`.
- **`--liveness-cmd` semantics:** rc 0 → alive; rc 1 → dead; timeout / `OSError` → alive (fail-safe).
- **Mutual exclusion:** `--process-name` + `--liveness-cmd` together → `SystemExit` (argparse usage error).
- **Send-target:** `_resolve_send_target` with `process_name="grok"` prefers the `grok` pane over a bash pane.

## Ship & rollout

- Version bump `0.24.0 → 0.25.0`; document both flags in `watchdog --help` and the changelog.
- swarph-cli is **public PyPI** — synthetic test fixtures only, no cell-private data.
- After publish, re-wire the two crons removed on 2026-07-04:
  - `grok-researcher` → add `--process-name grok`
  - `gpt-ops` → add `--process-name node`
  (exact per-cell string chosen at rollout; `_pid_under` scoping keeps a generic name like `node` correct because only PIDs under that cell's panes count.)
- **Publish and cron re-wire are commander-gated.**

## Out of scope (YAGNI)

- **Cell-yaml auto-resolution** of the process name (the watchdog reading a cell's spawn config to infer its process). A future DRY consolidation; the CLI flag matches today's cron config surface.
- The A2 respawn mechanism itself (`_spawn_via_swarph`) — unchanged.
- Option (b), the model-agnostic **sidecar-wake-with-verify** fix (C-u clear-input + turn-marker verify). That addresses doorbell-on-DM delivery, a *different* case than dormancy recovery, and is tracked separately.

## Invariants preserved

- Liveness stays a **real probe**, never a stale-cursor proxy, for the destructive A2 gate.
- A broken/uncertain detector **fails toward "alive"** (no false-positive respawn).
- Default behavior is **unchanged** for every currently-wired Claude cell.
