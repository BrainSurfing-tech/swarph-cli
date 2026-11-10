"""Only fixture listeners/credentials; exercise the real AF_UNIX peer check."""
import dataclasses
import http.client
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("Unix gateway transport", allow_module_level=True)

from swarph_cli import local_proxy, proxy_monitor
from swarph_cli.local_proxy import Config, Proxy, load_config
from swarph_cli.proxy_routes import ProxyError

TOKEN = "fixture-only-1557-token"


@pytest.fixture
def store(tmp_path_factory):
    # Darwin's long runner TMPDIR exceeds sun_path; keep a private, canonical store.
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory(prefix="p1557-", dir="/private/tmp") as directory:
            yield Path(directory)
        return
    path = tmp_path_factory.mktemp("uds1557").resolve()
    path.chmod(0o700)
    yield path


@contextmanager
def gateway(store, *, response=b'{"fixture":true}'):
    received = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            raw = b""
            while b"\r\n\r\n" not in raw:
                part = self.request.recv(65536)
                if not part:
                    received.append(raw)
                    return
                raw += part
            head, body = raw.split(b"\r\n\r\n", 1)
            length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            while len(body) < length:
                part = self.request.recv(length - len(body))
                if not part:
                    break
                body += part
            received.append(head + b"\r\n\r\n" + body)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                                 + str(len(response)).encode() + b"\r\n\r\n" + response)

    path = store / (uuid.uuid4().hex[:8] + ".sock")
    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield path, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def config(store, **fields):
    return dataclasses.replace(Config("alice", os.getuid() + 12345, "http://localhost:8788",
                                      store / "token", store / "proxy.sock", store / "audit"), **fields)


def protected_config(store, **fields):
    path = store / "config.json"
    path.write_text(json.dumps({"cell_uid": os.getuid() + 12345,
                               "gateway_url": "http://localhost:8788",
                               "socket_path": str(store / "proxy.sock"),
                               "audit_path": str(store / "audit"), **fields}))
    path.chmod(0o600)
    return path


def request(proxy, verb="whoami", args=None):
    return proxy.request({"request_id": str(uuid.uuid4()), "verb": verb, "args": args or {}})


def wait_for_receipt(received):
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        time.sleep(0.001)
    assert received, "fixture listener did not observe the connection"


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/whoami", None),
    ("POST", "/messages", {"from_node": "alice", "to_node": "bob", "content": "fixture"}),
    ("PATCH", "/board/cards/20", {"stage": "review", "expect_stage_version": 2}),
])
def test_checked_unix_gateway_preserves_http_without_tcp(store, method, path, body):
    with gateway(store) as (endpoint, received), socket.socket() as tcp:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        tcp.settimeout(0.05)
        cfg = load_config("alice", protected_config(store, gateway_socket=str(endpoint),
                          gateway_uid=os.getuid(), gateway_url=f"http://127.0.0.1:{tcp.getsockname()[1]}"))
        assert Proxy(cfg, TOKEN).call_gateway(method, path, body) == (200, {"fixture": True})
        assert len(received) == 1
        head, payload = received[0].split(b"\r\n\r\n")
        assert head.startswith(f"{method} {path} HTTP/1.1\r\n".encode())
        assert f"Authorization: Bearer {TOKEN}\r\n".encode() in head + b"\r\n"
        assert (json.loads(payload) if payload else None) == body
        with pytest.raises(socket.timeout):
            tcp.accept()


@pytest.mark.parametrize("monitor", [False, True])
def test_wrong_gateway_uid_receives_zero_bytes(store, monitor, capsys):
    with gateway(store) as (endpoint, received):
        proxy = Proxy(config(store, gateway_socket=endpoint, gateway_uid=os.getuid() + 1), TOKEN)
        if monitor:
            from swarph_cli.commands import mesh
            with proxy_monitor._private_transport(proxy):
                result = mesh._http_get_json("http://localhost:8788/messages?to=alice", TOKEN)
        else:
            result = request(proxy)
        wait_for_receipt(received)
    assert received == [b""]  # Actual accepted connection, not a mocked peer_uid.
    if monitor:
        assert result[0] == 502
    else:
        assert result["error"] == "proxy_unavailable"
    output = str(capsys.readouterr())
    if not monitor:
        output += (store / "audit").read_text()
    assert TOKEN not in output


@pytest.mark.parametrize("url", ["http://127.0.0.1:8788", "http://127.99.4.3", "http://[::1]",
                                "http://LOCALHOST", "http://localhost.", "http://[::ffff:127.0.0.1]"])
def test_loopback_refused_at_startup_unless_explicitly_unsafe(store, url):
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        load_config("alice", protected_config(store, gateway_url=url))
    assert load_config("alice", protected_config(store, gateway_url=url,
                       allow_loopback_tcp=True)).allow_loopback_tcp is True


def test_remote_tailnet_url_still_allowed(store):
    assert load_config("alice", protected_config(store, gateway_url="http://100.64.1.2:8788")).gateway_socket is None


@pytest.mark.parametrize("fields", [
    {"gateway_socket": "relative.sock", "gateway_uid": 0},
    {"gateway_socket": None}, {"gateway_socket": ""},
    {"gateway_uid": 1000}, {"gateway_uid": True},
    {"allow_loopback_tcp": 1}, {"allow_loopback_tcp": "true"},
])
def test_malformed_protected_transport_settings_refuse(store, fields):
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        load_config("alice", protected_config(store, **fields))


