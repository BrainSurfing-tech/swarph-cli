# Spawn tmux base-hoist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `swarph spawn`'s named-tmux launch provider-non-discriminatory by hoisting it into the base `ProviderMembrane.pre_launch`, so codex + antigravity get a tmux session like claude + grok already do.

**Architecture:** A hoist + a dedup in one file. The session-gated tmux branch currently duplicated in `ClaudeMembrane.pre_launch` and `GrokMembrane.pre_launch` moves into the base `ProviderMembrane.pre_launch`; Claude delegates to `super()` then keeps only its Windows-Terminal-relaunch extra; Grok's now-identical override is deleted. Codex/Antigravity (no override) inherit the base and gain tmux automatically. `_launch_via_tmux` and its `$TMUX` loop-breaker are untouched.

**Tech Stack:** Python 3 stdlib only. pytest.

**Board:** spawn-tmux project, card #2. Branch `feat/spawn-tmux-base-hoist` off `main` in `/home/ubuntu/swarph-cli` (PUBLIC repo).

## Global Constraints

- Provider-non-discriminatory: every membrane gets tmux-when-named via the BASE `pre_launch`, not per-membrane overrides.
- `session_name` is the interim per-spawn opt-out (unnamed spawn → no tmux); the none/tmux/service onboarding tier is a separate later card, out of scope.
- Claude keeps ONLY its Claude-specific extra (the `_relaunch_in_windows_terminal` fallback + legacy-conhost warning, for its Ink-TUI/PowerShell bug), reached via `super().pre_launch(...)` then its existing body — unchanged behavior.
- `GrokMembrane.pre_launch` is deleted (it is byte-equivalent to the base tmux branch — verified: no extra beyond the branch + return); `GrokMembrane.launch()` is untouched.
- Codex/Antigravity inherit the base unchanged — no provider-specific extra (all provider TUIs render fine under a plain tmux PTY; validated live — all mesh peers run in tmux).
- `_launch_via_tmux` (spawn.py:1059), its `$TMUX` loop-breaker, and per-OS attach (`os.execve` POSIX / blocking `subprocess.run` win32) are UNCHANGED.
- No double-tmux via the external `claude-tmux@.service` template — the `$TMUX` loop-breaker inside `_launch_via_tmux` handles that (a cell already in tmux re-enters with `$TMUX` set → in-place exec).
- Unnamed spawns are byte-unchanged (base returns `None` when `session_name` is falsy).
- Stdlib only. PUBLIC repo → topology-free commit + PR bodies.
- Merge is COMMANDER-GATED (changes live spawn behavior): PR returns for commander nod, NO auto-merge.

## File Structure

- `src/swarph_cli/commands/spawn.py` — MODIFY three membranes: `ProviderMembrane.pre_launch` (add tmux branch), `ClaudeMembrane.pre_launch` (delegate to super, keep WT extra), delete `GrokMembrane.pre_launch`.
- `tests/test_spawn_base_hoist.py` — CREATE: membrane-dispatch tests (mock `_launch_via_tmux` + `_relaunch_in_windows_terminal`; assert which membranes call tmux and how they handle the return). Does NOT re-test `_launch_via_tmux` internals (covered by `tests/test_spawn_tmux_session.py`).

---

### Task 1: Hoist tmux launch to the base membrane (+ Claude delegate, Grok delete)

**Files:**
- Modify: `src/swarph_cli/commands/spawn.py` (`ProviderMembrane.pre_launch` ~1226; `ClaudeMembrane.pre_launch` ~1275; delete `GrokMembrane.pre_launch` ~1483)
- Test: `tests/test_spawn_base_hoist.py` (create)

**Interfaces:**
- Consumes: `spawn._launch_via_tmux(binary: str, argv: list[str], cwd, session_name: str) -> bool` (unchanged); `spawn._relaunch_in_windows_terminal(binary, argv, cwd) -> bool` (unchanged); `spawn.ProviderMembrane`, `ClaudeMembrane`, `GrokMembrane`, `CodexMembrane`, `AntigravityMembrane`.
- Produces: base `ProviderMembrane.pre_launch` now returns `0` when `session_name` set and `_launch_via_tmux` succeeds, else `None`; `ClaudeMembrane.pre_launch` returns the base result if not `None`, else its WT-relaunch/conhost result; `GrokMembrane` no longer defines `pre_launch`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spawn_base_hoist.py`:

```python
"""Provider-non-discriminatory tmux launch: the named-tmux branch lives in the
BASE ProviderMembrane.pre_launch, so every membrane (claude/codex/antigravity/
grok) gets a session when spawned with a name. Claude keeps only its extra WT
relaunch. These tests pin the membrane DISPATCH by mocking _launch_via_tmux and
_relaunch_in_windows_terminal — they do NOT exercise _launch_via_tmux internals
(that's tests/test_spawn_tmux_session.py)."""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

from swarph_cli.commands import spawn

BIN = "/usr/bin/claude"
ARGV = ["claude", "--name", "lab"]


def _cell():
    return types.SimpleNamespace(cwd=Path("/home/ubuntu/lab"))


