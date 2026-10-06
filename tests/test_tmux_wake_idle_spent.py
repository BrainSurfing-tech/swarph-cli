"""Card #1010 — standing-wake suppression strands idle cells: a wake is
standing only while OBSERVED, and an idle cell at an empty prompt means the
wake is SPENT.

Measured on friendly-coder (droplet, 2026-10-06): its tail was down, typing
was its only wake, and the tmux sink's standing-wake claim reported every
delivered DM with ZERO keystrokes while the cell slept at an empty prompt —
the wake had submitted, its turn had run and ended, and the flag read the
next DM as covered. The cell idled on with a delivered inbox.

THE RULE (accept, #1072): the tmux sink treats a wake as STANDING only
while the pane shows the injected text UNsubmitted, or the cell is
MID-TURN. An idle pane at an empty prompt spends the wake — the next DM is
typed. Standing state is not carried across a monitor restart without
re-checking the pane: every honor of the flag reads the pane first.

The first two tests are red on 0.72.3 (main): base claimed the DM
delivered on the standing wake with zero keystrokes. The boundary tests pin
the two shapes that DO keep a wake standing.
"""

from __future__ import annotations

import time

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog


class _StubState:
    def __init__(self, ledgers=None):
        self.gateway = "http://gw:8788"
        self.self_name = "cursor-lin"
        self.token = "tok"
        self._ledgers: dict = ledgers if ledgers is not None else {}

    def ledger(self, name: str) -> dict:
        return self._ledgers.setdefault(
            name, {"last_delivered_id": 0, "last_delivery_at": 0.0,
                   "consecutive_failures": 0})


def _rig(monkeypatch, *, composer="clear", unread=1, running=False,
         wake_result=True):
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
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


def test_idle_empty_prompt_spends_the_wake_and_types_the_next_dm(monkeypatch):
    """>>> RED ON 0.72.3: base returned True here with calls["wake"] == 0 —
    the DM was claimed delivered on the STANDING wake while the cell idled
    at an empty prompt (friendly-coder's strand). <<< The flag plus an idle
    clear composer means the wake submitted, its turn ran AND ended, and
    the cell went back to sleep with the inbox undrained: the next DM is
    TYPED, the flag re-arms, and the anchor moves to the new injection."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    led["last_wake_injected_at"] = now - 60  # fresh anchor, inside the old bound
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1            # the DM was typed
    assert calls["enter"] == 0           # no nudge — the composer was empty
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is True       # re-armed by the inject
    assert led["last_wake_injected_at"] > now - 5  # re-anchored where it landed


def test_persisted_flag_after_monitor_restart_is_rechecked_then_typed(monkeypatch):
    """>>> RED ON 0.72.3: the ledger flag persists across a monitor restart,
    and base honored it on the pane's clear composer — a restarted monitor
    claimed the DM delivered with zero keystrokes. <<< A restart does not
    get to carry standing state on faith: the flag is honored only after
    the pane is re-read, and an idle empty prompt spends it. The persisted
    ledger below is exactly what the new monitor process would load."""
    calls = _rig(monkeypatch, running=False)
    # the ledger a restarted monitor loads from disk: flag up, anchor fresh
    persisted = {"last_delivered_id": 0, "last_delivery_at": 0.0,
                 "consecutive_failures": 0, "wake_outstanding": True,
                 "last_wake_injected_at": time.time() - 60}
    state = _StubState(ledgers={"tmux:cursor-lin": dict(persisted)})
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1            # re-checked the pane, then typed
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True


def test_unsubmitted_wake_text_keeps_the_wake_standing_nudge_only(monkeypatch):
    """Boundary one: the pane OBSERVABLY holds the injected text
    unsubmitted — the wake is standing, and the sink sends ONE verified
    Enter, never new text (stacking was the commander's original defect)."""
    calls = _rig(monkeypatch, composer="wake", running=False)
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    led["last_wake_injected_at"] = time.time() - 60
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["enter"] == 1           # nudged
    assert calls["wake"] == 0            # no second copy typed


def test_midturn_cell_keeps_the_wake_no_keystroke(monkeypatch):
    """Boundary two: the cell is MID-TURN — the wake is standing (the turn
    it started is consuming the inbox), and NO keystroke of any kind goes
    in (#619: a keypress into a running TUI queues it input-gated)."""
    calls = _rig(monkeypatch, running=False)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: True)
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    led["last_wake_injected_at"] = time.time() - 60
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # deferred, not failed
    assert calls["wake"] == 0 and calls["enter"] == 0


def test_unreadable_pane_after_restart_fails_loud_not_blind(monkeypatch):
    """A restarted monitor that cannot read the pane must not honor the
    persisted flag either — unknown fails closed, loudly, with zero
    keystrokes."""
    calls = _rig(monkeypatch, composer=None, running=False)
    persisted = {"last_delivered_id": 0, "last_delivery_at": 0.0,
                 "consecutive_failures": 0, "wake_outstanding": True,
                 "last_wake_injected_at": time.time() - 60}
    state = _StubState(ledgers={"tmux:cursor-lin": dict(persisted)})
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is False
    assert calls["wake"] == 0 and calls["enter"] == 0