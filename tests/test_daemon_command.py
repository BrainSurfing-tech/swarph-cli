"""Tests for ``swarph daemon`` — mocks HTTP + filesystem.

The daemon's structural value is replacing the orphaned-tail-F class
with one foreground process. Tests cover: cursor read/write atomicity,
backoff selection, single iteration end-to-end (via --once), gateway
failure modes (5xx, network unreachable), signal-driven shutdown,
verb dispatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from swarph_cli.commands import daemon as daemon_cmd
from swarph_cli.commands.daemon import (
    DaemonState,
    _BACKOFF_5XX_SECONDS,
    _BACKOFF_5XX_THRESHOLD_SECONDS,
    _BACKOFF_EMPTY_SECONDS,
    _BACKOFF_EMPTY_THRESHOLD,
    _drain_iteration,
    _read_cursor,
    _select_next_poll_seconds,
    _write_cursor_atomic,
)


# ---------------------------------------------------------------------------
# Cursor — read/write/atomicity
# ---------------------------------------------------------------------------


def test_read_cursor_returns_default_on_missing(tmp_path):
    cursor = _read_cursor(tmp_path / "no-such.json")
    assert cursor == {"last_msg_id": 0, "tasks_snapshot": {}}


def test_read_cursor_loads_existing(tmp_path):
    p = tmp_path / "cursor.json"
    p.write_text(json.dumps({"last_msg_id": 42, "tasks_snapshot": {"a": 1}}))
    cursor = _read_cursor(p)
    assert cursor["last_msg_id"] == 42
    assert cursor["tasks_snapshot"] == {"a": 1}


def test_read_cursor_raises_on_corrupted_file(tmp_path, capsys):
    p = tmp_path / "cursor.json"
    p.write_text("not json {{{")
    with pytest.raises(json.JSONDecodeError):
        _read_cursor(p)
    err = capsys.readouterr().err
    assert "CORRUPTED" in err
    assert "Refusing to overwrite" in err


def test_write_cursor_atomic_uses_rename(tmp_path):
    p = tmp_path / "cursor.json"
    _write_cursor_atomic(p, {"last_msg_id": 7})
    assert p.exists()
    assert json.loads(p.read_text())["last_msg_id"] == 7
    # No leftover .tmp files
    tmps = list(tmp_path.glob("cursor.json.tmp.*"))
    assert tmps == []


def test_write_cursor_atomic_creates_parent_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "cursor.json"
    _write_cursor_atomic(nested, {"last_msg_id": 1})
    assert nested.exists()


def test_write_cursor_atomic_overwrites_cleanly(tmp_path):
    p = tmp_path / "cursor.json"
    _write_cursor_atomic(p, {"last_msg_id": 1})
    _write_cursor_atomic(p, {"last_msg_id": 99})
    assert json.loads(p.read_text())["last_msg_id"] == 99


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def _state(tmp_path) -> DaemonState:
    return DaemonState(
        self_name="lab-test",
        state_dir=tmp_path,
        gateway="http://x:8788",
        token="tok",
        poll_s=30,
        auto_act=False,
    )


def test_backoff_returns_base_when_healthy(tmp_path):
    s = _state(tmp_path)
    assert _select_next_poll_seconds(s) == 30


def test_backoff_kicks_in_after_consecutive_empty(tmp_path):
    s = _state(tmp_path)
    s.consecutive_empty = _BACKOFF_EMPTY_THRESHOLD
    assert _select_next_poll_seconds(s) == _BACKOFF_EMPTY_SECONDS


def test_backoff_5xx_kicks_in_after_5min_disconnect(tmp_path):
    s = _state(tmp_path)
    s.disconnect_since = time.time() - (_BACKOFF_5XX_THRESHOLD_SECONDS + 1)
    assert _select_next_poll_seconds(s) == _BACKOFF_5XX_SECONDS


def test_backoff_5xx_short_outage_uses_base(tmp_path):
    s = _state(tmp_path)
    s.disconnect_since = time.time() - 10  # 10s out
    assert _select_next_poll_seconds(s) == 30


# ---------------------------------------------------------------------------
# _drain_iteration — happy path + failure modes
# ---------------------------------------------------------------------------


def _http_factory(scripted: list):
    """Returns (fake_http_get, captured_calls). scripted is a list of
    (status, body) pairs returned in order."""
    captured = []
    it = iter(scripted)

    def fake(url, *, token, timeout=10.0):
        captured.append({"url": url, "token": token})
        return next(it)

    return fake, captured


def test_drain_iteration_empty_inbox_advances_empty_counter(tmp_path, monkeypatch):
    s = _state(tmp_path)
    fake, _ = _http_factory([(200, {"messages": []})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.consecutive_empty == 1
    assert s.dms_seen == 0


def test_drain_iteration_processes_messages_and_advances_cursor(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.cursor["last_msg_id"] = 5
    fake, _ = _http_factory([
        (200, {"messages": [
            {"id": 6, "from_node": "drop", "kind": "fyi",
             "content": "hello", "created_at": "2026-05-08T20:00:00Z"},
            {"id": 7, "from_node": "drop", "kind": "fyi",
             "content": "second", "created_at": "2026-05-08T20:01:00Z"},
        ]}),
    ])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.dms_seen == 2
    assert s.cursor["last_msg_id"] == 7
    assert s.consecutive_empty == 0
    # Cursor flushed to disk
    on_disk = json.loads((tmp_path / "cursor.json").read_text())
    assert on_disk["last_msg_id"] == 7
    # inbox.log has both DMs as JSONL
    log_lines = (tmp_path / "inbox.log").read_text().strip().splitlines()
    assert len(log_lines) == 2
    assert json.loads(log_lines[0])["id"] == 6


def test_drain_iteration_filters_outbound_self_messages(tmp_path, monkeypatch):
    """Defense-in-depth filter: even if gateway returns messages where
    from_node==self_name (latent ?to_node= vs ?to= bug, fixed mid-session),
    the daemon must not log/process them as inbound. Regression-tested."""
    s = _state(tmp_path)
    fake, _ = _http_factory([
        (200, {"messages": [
            {"id": 1, "from_node": "drop", "to_node": "lab-test", "kind": "fyi",
             "content": "real inbound", "created_at": "z"},
            {"id": 2, "from_node": "lab-test", "to_node": "drop", "kind": "fyi",
             "content": "MY outbound — should be skipped", "created_at": "z"},
            {"id": 3, "from_node": "drop", "to_node": "lab-test", "kind": "fyi",
             "content": "more inbound", "created_at": "z"},
        ]}),
    ])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.dms_seen == 2  # only id=1 and id=3
    log_lines = (tmp_path / "inbox.log").read_text().strip().splitlines()
    assert len(log_lines) == 2
    contents = [json.loads(l)["content"] for l in log_lines]
    assert "MY outbound — should be skipped" not in contents


def test_drain_iteration_filters_already_seen_messages(tmp_path, monkeypatch):
    """Gateway returns recent N messages including some <= last_id; daemon
    must filter so it doesn't re-process."""
    s = _state(tmp_path)
    s.cursor["last_msg_id"] = 10
    fake, _ = _http_factory([
        (200, {"messages": [
            {"id": 8, "from_node": "x", "kind": "fyi",
             "content": "old", "created_at": "z"},
            {"id": 9, "from_node": "x", "kind": "fyi",
             "content": "old2", "created_at": "z"},
            {"id": 11, "from_node": "x", "kind": "fyi",
             "content": "new", "created_at": "z"},
        ]}),
    ])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.dms_seen == 1  # only id=11
    assert s.cursor["last_msg_id"] == 11


