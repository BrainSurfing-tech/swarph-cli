"""#356 — unrecognized query filters fail CLOSED, never OPEN."""

from __future__ import annotations

import importlib

import pytest

from swarph_cli.query_filters import (
    MESSAGES_GET_PARAMS,
    UnknownQueryFilter,
    refuse_unknown_query_on_url,
    refuse_unknown_query_params,
    unknown_query_params,
)


def test_unread_is_not_unread_only():
    """Specimen: drop passed unread=1; the gateway binds unread_only."""
    extra = unknown_query_params({"unread", "limit"}, MESSAGES_GET_PARAMS)
    assert extra == frozenset({"unread"})
    assert unknown_query_params({"unread_only", "limit"}, MESSAGES_GET_PARAMS) == frozenset()


def test_refuse_names_the_unknown_key():
    with pytest.raises(UnknownQueryFilter, match="unread") as exc:
        refuse_unknown_query_params(
            ["unread", "to"], MESSAGES_GET_PARAMS, endpoint="/messages",
        )
    assert "unfiltered superset" in str(exc.value)


def test_url_unread_refused_unread_only_accepted():
    with pytest.raises(UnknownQueryFilter, match=r"\['unread'\]"):
        refuse_unknown_query_on_url(
            "http://gw:8788/messages?to=drop&unread=1",
        )
    refuse_unknown_query_on_url(
        "http://gw:8788/messages?to=drop&unread_only=true&limit=20",
    )


def test_http_get_json_returns_400_without_calling_the_network(monkeypatch):
    from swarph_cli.commands import mesh

    def boom(*_a, **_k):
        raise AssertionError("must not hit the network on an unknown filter")

    monkeypatch.setattr(mesh.urllib.request, "urlopen", boom)
    status, body = mesh._http_get_json(
        "http://gw:8788/messages?unread=1", "tok",
    )
    assert status == 400
    assert "unread" in body["detail"]


@pytest.fixture
def gateway_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("jwt")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("MESH_DB_PATH", str(tmp_path / "mesh.db"))
    import swarph_cli.gateway.server as server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def test_gateway_400s_unread_and_accepts_unread_only(gateway_client):
    headers = {"Authorization": "Bearer test-token"}
    bad = gateway_client.get("/messages?to=drop&unread=1", headers=headers)
    assert bad.status_code == 400, bad.text
    assert "unread" in bad.json()["detail"]
    ok = gateway_client.get("/messages?to=drop&unread_only=true", headers=headers)
    assert ok.status_code == 200, ok.text
    assert "messages" in ok.json()
