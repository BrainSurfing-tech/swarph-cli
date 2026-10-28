"""Card #513 — the per-cell duplicate-session check: TWO live `swarph monitor`
sessions under one SWARPH_SELF raise ONE alarm row, and nothing is ever killed.

Fails on origin/main, where `swarph_cli.duplicate_session` does not exist (the
import below is a collection error), and passes with the change.
"""
from __future__ import annotations

import json

import swarph_cli.duplicate_session as ds


def _monitor(cell, *extra):
    return ["swarph", "monitor", "start", "--as", cell, *extra]


# ── detection is PURE: a (pid, argv) listing in, pids out ───────────────────


def test_a_second_monitor_for_the_same_cell_is_a_duplicate():
    entries = [
        (100, _monitor("opencode")),
        (200, _monitor("opencode")),          # the second live session
    ]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=100) == [200]


def test_the_monitor_itself_is_never_counted():
    """A lone monitor reports nothing — the alarm must name a SECOND session, not
    the asker. Two sessions is the fact; one is not."""
    entries = [(100, _monitor("opencode"))]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=100) == []


def test_another_cells_monitor_is_not_a_duplicate():
    entries = [(100, _monitor("opencode")), (200, _monitor("lab-ovh"))]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=100) == []


def test_a_non_monitor_swarph_process_is_not_a_duplicate():
    entries = [
        (100, _monitor("opencode")),
        (200, ["swarph", "mesh", "inbox", "--as", "opencode"]),
        (300, ["swarph", "monitor", "status", "--as", "opencode"]),
    ]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=100) == []


def test_both_as_spellings_name_the_cell():
    assert ds.is_monitor_for(_monitor("opencode"), "opencode")
    assert ds.is_monitor_for(
        ["swarph", "monitor", "start", "--as=opencode"], "opencode")
    assert not ds.is_monitor_for(
        ["swarph", "monitor", "start", "--as=other"], "opencode")


def test_a_prompt_that_quotes_the_command_is_not_the_process():
    """A process that merely QUOTES the command (a prompt, a log line) is not the
    monitor, so it must not be counted as a duplicate."""
    entries = [(100, ["bash", "-c", "swarph monitor start --as opencode"])]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=1) == []


def test_python_interpreter_prefixed_monitor_counts():
    entries = [
        (100, _monitor("opencode")),
        (200, ["python3", "/opt/swarph/swarph", "monitor", "start", "--as", "opencode"]),
    ]
    assert ds.duplicate_monitor_pids(entries, "opencode", my_pid=100) == [200]


def test_ps_output_is_parsed_into_pid_argv(monkeypatch):
    out = (
        "  PID ARGS\n"
        "  100 swarph monitor start --as opencode\n"
        "  200 swarph monitor start --as opencode --deliver tmux:opencode\n"
        "  300 swarph monitor status --as opencode\n"
    )

    class _P:
        stdout = out

    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: _P())
    entries = ds.ps_entries()
    assert (100, ["swarph", "monitor", "start", "--as", "opencode"]) in entries
    assert len(entries) == 3


# ── the alarm: ONE deduped row on card #513, and NEVER a kill ──────────────


class _Board:
    """card #513's obligations. A second ask while one is open is the defect."""

    def __init__(self):
        self.rows = []
        self.calls = []
        self.next_id = 1

    def handle(self, req, timeout=None):
        url = req.full_url
        self.calls.append((req.get_method(), url, req.data))

        class _Resp:
            def __init__(self, raw):
                self.status = 200
                self._raw = raw

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        if req.get_method() == "GET" and "obligations?" in url:
            want = "fallback_fired" if "status=fallback_fired" in url else "open"
            live = [r for r in self.rows if r["status"] == want]
            return _Resp(json.dumps({"obligations": live}).encode())
        if req.get_method() == "POST" and url.endswith("/ask"):
            body = json.loads(req.data.decode())
            row = {"id": self.next_id, "status": "open",
                   "holder": body["holder"], "what": body["what"],
                   "accept": body["accept"]}
            self.next_id += 1
            self.rows.append(row)
            return _Resp(json.dumps({"id": row["id"]}).encode())
        if "/process-instances" in url:
            return _Resp(b"{}")
        if req.get_method() == "POST" and "/close" in url:
            oid = int(url.rstrip("/").split("/")[-2])
            for row in self.rows:
                if row["id"] == oid:
                    row["status"] = "closed"
            return _Resp(b"{}")
        raise AssertionError(url)


