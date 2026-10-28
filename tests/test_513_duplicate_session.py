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


def test_a_long_poll_still_wakes_within_90_seconds(monkeypatch):
    import swarph_cli.commands.mesh as mesh

    slept = []
    monkeypatch.setattr(mesh.time, "sleep", lambda seconds: slept.append(seconds))

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

    def _stop(state, **kwargs):
        state.shutdown_requested = True

    monkeypatch.setattr(mesh, "_monitor_iteration", _stop)
    mesh._monitor_loop(_S())
    assert slept
    assert slept[0] <= 90


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