@pytest.mark.parametrize("uid", [None, True, -1, "1000"])
def test_unix_socket_needs_an_integer_uid(store, uid):
    with gateway(store) as (endpoint, _):
        with pytest.raises(ProxyError, match="^proxy_unavailable$"):
            load_config("alice", protected_config(store, gateway_socket=str(endpoint), gateway_uid=uid))


def test_socket_symlink_and_unsafe_parent_refused(store):
    with gateway(store) as (endpoint, received):
        link = store / "alias.sock"
        link.symlink_to(endpoint)
        proxy = Proxy(config(store, gateway_socket=link, gateway_uid=os.getuid()), TOKEN)
        assert request(proxy)["error"] == "proxy_unavailable"
        with pytest.raises(ProxyError, match="^proxy_unavailable$"):
            load_config("alice", protected_config(store, gateway_socket=str(link), gateway_uid=os.getuid()))
        store.chmod(0o777)
        try:
            proxy = Proxy(config(store, gateway_socket=endpoint, gateway_uid=os.getuid()), TOKEN)
            assert request(proxy)["error"] == "proxy_unavailable"
        finally:
            store.chmod(0o700)
        assert received == []


def test_replaced_socket_checked_again_on_each_request(store):
    with gateway(store) as (endpoint, received):
        proxy = Proxy(config(store, gateway_socket=endpoint, gateway_uid=os.getuid()), TOKEN)
        assert request(proxy)["ok"]
        endpoint.unlink()
        endpoint.symlink_to(store / "elsewhere")
        assert request(proxy)["error"] == "proxy_unavailable"
        assert len(received) == 1


def test_dns_loopback_and_noncanonical_address_cannot_bypass_check(store):
    with socket.socket() as tcp:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        tcp.settimeout(1)
        proxy = Proxy(config(store, gateway_url=f"http://2130706433:{tcp.getsockname()[1]}"), TOKEN)
        assert request(proxy)["error"] == "proxy_unavailable"
        connection, _ = tcp.accept()
        with connection:
            assert connection.recv(1024) == b""


def test_checked_connection_is_never_automatically_reopened(store, monkeypatch):
    original = http.client.HTTPConnection.request
    connections = []
    def disconnected(connection, *args, **kwargs):
        connections.append(connection)
        connection.close()
        return original(connection, *args, **kwargs)
    monkeypatch.setattr(http.client.HTTPConnection, "request", disconnected)
    with gateway(store) as (endpoint, received):
        proxy = Proxy(config(store, gateway_socket=endpoint, gateway_uid=os.getuid()), TOKEN)
        assert request(proxy)["error"] == "upstream_ambiguous"
        wait_for_receipt(received)
    assert len(connections) == 1 and connections[0].auto_open is False
    assert received == [b""]


def test_monitor_all_gateway_helpers_share_checked_socket(store, monkeypatch):
    from swarph_cli import duplicate_session, stall_alert
    from swarph_cli.commands import mesh, watchdog
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("monitor bypassed checked socket")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    with gateway(store) as (endpoint, received):
        proxy = Proxy(config(store, gateway_socket=endpoint, gateway_uid=os.getuid()), TOKEN)
        with proxy_monitor._private_transport(proxy):
            assert mesh._http_get_json("http://localhost:8788/messages?to=alice", TOKEN)[0] == 200
            assert duplicate_session._request("POST", "http://localhost:8788/instances", TOKEN, {}) == {"fixture": True}
            assert stall_alert._request("POST", "http://localhost:8788/board/cards/989/ask", TOKEN, {}) == {"fixture": True}
            assert watchdog._gateway_request("http://localhost:8788", "/board/obligations", {}, TOKEN)[1] == 200
        assert len(received) == 4
        assert all(f"Bearer {TOKEN}".encode() in raw for raw in received)
        assert received[0].startswith(b"GET /messages?to_node=alice HTTP/1.1")


def test_pinned_monitor_tick_uses_unix_socket_for_poll_unread_watchdog_and_alert(store, monkeypatch):
    from swarph_cli.commands import mesh, monitor
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("pinned tick attempted urllib")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    class BusySink:
        name = "fixture-busy"
        keeps_ledger = is_push = True
        def observe(self, *args):
            return False
        def deliver(self, state, *args):
            state.shutdown_requested = True
            return None
    response = b'{"messages":[{"id":1,"from_node":"bob","content":"fixture"}],"obligations":[],"n":1}'
    with gateway(store, response=response) as (endpoint, received):
        cfg = config(store, gateway_socket=endpoint, gateway_uid=os.getuid())
        state = mesh.MonitorState(self_name="alice", state_dir=store / "state",
                                  gateway=cfg.gateway_url, token=TOKEN, sinks=[BusySink()])
        state.ledger("fixture-busy").update(deferred_ticks=59, wake_outstanding=True)
        with proxy_monitor._private_transport(Proxy(cfg, TOKEN)):
            assert monitor._run_pinned(state, state.pidfile_path, "alice", state.sinks, state.poll_s) == 0
        lines = [raw.split(b"\r\n", 1)[0] for raw in received]
        assert b"GET /messages?to_node=alice&limit=50 HTTP/1.1" in lines
        assert b"GET /messages/unread-count?to_node=alice HTTP/1.1" in lines
        assert b"GET /board/obligations?card_id=989&status=open HTTP/1.1" in lines
        assert b"POST /board/cards/989/ask HTTP/1.1" in lines
        assert all(f"Bearer {TOKEN}".encode() in raw for raw in received)
        assert state.iterations == 1
