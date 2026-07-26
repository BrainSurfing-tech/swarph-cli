# Spawn tmux base-hoist — design

**Date:** 2026-07-21
**Status:** approved, ready for planning
**Board:** spawn-tmux project, card #2 "Hoist `_launch_via_tmux` to base membrane"

## Goal

Make the internal tmux-session launch on `swarph spawn` **provider-non-discriminatory**: every
provider membrane gets a named tmux session by default (when a spawn name is given), instead of only
the two membranes that happen to override `pre_launch`.

## Problem (current 0.34.0 behavior)

tmux-launch is opt-in *per membrane*, not in the base — so it discriminates by provider:

- `ProviderMembrane.pre_launch` (base) → `return None` (no tmux).
- `ClaudeMembrane.pre_launch` → overrides: `if session_name and _launch_via_tmux(...): return 0`,
  then a Windows-Terminal-relaunch fallback + a legacy-conhost warning.
- `GrokMembrane.pre_launch` → overrides: the *same* `_launch_via_tmux` call — a duplicate of Claude's
  tmux branch (minus the WT parts).
- `CodexMembrane`, `AntigravityMembrane` → **no `pre_launch` override** → inherit the base
  `return None` → spawn **bare** (no tmux session, no `send-keys` supervision).

So `swarph spawn <codex|antigravity cell>` runs without a session. In production these providers only
get tmux because they're launched via the provider-agnostic external `claude-tmux@.service` template,
which masks the gap — an operator spawning them interactively gets no session.

The OS-generalization already landed independently: the tmux call site has no `win32` gate, and the
per-OS mechanics (`os.execve` on POSIX, blocking `subprocess.run` on Windows) live inside
`_launch_via_tmux` / `launch()`. This design changes **only** the provider dimension.

## Design

A hoist plus a dedup — it generalizes the *existing* session-gated behavior, adds no new machinery.

### 1. Base `ProviderMembrane.pre_launch` — do the session-gated tmux launch

Replace the base's `return None` with the branch currently duplicated in Claude/Grok:

```python
def pre_launch(self, cell, binary, argv, *, no_banner, session_name=None):
    # A named spawn runs the cell in a tmux session (durable + send-keys-supervisable).
    # No-op when unnamed / already inside tmux / tmux absent (then fall through to None).
    if session_name and _launch_via_tmux(binary, argv, cell.cwd, session_name):
        return 0
    return None
```

All four membranes now get tmux-when-named by default. The `session_name` gate is the interim
per-spawn opt-out (spawn unnamed → no session). A richer none/tmux/service onboarding tier is a
**separate later card**, out of scope here.

### 2. `ClaudeMembrane.pre_launch` — keep only the Claude-specific extra

Delegate the tmux part to the base, then add Claude's own fallback (the WT-relaunch + conhost warning
exist only for Claude's Ink-TUI/PowerShell rendering bug):

```python
def pre_launch(self, cell, binary, argv, *, no_banner, session_name=None):
    rc = super().pre_launch(cell, binary, argv, no_banner=no_banner, session_name=session_name)
    if rc is not None:
        return rc
    # Claude-only: relaunch in Windows Terminal / warn on legacy conhost. (unchanged body)
    ...
```

### 3. `GrokMembrane.pre_launch` — delete

Once the base carries the tmux branch, Grok's override is byte-identical to the base → remove it;
Grok inherits the base. Closes the previously-flagged Claude/Grok duplication.

### 4. `CodexMembrane`, `AntigravityMembrane` — unchanged

No override added; they inherit the new base tmux behavior automatically. Their `launch()` (execve)
is untouched. No provider-specific extra is needed — every provider's TUI renders correctly under a
plain tmux PTY (validated live: all mesh peers already run inside tmux).

## Safety / non-regression

- **No double-tmux via the external template:** the `$TMUX` loop-breaker inside `_launch_via_tmux`
  short-circuits to an in-place exec when already inside a tmux session. A cell launched by the
  `claude-tmux@.service` template (which wraps it in tmux, then runs `swarph spawn` inside) re-enters
  with `$TMUX` set → in-place exec, no second session. Adding internal tmux to codex/antigravity does
  not double-wrap the template path.
- **Claude behavior unchanged:** the WT-relaunch + conhost warning still fire in the same
  circumstances (only when the base tmux branch returned `None`).
- **Unnamed spawns unchanged:** `session_name` unset → base returns `None` → current bare behavior.

## Testing

- Base `pre_launch` calls `_launch_via_tmux(binary, argv, cwd, session_name)` and returns `0` when
  `session_name` is set and the launch succeeds; returns `None` when `session_name` is unset.
- `ClaudeMembrane.pre_launch` returns the base's `0` when the base took over (no WT-relaunch reached);
  and, when the base returns `None`, still reaches its WT-relaunch/conhost path.
- `GrokMembrane` has no own `pre_launch` (resolves to `ProviderMembrane.pre_launch`).
- `CodexMembrane` / `AntigravityMembrane` resolve `pre_launch` to the base and take the tmux path when
  named.
- `$TMUX` set → `_launch_via_tmux` does the in-place exec branch (no second session created).
- Mock `_launch_via_tmux` (and the WT-relaunch helper) — assert call/return, not real process launch.

## Scope / constraints

- Single repo (`swarph-cli`), single file (`src/swarph_cli/commands/spawn.py`) + its tests.
- Stdlib only (no new deps).
- Changes live spawn behavior → ships as a reviewed PR; **merge is commander-gated** (no auto-merge).
- Onboarding-tier selector (none / tmux / tmux+service) is a separate later card, not this one.
