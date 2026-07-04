# Pluggable Liveness for `swarph watchdog` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un-couple the watchdog's A2 liveness gate from a hardcoded `pgrep "claude"` via two mutually-exclusive `--check` flags (`--process-name`, `--liveness-cmd`), so non-Claude cells can be recovered without being mis-read as dead — while every existing Claude cron stays byte-for-byte unchanged.

**Architecture:** Two functions carry the Claude assumption — `_process_alive` (the pgrep liveness gate) and `_resolve_send_target` (which pane the A1 wake lands on). Each takes a new `process_name` parameter (default `"claude"`). A new `--liveness-cmd` escape hatch runs an arbitrary probe (rc 0 = alive) via a small helper that fails toward "alive" on error. Both flags live in one argparse mutually-exclusive group. The `--check` handler branches on which was given. Nothing downstream of the liveness boolean changes.

**Tech Stack:** Python 3, argparse, `subprocess`; pytest with `unittest.mock.patch` (the existing `tests/test_watchdog.py` pattern).

## Global Constraints

- **Default `process_name="claude"`** everywhere — no flags → identical behavior to today. This is the load-bearing backward-compat invariant; a test locks the default `pgrep` arg as `"claude"`.
- **`--process-name` and `--liveness-cmd` are mutually exclusive** — one argparse `add_mutually_exclusive_group()`; passing both is a usage error (`SystemExit`).
- **Fail toward alive:** the `--liveness-cmd` path returns `True` (alive) on timeout / `OSError`, mirroring `_process_alive`'s existing fail-safe — a broken/uncertain detector must never false-fire the destructive A2 respawn.
- **Liveness stays a real probe**, never a stale-cursor proxy, for the destructive gate.
- **Public PyPI** — synthetic test fixtures only, no cell-private data.
- **Version bump `0.24.0 → 0.25.0`** in BOTH `pyproject.toml:7` and `src/swarph_cli/__init__.py:19`.
- **TDD** — every change is test-first; the plan ends at merged + green. Publish to PyPI and cron re-wire (`--process-name grok` / `node`) are commander-gated and OUT of this plan's execution scope.
- Branch `feat/watchdog-pluggable-liveness` already exists and is checked out. Stage only the specific files each commit names — never `git add -A` (the tree has an untracked local-only `.codegraph/`).

## File Structure

- `src/swarph_cli/commands/watchdog.py` — all production changes:
  - `_process_alive(tmux_session, process_name="claude")` (currently `:583`) — pgrep string.
  - `_resolve_send_target(name, process_name="claude")` (currently `:641`) — preferred-pane match.
  - `_tmux_send_keys(name, text, clear_input=False, process_name="claude")` (currently `:668`) — passes `process_name` to `_resolve_send_target`. Two call sites (`:1447`, `:1490`).
  - new `_liveness_via_cmd(cmd)` helper.
  - `_build_parser()` (`:1687`) — the mutually-exclusive group.
  - `--check` handler liveness call site (`:1272`).
- `tests/test_watchdog.py` — extend with the new cases (existing fixtures: `isolated_state`, `stale_cursor`, `fresh_cursor`; existing mock idiom: `patch("swarph_cli.commands.watchdog.<fn>", ...)`).
- `pyproject.toml`, `src/swarph_cli/__init__.py` — version bump.

---

### Task 1: `--process-name` flag threaded into the liveness gate

**Files:**
- Modify: `src/swarph_cli/commands/watchdog.py` (`_process_alive` `:583`; `_build_parser` `:1687`; handler call site `:1272`)
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Produces: `_process_alive(tmux_session: str, process_name: str = "claude") -> bool` — issues `pgrep -f <process_name>`, unchanged pane-scoping via `_pid_under`.
- Produces: argparse `--process-name` (default `"claude"`), added inside a new `add_mutually_exclusive_group()` named `liveness_group` (Task 2 adds `--liveness-cmd` to the same group).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_watchdog.py`:

```python
from types import SimpleNamespace


