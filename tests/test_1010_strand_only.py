"""#1010 STRAND-ONLY (lab's ruling after science-claude's #1101 FAIL, the
5th on PR 534): PR 534 ships NO pane idle/busy classifier change — every
pane keeps base 0.72.3's behaviour — and the strand is fixed in the
LEDGER layer alone.

THE STRAND (measured by droplet on friendly-coder): the tmux sink's
standing-wake path returned True with ZERO keystrokes. The monitor
advanced `last_delivered_id`, owed nothing from then on, and the DM
batch was never looked at again — every DM "delivered" in the log while
the cell idled at an empty prompt, typing its only wake, forever. The
repro that opened #1010: an ANSWERED wake, a monitor RESTART, a new DM.

THE FIX (no classification): the standing-wake hold reports the hold,
not a delivery. deliver returns None — the cursor does not advance, the
batch stays OWED and is retried on every poll — and BASE'S OWN stale
bound (_WAKE_STALE_S = 600 s from the injection) re-injects and types
at the first idle poll past it. Bounded, loud, never silently dropped.

>>> RED on e445877 (base 0.72.3): the monitor-level repro below never
typed — the first poll returned True with zero keystrokes, the cursor
advanced, and every later poll owed nothing (assert wake_calls == 1
fails with 0). The unit pins of the hold returned True there too.
"""
from __future__ import annotations

import pathlib
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


def _rig(monkeypatch, *, composer="clear", unread=3,
         session_created=None, running=False):
    """Pin every observation deliver() makes; record the keystrokes.

    running defaults to False: the #1010 repro is the ANSWERED wake — the
    turn already ran, the pane idles at an empty prompt. The rig pins
    `_capture_pane_lines` too: the conftest guard fails any unmocked tmux
    call, and deliver() reads the pane for the grok-block check before
    any of this."""
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: ["row"])
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: session_created)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running)
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread)

    def fake_wake(target):
        calls["wake"] += 1
        return True

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


# ── the hold: loud, bounded, zero keystrokes, nothing marked delivered ───

def test_standing_wake_hold_is_loud_bounded_and_keeps_the_dm_owed(
        monkeypatch, capsys):
    """#1010 clause 2: a DM suppressed by a standing wake is NEVER
    marked delivered while untyped. The hold says what it is doing — the
    trust window, the wake's age, and that the stale bound types — and
    returns None, so the monitor's cursor stays put and the batch is
    retried next poll. >>> RED on e445877: this returned True — delivery
    reported, zero keystrokes, the DM stranded."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", unread=3,
                 session_created=now - 7200, running=False)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["wake"] == 0 and calls["enter"] == 0, "zero keys inside the window"
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is True, "the standing wake is undisturbed"
    out = capsys.readouterr().out
    assert "standing wake inside its 600s trust window" in out, out
    assert "DM held owed" in out
    assert "zero keys" in out
    assert "re-injects and types" in out
    # the monitor's DEFERRED line names the real cause, not human text
    assert "standing wake" in (sink.deferred_reason or "")
    assert "human text" not in (sink.deferred_reason or "")


def test_no_keystroke_sooner_than_base_inside_the_window(monkeypatch):
    """The FAIL arm: no keystroke SOONER than base. Repeated polls inside
    the trust window type nothing — the anti-stack property the
    edge-trigger exists for (#312) survives the hold. Base stacked zero
    keys here too; the difference is only that base also claimed
    delivery."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", unread=3,
                 session_created=now - 7200, running=False)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")
    for _ in range(5):
        assert sink.deliver(state, [{"id": 9}], 9) is None
    assert calls["wake"] == 0 and calls["enter"] == 0


def test_held_dm_is_typed_when_the_600s_window_lapses(monkeypatch):
    """#1010 clause 2, the other arm: the held DM does not stay held
    forever — BASE'S OWN stale bound (a wake undrained for
    _WAKE_STALE_S) re-injects, and the first idle poll past the bound
    carries a real keystroke. The DM is then delivered for real."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", unread=3,
                 session_created=now - 7200, running=False)
    state = _owed_state(injected_at=now - 601)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1, "the stale bound re-injects — a keystroke"
    led = state.ledger("tmux:cursor-lin")
    assert led["last_wake_injected_at"] > now - 5, "the retry re-anchors"


# ── the original repro, at the monitor: answered wake, restart, new DM ────

class _MonitorStubState(_StubState):
    """The poll-loop pass reads: sinks, the observed cursor, the ledgers,
    the idle-guard clock, and the inbox log for replay."""

    def __init__(self, observed_id, delivered_id, ledgers=None, inbox_log=None):
        super().__init__()
        if ledgers is not None:
            self._ledgers = ledgers  # the restart shape: one persisted ledger
        self.sinks = [mesh.TmuxSink("cursor-lin")]
        self.ledgers = self._ledgers
        self.observed = {"last_msg_id": observed_id}
        self.min_interval_s = 60
        self.deliveries: dict = {}
        self.ledgers_path = pathlib.Path("/dev/null")
        self.log_prefix = "[test]"
        self.inbox_log_path = inbox_log or pathlib.Path(
            "/tmp/1010-strand-no-such-inbox.log")
        self.replay_limit = 50
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


def test_the_original_repro_types_by_600s_even_after_a_restart(monkeypatch,
                                                               capsys):
    """THE #1010 repro, end to end through the poll loop: an ANSWERED
    wake (turn ran and finished; the flag stands, injected 60 s ago), a
    monitor RESTART (a fresh state over the SAME ledger), a new DM.

    At the head: the first poll HOLDS (zero keys, cursor unmoved, loud
    line); 600 s later the stale bound re-injects — ONE keystroke — and
    the ledger finally advances. Nothing strands and nothing types
    sooner than base.

    >>> RED on e445877: the first poll reported delivery with ZERO
    keystrokes, the cursor advanced to 10, and every later poll owed
    nothing — the DM was never typed (wake_calls stays 0)."""
    now = time.time()
    calls = _rig(monkeypatch, composer="clear", unread=3,
                 session_created=now - 7200, running=False)

    # an answered wake, 60 s inside its trust window; the monitor owes DM 10
    ledgers: dict = {}
    state = _MonitorStubState(observed_id=10, delivered_id=9, ledgers=ledgers)
    state.ledger("tmux:cursor-lin")["last_wake_injected_at"] = now - 60

    _poll(monkeypatch, state, unread=3)  # poll 1: the hold
    led = ledgers["tmux:cursor-lin"]
    assert led["last_delivered_id"] == 9, "nothing marked delivered untyped"
    assert calls["wake"] == 0 and calls["enter"] == 0
    assert "DM held owed" in capsys.readouterr().out
    assert led.get("deferred_ticks") == 1, "the deferral is counted, not a failure"

    # monitor restart: a fresh state over the SAME persisted ledger
    state2 = _MonitorStubState(observed_id=10, delivered_id=9, ledgers=ledgers)
    # 600 s pass; the same injection is now past base's stale bound
    ledgers["tmux:cursor-lin"]["last_wake_injected_at"] = time.time() - 601

    _poll(monkeypatch, state2, unread=3)  # poll 2: past the bound
    assert calls["wake"] == 1, "the stale bound re-injects and TYPES"
    assert ledgers["tmux:cursor-lin"]["last_delivered_id"] == 10

    _poll(monkeypatch, state2, unread=3)  # poll 3: owes nothing now
    assert calls["wake"] == 1, "delivered for real — no repeat keystroke"
    assert "delivered to tmux:cursor-lin up to id 10" in capsys.readouterr().out