def _call(membrane, monkeypatch, *, session_name, tmux_ok=True, wt_ok=False):
    launch = MagicMock(return_value=tmux_ok)
    wt = MagicMock(return_value=wt_ok)
    monkeypatch.setattr(spawn, "_launch_via_tmux", launch)
    monkeypatch.setattr(spawn, "_relaunch_in_windows_terminal", wt)
    rc = membrane.pre_launch(_cell(), BIN, ARGV, no_banner=True, session_name=session_name)
    return rc, launch, wt


def test_base_launches_tmux_when_named(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once_with(BIN, ARGV, Path("/home/ubuntu/lab"), "lab")


def test_base_returns_none_when_unnamed(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name=None)
    assert rc is None
    launch.assert_not_called()


def test_base_returns_none_when_tmux_declines(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=False)
    assert rc is None
    launch.assert_called_once()


def test_claude_takes_base_tmux_without_reaching_wt(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once()
    wt.assert_not_called()          # base took over → Claude's WT path not reached


def test_claude_reaches_wt_when_base_declines(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=False, wt_ok=True)
    assert rc == 0                  # WT relaunch took over
    wt.assert_called_once()


def test_grok_has_no_own_pre_launch():
    # Grok's override is deleted → resolves to the base implementation.
    assert spawn.GrokMembrane.pre_launch is spawn.ProviderMembrane.pre_launch


def test_codex_and_antigravity_inherit_base_tmux(monkeypatch):
    for key in ("codex", "antigravity"):
        m = spawn.MEMBRANES[key]
        assert type(m).pre_launch is spawn.ProviderMembrane.pre_launch
        rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
        assert rc == 0, key
        launch.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && python3 -m pytest tests/test_spawn_base_hoist.py -v`
Expected: FAIL — `test_base_launches_tmux_when_named` fails (base `pre_launch` returns `None`, not `0`); `test_grok_has_no_own_pre_launch` fails (Grok still defines its own); `test_codex_and_antigravity_inherit_base_tmux` fails (base returns `None`). (`test_base_returns_none_when_unnamed` may already pass — fine.)

- [ ] **Step 3: Edit base `ProviderMembrane.pre_launch`**

In `src/swarph_cli/commands/spawn.py`, the base `ProviderMembrane.pre_launch` body is currently just `return None`. Replace that body (keep the signature + docstring) with:

```python
        # A named spawn runs the cell in a tmux session (durable + send-keys-
        # supervisable), for EVERY provider. No-op when unnamed / already inside
        # tmux / tmux absent (then fall through to None). tmux's PTY answers the
        # TUI's terminal queries so every provider's TUI renders correctly.
        if session_name and _launch_via_tmux(binary, argv, cell.cwd, session_name):
            return 0
        return None
```

- [ ] **Step 4: Edit `ClaudeMembrane.pre_launch` — delegate to super, keep the WT extra**

Its current body starts with the tmux branch (`if session_name and _launch_via_tmux(...): return 0`) followed by the `_relaunch_in_windows_terminal` fallback and the legacy-conhost warning. Replace ONLY the leading tmux branch with a `super()` delegation; leave the WT-relaunch fallback and the conhost-warning block that follow it VERBATIM:

```python
        # tmux launch is now provider-generic (base). Claude keeps ONLY its extra:
        # the Windows-Terminal relaunch + legacy-conhost warning for the Ink-TUI/
        # PowerShell Enter-inserts-'m' bug that is Claude-specific.
        rc = super().pre_launch(
            cell, binary, argv, no_banner=no_banner, session_name=session_name
        )
        if rc is not None:
            return rc

        # conhost TUI auto-fix fallback (no tmux): ... [KEEP THE EXISTING
        # _relaunch_in_windows_terminal(...) BLOCK AND THE conhost-warning BLOCK
        # AND THE FINAL `return None` EXACTLY AS THEY ARE].
```

Do not modify `_relaunch_in_windows_terminal`, the warning text, or the final `return None`.

- [ ] **Step 5: Delete `GrokMembrane.pre_launch`**

Remove the entire `GrokMembrane.pre_launch` method (the `def pre_launch(...)` through its `return None`). Leave `GrokMembrane.launch()` and every other Grok method untouched. Grok now inherits `ProviderMembrane.pre_launch`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /home/ubuntu/swarph-cli && python3 -m pytest tests/test_spawn_base_hoist.py -v`
Expected: PASS (7 passed).

- [ ] **Step 7: Run the full spawn + tmux suites for non-regression**

Run: `cd /home/ubuntu/swarph-cli && python3 -m pytest tests/test_spawn_command.py tests/test_spawn_tmux_session.py tests/test_spawn_windows_relaunch.py tests/test_spawn_base_hoist.py -q`
Expected: all pass. In particular the Windows-relaunch tests still pass (Claude's WT behavior is unchanged) and the tmux-session tests still pass (`_launch_via_tmux` untouched).

- [ ] **Step 8: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/spawn.py tests/test_spawn_base_hoist.py
git commit -m "feat(spawn): hoist tmux launch to base membrane (all providers get a session)"
```

---

## Done criteria

Green tests on `feat/spawn-tmux-base-hoist` (new file + the spawn/tmux/windows suites). No behavior change for claude (WT extra intact) or unnamed spawns; codex + antigravity now get a named tmux session; grok unchanged behaviorally (inherits the identical base). Merge is commander-gated — PR returns for the nod, no auto-merge. Deploy = none (pip package; consumers pick it up on the next release, a separate step).
