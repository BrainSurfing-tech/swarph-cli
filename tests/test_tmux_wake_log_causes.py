"""#1077 clause (5), added to the accept by lab-ovh before the rework
closed: the log text must NAME THE REAL CAUSE.

Two measured mislabels, one test each:

  1. The engine's DEFERRED line hardcoded 'composer holds human text' for
     EVERY deferral — including a mid-turn one (#619), where no human is
     involved. Now the sink reports why it deferred (`deferred_reason`)
     and the engine prints that.

  2. An unsubmitted wake — the inject landed but never submitted, the
     text observably stuck in the composer — returned False without a
     failure_reason, so the engine's fallback printed 'the sink is
     probably gone (session restart / renamed target)' for a sink that is
     alive and holding our text. Now that site names the stuck wake.

Both tests are red on 97092a4 (the rework head that closed #1077 without
clause (5)) and on main.
"""

from __future__ import annotations

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog


class _Engine:
    """The slices of MonitorState that _monitor_deliver touches."""

    def __init__(self, tmp_path):
        self.observed = {"last_msg_id": 5}
        self.min_interval_s = 0.0
        self.replay_limit = 50
        self.inbox_log_path = tmp_path / "inbox.log"
        self.inbox_log_path.write_text("", encoding="utf-8")
        self.log_prefix = "[test]"
        self.deliveries = {}
        self.ledgers = {}
        self.ledgers_path = tmp_path / "ledgers.json"
        self.sinks = []

    def ledger(self, name):
        return self.ledgers.setdefault(name, mesh._new_ledger())


def _rig(monkeypatch, *, composer="clear", running=False, wake_ok=True,
         pending=False):
    calls = {"wake": 0, "enter": 0}
    monkeypatch.setattr(mesh, "_composer_state", lambda t: composer)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: running)
    monkeypatch.setattr(mesh, "_opencode_in_progress", lambda t: False)
    monkeypatch.setattr(mesh, "_grok_in_progress", lambda t: False)
    monkeypatch.setattr(mesh, "_wake_still_pending", lambda t: pending)
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: 1)

    def fake_wake(target):
        calls["wake"] += 1
        return wake_ok

    def fake_enter(target):
        calls["enter"] += 1
        return True

    monkeypatch.setattr(mesh, "_tmux_wake", fake_wake)
    monkeypatch.setattr(mesh, "_tmux_enter", fake_enter)
    return calls


def test_mid_turn_deferral_names_mid_turn_not_human_text(
        monkeypatch, tmp_path, capsys):
    """>>> RED on 97092a4 and main: the engine printed 'delivery DEFERRED
    for … (composer holds human text)' for a MID-TURN deferral — the
    human is not involved, and the log named the wrong cause. <<< The
    DEFERRED line now carries the sink's reason: mid-turn, the
    input-gated queue. No failure counted either way (a deferral is not a
    dead sink)."""
    _rig(monkeypatch, composer="clear", running=True)
    state = _Engine(tmp_path)
    state.sinks = [mesh.TmuxSink("sac")]
    mesh._monitor_deliver(state)

    out = capsys.readouterr().out
    assert "DEFERRED" in out
    assert "mid-turn" in out                      # the real cause, named
    assert "composer holds human text" not in out  # not the wrong cause
    led = state.ledgers["tmux:sac"]
    assert led["last_delivered_id"] == 0           # still owed
    assert led["consecutive_failures"] == 0        # not a failure


def test_human_busy_composer_still_names_human_text(
        monkeypatch, tmp_path, capsys):
    """The correction cuts both ways: a deferral for a composer that
    OBSERVABLY holds human text still says so."""
    _rig(monkeypatch, composer="busy")
    state = _Engine(tmp_path)
    state.sinks = [mesh.TmuxSink("sac")]
    mesh._monitor_deliver(state)

    out = capsys.readouterr().out
    assert "DEFERRED" in out
    assert "composer holds human text" in out


def test_unsubmitted_wake_names_the_stuck_wake_not_a_dead_sink(
        monkeypatch, tmp_path, capsys):
    """>>> RED on 97092a4 and main: the inject landed but never submitted
    — the text observably stuck in the composer — and the failure line
    fell back to 'the sink is probably gone (session restart / renamed
    target)' for a sink that is alive and holding our wake. <<< The
    failure names the stuck wake and says the sink is alive; the flag is
    armed so the next poll nudges instead of stacking."""
    _rig(monkeypatch, composer="clear", running=False,
         wake_ok=False, pending=True)
    state = _Engine(tmp_path)
    state.sinks = [mesh.TmuxSink("sac")]
    mesh._monitor_deliver(state)

    err = capsys.readouterr().err
    assert "DELIVERY FAILED" in err               # the failure stays loud
    assert "stuck" in err                        # the real cause, named
    assert "alive" in err
    assert "probably gone" not in err             # not the wrong cause
    led = state.ledgers["tmux:sac"]
    assert led["consecutive_failures"] == 1
    assert led["wake_outstanding"] is True         # nudge owed, no stacking