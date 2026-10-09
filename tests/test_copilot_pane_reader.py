"""Card #386 / row #1408: Copilot's composer is not Muse or Opencode.

Fixtures are real 1.0.94 capture-pane input blocks, without transcript or
credentials. Idle/draft frames came from our throwaway reader-fixture and
reader-mono windows; busy.txt came from our active copilot pane. Only the
human-draft and quote tests transform these captures.
"""

import json
from pathlib import Path
import subprocess

import pytest

from swarph_cli.commands import mesh


FIXTURES = Path(__file__).parent / "fixtures" / "copilot-386"


def frame(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "name,composer,running",
    [
        ("idle-box.txt", "clear", False),
        ("idle.txt", "clear", False),
        ("busy.txt", "clear", True),
        ("pending-box.txt", "wake", False),
        ("pending.txt", "wake", False),
    ],
)
def test_real_copilot_frames(monkeypatch, name, composer, running):
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda _: frame(name))
    assert mesh._composer_state("copilot") == composer
    assert mesh._agent_running("copilot") is running
    assert mesh._wake_prompt_for(frame(name)) == "check mesh: swarph_dm_unread"


class State:
    gateway = "http://test.invalid"
    self_name = "copilot"
    token = "test-only"

    def __init__(self):
        self.ledgers = {}

    def ledger(self, name):
        return self.ledgers.setdefault(name, {})


def rig(monkeypatch, captures):
    calls = []
    frames = iter(captures)
    current = captures[-1]

    def run(argv, **kwargs):
        nonlocal current
        if argv[1] == "capture-pane":
            current = next(frames, current)
            return subprocess.CompletedProcess(
                argv, 0 if current is not None else 1,
                stdout="\n".join(current or []), stderr="",
            )
        if argv[1] == "display-message":
            return subprocess.CompletedProcess(argv, 0, stdout="1", stderr="")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _: None)
    return calls


def test_busy_turn_defers_without_keys(monkeypatch):
    calls = rig(monkeypatch, [frame("busy.txt")])
    assert mesh.TmuxSink("copilot").deliver(State(), [{"id": 1}], 1) is None
    assert calls == []


def test_landed_wake_is_verified_and_not_reinjected(monkeypatch):
    idle = frame("idle-box.txt")
    calls = rig(monkeypatch, [idle] * 5 + [frame("busy.txt")])
    state = State()
    sink = mesh.TmuxSink("copilot")
    assert sink.deliver(state, [{"id": 1}], 1) is True
    injected = [c for c in calls if "-l" in c]
    assert len(injected) == 1
    assert injected[0][-1] == "check mesh: swarph_dm_unread"
    # The monitor advances the sink cursor only after this verified delivery.
    state.ledger(sink.name)["last_delivered_id"] = 1
    assert sink.deliver(state, [{"id": 2}], 2) is None
    assert [c for c in calls if "-l" in c] == injected


@pytest.mark.parametrize("idle_name", ["idle.txt", "idle-box.txt"])
def test_engine_persists_delivery_and_does_not_retry(monkeypatch, tmp_path, idle_name):
    from swarph_cli.commands import watchdog

    idle = frame(idle_name)
    calls = rig(monkeypatch, [idle] * 4 + [frame("busy.txt")])
    monkeypatch.setattr(watchdog, "_gateway_unread_count", lambda *args: 1)
    sink = mesh.TmuxSink("copilot")
    state = mesh.MonitorState(
        self_name="copilot", state_dir=tmp_path, gateway="http://test.invalid",
        token="test-only", sinks=[sink], min_interval_s=0,
    )
    state.observed["last_msg_id"] = 1
    state.inbox_log_path.write_text('{"id": 1}\n', encoding="utf-8")
    mesh._monitor_deliver(state)
    led = state.ledger(sink.name)
    assert led["last_delivered_id"] == 1
    assert led["consecutive_failures"] == 0
    assert state.deliveries == {sink.name: 1}
    saved = json.loads(state.ledgers_path.read_text(encoding="utf-8"))
    assert saved[sink.name]["last_delivered_id"] == 1
    delivered_keys = list(calls)
    for _ in range(4):
        mesh._monitor_deliver(state)
    assert calls == delivered_keys


def test_unreadable_pane_never_receives_keys_on_repeated_polls(monkeypatch):
    calls = rig(monkeypatch, [None])
    state = State()
    sink = mesh.TmuxSink("copilot")
    for _ in range(4):
        assert sink.deliver(state, [{"id": 1}], 1) is False
    assert calls == []
    assert not state.ledger(sink.name).get("last_delivered_id")


def test_human_draft_is_not_an_idle_composer(monkeypatch):
    lines = frame("pending-box.txt")
    lines = [ln.replace("check mesh: swarph_dm_unread", "my unfinished draft")
             for ln in lines]
    calls = rig(monkeypatch, [lines])
    assert mesh.TmuxSink("copilot").deliver(State(), [{"id": 1}], 1) is None
    assert calls == []


@pytest.mark.parametrize("name", ["idle.txt", "idle-box.txt"])
def test_incomplete_copilot_frame_fails_closed(monkeypatch, name):
    lines = frame(name)
    lines = [ln for ln in lines if ln.strip()][:-1]
    calls = rig(monkeypatch, [lines])
    assert mesh._composer_state("copilot") is None
    assert mesh._agent_running("copilot") is None
    assert mesh.TmuxSink("copilot").deliver(State(), [{"id": 1}], 1) is False
    assert calls == []


def test_copilot_quote_above_cursor_does_not_take_over(monkeypatch):
    lines = frame("idle.txt") + [
        "→ Add a follow-up          ctrl+c to stop", "model · 50%", "~",
    ]
    rig(monkeypatch, [lines])
    assert mesh._copilot_frame(lines) is None
    assert mesh._composer_state("cursor") == "clear"
    assert mesh._agent_running("cursor") is True
