import swarph_cli.commands.daemon as daemon
from swarph_cli.commands.daemon import DaemonState, _render_delivery_block, attempt_delivery


def _state(tmp_path, auto_act=True) -> DaemonState:
    return DaemonState(
        self_name="cell",
        state_dir=tmp_path,
        gateway="http://gw",
        token="tok",
        poll_s=1,
        auto_act=auto_act,
    )


def _dm(i, kind="question", thread_id=None):
    return {
        "id": i,
        "from_node": "peer",
        "kind": kind,
        "thread_id": thread_id,
        "content": f"m{i}",
        "created_at": "t",
    }


def test_once_mode_runs_a_complete_tick(tmp_path, monkeypatch):
    calls = []

    async def fake_iteration(state):
        calls.append("drain")

    monkeypatch.setattr(daemon, "_resolve_token", lambda token_file: "tok")
    monkeypatch.setattr(daemon, "_drain_iteration", fake_iteration)
    monkeypatch.setattr(daemon, "attempt_delivery", lambda state: calls.append("delivery"))
    rc = daemon.run_daemon(
        ["--once", "--auto-act", "--self", "x", "--gateway", "http://gw", "--state-dir", str(tmp_path)]
    )
    assert rc == 0
    assert calls == ["drain", "delivery"]


def test_route_records_drained_dm_without_auto_act(tmp_path):
    state = _state(tmp_path, auto_act=False)
    daemon._route_to_handler(state, _dm(1))
    assert [entry["id"] for entry in state.queue.pending()] == [1]


def test_legacy_auto_act_never_injects_or_deletes(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.queue.enqueue(_dm(1))
    monkeypatch.setattr(
        daemon.session_bridge,
        "inject",
        lambda pane, text: (_ for _ in ()).throw(AssertionError("must not inject")),
    )
    attempt_delivery(state)
    assert [entry["id"] for entry in state.queue.pending()] == [1]
    assert state.queue.deferred_ticks == 0


def test_legacy_auto_act_never_resolves_or_probes_a_human_pane(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.queue.enqueue(_dm(1))
    monkeypatch.setattr(
        daemon.session_bridge,
        "resolve_session_pane",
        lambda name: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    attempt_delivery(state)
    assert [entry["id"] for entry in state.queue.pending()] == [1]


def test_render_block_still_bounds_content_for_a_future_executor():
    block = _render_delivery_block(
        [{"from": "peer", "kind": "question", "content": "x" * 5000}]
    )
    assert "mesh delivery" in block
    assert len(block) < 5000
