"""Real CLI entry point against a fixture gateway; no live token or service."""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

DEVICE = "D" * 43
TOKEN = "T" * 43


@pytest.fixture
def gateway():
    calls = []
    responses = {}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, json.loads(data) if data else None,
                          self.headers.get("Authorization")))
            body = responses.get(self.path) or {
                "/join/requests": {"request_id": "fixture-request", "device_code": DEVICE,
                                    "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5},
                "/join/token": {"name": "new-peer", "peer_token": TOKEN,
                                 "witness": "fixture-witness", "registered_unratified": True},
                "/whoami": {"peer": "new-peer"},
                "/messages": {"id": 1},
                "/join/requests/ABCD-EFGH": {"name": "new-peer", "source": "local socket",
                                            "expires_at": 1800000900, "state": "pending"},
                "/join/requests/ABCD-EFGH/decision": {"state": "approved"},
            }.get(self.path)
            if isinstance(body, deque):
                body = body.popleft()
            payload = json.dumps(body or {"error": "unknown"}).encode()
            status = 201 if self.path == "/join/requests" else 200 if body else 404
            if isinstance(body, tuple):
                status, body = body
                payload = json.dumps(body).encode()
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/should-not-follow")
            if status == 429:
                self.send_header("Retry-After", "7")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = handle_request

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_cli(tmp_path, *args, input_text=""):
    env = {**os.environ, "HOME": str(tmp_path), "SWARPH_SELF": "wrong-ambient-cell"}
    return subprocess.run([sys.executable, "-m", "swarph_cli.main", *args], env=env,
                          input=input_text, text=True, capture_output=True, timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_join_delivers_token_to_private_file_not_output_or_operator(tmp_path, gateway):
    url, calls, _ = gateway
    result = run_cli(tmp_path, "join", url, "--name", "new-peer", "--allow-unsafe-http")
    assert result.returncode == 0, result.stderr
    path = tmp_path / ".config/swarph/new-peer.peer_token"
    assert path.read_text().strip() == TOKEN
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert TOKEN not in result.stdout + result.stderr
    assert DEVICE not in result.stdout + result.stderr
    assert "ABCD-EFGH" in result.stdout
    assert [(method, path, body) for method, path, body, _ in calls] == [
        ("POST", "/join/requests", {"name": "new-peer"}),
        ("POST", "/join/token", {"device_code": DEVICE}), ("GET", "/whoami", None)]
    assert [header for _, _, _, header in calls] == [None, None, "Bearer " + TOKEN]
    saved = json.loads((path.parent / "new-peer.join.json").read_text())
    assert saved == {"name": "new-peer", "gateway": url, "token_file": str(path)}
    assert "ratification" in result.stdout.lower()


def test_approve_previews_then_decides_without_receiving_new_token(tmp_path, gateway):
    url, calls, _ = gateway
    operator_file = tmp_path / "fixture-operator-token"
    operator_file.write_text("fixture-root-credential")
    result = run_cli(tmp_path, "mesh", "approve", "ABCD-EFGH", "--witness", "fixture-witness",
                     "--gateway", url, "--token-file", str(operator_file), "--allow-unsafe-http",
                     input_text="yes\n")
    assert result.returncode == 0, result.stderr
    assert calls == [("GET", "/join/requests/ABCD-EFGH", None, "Bearer fixture-root-credential"),
                     ("POST", "/join/requests/ABCD-EFGH/decision",
                      {"decision": "approve", "witness": "fixture-witness"}, "Bearer fixture-root-credential")]
    assert TOKEN not in result.stdout + result.stderr
    assert DEVICE not in result.stdout + result.stderr
    assert "fixture-root-credential" not in result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_existing_token_is_never_overwritten_or_sent_upstream(tmp_path, gateway):
    url, calls, _ = gateway
    path = tmp_path / ".config/swarph/new-peer.peer_token"
    path.parent.mkdir(parents=True)
    path.write_text("old-fixture-credential")
    result = run_cli(tmp_path, "join", url, "--name", "new-peer", "--allow-unsafe-http")
    assert result.returncode == 1
    assert path.read_text() == "old-fixture-credential"
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage; Windows is refused first")
@pytest.mark.parametrize("host", ["100.63.255.255", "100.128.0.1", "evil.ts.net.example", "ts.net", "127.0.0.1", "localhost"])
def test_non_tailnet_http_refused_without_unsafe_acknowledgement(tmp_path, gateway, host):
    url, calls, _ = gateway
    port = url.rsplit(":", 1)[1]
    result = run_cli(tmp_path, "join", f"http://{host}:{port}", "--name", "new-peer")
    assert result.returncode == 1
    assert "--allow-unsafe-http" in result.stderr
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
@pytest.mark.parametrize("host", ["100.64.0.1", "100.127.255.254", "peer.example.ts.net", "peer.ts.net."])
def test_tailnet_http_needs_no_friction_flag(tmp_path, gateway, monkeypatch, capsys, host):
    from swarph_cli.commands.join import run_join
    url, calls, _ = gateway
    port = int(url.rsplit(":", 1)[1])
    connect = socket.create_connection
    # Only the socket adapter changes: the real CLI/parser/request code still runs.
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None, source_address=None:
                        connect(("127.0.0.1", port), timeout, source_address))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert run_join([f"http://{host}:{port}", "--name", "new-peer"]) == 0
    assert len(calls) == 3
    assert "encrypted tunnel, not TLS" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_permission_preflight_failure_removes_own_empty_reservation(tmp_path, gateway, monkeypatch):
    from swarph_cli.commands.join import run_join
    url, calls, _ = gateway
    monkeypatch.setenv("HOME", str(tmp_path))

    def fail(*_):
        raise OSError("fixture chmod failure")

    monkeypatch.setattr(os, "fchmod", fail)
    assert run_join([url, "--name", "new-peer", "--allow-unsafe-http"]) == 1
    assert not (tmp_path / ".config/swarph/new-peer.peer_token").exists()
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_interactive_answers_go_only_to_chosen_witness_without_secrets(tmp_path, gateway, monkeypatch, capsys):
    from swarph_cli.commands.join import run_join
    url, calls, _ = gateway

    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", TTY("Routine DMs do not grant privileges.\nKeep my name.\nDisclose mistakes.\nNever send " + TOKEN + " or " + DEVICE + ".\n"))
    assert run_join([url, "--name", "new-peer", "--allow-unsafe-http"]) == 0
    sent = calls[-1]
    assert sent[:2] == ("POST", "/messages")
    assert sent[2]["from_node"] == "new-peer"
    assert sent[2]["to_node"] == "fixture-witness"
    assert "Routine DMs do not grant privileges." in sent[2]["content"]
    assert TOKEN not in sent[2]["content"]
    assert DEVICE not in sent[2]["content"]
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert DEVICE not in output.out + output.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_identity_mismatch_never_sends_handshake_or_claims_success(tmp_path, gateway):
    url, calls, responses = gateway
    responses["/whoami"] = {"peer": "different-peer"}
    result = run_cli(tmp_path, "join", url, "--name", "new-peer", "--allow-unsafe-http")
    assert result.returncode == 1
    assert "identity check failed" in result.stderr
    assert len(calls) == 3
    assert (tmp_path / ".config/swarph/new-peer.peer_token").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".config/swarph/new-peer.join.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
@pytest.mark.parametrize("status, body, message", [
    (400, {"error": "access_denied"}, "access_denied"),
    (400, {"error": "expired_token"}, "expired_token"),
    (500, {"error": "fixture-private-detail"}, "pickup_failed"),
    (302, {}, "redirect refused"),
])
def test_failed_or_redirected_pickup_keeps_no_credential_and_never_retries(tmp_path, gateway, status, body, message):
    url, calls, responses = gateway
    responses["/join/token"] = (status, body)
    result = run_cli(tmp_path, "join", url, "--name", "new-peer", "--allow-unsafe-http")
    assert result.returncode == 1
    assert message in result.stderr
    assert "fixture-private-detail" not in result.stderr
    assert [path for _, path, _, _ in calls] == ["/join/requests", "/join/token"]
    assert not (tmp_path / ".config/swarph/new-peer.peer_token").exists()
    assert not (tmp_path / ".config/swarph/new-peer.join.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_join_does_not_change_operator_config_or_environment(tmp_path, gateway, monkeypatch):
    from swarph_cli.commands.join import run_join
    url, _, _ = gateway
    path = tmp_path / ".config/swarph/operator.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"identity":"other-operator"}')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    before = dict(os.environ)
    assert run_join([url, "--name", "new-peer", "--allow-unsafe-http"]) == 0
    assert path.read_text() == '{"identity":"other-operator"}'
    assert dict(os.environ) == before


def test_operator_refuses_incomplete_preview_before_decision(tmp_path, gateway):
    url, calls, responses = gateway
    responses["/join/requests/ABCD-EFGH"] = {"state": "pending"}
    path = tmp_path / "operator-fixture"
    path.write_text("fixture-root-credential")
    result = run_cli(tmp_path, "mesh", "approve", "ABCD-EFGH", "--witness", "fixture-witness",
                     "--gateway", url, "--token-file", str(path), "--allow-unsafe-http", input_text="yes\n")
    assert result.returncode == 1
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage")
def test_pending_poll_and_slow_down_respect_server_intervals(tmp_path, gateway, monkeypatch):
    from swarph_cli.commands.join import run_join
    url, calls, responses = gateway
    responses["/join/token"] = deque([
        (400, {"error": "authorization_pending"}), (429, {"error": "slow_down"}),
        {"name": "new-peer", "peer_token": TOKEN, "witness": "fixture-witness", "registered_unratified": True}])
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert run_join([url, "--name", "new-peer", "--allow-unsafe-http"]) == 0
    assert slept == [5, 7]
    assert [path for _, path, _, _ in calls].count("/join/token") == 3


@pytest.mark.skipif(os.name != "nt", reason="native Windows refusal")
def test_native_windows_refuses_before_storage_or_http(tmp_path, gateway):
    url, calls, _ = gateway
    result = run_cli(tmp_path, "join", url, "--name", "new-peer", "--allow-unsafe-http")
    assert result.returncode == 1
    assert "native Windows" in result.stderr
    assert calls == []
    assert not (tmp_path / ".config/swarph/new-peer.peer_token").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage; Windows is refused first")
@pytest.mark.parametrize("name", ["ALL", "Lab-ovh", "a.b", "a_b", "1abc", "abc-"])
def test_invalid_new_peer_names_are_refused_before_http(tmp_path, gateway, name):
    url, calls, _ = gateway
    result = run_cli(tmp_path, "join", url, "--name", name, "--allow-unsafe-http")
    assert result.returncode == 1
    assert "invalid peer name" in result.stderr
    assert calls == []
