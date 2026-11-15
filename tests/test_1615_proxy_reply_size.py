"""Real 0.75.0 proxy re-encoding versus the cell-side response line bound."""
import json
import socket
import sys
import threading
import uuid

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("local proxy is POSIX-only", allow_module_level=True)

from swarph_cli import local_proxy
from swarph_cli.commands import mcp_server as m
from test_1614_proxy_inbox import capped_proxy, messages
from test_1611_mcp_proxy import proxy  # inherited raising credential/file/direct-HTTP guards


def invoke(tool, newest):
    if tool == "unread":
        return m._dm_unread()
    if tool == "reply":
        return m._dm_reply(newest, "answer")
    return m._dm_thread(newest)


@pytest.mark.parametrize("tool", ("unread", "reply", "thread"))
def test_one_legal_unicode_dm_survives_proxy_ascii_reencoding(capped_proxy, tool):
    ctx = capped_proxy
    ctx.messages = messages(1)
    ctx.messages[0]["content"] = "—" * 180_000
    result = invoke(tool, 1)
    assert "error" not in result, result
    assert max(ctx.response_sizes) < local_proxy.MAX_LINE
    assert any(len(json.dumps(reply).encode()) > local_proxy.MAX_LINE
               for _, reply in ctx.wire if reply.get("ok"))
    if tool == "reply":
        assert ctx.marked == [1] and len(ctx.sent) == 1
    else:
        assert "—" * 180_000 in result["messages"][0]["content"]


@pytest.mark.parametrize("tool", ("unread", "reply", "thread"))
@pytest.mark.parametrize("status", (500, 401, 429))
def test_gateway_http_failure_is_one_inbox_read_not_a_cap_retry(capped_proxy, tool, status):
    ctx = capped_proxy
    ctx.messages = messages(1)
    ctx.failures[("GET", "/messages")] = status
    result = invoke(tool, 1)
    assert "upstream_error" in result["error"]
    assert len([p for p, _ in ctx.upstream if p == "/messages"]) == 1
    assert ctx.writes == []


def sized_messages(target, *, read):
    rows = messages(700, read=read, size=1150)
    raw = json.dumps({"messages": rows, "n": len(rows)}, ensure_ascii=False, separators=(",", ":")).encode()
    padding = target - len(raw)
    assert padding >= 0
    rows[-1]["content"] += "y" * padding
    assert len(rows[-1]["content"]) <= 200_000
    return rows


@pytest.mark.parametrize("tool", ("unread", "reply", "thread"))
@pytest.mark.parametrize("target", (1_040_000, 1_048_575, 1_048_576))
def test_compact_gateway_band_through_exactly_one_mib_works(capped_proxy, tool, target):
    ctx = capped_proxy
    ctx.messages = sized_messages(target, read=tool != "unread")
    result = invoke(tool, 700)
    assert "error" not in result, result
    assert max(ctx.response_sizes) == target
    assert any(len(json.dumps(reply).encode()) > local_proxy.MAX_LINE
               for _, reply in ctx.wire if reply.get("ok"))
    if tool == "reply":
        assert ctx.marked == [700] and len(ctx.sent) == 1
    else:
        assert 700 in [row["id"] for row in result["messages"]]


@pytest.mark.parametrize("tool", ("unread", "reply", "thread"))
def test_six_hundred_non_ascii_messages_are_not_a_client_cap_failure(capped_proxy, tool):
    ctx = capped_proxy
    ctx.messages = messages(600)
    for row in ctx.messages:
        row["content"] = "—→é " * 100
    result = invoke(tool, 600)
    assert "error" not in result, result
    assert max(ctx.response_sizes) < local_proxy.MAX_LINE
    assert any(len(json.dumps(reply).encode()) > local_proxy.MAX_LINE
               for _, reply in ctx.wire if reply.get("ok"))


@pytest.mark.parametrize("status", (500, 401, 429))
@pytest.mark.parametrize("path", ("/messages", "/messages/read"))
def test_failed_writes_are_never_retried(capped_proxy, path, status):
    ctx = capped_proxy
    ctx.messages = messages(1)
    ctx.failures[("POST", path)] = status
    result = m._dm_reply(1, "answer")
    assert "error" in result
    assert ctx.writes.count(path) == 1
    if path == "/messages":
        assert "/messages/read" not in ctx.writes
    else:
        assert result["reply_sent"] is True and result["acked"] is None


def test_reply_line_remains_bounded_even_for_valid_json(tmp_path):
    assert local_proxy.MAX_LINE < local_proxy.MAX_REPLY_LINE <= 7 * local_proxy.MAX_LINE
    endpoint = tmp_path.resolve().parent / ("p5-" + uuid.uuid4().hex[:8])
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()
        def serve():
            connection, _ = listener.accept()
            with connection:
                with connection.makefile("rb") as stream:
                    request = json.loads(stream.readline())
                reply = {"request_id": request["request_id"], "ok": True,
                         "status": 200, "body": {"padding": ""}}
                overhead = len(json.dumps(reply).encode()) + 1
                reply["body"]["padding"] = "x" * (local_proxy.MAX_REPLY_LINE + 1 - overhead)
                raw = json.dumps(reply).encode() + b"\n"
                assert len(raw) == local_proxy.MAX_REPLY_LINE + 1
                try:
                    connection.sendall(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            assert local_proxy.call(str(endpoint), "whoami", {})["error"] == "proxy_unavailable"
        finally:
            worker.join(3)
            endpoint.unlink(missing_ok=True)
    assert not worker.is_alive()


def test_sixfold_derivation_includes_numeric_and_unicode_expansion():
    examples = (b"1e15", b"[1e15,1e15]", '"é"'.encode(), '"😀"'.encode(),
                b'{"a":0,"b":0}', b'"\\u0000"')
    assert len(json.dumps(json.loads(b"1e15")).encode()) > 3 * len(b"1e15")
    for raw in examples:
        assert len(json.dumps(json.loads(raw), ensure_ascii=True).encode()) <= 6 * len(raw)
    envelope = {"request_id": "0" * 36, "ok": True, "status": 200, "body": None}
    assert len(json.dumps(envelope).encode()) - len(b"null") + 1 < 256


@pytest.mark.skipif(m.mcp is None, reason="optional MCP SDK not installed")
def test_reply_tool_description_names_the_read_original_reach_limit():
    import asyncio
    tool = next(t for t in asyncio.run(m.mcp.list_tools()) if t.name == "swarph_dm_reply")
    assert "read original older than the bounded inbox window" in tool.description
    assert "cannot be found" in tool.description
