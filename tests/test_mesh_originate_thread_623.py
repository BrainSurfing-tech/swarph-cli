"""#623 — originate a card-less thread; refuse self-wake (#185 / #409).

Threading solves retrieval, not the three missing signals. These tests pin
the CLI contract only: originate, join by one-token handle, list, and the
from_node==to_node / from_node-in-cc refusal that fails if removed.
"""
from __future__ import annotations

import uuid

import pytest

from swarph_cli.commands import mesh

HANDLE = "7597a1ba-d6f3-4d45-aa86-75ccec03bcde"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw.test:8788")
    # Recipient registry check must not hit the network.
    monkeypatch.setattr(mesh, "_check_recipient", lambda *a, **k: None)


def test_self_post_is_refused_and_nothing_is_sent(monkeypatch):
    """#409: from_node == to_node must not POST. Removing the refusal fails this."""
    posted = []
    monkeypatch.setattr(mesh, "_post_json", lambda *a, **k: posted.append(a) or (200, {}))
    rc = mesh.run_mesh(["send", "c1", "--kind", "fyi", "--content", "hi", "--as", "c1"])
    assert rc == 1
    assert posted == []


def test_self_in_cc_is_refused(monkeypatch):
    posted = []
    monkeypatch.setattr(mesh, "_post_json", lambda *a, **k: posted.append(1) or (200, {}))
    rc = mesh.run_mesh([
        "send", "c2", "--kind", "fyi", "--content", "hi", "--as", "c1", "--cc", "c1",
    ])
    assert rc == 1
    assert posted == []


def test_originate_returns_one_token_handle(monkeypatch, capsys):
    minted = {"thread_uuid": HANDLE, "created": True}

    def fake(url, body, token, **k):
        if url.endswith("/threads"):
            assert "thread_name" in body
            return (200, minted)
        assert body["thread_id"] == HANDLE
        assert body["from_node"] == "c1"
        assert body["to_node"] == "c2"
        return (200, {"id": 9, "from_node": "c1", "to_node": "c2",
                      "kind": "fyi", "thread_id": HANDLE})

    monkeypatch.setattr(mesh, "_post_json", fake)
    rc = mesh.run_mesh([
        "send", "c2", "--kind", "fyi", "--content", "open", "--thread", "new", "--as", "c1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"thread={HANDLE}" in out
    # One token: the handle pastes unmodified (no quotes, no spaces).
    token = out.strip().split("thread=")[-1].strip()
    assert token == HANDLE
    assert " " not in token
    uuid.UUID(token)


def test_join_existing_handle_posts_that_thread_id(monkeypatch):
    seen = {}

    def fake(url, body, token, **k):
        seen.update(body)
        return (200, {"id": 1, "from_node": "c1", "to_node": "c2",
                      "kind": "fyi", "thread_id": HANDLE})

    monkeypatch.setattr(mesh, "_post_json", fake)
    assert mesh.run_mesh([
        "send", "c2", "--kind", "fyi", "--content", "join",
        "--thread", HANDLE, "--as", "c1",
    ]) == 0
    assert seen["thread_id"] == HANDLE


def test_inbox_thread_prints_ordered_set(monkeypatch, capsys):
    def fake_get(url, token, **k):
        assert f"/threads/{HANDLE}" in url
        return (200, {"thread": {"thread_uuid": HANDLE}, "messages": [
            {"id": 1, "from_node": "c1", "kind": "fyi", "content": "first", "read_at": None},
            {"id": 2, "from_node": "c2", "kind": "answer", "content": "second", "read_at": None},
        ]})

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    rc = mesh.run_mesh(["inbox", "--thread", HANDLE, "--as", "c1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("first") < out.index("second")


def test_threads_lists_gateway_rows(monkeypatch, capsys):
    def fake_get(url, token, **k):
        assert url.endswith("/threads?limit=50") or "/threads?limit=50" in url
        return (200, {"threads": [
            {"thread_uuid": HANDLE, "peer_pair": "c1↔c2", "topic": "design"},
        ]})

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    rc = mesh.run_mesh(["threads", "--as", "c1"])
    assert rc == 0
    assert HANDLE in capsys.readouterr().out


def test_mutant_self_wake_check_removed_fails():
    """The refusal is the whole of #409. A stub that always allows must not pass
    the self-post test's contract: _self_wake_refusal('c1','c1',[]) is not None.
    """
    assert mesh._self_wake_refusal("c1", "c1", []) is not None
    assert mesh._self_wake_refusal("c1", "c2", ["c1"]) is not None
    assert mesh._self_wake_refusal("c1", "c2", []) is None