def _fake_run_factory(calls, pgrep_rc=1):
    """subprocess.run stub: records argv, answers the two calls _process_alive makes."""
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "list-panes"]:
            return SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
        if cmd and cmd[0] == "pgrep":
            # rc != 0 → _process_alive returns False before _pid_under; we only
            # assert the pgrep ARG here, not the liveness verdict.
            return SimpleNamespace(returncode=pgrep_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_run


def test_process_alive_default_greps_claude():
    from swarph_cli.commands import watchdog
    calls = []
    with patch("swarph_cli.commands.watchdog.subprocess.run", _fake_run_factory(calls)):
        watchdog._process_alive("some-session")
    pgrep_calls = [c for c in calls if c and c[0] == "pgrep"]
    assert pgrep_calls == [["pgrep", "-f", "claude"]]


def test_process_alive_honors_process_name():
    from swarph_cli.commands import watchdog
    calls = []
    with patch("swarph_cli.commands.watchdog.subprocess.run", _fake_run_factory(calls)):
        watchdog._process_alive("some-session", process_name="grok")
    pgrep_calls = [c for c in calls if c and c[0] == "pgrep"]
    assert pgrep_calls == [["pgrep", "-f", "grok"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py::test_process_alive_honors_process_name -v`
Expected: FAIL — `TypeError: _process_alive() got an unexpected keyword argument 'process_name'`.

- [ ] **Step 3: Add the `process_name` parameter to `_process_alive`**

In `src/swarph_cli/commands/watchdog.py`, change the signature and the pgrep line. Current (`:583`, `:617-618`):

```python
def _process_alive(tmux_session: str) -> bool:
    """Detect if a claude process is running INSIDE the named tmux session.
    ...
    """
```
```python
        pg = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=5,
        )
```

New:

```python
def _process_alive(tmux_session: str, process_name: str = "claude") -> bool:
    """Detect if a `process_name` process is running INSIDE the named tmux session.

    `process_name` (default "claude") is the command the cell's agent runs —
    node/codex cells pass "node", grok cells pass "grok". Scopes to the
    session's pane PIDs (and descendants) rather than a host-wide pgrep: on a
    multi-session host, an unrelated cell's process would otherwise mask THIS
    session's death and suppress the A2 alert. Best-effort; falls back to True
    (assume alive) on detection error so a broken detector never false-fires A2.
    """
```
```python
        pg = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True, text=True, timeout=5,
        )
```

- [ ] **Step 4: Add `--process-name` to the parser inside a mutually-exclusive group**

In `_build_parser()`, immediately before `p.add_argument("--no-respawn", ...)` (`:1725`), insert:

```python
    liveness_group = p.add_mutually_exclusive_group()
    liveness_group.add_argument(
        "--process-name", default="claude",
        help="Process the cell's agent runs, used by the liveness gate's "
             "`pgrep -f` (scoped to the session's panes). Default 'claude'; "
             "pass 'node' for a codex cell, 'grok' for a grok cell so a "
             "non-Claude cell isn't mis-read as dead. Mutually exclusive with "
             "--liveness-cmd.",
    )
```

- [ ] **Step 5: Pass `args.process_name` at the handler call site**

At `:1272`, change:

```python
    process_alive = _process_alive(tmux_session)
```
to:
```python
    process_alive = _process_alive(tmux_session, args.process_name)
```

- [ ] **Step 6: Run the new tests + the full watchdog suite**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py -v`
Expected: PASS — the two new tests pass and every pre-existing watchdog test still passes (the compat lock proves the default is unchanged).

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): --process-name threads the liveness gate's pgrep

Default 'claude' → existing crons unchanged (compat-locked by test)."
```

---

### Task 2: `--liveness-cmd` escape hatch + mutual exclusion

**Files:**
- Modify: `src/swarph_cli/commands/watchdog.py` (new `_liveness_via_cmd` helper; `_build_parser` group; handler branch at `:1272`)
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Consumes: `_process_alive(tmux_session, process_name)` (Task 1); `liveness_group` (Task 1).
- Produces: `_liveness_via_cmd(cmd: str) -> bool` — runs `cmd` (shell), `rc == 0` → True, non-zero → False, timeout/`OSError` → True (fail toward alive).
- Produces: argparse `--liveness-cmd` (default `None`) in `liveness_group` (so `--process-name` + `--liveness-cmd` together → `SystemExit`).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_watchdog.py`:

```python
def test_liveness_via_cmd_rc0_is_alive():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        assert watchdog._liveness_via_cmd("true") is True


def test_liveness_via_cmd_nonzero_is_dead():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
        assert watchdog._liveness_via_cmd("false") is False


def test_liveness_via_cmd_timeout_assumes_alive():
    from swarph_cli.commands import watchdog
    import subprocess as _sp
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               side_effect=_sp.TimeoutExpired(cmd="x", timeout=5)):
        assert watchdog._liveness_via_cmd("sleep 99") is True


def test_liveness_via_cmd_oserror_assumes_alive():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               side_effect=OSError("boom")):
        assert watchdog._liveness_via_cmd("bad") is True


def test_process_name_and_liveness_cmd_are_mutually_exclusive():
    from swarph_cli.commands import watchdog
    parser = watchdog._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--check", "--process-name", "grok",
                           "--liveness-cmd", "pgrep -f grok"])
```

