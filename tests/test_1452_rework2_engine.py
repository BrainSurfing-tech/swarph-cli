"""Card #1452 rework 2 (board note 89818, items 1-4): caught-up flag,
drain-closes-stall, dead-sink refresh bound, and two real-source mutants.

Real `_monitor_deliver`, real `MonitorState`, real sink classes. Sends and
pane reads are patched; the clock is `mesh.time`. The engine and the sinks
are not faked. Item 5 is the PR body, not a test.

The two mutant tests each apply one mutation to the real `mesh.py` (not a
copy), run one killing test alone in a subprocess, then restore the file.

Run: python -m pytest tests/test_1452_rework2_engine.py -v
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import swarph_cli.commands.mesh as mesh

_REWORK1_PATH = Path(__file__).with_name("test_1452_rework1_engine.py")
_spec = importlib.util.spec_from_file_location("card1452_rework1", _REWORK1_PATH)
_rework1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rework1)

_Rig = _rework1._Rig
T0 = _rework1.T0
BASE = _rework1.BASE
SINKS = _rework1.SINKS

_MESH_PY = Path(mesh.__file__).resolve()
_REPO = Path(__file__).resolve().parents[1]


def _refuse_after(rig, monkeypatch):
    """Replace the rig's send with one that records every attempt and,
    once `refused['on']` is set, returns a hard failure."""
    refused = {"on": False}

    if rig.kind == "tmux":
        def wake(_target):
            rig.sends.append((rig.clock["now"], None))
            return not refused["on"]
        monkeypatch.setattr(mesh, "_tmux_wake", wake)
    elif rig.kind == "muse":
        def send(_session, text):
            rig.sends.append((rig.clock["now"], text))
            if refused["on"]:
                return (False, "refused")
            return (True, "")
        monkeypatch.setattr(mesh, "_muse_send", send)
    else:
        def send(_thread, text):
            rig.sends.append((rig.clock["now"], text))
            if refused["on"]:
                return (False, "refused")
            return (True, "")
        monkeypatch.setattr(mesh, "_codex_send", send)
    return refused


# ── Item 1: plain tmux. A turn running when the caught-up refresh falls
# due used to clear the wake flag (#620), and with nothing newly owed the
# engine never re-entered. Keep the flag when deliver() returns None and
# unread is still above 0. Refreshes continue after the turn, then one
# stall alert.
def test_item1_plain_tmux_turn_running_at_refresh_keeps_wake_and_one_stall_alert(
        monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, "tmux")
    running = {"on": False}
    monkeypatch.setattr(mesh, "_agent_running", lambda _t: running["on"])
    rig.unread["n"] = 1
    rig.arrive(1)
    # The refresh is due at BASE. The turn covers that poll and the two
    # after it, then ends. The next poll is idle and still expired.
    turn_lo = T0 + BASE - 30
    turn_hi = T0 + BASE + 60
    first = int(BASE) + 90                       # first idle poll after the turn
    second = first + int(BASE) * 2               # k=1 → 1200s
    third = second + int(BASE) * 4               # k=2 → 2400s
    alert_at = third + int(mesh.STANDING_WAKE_EXPIRY_CAP_S)  # k=3 → cap 3600s
    t = T0
    while t <= T0 + 20000:
        running["on"] = turn_lo <= t <= turn_hi
        rig.poll(t)
        if turn_lo <= t <= turn_hi:
            assert rig.led.get("wake_outstanding") is True, (
                "a caught-up deferral with unread still up must keep the wake flag")
            assert rig.send_times() == [0], (
                "a turn covering the due refresh must not consume it")
        t += 30
    assert rig.send_times() == [0, first, second, third], (
        f"refreshes must continue after the turn; got {rig.send_times()}")
    assert len(rig.alert_calls) == 1, "one stall alert after the refresh bound"
    assert int(round(rig.alert_calls[0][0] - T0)) == alert_at
    assert rig.led.get("wake_outstanding") is True


# ── Item 2: the drain branch closes a standing stall row.
@pytest.mark.parametrize("kind", SINKS)
def test_item2_bound_alert_then_drain_clears(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.drive(T0, T0 + 20000)
    assert len(rig.alert_calls) == 1
    assert rig.clear_calls == []
    assert int(rig.led.get("standing_refresh_alerted_id", 0) or 0) != 0
    drain_at = T0 + 20030
    rig.unread["n"] = 0
    rig.poll(drain_at)
    assert len(rig.clear_calls) == 1, "the bound alert's row closes at the drain"
    assert rig.clear_calls[0][0] == drain_at
    assert rig.led.get("deferred_ticks", 0) == 0
    assert not rig.led.get("wake_outstanding")


@pytest.mark.parametrize("kind", SINKS)
def test_item2_tick_alert_then_drain_clears(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 5
    rig.arrive(1)
    next_id = 2
    t = T0
    while t <= T0 + 20000 and not rig.alert_calls:
        observed = int(rig.state.observed.get("last_msg_id", 0))
        if t > T0 and int(rig.led["last_delivered_id"]) >= observed:
            rig.arrive(next_id)
            next_id += 1
        rig.poll(t)
        t += 30
    assert rig.alert_calls, (
        f"a run of owed defers must reach the tick alert; "
        f"ticks={rig.led.get('deferred_ticks')}")
    assert rig.alert_calls[0][1][3] >= 60, (
        "this alert is the deferred-tick row, not the caught-up bound")
    assert int(rig.led.get("deferred_ticks", 0)) >= 60
    assert rig.clear_calls == []
    assert rig.led.get("wake_outstanding") is True
    drain_at = t
    rig.unread["n"] = 0
    rig.poll(drain_at)
    assert len(rig.clear_calls) == 1, "the tick alert's row closes at the drain"
    assert rig.clear_calls[0][0] == drain_at
    assert rig.led.get("deferred_ticks", 0) == 0
    assert not rig.led.get("wake_outstanding")


# ── Item 3: a refused caught-up refresh counts like a sent one, so a dead
# sink does not retry every poll. One wake, then the sink refuses, nothing
# newly owed: the attempts inside 2h stay about 4 (the wake plus the three
# bounded refreshes), not 100 or more, and the stall alert follows.
@pytest.mark.parametrize("kind", SINKS)
def test_item3_dead_sink_send_refused_nothing_owed_about_four_attempts_then_alert(
        monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    refused = _refuse_after(rig, monkeypatch)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.poll(T0)                              # the wake; nothing is owed after it
    assert rig.send_times() == [0]
    assert int(rig.led["last_delivered_id"]) >= int(rig.state.observed["last_msg_id"])
    refused["on"] = True
    rig.drive(T0 + 30, T0 + 7200)             # 2h, send refused, nothing newly owed
    in_two_hours = rig.send_times()
    assert 3 <= len(in_two_hours) <= 5, in_two_hours
    assert len(in_two_hours) < 100
    assert in_two_hours == [0, 600, 1800, 4200], in_two_hours
    assert rig.alert_calls == [], "the alert follows the 2h window, it is not inside it"
    rig.drive(T0 + 7230, T0 + 20000)
    assert len(rig.alert_calls) == 1
    assert int(round(rig.alert_calls[0][0] - T0)) == 7800
    assert rig.send_times() == [0, 600, 1800, 4200], (
        "the 3-refresh bound stops the dead sink; no further attempt")


# ── Item 4a: the caught-up refresh text carries the unread count.
# Kills a mutant that drops `{unread}` from `_standing_refresh_prompt`.
def test_item4_refresh_text_names_unread_count(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, "muse")
    rig.unread["n"] = 7
    rig.arrive(1)
    rig.drive(T0, T0 + BASE)
    assert len(rig.sends) >= 2
    text = rig.sends[1][1]
    assert "You have 7 unread mesh DM(s)." in text, (
        "refresh text dropped the unread count")


def _run_one_test_alone(nodeid: str) -> subprocess.CompletedProcess:
    """One pytest node, in its own process, against the tree as it is on disk."""
    env = dict(**__import__("os").environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "-q", "--tb=short", nodeid],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _mutate_real_source(mutator):
    """Apply `mutator` to the real mesh.py, yield, always restore.

    Not a copy: the bytes under src/ are what the subprocess imports.
    """
    assert _MESH_PY == _REPO / "src" / "swarph_cli" / "commands" / "mesh.py"
    original = _MESH_PY.read_text(encoding="utf-8")
    mutated = mutator(original)
    assert mutated != original
    _MESH_PY.write_text(mutated, encoding="utf-8")
    try:
        yield
    finally:
        _MESH_PY.write_text(original, encoding="utf-8")


def test_item4_mutant_refresh_text_dropping_unread_count_run_alone_on_real_source():
    """Mutant (a), run alone on the real source: drop the unread count from
    the caught-up refresh text. The count assertion must fail."""
    needle = 'f"You have {unread} unread mesh DM(s). "'
    replacement = '"You have unread mesh DM(s). "'

    def mutator(src: str) -> str:
        assert src.count(needle) == 1, "refresh-count line is not where the mutant aims"
        return src.replace(needle, replacement, 1)

    gen = _mutate_real_source(mutator)
    next(gen)
    try:
        proc = _run_one_test_alone(
            "tests/test_1452_rework2_engine.py::"
            "test_item4_refresh_text_names_unread_count")
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "mutant survived: refresh text dropping the unread count\n" + out)
    assert "refresh text dropped the unread count" in out, out


def test_item4_mutant_tmux_fresh_path_reset_and_later_of_anchor_together_run_alone_on_real_source():
    """Mutant (b), run alone on the real source: remove BOTH the tmux
    fresh-path `_note_fresh_wake` and the later-of anchor (prefer the old
    progress stamp). Each change alone is covered by the other; together
    they restore the 60s double wake, and item 1's test must fail."""
    fresh = "            _note_fresh_wake(led)\n"
    anchor = (
        "    anchor = max(float(led.get(\"wake_progress_at\") or 0.0),\n"
        "                 float(led.get(\"last_wake_injected_at\") or 0.0))\n"
    )
    anchor_mutant = (
        "    anchor = float(led.get(\"wake_progress_at\") or 0.0) or float(\n"
        "        led.get(\"last_wake_injected_at\") or 0.0)\n"
    )

    def mutator(src: str) -> str:
        assert src.count(fresh) == 1, "tmux fresh-path reset is not a single line"
        assert src.count(anchor) == 1, "later-of anchor is not a single expression"
        return src.replace(fresh, "", 1).replace(anchor, anchor_mutant, 1)

    gen = _mutate_real_source(mutator)
    next(gen)
    try:
        proc = _run_one_test_alone(
            "tests/test_1452_rework1_engine.py::"
            "test_item1_tmux_fresh_wake_no_double_wake")
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "mutant survived: tmux fresh-path reset and later-of anchor "
        "removed together\n" + out)
    assert "must not wake again inside N" in out, out
