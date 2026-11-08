"""MCP DM tools require SWARPH_SELF (#1499)."""
from __future__ import annotations

import pytest

from swarph_cli.commands import mcp_server


@pytest.fixture
def _clear_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.delenv("SWARPH_CELL", raising=False)


def test_dm_tools_refuse_when_swraph_self_unset(_clear_identity):
    for fn, args in (
        (mcp_server._dm_unread, ()),
        (mcp_server._dm_reply, (1, "hi")),
        (mcp_server._dm_thread, (1,)),
    ):
        out = fn(*args)
        assert out.get("n", 0) == 0
        err = out.get("error", "")
        assert "SWARPH_SELF" in err or "identity" in err.lower(), (fn.__name__, err)


def test_dm_unread_works_with_swraph_self_set(_clear_identity, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "friendly-coder")
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw.test:8788")
    monkeypatch.setattr(
        mcp_server._mesh,
        "_resolve_token",
        lambda who, tf: "tok",
    )
    monkeypatch.setattr(
        mcp_server._mesh,
        "_http_get_json",
        lambda url, tok, **kw: (200, {"messages": [], "n": 0}),
    )
    out = mcp_server._dm_unread()
    assert "error" not in out
    assert out["n"] == 0
