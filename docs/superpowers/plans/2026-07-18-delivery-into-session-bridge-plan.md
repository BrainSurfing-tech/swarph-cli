# Delivery-into-session Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `swarph daemon` drains a mesh DM, deliver it INTO the cell's live agent session (inject into the resident TUI pane) instead of only logging it — so drain-only cells stop being silently DM-blind.

**Architecture:** A new focused module `swarph_cli/session_bridge.py` ports the *hardened* pane primitives from lab-orchestrator's proven `workers/cell_wake.py` (positive-idle probe, `-l` literal sanitized send, fail-safe pane resolve, safe-modal dismiss) — cross-platform via `multiplexer.find_multiplexer()`. A persisted `DeliveryQueue` (`delivery_queue.py`) holds drained-but-undelivered DMs; a `stall_alert.py` backoff surfaces a stuck cell. The `swarph daemon` drain loop (`commands/daemon.py`) replaces its `_route_to_handler` stub with enqueue, and each tick runs `attempt_delivery` (resolve pane → probe → inject batch | defer | dismiss-modal), all behind the existing `--auto-act` opt-in.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `json`, `os`, `re`, `urllib`), pytest. No new runtime deps. All pane I/O through the multiplexer's resolved `tmux`/`psmux` binary.

## Global Constraints

Copied verbatim from the spec — every task's requirements implicitly include these:

- **Never crash the daemon.** The daemon's loud-on-down liveness (`ps aux | grep '[s]warph daemon'`) is load-bearing; bridge failures are caught + logged, the daemon keeps draining. A bridge exception must never take the drain loop down.
- **Idle-probe is fail-safe toward "busy".** Never inject into an ambiguous/busy/modal pane — defer. Positive-idle confirmation only (footer sentinel present AND no busy-markers).
- **Never lose a DM.** A drained-but-undelivered DM stays queued (persisted beside the cursor) and is retried; the gateway cursor advances separately (drain ≠ delivery), so a re-drain doesn't double-fetch, but an undelivered DM is never dropped.
- **Quiet unless actionable.** Actionable (wake) = `question`, `unblock`, and any `answer` carrying a non-null `thread_id`. Ride-along (delivered in the next batch, never triggers its own wake) = `fyi`, `status`, `answer` with `thread_id=null`. The wake flag is computable from the message alone.
- **Opt-in.** Active delivery ships behind the existing `swarph daemon --auto-act` flag; surface-only stays the default for un-opted cells.
- **Cross-platform via the multiplexer binary only.** All pane I/O uses `multiplexer.find_multiplexer()` (tmux on POSIX, psmux on Windows) — no OS-specific pane code, no hardcoded `"tmux"`.

**THE correctness risk (verify before wiring):** the pane-resolution convention — the daemon must reliably find ITS cell's agent pane (session name == cell `self_name`, positively-identified claude/node pane). Non-standard naming or a shell-only pane → fall back to surface-only, NEVER inject into the wrong pane. Task 3 includes a build step that verifies resolution against a real live session.

