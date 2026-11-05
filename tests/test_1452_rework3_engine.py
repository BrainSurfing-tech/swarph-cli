"""Card #1452 rework 3 (board note 89855, items 1-4). Item 5 is the PR body.

Real `_monitor_deliver`, real `MonitorState`, real sink classes. Sends and
pane reads are patched; the clock is `mesh.time`. The engine and the sinks
are not faked.

Item 4's mutant (and the rework-2 mutants) run on a temporary copy of the
source, one pytest node each, and must kill that mutant. They do not write
the checkout's mesh.py.

Run: python -m pytest tests/test_1452_rework3_engine.py -v
"""
import importlib.util
from pathlib import Path

import pytest

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog

_REWORK1_PATH = Path(__file__).with_name("test_1452_rework1_engine.py")
_spec = importlib.util.spec_from_file_location("card1452_rework1_from_r3", _REWORK1_PATH)
_rework1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rework1)

_REWORK2_PATH = Path(__file__).with_name("test_1452_rework2_engine.py")
_spec2 = importlib.util.spec_from_file_location("card1452_rework2_from_r3", _REWORK2_PATH)
_rework2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_rework2)

_Rig = _rework1._Rig
T0 = _rework1.T0
SINKS = _rework1.SINKS


# ── Item 1 (route A): a new DM resets the refresh count and does not zero
# the stall marker. Bound alert, the new DM wakes, the drain closes the
# row. A later stall opens its own row.
@pytest.mark.parametrize("kind", SINKS)
def test_item1_bound_alert_new_dm_wakes_drain_closes_row(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.drive(T0, T0 + 7800)
    assert rig.send_times() == [0, 600, 1800, 4200]
    assert len(rig.alert_calls) == 1
    assert int(round(rig.alert_calls[0][0] - T0)) == 7800
    assert int(rig.led.get("standing_refresh_alerted_id", 0) or 0) != 0
    assert rig.clear_calls == []

    rig.arrive(2)
    rig.poll(T0 + 8100)
    assert rig.send_times() == [0, 600, 1800, 4200, 8100], (
        "the new DM must wake the cell")
    assert rig.led["standing_refresh_count"] == 0
    assert int(rig.led.get("standing_refresh_alerted_id", 0) or 0) != 0, (
        "a new DM must not zero the stall marker")
    assert rig.clear_calls == [], "the row stays open across the new wake"
    assert rig.led.get("wake_outstanding") is True

    rig.unread["n"] = 0
    rig.poll(T0 + 8130)
    assert len(rig.clear_calls) == 1, "the row closes at the drain"
    assert rig.clear_calls[0][0] == T0 + 8130
    assert rig.led.get("deferred_ticks", 0) == 0
    assert not rig.led.get("wake_outstanding")
    assert int(rig.led.get("standing_refresh_alerted_id", 0) or 0) == 0
    assert rig.send_times() == [0, 600, 1800, 4200, 8100], (
        "the drain itself sends nothing")

    rig.unread["n"] = 1
    rig.arrive(3)
    rig.poll(T0 + 8160)
    assert rig.send_times()[-1] == 8160
    rig.drive(T0 + 8190, T0 + 16000)
    assert rig.send_times() == [
        0, 600, 1800, 4200, 8100, 8160, 8760, 9960, 12360]
    assert len(rig.alert_calls) == 2, "a later stall opens its own row"
    assert int(round(rig.alert_calls[1][0] - T0)) == 15960
    assert len(rig.clear_calls) == 1, "the new row is still open"


# ── Item 2 (route B): a tick alert inside a plain-tmux turn, DMs read
# inside the turn. The row closes at that drain. The turn's end, with
# nothing unread, does not wake. No count read without an alert or a flag.
def test_item2_tick_alert_inside_plain_tmux_turn_drain_closes_no_wake(
        monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, "tmux")
    running = {"on": True}
    monkeypatch.setattr(mesh, "_agent_running", lambda _t: running["on"])
    reads = {"n": 0}

    def _count(*_a, **_k):
        reads["n"] += 1
        return rig.unread["n"]

    monkeypatch.setattr(watchdog, "_gateway_unread_count", _count)

    rig.poll(T0 - 30)  # nothing owed, no flag, no alert
    assert reads["n"] == 0, "no extra read without an alert or a wake flag"

    rig.unread["n"] = 4
    rig.arrive(1)
    for i in range(60):
        rig.poll(T0 + 30 * i)
    assert reads["n"] == 0, (
        "ticks below _STALL_FIRST and no wake flag: no count read")
    assert len(rig.alert_calls) == 1
    assert rig.alert_calls[0][1][3] == 60, "this is the deferred-tick alert"
    assert rig.send_times() == []
    assert rig.clear_calls == []
    assert int(rig.led.get("deferred_ticks", 0)) == 60
    assert not rig.led.get("wake_outstanding")

    # Still inside the turn, mail still unread: the alert stands, so the
    # count is read, and the row stays open.
    for i in range(60, 63):
        before = reads["n"]
        rig.poll(T0 + 30 * i)
        assert reads["n"] == before + 1
        assert rig.clear_calls == []
    assert rig.send_times() == []

    drain_at = T0 + 30 * 63
    rig.unread["n"] = 0
    rig.poll(drain_at)
    assert len(rig.clear_calls) == 1, "the row closes at the drain, inside the turn"
    assert rig.clear_calls[0][0] == drain_at
    assert rig.led.get("deferred_ticks", 0) == 0
    assert int(rig.led["last_delivered_id"]) == 1
    assert not rig.led.get("wake_outstanding")
    assert rig.send_times() == []

    reads_after = reads["n"]
    for i in range(64, 67):
        rig.poll(T0 + 30 * i)
    assert reads["n"] == reads_after, (
        "once the alert and the flag are both down, no extra read")
    assert rig.send_times() == []

    running["on"] = False  # the turn ends with nothing unread
    for i in range(67, 71):
        rig.poll(T0 + 30 * i)
    assert rig.send_times() == [], "no wake on nothing unread at the turn's end"
    assert reads["n"] == reads_after
    assert len(rig.clear_calls) == 1


# ── Item 3: science-claude mutant F4. Turn running at the first
# _agent_running read, idle at the second, send refused. The head keeps
# the wake flag, so the attempts are 30, 630, 1830, 4230 and the alert
# is at 7830. Dropping the flag-keep makes this fail.
def test_item3_failed_refresh_keeps_flag_running_then_idle_refused(
        monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, "tmux")
    calls = {"n": 0}

    def _agent(_t):
        calls["n"] += 1
        # Odd calls are idle, even calls are running. The fresh wake is
        # the first call (idle, send). Each later deliver reads running
        # first (clears the flag) and idle second (the refused send).
        return calls["n"] % 2 == 0

    monkeypatch.setattr(mesh, "_agent_running", _agent)
    refused = {"on": False}

    def _wake(_target):
        rig.sends.append((rig.clock["now"], None))
        return not refused["on"]

    monkeypatch.setattr(mesh, "_tmux_wake", _wake)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.poll(T0 + 30)  # the good wake; nothing is newly owed after it
    assert rig.send_times() == [30]
    assert rig.led.get("wake_outstanding") is True
    assert calls["n"] == 1
    refused["on"] = True
    seen = {}

    def _on_poll(t):
        if t == T0 + 660:
            seen["flag"] = rig.led.get("wake_outstanding")

    rig.drive(T0 + 60, T0 + 20000, on_poll=_on_poll)
    assert seen.get("flag") is True, (
        "failed refresh did not keep the wake flag")
    assert rig.send_times() == [30, 630, 1830, 4230], (
        "failed refresh did not keep the wake flag; "
        f"attempts {rig.send_times()}")
    assert len(rig.alert_calls) == 1
    assert int(round(rig.alert_calls[0][0] - T0)) == 7830
    assert rig.led.get("wake_outstanding") is True


def test_item3_mutant_failed_refresh_not_keeping_flag_run_alone_on_temp_copy():
    """F4, run alone on a temporary copy: the failed refresh does not put
    the wake flag back. The sequence test must fail."""
    needle = (
        "                    if isinstance(unread, int) and unread > 0:\n"
        "                        led[\"wake_outstanding\"] = True\n"
        "                led[\"standing_refresh_count\"] = refresh_count + 1\n"
    )
    replacement = (
        "                led[\"standing_refresh_count\"] = refresh_count + 1\n"
    )

    def mutator(src: str) -> str:
        assert src.count(needle) == 1, (
            "the failed-refresh flag keep is not a single block")
        return src.replace(needle, replacement, 1)

    proc = _rework2._run_mutant_alone_on_temp_copy(
        mutator,
        "tests/test_1452_rework3_engine.py::"
        "test_item3_failed_refresh_keeps_flag_running_then_idle_refused")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "mutant survived: failed refresh not keeping the wake flag\n" + out)
    assert "failed refresh did not keep the wake flag" in out, out