class _State:
    self_name = "opencode"
    gateway = "http://gw"
    token = "tok"


def test_check_raises_exactly_one_row_for_a_duplicate(monkeypatch):
    board = _Board()
    monkeypatch.setattr(ds.urllib.request, "urlopen", board.handle)
    monkeypatch.setattr(ds, "ps_entries", lambda: [
        (100, _monitor("opencode")), (200, _monitor("opencode"))])

    assert ds.check(_State(), my_pid=100) == [200]
    assert ds.check(_State(), my_pid=100) == [200]        # second tick: no new row

    asks = [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]
    assert len(asks) == 1, "a second ask while the row is open is the defect"
    assert str(ds.CARD) in asks[0][1]
    body = json.loads(asks[0][2].decode())
    assert body["created_by"] == "opencode"
    assert "cell=opencode " in body["accept"]
    assert "200" in body["what"]
    assert body["holder"] != "opencode", "a cell cannot be told what it cannot receive"


def test_a_pid_leaving_ps_is_not_a_confirmed_exit(monkeypatch):
    """Card #1331. Disappearance used to close the alarm. It stays open.
    Confirmed exit is a different call."""
    board = _Board()
    monkeypatch.setattr(ds.urllib.request, "urlopen", board.handle)

    monkeypatch.setattr(ds, "ps_entries", lambda: [
        (100, _monitor("opencode")), (200, _monitor("opencode"))])
    ds.check(_State(), my_pid=100)
    assert [r["status"] for r in board.rows] == ["open"]

    monkeypatch.setattr(ds, "ps_entries", lambda: [(100, _monitor("opencode"))])
    assert ds.check(_State(), my_pid=100) == []
    assert [r["status"] for r in board.rows] == ["open"]
    closes = [c for c in board.calls if c[0] == "POST" and "/close" in c[1]]
    assert closes == []


def test_a_one_shot_is_not_a_duplicate_and_a_resident_is(monkeypatch):
    board = _Board()
    monkeypatch.setattr(ds.urllib.request, "urlopen", board.handle)
    monkeypatch.setattr(ds, "ps_entries", lambda: [
        (100, _monitor("opencode")),
        (200, ["swarph", "mesh", "wait", "--as", "opencode"]),
        (201, ["swarph", "mesh", "send", "--as", "opencode"]),
        (202, ["swarph", "board", "obligations", "list", "--as", "opencode"]),
        (203, ["swarph", "mcp-server", "--as", "opencode"]),
        (204, ["swarph", "channel-serve", "--as", "opencode"]),
        (205, ["python3", "-m", "swarph_desktop", "--as", "opencode"]),
    ])
    assert ds.check(_State(), my_pid=100) == [203, 204, 205]


def _drive(monkeypatch, state, *, horizon):
    """Run the loop on a fake clock. Stop once `horizon` seconds have been slept."""
    import swarph_cli.commands.mesh as mesh

    clock = {"t": 0.0}
    polls = {"n": 0}
    claims = {"n": 0}
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        clock["t"] += float(seconds)
        if clock["t"] >= horizon:
            state.shutdown_requested = True

    def _poll(state, **kwargs):
        polls["n"] += 1

    real_claim = ds.claim_if_due

    def _claim(state, now=None):
        ok = real_claim(state, now)
        if ok:
            claims["n"] += 1
        return ok

    monkeypatch.setattr(mesh.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ds.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mesh.time, "sleep", _sleep)
    monkeypatch.setattr(mesh, "_monitor_iteration", _poll)
    monkeypatch.setattr(ds, "claim_instance", lambda *args, **kwargs: True)
    monkeypatch.setattr(ds, "confirm_exit", lambda *args, **kwargs: True)
    monkeypatch.setattr(ds, "claim_if_due", _claim)
    mesh._monitor_loop(state)
    return polls["n"], claims["n"], slept


