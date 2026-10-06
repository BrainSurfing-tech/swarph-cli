"""card #961 — grok 1.0.41 pane fixtures. These assertions fail on main:
main has no grok reader, so an idle ``│ ❯`` box is not a clear composer and
``Waiting for response`` is not a running turn.
"""
from pathlib import Path

from swarph_cli.commands import mesh

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "grok-715"


def lines(name):
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def test_idle_box_is_clear_and_not_running():
    pane = lines("idle.txt")
    assert mesh._is_grok_pane(pane)
    assert mesh._grok_composer_state(pane) == "clear"
    assert mesh._grok_running(pane) is False
    assert mesh._wake_prompt_for(pane) == "check mesh: swarph_dm_unread"


def test_typed_text_defers():
    pane = lines("typed.txt")
    assert mesh._grok_input(pane) == "draft-not-submitted"
    assert mesh._grok_composer_state(pane) == "busy"
    assert mesh._grok_running(pane) is False


def test_running_turn_is_not_idle():
    pane = lines("running.txt")
    assert mesh._grok_running(pane) is True
    # the box is empty while the turn runs; empty must not read as "inject"
    assert mesh._grok_input(pane) == ""


def test_finished_turn_reads_idle_so_the_next_dm_can_wake():
    pane = lines("finished.txt")
    assert "Worked for" in "\n".join(pane)
    assert mesh._grok_running(pane) is False
    # the submitted prompt stays in history with a bare ❯; the live box is empty
    assert mesh._grok_input(pane) == ""
    assert mesh._grok_composer_state(pane) == "clear"


def test_history_prompt_is_not_composer_text():
    pane = lines("finished.txt")
    assert any(ln.strip().startswith("❯ Reply") for ln in pane)
    assert mesh._grok_composer_state(pane) == "clear"


class _State:
    def __init__(self, led):
        self.led = led
        self.gateway = "http://stub.invalid"
        self.self_name = "grok-researcher"
        self.token = "tok"

    def ledger(self, _name):
        return self.led


def test_a_running_delivery_sends_zero_keys(monkeypatch):
    pane = lines("running.txt")
    keys = []
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda _t: pane)
    # #1100: the running classifier reads the RAW pane — same fixture,
    # blanks preserved (the grok fixtures carry none, so the view is
    # identical), so the rig scripts both seams.
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda _t: pane)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda _t: keys.append("wake") or True)
    monkeypatch.setattr(mesh, "_tmux_enter", lambda _t: keys.append("enter") or True)
    led = {"wake_outstanding": True}
    assert mesh.TmuxSink("grok-715-sac").deliver(_State(led), [], 1) is None
    assert keys == []
    assert led["wake_outstanding"] is True


def test_a_finished_clear_box_injects_again(monkeypatch):
    pane = lines("finished.txt")
    keys = []
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda _t: pane)
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda _t: pane)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda _t: keys.append("wake") or True)
    monkeypatch.setattr(mesh, "_tmux_enter", lambda _t: keys.append("enter") or True)
    monkeypatch.setattr(
        "swarph_cli.commands.watchdog._gateway_unread_count", lambda *_a: 1)
    led = {"wake_outstanding": True, "last_wake_injected_at": 1.0}
    assert mesh.TmuxSink("grok-715-sac").deliver(_State(led), [], 2) is True
    assert keys == ["wake"]
