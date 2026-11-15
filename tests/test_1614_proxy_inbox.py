"""Bounded MCP inbox behavior through the real 1 MiB local proxy, not a fake cap."""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("local proxy is POSIX-only", allow_module_level=True)

from swarph_cli import local_proxy
from swarph_cli.commands import mcp_server as m
from test_1611_mcp_proxy import V075, proxy  # shared raising credential/HTTP/file guards


def messages(n, *, read=False, size=1400):
    return [{"id": i, "from_node": "sender", "to_node": "opencode", "kind": "fyi",
             "content": "x" * size, "thread_id": "conversation",
             "read_at": "2026-11-01T00:00:00Z" if read else None,
             "created_at": "2026-11-01T00:00:00Z"} for i in range(1, n + 1)]


@pytest.fixture
def capped_proxy(proxy, monkeypatch, tmp_path):
    ctx = SimpleNamespace(messages=[], wire=[], upstream=[], sent=[], marked=[],
                          failures={}, writes=[], response_sizes=[])
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            args = parse_qs(url.query)
            ctx.upstream.append((url.path, args))
            status = ctx.failures.get(("GET", url.path))
            if status:
                return self.answer({"detail": "fixture failure"}, status)
            if url.path == "/whoami":
                body = {"peer": "opencode"}
            else:
                rows = ctx.messages
                if args.get("unread_only") == ["true"]:
                    rows = [r for r in rows if r["read_at"] is None]
                rows = sorted(rows, key=lambda r: r["id"], reverse=True)[:int(args["limit"][0])]
                body = {"messages": rows, "n": len(rows)}
            self.answer(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ctx.writes.append(self.path)
            status = ctx.failures.get(("POST", self.path))
            if status:
                return self.answer({"detail": "fixture failure"}, status)
            if self.path == "/messages":
                ctx.sent.append(body)
                self.answer({"id": 1001, "thread_id": None})
            else:
                ctx.marked.extend(body["ids"])
                self.answer({"ids": body["ids"]})

        def answer(self, body, status=200):
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            ctx.response_sizes.append(len(raw))
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the actual proxy rejects Content-Length before reading a large body

    class RecordingProxy(local_proxy.Proxy):
        def request(self, request, **kwargs):
            result = super().request(request, **kwargs)
            ctx.wire.append((request, result))
            return result

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = tmp_path.resolve().parent / ("p4-" + uuid.uuid4().hex[:8])
    config = local_proxy.Config("opencode", os.getuid(), f"http://127.0.0.1:{server.server_port}",
                                tmp_path / "peer_token", endpoint, tmp_path / "audit",
                                allow_loopback_tcp=True)
    real = RecordingProxy(config, "fixture-cap-sentinel")
    monkeypatch.setenv("SWARPH_PROXY_SOCKET", str(endpoint))
    stop = threading.Event()
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(endpoint))
            listener.listen()
            listener.settimeout(0.1)
            def serve():
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    real.handle(connection)
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield ctx
            finally:
                stop.set()
                worker.join(3)
                endpoint.unlink(missing_ok=True)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(3)
    assert local_proxy.MAX_LINE == 1_048_576
    for request, _ in ctx.wire:
        verb, args = request["verb"], request["args"]
        assert verb in V075 and V075[verb][0] <= args.keys() <= V075[verb][0] | V075[verb][1]


@pytest.mark.parametrize("tool", ("reply", "thread"))
def test_original_is_found_in_unread_without_over_cap_read_history(capped_proxy, tool):
    ctx = capped_proxy
    ctx.messages = messages(1000, read=True)
    ctx.messages[-1]["read_at"] = None
    assert len(json.dumps({"messages": ctx.messages}).encode()) > local_proxy.MAX_LINE
    result = m._dm_reply(1000, "answer") if tool == "reply" else m._dm_thread(1000)
    assert "error" not in result, result
    inbox = [request["args"] for request, _ in ctx.wire if request["verb"] == "mesh.inbox"]
    assert inbox[0]["unread_only"] is True
    if tool == "reply":
        assert result["acked"] == 1000 and ctx.marked == [1000]
        assert ctx.sent[0]["to_node"] == "sender" and ctx.sent[0]["reply_to"] == 1000
    else:
        assert result["messages"][0]["id"] == 1000


@pytest.mark.parametrize("tool", ("reply", "thread"))
def test_original_lookup_shrinks_an_over_cap_unread_window(capped_proxy, tool):
    ctx = capped_proxy
    ctx.messages = messages(1000)
    result = m._dm_reply(1000, "answer") if tool == "reply" else m._dm_thread(1000)
    assert "error" not in result, result
    attempts = [(r["args"], answer) for r, answer in ctx.wire if r["verb"] == "mesh.inbox"]
    assert attempts[0][1]["error"] == "upstream_error"  # real call_gateway Content-Length check
    assert attempts[-1][0]["limit"] < attempts[0][0]["limit"]
    assert attempts[-1][1]["ok"] is True
    if tool == "reply":
        assert ctx.sent[0]["reply_to"] == 1000 and ctx.marked == [1000]


def test_read_original_falls_back_to_a_smaller_all_message_window(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(1000, read=True)
    result = m._dm_reply(1000, "answer")
    assert "error" not in result, result
    assert ctx.marked == [1000] and len(ctx.sent) == 1
    attempts = [r["args"] for r, _ in ctx.wire if r["verb"] == "mesh.inbox"]
    assert attempts[0]["unread_only"] is True and attempts[-1]["unread_only"] is False
    assert attempts[-1]["limit"] < 1000


@pytest.mark.parametrize("older", (100, 150, 400))
def test_unread_shows_the_new_question_and_exact_omission_count(capped_proxy, older):
    ctx = capped_proxy
    ctx.messages = messages(older + 1, size=300)
    ctx.messages[-1].update(kind="question", content="new question", created_at="2026-11-15T17:59:00Z")
    result = m._dm_unread()
    assert "error" not in result, result
    assert older + 1 in [row["id"] for row in result["messages"]]
    assert result["n"] == 100 and result["not_returned"] == older + 1 - 100
    assert result["total_unknown"] is False and result["window_full"] is False


def test_over_cap_unread_reports_a_bound_not_a_false_exact_count(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(800)
    ctx.messages[-1].update(kind="question", content="new question")
    result = m._dm_unread()
    assert "error" not in result, result
    assert 800 in [row["id"] for row in result["messages"]] and result["n"] == 100
    assert result["total_unknown"] is True and result["window_full"] is True
    assert result["not_returned_at_least"] == 400 and "not_returned" not in result
    assert "unknown" in result["note"].lower()
    assert any(answer.get("error") == "upstream_error" for _, answer in ctx.wire)


def test_an_oversize_single_message_fails_loudly_after_bounded_read_retries(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(1, size=local_proxy.MAX_LINE + 1)
    result = m._dm_reply(1, "answer")
    assert "upstream_error" in result["error"]
    attempts = [r["args"]["limit"] for r, _ in ctx.wire if r["verb"] == "mesh.inbox"]
    assert attempts == [1000, 500, 250, 125, 62, 31, 15, 7, 3, 1]
    assert ctx.sent == [] and ctx.marked == []


def test_full_unread_window_never_claims_an_exact_total(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(1000, size=10)
    result = m._dm_unread()
    assert "error" not in result, result
    assert result["total_unknown"] is True and result["not_returned_at_least"] == 900
    assert "not_returned" not in result
