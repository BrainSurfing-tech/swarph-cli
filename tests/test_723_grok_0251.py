"""card #961 — grok 0.2.51 pane fixtures. These assertions fail on 87c8fba:
that head only treats ``Waiting for response`` as running and ``Worked for``
as finished, and an empty box beside a rate-limit line is a clear composer.
"""
from pathlib import Path

from swarph_cli.commands import mesh

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "grok-723"


def lines(name):
    return (FIX / name).read_text(encoding="utf-8").splitlines()


class _State:
    def __init__(self, led):
        self.led = led
        self.gateway = "http://stub.invalid"
        self.self_name = "grok-researcher"
        self.token = "tok"

    def ledger(self, _name):
        return self.led


def _deliver(monkeypatch, pane, led):
    keys = []
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda _t: pane)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda _t: keys.append("wake") or True)
    monkeypatch.setattr(mesh, "_tmux_enter", lambda _t: keys.append("enter") or True)
    outcome = mesh.TmuxSink("grok-723-sac").deliver(_State(led), [], 1)
    return outcome, keys


def test_idle_0251_box_is_clear():
    pane = lines("idle.txt")
    assert "Grok Build  0.2.51" in "\n".join(pane)
    assert mesh._is_grok_pane(pane)
    assert mesh._grok_block_reason(pane) is None
    assert mesh._grok_composer_state(pane) == "clear"
    assert mesh._grok_running(pane) is False
    assert mesh._wake_prompt_for(pane) == "check mesh: swarph_dm_unread"


def test_typed_0251_defers():
    pane = lines("typed.txt")
    assert mesh._grok_block_reason(pane) is None
    assert mesh._grok_input(pane) == "draft-not-submitted"
    assert mesh._grok_composer_state(pane) == "busy"
    assert mesh._grok_running(pane) is False


def test_running_0251_is_not_idle(monkeypatch):
    pane = lines("running.txt")
    assert "Waiting…" in "\n".join(pane)
    assert "Waiting for response" not in "\n".join(pane)
    assert mesh._grok_running(pane) is True
    assert mesh._grok_input(pane) == ""
    outcome, keys = _deliver(
        monkeypatch, pane, {"wake_outstanding": True, "last_wake_injected_at": 1.0})
    assert outcome is None
    assert keys == []


def test_finished_0251_thought_for_can_wake_again():
    pane = lines("finished.txt")
    text = "\n".join(pane)
    assert "Thought for" in text
    assert "Worked for" not in text
    assert "Ctrl+c:cancel" not in text
    assert mesh._grok_running(pane) is False
    assert mesh._grok_input(pane) == ""
    assert mesh._grok_composer_state(pane) == "clear"


def test_rate_limit_defers_with_named_reason_and_zero_keys(monkeypatch, capsys):
    pane = lines("rate-limit.txt")
    assert "rate limit" in "\n".join(pane).lower()
    assert mesh._grok_block_reason(pane) == "grok-rate-limit"
    assert mesh._grok_running(pane) is False
    # a previous turn's "Thought for" must not win over the limit screen:
    # the block check runs FIRST in deliver(), before any pane-reading logic
    poisoned = pane + ["     ◆ Thought for 2s"]
    assert mesh._grok_block_reason(poisoned) == "grok-rate-limit"
    outcome, keys = _deliver(monkeypatch, pane, {})
    assert outcome is None
    assert keys == []
    assert "grok-rate-limit" in capsys.readouterr().out


def test_usage_limit_modal_defers_instead_of_failing(monkeypatch, capsys):
    pane = lines("usage-limit.txt")
    assert "You hit your free usage limit" in "\n".join(pane)
    assert mesh._is_grok_pane(pane) is False
    assert mesh._grok_block_reason(pane) == "grok-usage-limit"
    outcome, keys = _deliver(monkeypatch, pane, {})
    assert outcome is None
    assert keys == []
    err = capsys.readouterr().out
    assert "grok-usage-limit" in err
    assert "DELIVERY FAILED" not in err