def test_a_long_poll_still_claims_within_90_and_does_not_shorten_the_poll(monkeypatch):
    """--poll-s 300 over 270s is one inbox poll and at least three claims.
    A 300s outage backoff is the poll deadline, not a 90s poll."""
    import swarph_cli.commands.mesh as mesh

    class _S:
        shutdown_requested = False
        log_prefix = "t"
        self_name = "opencode"
        sinks = []
        gateway = "http://127.0.0.1:9"
        token = "t"
        state_dir = "/tmp"
        poll_s = 300
        disconnect_since = None
        consecutive_empty = 0

    polls, claims, slept = _drive(monkeypatch, _S(), horizon=270)
    assert polls == 1
    assert claims >= 3
    assert slept
    assert all(s <= 90 for s in slept)

    class _Back:
        shutdown_requested = False
        log_prefix = "t"
        self_name = "opencode"
        sinks = []
        gateway = "http://127.0.0.1:9"
        token = "t"
        state_dir = "/tmp"
        poll_s = 30
        disconnect_since = mesh.time.time() - mesh._BACKOFF_5XX_THRESHOLD_SECONDS - 5
        consecutive_empty = 0

    polls, claims, slept = _drive(monkeypatch, _Back(), horizon=270)
    assert mesh._select_next_poll_seconds(_Back()) == mesh._BACKOFF_5XX_SECONDS
    assert polls == 1, "a 300s backoff was shortened"
    assert claims >= 3
    assert all(s <= 90 for s in slept)


def test_the_alarm_names_a_fresh_instance_id(monkeypatch):
    monkeypatch.setenv("SWARPH_RESUME_SESSION", "resumed-session-1331")
    board = _Board()
    monkeypatch.setattr(ds.urllib.request, "urlopen", board.handle)
    monkeypatch.setattr(ds, "ps_entries", lambda: [
        (100, _monitor("opencode")), (200, _monitor("opencode"))])
    ds.check(_State(), my_pid=100)
    asks = [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]
    body = json.loads(asks[0][2].decode())
    assert "instance_id=" in body["what"]
    assert "resumed-session-1331" not in body["what"]
    claims = [c for c in board.calls if "/process-instances/claim" in c[1]]
    assert len(claims) == 1


