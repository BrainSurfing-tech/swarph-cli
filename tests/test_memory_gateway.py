import json
import types
import pytest

from swarph_cli.commands import memory


def _fake_http_post_capturing(store):
    def _fp(url, body, token, accept="application/json, text/event-stream", timeout=30):
        store["url"] = url
        store["body"] = body
        store["token"] = token
        # gateway /memory returns {"result": ...}
        return json.dumps({"result": store["_ret"]})
    return _fp


def _set_peer(monkeypatch, tmp_path, name="science-claude", token="peer_tok"):
    monkeypatch.setenv("SWARPH_SELF", name)
    p = tmp_path / f"{name}.peer_token"
    p.write_text(token)
    monkeypatch.setattr(memory.brain_ask, "_peer_token_path", lambda n: p)


def test_get_page_routes_via_gateway_with_peer_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    _set_peer(monkeypatch, tmp_path)
    store = {"_ret": {"slug": "s1", "compiled_truth": "b"}}
    monkeypatch.setattr(memory.brain_ask, "_http_post", _fake_http_post_capturing(store))
    out = memory.get_page("http://ignored:8792/mcp", "ignored_token", "s1")
    assert out == {"slug": "s1", "compiled_truth": "b"}
    assert store["url"] == "http://gw:8788/memory"
    assert store["token"] == "peer_tok"                     # peer token, not the gbrain token
    assert store["body"] == {"op": "get", "arguments": {"slug": "s1"}}


def test_list_pages_routes_via_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    _set_peer(monkeypatch, tmp_path)
    store = {"_ret": [{"slug": "a"}, {"slug": "b"}]}
    monkeypatch.setattr(memory.brain_ask, "_http_post", _fake_http_post_capturing(store))
    out = memory.list_pages("http://ignored:8792/mcp", "ignored", type_="feedback", tag="sec", limit=5)
    assert out == [{"slug": "a"}, {"slug": "b"}]
    assert store["url"] == "http://gw:8788/memory"
    assert store["body"] == {"op": "list",
                             "arguments": {"limit": 5, "type": "feedback", "tag": "sec"}}


def test_direct_path_unchanged_when_gateway_unset(monkeypatch):
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    calls = {}
    def fake_mcp(url, token, tool, arguments):
        calls["args"] = (url, token, tool, arguments)
        return {"slug": "s1"}
    monkeypatch.setattr(memory, "_mcp_call", fake_mcp)
    out = memory.get_page("http://localhost:8792/mcp", "gbrain_tok", "s1")
    assert out == {"slug": "s1"}
    assert calls["args"] == ("http://localhost:8792/mcp", "gbrain_tok", "get_page", {"slug": "s1"})


def test_backlinks_traversal_inherits_gateway_routing(monkeypatch, tmp_path):
    """links --backlinks walk must run entirely through the routed primitives:
    it drives list_pages + get_page, which go via the gateway — no direct MCP."""
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    _set_peer(monkeypatch, tmp_path)
    pages = {"a": "links to [[b]]", "b": "leaf"}
    def fake_gateway(gw_base, peer_token, op, arguments):
        assert gw_base == "http://gw:8788" and peer_token == "peer_tok"
        if op == "list":
            return [{"slug": "a"}, {"slug": "b"}]
        return {"slug": arguments["slug"], "compiled_truth": pages[arguments["slug"]]}
    monkeypatch.setattr(memory, "_gateway_call", fake_gateway)
    # a direct MCP call here would mean routing leaked past the primitive layer:
    monkeypatch.setattr(memory, "_mcp_call",
                        lambda *a, **k: pytest.fail("traversal bypassed the gateway"))
    back = memory.backlinks("http://ignored:8792/mcp", "ignored", "b")
    assert back == ["a"]     # a links to b
