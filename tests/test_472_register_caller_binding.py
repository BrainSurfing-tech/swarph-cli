"""#472 — the bundled gateway's POST /peers/register had ZERO caller binding.

`pip install swarph-cli && swarph gateway serve` stands up this gateway. Its
peers_register called _authorize and never _check_caller_binding, so any
per-peer-authenticated caller could register ANY name and be handed a freshly
minted credential for it. The deployed gateway (mesh-gateway #467b/#108) binds
the caller to req.name unconditionally, before the transaction. This ports that
one call, with the same field and site names so both trees' telemetry agrees.

Scope, stated: the shared token stays unbound (C2's documented no-op; closure
is C5), and MESH_CALLER_BINDING_ENFORCE still defaults to warn, as in the
deployed gateway.
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")


def _load(monkeypatch):
    import importlib
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("MESH_DB_PATH", os.path.join(tempfile.mkdtemp(), "mesh.db"))
    from swarph_cli.gateway import server
    importlib.reload(server)
    return server


def _client(server):
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def _register(client, name, token="test-token"):
    return client.post("/peers/register", headers={"Authorization": f"Bearer {token}"},
                       json={"name": name, "url": "http://x:8787", "capabilities": {}})


def _rows(server, sql, *args):
    with sqlite3.connect(server.DB_PATH) as c:
        return c.execute(sql, args).fetchall()


def _alpha(server, client):
    r = _register(client, "alpha-472")
    assert r.status_code == 200 and r.json()["peer_token"], r.text
    return r.json()["peer_token"]


def test_a_peer_token_cannot_register_ANOTHER_name_under_enforce(monkeypatch):
    """The card's defect. On main: 200 and a freshly minted token for beta."""
    server = _load(monkeypatch)
    c = _client(server)
    tok = _alpha(server, c)
    monkeypatch.setattr(server, "MESH_CALLER_BINDING_ENFORCE", True)
    r = _register(c, "beta-472", token=tok)
    assert r.status_code == 403, r.text
    assert "register_mint_target" in r.json()["detail"]
    assert _rows(server, "SELECT 1 FROM claude_peers WHERE name='beta-472'") == []
    assert _rows(server, "SELECT 1 FROM peer_tokens WHERE peer='beta-472'") == []


def test_warn_mode_records_the_mismatch(monkeypatch):
    """Default (warn) mode: the call proceeds, as everywhere C2 binds, but the
    mismatch is now recorded. On main no row is written: the check never ran."""
    server = _load(monkeypatch)
    c = _client(server)
    tok = _alpha(server, c)
    monkeypatch.setattr(server, "MESH_CALLER_BINDING_ENFORCE", False)
    assert _register(c, "gamma-472", token=tok).status_code == 200
    rows = _rows(server, "SELECT peer, metadata FROM peer_health_events "
                         "WHERE event_type='caller_binding_mismatch'")
    assert any(p == "alpha-472" and '"site": "peers_register_mint"' in m for p, m in rows), rows


def test_a_peer_can_still_re_register_ITSELF_under_enforce(monkeypatch):
    server = _load(monkeypatch)
    c = _client(server)
    tok = _alpha(server, c)
    monkeypatch.setattr(server, "MESH_CALLER_BINDING_ENFORCE", True)
    r = _register(c, "alpha-472", token=tok)
    assert r.status_code == 200, r.text
    assert r.json()["token_status"] == "existing"


def test_the_card_measurement_peers_register_calls_the_binder():
    """The card's own AST measurement: _check_caller_binding x0 on main."""
    from swarph_cli.gateway import server
    fn = ast.parse(inspect.getsource(server.peers_register))
    calls = [n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls.count("_authorize") == 1
    assert calls.count("_check_caller_binding") >= 1
