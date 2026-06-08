"""T4 — per-peer no-spam cooldown for ``--dm-wake``.

A peer that stays stale across many watchdog ticks should be DM-woken ONCE
per cooldown window, not on every tick. The cooldown is keyed per-peer in a
small JSON state file ``{peer_name: last_wake_epoch}`` whose path mirrors the
watchdog's XDG_STATE_HOME/swarph state convention (same dir as the log +
A1 marker).

Contract:
  * ``_load_dm_wake_state`` never raises — missing/corrupt → ``{}``;
  * round-trips a dict through ``_save_dm_wake_state`` → ``_load_dm_wake_state``;
  * inside the scan loop: a stale peer DM'd at t0 is SKIPPED while
    ``now - last_wake < cooldown_sec`` (no DM, doesn't count as a wake →
    not exit 3); past the window it re-wakes;
  * a FAILED DM does not stamp the cooldown → the peer is retried next tick.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from swarph_cli.commands.watchdog import (
    _DM_WAKE_PROMPT,
    _dm_wake_scan,
    _load_dm_wake_state,
    _resolve_dm_wake_state_path,
    _save_dm_wake_state,
    _build_parser,
)


# ---------------------------------------------------------------------------
# state-file load/save primitives
# ---------------------------------------------------------------------------


def test_load_missing_path_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert _load_dm_wake_state(missing) == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("}{ not json at all !!!")
    assert _load_dm_wake_state(corrupt) == {}


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = {"droplet": 1717000000, "gpu-wsl": 1717000123}
    _save_dm_wake_state(path, state)
    assert _load_dm_wake_state(path) == state


def test_save_is_best_effort_swallows_errors(tmp_path):
    # Parent dir does not exist and cannot be created (path under a file).
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    bad_path = not_a_dir / "nested" / "state.json"
    # Must NOT raise.
    _save_dm_wake_state(bad_path, {"droplet": 1})


def test_state_path_mirrors_log_dir_convention(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = _resolve_dm_wake_state_path(None)
    assert path == tmp_path / "state" / "swarph" / "dm_wake_state.json"


# ---------------------------------------------------------------------------
# scan-loop cooldown gating
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(tmp_path, monkeypatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    yield tmp_path


def _args(state_path: Path, **over):
    p = _build_parser()
    ns = p.parse_args(["--check", "--cell", "lab", "--dm-wake", "--threshold", "60"])
    ns.dm_wake_state_path = str(state_path)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _run_scan(args, log_path, now_epoch):
    return _dm_wake_scan(args, log_path, now_epoch=now_epoch)


def test_cooldown_suppresses_second_tick(isolated_state):
    """Same stale peer, tick-2 within cooldown → DM fired exactly once."""
    state_path = isolated_state / "dmstate.json"
    log_path = isolated_state / "wd.log"
    args = _args(state_path, dm_wake_cooldown_sec=1800)

    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        # stale peer returned both ticks (still stranded)
        return_value=[{"name": "droplet", "last_health": "2020-01-01T00:00:00+00:00"}],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        fired_1 = _run_scan(args, log_path, now_epoch=1_700_000_000)
        fired_2 = _run_scan(args, log_path, now_epoch=1_700_000_000 + 600)  # 10min later < 1800

    assert dm_mock.call_count == 1
    assert fired_1 == 1
    assert fired_2 == 0  # tick-2 suppressed by cooldown → not a wake (no exit 3)


def test_past_cooldown_rewakes(isolated_state):
    state_path = isolated_state / "dmstate.json"
    log_path = isolated_state / "wd.log"
    args = _args(state_path, dm_wake_cooldown_sec=1800)

    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[{"name": "droplet", "last_health": "2020-01-01T00:00:00+00:00"}],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ) as dm_mock:
        _run_scan(args, log_path, now_epoch=1_700_000_000)
        fired_2 = _run_scan(args, log_path, now_epoch=1_700_000_000 + 1801)  # past cooldown

    assert dm_mock.call_count == 2
    assert fired_2 == 1


def test_failed_dm_does_not_start_cooldown(isolated_state):
    """A DM that returns False must not stamp the cooldown → retried next tick."""
    state_path = isolated_state / "dmstate.json"
    log_path = isolated_state / "wd.log"
    args = _args(state_path, dm_wake_cooldown_sec=1800)

    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[{"name": "droplet", "last_health": "2020-01-01T00:00:00+00:00"}],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=False
    ) as dm_mock:
        _run_scan(args, log_path, now_epoch=1_700_000_000)
        # tick-2 well within would-be cooldown; since tick-1 FAILED, state
        # was never stamped → this tick tries again.
        _run_scan(args, log_path, now_epoch=1_700_000_000 + 10)

    assert dm_mock.call_count == 2  # retried because cooldown never started


def test_cooldown_flag_default_is_1800():
    p = _build_parser()
    ns = p.parse_args(["--check"])
    assert ns.dm_wake_cooldown_sec == 1800


def test_successful_wake_stamps_state(isolated_state):
    """After a successful wake, the peer's epoch is recorded in the state file."""
    state_path = isolated_state / "dmstate.json"
    log_path = isolated_state / "wd.log"
    args = _args(state_path, dm_wake_cooldown_sec=1800)

    with patch(
        "swarph_cli.commands.watchdog._fetch_peers",
        return_value=[{"name": "droplet", "last_health": "2020-01-01T00:00:00+00:00"}],
    ), patch(
        "swarph_cli.commands.watchdog._dm_wake", return_value=True
    ):
        _run_scan(args, log_path, now_epoch=1_700_000_000)

    assert _load_dm_wake_state(state_path) == {"droplet": 1_700_000_000}
