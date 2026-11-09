"""Card20: real local socket and private fixture gateway; never the live mesh."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX proxy only")
if sys.platform not in {"linux", "darwin"}:
    pytest.skip("POSIX proxy only", allow_module_level=True)

from swarph_cli.local_proxy import Config, Proxy, call, failure, peer_uid


@pytest.fixture
def tmp_path(tmp_path):
    # macOS's /var alias is root-owned but the runtime deliberately accepts only physical paths.
    return tmp_path.resolve()


def short_socket(tmp_path):
    # Darwin's sun_path is shorter than pytest's descriptive per-test directories.
    return tmp_path.parent / ("p20-" + uuid.uuid4().hex[:8])


def test_complete_gateway_json_is_the_only_success(tmp_path):
    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/whoami"
            body = b'{"peer":"alice"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        config = Config("alice", 1001, f"http://127.0.0.1:{server.server_port}",
                        tmp_path / "peer_token", tmp_path / "proxy.sock", tmp_path / "audit")
        proxy = Proxy(config, "fixture-token")
        request = {"request_id": str(uuid.uuid4()), "verb": "whoami", "args": {}}
        reply = proxy.request(request)
        assert reply == {"request_id": request["request_id"], "ok": True,
                         "status": 200, "body": {"peer": "alice"}}
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@contextmanager
def gateway(tmp_path, response, *, uid=None):
    seen = []
    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_PATCH(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.command, self.path, json.loads(body) if body else None))
            response(self)

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = Config("alice", os.getuid() if uid is None else uid,
                    f"http://127.0.0.1:{server.server_port}",
                    tmp_path / "peer_token", short_socket(tmp_path), tmp_path / "audit")
    try:
        yield Proxy(config, "fixture-secret-20"), seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def answer(handler, body=b'{"id":1}', status=200, **headers):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def request(proxy, verb="whoami", args=None):
    return proxy.request({"request_id": str(uuid.uuid4()), "verb": verb, "args": args or {}})


def socket_request(proxy, verb="whoami", args=None):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(proxy.config.socket_path))
        listener.listen()
        def serve_one():
            connection, _ = listener.accept()
            proxy.handle(connection)
        worker = threading.Thread(target=serve_one)
        worker.start()
        try:
            return call(str(proxy.config.socket_path), verb, args or {})
        finally:
            worker.join(timeout=3)
            assert not worker.is_alive()


def test_real_socket_pins_inbox_and_never_sends_auth_back(tmp_path):
    with gateway(tmp_path, lambda h: answer(h, b'{"messages":[]}')) as (proxy, seen):
        result = socket_request(proxy, "mesh.inbox")
        assert result["ok"]
        assert seen[0][1].startswith("/messages?")
        from urllib.parse import parse_qs, urlsplit
        assert parse_qs(urlsplit(seen[0][1]).query)["to_node"] == ["alice"]
        assert "fixture-secret-20" not in json.dumps(result)


def test_real_peer_uid_denies_the_wrong_user_before_gateway(tmp_path):
    with gateway(tmp_path, lambda h: answer(h), uid=os.getuid() + 1) as (proxy, seen):
        left, right = socket.socketpair()
        with left, right:
            assert peer_uid(left) == os.getuid()
        result = socket_request(proxy)
        assert result["error"] == "peer_not_allowed"
        assert seen == []


@pytest.mark.parametrize("body,status", [(b'{}', 403), (b'not-json', 200), (b'', 204),
                                         (b'{"detail":"fixture-secret-20"}', 200)])
def test_bad_upstream_never_becomes_success_or_exposes_detail(tmp_path, body, status):
    with gateway(tmp_path, lambda h: answer(h, body, status)) as (proxy, _):
        result = request(proxy)
        assert result["error"] == "upstream_error"
        assert "fixture-secret-20" not in json.dumps(result)


def test_redirect_is_not_followed_even_to_a_token_mint_route(tmp_path):
    def redirect(h):
        if h.path == "/peers/register":
            answer(h, b'{"token":"new-secret"}')
        else:
            answer(h, b'{}', 302, Location=f"http://127.0.0.1:{h.server.server_port}/peers/register")
    with gateway(tmp_path, redirect) as (proxy, seen):
        result = request(proxy)
        assert [path for _, path, _ in seen] == ["/whoami"]
        assert result["error"] == "upstream_error"
        assert result["status"] == 302


def test_truncated_body_is_upstream_error(tmp_path):
    def truncated(h):
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(b'{"ok":true}')
        h.close_connection = True
    with gateway(tmp_path, truncated) as (proxy, _):
        assert request(proxy)["error"] == "upstream_error"


def test_timeout_after_send_is_ambiguous_and_not_retried(tmp_path, monkeypatch):
    import swarph_cli.local_proxy as local
    def delayed(h):
        time.sleep(0.1)
    monkeypatch.setattr(local, "UPSTREAM_TIMEOUT", 0.02)
    with gateway(tmp_path, delayed) as (proxy, seen):
        assert request(proxy)["error"] == "upstream_ambiguous"
        assert len(seen) == 1


def test_before_send_failure_is_unreachable_and_missing_proxy_has_no_fallback(tmp_path):
    # Bind then close a private ephemeral port; no external host can be contacted.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Config("alice", os.getuid() + 1, f"http://127.0.0.1:{port}",
                    tmp_path / "token", tmp_path / "missing.sock", tmp_path / "audit")
    assert request(Proxy(config, "fixture-secret-20"))["error"] == "upstream_unreachable"
    assert call(str(config.socket_path), "whoami", {})["error"] == "proxy_unavailable"


@pytest.mark.parametrize("version,allowed", [(None, False), (0, True), (False, False)])
def test_move_requires_observed_stage_version_and_never_done(tmp_path, version, allowed):
    def card(h):
        if h.command == "GET":
            answer(h, json.dumps({"id": 20, "stage_version": version}).encode())
        else:
            answer(h)
    with gateway(tmp_path, card) as (proxy, seen):
        result = request(proxy, "board.cards.move", {"id": 20, "stage": "review",
                         "expect_stage": "build", "expect_stage_version": 0})
        assert result["ok"] is allowed
        assert len(seen) == (2 if allowed else 1)
        result = request(proxy, "board.cards.move", {"id": 20, "stage": "done",
                         "expect_stage": "build", "expect_stage_version": 0})
        assert result["error"] == "verb_not_allowed"


def test_say_resolves_only_trusted_card_thread_and_pins_sender(tmp_path):
    thread = "11111111-1111-4111-8111-111111111111"
    def card(h):
        answer(h, json.dumps({"id": 20, "thread_uuid": thread, "assignee": None}).encode())
    with gateway(tmp_path, card) as (proxy, seen):
        assert request(proxy, "board.cards.say", {"id": 20, "content": "hello"})["ok"]
        assert seen[-1] == ("POST", "/messages", {"from_node": "alice", "to_node": "alice",
                            "thread_id": thread, "kind": "status", "content": "hello"})


def test_credential_store_rejects_writable_config_token_symlinks_and_env_urls(tmp_path, monkeypatch):
    from swarph_cli.local_proxy import load_config, read_token
    from swarph_cli.proxy_routes import ProxyError
    path = tmp_path / "config.json"
    token = tmp_path / "peer_token"
    token.write_text("fake-store-token")
    token.chmod(0o600)
    path.write_text(json.dumps({"cell_uid": os.getuid() + 1234, "gateway_url": "http://127.0.0.1:1234",
                               "socket_path": str(tmp_path / "socket"), "audit_path": str(tmp_path / "audit")}))
    path.chmod(0o600)
    monkeypatch.setenv("MESH_GATEWAY_URL", "https://attacker.invalid")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "not-the-store-token")
    config = load_config("alice", path)
    assert config.gateway_url == "http://127.0.0.1:1234"
    assert read_token(config) == "fake-store-token"
    token.chmod(0o644)
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        read_token(config)
    token.chmod(0o600)
    other = tmp_path / "link"
    other.symlink_to(token)
    import dataclasses
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        read_token(dataclasses.replace(config, token_file=other))
    path.chmod(0o666)
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        load_config("alice", path)


def test_socket_directory_symlink_swap_cannot_chmod_the_token(tmp_path):
    from swarph_cli.local_proxy import safe_parent
    from swarph_cli.proxy_routes import ProxyError
    token = tmp_path / "peer_token"
    token.write_text("fake-store-token")
    token.chmod(0o600)
    target = tmp_path / "attacker-dir"
    target.mkdir()
    swapped = tmp_path / "socket-parent"
    swapped.symlink_to(target, target_is_directory=True)
    (target / "proxy.sock").symlink_to(token)
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        safe_parent(swapped / "proxy.sock", os.getuid() + 1234)
    assert token.stat().st_mode & 0o777 == 0o600
    assert token.read_text() == "fake-store-token"


def test_audit_contains_only_validated_metadata_not_token_or_caller_body(tmp_path, capsys):
    config = Config("alice", os.getuid() + 1234, "http://127.0.0.1:1",
                    tmp_path / "peer_token", tmp_path / "proxy.sock", tmp_path / "audit")
    proxy = Proxy(config, "fake-secret20")
    mask = os.umask(0o077)
    try:
        result = request(proxy, "mesh.send", {"to_node": "bob", "content": "fake-secret20"})
    finally:
        os.umask(mask)
    assert result["error"] == "upstream_unreachable"
    entry = json.loads(config.audit_path.read_text())
    assert entry["verb"] == "mesh.send" and entry["outcome"] == "upstream_unreachable"
    assert entry["targets"] == {}
    assert config.audit_path.stat().st_mode & 0o777 == 0o640
    assert "fake-secret20" not in config.audit_path.read_text() + json.dumps(result) + str(capsys.readouterr())


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX child process")
def test_running_proxy_segv_has_zero_core_limit_and_no_core_dump(tmp_path):
    """Real secret-bearing fixture proxy only; no host kernel/sysctl changes."""
    import signal
    from swarph_cli.local_proxy import serve
    token = tmp_path / "peer_token"
    token.write_text("fake-crash-token")
    token.chmod(0o600)
    config = Config("alice", os.getuid() + 1234, "http://127.0.0.1:1", token,
                    short_socket(tmp_path), tmp_path / "audit")
    pid = os.fork()
    if pid == 0:
        try:
            import faulthandler
            faulthandler.disable()  # The deliberate crash is the assertion, not a pytest traceback.
            os.chdir(tmp_path)
            serve(config)
        finally:
            os._exit(1)
    try:
        deadline = time.monotonic() + 5
        while not config.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert config.socket_path.exists(), "fixture proxy did not start"
        if sys.platform.startswith("linux"):
            limits = open(f"/proc/{pid}/limits").read()
            assert "Max core file size        0                    0" in limits
        os.kill(pid, signal.SIGSEGV)
        _, status = os.waitpid(pid, 0)
        pid = None
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSEGV
        assert not os.WCOREDUMP(status), "kernel reported a core dump"
        assert list(tmp_path.rglob("core*")) == []
    finally:
        if pid:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_dumpability_is_disabled_in_the_real_child_kernel():
    script = '''
import ctypes, json, resource, sys
from swarph_cli.local_proxy import harden_process
harden_process()
result = {"core": resource.getrlimit(resource.RLIMIT_CORE)}
if sys.platform.startswith("linux"):
    result["dumpable"] = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)
print(json.dumps(result))
'''
    env = {**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout)
    assert measured["core"] == [0, 0]
    if sys.platform.startswith("linux"):
        assert measured["dumpable"] == 0


def test_client_cli_returns_fixed_error_without_any_token_fallback(tmp_path, monkeypatch, capsys):
    from swarph_cli.main import main
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "fake-env-token-should-never-appear")
    rc = main(["proxy", "call", "whoami", "--args-json", "{}", "--socket", str(tmp_path / "missing")])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out)["error"] == "proxy_unavailable"
    assert "fake-env-token" not in captured.out + captured.err


def test_escaped_token_reflection_is_also_refused(tmp_path):
    secret = 'fake-secret-with-"-and-\\-20'
    with gateway(tmp_path, lambda h: answer(h, json.dumps({"nested": [secret]}).encode())) as (proxy, _):
        proxy = Proxy(proxy.config, secret)
        assert request(proxy)["error"] == "upstream_error"


def test_connection_setup_cannot_outlive_the_shared_upstream_deadline(tmp_path, monkeypatch):
    import swarph_cli.local_proxy as local
    def slow_connect(_):
        time.sleep(0.2)  # Simulate a resolver that ignores the socket timeout.
        raise OSError("fake-secret20")
    monkeypatch.setattr(local.http.client.HTTPConnection, "connect", slow_connect)
    monkeypatch.setattr(local, "UPSTREAM_TIMEOUT", 0.02)
    config = Config("alice", os.getuid() + 1234, "http://127.0.0.1:1",
                    tmp_path / "token", tmp_path / "socket", tmp_path / "audit")
    before = time.monotonic()
    result = request(Proxy(config, "fake-secret20"))
    assert result["error"] == "upstream_unreachable"
    assert time.monotonic() - before < 0.15


def test_numeric_token_cannot_be_reflected_as_a_number_or_audit_target(tmp_path):
    secret = "123456789012345678901234567890"
    with gateway(tmp_path, lambda h: answer(h, b'{"id":123456789012345678901234567890}'),
                 uid=os.getuid() + 1234) as (proxy, _):
        proxy = Proxy(proxy.config, secret)
        result = request(proxy, "board.card.get", {"id": int(secret)})
        assert result["error"] == "upstream_error"
        assert secret not in json.dumps(result)
        assert secret not in proxy.config.audit_path.read_text()


def test_proxy_refuses_startup_when_the_audit_file_has_unsafe_permissions(tmp_path):
    from pathlib import Path
    token, audit, endpoint = tmp_path / "peer_token", tmp_path / "audit", short_socket(tmp_path)
    token.write_text("fake-startup-token")
    token.chmod(0o600)
    audit.write_text("")
    audit.chmod(0o600)  # Contract requires the provisioned audit file to be 0640.
    script = f'''from pathlib import Path
from swarph_cli.local_proxy import Config, serve
from swarph_cli.proxy_routes import ProxyError
try:
    serve(Config("alice", {os.getuid()+1234}, "http://127.0.0.1:1", Path({str(token)!r}), Path({str(endpoint)!r}), Path({str(audit)!r})))
except ProxyError as error:
    print(error.error)
    raise SystemExit(2)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    try:
        process = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("proxy accepted requests without a usable audit file")
    finally:
        endpoint.unlink(missing_ok=True)
    assert process.returncode == 2 and process.stdout.strip() == "proxy_unavailable"
    assert audit.read_text() == ""
