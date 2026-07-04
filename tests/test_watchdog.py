"""Tests for ``swarph watchdog`` (v0.7 stranded-session detection + recovery).

Per drop-mother #1021 + beta #1019 design. Covers detection signals
(cursor mtime PRIMARY + pgrep FALLBACK + AND-gate), threshold logic,
A1/A2 escalation paths, and dry-run --no-respawn safety mode.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

import pytest

_POSIX_WATCHDOG_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="watchdog targets systemd units + /proc — POSIX-only",
)

from swarph_cli.commands.watchdog import (
    _DEFAULT_THRESHOLD_SEC,
    _resolve_activity_marker_path,
    _resolve_cursor_path,
    _resolve_log_path,
    run_watchdog,
)


def _fake_run_factory(calls, pgrep_rc=1):
    """subprocess.run stub: records argv, answers the two calls _process_alive makes."""
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "list-panes"]:
            return SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
        if cmd and cmd[0] == "pgrep":
            # rc != 0 → _process_alive returns False before _pid_under; we only
            # assert the pgrep ARG here, not the liveness verdict.
            return SimpleNamespace(returncode=pgrep_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_run


def test_process_alive_default_greps_claude():
    from swarph_cli.commands import watchdog
    calls = []
    with patch("swarph_cli.commands.watchdog.subprocess.run", _fake_run_factory(calls)):
        watchdog._process_alive("some-session")
    pgrep_calls = [c for c in calls if c and c[0] == "pgrep"]
    assert pgrep_calls == [["pgrep", "-f", "claude"]]


def test_process_alive_honors_process_name():
    from swarph_cli.commands import watchdog
    calls = []
    with patch("swarph_cli.commands.watchdog.subprocess.run", _fake_run_factory(calls)):
        watchdog._process_alive("some-session", process_name="grok")
    pgrep_calls = [c for c in calls if c and c[0] == "pgrep"]
    assert pgrep_calls == [["pgrep", "-f", "grok"]]


def _panes_run(stdout):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_resolve_send_target_default_prefers_node_pane():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 bash\n%2 node\n")):
        assert watchdog._resolve_send_target("sess") == "%2"


def test_resolve_send_target_honors_process_name():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 bash\n%2 grok\n")):
        assert watchdog._resolve_send_target("sess", process_name="grok") == "%2"


def test_resolve_send_target_process_name_wins_over_fallback():
    from swarph_cli.commands import watchdog
    # both a node pane and a grok pane present; process_name='grok' must win.
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=_panes_run("%1 node\n%2 grok\n")):
        assert watchdog._resolve_send_target("sess", process_name="grok") == "%2"


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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
# F2 — fail-closed on unread=None
# ---------------------------------------------------------------------------


def test_gateway_unread_unknown_returns_noop(
    isolated_state, stale_cursor, monkeypatch, capsys
):
    """F2 fix (commander #1092 / droplet #1089) — if gateway returns None
    for unread count, fail CLOSED rather than firing A1.

    Old behavior fired A1 on None ("gateway down ≠ session dead"); production
    surfaced the case where gateway is fine but the count is still None
    (parser mismatch, transient error), and A1 spammed the tmux buffer
    13 times across 65min. New contract: 'respect peer-time when uncertain' —
    trade false-negative (occasional missed wake on real strands) for
    elimination of the false-positive spam class.
    """
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys") as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 0
    send_mock.assert_not_called()


# ---------------------------------------------------------------------------
# F1 — same-stale-window A1 suppression
# ---------------------------------------------------------------------------


def test_a1_fires_at_most_once_per_stale_window(
    isolated_state, stale_cursor, monkeypatch
):
    """F1 fix — repeated checks within the same stale window (cursor mtime
    unchanged) fire A1 only on the first invocation; subsequent checks
    noop with reason 'noop_a1_already_fired_this_window'.

    Production incident (commander #1092): cron at */5 fired A1 13 times
    across 65min into an actively-working session's tmux buffer because
    cursor only updates at turn-end, not mid-bash. After F1, watchdog
    fires AT MOST ONCE per stale window; re-arms on cursor advance.

    Pinned to ``--no-model-rung``: with the A1.5 rung enabled (default),
    same-window escalation goes A1 -> A1.5 -> A2 instead of noop — that
    ladder is covered in test_watchdog_model_rung.py. This test guards the
    F1 suppression semantics on the rung-disabled path.
    """
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        # First invocation — A1 fires
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60", "--no-model-rung",
            "--log", str(log_path),
        ])
        # Second invocation, no cursor change — A1 must NOT fire again
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60", "--no-model-rung",
            "--log", str(log_path),
        ])
        # Third invocation — still suppressed
        rc3 = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60", "--no-model-rung",
            "--log", str(log_path),
        ])
    assert rc1 == 1
    assert rc2 == 0
    assert rc3 == 0
    assert send_mock.call_count == 1  # NOT 3 — suppressed on rc2 and rc3
    # Second log entry should record the suppression reason explicitly
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    parsed_second = json.loads(lines[1])
    assert parsed_second["details"]["decision"] == "noop_a1_already_fired_this_window"


def test_a1_rearms_after_cursor_advance(
    isolated_state, stale_cursor, monkeypatch
):
    """F1 fix — after the suppressed window, if cursor advances (session
    recovered, even briefly), the marker no longer matches and subsequent
    stale windows fire A1 again. Ensures we don't permanently mute A1 on
    a peer that recovered then re-stranded."""
    import os as _os
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=2), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        # First A1 fires
        run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
        # Simulate cursor advancing 5min — but still stale (8min > 60s threshold)
        new_mtime = time.time() - 480
        _os.utime(stale_cursor, (new_mtime, new_mtime))
        # Second invocation — A1 must fire again (cursor advanced ⇒ new window)
        run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert send_mock.call_count == 2


# ---------------------------------------------------------------------------
# F3 — tmux pane_activity AND-gate (mother #1087)
# ---------------------------------------------------------------------------


def test_pane_activity_recent_suppresses_a1(
    isolated_state, stale_cursor, monkeypatch
):
    """F3 — cursor stale + alive + unread > 0 but pane_activity recent →
    suppress A1. Session is working in a long bash block; cursor only
    updates at turn-end. Same incident class as commander #1092 65-min
    spam, but caught upstream of F1 marker by checking pane_activity
    BEFORE firing."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=30), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys") as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
            "--pane-activity-threshold", "600",
        ])
    assert rc == 0
    send_mock.assert_not_called()


