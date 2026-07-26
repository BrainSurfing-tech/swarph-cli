"""The sidecar's read cursor must NOT share a failure mode with DELIVERY.

INCIDENT SHAPE, reported by peer cell `droplet` 2026-07-26: `last_msg_id` was
only ever written inside `if _tmux_wake(...)`. `_tmux_wake` returns False
whenever the tmux pane is gone (session restart, tmux server reaped, target
renamed) — so the cursor never advanced, every poll re-selected the same
messages, and the sidecar re-logged them forever with no backoff and no alarm.
The idle-guard branch had the same effect for a different reason.

This is the SAME shape as the 2026-07-10 grok-researcher incident one function
below the docstring that warns about it (see
tests/test_mesh_sidecar_wake_survives_read.py): there, read-marking was tied to
listing and one request was answered 67 times (~410k tokens).

THE RULE: advance the cursor when the messages are OBSERVED, not when delivery
succeeds. Waking is DELIVERY; the cursor is BOOKKEEPING.

Run: venv/bin/python -m pytest tests/test_mesh_sidecar_cursor_decoupled.py -v
"""
from swarph_cli.commands import mesh


def _state(tmp_path, wake_min_interval_s=0):
    return mesh.MeshSidecarState(
        self_name="lab-ovh",
        state_dir=tmp_path,
        gateway="http://gw:8788",
        token="tok",
        tmux_target="dead-session:0.0",
        poll_s=90,
        wake_min_interval_s=wake_min_interval_s,
    )


def _one_message(msg_id):
    def fake_get(url, token, **k):
        return (200, {"messages": [{
            "id": msg_id, "from_node": "droplet", "kind": "question",
            "content": "cursor check", "read_at": None,
        }]})
    return fake_get


def test_cursor_advances_when_the_wake_fails(monkeypatch, tmp_path):
    """A dead tmux pane must not freeze the cursor — the core regression."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(5001))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)  # pane is gone

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)

    assert state.cursor["last_msg_id"] == 5001, (
        "the message was OBSERVED; the cursor must advance even though delivery failed"
    )
    assert state.wakes_sent == 0, "a failed wake must not be counted as sent"
    assert state.consecutive_wake_failures == 1, "delivery failure must be visible"


def test_failed_wake_does_not_replay_the_same_message(monkeypatch, tmp_path):
    """The actual harm: infinite re-selection of the same DM, poll after poll."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(5001))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)

    logged = []
    monkeypatch.setattr(mesh, "_log_dm", lambda st, dm: logged.append(dm["id"]))

    state = _state(tmp_path)
    for _ in range(5):
        mesh._sidecar_iteration(state)

    assert logged == [5001], (
        f"id 5001 was processed {len(logged)}× across 5 polls — this is the "
        "grok-researcher replay loop (67 answers to one request)"
    )


def test_cursor_survives_across_processes_when_wake_fails(monkeypatch, tmp_path):
    """The cursor must be PERSISTED, not just held in memory, on the failure path."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(5001))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda target: False)

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)

    on_disk = mesh._read_cursor(state.cursor_path)
    assert on_disk["last_msg_id"] == 5001, (
        "a restarted sidecar must not rewind and re-deliver everything"
    )


def test_idle_guard_advances_the_cursor_too(monkeypatch, tmp_path):
    """Suppressing a wake is a DELIVERY decision; it must not rewind bookkeeping."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(6002))
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    state = _state(tmp_path, wake_min_interval_s=3600)
    state.cursor["last_wake_at"] = 10 ** 12  # far future => guard always suppresses

    mesh._sidecar_iteration(state)

    assert woke == [], "the idle guard suppressed the wake (precondition)"
    assert state.cursor["last_msg_id"] == 6002, (
        "an idle-suppressed wake must still advance the cursor"
    )
    assert state.cursor["pending_wake"] is True, "the undelivered wake must be recorded"


def test_successful_wake_still_advances_and_counts(monkeypatch, tmp_path):
    """Regression guard: the healthy path is unchanged."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(7003))
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)

    assert woke == ["dead-session:0.0"]
    assert state.cursor["last_msg_id"] == 7003
    assert state.wakes_sent == 1
    assert state.cursor["last_wake_at"] > 0, "a successful wake stamps the idle guard"
    assert state.cursor["pending_wake"] is False
    assert state.consecutive_wake_failures == 0


def test_wake_failure_counter_resets_on_recovery(monkeypatch, tmp_path):
    """A pane that comes back must clear the failure streak."""
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(8004))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)
    assert state.consecutive_wake_failures == 1

    # Same iteration shape, but the pane is back AND a newer message arrives.
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(8005))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: True)
    mesh._sidecar_iteration(state)

    assert state.consecutive_wake_failures == 0, "recovery clears the streak"
    assert state.cursor["last_msg_id"] == 8005


def test_pending_wake_lands_after_restart_with_no_new_mail(monkeypatch, tmp_path):
    """The new retry mechanism, end to end.

    Re-selection used to be the retry: the cursor stayed put so the same
    messages came back next poll. Now the cursor advances, so the owed wake must
    survive in `pending_wake` -- including across a process restart, and with an
    EMPTY poll (no new mail will ever arrive to carry it).
    """
    monkeypatch.setattr(mesh, "_http_get_json", _one_message(9006))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)  # pane gone

    state = _state(tmp_path)
    mesh._sidecar_iteration(state)
    assert state.cursor["pending_wake"] is True

    # Fresh state object == a restarted sidecar reading cursor.json from disk.
    revived = _state(tmp_path)
    assert revived.cursor["last_msg_id"] == 9006, "no rewind"
    assert revived.cursor["pending_wake"] is True, "the owed wake survived the restart"

    woke = []
    monkeypatch.setattr(mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": []}))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)
    mesh._sidecar_iteration(revived)

    assert woke == ["dead-session:0.0"], "an empty poll still delivers the owed wake"
    assert revived.cursor["pending_wake"] is False, "and clears it once delivered"
