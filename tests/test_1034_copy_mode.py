"""card #1034: a tmux pane in copy-mode must not swallow the wake.

A pane in copy-mode reads idle on the captured screen, so the monitor used to
send keys that tmux consumed as copy-mode commands -- the TUI never saw them and
the DM was marked delivered. Ask tmux (#{pane_in_mode}), send nothing, keep the
DM owed.
"""
import shutil
import subprocess
import uuid

import pytest

from swarph_cli.commands import mesh


@pytest.mark.live_tmux
def test_pane_in_mode_reads_a_real_copy_mode_pane():
    """Reason for live_tmux: #{pane_in_mode} is answerable only by a real tmux
    server against a real pane, so this test drives one (new-session, copy-mode,
    cancel). Against a REAL pane: not-in-mode is False, copy-mode is True, and
    leaving copy-mode returns to False. On base there is no _pane_in_mode, so
    this fails (AttributeError) -- red before the fix."""
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    sess = "cw1034-" + uuid.uuid4().hex[:8]
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-x", "80", "-y", "24"],
                   check=True)
    try:
        pane = f"{sess}:0.0"
        assert mesh._pane_in_mode(pane) is False
        subprocess.run(["tmux", "copy-mode", "-t", pane], check=True)
        assert mesh._pane_in_mode(pane) is True
        # a keystroke into copy-mode is a MODE command, not a wake: tmux drops it
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"], check=True)
        assert mesh._pane_in_mode(pane) is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess])


def test_the_sink_defers_in_copy_mode_without_sending(monkeypatch):
    """A pane in copy-mode: the sink sends ZERO keys and keeps the DM owed
    (deliver returns None, the deferral). On base there is no _pane_in_mode, so
    the monkeypatch raises -- red before the fix."""
    sent = []
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: ["idle-looking screen"])
    monkeypatch.setattr(mesh, "_pane_in_mode", lambda t: True)
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: sent.append(("wake", t)) or True)
    monkeypatch.setattr(mesh, "_tmux_enter", lambda t: sent.append(("enter", t)) or True)

    class _State:
        def __init__(self):
            self._ledgers = {}

        def ledger(self, name):
            return self._ledgers.setdefault(name, {})

    sink = mesh.TmuxSink("opencode")
    out = sink.deliver(_State(), [{"id": 1, "from_node": "lab-ovh"}], 1)
    assert out is None, "a copy-mode pane must DEFER, not deliver"
    assert sent == [], f"no keys may be sent into copy-mode, got {sent}"
