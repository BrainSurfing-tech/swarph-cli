"""Card #1702: join refuses onboard aliases, and a failed handshake still names onboard."""
from __future__ import annotations

import os
import sys

import pytest

from swarph_cli.commands.join import run_join
from swarph_shared.peer_registry import KNOWN_ALIASES

_DEVICE = "D" * 43
_TOKEN = "T" * 43


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.mark.parametrize("name", list(KNOWN_ALIASES) + ["new-cell"])
def test_join_refuses_onboard_aliases_before_any_request(name, monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args)
        return 500, {}, None

    monkeypatch.setattr("swarph_cli.commands.join._request", fake_request)
    rc = run_join(["http://100.64.0.1:8788", "--name", name])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    mapped = KNOWN_ALIASES.get(name)
    if isinstance(mapped, str) and mapped != name:
        assert rc == 1
        assert calls == []
        if os.name == "nt":
            assert "native Windows" in captured.err
            assert "onboard alias" not in text
        else:
            assert name in captured.err
            assert mapped in captured.err
            assert "onboard alias" in captured.err
            assert "choose another name" in captured.err
    else:
        assert "onboard alias" not in text
        if os.name != "nt":
            assert calls
    assert _TOKEN not in text


def test_alias_added_at_run_time_is_refused_before_any_request(monkeypatch, tmp_path, capsys):
    if os.name == "nt":
        pytest.skip("native Windows refusal comes before the alias check")
    import swarph_shared.peer_registry as registry
    _home(monkeypatch, tmp_path)
    monkeypatch.setitem(registry.KNOWN_ALIASES, "extra-peer", "other-peer")
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args)
        return 500, {}, None

    monkeypatch.setattr("swarph_cli.commands.join._request", fake_request)
    rc = run_join(["http://100.64.0.1:8788", "--name", "extra-peer"])
    captured = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert "extra-peer is an onboard alias for other-peer" in captured.err
    assert "choose another name" in captured.err
    assert _TOKEN not in captured.out + captured.err


def _handshake_stub(mode):
    """A loopback gateway. The handshake reply is not JSON, or the socket closes."""
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            self._reply()

        def do_GET(self):
            self._reply()

        def _reply(self):
            if self.path == "/messages":
                if mode == "close":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                body = {"plain": b"nope", "html": b"<html>bad</html>", "empty": b""}[mode]
                status = {"plain": 500, "html": 502, "empty": 503}[mode]
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                return
            bodies = {
                "/join/requests": {"request_id": "fixture", "device_code": _DEVICE,
                                   "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5},
                "/join/token": {"name": "new-cell", "peer_token": _TOKEN,
                                "witness": "fixture-witness", "registered_unratified": True},
                "/whoami": {"peer": "new-cell"},
            }
            payload = json.dumps(bodies[self.path]).encode()
            status = 201 if self.path == "/join/requests" else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
@pytest.mark.parametrize("mode", ["plain", "html", "empty", "close"])
def test_handshake_failure_prints_the_onboard_line(mode, monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["north", "east", "south", "west"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    server = _handshake_stub(mode)
    host, port = server.server_address
    try:
        rc = run_join([f"http://{host}:{port}", "--name", "new-cell", "--allow-unsafe-http"])
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    token_path = tmp_path / ".config" / "swarph" / "new-cell.peer_token"
    line = f"swarph onboard new-cell --gateway http://{host}:{port} --token-file {token_path}"
    assert rc == 1
    assert "Next, on this machine:" in captured.out
    assert line in captured.out
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err
    assert token_path.is_file()
