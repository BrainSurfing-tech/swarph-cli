"""Protected config and the public request boundary, with fixture credentials only."""
import json
import dataclasses
import os
from pathlib import Path
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix proxy")


@pytest.fixture
def tmp_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("proxy1539").resolve()
    path.chmod(0o700)
    return path


def protected_config(tmp_path, fields):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "cell_uid": os.getuid() + 1234, "gateway_url": "http://127.0.0.1:1234",
        "allow_loopback_tcp": True,
        "socket_path": str(tmp_path / "socket"), "audit_path": str(tmp_path / "audit"),
        **fields,
    }))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("platform, fields, expected, source", [
    ("darwin", {}, True, "default"), ("linux", {}, False, "default"),
    ("darwin", {"read_only": False}, False, "config"),
    ("darwin", {"read_only": True}, True, "config"),
    ("linux", {"read_only": True}, True, "config"),
])
def test_protected_mode_default_and_override(tmp_path, monkeypatch, capsys,
                                           platform, fields, expected, source):
    from swarph_cli import local_proxy
    path = protected_config(tmp_path, fields)
    monkeypatch.setattr(local_proxy.sys, "platform", platform)
    monkeypatch.setenv("SWARPH_PROXY_READ_ONLY", "false")
    monkeypatch.setenv("READ_ONLY", "false")
    config = local_proxy.load_config("alice", path)
    assert config.read_only is expected
    assert capsys.readouterr().err == f"proxy read_only={str(expected).lower()} source={source}\n"


@pytest.fixture
def gateway(tmp_path):
    from swarph_cli.local_proxy import Config, Proxy
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, json.loads(raw) if raw else None))
            body = json.dumps({"fixture": [1, "unchanged"], "stage_version": 1,
                               "thread_uuid": "11111111-1111-4111-8111-111111111111"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PATCH = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    config = Config("alice", os.getuid() + 1234,
                    f"http://127.0.0.1:{server.server_port}", tmp_path / "unused-token",
                    tmp_path / "socket", tmp_path / "audit", read_only=True, allow_loopback_tcp=True)
    try:
        yield Proxy(config, "fixture-only-credential-1539"), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(proxy, verb, args):
    return proxy.request({"request_id": str(uuid.uuid4()), "verb": verb, "args": args})


def test_read_only_refuses_send_without_any_gateway_call(gateway):
    proxy, calls = gateway
    reply = request(proxy, "mesh.send", {"to_node": "peer-two", "content": "fixture"})
    assert reply["error"] == "verb_not_allowed"
    assert calls == []


WRITES = [
    ("board.cards.say", {"id": 20, "content": "fixture"}),
    ("mesh.send", {"to_node": "peer-two", "content": "fixture"}),
    ("channel.post", {"channel": "fixture-room", "content": "fixture"}),
    ("mesh.mark_read", {"ids": [3, 9]}),
    ("board.obligations.take", {"id": 20, "expect_status": "open"}),
    ("board.obligations.decline", {"id": 20, "expect_status": "open", "why": "fixture"}),
    ("board.obligations.close", {"id": 20, "expect_status": "open",
                                "expect_accept_sha256": "a" * 64,
                                "outcome": "pass", "evidence": "fixture"}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "ready",
                         "expect_stage_version": 1}),
]
READS = [
    ("committee.motions.list", {"channel": "room-one"}),
    ("committee.motion.get", {"channel": "room-one", "id": 7}),
    ("whoami", {}), ("board.projects.list", {}),
    ("board.cards.list", {"project": 7, "stage": "ready", "assignee": "peer-two",
                          "label": "fixture", "q": "é &actor=other"}),
    ("board.card.get", {"id": 20}), ("board.cards.graph", {"id": 20}),
    ("board.cards.thread", {"id": 20, "limit": 5}),
    ("board.obligations.list", {"card_id": 20, "status": "open"}),
    ("mesh.peers", {"enabled_only": True}), ("rights", {}),
    ("mesh.inbox", {"unread_only": True, "limit": 3}),
    ("memory.get", {"slug": "fixture"}), ("memory.list", {"limit": 5}),
]


@pytest.mark.parametrize("verb, args", WRITES)
def test_each_write_is_refused_before_preparatory_reads(gateway, verb, args):
    proxy, calls = gateway
    assert request(proxy, verb, args)["error"] == "verb_not_allowed"
    assert calls == []
    # Prove the fixture reaches the existing route when the operator enables writes.
    proxy.config = dataclasses.replace(proxy.config, read_only=False)
    assert request(proxy, verb, args)["ok"] is True
    assert len(calls) == (2 if verb in {"board.cards.say", "board.cards.move"} else 1)


@pytest.mark.parametrize("verb, args", READS)
def test_each_read_is_unchanged_in_read_only_mode(gateway, verb, args):
    proxy, calls = gateway
    envelope = {"request_id": str(uuid.uuid4()), "verb": verb, "args": args}
    enabled = proxy.request(envelope)
    assert enabled["ok"] is True
    assert len(calls) == 1
    first = calls.copy()
    calls.clear()
    proxy.config = dataclasses.replace(proxy.config, read_only=False)
    assert proxy.request(envelope) == enabled
    assert calls == first


@pytest.mark.parametrize("verb", ["future.write", "board.cards.create", "Mesh.send",
                                  "mesh.send ", None, [], {}, True])
def test_anything_outside_the_read_set_is_refused(gateway, verb):
    proxy, calls = gateway
    assert request(proxy, verb, {})["error"] == "verb_not_allowed"
    assert calls == []


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_present_non_boolean_mode_refuses_startup(tmp_path, value, capsys):
    from swarph_cli.local_proxy import load_config
    from swarph_cli.proxy_routes import ProxyError
    with pytest.raises(ProxyError, match="^proxy_unavailable$"):
        load_config("alice", protected_config(tmp_path, {"read_only": value}))
    assert capsys.readouterr().err == ""


def test_caller_cannot_switch_mode_or_supply_a_route(gateway, monkeypatch):
    proxy, calls = gateway
    monkeypatch.setenv("READ_ONLY", "false")
    monkeypatch.setenv("SWARPH_PROXY_READ_ONLY", "false")
    args = {"to_node": "peer-two", "content": "fixture", "read_only": False}
    assert request(proxy, "mesh.send", args)["error"] == "verb_not_allowed"
    envelope = {"request_id": str(uuid.uuid4()), "verb": "whoami", "args": {},
                "read_only": False}
    assert proxy.request(envelope)["error"] == "bad_args"
    for key, value in [("read_only", False), ("path", "/messages"), ("actor", "other")]:
        assert request(proxy, "whoami", {key: value})["error"] == "bad_args"
    assert calls == []


def test_read_policy_is_explicit_and_covers_the_published_read_verbs():
    from swarph_cli.proxy_routes import READ_VERBS
    assert READ_VERBS == {verb for verb, _ in READS}


def test_launchd_template_documents_the_enforced_darwin_default():
    from swarph_cli.launchd_render import render_proxy_template
    template = Path(__file__).resolve().parents[1] / "src/swarph_cli/launchd/swarph-proxy.plist"
    assert "Darwin defaults read_only=true" in template.read_text()
    assert template.read_text() == render_proxy_template("swarph-proxy.plist")
