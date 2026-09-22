"""Card #735: the DM MCP tools are a CHEAP COMPLETE read.

Every unread body comes back byte-identical in ONE call, in a lean schema that
is measurably smaller than the `mesh inbox --peek --json` window of the same
messages; no cursor, no separate ack; replying is the ack.

CAN-FAIL, executed not described: set SWARPH_735_MUTATE=truncate (bodies cut to
160 chars + an ellipsis, the `--peek` defect rebuilt) or =cursor (half the batch
plus a `next_cursor`) and this file goes RED.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest

from swarph_cli.commands import mcp_server

MUTATE = os.environ.get("SWARPH_735_MUTATE", "")

LONG = ("STATUS 2026-09-05 21:22Z: three cards built-and-unmoved (#372 refused: the accept "
        "asks for a live can-fail I cannot run from this seat), a question about #139, and "
        "a note that the smoke test was green only because the fixture was stale.\n" * 6)


def _gateway_shaped(i: int, body: str, thread: str | None = None) -> dict:
    return {"id": 32000 + i, "from_node": "cursor-lin", "to_node": "lab-ovh",
            "kind": "status", "created_at": f"2026-09-05T21:{i:02d}:00+00:00",
            "thread_id": thread, "content": body, "read_at": None, "channel": None,
            "mentions": [], "cc": [], "priority": None, "related_task_id": None}


FIXTURE = [
    _gateway_shaped(1, LONG),
    _gateway_shaped(2, "short ack, no pad — acked 32124, holding"),
    _gateway_shaped(3, "unicode ✓ « » — and a trailing ellipsis that is REAL…"),
    _gateway_shaped(4, "in a thread", thread="81570cab-0000-4000-8000-000000000001"),
    _gateway_shaped(5, "x" * 5000),
]


@pytest.fixture(autouse=True)
def _session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_dm_session", lambda self_name=None: ("lab-ovh", "http://gw", "tok"))
    if MUTATE == "truncate":
        real = mcp_server._lean

        def cut(m):
            out = real(m)
            if len(out["content"]) > 160:
                out["content"] = out["content"][:160] + "…"
            return out
        monkeypatch.setattr(mcp_server, "_lean", cut)
    if MUTATE == "cursor":
        real_unread = mcp_server._dm_unread

        def paged(self_name=None):
            out = real_unread(self_name)
            half = out["messages"][: max(1, len(out["messages"]) // 2)]
            out.update(messages=half, n=len(half), next_cursor=half[-1]["id"] if half else None)
            return out
        monkeypatch.setattr(mcp_server, "_dm_unread", paged)


def _serve(payload_by_url: dict):
    def fake_get(url, token, **kw):
        for key, payload in payload_by_url.items():
            if key in url:
                return 200, payload
        return 404, {"detail": f"no fixture for {url}"}
    return fake_get


def _assert_complete(result: dict, source: list[dict]):
    """The accept's check: every source body, in full, byte-equal, once; no cursor."""
    assert "error" not in result, result
    got = {m["id"]: m for m in result["messages"]}
    assert set(got) == {m["id"] for m in source}, (set(got), {m["id"] for m in source})
    for src in source:
        body = got[src["id"]]["content"]
        assert body == src["content"], (
            f"message {src['id']} body differs from the source: got {len(body)} chars, "
            f"source {len(src['content'])} chars; tail={body[-8:]!r}")
        assert body.encode("utf-8") == src["content"].encode("utf-8")
    assert result["n"] == len(source) and result["not_returned"] == 0
    assert not {k for k in result if "cursor" in k or k in ("next", "previous", "close")}, result.keys()


def test_unread_returns_every_body_in_full_in_one_call(monkeypatch):
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"/messages?to=lab-ovh&unread_only=true": {"messages": list(reversed(FIXTURE)), "n": 5}}))
    _assert_complete(mcp_server._dm_unread(), FIXTURE)


def test_lean_schema_drops_null_noise_but_never_content(monkeypatch):
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"unread_only=true": {"messages": FIXTURE, "n": 5}}))
    out = mcp_server._dm_unread()
    for m in out["messages"]:
        assert "content" in m and not {k for k, v in m.items() if v in (None, "", [], {})}, m
        assert set(m) <= set(mcp_server.DM_FIELDS)
    assert [m["id"] for m in out["messages"]] == sorted(m["id"] for m in FIXTURE)  # oldest first


