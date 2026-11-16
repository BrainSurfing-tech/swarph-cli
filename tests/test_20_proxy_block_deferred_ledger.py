"""Card #1635 (obligation #5622): deferred wake + pull ledger window.

D2: while the oversized DM is the NEWEST row and unread (the block just
declared, no newer DM delivered), each poll must still run the delivery
step (_monitor_deliver) and write the drain heartbeat -- so a wake
deferred by the idle guard just before the block is sent on the first
idle poll after the guard expires, not when the next DM arrives.

D1: on idle blocked polls the pull sink must see the WHOLE delivered
window, not only the newest row, so out-of-order acknowledgements move
the pull ledger exactly as on main: an acknowledgement of only the
newer delivered DM never passes the older unread delivered DM, and an
acknowledgement of only the older one advances the ledger to it.
A pin on the ledger state BEFORE any acknowledgement kills the
empty-window mutant (passing [] to the pull sink would jump the
ledger straight to the observation cursor).
"""
import sys

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("proxy monitor is POSIX-only", allow_module_level=True)

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from swarph_cli import proxy_monitor
from swarph_cli.commands import mesh
from swarph_cli.local_proxy import Config, Proxy

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
    state = SimpleNamespace(messages=[])

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path == "/messages":
                limit = int(query.get("limit", ["50"])[0])
                rows = sorted(state.messages, key=lambda m: m["id"],
                              reverse=True)
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
                pass

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
            url=f"http://127.0.0.1:{server.server_port}", calls=calls,
            state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _iterate(tmp_path, gateway, state):
    config = Config("alice", os.getuid() + 1234, gateway.url,
                    tmp_path / "unused-token", tmp_path / "unused.sock",
                    tmp_path / "audit", allow_loopback_tcp=True)
    with proxy_monitor._private_transport(Proxy(config, "fixture-token")):
        mesh._monitor_iteration(state, poll_channels=False)


def _pull_state(tmp_path, gateway):
    return mesh.MonitorState(
        self_name="alice", state_dir=tmp_path / "state", gateway=gateway.url,
        token="fixture-token", sinks=[mesh.parse_sink("pull")])


def _inbox_ids(state):
    path = state.inbox_log_path
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text().splitlines()
            if line.strip()]


def _message_calls(gateway):
    return [c for c in gateway.calls if c.startswith("/messages?")]


def _limits(gateway):
    return [int(parse_qs(urlsplit(c).query)["limit"][0])
            for c in _message_calls(gateway)]


class _Push(mesh.Sink):
    is_push = True

    def __init__(self):
        super().__init__("probe-push")
        self.sent = []

    def deliver(self, state, dms, up_to_id):
        self.sent.append(up_to_id)
        return True


# ── D2 ────────────────────────────────────────────────────────────────

def test_d2_deferred_wake_sent_on_first_idle_poll_after_guard(
        tmp_path, gateway, capsys, monkeypatch):
    push = _Push()
    state = mesh.MonitorState(
        self_name="alice", state_dir=tmp_path / "state", gateway=gateway.url,
        token="fixture-token", sinks=[push], min_interval_s=60)
    gateway.state.messages = [_msg(1)]
    _iterate(tmp_path, gateway, state)
    assert push.sent == [1]
    gateway.state.messages.append(_msg(2))
    _iterate(tmp_path, gateway, state)
    assert _inbox_ids(state) == [1, 2]
    assert push.sent == [1]  # DM 2's wake deferred by the idle guard

    delivered = []
    real_deliver = mesh._monitor_deliver

    def _spy(deliver_state):
        delivered.append(deliver_state.iterations)
        return real_deliver(deliver_state)

    monkeypatch.setattr(mesh, "_monitor_deliver", _spy)
    gateway.state.messages.append(_msg(3, BIG))
    _iterate(tmp_path, gateway, state)
    blocked = state.state_dir / "inbox_blocked.json"
    assert blocked.exists()
    # The declaring poll itself must run the delivery step and heartbeat.
    assert delivered == [state.iterations]
    heartbeat = state.state_dir / "drain_heartbeat.json"
    assert json.loads(heartbeat.read_text())["iterations"] == \
        state.iterations

    # Guard expires; no newer DM arrives. The deferred wake for DM 2
    # must be sent on this first idle poll, not on the next DM.
    led = state.ledger("probe-push")
    led["last_delivery_at"] = time.time() - 120
    gateway.calls.clear()
    capsys.readouterr()
    _iterate(tmp_path, gateway, state)
    assert push.sent == [1, 2]
    assert delivered[-1] == state.iterations
    assert json.loads(heartbeat.read_text())["iterations"] == \
        state.iterations
    assert blocked.exists()
    assert len(_message_calls(gateway)) <= 3
    assert capsys.readouterr().err == ""
    _iterate(tmp_path, gateway, state)
    assert push.sent == [1, 2]  # no duplicate wake on later idle polls