**Reference source (port, don't re-invent):** `/home/ubuntu/lab-orchestrator/workers/cell_wake.py` — the hardened, incident-proven original. The port adapts hardcoded `"tmux"` → `multiplexer.find_multiplexer()` and splits the bool `_probe_idle` into the three-way `probe_pane`.

**DO NOT reuse `commands/watchdog.py`'s wake functions** — its `_resolve_send_target` returns a bare `str` (not fail-safe `Optional`), its `_tmux_send_keys` sends WITHOUT `-l` (key-name lookup, unsafe for arbitrary DM content), and it has no positive-idle probe / sanitize / modal-dismiss. Different safety contract; leave that security-critical recovery code untouched.

---

### Task 1: `session_bridge` — the idle probe (safety gate)

**Files:**
- Create: `src/swarph_cli/session_bridge.py`
- Test: `tests/test_session_bridge_probe.py`

**Interfaces:**
- Consumes: `swarph_cli.multiplexer.find_multiplexer() -> str | None`.
- Produces:
  - `probe_pane(pane_id: str) -> str` returning one of `"idle" | "busy" | "modal"`.
  - `try_dismiss_safe_modal(pane_id: str) -> bool`.
  - module constants `_BUSY_MARKERS`, `_IDLE_SENTINEL`, `_SAFE_DISMISSABLE_MODALS`.
  - internal helper `_capture(pane_id: str) -> str | None` (None on any failure).
  - internal helper `_mux() -> str | None` (the resolved multiplexer binary).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_bridge_probe.py
import subprocess
import swarph_cli.session_bridge as sb


class _CP:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def _fake_capture(monkeypatch, rc, out):
    def fake_run(cmd, **kw):
        return _CP(rc, out)
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")


def test_probe_idle_on_footer_sentinel(monkeypatch):
    _fake_capture(monkeypatch, 0, "some output\n? for shortcuts\n")
    assert sb.probe_pane("%1") == "idle"


def test_probe_busy_on_esc_to_interrupt(monkeypatch):
    _fake_capture(monkeypatch, 0, "Thinking…\nesc to interrupt\n")
    assert sb.probe_pane("%1") == "busy"


def test_probe_modal_on_safe_survey(monkeypatch):
    _fake_capture(monkeypatch, 0, "How is Claude doing this session?\n❯ 1. Bad\n")
    assert sb.probe_pane("%1") == "modal"


def test_probe_busy_on_capture_failure(monkeypatch):
    _fake_capture(monkeypatch, 1, "")
    assert sb.probe_pane("%1") == "busy"


def test_probe_busy_on_empty(monkeypatch):
    _fake_capture(monkeypatch, 0, "   \n")
    assert sb.probe_pane("%1") == "busy"


def test_probe_busy_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.probe_pane("%1") == "busy"


def test_dismiss_returns_false_when_no_safe_modal(monkeypatch):
    _fake_capture(monkeypatch, 0, "esc to interrupt\n")
    assert sb.try_dismiss_safe_modal("%1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_probe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'swarph_cli.session_bridge'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/session_bridge.py
"""session_bridge — hardened primitives to deliver a mesh DM INTO a cell's
live agent TUI pane. Ported from lab-orchestrator/workers/cell_wake.py (the
incident-proven original); hardcoded "tmux" replaced by the cross-platform
multiplexer binary. Fail-safe throughout: never inject into a busy/ambiguous
pane; any error → defer, never raise.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from swarph_cli.multiplexer import find_multiplexer

# Busy / dialog / approval markers in a Claude TUI pane. ANY (case-insensitive)
# means mid-turn or a non-idle prompt → not safe to inject.
_BUSY_MARKERS = (
    "esc to interrupt", "thinking…", "compacting", "(esc)",
    "(y/n)", "❯ 1.", "do you want", "approve", "│ >",
)
# Positive idle sentinel — the Claude REPL footer hint. Presence (with NO busy
# marker) POSITIVELY confirms an idle input prompt.
_IDLE_SENTINEL = "? for shortcuts"
# Modals ALWAYS safe to Escape-dismiss — pure telemetry the cell never answers
# but which trips a busy marker and stalls a wake forever.
_SAFE_DISMISSABLE_MODALS = (
    "how is claude doing this session",
)

_WS_RUN = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _mux() -> Optional[str]:
    """The resolved tmux-compatible binary (tmux/psmux), or None."""
    return find_multiplexer()


def _capture(pane_id: str) -> Optional[str]:
    """`capture-pane -p` the pane. None on any failure (fail-safe)."""
    mux = _mux()
    if mux is None:
        return None
    try:
        r = subprocess.run(
            [mux, "capture-pane", "-p", "-t", pane_id],
            capture_output=True, timeout=5, text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


def probe_pane(pane_id: str) -> str:
    """Three-way pane state: "idle" | "busy" | "modal".

    "modal" (a known-safe dismissable telemetry popup) is checked BEFORE
    "busy" so it routes to the dismiss path. Positive-idle only: an idle
    footer/bare-">" prompt with NO busy marker. Everything else, incl. any
    capture failure, → "busy" (defer, never inject)."""
    content = _capture(pane_id)
    if content is None or not content.strip():
        return "busy"
    low = content.lower()
    if any(m in low for m in _SAFE_DISMISSABLE_MODALS):
        return "modal"
    if any(m in low for m in _BUSY_MARKERS):
        return "busy"
    if _IDLE_SENTINEL in low:
        return "idle"
    non_empty = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    if non_empty and non_empty[-1].strip() == ">":
        return "idle"
    return "busy"


def _send_key(pane_id: str, key: str) -> bool:
    """Send a single KEY (key-name, NOT literal). Fail-safe on error."""
    mux = _mux()
    if mux is None:
        return False
    try:
        r = subprocess.run(
            [mux, "send-keys", "-t", pane_id, key],
            capture_output=True, timeout=5, text=True,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return False


def try_dismiss_safe_modal(pane_id: str) -> bool:
    """If the pane shows a KNOWN-SAFE dismissable modal, Escape it + return
    True. False when none present (caller never Escapes a real busy state)."""
    content = _capture(pane_id)
    if content is None:
        return False
    low = content.lower()
    if not any(m in low for m in _SAFE_DISMISSABLE_MODALS):
        return False
    if not _send_key(pane_id, "Escape"):
        return False
    time.sleep(0.4)  # let the TUI re-render before the caller re-probes
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_probe.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/session_bridge.py tests/test_session_bridge_probe.py
git commit -m "feat(bridge): session_bridge idle probe + safe-modal dismiss (ported from cell_wake)"
```

---

### Task 2: `session_bridge` — sanitized literal inject

**Files:**
- Modify: `src/swarph_cli/session_bridge.py` (add `_sanitize`, `inject`)
- Test: `tests/test_session_bridge_inject.py`

**Interfaces:**
- Consumes: `_mux()` from Task 1.
- Produces:
  - `_sanitize(text: str) -> str` — strip all C0/C1 control bytes, collapse whitespace runs to single space, strip.
  - `inject(pane_id: str, text: str) -> bool` — sanitize → literal `send-keys -l <body>` → one `Enter`. Leading `/` defanged. `False` on any failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_bridge_inject.py
import swarph_cli.session_bridge as sb


def test_sanitize_collapses_newlines_and_strips_control():
    # Newlines collapse to single spaces; the ESC control byte (\x1b) is
    # STRIPPED so no ANSI interpretation is possible. The security property is
    # "no ESC byte reaches the pane" (a broken ANSI seq's residual bracket
    # chars are inert literal text) — not "all bracket chars removed".
    out = sb._sanitize("hello\n\nworld \x1b more  x")
    assert out == "hello world more x"
    assert "\x1b" not in out and "\n" not in out


def test_sanitize_empty():
    assert sb._sanitize("") == ""


def test_inject_sends_literal_then_enter(monkeypatch):
    calls = []

    class _CP:
        returncode = 0

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _CP()

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    assert sb.inject("%1", "reply now") is True
    # exactly two calls: literal body (-l) then a bare Enter
    assert calls[0] == ["tmux", "send-keys", "-t", "%1", "-l", "reply now"]
    assert calls[1] == ["tmux", "send-keys", "-t", "%1", "Enter"]


def test_inject_defangs_leading_slash(monkeypatch):
    calls = []

    class _CP:
        returncode = 0

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: (calls.append(cmd) or _CP()))
    sb.inject("%1", "/model haiku")
    # leading slash is space-prefixed so the TUI never reads a slash-command
    assert calls[0] == ["tmux", "send-keys", "-t", "%1", "-l", " /model haiku"]


def test_inject_false_on_nonzero_exit(monkeypatch):
    class _CP:
        returncode = 1

    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP())
    assert sb.inject("%1", "x") is False


def test_inject_false_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.inject("%1", "x") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_inject.py -q`
Expected: FAIL — `AttributeError: module 'swarph_cli.session_bridge' has no attribute '_sanitize'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/swarph_cli/session_bridge.py`:

```python
def _sanitize(text: str) -> str:
    """Strip ALL control bytes, then collapse whitespace runs to a space.
    Result contains no control bytes and no \\n/\\r, so even a literal `-l`
    send cannot trigger embedded-key / ANSI interpretation."""
    if not text:
        return ""
    return _WS_RUN.sub(" ", _CTRL.sub("", text)).strip()


def inject(pane_id: str, text: str) -> bool:
    """Deliver `text` into the pane: sanitize → literal `send-keys -l` →
    exactly one `Enter`. Leading `/` defanged. Two subprocess calls; True iff
    both return 0. Fail-safe: no mux / any error → False (caller re-queues)."""
    mux = _mux()
    if mux is None:
        return False
    body = _sanitize(text)
    if body.startswith("/"):
        body = " " + body
    try:
        r1 = subprocess.run(
            [mux, "send-keys", "-t", pane_id, "-l", body],
            capture_output=True, timeout=5, text=True,
        )
        if r1.returncode != 0:
            return False
        r2 = subprocess.run(
            [mux, "send-keys", "-t", pane_id, "Enter"],
            capture_output=True, timeout=5, text=True,
        )
        return r2.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_inject.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/session_bridge.py tests/test_session_bridge_inject.py
git commit -m "feat(bridge): sanitized literal-send inject with slash-defang"
```

---

### Task 3: `session_bridge` — pane resolution (THE correctness risk)

**Files:**
- Modify: `src/swarph_cli/session_bridge.py` (add `resolve_session_pane`)
- Test: `tests/test_session_bridge_resolve.py`

**Interfaces:**
- Consumes: `_mux()` from Task 1.
- Produces: `resolve_session_pane(self_name: str) -> str | None` — the pane-id of the claude/node TUI pane in the tmux/psmux session named `self_name`; `None` on ANY failure or when no claude/node pane is positively identified (caller stays surface-only, NEVER injects into a shell pane).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_bridge_resolve.py
import swarph_cli.session_bridge as sb


class _CP:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def test_resolve_returns_claude_pane(monkeypatch):
    out = "%0 bash\n%1 claude\n"
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, out))
    assert sb.resolve_session_pane("lab-ovh") == "%1"


def test_resolve_returns_node_pane(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, "%2 node\n"))
    assert sb.resolve_session_pane("cell") == "%2"


def test_resolve_none_when_only_shell_panes(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(0, "%0 bash\n%1 vim\n"))
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_on_nonzero(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(1, ""))
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.resolve_session_pane("cell") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_resolve.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'resolve_session_pane'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/swarph_cli/session_bridge.py`:

```python
def resolve_session_pane(self_name: str) -> Optional[str]:
    """Resolve this cell's own agent pane.

    Convention: the tmux/psmux session hosting the cell's resident agent is
    named after the cell (`self_name`). `send-keys -t <session>` lands on the
    ACTIVE pane, which on a multi-pane cell can be a shell where an injected
    `/model ...` would run as a SHELL command — so this returns the pane-id
    ONLY when a claude/node pane is POSITIVELY identified, and None on ANY
    failure or no match. None → caller stays surface-only, NEVER injects."""
    mux = _mux()
    if mux is None:
        return None
    try:
        r = subprocess.run(
            [mux, "list-panes", "-t", self_name, "-F",
             "#{pane_id} #{pane_current_command}"],
            capture_output=True, timeout=5, text=True,
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("claude", "node"):
                return parts[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_session_bridge_resolve.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: VERIFY THE CONVENTION AGAINST A REAL LIVE SESSION (the correctness risk)**

Run this one-off probe on lab-ovh (a cell with a live agent session):

```bash
cd /home/ubuntu/swarph-cli && python -c "
import swarph_cli.session_bridge as sb
from swarph_cli.multiplexer import find_multiplexer
print('mux:', find_multiplexer())
for name in ('lab-ovh',):
    print(name, '->', sb.resolve_session_pane(name))
"
```

Expected: `mux:` prints a real binary path; `lab-ovh -> %N` (a pane-id) if lab-ovh's agent session is named `lab-ovh` and has a claude/node pane. **If it prints `None`**, the session-name convention does not hold for this cell — record the finding in the task report: the bridge will fall back to surface-only for that cell (SAFE — never mis-injects), and the resolution convention (session name vs. cell name) is a rollout config point, not a code bug. Either outcome is acceptable to proceed; the point is to KNOW before wiring, not to require a hit.

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/session_bridge.py tests/test_session_bridge_resolve.py
git commit -m "feat(bridge): fail-safe pane resolution (positive claude/node match)"
```

---

### Task 4: `DeliveryQueue` — persisted pending-DM state

**Files:**
- Create: `src/swarph_cli/delivery_queue.py`
- Test: `tests/test_delivery_queue.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone).
- Produces:
  - `wake_for(kind: str, thread_id) -> bool` — `kind in ("question","unblock") or (kind == "answer" and thread_id is not None)`.
  - class `DeliveryQueue(path: pathlib.Path)` with:
    - `enqueue(dm: dict) -> None` — append `{id, from, kind, thread_id, content, wake}` if `dm["id"]` not already pending; persist.
    - `pending() -> list[dict]` — current entries (list copy).
    - `any_wake() -> bool` — True if any pending entry has `wake=True`.
    - `remove(ids: set) -> None` — drop delivered ids; persist.
    - `deferred_ticks: int` attribute — consecutive busy ticks (queue-level, not per-entry).
    - `bump_deferred() -> int` — increment `deferred_ticks`, persist, return new value.
    - `reset_deferred() -> None` — set `deferred_ticks = 0`, persist.

> **Refinement vs. spec:** the spec listed `deferred_ticks` per-entry; this plan tracks it once at the queue level, which is what the stall semantics ("the cell is busy") actually need — simpler and equivalent. Recorded here so the reviewer sees it is intentional.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delivery_queue.py
from swarph_cli.delivery_queue import DeliveryQueue, wake_for


def _dm(i, kind="fyi", thread_id=None):
    return {"id": i, "from_node": "peer", "kind": kind,
            "thread_id": thread_id, "content": f"m{i}"}


def test_wake_for_rules():
    assert wake_for("question", None) is True
    assert wake_for("unblock", None) is True
    assert wake_for("answer", "t1") is True     # threaded answer = targeted
    assert wake_for("answer", None) is False    # broadcast answer = ride-along
    assert wake_for("fyi", None) is False
    assert wake_for("status", "t9") is False    # status never wakes


def test_enqueue_and_pending(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1, "question"))
    q.enqueue(_dm(2, "fyi"))
    p = q.pending()
    assert [e["id"] for e in p] == [1, 2]
    assert p[0]["wake"] is True and p[1]["wake"] is False
    assert q.any_wake() is True


def test_enqueue_dedups_by_id(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1))
    q.enqueue(_dm(1))
    assert len(q.pending()) == 1


def test_persist_across_reload(tmp_path):
    p = tmp_path / "q.json"
    q = DeliveryQueue(p)
    q.enqueue(_dm(1, "unblock"))
    q.bump_deferred()
    q2 = DeliveryQueue(p)                 # fresh instance reads the file
    assert [e["id"] for e in q2.pending()] == [1]
    assert q2.deferred_ticks == 1


def test_remove_and_reset(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1)); q.enqueue(_dm(2))
    q.bump_deferred(); q.bump_deferred()
    q.remove({1})
    q.reset_deferred()
    assert [e["id"] for e in q.pending()] == [2]
    assert q.deferred_ticks == 0


def test_corrupt_file_is_empty_failsafe(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{not json")
    q = DeliveryQueue(p)                  # must not raise
    assert q.pending() == []
    assert q.deferred_ticks == 0


def test_valid_json_wrong_shape_is_empty_failsafe(tmp_path):
    # a torn write can leave syntactically valid JSON of the wrong shape;
    # must be treated as empty, never raise (never lose the daemon at startup).
    for bad in ("null", "[1,2,3]", '"a string"', "42"):
        p = tmp_path / "q.json"
        p.write_text(bad)
        q = DeliveryQueue(p)
        assert q.pending() == []
        assert q.deferred_ticks == 0


def test_pending_is_defensive_copy(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1))
    q.pending()[0]["content"] = "MUTATED"     # caller mutation must not leak
    assert q.pending()[0]["content"] == "m1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_delivery_queue.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'swarph_cli.delivery_queue'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/delivery_queue.py
"""DeliveryQueue — drained-but-undelivered mesh DMs, persisted beside the
daemon cursor (write-and-rename atomic) so it survives a restart. A DM is
never lost: it stays queued until injected into the session. Fail-safe: a
corrupt/unreadable file is treated as empty (never raises)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List


def wake_for(kind: str, thread_id) -> bool:
    """Actionable (wake on next idle) = question / unblock, or a threaded
    answer (targeted reply). Broadcast answers / fyi / status ride along."""
    if kind in ("question", "unblock"):
        return True
    return kind == "answer" and thread_id is not None


class DeliveryQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending: List[dict] = []
        self.deferred_ticks = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):   # valid JSON but wrong shape (null, list, scalar)
                raise ValueError("queue file is not a JSON object")
            self._pending = list(data.get("pending", []))
            self.deferred_ticks = int(data.get("deferred_ticks", 0))
        except (FileNotFoundError, ValueError, OSError, TypeError, AttributeError):
            self._pending = []
            self.deferred_ticks = 0

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"pending": self._pending,
                        "deferred_ticks": self.deferred_ticks},
                       indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # atomic

    def enqueue(self, dm: dict) -> None:
        mid = dm["id"]
        if any(e["id"] == mid for e in self._pending):
            return
        kind = dm.get("kind", "")
        thread_id = dm.get("thread_id")
        self._pending.append({
            "id": mid,
            "from": dm.get("from_node"),
            "kind": kind,
            "thread_id": thread_id,
            "content": dm.get("content", ""),
            "wake": wake_for(kind, thread_id),
        })
        self._persist()

    def pending(self) -> List[dict]:
        return [dict(e) for e in self._pending]   # defensive copy — callers hold + mutate

    def any_wake(self) -> bool:
        return any(e.get("wake") for e in self._pending)

    def remove(self, ids: set) -> None:
        self._pending = [e for e in self._pending if e["id"] not in ids]
        self._persist()

    def bump_deferred(self) -> int:
        self.deferred_ticks += 1
        self._persist()
        return self.deferred_ticks

    def reset_deferred(self) -> None:
        if self.deferred_ticks != 0:
            self.deferred_ticks = 0
            self._persist()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_delivery_queue.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/delivery_queue.py tests/test_delivery_queue.py
