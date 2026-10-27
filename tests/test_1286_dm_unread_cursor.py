"""Card #1286: swarph_dm_unread agrees with the monitor cursor.

A DM with read_at NULL that the cursor has already passed is not unread.
The call is a peek: cursor.json is not rewritten and nothing is marked read.
"""
from __future__ import annotations

import datetime
import json
import time

from swarph_cli.commands import mesh
from swarph_cli.commands import mcp_server
from swarph_cli.dm_frame import frame_text


def test_1286_stale_unread_behind_the_cursor_is_not_new(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mcp_server, "_dm_session", lambda self_name=None: ("lab-ovh", "http://gw", "tok"),
    )
    state = tmp_path / "mesh-sidecar"
    state.mkdir()
    # Path.home() ignores HOME on Windows, so point the sidecar helper itself.
    monkeypatch.setattr(mcp_server._mesh, "_default_sidecar_state_dir", lambda who: state)
    cursor = state / "cursor.json"
    cursor.write_text(json.dumps({"last_msg_id": 200}), encoding="utf-8")
    before = cursor.read_bytes()
    rows = [
        {"id": 100, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "status",
         "content": "STALE1286", "read_at": None},
        {"id": 200, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "status",
         "content": "BOUNDARY1286", "read_at": None},
        {"id": 300, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "status",
         "content": "NEW1286 full body", "read_at": None},
        {"id": 400, "from_node": "lab-ovh", "to_node": "cursor-lin", "kind": "status",
         "content": "SELF1286", "read_at": None},
    ]

    def fake_get(url, token, **kw):
        assert "unread_only=true" in url
        return 200, {"messages": list(reversed(rows)), "n": len(rows)}

    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", fake_get)
    out = mcp_server._dm_unread()
    # 200 equals the cursor: observed, not yet delivered, so it is returned.
    # 100 is strictly behind and stays hidden.
    assert [m["id"] for m in out["messages"]] == [200, 300]
    assert out["messages"][1]["content"] == frame_text(rows[2])
    assert "NEW1286 full body" in out["messages"][1]["content"]
    assert "BOUNDARY1286" in out["messages"][0]["content"]
    assert "STALE1286" not in json.dumps(out)
    assert "SELF1286" not in json.dumps(out)
    assert out["n"] == 2 and out["not_returned"] == 0
    # 100 is behind the cursor and has no created_at, so it is not within 24 h.
    assert out.get("hidden_stale") == 1
    assert cursor.read_bytes() == before


def test_1286_monitor_iteration_then_unread_returns_the_woken_dm(monkeypatch, tmp_path):
    """One real monitor iteration observes a new DM, then unread still returns it.

    The cursor advances before delivery, so the woken id equals last_msg_id.
    On 74347164 unread uses a strict `>` and the DM is gone.
    """
    state_dir = tmp_path / "mesh-sidecar"
    state_dir.mkdir()
    (state_dir / "cursor.json").write_text(
        json.dumps({"last_msg_id": 100}), encoding="utf-8")
    monkeypatch.setattr(mcp_server._mesh, "_default_sidecar_state_dir", lambda who: state_dir)
    monkeypatch.setattr(
        mcp_server, "_dm_session", lambda self_name=None: ("lab-ovh", "http://gw", "tok"),
    )
    woken = {
        "id": 501, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "question",
        "content": "WOKEN1286 full body", "read_at": None,
    }
    stale = {
        "id": 50, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "status",
        "content": "BEHIND1286", "read_at": None,
    }

    def fake_get(url, token, **kw):
        return 200, {"messages": [stale, woken]}

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    state = mesh.MonitorState(
        self_name="lab-ovh",
        state_dir=state_dir,
        gateway="http://gw",
        token="tok",
        sinks=[mesh.parse_sink("none")],
    )
    mesh._monitor_iteration(state, poll_channels=False)
    out = mcp_server._dm_unread()
    ids = [m["id"] for m in out["messages"]]
    assert ids == [501]
    assert "WOKEN1286 full body" in out["messages"][0]["content"]
    assert "BEHIND1286" not in json.dumps(out)
    assert out.get("hidden_stale") == 1


