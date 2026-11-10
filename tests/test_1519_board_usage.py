"""card #1519 — CLI half of structured usage readings.

Fake gateway (the existing board test pattern): --usage parses into the
reading JSON on take and on close; malformed --usage exits 2 with no
request sent; `board usage` shows 'no reading' for a lane with nothing
and a real 0 for a row whose close equals its take.
"""
from __future__ import annotations

import json

from swarph_cli.commands import board

USAGE_PAYLOAD = {
    "lanes": {
        "empty-lane": {"lane": "empty-lane", "state": "no reading",
                       "latest": None, "deltas": [], "trend": []},
        "claude-max": {
            "lane": "claude-max", "state": "ok",
            "latest": {"lane": "claude-max", "unit": "weekly_pct", "value": 42,
                       "read_at": "2026-11-09T10:00:00+00:00"},
            "deltas": [{"obligation_id": 7, "lane": "claude-max",
                        "unit": "weekly_pct", "take_value": 42,
                        "close_value": 42, "delta": 0.0,
                        "read_at": "2026-11-09T11:00:00+00:00"}],
            "trend": [{"date": "2026-11-09", "reading_count": 2,
                       "delta_by_unit": {"weekly_pct": 0.0}}],
        },
    },
    "days": 7,
}


def _harness(monkeypatch, tmp_path, *, payload=None):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    calls = []

    def fake_post(url, body, token, *, timeout=10.0):
        calls.append(("POST", url, body))
        return 200, {"id": 22, "status": "closed", "closed_by": "cursor-lin",
                     "close_outcome": body.get("outcome", "pass"),
                     "holder": "cursor-lin", "step": "build", "card_id": 1,
                     "state": "taken", "due": None, "close_with": "x"}

    def fake_get(url, token, *, timeout=10.0):
        calls.append(("GET", url, None))
        if "/board/usage" in url:
            return 200, payload if payload is not None else USAGE_PAYLOAD
        return 200, {"obligations": []}

    monkeypatch.setattr(board, "_post_json", fake_post)
    monkeypatch.setattr(board, "_http_get_json", fake_get)
    return calls


def test_take_usage_parses_into_the_reading(monkeypatch, tmp_path):
    calls = _harness(monkeypatch, tmp_path)
    rc = board.run_board(["obligations", "take", "22", "--as", "cursor-lin",
                          "--usage", "claude-max:weekly_pct=42@2026-11-15T00:00:00Z"])
    assert rc == 0
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/board/obligations/22/take")
    reading = posts[0][2]["usage"]
    assert reading["lane"] == "claude-max"
    assert reading["unit"] == "weekly_pct"
    assert reading["value"] == 42
    assert reading["window_resets_at"] == "2026-11-15T00:00:00Z"
    assert reading["read_at"]  # stamped client-side


def test_close_usage_parses_into_the_reading(monkeypatch, tmp_path):
    calls = _harness(monkeypatch, tmp_path)
    rc = board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                          "--outcome", "pass", "--evidence", "ran the check",
                          "--usage", "claude-max:weekly_pct=42@2026-11-15T00:00:00Z"])
    assert rc == 0
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/board/obligations/22/close")
    reading = posts[0][2]["usage"]
    assert reading["lane"] == "claude-max"
    assert reading["unit"] == "weekly_pct"
    assert reading["value"] == 42
    assert reading["window_resets_at"] == "2026-11-15T00:00:00Z"
    assert reading["read_at"]


def test_malformed_usage_exits_2_without_a_request(monkeypatch, tmp_path):
    calls = _harness(monkeypatch, tmp_path)
    rc = board.run_board(["obligations", "take", "22", "--as", "cursor-lin",
                          "--usage", "not-a-reading"])
    assert rc == 2
    assert calls == []
    rc = board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                          "--outcome", "pass", "--evidence", "ran the check",
                          "--usage", "claude-max:bogus_unit=42"])
    assert rc == 2
    assert calls == [], "malformed --usage sends nothing, not even the taken_at read"


def test_board_usage_human_distinguishes_no_reading_from_zero_delta(monkeypatch, tmp_path, capsys):
    _harness(monkeypatch, tmp_path)
    rc = board.run_board(["usage", "--as", "cursor-lin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "empty-lane: no reading" in out
    assert "delta #7: 0.0 weekly_pct (42 -> 42)" in out
    assert "shared-auth" not in out


def test_board_usage_json_parses(monkeypatch, tmp_path, capsys):
    _harness(monkeypatch, tmp_path)
    rc = board.run_board(["usage", "--as", "cursor-lin", "--json",
                          "--lane", "claude-max", "--days", "3"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == USAGE_PAYLOAD
