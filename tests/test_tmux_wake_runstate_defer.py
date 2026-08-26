"""#619: never send a wake keystroke into a RUNNING TUI — cursor's follow-up
queue is input-gated, so a queued wake on an unattended cell never fires.

Measured live on cursor-lin, 2026-08-26, three times in one morning:

  09:57Z  manual inject + ONE Enter mid-turn -> queued -> fired at 10:00Z,
          but ONLY because the commander typed at 10:00Z (his input flushed
          the queue — visible in the pane's 'follow-ups' box).
  10:09Z  second controlled injection -> fired only after his next input.
          Two instances, zero auto-fires: the queue is INPUT-GATED.
  10:01Z  the monitor's stale re-inject mid-turn -> verify loop hammered 4
          Enters -> failures=1 for a wake that never became a turn.

The fix is not to understand the queue better but to never need it: defer
(None — wake stays owed, no failure counted) while the composer row carries
the run-state hint, and inject into the first IDLE composer, where Enter
submits immediately — the path the fleet has run for weeks.
"""

from __future__ import annotations

import time

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog


class _StubState:
    def __init__(self):
        self.gateway = "http://gw:8788"
        self.self_name = "cursor-lin"
        self.token = "tok"
        self._ledgers: dict = {}

    def ledger(self, name: str) -> dict:
        return self._ledgers.setdefault(
            name, {"last_delivered_id": 0, "last_delivery_at": 0.0,
                   "consecutive_failures": 0})


def _rig(monkeypatch, *, composer="clear", unread=3, session_created=None,
         wake_result=True, running=False):
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: session_created)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running)
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread)

    def fake_wake(target):
        calls["wake"] += 1
        return wake_result

    def fake_enter(target):
        calls["enter"] += 1
        return True

    monkeypatch.setattr(mesh, "_tmux_wake", fake_wake)
    monkeypatch.setattr(mesh, "_tmux_enter", fake_enter)
    return calls


def _owed_state(injected_at=None):
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    if injected_at is not None:
        led["last_wake_injected_at"] = injected_at
    return state


# ── the deferral, at every keystroke site ────────────────────────────────

def test_fresh_inject_defers_while_running(monkeypatch):
    """The measured 10:01Z shape: mid-turn injection must not happen."""
    calls = _rig(monkeypatch, composer="clear", running=True)
    state = _StubState()  # no wake_outstanding — fresh path
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # deferred, not failed
    assert calls["wake"] == 0
    led = state.ledger("tmux:cursor-lin")
    assert not led.get("wake_outstanding")  # nothing claimed, wake stays owed


def test_fresh_inject_fires_when_idle(monkeypatch):
    """Control: the deferral must not swallow the normal path."""
    calls = _rig(monkeypatch, composer="clear", running=False)
    state = _StubState()
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True


def test_stale_reinject_defers_while_running(monkeypatch):
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", session_created=now - 7200,
                 running=True)
    state = _owed_state(injected_at=now - mesh._WAKE_STALE_S - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["wake"] == 0  # the stale wake is retried next poll, not queued


def test_respawn_reinject_defers_while_running(monkeypatch):
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", session_created=now - 30,
                 running=True)
    state = _owed_state(injected_at=now - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["wake"] == 0


def test_wake_nudge_defers_while_running(monkeypatch):
    """Wake text observed sitting in the composer, agent mid-turn: an Enter
    would QUEUE it (input-gated) rather than submit it. Defer to idle."""
    calls = _rig(monkeypatch, composer="wake", running=True)
    state = _StubState()  # fresh path
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["enter"] == 0


def test_outstanding_wake_nudge_defers_while_running(monkeypatch):
    calls = _rig(monkeypatch, composer="wake", running=True)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["enter"] == 0


# ── the run-state signal itself ──────────────────────────────────────────

def _lines_running():
    return ["some scrollback", "→ Add a follow-up          ctrl+c to stop",
            "Kimi K3 Max · 50%"]


def _lines_idle():
    return ["some scrollback", "→ Add a follow-up", "Kimi K3 Max · 50%"]


def test_agent_running_reads_the_composer_row(monkeypatch):
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _lines_running())
    assert mesh._agent_running("cursor-lin") is True
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _lines_idle())
    assert mesh._agent_running("cursor-lin") is False


def test_agent_running_ignores_scrollback_quoting_the_hint(monkeypatch):
    """The hint string in HISTORY (a discussion about the TUI — which has
    literally happened on cursor-lin) must not read as running. Only the
    composer row counts."""
    lines = ["user: why does it say ctrl+c to stop?", "→ Add a follow-up"]
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: lines)
    assert mesh._agent_running("cursor-lin") is False


def test_agent_running_unreadable_is_none(monkeypatch):
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: None)
    assert mesh._agent_running("cursor-lin") is None


# ── #620: the flag must clear when the wake is OBSERVED to have fired ────

def test_running_observation_clears_the_zombie_flag(monkeypatch):
    """The measured 10:22Z defect: wake fired at 10:18, turn is RUNNING,
    flag still stands with a fresh anchor. The running observation must
    clear it — the cell is awake, the wake's job is done — and the fresh
    path then defers (#619) instead of claiming a standing wake."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", session_created=now - 7200,
                 running=True)
    state = _owed_state(injected_at=now - 60)  # fresh anchor, inside window
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # deferred, not "standing"
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is False  # zombie cleared
    assert calls["wake"] == 0 and calls["enter"] == 0


def test_flag_stays_when_no_turn_is_observed(monkeypatch):
    """Idle + fresh anchor + no running observation: the wake is plausibly
    still pending — the standing path must NOT be disturbed."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", session_created=now - 7200,
                 running=False)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True  # standing wake
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True
    assert calls["wake"] == 0


def test_unknown_runstate_does_not_clear(monkeypatch):
    """None (pane momentarily unreadable) is not a firing observation —
    only a positively-observed turn clears the flag."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", session_created=now - 7200,
                 running=False)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: None)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True  # standing, undisturbed
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True
    assert calls["wake"] == 0


def test_full_zombie_cycle_fires_the_next_wake(monkeypatch):
    """The end-to-end contract: wake fires -> turn runs (flag clears,
    delivery defers) -> turn ends -> next poll injects for real."""
    now = time.time()
    running = {"v": True}
    calls = _rig(monkeypatch, composer="clear", session_created=now - 7200)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running["v"])
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None   # mid-turn: cleared+deferred
    running["v"] = False                                  # turn ends
    assert sink.deliver(state, [{"id": 9}], 9) is True   # idle: real injection
    assert calls["wake"] == 1
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True
