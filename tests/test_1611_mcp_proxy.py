"""MCP helper boundary against a real socket speaking the v0.75.0 protocol."""
import builtins
import asyncio
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("local proxy is POSIX-only", allow_module_level=True)

from swarph_cli.commands import mcp_server as m

# Frozen installed v0.75.0 request keys, not the evolving proxy_routes registry.
V075 = {
    "whoami": (set(), set()), "mesh.peers": (set(), {"enabled_only"}),
    "mesh.inbox": (set(), {"limit", "unread_only", "since", "kind"}),
    "mesh.send": ({"to_node", "content"}, {"kind", "reply_to"}),
    "mesh.mark_read": ({"ids"}, set()),
    "memory.get": ({"slug"}, set()),
    "memory.list": (set(), {"type", "tag", "limit"}),
}


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    ctx = SimpleNamespace(calls=[], forbidden=[], messages=[{
        "id": 7, "from_node": "sender", "to_node": "opencode", "kind": "question",
        "content": "whole body " + "x" * 1000, "thread_id": "thread-seven",
        "created_at": "2026-11-15T12:00:00Z",
    }], failures={}, overrides={}, peer="opencode", pages=[{"slug": "page-a", "compiled_truth": "fixture memory"}])
    endpoint = tmp_path.resolve().parent / ("p2-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("SWARPH_PROXY_SOCKET", str(endpoint))
    for name in ("MESH_GATEWAY_URL", "SWARPH_SELF", "SWARPH_CELL", "SWARPH_BRAIN_GATEWAY",
                 "SWARPH_BRAIN_MCP", "GBRAIN_MCP_URL"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        ctx.forbidden.append(True)
        raise RuntimeError("credential/direct transport must not be reached")

    monkeypatch.setattr(m._mesh, "_resolve_token", forbidden)
    monkeypatch.setattr(m.brain_ask, "_resolve_token", forbidden)
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", forbidden)
    monkeypatch.setattr(m, "env_gateway", forbidden)
    monkeypatch.setattr(m._mesh, "_http_get_json", forbidden)
    monkeypatch.setattr(m._mesh, "_post_json", forbidden)
    monkeypatch.setattr(m.memory, "_mcp_call", forbidden)
    monkeypatch.setattr(m.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(m, "_metaedge_token", forbidden)
    for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
        original = getattr(owner, name)
        def guarded(path, *args, _original=original, **kwargs):
            if not isinstance(path, int) and "peer_token" in str(path):
                forbidden()
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(owner, name, guarded)

    stop = threading.Event()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()
        listener.settimeout(0.1)
        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    with conn.makefile("rb") as stream:
                        request = json.loads(stream.readline())
                    ctx.calls.append(request)
                    verb, args = request["verb"], request["args"]
                    valid = (set(request) == {"request_id", "verb", "args"}
                             and verb in V075 and V075[verb][0] <= args.keys()
                             and args.keys() <= (V075[verb][0] | V075[verb][1]))
                    if not valid or verb in ctx.failures:
                        reply = {"request_id": request["request_id"], "ok": False,
                                 "error": ctx.failures.get(verb, "verb_not_allowed")}
                    else:
                        body = {"peer": ctx.peer} if verb == "whoami" else {"messages": ctx.messages}
                        if verb == "mesh.send":
                            body = {"id": 88, "thread_id": None}
                        elif verb == "mesh.mark_read":
                            body = {"updated": len(args["ids"])}
                        elif verb == "memory.get":
                            body = {"result": {"slug": args["slug"], "compiled_truth": "fixture memory"}}
                        elif verb == "memory.list":
                            body = {"result": ctx.pages}
                        reply = {"request_id": request["request_id"], "ok": True,
                                 "status": 200, "body": body}
                    reply.update(ctx.overrides.get(verb, {}))
                    conn.sendall(json.dumps(reply).encode() + b"\n")
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield ctx
        finally:
            stop.set()
            worker.join(2)
            endpoint.unlink(missing_ok=True)
    assert ctx.forbidden == [], "credential/direct path was attempted even if its error was swallowed"
    assert all(c["verb"] in V075 for c in ctx.calls)


def test_unread_uses_proxy_without_gateway_identity_or_credentials(proxy):
    result = m._dm_unread()
    assert "error" not in result, result
    assert result["self"] == "opencode" and result["n"] == 1
    assert "whole body " + "x" * 1000 in result["messages"][0]["content"]
    assert [c["verb"] for c in proxy.calls] == ["whoami", "mesh.inbox"]
    assert proxy.calls[-1]["args"] == {"unread_only": True, "limit": 1000}


@pytest.mark.parametrize("op,kwargs,wanted", (
    ("get", {"slug": "page-a"}, {"slug": "page-a"}),
    ("list", {"type": "reference", "tag": "project", "limit": 4},
     {"type": "reference", "tag": "project", "limit": 4}),
))
def test_memory_reads_use_installed_proxy_verbs_before_any_resolver(proxy, op, kwargs, wanted):
    result = m._memory_navigate(op, **kwargs)
    assert not isinstance(result, dict) or "error" not in result, result
    assert result == (proxy.pages[0] if op == "get" else proxy.pages)
    assert [c["verb"] for c in proxy.calls] == ["memory." + op]
    assert proxy.calls[0]["args"] == wanted


def test_reply_routes_to_original_sender_then_acks_without_thread_id(proxy):
    result = m._dm_reply(7, "reply body")
    assert "error" not in result, result
    assert result["id"] == 88 and result["to_node"] == "sender" and result["acked"] == 7
    assert result["thread_id"] is None
    assert "0.75.0" in result["note"] and "thread" in result["note"]
    assert [c["verb"] for c in proxy.calls] == ["whoami", "mesh.inbox", "mesh.send", "mesh.mark_read"]
    assert proxy.calls[1]["args"] == {"unread_only": False, "limit": 1000}
    assert proxy.calls[2]["args"] == {"to_node": "sender", "content": "reply body", "kind": "answer", "reply_to": 7}
    assert proxy.calls[3]["args"] == {"ids": [7]}


def test_thread_is_explicitly_inbound_only_with_full_framed_bodies(proxy):
    original = proxy.messages[0]
    proxy.messages.extend([
        {**original, "id": 8, "from_node": "opencode", "to_node": "sender", "content": "outbound"},
        {**original, "id": 9, "content": "second inbound"},
        {**original, "id": 10, "thread_id": "other-thread", "content": "other conversation"},
    ])
    result = m._dm_thread(7)
    assert "error" not in result, result
    assert result["inbound_only"] is True and "inbound" in result["note"].lower()
    assert result["thread_id"] == "thread-seven" and result["n"] == 2
    assert [msg["id"] for msg in result["messages"]] == [7, 9]
    assert original["content"] in result["messages"][0]["content"]
    assert [c["verb"] for c in proxy.calls] == ["whoami", "mesh.inbox"]


@pytest.mark.parametrize("op", ("links", "backlinks", "traverse", "unknown"))
def test_unsupported_memory_operations_refuse_before_any_credential_or_rpc(proxy, op):
    assert "proxy mode" in m._memory_navigate(op, slug="page-a")["error"]
    assert proxy.calls == []


def test_empty_memory_list_is_not_confused_with_failure(proxy):
    proxy.pages = []
    assert m._memory_navigate("list") == {"result": []}


def test_search_refuses_before_any_direct_transport(proxy):
    result = m._search("query", url="https://unused.invalid")
    assert isinstance(result, dict) and "proxy mode" in result["error"]
    assert proxy.calls == []


@pytest.mark.skipif(m.mcp is None, reason="optional MCP SDK not installed")
def test_registered_search_refuses_before_even_looking_up_its_env_token(proxy):
    result = asyncio.run(m.mcp.call_tool("swarph_search", {"query": "q"}))
    assert "proxy mode" in str(result)
    assert proxy.calls == []


_ROUTED = (
    (m._dm_unread, ()), (m._dm_reply, (7, "reply")), (m._dm_thread, (7,)),
    (m._memory_navigate, ("get", "page-a")), (m._memory_navigate, ("list",)),
)


@pytest.mark.parametrize("call,args", _ROUTED)
def test_down_proxy_returns_error_without_direct_fallback(proxy, monkeypatch, tmp_path, call, args):
    monkeypatch.setenv("SWARPH_PROXY_SOCKET", str(tmp_path / "missing.sock"))
    result = call(*args)
    assert "proxy mode" in result["error"] and "proxy_unavailable" in result["error"]
    assert proxy.calls == []


@pytest.mark.parametrize("value", ("", "relative.sock", "   "))
@pytest.mark.parametrize("call,args", _ROUTED)
def test_bad_socket_setting_never_selects_direct_credentials(proxy, monkeypatch, value, call, args):
    monkeypatch.setenv("SWARPH_PROXY_SOCKET", value)
    assert "proxy mode" in call(*args)["error"]
    assert proxy.calls == []


@pytest.mark.parametrize("call,args,verb", (
    (m._dm_unread, (), "mesh.inbox"), (m._dm_reply, (7, "reply"), "mesh.send"),
    (m._dm_thread, (7,), "mesh.inbox"),
    (m._memory_navigate, ("get", "page-a"), "memory.get"),
    (m._memory_navigate, ("list",), "memory.list"),
))
def test_proxy_refusal_is_loud_and_never_falls_back(proxy, call, args, verb):
    proxy.failures[verb] = "upstream_ambiguous"
    result = call(*args)
    assert "proxy mode" in result["error"] and "upstream_ambiguous" in result["error"]
    assert sum(c["verb"] == verb for c in proxy.calls) == 1
    if verb == "mesh.send":
        assert not any(c["verb"] == "mesh.mark_read" for c in proxy.calls)


def test_failed_ack_reports_sent_reply_without_retrying_send(proxy):
    proxy.failures["mesh.mark_read"] = "proxy_unavailable"
    result = m._dm_reply(7, "reply")
    assert result["reply_sent"] is True and result["id"] == 88 and result["acked"] is None
    assert "proxy mode" in result["error"] and "reply sent" in result["error"]
    assert [c["verb"] for c in proxy.calls].count("mesh.send") == 1
    assert [c["verb"] for c in proxy.calls].count("mesh.mark_read") == 1


@pytest.mark.parametrize("call,args", _ROUTED[:3])
def test_declared_identity_must_match_proxy_identity(proxy, monkeypatch, call, args):
    monkeypatch.setenv("SWARPH_SELF", "different-cell")
    assert "proxy mode" in call(*args)["error"]
    assert [c["verb"] for c in proxy.calls] == ["whoami"]


@pytest.mark.parametrize("peer", (None, "", 9))
def test_unscoped_proxy_identity_is_not_accepted(proxy, peer):
    proxy.peer = peer
    assert "proxy mode" in m._dm_unread()["error"]
    assert [c["verb"] for c in proxy.calls] == ["whoami"]


def test_unknown_message_never_sends_or_marks_anything(proxy):
    assert "proxy mode" in m._dm_reply(999, "reply")["error"]
    assert [c["verb"] for c in proxy.calls] == ["whoami", "mesh.inbox"]


@pytest.mark.skipif(m.mcp is None, reason="optional MCP SDK not installed")
@pytest.mark.parametrize("name,args,marker", (
    ("swarph_dm_unread", {}, "whole body"),
    ("swarph_dm_reply", {"message_id": 7, "text": "reply"}, "0.75.0"),
    ("swarph_dm_thread", {"message_id": 7}, "inbound"),
    ("swarph_memory_navigate", {"op": "get", "slug": "page-a"}, "fixture memory"),
    ("swarph_memory_navigate", {"op": "list"}, "page-a"),
))
def test_registered_tools_reach_the_same_credential_free_proxy_seam(proxy, name, args, marker):
    result = asyncio.run(m.mcp.call_tool(name, args))
    assert marker in str(result)


@pytest.mark.skipif(m.mcp is None, reason="optional MCP SDK not installed")
def test_every_registered_tool_is_in_the_auth_inventory():
    assert {t.name for t in asyncio.run(m.mcp.list_tools())} == {
        "swarph_search", "swarph_add", "swarph_describe", "swarph_codegraph_query",
        "swarph_memory_navigate", "swarph_timeline_navigate", "swarph_dm_unread",
        "swarph_dm_reply", "swarph_dm_thread",
    }


@pytest.mark.parametrize("override", (
    {"request_id": "wrong-request"}, {"status": True}, {"status": 503},
    {"ok": 1}, {"body": None},
))
def test_invalid_proxy_envelope_never_becomes_a_success_or_fallback(proxy, override):
    proxy.overrides["whoami"] = override
    assert "proxy mode" in m._dm_unread()["error"]
    assert [c["verb"] for c in proxy.calls] == ["whoami"]


def test_root_proxy_identity_is_refused(proxy):
    proxy.overrides["whoami"] = {"body": {"peer": "opencode", "is_root": True}}
    assert "proxy mode" in m._dm_unread()["error"]
    assert [c["verb"] for c in proxy.calls] == ["whoami"]


def test_direct_session_is_refused_before_any_resolver(proxy):
    with pytest.raises(RuntimeError, match="proxy mode"):
        m._dm_session()
    assert proxy.calls == []


@pytest.mark.parametrize("declared", ("opencode", " opencode "))
def test_matching_declared_identity_is_accepted(proxy, monkeypatch, declared):
    monkeypatch.setenv("SWARPH_SELF", declared)
    assert m._dm_unread()["self"] == "opencode"


def test_ambient_cell_cannot_override_the_proxy_peer(proxy, monkeypatch):
    monkeypatch.setenv("SWARPH_CELL", "leaked-cell")
    assert m._dm_unread()["self"] == "opencode"


@pytest.mark.parametrize("message_id", (0, -1, True))
def test_invalid_message_id_refuses_before_rpc(proxy, message_id):
    assert "proxy mode" in m._dm_reply(message_id, "reply")["error"]
    assert proxy.calls == []


def test_unsupported_wire_verb_is_refused_locally(proxy):
    with pytest.raises(RuntimeError, match="proxy mode"):
        m._proxy_request("memory.links", {"slug": "page-a"})
    assert proxy.calls == []