def test_drain_iteration_processes_in_id_order(tmp_path, monkeypatch):
    """Gateway may return messages in any order; daemon must process
    oldest-first so cursor advances monotonically."""
    s = _state(tmp_path)
    fake, _ = _http_factory([
        (200, {"messages": [
            {"id": 3, "from_node": "x", "kind": "fyi",
             "content": "third", "created_at": "z"},
            {"id": 1, "from_node": "x", "kind": "fyi",
             "content": "first", "created_at": "z"},
            {"id": 2, "from_node": "x", "kind": "fyi",
             "content": "second", "created_at": "z"},
        ]}),
    ])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    log_ids = [
        json.loads(line)["id"]
        for line in (tmp_path / "inbox.log").read_text().strip().splitlines()
    ]
    assert log_ids == [1, 2, 3]
    assert s.cursor["last_msg_id"] == 3


def test_drain_iteration_5xx_records_disconnect(tmp_path, monkeypatch, capsys):
    s = _state(tmp_path)
    fake, _ = _http_factory([(503, {"detail": "service unavailable"})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.disconnect_since is not None
    err = capsys.readouterr().err
    assert "503" in err
    assert "service unavailable" in err


def test_drain_iteration_4xx_logs_loud(tmp_path, monkeypatch, capsys):
    s = _state(tmp_path)
    fake, _ = _http_factory([(401, {"detail": "bad token"})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    err = capsys.readouterr().err
    assert "401" in err
    # 4xx is not a "disconnect" — caller probably has wrong creds
    assert s.disconnect_since is None


def test_drain_iteration_network_unreachable_records_disconnect(
    tmp_path, monkeypatch
):
    s = _state(tmp_path)
    fake, _ = _http_factory([(0, {"detail": "name resolution failure"})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.disconnect_since is not None


def test_drain_iteration_clears_disconnect_on_recovery(tmp_path, monkeypatch):
    s = _state(tmp_path)
    s.disconnect_since = time.time()
    fake, _ = _http_factory([(200, {"messages": []})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    asyncio.run(_drain_iteration(s))
    assert s.disconnect_since is None


# ---------------------------------------------------------------------------
# run_daemon — verb entry point with --once test mode
# ---------------------------------------------------------------------------


def test_run_daemon_once_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake, _ = _http_factory([(200, {"messages": []})])
    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    rc = daemon_cmd.run_daemon(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--self",
            "test-peer",
            "--once",
        ]
    )
    assert rc == 0


def test_run_daemon_resolves_self_from_state_dir_basename(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    captured = []

    def fake(url, *, token, timeout=10.0):
        captured.append(url)
        return 200, {"messages": []}

    monkeypatch.setattr(daemon_cmd, "_http_get", fake)
    state_dir = tmp_path / "swarph_state" / "auto-named-peer"
    rc = daemon_cmd.run_daemon(
        ["--state-dir", str(state_dir), "--once"]
    )
    assert rc == 0
    # URL should embed auto-named-peer (the basename of state-dir).
    # Gateway accepts ?to=, NOT ?to_node= — the latter is silently
    # ignored. Bug regression-test for the session-long latent issue.
    assert "to=auto-named-peer" in captured[0]
    assert "to_node=" not in captured[0]


def test_run_daemon_no_identity_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    rc = daemon_cmd.run_daemon(["--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot resolve identity" in err


# ---------------------------------------------------------------------------
# Verb dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_daemon_verb(monkeypatch):
    from swarph_cli import main as main_mod

    captured = {}

    def fake_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("swarph_cli.commands.daemon.run_daemon", fake_run)
    rc = main_mod.main(["daemon", "--state-dir", "/tmp/x", "--once"])
    assert rc == 0
    assert captured["argv"] == ["--state-dir", "/tmp/x", "--once"]
