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


def _rig(monkeypatch, *, composer="clear", unread=3, wake_result=True,
         running=False):
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running)
    monkeypatch.setattr(mesh, "_opencode_in_progress", lambda t: False)
    # #1083 hermeticity: the grok-block and muse probes read the pane —
    # an unpatched seam reached the REAL tmux binary.
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: None)
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


def test_outstanding_wake_defers_while_running(monkeypatch):
    """#1010 cut of the old stale/respawn legs: a flag plus a mid-turn cell
    defers whatever the wake's age — the suppression question is decided by
    the pane, never by the clock or the session."""
    calls = _rig(monkeypatch, composer="clear", running=True)
    state = _owed_state(injected_at=1.0)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["wake"] == 0  # retried next poll, not queued mid-turn


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
    # #1083: the POSITIVE idle signature — the Tip row sits directly above
    # the bare placeholder composer (measured on real idle captures;
    # tests/pane_renders/cursor/idle-lin.txt).
    return ["some scrollback", "Tip: mesh is quiet", "→ Add a follow-up",
            "Kimi K3 Max · 50%"]


def test_agent_running_reads_the_composer_row(monkeypatch):
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _lines_running())
    assert mesh._agent_running("cursor-lin") is True
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _lines_idle())
    assert mesh._agent_running("cursor-lin") is False


def test_agent_running_ignores_scrollback_quoting_the_hint(monkeypatch):
    """The hint string in HISTORY (a discussion about the TUI — which has
    literally happened on cursor-lin) must not read as RUNNING: only the
    composer row counts. #1083: it is not positively IDLE either — the
    quote carries no parens (never busy-shaped) but the pane shows no
    measured idle signature, so it reads UNKNOWN: held while a wake is
    in play, typed (silently, the historical contract) when none is."""
    lines = ["user: why does it say ctrl+c to stop?", "→ Add a follow-up"]
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: lines)
    assert mesh._agent_running("cursor-lin") is None


def test_agent_running_unreadable_is_none(monkeypatch):
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: None)
    assert mesh._agent_running("cursor-lin") is None


# ── #620: the flag must clear when the wake is OBSERVED to have fired ────

def test_running_observation_clears_the_zombie_flag(monkeypatch):
    """The measured 10:22Z defect: wake fired at 10:18, turn is RUNNING,
    flag still stands with a fresh anchor. The running observation must
    clear it — the cell is awake, the wake's job is done — and the fresh
    path then defers (#619) instead of claiming a standing wake."""
    calls = _rig(monkeypatch, composer="clear", running=True)
    state = _owed_state(injected_at=time.time() - 60)  # fresh anchor
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # deferred, not "standing"
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is False  # zombie cleared
    assert calls["wake"] == 0 and calls["enter"] == 0


def test_no_turn_observation_spends_the_wake_not_keeps_it(monkeypatch):
    """>>> #1010, red on main: main kept the flag here — 'the wake is
    plausibly still pending' — and the next DM was claimed delivered on the
    STANDING wake with zero keystrokes while the cell idled at an empty
    prompt. <<< Idle + clear + no running observation means the wake is
    SPENT: the flag drops and the next DM is typed."""
    calls = _rig(monkeypatch, composer="clear", running=False)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    led = state.ledger("tmux:cursor-lin")
    assert calls["wake"] == 1          # the next DM was TYPED
    assert led["wake_outstanding"] is True  # re-armed by the fresh inject


def test_unknown_runstate_holds_zero_keystrokes_within_the_turn_bound(monkeypatch, capsys):
    """>>> #1073 round 2 (#1077), red on 6c4180b: main treated an unknown
    runstate as 'undisturbed standing', and the #1010 spent rule then read
    it as positively idle — either way a keystroke could land in a pane
    whose busy render the detector does not know (science-claude measured
    '✻ Pondering… (esc to cancel)' at 6c4180b: the second DM went in).
    <<< None is NOT a positive turn observation, and it is NOT a positive
    idle observation either: while a wake of ours may still be turning,
    unknown holds ZERO keystrokes with a loud log naming the state, and
    the flag stands untouched (neither spent nor spent-claimed)."""
    calls = _rig(monkeypatch, composer="clear", running=False)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: None)
    # the detail seam must not read a REAL pane — script the fixture render
    monkeypatch.setattr(mesh, "_capture_pane_lines",
                        lambda t: ["✻ Pondering… (esc to cancel)",
                                   "→ Add a follow-up"])
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # held, not typed
    assert calls["wake"] == 0 and calls["enter"] == 0
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is True  # neither spent nor delivered
    out = capsys.readouterr().out
    assert "not positively recognised" in out and "zero keys" in out
    assert "Pondering" in out  # the log NAMES the unrecognised state


