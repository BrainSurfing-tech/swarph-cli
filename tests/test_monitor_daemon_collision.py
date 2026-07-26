"""`swarph monitor` must never share a state dir with a live `swarph daemon`.

Found by peer cell `droplet` reviewing PR #139 — and it was LIVE on his box:
his swarph-inbox-daemon runs with --state-dir /var/lib/swarph/droplet, which
contains cursor.json + inbox.log. `swarph monitor` uses the IDENTICAL layout.

So `swarph monitor start --state-dir /var/lib/swarph/droplet` — the obvious
thing for an operator to type, because that is where the state already is —
puts TWO processes on one cursor, each advancing it on its own poll.
Interleaved writes give lost DMs or repeats, and both are SILENT.

_MONITOR_PIDFILE guarded monitor-against-monitor. Nothing guarded
monitor-against-daemon.

droplet's call, and the reason it is a refusal rather than a silent fallback to
a private dir: sharing the directory is legitimate once one of the two is
stopped, so a hard error TEACHES the constraint instead of hiding it.

Run: venv/bin/python -m pytest tests/test_monitor_daemon_collision.py -v
"""
import json
import os

from swarph_cli.commands import monitor


def test_refuses_when_daemon_pidfile_is_live(tmp_path):
    """The reliable detector: a daemon that announced itself."""
    (tmp_path / "daemon.pid").write_text(json.dumps({"pid": os.getppid()}), encoding="utf-8")
    reason, detail = monitor._daemon_owns_state_dir(tmp_path)
    assert reason == "live daemon pidfile", reason
    assert "daemon.pid" in detail


def test_refuses_on_daemon_owned_cursor_without_a_pidfile(tmp_path):
    """THE CASE THAT MATTERS: droplet's daemon has been running since before
    the pidfile existed, so only the cursor schema betrays it."""
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 8387, "tasks_snapshot": {}}), encoding="utf-8")
    reason, detail = monitor._daemon_owns_state_dir(tmp_path)
    assert reason == "daemon-owned cursor", reason
    assert "tasks_snapshot" in detail


def test_allows_a_monitor_owned_state_dir(tmp_path):
    """A monitor's own cursor has no tasks_snapshot — must NOT false-positive."""
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 42, "pending_wake": False}), encoding="utf-8")
    assert monitor._daemon_owns_state_dir(tmp_path) == (None, None)


def test_allows_an_empty_state_dir(tmp_path):
    assert monitor._daemon_owns_state_dir(tmp_path) == (None, None)


def test_dead_daemon_pidfile_does_not_block(tmp_path):
    """A stale pidfile must not wedge the monitor out of a dir nobody owns."""
    (tmp_path / "daemon.pid").write_text(json.dumps({"pid": 2 ** 22}), encoding="utf-8")
    assert monitor._daemon_owns_state_dir(tmp_path) == (None, None)


def test_corrupt_pidfile_and_cursor_do_not_crash(tmp_path):
    """Fail OPEN on unreadable markers: a garbage file must not be a DoS on start."""
    (tmp_path / "daemon.pid").write_text("{not json", encoding="utf-8")
    (tmp_path / "cursor.json").write_text("{also not json", encoding="utf-8")
    assert monitor._daemon_owns_state_dir(tmp_path) == (None, None)


def test_start_refuses_with_exit_2_and_names_both_files(tmp_path, monkeypatch, capsys):
    """End-to-end: the operator gets a loud, actionable refusal."""
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 8387, "tasks_snapshot": {}}), encoding="utf-8")
    rc = monitor.run_monitor(["start", "--state-dir", str(tmp_path), "--as", "droplet"])
    assert rc == 2, "must refuse, not race"
    err = capsys.readouterr().err
    assert "REFUSING" in err
    assert "swarph daemon" in err, "name the other writer"
    assert "cursor.json" in err, "name the contended file"
    assert "--state-dir" in err, "tell them how to fix it"
