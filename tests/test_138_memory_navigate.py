"""#138 — memory-navigate must not render zero matches or a gateway error as silence.

On current main both cases return []: a no-match list, and a bogus op whose
gateway answer is 422.
"""
from __future__ import annotations

import io
import urllib.error

from swarph_cli.commands import mcp_server


def test_list_no_match_is_explicit_empty(monkeypatch):
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mcp_server.brain_ask, "_self_name", lambda: "cursor-lin")
    monkeypatch.setattr(mcp_server.memory, "list_pages", lambda *a, **k: [])
    assert mcp_server._memory_navigate("list", tag="swairm") == {"result": []}


def test_jsonrpc_error_envelope_is_an_error_for_list_get_and_links(monkeypatch):
    # On 3e5b7df this renders {result: []} for list, {} for get, and [] for links.
    err = '{"error": {"code": -32001}}'
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mcp_server.brain_ask, "_self_name", lambda: "cursor-lin")
    monkeypatch.setattr(mcp_server.memory.brain_ask, "_http_post", lambda *a, **k: err)
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    for op, kwargs in (("list", {"tag": "swairm"}), ("get", {"slug": "x"}), ("links", {"slug": "x"})):
        out = mcp_server._memory_navigate(op, **kwargs)
        assert isinstance(out, dict) and "error" in out, (op, out)
        assert "-32001" in out["error"]
        assert out != {"result": []}


def test_bogus_op_surfaces_gateway_422(monkeypatch):
    def _reject(op, arguments):
        raise urllib.error.HTTPError(
            "http://gw/memory", 422, "Unprocessable Entity",
            hdrs=None, fp=io.BytesIO(b"Input should be get or list"))

    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mcp_server.brain_ask, "_self_name", lambda: "cursor-lin")
    monkeypatch.setattr(mcp_server.memory, "_via_gateway", _reject)
    out = mcp_server._memory_navigate("bogus_op")
    assert out["status"] == 422
    assert "Input should be get or list" in out["error"]
    assert out != []
