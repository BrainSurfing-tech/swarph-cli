"""Card #1286: swarph_dm_unread agrees with the monitor cursor.

A DM with read_at NULL that the cursor has already passed is not unread.
The call is a peek: cursor.json is not rewritten and nothing is marked read.
"""
from __future__ import annotations

import json

from swarph_cli.commands import mcp_server
from swarph_cli.dm_frame import frame_text


def test_1286_stale_unread_behind_the_cursor_is_not_new(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        mcp_server, "_dm_session", lambda self_name=None: ("lab-ovh", "http://gw", "tok"),
    )
    state = tmp_path / "swarph_state" / "lab-ovh" / "mesh-sidecar"
    state.mkdir(parents=True)
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
    assert [m["id"] for m in out["messages"]] == [300]
    assert out["messages"][0]["content"] == frame_text(rows[2])
    assert "NEW1286 full body" in out["messages"][0]["content"]
    assert "STALE1286" not in json.dumps(out)
    assert "SELF1286" not in json.dumps(out)
    assert out["n"] == 1 and out["not_returned"] == 0
    assert cursor.read_bytes() == before
