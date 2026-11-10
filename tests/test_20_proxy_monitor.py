"""Card #20 private monitor CLI: fake credentials and system boundaries only."""
import sys

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("the isolated proxy monitor is POSIX-only", allow_module_level=True)

import ctypes
import grp
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import threading
import time
import urllib.request
from types import SimpleNamespace

from swarph_cli import proxy_monitor


def test_pinned_tick_keeps_unread_and_stall_alerts_off_urllib(monkeypatch, tmp_path, gateway):
    from swarph_cli.commands import mesh, monitor
    from swarph_cli.local_proxy import Config, Proxy

    def forbidden(*args, **kwargs):
        pytest.fail("pinned monitor attempted urllib instead of private transport")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    gateway.responses["/messages/unread-count"] = (200, {"to_node": "alice", "n": 1})

    class BusySink:
        name = "fixture-busy"
        keeps_ledger = is_push = True

        def observe(self, *args):
            return False

        def deliver(self, state, *args):
            state.shutdown_requested = True
            return None

    config = Config("alice", os.getuid()+1234, gateway.url, tmp_path / "unused-token",
                    tmp_path / "unused.sock", tmp_path / "audit", allow_loopback_tcp=True)
    state = mesh.MonitorState(self_name="alice", state_dir=tmp_path / "state",
                              gateway=gateway.url, token="fixture-private-monitor-token",
                              sinks=[BusySink()])
    state.ledger("fixture-busy").update(deferred_ticks=59, wake_outstanding=True)
    with proxy_monitor._private_transport(Proxy(config, "fixture-private-monitor-token")):
        assert monitor._run_pinned(state, state.pidfile_path, "alice", state.sinks, state.poll_s) == 0
    paths = [path for method, path, auth in gateway.calls]
    assert "/messages/unread-count?to_node=alice" in paths
    assert "/board/obligations?card_id=989&status=open" in paths
    assert "/board/obligations?card_id=989&status=fallback_fired" in paths
    assert "/board/cards/989/ask" in paths
    assert state.iterations == 1 and state.ledger("fixture-busy")["deferred_ticks"] == 60


@pytest.mark.parametrize("override", [
    ["--gateway", "http://caller.invalid"],
    ["--token", "fixture-token"],
    ["--token-file", "/caller/credential"],
    ["--deliver", "tmux:cell:0"],
])
def test_monitor_cli_has_no_caller_credential_gateway_or_delivery_overrides(override):
    with pytest.raises(SystemExit) as stopped:
        proxy_monitor.main(["--cell", "alice", "--state-dir", "/unused", *override])
    assert stopped.value.code == 2


def test_missing_config_never_uses_gateway_or_token_from_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://caller.invalid")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "fixture-secret-not-for-logs")
    assert proxy_monitor.main([
        "--cell", "alice", "--config", str(tmp_path / "missing.json"),
        "--state-dir", str(tmp_path / "monitor"),
    ]) == 2
    captured = capsys.readouterr()
    assert "fixture-secret-not-for-logs" not in captured.out + captured.err
    assert not (tmp_path / "monitor").exists()


@pytest.fixture(autouse=True)
def no_real_process_hardening(monkeypatch):
    monkeypatch.setattr(resource, "setrlimit", lambda *_: None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kw: SimpleNamespace(
        prctl=lambda *_: 0, ptrace=lambda *_: 0))


@pytest.fixture
def monitor_install(monkeypatch, tmp_path):
    """Real private fixture files; only privileged OS boundaries are replaced."""
    monkeypatch.setattr(grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setattr(os, "fork", lambda: pytest.fail("private monitor must stay inline"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("monitor must not run tmux"))
    monkeypatch.setattr(time, "sleep", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    temporary = tempfile.TemporaryDirectory(prefix="monitor-fixture-", dir=tmp_path)
    fixture_root = Path(temporary.name)
    fixture_root.chmod(0o755)
    # Model traversable install ancestors without chmodding pytest's private root.
    private_ancestors = set(fixture_root.parents)
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if path in private_ancestors:
            values = list(info)
            values[0] |= 0o111
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    store = fixture_root / "store"
    store.mkdir(mode=0o700)
    token = store / "peer_token"
    token.write_text("fixture-private-monitor-token")
    token.chmod(0o600)
    state = fixture_root / "monitor"
    state.mkdir(mode=0o2750)
    state.chmod(0o2750)
    config = store / "config.json"

    def configure(gateway):
        config.write_text(json.dumps({
            "cell_uid": os.getuid() + 1,
            "gateway_url": gateway,
            "allow_loopback_tcp": True,
            "token_file": str(token),
            "socket_path": str(store / "proxy.sock"),
            "audit_path": str(store / "audit.jsonl"),
        }))
        config.chmod(0o600)
        return ["--cell", "alice", "--config", str(config), "--state-dir", str(state)]

    try:
        yield SimpleNamespace(configure=configure, state=state, token=token, store=store)
    finally:
        temporary.cleanup()


@pytest.fixture
def gateway():
    calls = []
    responses = {}

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get("Authorization")))
            status, body = responses.get(self.path.split("?")[0], (200, {
                "messages": [{"id": 17, "from_node": "bob", "kind": "fyi", "content": "hello"}],
            }))
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/credential-sink")
            raw = json.dumps(body).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, self.headers.get("Authorization")))
            self.send_response(responses.get("POST", (200, {}))[0])
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", calls=calls, responses=responses)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_monitor_runs_existing_pull_engine_inline_with_config_identity(monkeypatch, monitor_install, gateway):
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://caller.invalid")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "caller-token-must-not-be-used")
    monkeypatch.setenv("SWARPH_SELF", "other-cell")
    monkeypatch.setenv("SWARPH_ROWCLEAR", "live")
    argv = monitor_install.configure(gateway.url)
    assert proxy_monitor.main(argv) == 130
    inbox = monitor_install.state / "inbox.log"
    assert json.loads(inbox.read_text()) == {
        "id": 17, "from_node": "bob", "kind": "fyi", "content": "hello",
    }
    assert inbox.stat().st_mode & 0o777 == 0o640
    assert json.loads((monitor_install.state / "cursor.json").read_text())["last_msg_id"] == 17
    assert set(json.loads((monitor_install.state / "ledgers.json").read_text())) == {"pull"}
    assert gateway.calls[0] == (
        "GET", "/messages?to_node=alice&limit=50", "Bearer fixture-private-monitor-token",
    )
    assert all(call[2] == "Bearer fixture-private-monitor-token" for call in gateway.calls)


