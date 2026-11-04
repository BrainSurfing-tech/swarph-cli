"""Card #1452 rework 1 (board note 89766, items 1-7): standing-wake fixes
driven through the REAL `_monitor_deliver` engine.

Real MonitorState + real sink classes (TmuxSink / MuseSink / CodexSink);
only the sinks' send functions (and tmux pane reads) are patched, plus a
fake clock (`mesh.time`) and the gateway unread count. The engine and the
sinks are never faked. Each test notes which item it proves.

Run: python -m pytest tests/test_1452_rework1_engine.py -v
"""
import types

import pytest

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog
from swarph_cli import stall_alert as alerts

T0 = 1_000_000.0
BASE = getattr(mesh, "STANDING_WAKE_EXPIRY_S", 600.0)
MAX_REFRESH = getattr(mesh, "STANDING_WAKE_MAX_CAUGHTUP_REFRESHES", 3)
SINKS = ["tmux", "muse", "codex"]


def _dm(i):
    return {"id": i, "from_node": "lab-ovh", "kind": "question",
            "content": f"body-{i}", "read_at": None}


class _Rig:
    """One real MonitorState + one real sink, sends/clock/unread faked."""

    def __init__(self, monkeypatch, tmp_path, kind):
        self.kind = kind
        self.clock = {"now": T0}
        self.sends = []          # (abs_time, text_or_None)
        self.unread = {"n": 1}
        self.alert_calls = []    # (abs_time, args) of send_stall_alert
        self.clear_calls = []    # (abs_time, args) of clear_stall_alert
        fake_time = types.SimpleNamespace(
            time=lambda: self.clock["now"],
            monotonic=lambda: self.clock["now"],
            sleep=lambda s: None)
        monkeypatch.setattr(mesh, "time", fake_time)
        monkeypatch.setattr(watchdog, "_gateway_unread_count",
                            lambda *a, **k: self.unread["n"])
        monkeypatch.setattr(
            alerts, "send_stall_alert",
            lambda *a: self.alert_calls.append((self.clock["now"], a)) or True)
        monkeypatch.setattr(
            alerts, "clear_stall_alert",
            lambda *a: self.clear_calls.append((self.clock["now"], a)) or True)
        if kind == "muse":
            monkeypatch.setattr(
                mesh, "_muse_send",
                lambda s, t: self.sends.append((self.clock["now"], t)) or (True, ""))
            sink = mesh.MuseSink("sess-r1")
        elif kind == "codex":
            monkeypatch.setattr(
                mesh, "_codex_send",
                lambda th, t: self.sends.append((self.clock["now"], t)) or (True, ""))
            sink = mesh.CodexSink("thread-r1")
        else:
            monkeypatch.setattr(
                mesh, "_tmux_wake",
                lambda t: self.sends.append((self.clock["now"], None)) or True)
            monkeypatch.setattr(mesh, "_tmux_enter", lambda t: True)
            monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: ["row"])
            monkeypatch.setattr(mesh, "_composer_state", lambda t: "clear")
            monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: T0 - 7200)
            monkeypatch.setattr(mesh, "_agent_running", lambda t: False)
            monkeypatch.setattr(mesh, "_pane_in_mode", lambda t: False)
            monkeypatch.setattr(mesh, "_opencode_in_progress", lambda *a, **k: None)
            monkeypatch.setattr(mesh, "_grok_in_progress", lambda t: False)
            monkeypatch.setattr(mesh, "_opencode_turn_finished_target", lambda t: False)
            monkeypatch.setattr(mesh, "_grok_turn_finished_target", lambda t: False)
            sink = mesh.TmuxSink("cell-r1:0.0")
        self.sink = sink
        self.state = mesh.MonitorState(
            self_name="cell-r1", state_dir=tmp_path, gateway="http://gw:8788",
            token="tok", sinks=[sink], poll_s=30, min_interval_s=0)

    @property
    def led(self):
        return self.state.ledger(self.sink.name)

    def arrive(self, i):
        mesh._log_dm(self.state, _dm(i))
        self.state.observed["last_msg_id"] = i

    def poll(self, now):
        self.clock["now"] = float(now)
        mesh._monitor_deliver(self.state)

    def drive(self, t_start, t_end, *, step=30, on_poll=None):
        t = t_start
        while t <= t_end:
            self.clock["now"] = float(t)
            if on_poll is not None:
                on_poll(t)
            mesh._monitor_deliver(self.state)
            t += step

    def send_times(self):
        return [int(round(s[0] - T0)) for s in self.sends]


