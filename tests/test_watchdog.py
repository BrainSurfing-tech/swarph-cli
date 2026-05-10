"""Tests for ``swarph watchdog`` (v0.7 stranded-session detection + recovery).

Per drop-mother #1021 + beta #1019 design. Covers detection signals
(cursor mtime PRIMARY + pgrep FALLBACK + AND-gate), threshold logic,
A1/A2 escalation paths, and dry-run --no-respawn safety mode.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from swarph_cli.commands.watchdog import (
    _DEFAULT_THRESHOLD_SEC,
    _resolve_cursor_path,
    _resolve_log_path,
    run_watchdog,
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch) -> Iterator[Path]:
    """Pin TMPDIR + XDG_STATE_HOME under tmp_path; clear MESH_GATEWAY_TOKEN."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    yield tmp_path


@pytest.fixture
def fresh_cursor(isolated_state):
    """Make a cursor file with current mtime (session is healthy)."""
    cursor = isolated_state / "lab-cursor.json"
    cursor.write_text('{"last_msg_id": 100}')
    return cursor


@pytest.fixture
def stale_cursor(isolated_state):
    """Make a cursor file with mtime 1hr in the past (session is stranded)."""
    cursor = isolated_state / "lab-cursor.json"
    cursor.write_text('{"last_msg_id": 100}')
    one_hour_ago = time.time() - 3600
    import os as _os
    _os.utime(cursor, (one_hour_ago, one_hour_ago))
    return cursor


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_resolve_cursor_path_explicit_wins(isolated_state):
    explicit = isolated_state / "explicit.json"
    assert _resolve_cursor_path("lab", str(explicit)) == explicit


def test_resolve_cursor_path_role_in_tmpdir(isolated_state):
    cursor = isolated_state / "lab-cursor.json"
    cursor.write_text("{}")
    assert _resolve_cursor_path("lab", None) == cursor


def test_resolve_cursor_path_falls_back_to_canonical_lab_path(isolated_state):
    # No <role>-cursor.json in TMPDIR → fallback to /tmp/lab-claude-cursor.json
    assert _resolve_cursor_path("lab", None) == Path("/tmp/lab-claude-cursor.json")


def test_resolve_log_path_explicit_wins(isolated_state):
    explicit = isolated_state / "custom.log"
    assert _resolve_log_path(str(explicit)) == explicit


def test_resolve_log_path_uses_xdg_state_home(isolated_state):
    expected = isolated_state / "state" / "swarph" / "watchdog.log"
    assert _resolve_log_path(None) == expected


# ---------------------------------------------------------------------------
# run_watchdog — argparse + dispatch
# ---------------------------------------------------------------------------


def test_run_watchdog_no_args_prints_usage(isolated_state, capsys):
    rc = run_watchdog(argv=[])
    captured = capsys.readouterr()
    assert rc == 0
    assert "watchdog" in captured.err.lower()


def test_run_watchdog_without_check_returns_4(isolated_state, capsys):
    rc = run_watchdog(argv=["--cell", "lab"])
    assert rc == 4


def test_run_watchdog_unreadable_cursor_returns_3(isolated_state, capsys):
    """Cursor doesn't exist + fallback /tmp/lab-claude-cursor.json also doesn't
    exist on test host → exit 3 detection error."""
    with patch("swarph_cli.commands.watchdog._resolve_cursor_path") as mock:
        mock.return_value = isolated_state / "nonexistent.json"
        rc = run_watchdog(argv=["--check", "--cell", "lab"])
        assert rc == 3


# ---------------------------------------------------------------------------
# Decision matrix — fresh cursor → noop
# ---------------------------------------------------------------------------


def test_fresh_cursor_returns_noop(isolated_state, fresh_cursor):
    rc = run_watchdog(argv=[
        "--check", "--cell", "lab",
        "--cursor", str(fresh_cursor),
    ])
    assert rc == 0  # healthy session


# ---------------------------------------------------------------------------
# Decision matrix — stale cursor + alive process + unread DMs → A1
# ---------------------------------------------------------------------------


