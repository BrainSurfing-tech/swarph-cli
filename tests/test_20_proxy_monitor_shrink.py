"""Card #1617: proxy-mode monitor inbox poll shrink on the 1 MiB cap.

Through the real Proxy.call_gateway cap (stand-in gateway, escaped JSON so one
180k U+0001 DM is >1 MiB on the wire) and the real _private_transport +
_monitor_iteration. Direct mode is untouched: the shrink is driven only by the
typed cap marker the proxy transport adds.
"""
import sys

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("proxy monitor is POSIX-only", allow_module_level=True)

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from swarph_cli import proxy_monitor
from swarph_cli.commands import mesh
from swarph_cli.local_proxy import Config, Proxy

BIG = "\x01" * 180_000


def _msg(i, text=None):
    return {"id": i, "from_node": "bob", "to_node": "alice", "kind": "fyi",
            "content": text if text is not None else f"message {i}"}


@pytest.fixture
def gateway():
    calls = []
    state = SimpleNamespace(messages=[], fail_status=None)

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            parts = urlsplit(self.path)
            if parts.path == "/messages" and state.fail_status:
                raw = json.dumps({"detail": "boom"}).encode()
                self.send_response(state.fail_status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parts.path == "/messages":
                limit = int(parse_qs(parts.query).get("limit", ["50"])[0])
                rows = sorted(state.messages, key=lambda m: m["id"], reverse=True)[:limit]
                body = {"messages": rows, "n": len(rows)}
            else:
                body = {}
            raw = json.dumps(body, ensure_ascii=True).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}", calls=calls, state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _drive(tmp_path, gateway):
    config = Config("alice", os.getuid() + 1234, gateway.url,
                    tmp_path / "unused-token", tmp_path / "unused.sock",
                    tmp_path / "audit", allow_loopback_tcp=True)
    state_dir = tmp_path / "state"
    state = mesh.MonitorState(
        self_name="alice", state_dir=state_dir, gateway=gateway.url,
        token="fixture-token", sinks=[mesh.parse_sink("pull")])
    with proxy_monitor._private_transport(Proxy(config, "fixture-token")):
        mesh._monitor_iteration(state)
    return state


def _inbox_ids(state):
    path = state.inbox_log_path
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()]


def _message_calls(gateway):
    return [c for c in gateway.calls if c.startswith("/messages?")]


@pytest.mark.parametrize("newer", [10, 49])
def test_shrink_delivers_newer_dms_around_oversized_row(tmp_path, gateway, capsys, newer):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)] + [
        _msg(i) for i in range(4, 4 + newer)]
    state = _drive(tmp_path, gateway)
    assert _inbox_ids(state) == list(range(4, 4 + newer))
    err = capsys.readouterr().err
    assert "limit=50" in err and "retrying" in err  # each shrink is logged
    limits = [int(parse_qs(urlsplit(c).query)["limit"][0]) for c in _message_calls(gateway)]
    assert limits[0] == 50 and len(limits) > 1 and limits[-1] < 50


def test_oversized_newest_row_writes_distinct_blocked_state(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _drive(tmp_path, gateway)
    blocked = state.state_dir / "inbox_blocked.json"
    assert blocked.exists()
    payload = json.loads(blocked.read_text())
    assert payload["state"] == "inbox_blocked_oversized_newest"
    assert payload["limit"] == 1
    err = capsys.readouterr().err
    assert "inbox_blocked_oversized_newest" in err
    assert "gateway 502" not in err
    # A second poll in the same episode does not re-log the blocked line.
    with proxy_monitor._private_transport(Proxy(
            Config("alice", os.getuid() + 1234, gateway.url, tmp_path / "t",
                   tmp_path / "s", tmp_path / "a", allow_loopback_tcp=True),
            "fixture-token")):
        mesh._monitor_iteration(state)
    err2 = capsys.readouterr().err
    assert "inbox_blocked_oversized_newest" not in err2
    assert "gateway 502" not in err2


def test_blocked_state_surfaces_in_monitor_status(tmp_path, gateway):
    from swarph_cli.commands import monitor
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _drive(tmp_path, gateway)
    info = monitor._collect(SimpleNamespace(
        self_name="alice", state_dir=str(state.state_dir)))
    assert info["inbox_blocked"]["state"] == "inbox_blocked_oversized_newest"


def test_gateway_500_is_not_retried_with_smaller_window(tmp_path, gateway, capsys):
    gateway.state.fail_status = 500
    gateway.state.messages = [_msg(1)]
    state = _drive(tmp_path, gateway)
    assert len(_message_calls(gateway)) == 1
    err = capsys.readouterr().err
    assert "gateway 500" in err
    assert "retrying" not in err
    assert not (state.state_dir / "inbox_blocked.json").exists()
