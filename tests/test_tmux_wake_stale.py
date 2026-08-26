"""#616: a wake that never drained the inbox is LOST, not pending — bound the
trust, then verify by retry.

Measured live on cursor-lin, 2026-08-26 08:49-09:13Z, with the MERGED #611
fix running: a wake injected mid-turn at 08:49:17Z queued a TUI follow-up
that never fired (raced the commander's own line). Every DM after it was
marked delivered in silence — wake_outstanding=true, composer clear, same
session, "awaiting drain" — and the re-arm (unread == 0) is unreachable for
a sleeping cell: deliver() is only entered when a NEW DM exists, which makes
unread >= 1 by construction. The commander noticed what the ledger could
not, again: "why won't you wake when dm come in :'("

#611 re-anchored the flag in SPACE (which pane). This bounds it in TIME
(how long a drain-less wake stays trusted): _WAKE_STALE_S.
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
         wake_result=True):
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: session_created)
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


def test_stale_wake_reinjects_and_reanchors(monkeypatch):
    """The measured defect: same session, clear composer, wake older than the
    bound with no drain — the wake is lost; re-inject, don't wait forever."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 7200)  # same session
    state = _owed_state(injected_at=now - mesh._WAKE_STALE_S - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    led = state.ledger("tmux:cursor-lin")
    assert led["last_wake_injected_at"] > now - 5  # re-anchored


def test_fresh_wake_does_not_stack(monkeypatch):
    """Inside the trust window the old behaviour stands: a pending wake must
    not be stacked by every poll."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 7200)
    state = _owed_state(injected_at=now - 60)  # 1 min old, bound is 10
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 0


def test_reanchor_rate_limits_the_retry(monkeypatch):
    """A successful re-injection re-anchors the clock: the very next poll is
    inside the window again and stays silent — at most one retry per bound."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 7200)
    state = _owed_state(injected_at=now - mesh._WAKE_STALE_S - 60)
    sink = mesh.TmuxSink("cursor-lin")

    sink.deliver(state, [{"id": 9}], 9)
    assert calls["wake"] == 1
    sink.deliver(state, [{"id": 10}], 10)
    assert calls["wake"] == 1  # no second stack


def test_stale_reinject_defers_on_human_adoption(monkeypatch):
    """The politeness gate survives the retry: a human who adopted the
    composer mid-settle defers, the wake stays owed, no failure counted."""
    now = time.time()
    _rig(monkeypatch, session_created=now - 7200, wake_result=None)
    state = _owed_state(injected_at=now - mesh._WAKE_STALE_S - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is True


def test_stale_reinject_fails_loud_when_unreadable(monkeypatch):
    now = time.time()
    _rig(monkeypatch, session_created=now - 7200, wake_result=False)
    state = _owed_state(injected_at=now - mesh._WAKE_STALE_S - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is False


def test_respawn_still_wins_over_the_clock(monkeypatch):
    """#611's space anchor is checked first: a session NEWER than the
    injection re-injects even inside the time bound."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 30)  # respawned 30s ago
    state = _owed_state(injected_at=now - 60)            # inside the window
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1


def test_undated_wake_is_always_stale(monkeypatch):
    """lab-ovh's rebase review: with injected_at absent (a pre-anchor ledger),
    now - 0 > _WAKE_STALE_S is always true, so the stale path re-injects even
    when the session age is UNKNOWABLE — the #327 hole (unknown age kept the
    old behaviour, stuck forever) is closed by the clock. This reachability
    is what retires #332's 'injection predates the ledger anchor' log arm:
    pinned here so its removal reads as reviewed behaviour, not an accident."""
    calls = _rig(monkeypatch, session_created=None)  # tmux cannot answer
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True  # and NO last_wake_injected_at key
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert calls["wake"] == 1
    assert led["last_wake_injected_at"] > 0  # anchored by the re-inject