def test_pane_activity_old_falls_through_to_a1(
    isolated_state, stale_cursor, monkeypatch
):
    """F3 — pane_activity OLDER than threshold means session has actually
    been quiet; A1 still fires. Stop signal compatibility check."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=1200), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
            "--pane-activity-threshold", "600",
        ])
    assert rc == 1
    send_mock.assert_called_once()


def test_pane_activity_unavailable_falls_through_to_a1(
    isolated_state, stale_cursor, monkeypatch
):
    """F3 — detection error (tmux missing / older tmux without
    #{pane_activity}) returns None; A1 still fires. F3 is a strengthening
    of the gate, not a hard dependency."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 1
    send_mock.assert_called_once()


# ---------------------------------------------------------------------------
# F4 — cell.yaml-pinned cursor_path + tmux_session (mother #1057/#1060 + beta #1061/#1065)
# ---------------------------------------------------------------------------


def test_resolve_cursor_path_cell_yaml_pin_beats_default(isolated_state):
    """F4 — cell.yaml extra.cursor_path takes precedence over the
    /tmp/lab-claude-cursor.json fallback when no explicit --cursor."""
    from swarph_cli.commands.watchdog import _resolve_cursor_path
    pinned = isolated_state / "custom-cursor.json"
    assert _resolve_cursor_path("lab", None, str(pinned)) == pinned


def test_resolve_cursor_path_explicit_beats_cell_yaml_pin(isolated_state):
    """F4 — explicit --cursor still wins over cell.yaml pin."""
    from swarph_cli.commands.watchdog import _resolve_cursor_path
    explicit = isolated_state / "explicit-cursor.json"
    pinned = isolated_state / "pinned-cursor.json"
    assert _resolve_cursor_path("lab", str(explicit), str(pinned)) == explicit


def test_resolve_tmux_session_cell_yaml_pin_beats_role(isolated_state):
    """F4 — cell.yaml extra.tmux_session takes precedence over role
    default when no explicit --tmux-session."""
    from swarph_cli.commands.watchdog import _resolve_tmux_session
    assert _resolve_tmux_session("drop-mother", None, "drop-mother-tmux") == "drop-mother-tmux"


def test_resolve_tmux_session_explicit_beats_cell_yaml_pin(isolated_state):
    """F4 — explicit --tmux-session still wins over cell.yaml pin."""
    from swarph_cli.commands.watchdog import _resolve_tmux_session
    assert _resolve_tmux_session("lab", "explicit-name", "pinned-name") == "explicit-name"


def test_resolve_tmux_session_falls_back_to_role(isolated_state):
    """F4 — no explicit + no cell.yaml pin → role itself."""
    from swarph_cli.commands.watchdog import _resolve_tmux_session
    assert _resolve_tmux_session("lab", None, None) == "lab"


def test_a1_marker_path_keyed_on_role_and_tmux_session(isolated_state):
    """F4 — marker filename includes both role + tmux_session to prevent
    sibling-instance marker collisions (mother #1103 follow-up)."""
    from swarph_cli.commands.watchdog import _a1_marker_path
    log_path = isolated_state / "wd.log"
    m1 = _a1_marker_path(log_path, "drop-on-meta-edge", "drop-on-meta-edge")
    m2 = _a1_marker_path(log_path, "drop-on-meta-edge", "drop-on-meta-edge-2")
    assert m1 != m2
    assert m1.name == "a1-fired-drop-on-meta-edge-drop-on-meta-edge.marker"
    assert m2.name == "a1-fired-drop-on-meta-edge-drop-on-meta-edge-2.marker"


def test_a1_marker_path_sanitizes_tmux_session(isolated_state):
    """F4 — tmux_session sanitized to alphanumeric+underscore so
    cell.yaml-pinned values with weird chars don't break the filename."""
    from swarph_cli.commands.watchdog import _a1_marker_path
    log_path = isolated_state / "wd.log"
    m = _a1_marker_path(log_path, "lab", "weird/name with spaces!")
    assert ":" not in m.name and "/" not in m.name and " " not in m.name


def test_a2_escalation_clears_a1_marker(
    isolated_state, stale_cursor, monkeypatch
):
    """F1 fix — A2 respawn path clears the marker so the post-respawn
    session starts with a clean slate (otherwise a recovered+re-stranded
    session would inherit a stale marker matching the OLD cursor_mtime,
    which is theoretically possible since marker stores cursor_mtime not
    epoch-now). Defensive cleanup."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=5), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True):
        # First fire — record marker
        run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
            "--log", str(log_path),
        ])
    # F4 v0.7.2 marker keyed on (role, tmux_session) — tmux_session defaults
    # to role when no --tmux-session arg + no cell.yaml pin, so filename is
    # a1-fired-{role}-{role}.marker.
    marker = log_path.parent / "a1-fired-lab-lab.marker"
    assert marker.exists()

    # Now force A2 path (process dead) and confirm marker is gone
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=False), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=5), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=False), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True):
        run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
            "--log", str(log_path),
        ])
    assert not marker.exists()


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


# ---------------------------------------------------------------------------
# --install-service (v0.7.3 — closes ev_6954f748 substrate-component-install)
# ---------------------------------------------------------------------------


@_POSIX_WATCHDOG_SKIP
def test_install_service_dry_run_writes_no_files(isolated_state, capsys):
    """--dry-run prints what would be written without touching the filesystem."""
    rc = run_watchdog(argv=["--install-service", "--cell", "droplet", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    # Dry-run output goes to stderr
    assert "DRY RUN" in captured.err
    assert "cell=droplet" in captured.err
    # All three target files surface in the preview — PER-CELL names
    # (v0.10.1: fixed names clobbered the existing unit on multi-cell hosts)
    assert "/etc/systemd/system/swarph-watchdog-droplet.service" in captured.err
    assert "/etc/systemd/system/swarph-watchdog-droplet.timer" in captured.err
    assert "/etc/default/swarph-watchdog-droplet" in captured.err
    # SWARPH_CELL was templated to the requested role
    assert "SWARPH_CELL=droplet" in captured.err
    # ExecStart carries the cell explicitly (0.10.0 dropped it entirely)
    assert "watchdog --check --cell droplet" in captured.err
    # Timer requires the PER-CELL service, not the legacy fixed name
    assert "Requires=swarph-watchdog-droplet.service" in captured.err
    # EnvironmentFile is per-cell too
    assert "EnvironmentFile=-/etc/default/swarph-watchdog-droplet" in captured.err
    # The bundled service file's identifying line shows up
    assert "Swarph watchdog one-shot check" in captured.err


def test_install_service_two_cells_distinct_targets(isolated_state, capsys):
    """The clobber regression: two cells on one host must produce DISJOINT
    target paths — a second-cell install can never overwrite the first's
    unit/timer/default files (0.10.0 wrote fixed names and clobbered)."""
    run_watchdog(argv=["--install-service", "--cell", "lab", "--dry-run"])
    out_lab = capsys.readouterr().err
    run_watchdog(argv=["--install-service", "--cell", "science-claude", "--dry-run"])
    out_sci = capsys.readouterr().err

    def targets(err):
        return {
            line.split("would write ")[1].rstrip(":")
            for line in err.splitlines()
            if "would write " in line
        }

    lab_t, sci_t = targets(out_lab), targets(out_sci)
    assert len(lab_t) == 3 and len(sci_t) == 3
    assert lab_t.isdisjoint(sci_t)  # the whole point: no shared filenames
    # Template-drift guard (drop nit): each cell's ExecStart MUST carry its
    # own --cell — the 0.10.0 generated ExecStart dropped the flag entirely.
    assert "watchdog --check --cell lab" in out_lab
    assert "watchdog --check --cell science-claude" in out_sci


def test_install_service_dry_run_default_cell_is_lab(isolated_state, capsys):
    """Without --cell, the dry-run preview keeps SWARPH_CELL=lab default."""
    rc = run_watchdog(argv=["--install-service", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "SWARPH_CELL=lab" in captured.err


@_POSIX_WATCHDOG_SKIP
def test_install_service_without_sudo_returns_4(isolated_state, capsys, monkeypatch):
    """Non-root install (no --dry-run) refuses with helpful message + exit 4."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    rc = run_watchdog(argv=["--install-service", "--cell", "droplet"])
    assert rc == 4
    captured = capsys.readouterr()
    assert "requires root" in captured.err
    assert "--dry-run" in captured.err  # hint surfaces


def test_bundled_systemd_files_readable():
    """Package-data manifest correctness — importlib.resources can read all
    three bundled templates. Regression guard for pyproject package-data
    declaration."""
    from swarph_cli.commands.watchdog import _bundled_systemd_files

    files = _bundled_systemd_files()
    assert set(files.keys()) == {
        "swarph-watchdog.service",
        "swarph-watchdog.timer",
        "swarph-watchdog.default",
    }
    # Service file has the expected Type=oneshot shape
    assert "Type=oneshot" in files["swarph-watchdog.service"]
    assert "ExecStart=/usr/local/bin/swarph watchdog --check" in files["swarph-watchdog.service"]
    # Timer fires every 5 minutes
    assert "OnUnitActiveSec=5min" in files["swarph-watchdog.timer"]
    # Default file has the SWARPH_CELL=lab template line
    assert "SWARPH_CELL=lab" in files["swarph-watchdog.default"]


# ---------------------------------------------------------------------------
# v0.7.4 — _resolve_swarph_bin + ExecStart templating
# ---------------------------------------------------------------------------
#
# v0.7.3 shipped a hardcoded ExecStart=/usr/local/bin/swarph that broke
# pipx-installed peers (lab + drop both hit this on first install attempt
# 2026-05-14). v0.7.4 resolves the path at install time via _resolve_swarph_bin
# and substitutes into the bundled service template.


@_POSIX_WATCHDOG_SKIP
def test_resolve_swarph_bin_absolute_argv0_wins(monkeypatch):
    """If sys.argv[0] is absolute, it's the most reliable signal — use it."""
    from swarph_cli.commands.watchdog import _resolve_swarph_bin

    monkeypatch.setattr("sys.argv", ["/home/ubuntu/.local/bin/swarph", "watchdog"])
    assert _resolve_swarph_bin() == "/home/ubuntu/.local/bin/swarph"


def test_resolve_swarph_bin_bare_name_resolved_via_path(monkeypatch, tmp_path):
    """Bare-name argv[0] resolves via PATH."""
    from swarph_cli.commands.watchdog import _resolve_swarph_bin

    fake_bin = tmp_path / "swarph"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["swarph", "watchdog"])
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bin) if name == "swarph" else None)
    assert _resolve_swarph_bin() == str(fake_bin)