def test_stale_cursor_alive_process_unread_dms_fires_a1(
    isolated_state, stale_cursor, monkeypatch, capsys
):
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",  # well below stale's 1hr
        ])
    assert rc == 1
    send_mock.assert_called_once()
    # Wake-prompt text contains diagnostic info
    args = send_mock.call_args[0]
    assert "watchdog wake" in args[1]
    assert "cursor stale" in args[1]


# ---------------------------------------------------------------------------
# Decision matrix — stale cursor + alive process + zero unread → noop
# ---------------------------------------------------------------------------


def test_stale_cursor_alive_no_unread_returns_noop(
    isolated_state, stale_cursor, monkeypatch
):
    """Cursor stale but inbox empty → no point waking; legitimate-pause case."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=0), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True):
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 0


# ---------------------------------------------------------------------------
# Decision matrix — stale cursor + dead process → A2 respawn
# ---------------------------------------------------------------------------


def test_stale_cursor_dead_process_fires_a2(
    isolated_state, stale_cursor, monkeypatch
):
    """Process dead → A2 respawn regardless of unread count."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=False), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=0), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=False), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 2
    spawn_mock.assert_called_once_with("lab", "lab")


def test_no_respawn_flag_skips_a2(
    isolated_state, stale_cursor, monkeypatch
):
    """--no-respawn dry-run mode logs but doesn't actually invoke spawn."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=False), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=5), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph") as spawn_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
            "--no-respawn",
        ])
    assert rc == 2  # signals A2 would have fired
    spawn_mock.assert_not_called()  # but didn't actually


# ---------------------------------------------------------------------------
# Decision matrix — stale cursor + alive process + unread + tmux missing → A2
# ---------------------------------------------------------------------------


def test_stale_cursor_alive_tmux_missing_fires_a2(
    isolated_state, stale_cursor, monkeypatch
):
    """Process alive somewhere but tmux session gone — partial state. A2."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=2), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=False), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 2
    spawn_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Detection error — gateway unreachable → assume unread (still try A1)
# ---------------------------------------------------------------------------


def test_gateway_unreachable_does_not_block_a1(
    isolated_state, stale_cursor, monkeypatch, capsys
):
    """If gateway is down, return None for unread — watchdog still tries A1
    (assume unread; gateway-down ≠ session-dead)."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    # unread=None passes the `if unread == 0` short-circuit and goes to A1
    assert rc == 1
    send_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Logging behaviour
# ---------------------------------------------------------------------------


def test_watchdog_writes_log_entry_per_check(
    isolated_state, fresh_cursor, monkeypatch
):
    log_path = isolated_state / "wd.log"
    rc = run_watchdog(argv=[
        "--check", "--cell", "lab",
        "--cursor", str(fresh_cursor),
        "--log", str(log_path),
    ])
    assert rc == 0
    assert log_path.exists()
    line = log_path.read_text().strip()
    parsed = json.loads(line)
    assert parsed["event"] == "noop"
    assert parsed["details"]["decision"] == "healthy_cursor_fresh"


def test_watchdog_log_appends_across_invocations(isolated_state, monkeypatch):
    """Use two distinct cursor paths to avoid the fresh/stale fixture
    collision (both fixtures write to the same path; stale wins last
    write, breaking 'fresh-then-stale' sequencing)."""
    import os as _os
    log_path = isolated_state / "wd.log"
    fresh = isolated_state / "fresh-cursor.json"
    fresh.write_text("{}")
    stale = isolated_state / "stale-cursor.json"
    stale.write_text("{}")
    one_hour_ago = time.time() - 3600
    _os.utime(stale, (one_hour_ago, one_hour_ago))

    run_watchdog(argv=[
        "--check", "--cell", "lab", "--cursor", str(fresh),
        "--log", str(log_path),
    ])
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=0), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True):
        run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale),
            "--threshold", "60", "--log", str(log_path),
        ])
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2  # one entry per invocation
    parsed_first = json.loads(lines[0])
    parsed_second = json.loads(lines[1])
    assert parsed_first["details"]["decision"] == "healthy_cursor_fresh"
    assert parsed_second["details"]["decision"] == "noop_no_unread"
