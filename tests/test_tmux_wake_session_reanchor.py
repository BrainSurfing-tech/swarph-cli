"""#611: `wake_outstanding` must be re-anchored to the tmux SESSION the wake
was injected into — a respawned cell was never re-woken, and the ledger
reported itself current.

Measured live on cursor-lin, 2026-08-26 07:03-07:34Z: six DMs drained and
archived, ledger advanced to the newest id, zero failures — and zero wake
text injected for 31 minutes, because the session had been recreated at
05:37Z and the flag from the OLD pane read the NEW pane's clear composer as
"wake submitted, awaiting drain". The commander noticed what the ledger
could not.

The split (one variable had answered two questions): `last_delivery_at` is
re-anchored by EVERY successful poll, including the silent no-inject path —
so it cannot date the last injection. `last_wake_injected_at` is set ONLY
where `_tmux_wake` actually lands. The clear-composer path re-injects when
the session is NEWER than the last injection, and only then.
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


def _rig(monkeypatch, *, composer="clear", unread=3, session_created=None,
         wake_result=True):
    """Pin the four observations deliver() makes; return the call recorders."""
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


def _owed_state(injected_at=None):
    state = _StubState()
    led = state.ledger("tmux:cursor-lin")
    led["wake_outstanding"] = True
    if injected_at is not None:
        led["last_wake_injected_at"] = injected_at
    return state


def test_respawned_session_re_injects_the_owed_wake(monkeypatch):
    """The measured defect: session born AFTER the last injection means the
    standing wake died with the old pane — re-inject, still gated."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 60)   # respawned a minute ago
    state = _owed_state(injected_at=now - 3600)           # wake died with the old pane
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1, "the owed wake must be re-injected into the new session"
    assert state.ledger("tmux:cursor-lin")["last_wake_injected_at"] > now - 5


def test_same_session_clear_composer_does_not_stack(monkeypatch):
    """The anti-stack property the edge-trigger exists for (#312): a wake
    injected into THIS session, undrained, earns NO second injection."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 3600)  # session predates the wake
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 0, "same session + clear composer = submitted, not lost"


def test_unknown_session_age_keeps_the_old_behaviour(monkeypatch):
    """session_created unreadable while capture-pane answered is a state we
    decline to act on FOR THE RESPAWN CHECK — no blind keystrokes. (#616
    narrows this: the TIME bound needs only the clock, so a stale wake still
    re-injects with age unknown; this test pins a wake INSIDE the trust
    window, where unknown age alone must not trigger anything.)"""
    now = time.time()
    calls = _rig(monkeypatch, session_created=None)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 0


def test_pre_upgrade_ledger_self_heals(monkeypatch):
    """A ledger written before this fix has no last_wake_injected_at. Default
    0 makes any real session newer — the first poll after the upgrade
    re-injects the owed wake. Exactly today's cursor-lin shape."""
    calls = _rig(monkeypatch, session_created=time.time() - 300)
    state = _owed_state()  # no last_wake_injected_at key at all
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1


def test_re_inject_deferral_propagates(monkeypatch):
    """The human adopting the composer mid-settle during the RE-inject defers
    exactly as on the fresh path — the wake stays owed, no failure counted."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 60, wake_result=None)
    state = _owed_state(injected_at=now - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is None
    assert calls["wake"] == 1


def test_fresh_path_anchors_the_injection_timestamp(monkeypatch):
    """last_wake_injected_at is set where the wake LANDS, not where the
    ledger happens to advance — the split that makes the re-anchor possible."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 3600)
    state = _StubState()  # no wake_outstanding: the fresh path
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    led = state.ledger("tmux:cursor-lin")
    assert led["wake_outstanding"] is True
    assert led["last_wake_injected_at"] > now - 5


def test_standing_wake_delivery_report_is_logged(monkeypatch, capsys):
    """The silent-True branch was the ONLY delivery report with zero log
    lines — a swallowed wake left no trace (cursor-win, 2026-08-26, a
    40-minute investigation that one log read would have collapsed). The
    report now names itself and the standing wake's age."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 3600)
    state = _owed_state(injected_at=now - 60)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 0
    out = capsys.readouterr().out
    assert "STANDING wake" in out
    assert "no keystroke this poll" in out
    assert re.search(r"injected \d+s ago", out), out


def test_standing_wake_without_anchor_says_so(monkeypatch, capsys):
    """Unknown session age + a pre-anchor ledger: the standing report must
    not print a nonsense epoch-sized age — it names the missing anchor."""
    _rig(monkeypatch, session_created=None)
    state = _owed_state()  # no last_wake_injected_at key at all
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    out = capsys.readouterr().out
    assert "STANDING wake" in out
    assert "predates the ledger anchor" in out


def test_reinject_path_does_not_claim_a_standing_wake(monkeypatch, capsys):
    """A re-injection IS fresh evidence — it must not wear the standing-wake
    wording, or the log line loses its meaning."""
    now = time.time()
    calls = _rig(monkeypatch, session_created=now - 60)
    state = _owed_state(injected_at=now - 3600)
    sink = mesh.TmuxSink("cursor-lin")

    assert sink.deliver(state, [], 29101) is True
    assert calls["wake"] == 1
    assert "STANDING wake" not in capsys.readouterr().out