(`pytest` is already imported at the top of `tests/test_watchdog.py`; `SimpleNamespace` is imported in Task 1.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py -k "liveness_via_cmd or mutually_exclusive" -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_liveness_via_cmd'`, and the mutual-exclusion test does NOT raise (no `--liveness-cmd` yet).

- [ ] **Step 3: Add the `_liveness_via_cmd` helper**

In `src/swarph_cli/commands/watchdog.py`, immediately after `_process_alive` (after its closing line, before `def _tmux_session_exists`), add:

```python
def _liveness_via_cmd(cmd: str) -> bool:
    """Escape-hatch liveness probe: run `cmd`; exit 0 = alive, non-zero = dead.

    For cells whose liveness a process name can't express. Bounded timeout;
    on timeout / OSError assume ALIVE — a broken or slow probe must never
    false-fire the destructive A2 respawn (same fail-safe as _process_alive).
    """
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True
```

- [ ] **Step 4: Add `--liveness-cmd` to the mutually-exclusive group**

In `_build_parser()`, directly after the `liveness_group.add_argument("--process-name", ...)` block from Task 1, add:

```python
    liveness_group.add_argument(
        "--liveness-cmd", default=None,
        help="Escape hatch: shell command whose exit status is the liveness "
             "verdict (0 = alive, non-zero = dead) instead of the pgrep gate. "
             "On timeout/error the cell is assumed ALIVE (never false-fire the "
             "destructive A2 respawn). Mutually exclusive with --process-name.",
    )
```

- [ ] **Step 5: Branch the handler on `--liveness-cmd`**

At `:1272` (now `process_alive = _process_alive(tmux_session, args.process_name)` after Task 1), replace with:

```python
    if args.liveness_cmd:
        process_alive = _liveness_via_cmd(args.liveness_cmd)
    else:
        process_alive = _process_alive(tmux_session, args.process_name)
```

- [ ] **Step 6: Run the new tests + the full watchdog suite**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py -v`
Expected: PASS — all five new tests pass; the full suite stays green.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): --liveness-cmd escape hatch (fail-toward-alive), mutually exclusive with --process-name"
```

---

### Task 3: thread `process_name` into the send-target

**Files:**
- Modify: `src/swarph_cli/commands/watchdog.py` (`_resolve_send_target` `:641`; `_tmux_send_keys` `:668`; call sites `:1447`, `:1490`)
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Consumes: `args.process_name` (Task 1).
- Produces: `_resolve_send_target(name: str, process_name: str = "claude") -> str` — prefers a pane whose command == `process_name`, else the existing claude/node heuristic, else the session name.
- Produces: `_tmux_send_keys(name, text, clear_input=False, process_name="claude") -> bool` — forwards `process_name` to `_resolve_send_target`.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_watchdog.py`:

```python
def _panes_run(stdout):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_resolve_send_target_default_prefers_node_pane():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 bash\n%2 node\n")):
        assert watchdog._resolve_send_target("sess") == "%2"


def test_resolve_send_target_honors_process_name():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 bash\n%2 grok\n")):
        assert watchdog._resolve_send_target("sess", process_name="grok") == "%2"


def test_resolve_send_target_process_name_wins_over_fallback():
    from swarph_cli.commands import watchdog
    # both a node pane and a grok pane present; process_name='grok' must win.
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 node\n%2 grok\n")):
        assert watchdog._resolve_send_target("sess", process_name="grok") == "%2"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py -k resolve_send_target -v`
Expected: FAIL — `test_resolve_send_target_honors_process_name` errors (`unexpected keyword argument 'process_name'`); `test_resolve_send_target_process_name_wins_over_fallback` fails (returns `%1`, the node pane, under today's single-pass logic).

- [ ] **Step 3: Rewrite `_resolve_send_target` as two-pass, process-name-first**

Replace the function body (`:641-665`). Current match loop:

```python
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("claude", "node"):
                return parts[0]
```

New full function:

```python
def _resolve_send_target(name: str, process_name: str = "claude") -> str:
    """Resolve a session name to the pane actually running the cell's agent.

    `send-keys -t <session>` lands on the session's ACTIVE pane — on a
    multi-pane cell that can be a bash/log pane, where an injected wake would
    execute as a SHELL command. Prefer the pane whose current command matches
    the cell's `process_name`; then fall back to the claude-CLI heuristic
    (claude runs under node); then to the session name unchanged when tmux is
    unavailable, the listing fails, or no pane matches.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", name, "-F",
             "#{pane_id} #{pane_current_command}"],
            capture_output=True, timeout=5, text=True,
        )
        if result.returncode != 0:
            return name
        panes = [ln.split() for ln in result.stdout.splitlines()]
        for parts in panes:                       # exact process_name match wins
            if len(parts) >= 2 and parts[1] == process_name:
                return parts[0]
        for parts in panes:                       # claude-CLI fallback (node)
            if len(parts) >= 2 and parts[1] in ("claude", "node"):
                return parts[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return name
```

- [ ] **Step 4: Forward `process_name` through `_tmux_send_keys`**

Change the signature (`:668`) and the `_resolve_send_target` call (`:680`). Current:

```python
def _tmux_send_keys(name: str, text: str, clear_input: bool = False) -> bool:
    ...
    target = _resolve_send_target(name)
```
New:
```python
def _tmux_send_keys(
    name: str, text: str, clear_input: bool = False, process_name: str = "claude"
) -> bool:
    ...
    target = _resolve_send_target(name, process_name)
```
(Keep the existing docstring body; only the signature line and the `target =` line change.)

- [ ] **Step 5: Pass `args.process_name` at both `_tmux_send_keys` call sites**

At `:1447`:
```python
            sent = _tmux_send_keys(tmux_session, model_text, clear_input=True)
```
→
```python
            sent = _tmux_send_keys(tmux_session, model_text, clear_input=True,
                                   process_name=args.process_name)
```
At `:1490`:
```python
    sent = _tmux_send_keys(tmux_session, wake_text)
```
→
```python
    sent = _tmux_send_keys(tmux_session, wake_text, process_name=args.process_name)
```

- [ ] **Step 6: Run the new tests + the full watchdog suite**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py -v`
Expected: PASS — the three send-target tests pass; the existing A1 send tests (which patch `_tmux_send_keys` wholesale) are unaffected; full suite green.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): send-target honors --process-name so the wake lands on the agent pane"
```

---

### Task 4: version bump `0.24.0 → 0.25.0` + full-suite gate

**Files:**
- Modify: `pyproject.toml:7`, `src/swarph_cli/__init__.py:19`
- Test: `tests/test_watchdog.py` (whole suite) + a version-consistency check

**Interfaces:**
- Consumes: nothing (release bookkeeping).

- [ ] **Step 1: Write the failing test** — add to `tests/test_watchdog.py`:

```python
def test_version_is_0_25_0():
    import swarph_cli
    assert swarph_cli.__version__ == "0.25.0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py::test_version_is_0_25_0 -v`
Expected: FAIL — `assert '0.24.0' == '0.25.0'`.

- [ ] **Step 3: Bump both version pins**

`src/swarph_cli/__init__.py:19`:
```python
__version__ = "0.24.0"
```
→
```python
__version__ = "0.25.0"
```
`pyproject.toml:7`:
```toml
version = "0.24.0"
```
→
```toml
version = "0.25.0"
```

- [ ] **Step 4: Run the version test + the FULL package suite**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_watchdog.py::test_version_is_0_25_0 -v && python -m pytest -q`
Expected: PASS — the version test passes and the entire repo suite is green (no regression from the threading).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add pyproject.toml src/swarph_cli/__init__.py tests/test_watchdog.py
git commit -m "chore(release): bump swarph-cli 0.24.0 -> 0.25.0 (pluggable watchdog liveness)"
```

---

## Self-Review

**1. Spec coverage:**
- Two flags, mutually exclusive → Tasks 1 (`--process-name`, group) + 2 (`--liveness-cmd`, group extension, exclusion test). ✓
- Fail-toward-alive on cmd error → Task 2 Step 1 (timeout + OSError tests) / Step 3 (helper). ✓
- Thread identity into BOTH coupling points → Task 1 (`_process_alive`) + Task 3 (`_resolve_send_target` + `_tmux_send_keys`). ✓
- Default-`claude` backward-compat lock → Task 1 `test_process_alive_default_greps_claude` + Task 3 default send-target test; every task ends by running the full watchdog suite. ✓
- Version bump both files → Task 4. ✓
- Public-PyPI / synthetic fixtures → all tests use `SimpleNamespace` stubs + literal session names, no cell data. ✓
- Out of scope (cell-yaml auto-resolution, A2 respawn mechanism, sidecar-wake-verify, PyPI publish, cron re-wire) → not present in any task. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows the full before/after. ✓

**3. Type consistency:** `process_name: str = "claude"` is identical across `_process_alive`, `_resolve_send_target`, `_tmux_send_keys`. `_liveness_via_cmd(cmd: str) -> bool`. Handler passes `args.process_name` / `args.liveness_cmd` (argparse dests: `--process-name`→`process_name`, `--liveness-cmd`→`liveness_cmd`). Group variable `liveness_group` created in Task 1, extended in Task 2. ✓

**Note for the executor:** the line numbers (`:583`, `:1272`, etc.) are anchors from the pre-change file; after Task 1's edits later anchors shift by a few lines — locate by the quoted code, not the raw number.