# ── Item 1: Tmux fresh wake resets the progress clock; anchor is the LATER
# of wake_progress_at and last_wake_injected_at. Progress, drain, 3h idle,
# fresh wake, a DM 60s later — no second wake inside N (600s).
def test_item1_tmux_fresh_wake_no_double_wake(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, "tmux")
    unread_by_t = {T0: 3, T0 + 30: 2, T0 + 60: 1}

    def first_episode(t):
        if t == T0:
            rig.arrive(1)
        rig.unread["n"] = unread_by_t[t]

    rig.drive(T0, T0 + 60, on_poll=first_episode)
    assert rig.send_times() == [0]
    rig.unread["n"] = 0                       # drain
    rig.poll(T0 + 90)
    assert not rig.led.get("wake_outstanding")
    rig.drive(T0 + 120, T0 + 10860)           # 3h idle, nothing owed
    fresh_at = T0 + 10890
    rig.unread["n"] = 2
    rig.arrive(2)
    rig.poll(fresh_at)                        # fresh wake
    assert rig.send_times() == [0, 10890]
    rig.arrive(3)                             # a DM 60s later
    rig.drive(fresh_at + 60, fresh_at + 570)
    assert rig.send_times() == [0, 10890], (
        "a DM 60s after a fresh Tmux wake must not wake again inside N")
    rig.poll(fresh_at + BASE)                 # at N the wake may refresh
    assert rig.send_times() == [0, 10890, 10890 + int(BASE)]


