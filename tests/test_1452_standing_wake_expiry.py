"""Card #1452: a standing wake EXPIRES in every sink that holds one.

Real sink classes (TmuxSink / MuseSink / CodexSink) with only their send
function patched and a fake clock. Sequence per sink: fresh send, hold
(owed, no send), drain progress (owed, no send, clock restarts), expiry
(exactly one fresh wake), rate bound (no second wake inside N), and
unreadable unread (owed, no send, logged).

Run: python -m pytest tests/test_1452_standing_wake_expiry.py -v
"""
import types

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog

N = getattr(mesh, "STANDING_WAKE_EXPIRY_S", 600.0)
T0 = 1_000_000.0


class _State:
    gateway = "http://gw:8788"
    self_name = "cell-1452"
    token = "tok"

    def __init__(self):
        self._ledgers = {}

    def ledger(self, name):
        return self._ledgers.setdefault(name, {
            "last_delivered_id": 0, "last_delivery_at": 0.0,
            "consecutive_failures": 0})


def _dm(i):
    return {"id": i, "from_node": "lab-ovh", "kind": "question",
            "content": "wake up", "read_at": None}


def _clock(monkeypatch):
    box = {"now": T0}
    fake = types.SimpleNamespace(
        time=lambda: box["now"], monotonic=lambda: box["now"],
        sleep=lambda s: None)
    monkeypatch.setattr(mesh, "time", fake)
    return box


def _run_sequence(deliver, sends, unread_box, clock, capsys):
    """Shared r1-r5 sequence; `deliver(i)` calls the real sink once."""
    # fresh wake
    assert deliver(1) is True and len(sends) == 1
    # r4: standing, no progress, inside N -> owed, no send, not delivered
    clock["now"] = T0 + 60
    assert deliver(2) is None and len(sends) == 1
    assert "owed" in capsys.readouterr().out
    # r2: drain progress restarts the clock -> still owed, no send,
    # even once the ORIGINAL wake is older than N
    unread_box["n"] = 2
    clock["now"] = T0 + N - 10
    assert deliver(3) is None and len(sends) == 1
    clock["now"] = T0 + N + 60
    assert deliver(4) is None and len(sends) == 1
    # r1: N without progress -> exactly one fresh wake, clock restarts
    clock["now"] = T0 + N - 10 + N + 1
    assert deliver(5) is True and len(sends) == 2
    # r5: at most one wake per N
    clock["now"] += 5
    assert deliver(6) is None and len(sends) == 2
    # r3: unread unreadable -> owed, no send, logged as unreadable
    unread_box["n"] = None
    clock["now"] += N + 10
    assert deliver(7) is None and len(sends) == 2
    assert "unreadable" in capsys.readouterr().out


def test_muse_sink_standing_wake_expires(monkeypatch, capsys):
    clock = _clock(monkeypatch)
    sends = []
    monkeypatch.setattr(mesh, "_muse_send",
                        lambda s, t: sends.append((s, t)) or (True, ""))
    unread_box = {"n": 3}
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread_box["n"])
    sink = mesh.MuseSink("sess-1452")
    state = _State()
    _run_sequence(lambda i: sink.deliver(state, [_dm(i)], i),
                  sends, unread_box, clock, capsys)
    assert state.ledger(sink.name)["last_delivered_id"] == 0


def test_codex_sink_standing_wake_expires(monkeypatch, capsys):
    clock = _clock(monkeypatch)
    sends = []
    monkeypatch.setattr(mesh, "_codex_send",
                        lambda s, t: sends.append((s, t)) or (True, ""))
    unread_box = {"n": 3}
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread_box["n"])
    sink = mesh.CodexSink("thread-1452")
    state = _State()
    _run_sequence(lambda i: sink.deliver(state, [_dm(i)], i),
                  sends, unread_box, clock, capsys)
    assert state.ledger(sink.name)["last_delivered_id"] == 0


def test_tmux_sink_standing_wake_expires(monkeypatch, capsys):
    clock = _clock(monkeypatch)
    sends = {"wake": 0, "enter": 0}

    def wake(target):
        sends["wake"] += 1
        return True

    def enter(target):
        sends["enter"] += 1
        return True

    monkeypatch.setattr(mesh, "_tmux_wake", wake)
    monkeypatch.setattr(mesh, "_tmux_enter", enter)
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: ["row"])
    monkeypatch.setattr(mesh, "_composer_state", lambda t: "clear")
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: T0 - 7200)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: False)
    monkeypatch.setattr(mesh, "_pane_in_mode", lambda t: False)
    monkeypatch.setattr(mesh, "_opencode_in_progress", lambda t, led=None: None)
    monkeypatch.setattr(mesh, "_grok_in_progress", lambda t: False)
    monkeypatch.setattr(mesh, "_opencode_turn_finished_target", lambda t: False)
    monkeypatch.setattr(mesh, "_grok_turn_finished_target", lambda t: False)
    unread_box = {"n": 3}
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: unread_box["n"])
    sink = mesh.TmuxSink("cell-1452:0.0")
    state = _State()
    sent_list = []

    def deliver(i):
        before = sends["wake"]
        out = sink.deliver(state, [_dm(i)], i)
        if sends["wake"] > before:
            sent_list.append(i)
        return out

    _run_sequence(deliver, sent_list, unread_box, clock, capsys)
    assert sends["enter"] == 0
    assert state.ledger(sink.name)["last_delivered_id"] == 0