def test_full_zombie_cycle_fires_the_next_wake(monkeypatch):
    """The end-to-end contract: wake fires -> turn runs (flag clears,
    delivery defers) -> turn ends -> next poll injects for real."""
    running = {"v": True}
    calls = _rig(monkeypatch, composer="clear")
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running["v"])
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None   # mid-turn: cleared+deferred
    running["v"] = False                                  # turn ends
    assert sink.deliver(state, [{"id": 9}], 9) is True   # idle: real injection
    assert calls["wake"] == 1
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True


# ── #620 (per-poll re-arm): the drain must be observed WHEN it happens ────
# The in-deliver() re-arm only runs when a new DM is owed — and by then the
# inbox has re-filled, so unread == 0 is never seen and the fired wake's
# flag stands as a zombie (the measured 10:18-10:26Z silence). The re-arm
# now runs per poll, before the owes-nothing skip.


class _MonitorStubState(_StubState):
    def __init__(self, observed_id, delivered_id):
        super().__init__()
        self.sinks = [mesh.TmuxSink("cursor-lin")]
        self.ledgers = self._ledgers
        self.observed = {"last_msg_id": observed_id}
        self.min_interval_s = 60
        self.deliveries: dict = {}
        self.ledgers_path = "/dev/null"
        self.log_prefix = "[test]"
        led = self.ledger("tmux:cursor-lin")
        led["last_delivered_id"] = delivered_id
        led["last_delivery_at"] = time.time() - 3600
        led["wake_outstanding"] = True


def _poll(monkeypatch, state, unread):
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread)
    writes = []
    monkeypatch.setattr(mesh, "_write_ledgers_atomic",
                        lambda p, d: writes.append(dict(d)))
    mesh._monitor_deliver(state)
    return writes


def test_drain_observed_between_deliveries_clears_the_flag(monkeypatch):
    """THE measured hole: wake fired and the inbox drained BETWEEN owed
    deliveries (delivered == observed — the sink owes nothing, so deliver()
    never ran and never saw unread == 0). The per-poll re-arm catches it."""
    state = _MonitorStubState(observed_id=9, delivered_id=9)  # owes nothing
    writes = _poll(monkeypatch, state, unread=0)

    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is False
    assert writes  # the cleared flag is persisted, not just in-memory


def test_undrained_poll_keeps_the_flag(monkeypatch):
    """The #312 contract survives: a wake whose inbox never drained still
    stands — one wake covers everything until the cell actually reads."""
    state = _MonitorStubState(observed_id=9, delivered_id=9)
    writes = _poll(monkeypatch, state, unread=3)

    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True
    assert not writes


def test_unreadable_drain_signal_never_rearms_per_poll(monkeypatch):
    state = _MonitorStubState(observed_id=9, delivered_id=9)
    _poll(monkeypatch, state, unread=None)

    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True


def test_next_dm_after_the_drain_earns_a_fresh_wake(monkeypatch):
    """The full measured sequence, repaired: wake fires -> drain observed
    per-poll (flag cleared) -> new DM arrives -> fresh path injects."""
    calls = _rig(monkeypatch, composer="clear", running=False)
    state = _MonitorStubState(observed_id=9, delivered_id=9)
    _poll(monkeypatch, state, unread=0)  # the drain is seen, flag clears

    sink = state.sinks[0]
    assert sink.deliver(state, [{"id": 10}], 10) is True
    assert calls["wake"] == 1  # a REAL wake for the new DM