# ── Item 2: the engine evaluates a standing wake on EVERY poll, also when
# last_delivered_id >= observed (nothing newly owed — the lost-wake case),
# and records nonzero drain progress there. One unread DM, no later DM,
# still gets a refresh; progress seen while caught up restarts the clock.
@pytest.mark.parametrize("kind", SINKS)
def test_item2_caughtup_lost_wake_gets_refresh(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 3
    rig.arrive(1)
    rig.drive(T0, T0 + BASE)                  # fresh at 0, refresh at N
    assert rig.send_times() == [0, int(BASE)], (
        "one unread DM with no later DM must still get a standing-wake refresh")
    assert rig.led["last_delivered_id"] == 1  # only real sends moved evidence
    if kind in ("muse", "codex"):
        assert "unread" in rig.sends[1][1], (
            "item 5: the refresh text names what is unread, not the old body")
        assert "body-1" not in rig.sends[1][1]
    # Nonzero drain progress is recorded while caught up: unread 3 -> 2.
    rig.unread["n"] = 2
    rig.drive(T0 + BASE + 30, T0 + BASE + 300)
    assert rig.led["wake_progress_at"] == T0 + BASE + 30
    assert rig.led["wake_backoff_k"] == 0     # progress resets the backoff
    # A new DM restoring unread=3 does not instantly expire the wake: the
    # progress at +630 re-anchored the clock, so expiry is at +1230.
    rig.unread["n"] = 3
    rig.clock["now"] = T0 + 1000
    rig.arrive(2)
    mesh._monitor_deliver(rig.state)
    assert rig.send_times() == [0, int(BASE)]
    rig.drive(T0 + 1020, T0 + 1260)
    assert rig.send_times() == [0, int(BASE), 1230]


# ── Item 3: with nothing newly owed, at most STANDING_WAKE_MAX_CAUGHTUP_
# REFRESHES (3) refreshes for the same delivered id, then stop and raise
# the existing stall alert ONCE. A new DM resets the count.
@pytest.mark.parametrize("kind", SINKS)
def test_item3_caughtup_refresh_bound_and_reset(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.drive(T0, T0 + 20000)
    # Fresh wake at 0 + exactly 3 caught-up refreshes (600 / +1200 / +2400
    # under the item-4 backoff), then no further send for the same id.
    assert rig.send_times() == [0, 600, 1800, 4200]
    assert len(rig.sends) == 1 + MAX_REFRESH
    assert len(rig.alert_calls) == 1, "the stall alert is raised exactly once"
    assert rig.led["standing_refresh_count"] == MAX_REFRESH
    # A new DM resets the count: the owed expiry send fires, delivery
    # advances, and caught-up refreshes are available for the new id.
    rig.clock["now"] = T0 + 20100
    rig.arrive(2)
    mesh._monitor_deliver(rig.state)
    assert len(rig.sends) == 5
    assert rig.led["last_delivered_id"] == 2
    assert rig.led["standing_refresh_count"] == 0
    rig.drive(T0 + 20130, T0 + 24000)
    assert rig.send_times()[-1] == 23700     # one refresh for the new id
    assert len(rig.alert_calls) == 1         # still only the one alert


# ── Item 4: backoff — the limit is 600s x 2^k, capped at 3600s; k resets
# on drain progress or a drain to 0. A 60-minute turn with a DM every 2
# minutes (Codex and Muse) wakes at about 0, 10, 30 minutes; 24h never
# draining at a DM every 5 minutes gives about 26 wakes.
@pytest.mark.parametrize("kind", ["codex", "muse"])
def test_item4_backoff_wake_times_60min(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 30
    state = {"next_id": 1}

    def arrivals(t):
        if (t - T0) % 120 == 0:
            rig.arrive(state["next_id"])
            state["next_id"] += 1

    rig.drive(T0, T0 + 3600, on_poll=arrivals)
    assert rig.send_times() == [0, 600, 1800], (
        f"60-min turn, DM every 2 min: wakes at ~0, 10, 30 min; "
        f"got {rig.send_times()}")


@pytest.mark.parametrize("kind", ["codex", "muse"])
def test_item4_backoff_wake_count_24h(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 30
    state = {"next_id": 1}

    def arrivals(t):
        if (t - T0) % 300 == 0:
            rig.arrive(state["next_id"])
            state["next_id"] += 1

    rig.drive(T0, T0 + 86400, step=60, on_poll=arrivals)
    times = rig.send_times()
    assert times[:4] == [0, 600, 1800, 4200]
    assert len(times) == 26, f"24h never draining: ~26 wakes, got {len(times)} {times}"


# ── Item 5: on an observed unread of 0 the ledger advances to observed
# with NO send — a healthy drain costs no spurious wake.
@pytest.mark.parametrize("kind", SINKS)
def test_item5_drain_to_zero_no_spurious_wake(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 1
    rig.arrive(1)
    rig.poll(T0)
    assert len(rig.sends) == 1
    rig.unread["n"] = 2                       # a DM during the standing wake
    rig.arrive(2)
    rig.poll(T0 + 30)
    assert len(rig.sends) == 1
    rig.unread["n"] = 0                       # the cell drains fully
    rig.drive(T0 + 60, T0 + 1800)
    assert len(rig.sends) == 1, "a healthy drain must cost no spurious wake"
    assert rig.led["last_delivered_id"] == 2, (
        "unread 0 advances the ledger to observed with no send")
    assert not rig.led.get("wake_outstanding")
    assert rig.led["wake_backoff_k"] == 0


# ── Item 6: a cell that never drains must still REACH the stall alert —
# no (refresh) send may hide it by resetting the ticks or clearing it.
@pytest.mark.parametrize("kind", SINKS)
def test_item6_never_drains_reaches_stall_alert(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)
    rig.unread["n"] = 10
    state = {"next_id": 1}

    def arrivals(t):
        if (t - T0) % 120 == 0:
            rig.arrive(state["next_id"])
            state["next_id"] += 1

    rig.drive(T0, T0 + 7200, on_poll=arrivals)
    assert len(rig.sends) >= 3, "refresh sends happened during the stall"
    assert len(rig.alert_calls) >= 1, (
        "a never-draining cell must reach the stall alert despite the sends")
    assert rig.clear_calls == [], "no send may clear/hide the stall alert"
    assert rig.alert_calls[0][0] - T0 <= 3600


# ── Item 7: unread RISING during the hold is not drain progress — the
# wake still expires. Kills the `<` -> `!=` progress-comparison mutant
# (a rising count must not restart the clock).
@pytest.mark.parametrize("kind", SINKS)
def test_item7_rising_unread_still_expires(monkeypatch, tmp_path, kind):
    rig = _Rig(monkeypatch, tmp_path, kind)

    def rising(t):
        if t == T0:
            rig.arrive(1)
        rig.unread["n"] = 1 + int((t - T0) // 60)

    rig.drive(T0, T0 + 900, on_poll=rising)
    times = rig.send_times()
    assert len(times) == 2 and times[1] <= int(BASE) + 60, (
        f"unread rising during the hold must not suppress expiry; got {times}")
