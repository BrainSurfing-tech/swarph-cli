# Liveness marker fresh through a long turn (#2 / mesh-hygiene)

**Status:** spec (commander: "lab up #2", 2026-07-13). Precondition for safe auto-boot —
before any cell drops `--no-respawn`. Discovered by droplet; analysis converged (DMs 4622/4623).

## 1. Problem

The watchdog's liveness = freshest mtime of {drain-cursor, **activity-marker**}. The marker
(`$TMPDIR/<role>-claude-active.txt`) is touched by a **Stop** hook — i.e. only at **turn-end**.
During a long *autonomous* turn (a multi-hour SDD run = one continuous turn, no turn-end), the
marker goes stale, the drain-cursor is also stale (not draining), and F3 pane-activity can go
stale too. Result: a **false-stall** → the watchdog fires A1 (send-keys a wake INTO the working
session) — and A1 is **not** gated by `--no-respawn`, so it disrupts even a shadow-staged cell.
droplet proved this live (marker 6.5h stale mid-turn). **This is the hard precondition before
any cell can safely drop `--no-respawn` and let A2 actually recover cells.**

## 2. Fix

Touch the marker on **PreToolUse** (every tool call), not just Stop. An actively-working cell
makes tool calls throughout a long turn → the marker stays fresh → no false-stall. Turn it from
an ad-hoc manual Stop hook into a proper **swarph-cli bundled hook** so every cell gets it.

## 3. The three constraints a naive fix breaks (why this is subtle)

1. **Shared-user box:** lab-ovh runs ~6 cells under one `ubuntu` user → ONE
   `~/.claude/settings.json`. A user-level hook fires for EVERY cell.
2. **Project hooks are silently ignored** (Claude Code ≥2.1.186 — proven with science-claude:
   a `<cwd>/.claude/settings.local.json` Stop hook NEVER fired across a reload + reboot). So the
   hook MUST be user-level; a per-cell project hook is a dead end.
3. **SWARPH_CELL is contaminated** (#20): the shared `.bashrc` sets it last-wins = `gridiron`
   for all. So a hook resolving `${SWARPH_CELL}-claude-active.txt` touches the WRONG cell's
   marker. **A wrong-path touch is WORSE than no fix** — it makes a dead cell look alive (marker
   fresh from another cell's tool calls) → the watchdog never recovers it. The recovery layer
   must fail conservative; a mis-resolved marker fails aggressive.

## 4. Design — resolve by CWD, agree with the watchdog by construction

The one per-cell-reliable signal on a shared-user box is **cwd**: each cell's Claude session
runs in its own project dir, and hooks run in that cwd. So:

- New subcommand **`swarph hooks touch-activity`**: resolves the marker path using the SAME
  code the watchdog uses (`discover_cell_in_cwd()` → role → `_resolve_activity_marker_path`),
  then `touch`es it. Because hook and watchdog call the identical resolver, they AGREE by
  construction — no path drift possible. cwd-keyed → per-cell-correct despite SWARPH_CELL
  contamination.
- New builtin **`activity-marker`** HookBundle: a tiny script that runs
  `swarph hooks touch-activity` (best-effort, always exit 0 — a liveness hook must never fail a
  turn), bound to **PreToolUse (`""` = all tools)** + **Stop** + **StopFailure**.
- Fallback when no cell.yaml in cwd: use `${TMPDIR:-/tmp}/${SWARPH_CELL:-lab}-claude-active.txt`
  (the watchdog's own default), same formula → still agrees. (Weaker per-cell guarantee if
  SWARPH_CELL is wrong, but only for cells lacking a cell.yaml; cell.yaml is the reliable path.)

## 5. Scope / deployment discipline

- **BUILD (safe, this session):** the `touch-activity` subcommand + the `activity-marker`
  bundle + tests. Additive, touches NO live cell.
- **DO NOT auto-install onto live cells** — that's a `~/.claude/settings.json` change on ~6
  running cells (same risk class as #20), NOT to be done unsupervised. Ship the CAPABILITY
  (`swarph hooks install activity-marker`); let **droplet** validate it on its own box as part
  of its watchdog staging (it's the one cell already proving the long-turn failure), then roll
  out with the commander's eye.
- PreToolUse fires on every tool call; the touch is a microsecond `touch` — negligible latency.

## 6. Tests

- `touch-activity` resolves the marker path via cell.yaml-in-cwd → touches exactly the path the
  watchdog would read for that cwd (assert agreement by calling both resolvers).
- Fallback to `$TMPDIR/<role>-claude-active.txt` when no cell.yaml.
- Never non-zero exit even on an unwritable path (a liveness hook must not fail the turn).
- The `activity-marker` bundle binds PreToolUse + Stop + StopFailure; the script calls
  `swarph hooks touch-activity`.
- `swarph hooks install activity-marker` merges the three bindings into settings.json.