# ── D1 ────────────────────────────────────────────────────────────────

def _blocked_with_4_and_5_delivered(tmp_path, gateway):
    gateway.state.messages = [_msg(1), _msg(2), _msg(3, BIG)]
    state = _pull_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert (state.state_dir / "inbox_blocked.json").exists()
    gateway.state.messages += [_msg(4), _msg(5)]
    _iterate(tmp_path, gateway, state)
    assert _inbox_ids(state) == [4, 5]
    return state


@pytest.mark.parametrize("ack_first,expected", [(5, 0), (4, 4)])
def test_d1_idle_blocked_poll_sees_whole_delivered_window(
        tmp_path, gateway, ack_first, expected):
    state = _blocked_with_4_and_5_delivered(tmp_path, gateway)
    gateway.calls.clear()
    _iterate(tmp_path, gateway, state)  # idle, before any acknowledgement
    # Pin (kills the empty-window mutant): before any ACK the pull
    # ledger must NOT have jumped to the observation cursor (5).
    assert state.ledger("pull")["last_delivered_id"] == 0
    # The idle poll asked for the remembered delivered window (2),
    # not only limit=1, and stayed within the 3-read budget.
    assert 2 in _limits(gateway)
    assert len(_message_calls(gateway)) <= 3

    gateway.state.messages = [
        _msg(1), _msg(2), _msg(3, BIG),
        _msg(4, read=(ack_first == 4)), _msg(5, read=(ack_first == 5))]
    _iterate(tmp_path, gateway, state)  # idle, only one DM acknowledged
    # only-5-read never passes unread delivered DM 4 (stays at pin);
    # only-4-read advances exactly to 4.
    assert state.ledger("pull")["last_delivered_id"] == expected
    _iterate(tmp_path, gateway, state)
    assert state.ledger("pull")["last_delivered_id"] == expected


MED = "A" * 600_000  # fits alone (<1 MiB), two do not


# ── #5628 (a): capped unread probe -> limit-1 row is the window ──────

def test_a_capped_unread_probe_limit1_window_keeps_ledger_at_5(
        tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(i, read=True) for i in range(1, 6)]
    state = _pull_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert state.ledger("pull")["last_delivered_id"] == 5
    gateway.state.messages.append(_msg(6, BIG))
    _iterate(tmp_path, gateway, state)
    assert (state.state_dir / "inbox_blocked.json").exists()
    gateway.state.messages.append(_msg(7))
    _iterate(tmp_path, gateway, state)
    assert int(state.observed.get("last_msg_id", 0)) == 7
    assert state.ledger("pull")["last_delivered_id"] == 5
    # Idle: newest (7) already observed, remembered window is 1, and the
    # unread probe is capped (oversized 6 still unread), so the limit-1
    # row [7] is the window. Feeding [] instead would jump the ledger
    # to the cursor (7); the row itself is unread, so it must stay 5.
    for _ in range(2):
        gateway.calls.clear()
        capsys.readouterr()
        _iterate(tmp_path, gateway, state)
        assert state.ledger("pull")["last_delivered_id"] == 5
        assert len(_message_calls(gateway)) <= 3
        assert capsys.readouterr().err == ""


# ── #5628 (b): fully blocked (no-window) poll feeds no empty list ────