git commit -m "feat(bridge): persisted DeliveryQueue with wake-rule + dedup"
```

---

### Task 5: `stall_alert` — exponential-backoff stuck-cell alert

**Files:**
- Create: `src/swarph_cli/stall_alert.py`
- Test: `tests/test_stall_alert.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `is_alert_tick(deferred_ticks: int) -> bool` — True exactly at 6, 12, 24, 48, 96… (first at 6, doubling).
  - `send_stall_alert(gateway: str, token: str, self_name: str, count: int, pending_n: int) -> bool` — POST one DM to `to_node="commander"`, `kind="unblock"`, via `{gateway}/messages`. Returns True on 2xx. Fail-safe: never raises; POST failure → False + no exception.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stall_alert.py
import swarph_cli.stall_alert as st


def test_is_alert_tick_backoff_sequence():
    fire = [n for n in range(1, 100) if st.is_alert_tick(n)]
    assert fire == [6, 12, 24, 48, 96]


def test_is_alert_tick_below_threshold():
    assert not any(st.is_alert_tick(n) for n in range(0, 6))


def test_send_stall_alert_posts_unblock(monkeypatch):
    # Mock ONLY urlopen; let the real urllib.request.Request build so the
    # captured Request carries a genuine .full_url and .data (POST body).
    import json
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(st.urllib.request, "urlopen", fake_urlopen)
    ok = st.send_stall_alert("http://gw", "tok", "workstation-lc", 12, 3)
    assert ok is True
    assert captured["url"] == "http://gw/messages"
    body = json.loads(captured["body"].decode())
    assert body["to_node"] == "commander"
    assert body["kind"] == "unblock"
    assert body["from_node"] == "workstation-lc"
    assert "workstation-lc" in body["content"]


