"""Tests for ``swarph brain-ask`` — offline, mocked HTTP.

Live smoke (a real gbrain round-trip) is out of scope here; these cover the
request shape, SSE parse, chunk formatting, token resolution, and the
retrieval-only handler path.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from swarph_cli.commands import brain_ask as ba


# --- version ---------------------------------------------------------------

def test_version_is_0_32_0():
    import swarph_cli
    assert swarph_cli.__version__ == "0.32.0"


# --- request / response plumbing -------------------------------------------

def test_build_query_request_shape():
    body = ba._build_query_request("does the swarm know X", limit=4)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "query"
    assert body["params"]["arguments"] == {
        "query": "does the swarm know X", "limit": 4, "expand": False}


def test_parse_query_response_sse():
    chunks = [{"slug": "project_x", "chunk_text": "X is...", "score": 0.91}]
    inner = json.dumps({"result": {"content": [{"text": json.dumps(chunks)}]}})
    raw = f"event: message\ndata: {inner}\n\n"
    assert ba._parse_query_response(raw) == chunks


def test_parse_query_response_empty():
    inner = json.dumps({"result": {"content": [{"text": json.dumps([])}]}})
    assert ba._parse_query_response(f"data: {inner}") == []


def test_format_chunks_renders_slug_score_text():
    out = ba._format_chunks([{"slug": "p_x", "chunk_text": "hello", "score": 0.9}])
    assert "[p_x]" in out and "0.90" in out and "hello" in out


def test_format_chunks_empty():
    assert "no relevant" in ba._format_chunks([]).lower()


# --- token resolution (mirrors `swarph mesh` peer-token precedence) ---------

def test_resolve_token_prefers_env(monkeypatch):
    monkeypatch.setenv("GBRAIN_TOKEN", "gbrain_envtok")
    assert ba._resolve_token(token_file=None, self_name="lab-ovh") == "gbrain_envtok"


def test_resolve_token_falls_back_to_swarph_brain_token(monkeypatch):
    monkeypatch.delenv("GBRAIN_TOKEN", raising=False)
    monkeypatch.setenv("SWARPH_BRAIN_TOKEN", "gbrain_legacy")
    assert ba._resolve_token(token_file=None, self_name="lab-ovh") == "gbrain_legacy"


# --- the handler (retrieval-only path, HTTP mocked) ------------------------

def test_run_retrieval_only_prints_chunks(capsys, monkeypatch):
    chunks = [{"slug": "deferred", "chunk_text":
               "governor order Claude->Gemini->GPT->Grok", "score": 0.88}]
    inner = json.dumps({"result": {"content": [{"text": json.dumps(chunks)}]}})
    monkeypatch.setenv("GBRAIN_TOKEN", "gbrain_test")
    with patch.object(ba, "_http_post", return_value=f"data: {inner}"):
        rc = ba.run_brain_ask(["--no-synth", "what", "is", "the", "governor", "order"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[deferred]" in out and "governor order" in out


def test_run_no_token_errors_cleanly(capsys, monkeypatch):
    monkeypatch.delenv("GBRAIN_TOKEN", raising=False)
    monkeypatch.delenv("SWARPH_BRAIN_TOKEN", raising=False)
    monkeypatch.setattr(ba, "_peer_token_path", lambda self_name: __import__("pathlib").Path("/nonexistent"))
    rc = ba.run_brain_ask(["--no-synth", "anything"])
    assert rc == 2
    assert "token" in capsys.readouterr().err.lower()


# --- synthesis path (retrieve -> $0 facade cited answer) -------------------

def test_synthesize_returns_facade_text():
    chunks = [{"slug": "p_x", "chunk_text": "X is foo", "score": 0.9}]
    facade_resp = json.dumps({"choices": [{"message": {"content": "X is foo [p_x]."}}]})
    with patch.object(ba, "_http_post", return_value=facade_resp):
        out = ba._synthesize("http://facade/v1/chat/completions", "ftok", "what is X", chunks)
    assert "[p_x]" in out


def test_run_synth_path_prints_answer(capsys, monkeypatch):
    chunks = [{"slug": "p_x", "chunk_text": "X is foo", "score": 0.9}]
    query_resp = "data: " + json.dumps(
        {"result": {"content": [{"text": json.dumps(chunks)}]}})
    facade_resp = json.dumps({"choices": [{"message": {"content": "Answer: X is foo [p_x]."}}]})
    monkeypatch.setenv("GBRAIN_TOKEN", "gbrain_test")
    monkeypatch.setenv("SWARPH_FACADE", "http://facade/v1/chat/completions")
    monkeypatch.setenv("SWARPH_FACADE_TOKEN", "ftok")
    with patch.object(ba, "_http_post", side_effect=[query_resp, facade_resp]):
        rc = ba.run_brain_ask(["what", "is", "X"])
    assert rc == 0
    assert "Answer: X is foo" in capsys.readouterr().out


# --- endpoint resolution (0.14.1: SWARPH_BRAIN_MCP fallback + localhost default) ---

def test_resolve_endpoint_prefers_gbrain_mcp_url(monkeypatch):
    monkeypatch.setenv("GBRAIN_MCP_URL", "http://gb/mcp")
    monkeypatch.setenv("SWARPH_BRAIN_MCP", "http://sb/mcp")
    assert ba._resolve_endpoint() == "http://gb/mcp"


def test_resolve_endpoint_falls_back_to_swarph_brain_mcp(monkeypatch):
    monkeypatch.delenv("GBRAIN_MCP_URL", raising=False)
    monkeypatch.setenv("SWARPH_BRAIN_MCP", "http://sb/mcp")
    assert ba._resolve_endpoint() == "http://sb/mcp"


def test_resolve_endpoint_default_is_localhost(monkeypatch):
    monkeypatch.delenv("GBRAIN_MCP_URL", raising=False)
    monkeypatch.delenv("SWARPH_BRAIN_MCP", raising=False)
    assert ba._resolve_endpoint() == "http://127.0.0.1:8792/mcp"


# --- SWARPH_BRAIN_GATEWAY client path (Task 2) ------------------------------

def test_gateway_query_posts_brain_query_with_peer_token(monkeypatch, tmp_path):
    from swarph_cli.commands import brain_ask
    captured = {}
    def fake_http_post(url, body, token, accept="application/json, text/event-stream", timeout=30):
        captured.update(url=url, body=body, token=token)
        import json as _j
        return _j.dumps({"chunks": [{"slug": "s1", "chunk_text": "x"}]})
    monkeypatch.setattr(brain_ask, "_http_post", fake_http_post)
    out = brain_ask._gateway_query("http://gw:8788", "peer_tok_123", "hello", 5)
    assert out == [{"slug": "s1", "chunk_text": "x"}]
    assert captured["url"] == "http://gw:8788/brain/query"
    assert captured["body"] == {"query": "hello", "limit": 5}
    assert captured["token"] == "peer_tok_123"


def test_run_brain_ask_uses_gateway_when_env_set(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import brain_ask
    # peer token on disk for _self_name()
    cfg = tmp_path / ".config" / "swarph"
    cfg.mkdir(parents=True)
    (cfg / "wlc.peer_token").write_text("peer_tok_xyz", encoding="utf-8")
    monkeypatch.setattr(brain_ask.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SWARPH_SELF", "wlc")
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    seen = {}
    def fake_gateway_query(gw, tok, q, n):
        seen.update(gw=gw, tok=tok, q=q)
        return [{"slug": "s1", "chunk_text": "hit"}]
    monkeypatch.setattr(brain_ask, "_gateway_query", fake_gateway_query)
    rc = brain_ask.run_brain_ask(["--no-synth", "what", "is", "up"])
    assert rc == 0
    assert seen["gw"] == "http://gw:8788" and seen["tok"] == "peer_tok_xyz"
    assert "s1" in capsys.readouterr().out


def test_run_brain_ask_direct_path_unchanged_when_gateway_unset(monkeypatch, capsys):
    from swarph_cli.commands import brain_ask
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    monkeypatch.setenv("GBRAIN_TOKEN", "gbrain_direct")
    called = {}
    def fake_mcp(url, tok, q, n):
        called["hit"] = (url, tok)
        return [{"slug": "d"}]
    monkeypatch.setattr(brain_ask, "_mcp_query", fake_mcp)
    rc = brain_ask.run_brain_ask(["--no-synth", "hello"])
    assert rc == 0
    assert called["hit"][1] == "gbrain_direct"     # direct path used the gbrain token
    assert "d" in capsys.readouterr().out
