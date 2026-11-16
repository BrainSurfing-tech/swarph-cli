"""Card #1620 (obligation #5535): follow-ups to the PR #743 proxy block.

Items 1-3: an ``inbox_blocked_oversized_newest`` episode clears through an
``unread_only`` probe once the oversized row is marked read, a blocked poll
costs at most two inbox reads and logs nothing new, and older undelivered
rows are delivered through that probe or recorded as stranded.
Item 4: can-fail mutants for "blocked file never cleared" and "a gateway
error mid-search treated as a cap".
Item 5: ``monitor status`` surfaces the blocked state while not running.
Item 6: pins for PR #743's two surviving cap-flag mutants in
``local_proxy.call_gateway`` (no-Content-Length oversize; length mismatch).

Direct mode is untouched: every new branch is driven only by the typed
``proxy_cap_exceeded`` marker the proxy transport adds, which direct
``_http_get_json`` results never carry.
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
from swarph_cli.commands import mesh, monitor
from swarph_cli.local_proxy import Config, Proxy
from swarph_cli.proxy_routes import ProxyError

BIG = "\x01" * 180_000


def _msg(i, text=None, read=False):
    row = {"id": i, "from_node": "bob", "to_node": "alice", "kind": "fyi",
           "content": text if text is not None else f"message {i}"}
    if read:
        row["read_at"] = "2026-01-01T00:00:00Z"
    return row


@pytest.fixture
def gateway():
    calls = []
    state = SimpleNamespace(messages=[], fail_limits=set(), fail_all=None)

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path == "/messages" and state.fail_all:
                raw = json.dumps({"detail": "boom"}).encode()
                self.send_response(state.fail_all)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parts.path == "/messages":
                limit = int(query.get("limit", ["50"])[0])
                if limit in state.fail_limits:
                    raw = json.dumps({"detail": "boom"}).encode()
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                rows = sorted(state.messages, key=lambda m: m["id"], reverse=True)
                if query.get("unread_only") == ["true"]:
                    rows = [r for r in rows if not r.get("read_at")]
                rows = rows[:limit]
                body = {"messages": rows, "n": len(rows)}
            else:
                body = {}
            raw = json.dumps(body, ensure_ascii=True).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # the proxy refuses oversize bodies from the header alone

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


def _config(tmp_path, gateway):
    return Config("alice", os.getuid() + 1234, gateway.url,
                  tmp_path / "unused-token", tmp_path / "unused.sock",
                  tmp_path / "audit", allow_loopback_tcp=True)


def _new_state(tmp_path, gateway):
    return mesh.MonitorState(
        self_name="alice", state_dir=tmp_path / "state", gateway=gateway.url,
        token="fixture-token", sinks=[mesh.parse_sink("pull")])


def _iterate(tmp_path, gateway, state):
    with proxy_monitor._private_transport(Proxy(_config(tmp_path, gateway),
                                                "fixture-token")):
        mesh._monitor_iteration(state)


def _inbox_ids(state):
    path = state.inbox_log_path
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text().splitlines()
            if line.strip()]


def _message_calls(gateway):
    return [c for c in gateway.calls if c.startswith("/messages?")]


# ── item 1 + 3 (deliver branch) + 4a ────────────────────────────────────────

def test_block_clears_when_oversized_row_marked_read(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    blocked = state.state_dir / "inbox_blocked.json"
    assert blocked.exists()
    assert _inbox_ids(state) == []
    capsys.readouterr()
    # The oversized newest row is marked read elsewhere; no newer DM arrives.
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG, read=True)]
    _iterate(tmp_path, gateway, state)
    # Item 4a mutant ("blocked file never cleared") fails the next line.
    assert not blocked.exists()
    # Item 1/3: normal delivery resumes through the unread-only probe and
    # delivers the older unread rows behind the oversized one.
    assert _inbox_ids(state) == [1, 2]
    # And it stays resumed: a later small DM is delivered the same way.
    gateway.state.messages.append(_msg(4))
    _iterate(tmp_path, gateway, state)
    assert _inbox_ids(state) == [1, 2, 4]
    assert not blocked.exists()


# ── item 2 ──────────────────────────────────────────────────────────────────

def test_blocked_poll_costs_two_reads_and_logs_nothing(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert (state.state_dir / "inbox_blocked.json").exists()
    capsys.readouterr()
    gateway.calls.clear()
    _iterate(tmp_path, gateway, state)
    calls = _message_calls(gateway)
    assert len(calls) <= 2  # the normal read + at most the unread_only probe
    assert any("unread_only=true" in c for c in calls[1:])
    err = capsys.readouterr().err
    assert err == ""  # no shrink/blocked line repeats within the episode
    assert (state.state_dir / "inbox_blocked.json").exists()


# ── item 3 (record branch) ──────────────────────────────────────────────────

def test_stranded_older_rows_recorded_once_in_blocked_file(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2)]
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert _inbox_ids(state) == [1, 2]
    capsys.readouterr()
    # Oversized unread newest arrives; the unread probe cannot exclude it,
    # so the already-observed but undelivered older rows are stranded.
    gateway.state.messages.append(_msg(3, BIG))
    _iterate(tmp_path, gateway, state)
    blocked = state.state_dir / "inbox_blocked.json"
    payload = json.loads(blocked.read_text())
    assert payload["state"] == "inbox_blocked_oversized_newest"
    assert payload["stranded_older_count"] == 2
    assert payload["stranded_older_ids"] == [1, 2]
    err = capsys.readouterr().err
    assert err.count("inbox_blocked_oversized_newest") == 1
    # Recorded/logged once per episode: a further blocked poll stays silent
    # and does not rewrite the stranded record away.
    _iterate(tmp_path, gateway, state)
    assert capsys.readouterr().err == ""
    assert json.loads(blocked.read_text())["stranded_older_ids"] == [1, 2]


# ── item 4b ─────────────────────────────────────────────────────────────────

def test_gateway_500_mid_search_is_not_treated_as_cap(tmp_path, gateway, capsys):
    # Oversized row is not newest, so the poll enters the shrink search;
    # the gateway then fails one probed window with a real 500.
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG), _msg(4)]
    gateway.state.fail_limits = {25}
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert not (state.state_dir / "inbox_blocked.json").exists()
    err = capsys.readouterr().err
    assert "gateway 500" in err
    assert "inbox_blocked_oversized_newest" not in err


def test_gateway_500_on_unread_probe_is_not_treated_as_cap(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert (state.state_dir / "inbox_blocked.json").exists()
    capsys.readouterr()
    gateway.state.fail_all = 500
    _iterate(tmp_path, gateway, state)
    err = capsys.readouterr().err
    assert "gateway 500" in err


# ── item 5 ──────────────────────────────────────────────────────────────────

def test_status_shows_inbox_blocked_when_not_running(tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _new_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    info = monitor._collect(SimpleNamespace(
        self_name="alice", state_dir=str(state.state_dir)))
    assert info["running"] is False
    assert info["inbox_blocked"]["state"] == "inbox_blocked_oversized_newest"
    monitor._print_status(info, 0)
    out = capsys.readouterr().out
    assert "not running" in out
    assert "inbox_blocked_oversized_newest" in out


# ── item 6: cap-flag pins at the two surviving-mutant sites ─────────────────

def _raw_gateway(tmp_path, respond):
    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = Config("alice", os.getuid() + 1234,
                    f"http://127.0.0.1:{server.server_port}",
                    tmp_path / "t", tmp_path / "s", tmp_path / "a",
                    allow_loopback_tcp=True)
    return server, thread, Proxy(config, "fixture-token")


def test_item6a_no_content_length_oversize_sets_cap_flag(tmp_path):
    # Site: call_gateway's body read exceeds MAX_LINE with NO Content-Length
    # header at all. Today's behaviour (pinned): cap_exceeded IS set -- this
    # is a genuine size refusal. Removing the flag here fails this test.
    def respond(h):
        h.send_response(200)
        h.end_headers()
        h.wfile.write(b'{"x":"' + b"A" * 1_100_000 + b'"}')
        h.close_connection = True

    server, thread, proxy = _raw_gateway(tmp_path, respond)
    try:
        with pytest.raises(ProxyError) as caught:
            proxy.call_gateway("GET", "/whoami")
        assert caught.value.error == "upstream_error"
        assert getattr(caught.value, "cap_exceeded", False) is True
        with proxy_monitor._private_transport(proxy):
            status, body = mesh._http_get_json(f"{proxy.config.gateway_url}/messages?to=alice&limit=50", "fixture-token")
        assert status == 502
        assert body.get("proxy_cap_exceeded") is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_item6b_content_length_mismatch_has_no_cap_flag(tmp_path):
    # Site: declared Content-Length vs actual bytes differ WITHOUT exceeding
    # the cap. Today's behaviour (pinned, and correct per the cap semantics:
    # a framing error is not a size refusal, so shrinking the window could
    # never fix it): upstream_error with NO cap_exceeded flag. Adding the
    # flag here fails this test.
    def respond(h):
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(b'{"ok":true}')
        h.close_connection = True

    server, thread, proxy = _raw_gateway(tmp_path, respond)
    try:
        with pytest.raises(ProxyError) as caught:
            proxy.call_gateway("GET", "/whoami")
        assert caught.value.error == "upstream_error"
        assert getattr(caught.value, "cap_exceeded", False) is False
        with proxy_monitor._private_transport(proxy):
            status, body = mesh._http_get_json(f"{proxy.config.gateway_url}/messages?to=alice&limit=50", "fixture-token")
        assert "proxy_cap_exceeded" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# ── direct mode untouched ───────────────────────────────────────────────────

def test_direct_mode_never_probes_unread(tmp_path, monkeypatch):
    seen = []

    def fake_get(url, token, **_kw):
        seen.append(url)
        return 200, {"messages": [_msg(1)], "n": 1}

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    state = _new_state(tmp_path, SimpleNamespace(url="http://gateway.invalid"))
    mesh._monitor_iteration(state)
    assert _inbox_ids(state) == [1]
    inbox_reads = [u for u in seen if "/messages?" in u]
    assert len(inbox_reads) == 1
    assert "unread_only" not in inbox_reads[0]
    assert all("unread_only" not in u for u in seen)
    assert not (state.state_dir / "inbox_blocked.json").exists()
