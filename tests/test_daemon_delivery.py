import asyncio
from pathlib import Path

import swarph_cli.commands.daemon as d
from swarph_cli.commands.daemon import DaemonState, attempt_delivery, _render_delivery_block


def _state(tmp_path, auto_act=True) -> DaemonState:
    return DaemonState(
        self_name="cell", state_dir=tmp_path, gateway="http://gw",
        token="tok", poll_s=1, auto_act=auto_act,
    )


def _dm(i, kind="question", thread_id=None):
    return {"id": i, "from_node": "peer", "kind": kind,
            "thread_id": thread_id, "content": f"m{i}",
            "created_at": "t"}


def test_once_mode_runs_a_complete_tick(tmp_path, monkeypatch):
    # --once is a single COMPLETE tick: drain THEN deliver (not drain only),
    # so `swarph daemon --once --auto-act` actually injects, matching one loop
    # iteration.
    calls = []

    async def fake_iter(state):
        calls.append("drain")

    monkeypatch.setattr(d, "_resolve_token", lambda tf: "tok")
    monkeypatch.setattr(d, "_drain_iteration", fake_iter)
    monkeypatch.setattr(d, "attempt_delivery", lambda state: calls.append("deliver"))
    rc = d.run_daemon(["--once", "--auto-act", "--self", "x",
                       "--gateway", "http://gw", "--state-dir", str(tmp_path)])
    assert rc == 0
    assert calls == ["drain", "deliver"]


def test_render_block_lists_entries():
    block = _render_delivery_block([
        {"from": "droplet", "kind": "question", "content": "ping?"},
    ])
    assert "mesh delivery" in block
    assert "droplet" in block and "ping?" in block


def test_delivery_injects_on_idle(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    injected = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "idle")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: injected.update(pane=p, text=t) or True)
    attempt_delivery(s)
    assert injected["pane"] == "%1"
    assert "m1" in injected["text"]
    assert s.queue.pending() == []          # delivered → dequeued
    assert s.queue.deferred_ticks == 0


def test_delivery_defers_on_busy_and_counts(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "busy")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: (_ for _ in ()).throw(AssertionError("must not inject")))
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]   # not lost
    assert s.queue.deferred_ticks == 1


def test_delivery_stall_alert_fires_at_threshold(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    s.queue.deferred_ticks = 5             # next bump → 6 → alert
    alerts = []
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "busy")
    monkeypatch.setattr(d.stall_alert, "send_stall_alert",
                        lambda *a, **k: alerts.append(a) or True)
    attempt_delivery(s)
    assert len(alerts) == 1               # fired exactly once at tick 6


def test_delivery_holds_ride_along_only(tmp_path, monkeypatch):
    # only a fyi (ride-along, wake=False) is queued → must NOT wake an idle
    # cell; stays queued, no deferred bump (intentional wait, not a stall).
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1, kind="fyi"))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not wake on fyi")))
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]
    assert s.queue.deferred_ticks == 0


def test_delivery_batches_ride_along_with_actionable(tmp_path, monkeypatch):
    # a question (wake) + a fyi (ride-along) → the wake delivers BOTH in one block
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1, kind="question"))
    s.queue.enqueue(_dm(2, kind="fyi"))
    injected = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "idle")
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: injected.update(text=t) or True)
    attempt_delivery(s)
    assert "m1" in injected["text"] and "m2" in injected["text"]
    assert s.queue.pending() == []


def test_delivery_noop_when_not_auto_act(tmp_path, monkeypatch):
    s = _state(tmp_path, auto_act=False)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(AssertionError("must not resolve")))
    attempt_delivery(s)                    # surface-only: no delivery attempt
    assert [e["id"] for e in s.queue.pending()] == [1]


def test_delivery_surface_only_when_no_pane(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: None)
    attempt_delivery(s)                    # headless cell → queued, no crash
    assert [e["id"] for e in s.queue.pending()] == [1]


def test_session_name_env_override(tmp_path, monkeypatch):
    # a cell whose tmux session name differs from its mesh self_name sets
    # SWARPH_SESSION_NAME; resolution uses THAT, not self_name.
    monkeypatch.setenv("SWARPH_SESSION_NAME", "lab")
    s = _state(tmp_path)
    assert s.session_name == "lab"
    s.queue.enqueue(_dm(1))
    seen = {}
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: seen.update(name=n) or None)
    attempt_delivery(s)
    assert seen["name"] == "lab"          # resolved by session_name, not self_name "cell"


def test_attempt_delivery_never_raises(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane",
                        lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    attempt_delivery(s)                    # must swallow the exception


def test_delivery_holds_when_inject_fails_no_bump(tmp_path, monkeypatch):
    # idle pane but inject() fails → entries stay queued, deferred NOT bumped
    # (idle-but-send-failed is transient, not a stall).
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))                       # question → wake
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "idle")
    monkeypatch.setattr(d.session_bridge, "inject", lambda p, t: False)
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]
    assert s.queue.deferred_ticks == 0


def test_delivery_defers_on_stuck_modal(tmp_path, monkeypatch):
    # a modal that won't dismiss to idle defers like busy (bump), never injects.
    s = _state(tmp_path)
    s.queue.enqueue(_dm(1))
    monkeypatch.setattr(d.session_bridge, "resolve_session_pane", lambda n: "%1")
    monkeypatch.setattr(d.session_bridge, "probe_pane", lambda p: "modal")
    monkeypatch.setattr(d.session_bridge, "try_dismiss_safe_modal", lambda p: False)
    monkeypatch.setattr(d.session_bridge, "inject",
                        lambda p, t: (_ for _ in ()).throw(AssertionError("must not inject")))
    attempt_delivery(s)
    assert [e["id"] for e in s.queue.pending()] == [1]
    assert s.queue.deferred_ticks == 1


def test_render_block_truncates_oversized_content():
    big = "x" * 5000
    block = _render_delivery_block([{"from": "p", "kind": "question", "content": big}])
    assert "…(truncated)" in block
    assert len(block) < 5000


import shutil
import subprocess
import time
import pytest
from swarph_cli.multiplexer import find_multiplexer


@pytest.mark.skipif(find_multiplexer() is None, reason="no tmux/psmux available")
def test_end_to_end_injects_into_real_pane(tmp_path, monkeypatch):
    mux = find_multiplexer()
    sess = "swarph-bridge-it"
    subprocess.run([mux, "kill-session", "-t", sess],
                   capture_output=True)
    # A pane whose current command is a shell won't be resolved (we need a
    # 'claude'/'node' command). Simulate by launching a long-lived process
    # renamed to 'node', and print the idle sentinel so probe_pane == idle.
    subprocess.run(
        [mux, "new-session", "-d", "-s", sess,
         "bash -c 'echo \"? for shortcuts\"; exec -a node sleep 60'"],
        check=True)
    time.sleep(0.5)
    try:
        import swarph_cli.session_bridge as sb
        pane = sb.resolve_session_pane(sess)
        assert pane is not None, "resolve must find the node pane"
        assert sb.probe_pane(pane) == "idle"
        assert sb.inject(pane, "HELLO_BRIDGE_MARKER") is True
        time.sleep(0.3)
        cap = subprocess.run([mux, "capture-pane", "-p", "-t", pane],
                             capture_output=True, text=True).stdout
        assert "HELLO_BRIDGE_MARKER" in cap
    finally:
        subprocess.run([mux, "kill-session", "-t", sess], capture_output=True)
