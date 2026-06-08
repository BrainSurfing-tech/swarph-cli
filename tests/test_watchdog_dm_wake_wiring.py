"""T3 — ``--dm-wake`` wiring into the watchdog decision matrix.

The ``--dm-wake`` flag turns the watchdog into the mesh's cross-host wake
source: in ADDITION to the local own-session A1/A2 decision, it scans the
gateway peer list for peers whose ``last_health`` is staler than the session
staleness threshold and sends each a wake DM (``_dm_wake`` → POST /messages).

Exit-precedence contract (T3):
  * local own-session action wins — if the local decision returns a real
    action / error code (1/2/3/4), that is returned unchanged;
  * exit 3 (A1-DM) is surfaced ONLY when the local decision was a no-op
    (rc == 0) AND at least one cross-host wake DM fired;
  * with ``--dm-wake`` OFF the scan is a pure no-op → 0/1/2 preserved.

Self-exclusion: the watchdog never DM-wakes its own peer name (covered by the
local A1/A2 path).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from swarph_cli.commands.watchdog import (
    _DM_WAKE_PROMPT,
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
    """Cursor file with current mtime → local session is healthy (local noop)."""
    cursor = isolated_state / "lab-cursor.json"
    cursor.write_text('{"last_msg_id": 100}')
    return cursor


def _peer(name: str, age_sec: float) -> dict:
    """A peers-list entry with last_health ``age_sec`` seconds in the past."""
    ts = time.time() - age_sec
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return {"name": name, "last_health": iso}


# ---------------------------------------------------------------------------
# --dm-wake set + a stale cross-host peer present → wake fires, exit 3
# ---------------------------------------------------------------------------


def test_dm_wake_fires_for_stale_cross_host_peer_exit_3(
    isolated_state, fresh_cursor
):
    stale_peer = _peer("droplet", age_sec=7200)  # 2hr stale, NOT self
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[stale_peer],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),  # local session healthy → local noop
            "--threshold", "60",
            "--dm-wake",
        ])
    assert rc == 3
    dm_mock.assert_called_once()
    call_args = dm_mock.call_args[0]
    # _dm_wake(gateway, self_peer, target_peer, token, content)
    assert call_args[2] == "droplet"          # target_peer
    assert call_args[4] == _DM_WAKE_PROMPT     # content


# ---------------------------------------------------------------------------
# --dm-wake NOT set + same stale peer → today's behavior preserved (no DM)
# ---------------------------------------------------------------------------


def test_no_dm_wake_flag_preserves_todays_behavior(
    isolated_state, fresh_cursor
):
    stale_peer = _peer("droplet", age_sec=7200)
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[stale_peer],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),  # healthy cursor → local noop, exit 0
            "--threshold", "60",
        ])
    assert rc == 0          # today's value, unchanged
    dm_mock.assert_not_called()


# ---------------------------------------------------------------------------
# --dm-wake set but no stale peers → no DM, normal local exit (not 3)
# ---------------------------------------------------------------------------


def test_dm_wake_no_stale_peers_returns_local_value(
    isolated_state, fresh_cursor
):
    fresh_peer = _peer("droplet", age_sec=5)  # well within threshold → fresh
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[fresh_peer],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),
            "--threshold", "60",
            "--dm-wake",
        ])
    assert rc == 0          # local noop, no wake fired → NOT 3
    dm_mock.assert_not_called()


def test_dm_wake_empty_peers_returns_local_value(
    isolated_state, fresh_cursor
):
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers", return_value=[]
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),
            "--threshold", "60",
            "--dm-wake",
        ])
    assert rc == 0
    dm_mock.assert_not_called()


# ---------------------------------------------------------------------------
# self-exclusion — a stale peer whose name == self_peer is NEVER DM-woken
# ---------------------------------------------------------------------------


def test_dm_wake_excludes_self(isolated_state, fresh_cursor):
    self_stale = _peer("lab", age_sec=7200)  # name == self peer (cell role)
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[self_stale],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),
            "--threshold", "60",
            "--dm-wake",
        ])
    assert rc == 0          # only self was stale → no cross-host wake → local 0
    dm_mock.assert_not_called()


def test_dm_wake_excludes_self_but_wakes_other(isolated_state, fresh_cursor):
    """Self stale + another host stale → wake ONLY the other host, exit 3."""
    peers = [_peer("lab", age_sec=7200), _peer("droplet", age_sec=7200)]
    with patch(
        "swarph_cli.commands.watchdog._fetch_peers", return_value=peers
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(fresh_cursor),
            "--threshold", "60",
            "--dm-wake",
        ])
    assert rc == 3
    dm_mock.assert_called_once()
    assert dm_mock.call_args[0][2] == "droplet"  # never "lab"
