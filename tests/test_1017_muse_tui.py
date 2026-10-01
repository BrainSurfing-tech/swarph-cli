"""card #1017 — the muse 1.4.1 composer is a wake target.

Fixtures are capture-pane of a throwaway session `muse-1017-sac` running
muse-bin-1.4.1-R4503.1, never meta-muse. These assertions fail on main:
`_bottom_tui` does not return muse, so deliver sends nothing and the
failure line says the sink is probably gone.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import swarph_cli.commands.mesh as mesh

_FIX = Path(__file__).resolve().parent / "fixtures" / "muse-1017"


def _pane(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


class _State:
    def __init__(self, led):
        self.led = led
        self.gateway = "http://stub.invalid"
        self.self_name = "meta-muse"
        self.token = "tok"

    def ledger(self, _name):
        return self.led


def _record(monkeypatch, pane: str):
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, 0, stdout=pane, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    return calls


def _keys(calls):
    return [c for c in calls if len(c) > 1 and c[1] == "send-keys"]


def test_idle_empty_muse_composer_is_woken_once(monkeypatch):
    pane = _pane("idle.txt")
    assert mesh._bottom_tui(pane.splitlines()) == "muse"
    calls = _record(monkeypatch, pane)
    led = {}
    assert mesh.TmuxSink("muse-1017-sac").deliver(_State(led), [], 1) is True
    keys = _keys(calls)
    assert keys == [
        ["tmux", "send-keys", "-t", "muse-1017-sac", "-l", mesh._MUSE_WAKE_PROMPT],
        ["tmux", "send-keys", "-t", "muse-1017-sac", "Enter"],
    ]
    assert led["wake_outstanding"] is True
    assert "swarph_dm_unread" in mesh._MUSE_WAKE_PROMPT


def test_echo_model_label_is_still_muse_and_wakes_once(monkeypatch):
    """Drop #927: a status row that is not muse-spark must still be muse.
    Fails on main, where the status check requires the model name."""
    pane = _pane("idle.txt").replace("muse-spark-1.3-contributor", "echo")
    pane = pane.replace("echo · high · /tmp/muse-1017-ws · YOLO", "echo · ~/x · YOLO")
    assert "muse-spark" not in pane
    assert mesh._bottom_tui(pane.splitlines()) == "muse"
    calls = _record(monkeypatch, pane)
    assert mesh.TmuxSink("muse-1017-sac").deliver(_State({}), [], 1) is True
    sent = _keys(calls)
    assert sent[0][-1] == mesh._MUSE_WAKE_PROMPT
    assert "swarph_dm_unread" in sent[0][-1]
    assert sent[1:] == [["tmux", "send-keys", "-t", "muse-1017-sac", "Enter"]]


def test_muse_draft_defers_with_a_named_reason(monkeypatch, tmp_path, capsys):
    calls = _record(monkeypatch, _pane("draft.txt"))
    state = _Engine(tmp_path)
    state.sinks = [mesh.TmuxSink("muse-1017-sac")]
    mesh._monitor_deliver(state)
    assert _keys(calls) == []
    led = state.ledgers["tmux:muse-1017-sac"]
    assert led["last_delivered_id"] == 0
    assert led["consecutive_failures"] == 0
    out = capsys.readouterr()
    assert "composer holds human text" in out.out
    assert "DELIVERY FAILED" not in out.err


def test_muse_running_turn_defers_with_no_keystroke(monkeypatch, capsys):
    calls = _record(monkeypatch, _pane("running.txt"))
    assert mesh.TmuxSink("muse-1017-sac").deliver(_State({}), [], 1) is None
    assert _keys(calls) == []
    assert "muse turn in progress" in capsys.readouterr().out


def test_unrecognised_tui_names_itself(monkeypatch, tmp_path, capsys):
    calls = _record(monkeypatch, "ubuntu@box:~$\n$ \n")
    state = _Engine(tmp_path)
    sink = mesh.TmuxSink("muse-1017-sac")
    state.sinks = [sink]
    mesh._monitor_deliver(state)
    assert _keys(calls) == []
    err = capsys.readouterr().err
    assert "unrecognised TUI" in err
    assert "probably gone" not in err
    assert state.ledgers["tmux:muse-1017-sac"]["consecutive_failures"] == 1


class _Engine:
    def __init__(self, tmp_path):
        self.observed = {"last_msg_id": 5}
        self.min_interval_s = 0.0
        self.replay_limit = 50
        self.inbox_log_path = tmp_path / "inbox.log"
        self.inbox_log_path.write_text("", encoding="utf-8")
        self.log_prefix = "[test]"
        self.deliveries = {}
        self.ledgers = {}
        self.ledgers_path = tmp_path / "ledgers.json"
        self.sinks = []

    def ledger(self, name):
        return self.ledgers.setdefault(name, mesh._new_ledger())
