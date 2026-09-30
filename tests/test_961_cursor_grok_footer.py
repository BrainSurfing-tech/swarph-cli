"""card #961 — a Cursor footer that names a Grok model is still Cursor.

These assertions fail on main: ``_is_grok_pane`` matches the substring
``Grok 4.``, so the pane is classified as grok, the composer is unread,
and deliver sends no wake.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import swarph_cli.commands.mesh as mesh

# Read-only capture of the cursor-lin pane, 2026-09-30: the composer row,
# then the model footer. The rest of the live pane is scrollback and is
# not part of the classifier.
CURSOR_GROK_FOOTER = "\n".join([
    "some earlier scrollback",
    "→ Add a follow-up",
    "Grok 4.7 256K High Fast · 66.1% · 196 files edited",
])


class _State:
    def __init__(self, led):
        self.led = led
        self.gateway = "http://stub.invalid"
        self.self_name = "cursor-lin"
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


def test_a_cursor_footer_naming_grok_is_not_a_grok_pane():
    lines = [ln for ln in CURSOR_GROK_FOOTER.splitlines() if ln.strip()]
    assert mesh._is_grok_pane(lines) is False


def test_an_idle_cursor_pane_with_a_grok_footer_gets_one_wake(monkeypatch):
    calls = _record(monkeypatch, CURSOR_GROK_FOOTER)
    led = {}
    assert mesh.TmuxSink("cursor-lin-sac").deliver(_State(led), [], 1) is True
    wakes = [c for c in calls if c[:4] == ["tmux", "send-keys", "-t", "cursor-lin-sac"]
             and "-l" in c]
    enters = [c for c in calls if c[:3] == ["tmux", "send-keys", "-t"] and "Enter" in c]
    assert wakes == [["tmux", "send-keys", "-t", "cursor-lin-sac", "-l", mesh._WAKE_PROMPT]]
    assert len(enters) == 1
    assert led["wake_outstanding"] is True


def test_opencode_mentioning_grok_is_not_grok():
    src = Path(__file__).resolve().parent / "fixtures" / "opencode-pane-idle-footer-0710.txt"
    pane = src.read_text(encoding="utf-8").splitlines()
    pane.append("The model in that session was Grok 4.7")
    assert mesh._is_opencode_pane(pane) is True
    assert mesh._is_grok_pane(pane) is False