def test_cost_below_the_json_window_of_the_same_messages(monkeypatch):
    raw = {"messages": FIXTURE, "n": len(FIXTURE)}
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"unread_only=true": raw}))
    lean = json.dumps(mcp_server._dm_unread(), ensure_ascii=False)
    window = json.dumps(raw, indent=2, sort_keys=True)          # exactly what `mesh inbox --peek --json` prints
    content = sum(len(m["content"].encode()) for m in FIXTURE)
    assert len(lean.encode()) < len(window.encode()), (len(lean.encode()), len(window.encode()))
    assert len(lean.encode()) > content, "content bytes are irreducible; a smaller payload dropped some"


def test_ceiling_is_loud_and_never_shortens(monkeypatch):
    many = [_gateway_shaped(i, f"body {i} " + "y" * 300) for i in range(1, 151)]
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"unread_only=true": {"messages": list(reversed(many)), "n": 150}}))
    out = mcp_server._dm_unread()
    assert out["n"] == mcp_server.DM_CEILING == 100 and out["not_returned"] == 50 and out["unread_seen"] == 150
    assert [m["id"] for m in out["messages"]] == [m["id"] for m in many[:100]]      # the OLDEST, in full
    assert all(m["content"] == src["content"] for m, src in zip(out["messages"], many[:100]))


def test_gateway_error_is_loud_not_an_empty_inbox(monkeypatch):
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", lambda url, tok, **kw: (401, {"detail": "bad token"}))
    out = mcp_server._dm_unread()
    assert out["error"].startswith("gateway 401") and out["n"] == 0


def test_reply_is_the_ack_and_only_that_message(monkeypatch):
    posts = []
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"to_node=lab-ovh": {"messages": FIXTURE, "n": 5}}))

    def fake_post(url, body, token, **kw):
        posts.append((url, body))
        if url.endswith("/messages"):
            return 200, {"id": 99001, "thread_id": body.get("thread_id"), "closed_obligations": []}
        return 200, {}
    monkeypatch.setattr(mcp_server._mesh, "_post_json", fake_post)
    out = mcp_server._dm_reply(32004, "on it — thread 81570cab", kind="answer")
    assert out["id"] == 99001 and out["acked"] == 32004 and out["to_node"] == "cursor-lin"
    assert posts[0][0] == "http://gw/messages" and posts[0][1] == {
        "from_node": "lab-ovh", "to_node": "cursor-lin", "kind": "answer",
        "content": "on it — thread 81570cab", "thread_id": "81570cab-0000-4000-8000-000000000001"}
    reads = [u for u, _ in posts if u.endswith("/read")]
    assert reads == ["http://gw/messages/32004/read"], reads        # exactly one ack, the replied-to one
    assert mcp_server._dm_reply(32004, "   ")["error"] == "empty reply text"


def test_thread_returns_the_conversation_in_full(monkeypatch):
    convo = [_gateway_shaped(4, "in a thread", thread="81570cab-0000-4000-8000-000000000001"),
             {**_gateway_shaped(6, "reply " + "z" * 900, thread="81570cab-0000-4000-8000-000000000001"),
              "from_node": "lab-ovh", "to_node": "cursor-lin"}]
    monkeypatch.setattr(mcp_server._mesh, "_http_get_json", _serve({"thread_id=81570cab": {"messages": list(reversed(convo)), "n": 2},
                                                                     "to_node=lab-ovh": {"messages": FIXTURE, "n": 5}}))
    out = mcp_server._dm_thread(32004)
    assert out["thread_id"] == "81570cab-0000-4000-8000-000000000001" and out["n"] == 2
    assert [m["content"] for m in out["messages"]] == [m["content"] for m in convo]
    single = mcp_server._dm_thread(32002)
    assert single["thread_id"] is None and single["n"] == 1 and single["messages"][0]["content"] == FIXTURE[1]["content"]


def test_no_cursor_no_ack_verbs_on_the_tool_surface():
    if mcp_server.mcp is None:
        pytest.skip("mcp SDK extra not installed")
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"swarph_dm_unread", "swarph_dm_reply", "swarph_dm_thread"} <= names
    forbidden = {n for n in names if n.startswith("swarph_dm_") and any(w in n for w in ("next", "previous", "close", "ack", "cursor", "page"))}
    assert not forbidden, forbidden
    for fn in (mcp_server._dm_unread, mcp_server._dm_reply, mcp_server._dm_thread):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"cursor", "next", "previous", "page", "offset", "since_id", "after"}, (fn.__name__, params)