def test_send_stall_alert_failsafe_on_error(monkeypatch):
    # A network failure inside urlopen must be swallowed → False, never raise.
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 1) is False


def test_send_stall_alert_failsafe_on_bad_gateway():
    # a schemeless / misconfigured gateway must be caught at Request-build time,
    # not raised (no monkeypatch — Request(...) itself raises ValueError).
    assert st.send_stall_alert("not-a-valid-url", "tok", "cell", 6, 1) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_stall_alert.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'swarph_cli.stall_alert'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/stall_alert.py
"""stall_alert — surface a cell whose live session is perpetually busy so its
undelivered DMs don't rot silently. Exponential backoff (6,12,24,48…) prevents
the linear-flood failure (the 145-DM incident, feedback_modal_stalls_cell_wake).
Fail-safe: a failed alert POST never raises and never blocks delivery."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

_STALL_FIRST = 6  # first alert after this many consecutive deferred ticks


def is_alert_tick(deferred_ticks: int) -> bool:
    """True exactly at 6, 12, 24, 48, 96 … — first at _STALL_FIRST, doubling."""
    if deferred_ticks < _STALL_FIRST or deferred_ticks % _STALL_FIRST != 0:
        return False
    k = deferred_ticks // _STALL_FIRST
    return (k & (k - 1)) == 0  # k is a power of two


def send_stall_alert(gateway: str, token: str, self_name: str,
                     count: int, pending_n: int) -> bool:
    """POST one commander DM about the stall. True on 2xx; fail-safe False on
    any error (never raises — the daemon must keep draining)."""
    body = json.dumps({
        "from_node": self_name,
        "to_node": "commander",
        "kind": "unblock",
        "content": (f"[stall] {self_name} daemon: {count} consecutive ticks "
                    f"unable to deliver {pending_n} DM(s) into the live session "
                    f"(cell busy/never-idle). Undelivered DMs are queued, not "
                    f"lost; they inject on next idle."),
    }).encode("utf-8")
    try:
        # Request(...) parses the URL eagerly and raises ValueError on a
        # schemeless/misconfigured gateway — so build it INSIDE the try, or a
        # bad GATEWAY_URL crashes the daemon (the exact fail-safe this guards).
        req = urllib.request.Request(
            f"{gateway}/messages", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[swarph-daemon] stall-alert POST failed: {exc}",
              file=sys.stderr, flush=True)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_stall_alert.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/stall_alert.py tests/test_stall_alert.py
git commit -m "feat(bridge): exponential-backoff stall alert for a stuck cell"
```

---

### Task 6: Wire the drain loop + integration + version bump

**Files:**
- Modify: `src/swarph_cli/commands/daemon.py` (add `attempt_delivery`, `_render_delivery_block`; extend `DaemonState`; replace `_route_to_handler`; call in `_drain_loop`)
- Modify: `pyproject.toml` (version 0.32.2 → 0.33.0)
- Test: `tests/test_daemon_delivery.py`

**Interfaces:**
- Consumes:
  - `swarph_cli.session_bridge.resolve_session_pane`, `probe_pane`, `try_dismiss_safe_modal`, `inject` (Tasks 1-3).
  - `swarph_cli.delivery_queue.DeliveryQueue` (Task 4).
  - `swarph_cli.stall_alert.is_alert_tick`, `send_stall_alert` (Task 5).
  - existing `DaemonState`, `_drain_iteration`, `_drain_loop` (`commands/daemon.py`).
- Produces:
  - `DaemonState.queue: DeliveryQueue` (new field, initialized from `state_dir / "delivery_queue.json"`).
  - `DaemonState.session_name: str` (new field = `os.environ.get("SWARPH_SESSION_NAME", self_name)`; the tmux/psmux session used for pane resolution).
  - `_render_delivery_block(entries: list) -> str`.
  - `attempt_delivery(state: DaemonState) -> None` — never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_delivery.py
import asyncio
from pathlib import Path

import swarph_cli.commands.daemon as d
from swarph_cli.commands.daemon import DaemonState, attempt_delivery, _render_delivery_block


def _state(tmp_path, auto_act=True) -> DaemonState:
    return DaemonState(
        self_name="cell", state_dir=tmp_path, gateway="http://gw",
        token="tok", poll_s=1, auto_act=auto_act,
    )


def _dm(i, kind="question", thread_id=None):
    return {"id": i, "from_node": "peer", "kind": kind,
            "thread_id": thread_id, "content": f"m{i}",
            "created_at": "t"}


def test_render_block_lists_entries():
    block = _render_delivery_block([
        {"from": "droplet", "kind": "question", "content": "ping?"},
    ])
    assert "mesh delivery" in block
    assert "droplet" in block and "ping?" in block


def test_delivery_injects_on_idle(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    injected = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "idle")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: injected.update(pane=p, text=t) or True)
    attempt_delivery(s)
    assert injected["pane"] == "%1"
    assert "m1" in injected["text"]
    assert s.queue.pending() == []          # delivered → dequeued
    assert s.queue.deferred_ticks == 0


def test_delivery_defers_on_busy_and_counts(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "busy")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: (_ for _ in ()).throw(AssertionError("must not inject")))
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]   # not lost
    assert s.queue.deferred_ticks == 1


def test_delivery_stall_alert_fires_at_threshold(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    s.queue.deferred_ticks = 5             # next bump → 6 → alert
    alerts = []
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "busy")
    monkeypatch.setattr(d.stall_alert, "send_stall_alert",
                        lambda *a, **k: alerts.append(a) or True)
    attempt_delivery(s)
    assert len(alerts) == 1               # fired exactly once at tick 6


def test_delivery_holds_ride_along_only(tmp_path, monkeypatch):
    # only a fyi (ride-along, wake=False) is queued → must NOT wake an idle
    # cell; stays queued, no deferred bump (intentional wait, not a stall).
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1, kind="fyi"))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not wake on fyi")))
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]
    assert s.queue.deferred_ticks == 0


def test_delivery_batches_ride_along_with_actionable(tmp_path, monkeypatch):
    # a question (wake) + a fyi (ride-along) → the wake delivers BOTH in one block
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1, kind="question"))
    s.queue.enqueue(_dm(2, kind="fyi"))
    injected = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "idle")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: injected.update(text=t) or True)
    attempt_delivery(s)
    assert "m1" in injected["text"] and "m2" in injected["text"]
    assert s.queue.pending() == []


def test_delivery_noop_when_not_auto_act(tmp_path, monkeypatch):
    s = _state(tmp_path, auto_act=False)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not resolve")))
    attempt_delivery(s)                    # surface-only: no delivery attempt
    assert [e["id"] for e in s.queue.pending()] == [1]


def test_delivery_surface_only_when_no_pane(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: None)
    attempt_delivery(s)                    # headless cell → queued, no crash
    assert [e["id"] for e in s.queue.pending()] == [1]


def test_session_name_env_override(tmp_path, monkeypatch):
    # a cell whose tmux session name differs from its mesh self_name sets
    # SWARPH_SESSION_NAME; resolution uses THAT, not self_name.
    monkeypatch.setenv("SWARPH_SESSION_NAME", "lab")
    s = _state(tmp_path)
    assert s.session_name == "lab"
    s.queue.enqueue(_dm(1))
    seen = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: seen.update(name=n) or None)
    attempt_delivery(s)
    assert seen["name"] == "lab"          # resolved by session_name, not self_name "cell"


def test_attempt_delivery_never_raises(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    attempt_delivery(s)                    # must swallow the exception
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_daemon_delivery.py -q`
Expected: FAIL — `ImportError: cannot import name 'attempt_delivery'`.

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/commands/daemon.py`, add imports near the top (with the other `swarph_cli` imports):

```python
from swarph_cli import session_bridge, stall_alert
from swarph_cli.delivery_queue import DeliveryQueue
```

In `DaemonState.__init__`, after `self.inbox_log_path = ...`, add:

```python
        self.queue = DeliveryQueue(state_dir / "delivery_queue.json")
        # The tmux/psmux session hosting the agent is usually named after the
        # cell, but a cell's mesh id can differ from its session name (verified
        # on lab-ovh: mesh self_name="lab-ovh" but the session is named "lab").
        # SWARPH_SESSION_NAME overrides the session used for pane resolution;
        # defaults to self_name.
        self.session_name = os.environ.get("SWARPH_SESSION_NAME", self_name)
```

Replace `_route_to_handler` with enqueue-under-auto-act:

```python
def _route_to_handler(state: DaemonState, dm: dict) -> None:
    """Under --auto-act, enqueue the DM for delivery into the live session
    (attempt_delivery runs each tick). Surface-only (no auto-act) is unchanged
    — the DM is already logged by _log_dm; nothing further here."""
    if state.auto_act:
        state.queue.enqueue(dm)
```

Add the renderer + the delivery orchestrator (place after `_route_to_handler`):

```python
def _render_delivery_block(entries: list) -> str:
    """The compact block injected into the session — the resident model reads
    it as 'you have N mesh DM(s), act per DM SEMANTICS'."""
    lines = [f"📨 mesh delivery ({len(entries)} new):"]
    for e in entries:
        lines.append(
            f"  · from={e.get('from')} kind={e.get('kind')}: {e.get('content','')}"
        )
    lines.append("(act per DM SEMANTICS — reply AI-to-AI via mesh-gateway; "
                 "loop human only across a privilege boundary)")
    return " ".join(lines)


def attempt_delivery(state: DaemonState) -> None:
    """Try to deliver queued DMs into the cell's live session pane. Runs every
    tick. NEVER raises (the daemon must keep draining). Opt-in: no-op unless
    --auto-act. Fail-safe toward defer: only inject on POSITIVE idle."""
    if not state.auto_act:
        return
    try:
        if not state.queue.pending():
            return
        # Wake-gate: only inject when an ACTIONABLE DM is queued. If only
        # ride-along entries (fyi/status/broadcast-answer) are pending, hold
        # them — they deliver in the batch when the next actionable DM wakes
        # the cell. This is what prevents fyi-churn on an idle cell. Not a
        # stall (no deferred bump) — an intentional wait.
        if not state.queue.any_wake():
            return
        pane = session_bridge.resolve_session_pane(state.session_name)
        if pane is None:
            # Headless / non-standard cell — stay surface-only; DMs remain
            # queued (already logged). Not an error.
            return
        st = session_bridge.probe_pane(pane)
        if st == "modal":
            if session_bridge.try_dismiss_safe_modal(pane) and \
                    session_bridge.probe_pane(pane) == "idle":
                st = "idle"
        if st != "idle":
            n = state.queue.bump_deferred()
            if stall_alert.is_alert_tick(n):
                stall_alert.send_stall_alert(
                    state.gateway, state.token, state.self_name,
                    n, len(state.queue.pending()),
                )
            return
        entries = state.queue.pending()
        block = _render_delivery_block(entries)
        if session_bridge.inject(pane, block):
            state.queue.remove({e["id"] for e in entries})
            state.queue.reset_deferred()
        # inject failure → leave queued, retry next tick (no counter bump —
        # the cell was idle; a send failure is transient, not a stall).
    except Exception as exc:  # noqa: BLE001 — bridge must never crash the loop
        print(f"[swarph-daemon] delivery error (continuing): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
```

In `_drain_loop`, call delivery every tick — insert the single line **after** the `try/except` around `await _drain_iteration(state)` and **immediately before** the existing `delay = _select_next_poll_seconds(state)` line (so it runs each tick, whether or not new DMs arrived — this is what retries deferred entries):

```python
    while not state.shutdown_requested:
        try:
            await _drain_iteration(state)
        except Exception as exc:  # noqa: BLE001 — loud-on-error per §16.4
            print(
                f"[swarph-daemon] iteration error (continuing): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        attempt_delivery(state)   # deliver queued DMs every tick (guarded, never raises)

        delay = _select_next_poll_seconds(state)   # <-- existing line, unchanged
        # ... existing 1-second-chunk sleep loop unchanged below ...
```

- [ ] **Step 4: Run the delivery tests + the existing daemon suite**

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_daemon_delivery.py tests/test_daemon_command.py -q`
Expected: PASS — the 7 new delivery tests + all existing daemon tests still green (the `_route_to_handler` change is behavior-compatible: surface-only path unchanged).

- [ ] **Step 5: Integration test on a REAL throwaway tmux session**

Add to `tests/test_daemon_delivery.py` (guarded so CI without tmux skips cleanly):

```python
import shutil
import subprocess
import time
import pytest
from swarph_cli.multiplexer import find_multiplexer


@pytest.mark.skipif(find_multiplexer() is None, reason="no tmux/psmux available")
def test_end_to_end_injects_into_real_pane(tmp_path, monkeypatch):
    mux = find_multiplexer()
    sess = "swarph-bridge-it"
    subprocess.run([mux, "kill-session", "-t", sess],
                   capture_output=True)
    # A pane whose current command is a shell won't be resolved (we need a
    # 'claude'/'node' command). Simulate by launching a long-lived process
    # renamed to 'node', and print the idle sentinel so probe_pane == idle.
    subprocess.run(
        [mux, "new-session", "-d", "-s", sess,
         "bash -c 'echo \"? for shortcuts\"; exec -a node sleep 60'"],
        check=True)
    time.sleep(0.5)
    try:
        import swarph_cli.session_bridge as sb
        pane = sb.resolve_session_pane(sess)
        assert pane is not None, "resolve must find the node pane"
        assert sb.probe_pane(pane) == "idle"
        assert sb.inject(pane, "HELLO_BRIDGE_MARKER") is True
        time.sleep(0.3)
        cap = subprocess.run([mux, "capture-pane", "-p", "-t", pane],
                             capture_output=True, text=True).stdout
        assert "HELLO_BRIDGE_MARKER" in cap
    finally:
        subprocess.run([mux, "kill-session", "-t", sess], capture_output=True)
```

Run: `cd /home/ubuntu/swarph-cli && python -m pytest tests/test_daemon_delivery.py::test_end_to_end_injects_into_real_pane -q`
Expected: PASS on a box with tmux (lab-ovh); SKIPPED where no multiplexer is installed.

- [ ] **Step 6: Version bump**

Edit `pyproject.toml`: change `version = "0.32.2"` → `version = "0.33.0"`.

Run: `cd /home/ubuntu/swarph-cli && python -m pytest -q` (full suite — nothing else regressed).
Expected: all pass (bridge tests + the pre-existing suite).

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/daemon.py pyproject.toml tests/test_daemon_delivery.py
git commit -m "feat(bridge): wire delivery-into-session into the daemon drain loop (v0.33.0)"
```

---

## Done Criteria

- Green tests: `session_bridge` (probe/inject/resolve), `DeliveryQueue`, `stall_alert`, daemon delivery unit tests, and the real-pane integration test (or SKIPPED where no multiplexer). Pre-existing suite still green.
- The bridge is wired into `swarph daemon` behind `--auto-act`; surface-only is unchanged when the flag is off.
- End-to-end delivery proven on a real tmux pane.
- Version bumped 0.32.2 → 0.33.0.

**NOT part of this build (post-merge, separate):** the validation rollout on workstation-lc (the finder) + lab-ovh, then droplet / razorpeter / gpu-wsl / gemini-researcher; PyPI release; per-cell `--auto-act` provisioning.

**Rollout note (from Task 3's live verify):** a cell's tmux/psmux session name must match what pane resolution is given. Verified on lab-ovh: the daemon `self_name` is `lab-ovh` but the agent session is named `lab` — so lab-ovh's daemon must launch with `SWARPH_SESSION_NAME=lab`. Cells whose session name already equals their mesh id (e.g. `science-claude`, `drop-on-meta-edge`, `gridiron`) need no override. Where neither matches and no override is set, `resolve_session_pane` returns `None` → surface-only (safe, never mis-injects).