def test_check_never_kills_anything(monkeypatch):
    """The card's lean is unambiguous: NEVER KILL ANYTHING. The check may only
    raise a row. If any path signals a pid, this fails."""
    board = _Board()
    monkeypatch.setattr(ds.urllib.request, "urlopen", board.handle)
    monkeypatch.setattr(ds, "ps_entries", lambda: [
        (100, _monitor("opencode")), (200, _monitor("opencode"))])
    killed = []
    monkeypatch.setattr(ds.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert ds.check(_State(), my_pid=100) == [200]
    assert killed == [], "the check signalled a process; the lean forbids killing"


def test_enumeration_failure_is_not_a_duplicate(monkeypatch):
    """An enumeration that cannot run is not a duplicate and must not raise."""
    monkeypatch.setattr(ds, "ps_entries", lambda: [])
    assert ds.check(_State(), my_pid=100) == []


# ── the monitor loop wires it in, throttled, off the first tick ─────────────


def test_sigterm_sigint_and_monitor_stop_confirm_the_exit():
    """A real monitor subprocess, stopped three ways, writes confirmed_exit."""
    import sys
    if sys.platform != "linux":
        import pytest
        pytest.skip("monitor stop delivers SIGTERM, which this test signals on Linux")

    import os
    import threading
    import subprocess
    import time
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    rows = {}
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"messages": [], "channels": [], "peers": []})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            parts = urllib.parse.unquote(self.path.split("?", 1)[0]).strip("/").split("/")
            if len(parts) == 2 and parts[1] == "claim":
                iid = json.loads(raw.decode() or "{}")["instance_id"]
                with lock:
                    rows[iid] = {"status": "reporting", "exited_at": None}
                self._send(201, {"status": "reporting"})
                return
            if len(parts) == 3 and parts[0] == "process-instances" and parts[2] == "exit":
                iid = parts[1]
                with lock:
                    row = rows.get(iid)
                    if row is None:
                        self._send(404, {"detail": "unknown"})
                        return
                    row["status"] = "confirmed_exit"
                    row["exited_at"] = time.time()
                self._send(200, {"status": "confirmed_exit"})
                return
            self._send(404, {"detail": "no such route"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    gateway = f"http://{host}:{port}"
    root = Path(__file__).resolve().parents[1]
    src = str(root / "src")
    try:
        for how in ("SIGTERM", "SIGINT", "monitor-stop"):
            _stop_one(how, gateway, src, rows, lock)
    finally:
        server.shutdown()
        server.server_close()


def _stop_one(how, gateway, src, rows, lock):
    import os
    import signal
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        token_file = Path(tmp) / "token"
        token_file.write_text("scratch\n", encoding="utf-8")
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        before = set(rows)
        proc = subprocess.Popen(
            [sys.executable, "-m", "swarph_cli", "monitor", "start",
             "--foreground", "--as", "cell-3236",
             "--state-dir", str(state), "--gateway", gateway,
             "--token-file", str(token_file), "--deliver", "none",
             "--poll-s", "300"],
            cwd=src and str(Path(src).parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        iid = None
        try:
            deadline = time.time() + 15
            while time.time() < deadline and iid is None:
                with lock:
                    fresh = [k for k in rows if k not in before]
                if fresh:
                    iid = fresh[-1]
                    break
                if proc.poll() is not None:
                    err = proc.stderr.read().decode("utf-8", "replace")[-500:]
                    raise AssertionError(f"monitor exited before claim: {err}")
                time.sleep(0.05)
            assert iid, "monitor never claimed an instance"
            if how == "monitor-stop":
                stop = subprocess.run(
                    [sys.executable, "-m", "swarph_cli", "monitor", "stop",
                     "--as", "cell-3236", "--state-dir", str(state),
                     "--gateway", gateway, "--token-file", str(token_file)],
                    cwd=str(Path(src).parent),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert stop.returncode == 0, stop.stderr[-400:]
            else:
                proc.send_signal(signal.SIGTERM if how == "SIGTERM" else signal.SIGINT)
            deadline = time.time() + 15
            row = {}
            while time.time() < deadline:
                with lock:
                    row = dict(rows.get(iid) or {})
                if row.get("status") == "confirmed_exit" and row.get("exited_at"):
                    return
                if proc.poll() is not None:
                    time.sleep(0.3)
                    with lock:
                        row = dict(rows.get(iid) or {})
                    break
                time.sleep(0.05)
            assert row.get("status") == "confirmed_exit", (how, row)
            assert row.get("exited_at"), (how, row)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_a_clean_monitor_return_confirms_exit():
    import inspect
    import swarph_cli.commands.mesh as mesh
    src = inspect.getsource(mesh._monitor_loop)
    assert "confirm_exit" in src


def test_the_check_is_throttled_off_the_first_tick(monkeypatch):
    import swarph_cli.commands.mesh as mesh

    seen = []
    monkeypatch.setattr(ds, "check", lambda state: seen.append(state))

    class _S:
        iterations = 1

    mesh._maybe_duplicate_check(_S())
    assert seen == [], "tick 1 must stay clean for ordinary monitor tests"
    _S.iterations = ds.CHECK_EVERY
    mesh._maybe_duplicate_check(_S())
    assert len(seen) == 1, "every CHECK_EVERY ticks runs the check once"
