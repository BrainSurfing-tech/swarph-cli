"""One pin for the client reply bound, found in plan-review 5507 (card 1615, swarph-cli PR 742).

Offered to the builder as-is; it uses the PR's own capped_proxy fixture (the real Proxy.handle).
U+007F (DEL) is one byte in the gateway reply and six in the 0.75.0 proxy's ASCII re-encoding, so it is
the case that needs the factor 6. Bounds of 2x, 3x and 5x pass every test in the PR; this one fails them.
"""
import json
import sys

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("local proxy is POSIX-only", allow_module_level=True)

from swarph_cli import local_proxy
from swarph_cli.commands import mcp_server as m
from test_1614_proxy_inbox import capped_proxy, messages  # noqa: F401
from test_1611_mcp_proxy import proxy  # noqa: F401


def test_a_full_gateway_reply_of_del_characters_fits_the_client_bound(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(6)
    for row in ctx.messages[:5]:
        row["content"] = "\x7f" * 200_000  # the gateway's own limit per DM
    ctx.messages[5]["content"] = "\x7f"
    body = {"messages": sorted(ctx.messages, key=lambda r: r["id"], reverse=True), "n": 6}
    short = local_proxy.MAX_LINE - len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    assert 0 < short < 200_000
    ctx.messages[5]["content"] = "\x7f" * (1 + short)
    result = m._dm_unread()
    assert "error" not in result, result
    assert result["n"] == 6
    assert max(ctx.response_sizes) == local_proxy.MAX_LINE  # the largest reply the proxy accepts
    lines = [len(json.dumps(reply).encode()) + 1 for _, reply in ctx.wire if reply.get("ok")]
    assert 5 * local_proxy.MAX_LINE < max(lines) <= local_proxy.MAX_REPLY_LINE


def test_near_pure_del_gateway_object_needs_the_256_envelope_margin(tmp_path):
    import os
    import socket
    import threading
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = {"a": "\x7f" * (local_proxy.MAX_LINE - 8)}
    raw_gateway = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw_gateway) == local_proxy.MAX_LINE

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/whoami"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw_gateway)))
            self.end_headers()
            self.wfile.write(raw_gateway)

    wire_replies = []

    class RecordingProxy(local_proxy.Proxy):
        def request(self, request, **kwargs):
            result = super().request(request, **kwargs)
            wire_replies.append(result)
            return result

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = tmp_path.resolve().parent / ("p6-" + uuid.uuid4().hex[:8])
    config = local_proxy.Config(
        "opencode",
        os.getuid(),
        f"http://127.0.0.1:{server.server_port}",
        tmp_path / "peer_token",
        endpoint,
        tmp_path / "audit",
        allow_loopback_tcp=True,
    )
    real = RecordingProxy(config, "fixture-cap-sentinel")
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
                reply = local_proxy.call(str(endpoint), "whoami", {})
            finally:
                stop.set()
                worker.join(3)
                assert not worker.is_alive()
                endpoint.unlink(missing_ok=True)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(3)

    assert reply.get("ok") is True, reply
    assert reply["body"] == payload
    assert len(wire_replies) == 1
    wire_len = len(json.dumps(wire_replies[0], ensure_ascii=True).encode()) + 1
    assert wire_len == len(json.dumps(reply, ensure_ascii=True).encode()) + 1
    assert wire_len == 6_291_509 == 6 * local_proxy.MAX_LINE + 53
    assert 6 * local_proxy.MAX_LINE < wire_len <= local_proxy.MAX_REPLY_LINE