def _iso(offset_s: float) -> str:
    stamp = datetime.datetime.fromtimestamp(time.time() + offset_s, datetime.timezone.utc)
    return stamp.isoformat()


def _row(mid: int, content: str, created_at: str) -> dict:
    return {
        "id": mid, "from_node": "cursor-lin", "to_node": "lab-ovh", "kind": "question",
        "content": content, "read_at": None, "created_at": created_at,
    }


def _monitor(monkeypatch, tmp_path, rows, *, polls: int = 1, windows=None):
    state_dir = tmp_path / "mesh-sidecar"
    state_dir.mkdir()
    (state_dir / "cursor.json").write_text(
        json.dumps({"last_msg_id": 100}), encoding="utf-8")
    monkeypatch.setattr(mcp_server._mesh, "_default_sidecar_state_dir", lambda who: state_dir)
    monkeypatch.setattr(
        mcp_server, "_dm_session", lambda self_name=None: ("lab-ovh", "http://gw", "tok"),
    )
    seen = {"n": 0}

    def fake_get(url, token, **kw):
        if "unread_only" in url:
            return 200, {"messages": list(rows)}
        if windows is None:
            return 200, {"messages": list(rows)}
        index = min(seen["n"], len(windows) - 1)
        seen["n"] += 1
        wanted = windows[index]
        return 200, {"messages": [row for row in rows if row["id"] in wanted]}

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    state = mesh.MonitorState(
        self_name="lab-ovh",
        state_dir=state_dir,
        gateway="http://gw",
        token="tok",
        sinks=[mesh.parse_sink("none")],
    )
    for _ in range(polls):
        mesh._monitor_iteration(state, poll_channels=False)
    return mcp_server._dm_unread(), state


def test_1286_one_poll_observing_three_returns_all_three(monkeypatch, tmp_path):
    """One poll records the cursor at the newest id. The two earlier DMs are young."""
    young = _iso(0)
    rows = [
        _row(501, "ONE1286", young),
        _row(502, "TWO1286", young),
        _row(503, "THREE1286", young),
    ]
    out, state = _monitor(monkeypatch, tmp_path, rows)
    assert state.observed["last_msg_id"] == 503
    assert [m["id"] for m in out["messages"]] == [501, 502, 503]
    assert out.get("hidden_stale") == 0


def test_1286_three_polls_then_unread_returns_all_three(monkeypatch, tmp_path):
    """A busy host calls the tool after three polls, one new DM each."""
    young = _iso(-60)
    rows = [
        _row(501, "POLL1286a", young),
        _row(502, "POLL1286b", young),
        _row(503, "POLL1286c", young),
    ]
    out, state = _monitor(
        monkeypatch, tmp_path, rows, polls=3, windows=([501], [501, 502], [501, 502, 503]),
    )
    assert state.observed["last_msg_id"] == 503
    assert [m["id"] for m in out["messages"]] == [501, 502, 503]
    assert "POLL1286a" in out["messages"][0]["content"]
    assert out.get("hidden_stale") == 0


def test_1286_old_dm_behind_the_cursor_is_hidden_and_counted(monkeypatch, tmp_path):
    young = _iso(0)
    old = _iso(-(25 * 60 * 60))
    rows = [
        _row(50, "OLD1286", old),
        _row(80, "YOUNGBEHIND1286", young),
        _row(501, "FRESH1286", young),
    ]
    out, state = _monitor(monkeypatch, tmp_path, rows)
    assert state.observed["last_msg_id"] == 501
    assert "OLD1286" not in json.dumps(out)
    assert out.get("hidden_stale") == 1
    assert [m["id"] for m in out["messages"]] == [80, 501]
    assert "YOUNGBEHIND1286" in out["messages"][0]["content"]