def test_resolve_swarph_bin_falls_back_to_placeholder_if_unresolvable(monkeypatch):
    """All resolution paths fail → fall back to /usr/local/bin/swarph
    (v0.7.3 hardcode behavior; no regression vs prior version)."""
    from swarph_cli.commands.watchdog import _resolve_swarph_bin, _SWARPH_BIN_PLACEHOLDER

    monkeypatch.setattr("sys.argv", ["swarph", "watchdog"])
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert _resolve_swarph_bin() == _SWARPH_BIN_PLACEHOLDER


@_POSIX_WATCHDOG_SKIP
def test_install_service_dry_run_substitutes_swarph_bin(isolated_state, capsys, monkeypatch):
    """Dry-run preview shows resolved swarph path in ExecStart, not hardcoded
    /usr/local/bin/swarph (when the host actually has swarph elsewhere)."""
    pipx_path = "/home/ubuntu/.local/bin/swarph"
    monkeypatch.setattr("sys.argv", [pipx_path, "watchdog"])
    rc = run_watchdog(argv=["--install-service", "--cell", "droplet", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    # Header surfaces the resolved binary
    assert f"swarph_bin={pipx_path}" in captured.err
    # Service file's ExecStart now points at pipx path, NOT the placeholder
    assert f"ExecStart={pipx_path} watchdog --check" in captured.err
    # And the v0.7.3-hardcoded path no longer appears in the preview
    assert "ExecStart=/usr/local/bin/swarph watchdog --check" not in captured.err


@_POSIX_WATCHDOG_SKIP
def test_install_service_dry_run_preserves_default_path_when_absolute(isolated_state, capsys, monkeypatch):
    """When sys.argv[0] IS /usr/local/bin/swarph (the v0.7.3 default path),
    the substitution still happens but produces the same line — no diff vs
    v0.7.3 for this case."""
    monkeypatch.setattr("sys.argv", ["/usr/local/bin/swarph", "watchdog"])
    rc = run_watchdog(argv=["--install-service", "--cell", "lab", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "ExecStart=/usr/local/bin/swarph watchdog --check" in captured.err


@_POSIX_WATCHDOG_SKIP
def test_resolve_swarph_bin_relative_with_slash_resolves_to_absolute(tmp_path, monkeypatch):
    """Relative path with slash (e.g. editable install's venv/bin/swarph)
    must be absolutized — systemd ExecStart needs absolute. Regression guard
    for the abspath fix on top of v0.7.4 path-autodetect."""
    from swarph_cli.commands.watchdog import _resolve_swarph_bin

    fake = tmp_path / "swarph"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    # Simulate `./swarph` from cwd=tmp_path
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["./swarph", "watchdog"])
    # shutil.which('./swarph') returns the relative path as-is when the
    # input contains a slash. _resolve_swarph_bin must abspath it.
    resolved = _resolve_swarph_bin()
    assert Path(resolved).is_absolute(), (
        f"resolver returned non-absolute path: {resolved!r}"
    )
    assert resolved == str(fake)


# ---------------------------------------------------------------------------
# _process_alive — session-scoped (adversarial-sweep MED, watchdog.py:361)
# ---------------------------------------------------------------------------

import os
import swarph_cli.commands.watchdog as _wd


class _R:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _fake_run_factory_legacy(panes_rc, panes_out, pgrep_rc, pgrep_out, sessions_rc=0):
    """Mock subprocess.run distinguishing tmux list-panes vs list-sessions."""
    def fake_run(cmd, **kw):
        if cmd[0] == "tmux" and cmd[1] == "list-panes":
            return _R(panes_rc, panes_out)
        if cmd[0] == "tmux" and cmd[1] == "list-sessions":
            return _R(sessions_rc, "" if sessions_rc else "lab: 1 windows\n")
        if cmd[0] == "pgrep":
            return _R(pgrep_rc, pgrep_out)
        return _R(1, "")
    return fake_run


@_POSIX_WATCHDOG_SKIP
def test_pid_under_walks_proc_ancestry():
    pid = os.getpid()
    assert _wd._pid_under(pid, {pid}) is True            # self
    assert _wd._pid_under(pid, {os.getppid()}) is True   # parent
    assert _wd._pid_under(pid, {999999999}) is False     # bogus ancestor


def test_process_alive_ignores_host_wide_claude(monkeypatch):
    """A claude process that is NOT a descendant of THIS session's panes must
    NOT count as alive (the old host-wide pgrep masked dead sessions)."""
    monkeypatch.setattr(_wd.subprocess, "run",
                        _fake_run_factory_legacy(0, "1000\n", 0, "2000\n"))
    monkeypatch.setattr(_wd, "_pid_under", lambda pid, anc, **k: False)
    assert _wd._process_alive("mysess") is False


def test_process_alive_true_when_claude_under_session(monkeypatch):
    monkeypatch.setattr(_wd.subprocess, "run",
                        _fake_run_factory_legacy(0, "1000\n", 0, "2000\n"))
    monkeypatch.setattr(_wd, "_pid_under", lambda pid, anc, **k: True)
    assert _wd._process_alive("mysess") is True


def test_process_alive_false_when_session_absent_but_server_up(monkeypatch):
    """Server reachable + this session genuinely gone → dead (A2 may fire)."""
    monkeypatch.setattr(_wd.subprocess, "run",
                        _fake_run_factory_legacy(1, "", 0, "2000\n", sessions_rc=0))
    assert _wd._process_alive("ghost") is False


def test_process_alive_assumes_alive_when_no_tmux_server(monkeypatch):
    """Regression (deploy 2026-06-02): the watchdog runs as ROOT, whose tmux
    socket is empty — list-panes AND list-sessions both fail. We CANNOT
    determine liveness via tmux, so must assume alive, NOT false-fire an A2
    respawn of a session that's actually alive under ubuntu's tmux."""
    monkeypatch.setattr(_wd.subprocess, "run",
                        _fake_run_factory_legacy(1, "", 0, "2000\n", sessions_rc=1))
    assert _wd._process_alive("lab") is True


def test_process_alive_false_when_no_claude_anywhere(monkeypatch):
    monkeypatch.setattr(_wd.subprocess, "run",
                        _fake_run_factory_legacy(0, "1000\n", 1, ""))
    assert _wd._process_alive("mysess") is False


# ---------------------------------------------------------------------------
# _pane_activity_age_sec — fallback across pane/window/session (F3 fix 2026-06-02)
# ---------------------------------------------------------------------------


def test_pane_activity_age_falls_back_when_pane_empty(monkeypatch):
    """pane_activity is empty without monitor-activity (tmux 3.x). F3 must fall
    back to window/session activity, NOT return None — returning None made F3 a
    no-op and let A1 fire against a genuinely-active session."""
    recent = _wd._now() - 30
    monkeypatch.setattr(_wd.subprocess, "run",
                        lambda cmd, **kw: _R(0, f"|{recent}|{recent - 5}\n"))
    age = _wd._pane_activity_age_sec("lab")
    assert age is not None
    assert 25 <= age <= 40  # ~30s, allowing for execution slack


def test_pane_activity_age_none_when_all_blank(monkeypatch):
    monkeypatch.setattr(_wd.subprocess, "run", lambda cmd, **kw: _R(0, "||\n"))
    assert _wd._pane_activity_age_sec("lab") is None


def test_pane_activity_age_takes_most_recent(monkeypatch):
    """Uses the MAX (most recent) epoch across the three vars."""
    now = _wd._now()
    # pane empty, window 500s ago, session 10s ago → most recent = 10s
    monkeypatch.setattr(_wd.subprocess, "run",
                        lambda cmd, **kw: _R(0, f"|{now - 500}|{now - 10}\n"))
    age = _wd._pane_activity_age_sec("lab")
    assert age is not None and age <= 20


# ---------------------------------------------------------------------------
# Liveness generalization — turn-activity marker as a second signal
# (feedback_watchdog_liveness_proxy): the drain-cursor goes stale during active
# non-draining work; the Stop-hook active.txt (touched every turn-end) rescues it.
# ---------------------------------------------------------------------------


def test_resolve_activity_marker_explicit_wins(isolated_state):
    p = _resolve_activity_marker_path("lab", "/x/y.txt", "/cell/z.txt")
    assert p == Path("/x/y.txt")  # Path compare — OS-agnostic (Windows str() uses '\\')


def test_resolve_activity_marker_cell_yaml_beats_default(isolated_state):
    p = _resolve_activity_marker_path("lab", None, "/cell/z.txt")
    assert p == Path("/cell/z.txt")  # Path compare — OS-agnostic (Windows str() uses '\\')


def test_resolve_activity_marker_default_is_role_active_in_tmpdir(isolated_state):
    p = _resolve_activity_marker_path("lab", None, None)
    assert p == isolated_state / "lab-claude-active.txt"


def test_stale_cursor_but_fresh_activity_marker_returns_noop(isolated_state, stale_cursor):
    # Drain-cursor is stale (1hr) BUT the Stop-hook turn-activity marker at the
    # DEFAULT path is fresh → freshest-of-both is fresh → healthy noop, NO false A1.
    marker = isolated_state / "lab-claude-active.txt"
    marker.write_text("")  # current mtime
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None):
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 0  # marker freshness rescues the stale cursor


def test_stale_cursor_and_stale_marker_still_fires_a1(isolated_state, stale_cursor):
    # Both stale → unchanged legacy behavior (A1). Absent/stale marker is harmless.
    import os as _os
    marker = isolated_state / "lab-claude-active.txt"
    marker.write_text("")
    one_hour_ago = time.time() - 3600
    _os.utime(marker, (one_hour_ago, one_hour_ago))
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True):
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 1  # both signals dark → A1 (unchanged)


# ---------------------------------------------------------------------------
# Task 2 — _liveness_via_cmd escape hatch + mutual exclusion
# ---------------------------------------------------------------------------


def test_liveness_via_cmd_rc0_is_alive():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        assert watchdog._liveness_via_cmd("true") is True


def test_liveness_via_cmd_nonzero_is_dead():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
        assert watchdog._liveness_via_cmd("false") is False


def test_liveness_via_cmd_timeout_assumes_alive():
    from swarph_cli.commands import watchdog
    import subprocess as _sp
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               side_effect=_sp.TimeoutExpired(cmd="x", timeout=5)):
        assert watchdog._liveness_via_cmd("sleep 99") is True


def test_liveness_via_cmd_oserror_assumes_alive():
    from swarph_cli.commands import watchdog
    with patch("swarph_cli.commands.watchdog.subprocess.run",
               side_effect=OSError("boom")):
        assert watchdog._liveness_via_cmd("bad") is True


def test_process_name_and_liveness_cmd_are_mutually_exclusive():
    from swarph_cli.commands import watchdog
    parser = watchdog._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--check", "--process-name", "grok",
                           "--liveness-cmd", "pgrep -f grok"])


def test_parser_defaults_process_name_claude_liveness_cmd_none():
    from swarph_cli.commands import watchdog
    ns = watchdog._build_parser().parse_args(["--check"])
    assert ns.process_name == "claude"
    assert ns.liveness_cmd is None