def test_redirect_is_a_failed_poll_not_a_successful_empty_inbox(monitor_install, gateway, capsys):
    gateway.responses["/messages"] = (307, {"detail": "fixture-private-monitor-token"})
    assert proxy_monitor.main(monitor_install.configure(gateway.url)) == 130
    assert not (monitor_install.state / "drain_heartbeat.json").exists()
    assert (monitor_install.state / "inbox.log").read_text() == ""
    assert not any(path == "/credential-sink" for _method, path, _auth in gateway.calls)
    captured = capsys.readouterr()
    assert "fixture-private-monitor-token" not in captured.out + captured.err


def test_state_hidden_behind_private_parent_is_refused(monitor_install, gateway):
    argv = monitor_install.configure(gateway.url)
    monitor_install.state.parent.chmod(0o700)
    assert proxy_monitor.main(argv) == 2
    assert gateway.calls == []


def test_instance_registration_failure_does_not_stop_dm_drain(monitor_install, gateway, capsys):
    gateway.responses["POST"] = (404, {})
    assert proxy_monitor.main(monitor_install.configure(gateway.url)) == 130
    assert json.loads((monitor_install.state / "inbox.log").read_text())["id"] == 17
    assert "fixture-private-monitor-token" not in capsys.readouterr().err


def test_daemon_owned_state_keeps_existing_monitor_collision_guard(monitor_install, gateway):
    cursor = monitor_install.state / "cursor.json"
    cursor.write_text('{"tasks_snapshot": {}}')
    cursor.chmod(0o640)
    assert proxy_monitor.main(monitor_install.configure(gateway.url)) == 2
    assert gateway.calls == []
    assert cursor.read_text() == '{"tasks_snapshot": {}}'


@pytest.mark.parametrize("mode", [0o750, 0o2770, 0o2775])
def test_monitor_refuses_state_without_the_exact_readonly_cell_group_mode(mode, monitor_install, gateway):
    argv = monitor_install.configure(gateway.url)
    monitor_install.state.chmod(mode)
    assert proxy_monitor.main(argv) == 2
    assert gateway.calls == []


@pytest.mark.parametrize("filename", ["inbox.log", "cursor.json", "ledgers.json", "monitor.pid"])
def test_cell_cannot_turn_any_monitor_write_into_a_token_append(filename, monitor_install, gateway):
    argv = monitor_install.configure(gateway.url)
    before = monitor_install.token.read_bytes()
    (monitor_install.state / filename).symlink_to(monitor_install.token)
    assert proxy_monitor.main(argv) == 2
    assert monitor_install.token.read_bytes() == before
    assert gateway.calls == []


def test_hardlinked_inbox_is_refused_without_changing_the_secret(monitor_install, gateway):
    argv = monitor_install.configure(gateway.url)
    os.link(monitor_install.token, monitor_install.state / "inbox.log")
    assert proxy_monitor.main(argv) == 2
    assert monitor_install.token.read_text() == "fixture-private-monitor-token"
    assert gateway.calls == []


@pytest.mark.parametrize("status", [200, 500])
def test_upstream_cannot_echo_credential_into_inbox_or_monitor_logs(status, monitor_install, gateway, capsys):
    gateway.responses["/messages"] = (status, {
        "detail": "fixture-private-monitor-token",
        "messages": [{"id": 17, "content": "fixture-private-monitor-token"}],
    })
    assert proxy_monitor.main(monitor_install.configure(gateway.url)) == 130
    assert (monitor_install.state / "inbox.log").read_text() == ""
    captured = capsys.readouterr()
    assert "fixture-private-monitor-token" not in captured.out + captured.err
    assert "Gateway request failed." in captured.err


def test_transport_exception_text_cannot_reach_monitor_logs(monkeypatch, monitor_install, gateway, capsys):
    def failed_connect(_connection):
        raise RuntimeError("fixture-private-monitor-token")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", failed_connect)
    assert proxy_monitor.main(monitor_install.configure(gateway.url)) == 130
    captured = capsys.readouterr()
    assert "fixture-private-monitor-token" not in captured.out + captured.err
    assert (monitor_install.state / "inbox.log").read_text() == ""
    assert gateway.calls == []


def test_supervised_monitor_honors_an_existing_hold(monitor_install, gateway):
    argv = monitor_install.configure(gateway.url)
    hold = monitor_install.state / "supervision_hold.json"
    hold.write_text('{}')
    hold.chmod(0o640)
    assert proxy_monitor.main(argv) == 0
    assert hold.exists()
    assert gateway.calls == []
