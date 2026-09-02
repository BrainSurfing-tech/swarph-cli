"""#564 — first peers on a NEW bundled gateway must be minted.

`swarph gateway serve` is what someone who just `pip install swarph-cli`
runs. Operator onboard on that empty mesh used to defer (or mint-and-
discard). Either way the first cells walk away with no credential and
cannot use the tools.

These tests hit the bundled gateway the new-mesh path actually serves.
"""
from __future__ import annotations

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


def _register(client, name, **extra):
    body = {"name": name, "url": "http://x:8787", "capabilities": {}}
    body.update(extra)
    return client.post(
        "/peers/register",
        headers={"Authorization": "Bearer test-token"},
        json=body,
    )


def test_fresh_mesh_defer_MINTS_first_peer(monkeypatch):
    server = _load(monkeypatch)
    r = _register(_client(server), "first-cell", defer_token_mint=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_status"] == "minted"
    assert body["peer_token"]
    assert body["bootstrap_mint"] is True


def test_second_unratified_peer_also_mints(monkeypatch):
    server = _load(monkeypatch)
    c = _client(server)
    _register(c, "first-cell", defer_token_mint=True)
    r = _register(c, "second-cell", defer_token_mint=True)
    body = r.json()
    assert body["token_status"] == "minted"
    assert body["bootstrap_mint"] is True


def test_after_first_rung_defer_mints_nothing(monkeypatch):
    server = _load(monkeypatch)
    with sqlite3.connect(server.DB_PATH) as db:
        db.execute(
            "INSERT INTO claude_peers "
            "(name, url, capabilities, registered_at, last_seen, ratified) "
            "VALUES ('anchor', 'http://a:1', '{}', datetime('now'), "
            "datetime('now'), 1)"
        )
        db.commit()
    r = _register(_client(server), "later-cell", defer_token_mint=True)
    body = r.json()
    assert body["token_status"] == "deferred"
    assert body["peer_token"] is None
    assert body["bootstrap_mint"] is False
