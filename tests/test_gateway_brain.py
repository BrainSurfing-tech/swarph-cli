import json
import os
import importlib
import tempfile
import pytest
from types import SimpleNamespace


def _load_app(monkeypatch, *, gbrain_token="gbrain_hostheld", auth="tok_shared"):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", auth)
    monkeypatch.setenv("GATEWAY_GBRAIN_TOKEN", gbrain_token)
    monkeypatch.setenv("GATEWAY_GBRAIN_URL", "http://127.0.0.1:8792/mcp")
    # NOTE: ":memory:" (as written in the task brief) is incompatible with this
    # module's _conn()/_init_db(), which open a FRESH sqlite3.connect() per call
    # with no shared cache — each ":memory:" connection is an independent,
    # isolated DB, so the schema built by _init_db()'s first connection is gone
    # by its second. That fails at import time, before any endpoint code runs,
    # for every one of the 6 tests here — not the brief's expected 404/
    # AttributeError RED. Using a real temp-file path (matching the existing
    # pattern in tests/test_meta_edge_identity.py) fixes the scaffold without
    # touching production DB code, and restores the brief's intended RED.
    monkeypatch.setenv("MESH_DB_PATH", os.path.join(tempfile.mkdtemp(), "mesh.db"))
    from swarph_cli.gateway import server
    importlib.reload(server)          # re-read module-top env config
    return server


def _sse(chunks):
    inner = json.dumps(chunks)
    env = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": inner}]}}
    return "event: message\ndata: " + json.dumps(env) + "\n\n"


def _client(server):
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def test_brain_query_authenticates_and_returns_chunks(monkeypatch):
    server = _load_app(monkeypatch)
    chunks = [{"slug": "s1", "title": "T", "chunk_text": "hello", "score": 0.9}]
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: chunks)
    r = _client(server).post("/brain/query", json={"query": "hi", "limit": 5},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 200
    assert r.json() == {"chunks": chunks}


def test_brain_query_bad_token_401(monkeypatch):
    server = _load_app(monkeypatch)
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: [])
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_brain_query_unconfigured_503(monkeypatch):
    server = _load_app(monkeypatch, gbrain_token="")   # GATEWAY_GBRAIN_TOKEN unset
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 503


def test_brain_query_empty_query_400(monkeypatch):
    server = _load_app(monkeypatch)
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: [])
    r = _client(server).post("/brain/query", json={"query": "   "},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 400


def test_brain_query_upstream_error_502(monkeypatch):
    server = _load_app(monkeypatch)
    def boom(q, n): raise RuntimeError("gbrain down")
    monkeypatch.setattr(server, "_brain_query_upstream", boom)
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 502


def test_upstream_helper_is_read_only_by_construction(monkeypatch):
    """The MCP body the proxy sends upstream ALWAYS has params.name == 'query'."""
    server = _load_app(monkeypatch)
    captured = {}
    class FakeResp:
        def read(self): return _sse([{"slug": "s"}]).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        return FakeResp()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    out = server._brain_query_upstream("anything", 8)
    assert out == [{"slug": "s"}]
    assert captured["body"]["params"]["name"] == "query"          # read-only lock
    assert captured["auth"] == "Bearer gbrain_hostheld"           # gateway's held token
