"""Card #1286: swarph_dm_unread agrees with the monitor cursor.

A DM with read_at NULL that the cursor has already passed is not unread.
The call is a peek: cursor.json is not rewritten and nothing is marked read.
"""
from __future__ import annotations

import json

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
