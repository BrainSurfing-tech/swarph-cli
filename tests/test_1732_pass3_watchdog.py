"""Card #1732 pass 3: an open outage with a bad clock stays due, and a lock that cannot be taken does not drop peer alerts."""
from __future__ import annotations

import json
import sys

import pytest

from swarph_cli.scripts import wake_watchdog
from swarph_cli.scripts.wake_watchdog import GAP_SECONDS, scan
from test_1732_watchdog_idempotent import (
    MAX_CONSECUTIVE_LIST_FAILURES,
    NOW,
    _cell,
    _fake_swarph,
    _pass,
    _watch_env,
    _yaml,
)

_OPEN_ROW = {
    "id": 42,
    "card_id": 729,
    "holder": "commander",
    "status": "open",
    "created_by": "lab-ovh",
    "accept": "WAKE-DEAD lab-ovh",
    "taken_at": "2026-11-21T00:00:00Z",
}


def _notes(gw_path):
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    return gw, gw.get("notes") or []


def _arm_open_outage(tmp_path, monkeypatch, state):
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({"lab-ovh": state}), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [_OPEN_ROW],
        "say_rc": 0,
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    _watch_env(monkeypatch, tmp_path, root, state_path, stub)
    return state_path, gw_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_open_outage_with_non_numeric_alerted_at_realerts(tmp_path, monkeypatch, capsys):
    state_path, gw_path = _arm_open_outage(tmp_path, monkeypatch, {
        "outage": True,
        "alerted_at": "yesterday",
        "missing_since": NOW - 120.0,
        "outage_row": 42,
    })
    assert _pass(monkeypatch, NOW) == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "resetting non-numeric alerted_at" in err
    gw, notes = _notes(gw_path)
    assert len(notes) == 1
    assert "#42" in notes[0]["content"]
    assert [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []

    assert _pass(monkeypatch, NOW + 120.0) == 0
    gw, notes = _notes(gw_path)
    assert len(notes) == 1
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["lab-ovh"]["alerted_at"] == NOW


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_open_outage_with_non_numeric_missing_since_realerts(tmp_path, monkeypatch, capsys):
    state_path, gw_path = _arm_open_outage(tmp_path, monkeypatch, {
        "outage": True,
        "alerted_at": NOW - 10.0,
        "missing_since": "not-a-time",
        "outage_row": 42,
    })
    assert _pass(monkeypatch, NOW) == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "resetting non-numeric missing_since" in err
    _gw, notes = _notes(gw_path)
    assert len(notes) == 1
    assert "#42" in notes[0]["content"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(saved["lab-ovh"]["missing_since"], float)
    assert saved["lab-ovh"]["missing_since"] <= NOW - GAP_SECONDS

    assert _pass(monkeypatch, NOW + 120.0) == 0
    _gw, notes = _notes(gw_path)
    assert len(notes) == 1


def test_cell_not_in_outage_with_a_bad_clock_still_starts_the_gap():
    state = {"cell": {"missing_since": "not-a-time", "alerted_at": "yesterday"}}
    assert scan({"cell": "/tmp/no-such-inbox-1732"}, [], state, now=NOW) == []
    assert state["cell"]["missing_since"] == NOW
    assert state["cell"]["alerted_at"] is None
    assert state["cell"].get("outage") is not True


def _due_self_and_peer(tmp_path, monkeypatch):
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _cell(root, "peer-cell", alive=False)
    _yaml(tmp_path / "config")
    state_dir = tmp_path / "wd-state"
    state_dir.mkdir()
    state_path = state_dir / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"missing_since": NOW - 120.0},
        "peer-cell": {"missing_since": NOW - 120.0},
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [],
        "next_id": 1,
        "dm_rc": 0,
        "say_rc": 0,
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    _watch_env(monkeypatch, tmp_path, root, state_path, stub)
    return state_dir, state_path, gw_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_mode_0444_lock_file_still_alerts_a_due_peer(tmp_path, monkeypatch, capsys):
    _state_dir, state_path, gw_path = _due_self_and_peer(tmp_path, monkeypatch)
    lock_path = state_path.parent / (state_path.name + ".lock")
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o444)
    try:
        rc = _pass(monkeypatch, NOW)
    finally:
        lock_path.chmod(0o644)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.count("skipping self-outage mint this pass") == 1
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert any(d["to"] == "peer-cell" for d in gw["dms"])
    assert [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_mode_0555_state_directory_still_alerts_a_due_peer(tmp_path, monkeypatch, capsys):
    state_dir, _state_path, gw_path = _due_self_and_peer(tmp_path, monkeypatch)
    state_dir.chmod(0o555)
    try:
        rc = _pass(monkeypatch, NOW)
        err = capsys.readouterr().err
        gw = json.loads(gw_path.read_text(encoding="utf-8"))
    finally:
        state_dir.chmod(0o755)
    assert rc == 1
    assert "Traceback" not in err
    assert err.count("skipping self-outage mint this pass") == 1
    assert any(d["to"] == "peer-cell" for d in gw["dms"])
    assert [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_stored_ask_then_broken_listing_does_not_mint_a_second_row(
    tmp_path, monkeypatch, capsys
):
    """A stored ask that exits 1 holds the mint slot, so a broken listing mints no second row.

    The unstored fallback is a different ask: it prints no row id, so the slot
    stays a failure and test_unstored_failed_ask_does_not_block_fallback_mint
    can still mint once the listing cap is passed.
    """
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"missing_since": NOW - 120.0},
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [],
        "next_id": 1,
        "ask_rc": 1,
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    _watch_env(monkeypatch, tmp_path, root, state_path, stub)

    assert _pass(monkeypatch, NOW) == 1
    err = capsys.readouterr().err
    assert "holding the mint slot" in err
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in gw["obligations"]] == [1]
    assert len([c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]]) == 1

    gw["list_override"] = {"rc": 1, "stdout": "", "stderr": "listing down"}
    gw_path.write_text(json.dumps(gw), encoding="utf-8")
    for i in range(MAX_CONSECUTIVE_LIST_FAILURES):
        assert _pass(monkeypatch, NOW + (i + 1) * 120.0) == 1
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in gw["obligations"]] == [1]

    assert _pass(monkeypatch, NOW + (MAX_CONSECUTIVE_LIST_FAILURES + 1) * 120.0) == 0
    for extra in range(4):
        assert _pass(
            monkeypatch,
            NOW + (MAX_CONSECUTIVE_LIST_FAILURES + 2 + extra) * 120.0,
        ) == 0
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in gw["obligations"]] == [1]
    assert len([c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]]) == 1
