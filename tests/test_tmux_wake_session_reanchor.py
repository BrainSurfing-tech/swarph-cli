"""#611 history, #1010 present: the wake anchor survives in the LEDGER, but
the PANE decides whether a wake is standing.

Measured live on cursor-lin, 2026-08-26 07:03-07:34Z: six DMs drained and
archived, ledger advanced to the newest id, zero failures — and zero wake
text injected for 31 minutes, because the session had been recreated at
05:37Z and the flag from the OLD pane read the NEW pane's clear composer as
"wake submitted, awaiting drain". The commander noticed what the ledger
could not.

#611's fix re-anchored the flag in SPACE (which pane: re-inject when the
session is newer than the last injection). #1010 (droplet, 2026-10-06,
friendly-coder's strand) removed the anchor arithmetic: a wake is standing
only while OBSERVED — unsubmitted text in the pane, or a mid-turn cell —
so a respawned session's idle empty prompt is spent like any other and the
next DM is typed. The anchor timestamp survives (it still dates the ledger
for humans); it no longer gates anything. The tests that pinned
'in-window suppression' are rewritten to the spent rule and are red on
main.
"""

from __future__ import annotations

import re
import time

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog


class _StubState:
    """The three attributes TmuxSink.deliver reads, plus the ledger store."""

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
    """Pin the observations deliver() makes; return the call recorders."""
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    # pin the run-state seam too — unpatched it reads the LIVE pane,
    # which is mid-turn (running) whenever the suite runs on a real cell.
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


def _owed_state(injected_at=None):
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    if injected_at is not None:
        led["last_wake_injected_at"] = injected_at
    return state


def test_a_respawned_sessions_idle_prompt_types_the_owed_wake(monkeypatch):
    """The measured defect, re-derived under #1010: the session was born
    after the last injection, so the flag describes a wake that died with
    the old pane — and its clear composer is simply an idle empty prompt.
    The owed DM is typed (main reached the same re-inject via the space
    anchor; #1010 reaches it via the pane)."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=now - 3600)  # wake died with the old pane
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1, "the owed wake must be typed into the new session"
    assert state.ledger("tmux:cursor-lin")["last_wake_injected_at"] > now - 5


def test_same_session_idle_empty_prompt_types(monkeypatch):
    """>>> #1010, red on main: main suppressed the re-inject here — 'same
    session + clear composer = submitted, not lost' — the exact suppression
    that stranded friendly-coder. <<< Same session, idle cell, empty prompt:
    the wake is spent and the next DM is typed."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1


def test_unknown_session_age_is_irrelevant_the_pane_decides(monkeypatch):
    """>>> #1010, red on main: unknown session age kept the old behaviour
    (no re-inject). <<< The anchor arithmetic is gone; the composer
    observation decides, and an idle empty prompt types."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1


def test_pre_upgrade_ledger_self_heals(monkeypatch):
    """A ledger written before the anchor split has no
    last_wake_injected_at. The pane decides: an idle empty prompt types."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state()  # no last_wake_injected_at key at all
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1


def test_spent_retype_deferral_propagates(monkeypatch):
    """The human adopting the composer mid-settle during the spent re-type
    defers exactly as on the fresh path — the wake stays owed, no failure
    counted."""
    _rig(monkeypatch, running=False, wake_result=None)
    state = _owed_state(injected_at=time.time() - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is None
    assert state.ledger("tmux:cursor-lin")["wake_outstanding"] is not True


def test_fresh_path_anchors_the_injection_timestamp(monkeypatch):
    """last_wake_injected_at is set where the wake LANDS, not where the
    ledger happens to advance — the split that keeps the ledger honest for
    the humans reading it."""
    now = time.time()
    calls = _rig(monkeypatch, running=False)
    state = _StubState()  # no wake_outstanding: the fresh path
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is True
    assert led["last_wake_injected_at"] > now - 5


def test_no_standing_claim_is_printed_anymore_because_none_is_made(monkeypatch,
                                                                   capsys):
    """>>> #1010, red on main: the STANDING-wake delivery report (delivered
    with zero keystrokes, logged so the lie was at least visible) is gone
    because the claim itself is gone — an idle empty prompt types. <<< The
    scenario that used to log 'STANDING wake' now injects."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    out = capsys.readouterr().out
    assert "STANDING wake" not in out
    assert "injected" not in out  # no stale-age prose either


def test_undated_wake_retypes_without_a_standing_report(monkeypatch, capsys):
    """An undated flag (pre-anchor ledger) is spent like any other."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state()  # no last_wake_injected_at key at all
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    assert "STANDING wake" not in capsys.readouterr().out


def test_retype_does_not_claim_a_standing_wake(monkeypatch, capsys):
    """A re-type IS fresh evidence — it must not wear standing-wake wording
    (which no longer exists) and the log must stay silent."""
    calls = _rig(monkeypatch, running=False)
    state = _owed_state(injected_at=time.time() - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    assert "STANDING wake" not in capsys.readouterr().out