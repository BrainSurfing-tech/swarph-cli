import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("MESH_DB_PATH", str(tmp_path / "mesh.db"))
    import swarph_cli.gateway.server as server

    return importlib.reload(server)


@pytest.fixture
def client(gateway):
    from fastapi.testclient import TestClient

    return TestClient(gateway.app)


def _post(client, **overrides):
    payload = {
        "from_node": "service-peer",
        "to_node": "source-peer",
        "kind": "answer",
        "content": "reply",
        "idempotency_key": "delivery-key",
    } | overrides
    return client.post("/messages", json=payload, headers={"Authorization": "Bearer test-token"})


def test_idempotent_dm_retry_returns_the_original_message(client, gateway):
    first = _post(client)
    second = _post(client)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    with sqlite3.connect(gateway.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM claude_messages").fetchone()[0] == 1


def test_idempotency_key_cannot_be_reused_for_different_message(client):
    assert _post(client).status_code == 200
    conflict = _post(client, content="different reply")
    assert conflict.status_code == 409


def test_same_key_is_independent_for_different_effective_senders(client, gateway):
    first = _post(client, from_node="service-a")
    second = _post(client, from_node="service-b")

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    with sqlite3.connect(gateway.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM claude_messages").fetchone()[0] == 2


def test_concurrent_same_key_retries_create_one_message(gateway):
    from fastapi.testclient import TestClient

    barrier = Barrier(2)
    lock = Lock()
    calls = 0
    original_find = gateway._find_idempotent_message

    def find_after_both_preflights(connection, sender, key):
        nonlocal calls
        result = original_find(connection, sender, key)
        with lock:
            calls += 1
            synchronize = calls <= 2
        if synchronize:
            barrier.wait(timeout=5)
        return result

    def post_once(content="reply"):
        with TestClient(gateway.app) as client:
            return _post(client, content=content)

    gateway._find_idempotent_message = find_after_both_preflights
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post_once(), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with sqlite3.connect(gateway.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM claude_messages").fetchone()[0] == 1


def test_concurrent_conflicting_key_reuse_fails_closed(gateway):
    from fastapi.testclient import TestClient

    barrier = Barrier(2)
    lock = Lock()
    calls = 0
    original_find = gateway._find_idempotent_message

    def find_after_both_preflights(connection, sender, key):
        nonlocal calls
        result = original_find(connection, sender, key)
        with lock:
            calls += 1
            synchronize = calls <= 2
        if synchronize:
            barrier.wait(timeout=5)
        return result

    def post_once(content):
        with TestClient(gateway.app) as client:
            return _post(client, content=content)

    gateway._find_idempotent_message = find_after_both_preflights
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post_once, ("first", "second")))

    assert sorted(response.status_code for response in responses) == [200, 409]
    with sqlite3.connect(gateway.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM claude_messages").fetchone()[0] == 1


def test_idempotency_key_is_dm_only(client):
    response = client.post(
        "/messages",
        json={
            "from_node": "service-peer",
            "channel": "releases",
            "kind": "answer",
            "content": "reply",
            "idempotency_key": "delivery-key",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400


def test_existing_gateway_db_gets_the_idempotency_column(tmp_path, monkeypatch):
    import swarph_cli.gateway.server as server

    db_path = tmp_path / "legacy-mesh.db"
    schema = Path(server.SCHEMA_PATH).read_text(encoding="utf-8")
    legacy_schema = schema.replace("  idempotency_sender TEXT,               -- authenticated/effective sender scope\n", "")
    legacy_schema = legacy_schema.replace("  idempotency_key TEXT,                  -- optional client request key\n", "")
    legacy_schema = legacy_schema.replace("  idempotency_digest TEXT,               -- immutable request-intent digest\n", "")
    legacy_schema = legacy_schema.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_sender_idempotency\n"
        "  ON claude_messages(idempotency_sender, idempotency_key)\n"
        "  WHERE idempotency_sender IS NOT NULL AND idempotency_key IS NOT NULL;\n",
        "",
    )
    with sqlite3.connect(db_path) as db:
        db.executescript(legacy_schema)

    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("MESH_DB_PATH", str(db_path))
    server = importlib.reload(server)

    with sqlite3.connect(server.DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(claude_messages)")}
        indexes = {row[1] for row in db.execute("PRAGMA index_list(claude_messages)")}
    assert {"idempotency_sender", "idempotency_key", "idempotency_digest"} <= columns
    assert "idx_messages_sender_idempotency" in indexes
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        first = _post(client)
        second = _post(client)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
