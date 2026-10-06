"""#616 history, #1010 present: a wake that never drained the inbox is
re-typed at the FIRST owed poll, not after a trust bound.

Measured live on cursor-lin, 2026-08-26 08:49-09:13Z, with the MERGED #611
fix running: a wake injected mid-turn at 08:49:17Z queued a TUI follow-up
that never fired (raced the commander's own line). Every DM after it was
marked delivered in silence — wake_outstanding=true, composer clear, same
session, "awaiting drain" — and the re-arm (unread == 0) is unreachable for
a sleeping cell: deliver() is only entered when a NEW DM exists, which makes
unread >= 1 by construction. The commander noticed what the ledger could
not, again: "why won't you wake when dm come in :'("

#616 bounded that trust in TIME (_WAKE_STALE_S: re-inject after 10 minutes).
#1010 (droplet, 2026-10-06, friendly-coder's strand) removed the bound
entirely: a wake is standing only while OBSERVED — the pane holds the
injected text unsubmitted, or the cell is mid-turn. An IDLE pane at an EMPTY
prompt spends the wake immediately, so there is no window left to bound.
Every #616 scenario below still pins real behavior; the ones that pinned
'in-window suppression' are rewritten to the spent rule and are red on
main.
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
    # #1083 hermeticity: the grok-block probe reads the pane — pin it.
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


def _owed_state(injected_at):
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    led["last_wake_injected_at"] = injected_at
    return state


def test_lost_wake_is_retyped_immediately_no_bound(monkeypatch):
    """The measured defect shape (#616): same session, clear composer, wake
    older than the old bound with no drain. The wake is lost — and under
    #1010 it is re-typed at THIS poll, not after _WAKE_STALE_S."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=now - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    led = state.ledger("tmux:cursor-lin")
    assert led["last_wake_injected_at"] > now - 5  # re-anchored by the inject


def test_fresh_window_does_not_suppress_an_idle_empty_prompt(monkeypatch):
    """>>> #1010, red on main: main suppressed the re-inject inside the
    trust window ('a pending wake must not be stacked by every poll') — the
    same suppression that stranded friendly-coder. <<< A wake injected a
    minute ago, a cell observed IDLE at an EMPTY prompt: the wake is spent
    regardless of age, and the next DM is typed."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=now - 60)  # 1 min old, the old bound was 10
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1


def test_a_wake_that_started_no_turn_is_retyped_next_owed_poll(monkeypatch):
    """>>> #1010, red on main: the old re-anchor rate-limited the retry to
    one per bound — a wake that never started a turn cost ten minutes of
    silence per retry. <<< The rate limit is the OBSERVED pane now: a pane
    still showing idle+empty on the next owed poll means the last wake
    started no turn, and the next DM is typed again."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=now - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    assert sink.deliver(state, [{"id": 10}], 10) is True
    assert calls["wake"] == 2  # no clock bound — the pane did the limiting


def test_spent_retype_defers_on_human_adoption(monkeypatch):
    """The politeness gate survives the spent re-type: a human who adopted
    the composer mid-settle defers — the wake stays owed, no failure
    counted, and nothing of ours is driving (the flag stays down)."""
    _rig(monkeypatch, running=False, wake_result=None)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is not True


def test_spent_retype_fails_loud_when_unreadable(monkeypatch):
    _rig(monkeypatch, running=False, wake_result=False)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is False


def test_a_respawned_session_is_spent_like_any_other(monkeypatch):
    """#611's space anchor is subsumed: a session newer than the last
    injection used to be the ONLY respawn signal that re-injected. Under
    #1010 the pane observation decides — an idle empty prompt types,
    respawn or not."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1


def test_undated_wake_is_spent_and_retyped(monkeypatch):
    """lab-ovh's rebase case: with injected_at absent (a pre-anchor ledger)
    the wake is undated. Under #1010 the date does not matter — the pane is
    the only evidence — so the wake is re-typed and the anchor is set where
    the wake lands."""
    calls = _rig(monkeypatch, running=False)
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True  # and NO last_wake_injected_at key
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    assert led["last_wake_injected_at"] > 0  # anchored by the re-type