def test_b_fully_blocked_no_window_poll_keeps_ledger_at_7(
        tmp_path, gateway, capsys):
    gateway.state.messages = [_msg(i) for i in range(1, 11)]
    state = _pull_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    gateway.state.messages = [
        _msg(i, read=(i <= 7)) for i in range(1, 11)]
    _iterate(tmp_path, gateway, state)
    assert state.ledger("pull")["last_delivered_id"] == 7
    assert int(state.observed.get("last_msg_id", 0)) == 10
    # Stranded shape: 11, 12 and oversized 13 arrive before one poll.
    # Every read is capped, so this poll has no window at all; feeding
    # [] to the pull sink would jump the ledger to the cursor (10).
    gateway.state.messages += [_msg(11), _msg(12), _msg(13, BIG)]
    _iterate(tmp_path, gateway, state)
    blocked = state.state_dir / "inbox_blocked.json"
    assert blocked.exists()
    assert state.ledger("pull")["last_delivered_id"] == 7
    for _ in range(2):
        gateway.calls.clear()
        capsys.readouterr()
        _iterate(tmp_path, gateway, state)
        assert state.ledger("pull")["last_delivered_id"] == 7
        assert blocked.exists()
        assert len(_message_calls(gateway)) <= 3
        assert capsys.readouterr().err == ""


# ── #5628 (c): remembered-window read keeps the blocked episode ─────

def test_c_remembered_window_read_keeps_episode_next_idle_poll(
        tmp_path, gateway, capsys):
    state = _blocked_with_4_and_5_delivered(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)  # idle via remembered window (2)
    assert state.ledger("pull")["last_delivered_id"] == 0
    # The wide read unlinks the blocked file, so the episode now lives
    # only in _inbox_blocked_active. Dropping that flag here would send
    # the next poll into the window search (many reads, retry logs).
    gateway.calls.clear()
    capsys.readouterr()
    _iterate(tmp_path, gateway, state)
    assert _limits(gateway) == [50, 1, 2]
    assert capsys.readouterr().err == ""
    assert state._inbox_blocked_active is True
    assert state.ledger("pull")["last_delivered_id"] == 0


# ── #5628 (d): remembered window itself capped -> limit-1 fallback ──

def test_d_capped_remembered_window_falls_back_to_limit1_row(
        tmp_path, gateway, capsys):
    state = _blocked_with_4_and_5_delivered(tmp_path, gateway)
    assert state._inbox_blocked_window == 2
    # The two delivered rows no longer fit one window together (each
    # still fits alone), so the remembered limit-2 read is capped and
    # the poll must fall back to the limit-1 row [5], not to no window.
    gateway.state.messages = [
        _msg(1), _msg(2), _msg(3, BIG), _msg(4, MED), _msg(5, MED)]
    heartbeat = state.state_dir / "drain_heartbeat.json"
    for _ in range(2):
        gateway.calls.clear()
        capsys.readouterr()
        _iterate(tmp_path, gateway, state)
        # Row 5 is unread: ledger must not move (an empty/capped window
        # fed onward would jump it to the cursor, 5).
        assert state.ledger("pull")["last_delivered_id"] == 0
        assert _limits(gateway) == [50, 1, 2]
        assert capsys.readouterr().err == ""
        assert json.loads(heartbeat.read_text())["iterations"] == \
            state.iterations


def test_d1_ledger_at_3_stays_or_advances_with_out_of_order_acks(
        tmp_path, gateway):
    # Ledger legitimately at 3 (DMs 1..3 delivered and acknowledged),
    # then an oversized DM blocks and two newer DMs are delivered.
    gateway.state.messages = [_msg(1, read=True), _msg(2, read=True),
                              _msg(3, read=True)]
    state = _pull_state(tmp_path, gateway)
    _iterate(tmp_path, gateway, state)
    assert state.ledger("pull")["last_delivered_id"] == 3
    gateway.state.messages.append(_msg(4, BIG))
    _iterate(tmp_path, gateway, state)
    assert (state.state_dir / "inbox_blocked.json").exists()
    gateway.state.messages += [_msg(5), _msg(6)]
    _iterate(tmp_path, gateway, state)
    assert _inbox_ids(state)[-2:] == [5, 6]
    _iterate(tmp_path, gateway, state)  # idle, before any acknowledgement
    assert state.ledger("pull")["last_delivered_id"] == 3  # pin
    gateway.state.messages = [
        _msg(1, read=True), _msg(2, read=True), _msg(3, read=True),
        _msg(4, BIG), _msg(5), _msg(6, read=True)]
    _iterate(tmp_path, gateway, state)  # only newer delivered DM acked
    assert state.ledger("pull")["last_delivered_id"] == 3
