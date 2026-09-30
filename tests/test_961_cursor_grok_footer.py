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


def _grok_idle_lines():
    from pathlib import Path
    src = Path(__file__).resolve().parent / "fixtures" / "grok-723" / "idle.txt"
    return src.read_text(encoding="utf-8").splitlines()


def _wakes(calls, target):
    return [c for c in calls if c[:4] == ["tmux", "send-keys", "-t", target] and "-l" in c]


def test_a_grok_pane_quoting_the_cursor_placeholder_keeps_the_grok_wake(monkeypatch):
    pane = _grok_idle_lines()
    pane.insert(0, 'note: cursor renders "Add a follow-up" as its composer')
    assert mesh._is_grok_pane(pane) is True
    calls = _record(monkeypatch, "\n".join(pane))
    assert mesh.TmuxSink("grok-sac").deliver(_State({}), [], 1) is True
    assert _wakes(calls, "grok-sac") == [
        ["tmux", "send-keys", "-t", "grok-sac", "-l", mesh._GROK_WAKE_PROMPT]]
    assert mesh._GROK_WAKE_PROMPT != mesh._WAKE_PROMPT


def test_a_cursor_row_above_the_grok_composer_keeps_the_grok_wake(monkeypatch):
    pane = _grok_idle_lines()
    idx = next(i for i, ln in enumerate(pane) if "│" in ln and "❯" in ln)
    pane.insert(idx, "→ Add a follow-up")
    assert mesh._is_grok_pane(pane) is True
    assert mesh._is_cursor_composer(pane) is False
    calls = _record(monkeypatch, "\n".join(pane))
    assert mesh.TmuxSink("grok-sac").deliver(_State({}), [], 1) is True
    sent = _wakes(calls, "grok-sac")
    assert sent == [["tmux", "send-keys", "-t", "grok-sac", "-l", mesh._GROK_WAKE_PROMPT]]
    assert sent[0][-1] != mesh._WAKE_PROMPT


def test_a_quoted_placeholder_with_the_composer_off_screen_sends_nothing(monkeypatch):
    pane = [
        "→ Add a follow-up",
        'the output quotes "Add a follow-up" while the composer is gone',
    ]
    pane += ["scrollback"] * 20
    pane += [
        "2 tasks",
        "Grok 4.7 256K High Fast · 1% · 1 files edited",
        "~",
    ]
    assert mesh._is_cursor_composer(pane) is False
    calls = _record(monkeypatch, "\n".join(pane))
    assert mesh.TmuxSink("cursor-lin-sac").deliver(_State({}), [], 1) is False
    assert _wakes(calls, "cursor-lin-sac") == []
    assert not any("Enter" in c for c in calls)


def test_fc_blank_rows_still_read_as_an_idle_cursor_pane(monkeypatch):
    """Fails at 34d2bbf: the tail was the raw last N lines, and the blanks
    pushed '→ Add a follow-up' out of that window."""
    from pathlib import Path
    raw = (Path(__file__).resolve().parent / "fixtures" / "cursor-fc-bottom.txt").read_text(encoding="utf-8").splitlines()
    assert "" in raw
    assert raw[0].strip() == "→ Add a follow-up"
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda _t: raw)
    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    assert mesh._is_cursor_composer(raw) is True
    assert mesh._composer_state("fc") == "clear"
    assert mesh.TmuxSink("fc").deliver(_State({}), [], 1) is True
    assert _wakes(calls, "fc") == [["tmux", "send-keys", "-t", "fc", "-l", mesh._WAKE_PROMPT]]
    assert sum(1 for c in calls if "Enter" in c) == 1


def _drop_pane(name):
    from pathlib import Path
    root = Path(__file__).resolve().parent / "fixtures" / "cursor-quotes-grok-775"
    return (root / name).read_text(encoding="utf-8")


def test_a_cursor_pane_quoting_a_grok_box_stays_idle_and_wakes_once(monkeypatch):
    """Fails at fa297a3: a │/❯ pair anywhere classified the pane as grok."""
    for name in (
        "idle_detector_source_diff.txt",
        "idle_diff_grok_row_test.txt",
        "idle_own_test_hunk.txt",
    ):
        text = _drop_pane(name)
        calls = _record(monkeypatch, text)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert mesh._is_grok_pane(lines) is False, name
        assert mesh._is_cursor_composer(lines) is True, name
        assert mesh._composer_state("drop") == "clear", name
        assert mesh.TmuxSink("drop").deliver(_State({}), [], 1) is True, name
        assert _wakes(calls, "drop") == [
            ["tmux", "send-keys", "-t", "drop", "-l", mesh._WAKE_PROMPT]], name
        assert sum(1 for c in calls if "Enter" in c) == 1, name


def test_a_running_cursor_pane_quoting_a_grok_box_defers(monkeypatch):
    """Fails at fa297a3: the same pair woke a running cursor with the grok prompt."""
    for name in (
        "running_diff_grok_box_with_footer.txt",
        "running_grok_row_hunk.txt",
        "running_quoted_empty_grok_box.txt",
    ):
        text = _drop_pane(name)
        calls = _record(monkeypatch, text)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert mesh._is_cursor_composer(lines) is True, name
        assert mesh._is_grok_pane(lines) is False, name
        assert mesh.TmuxSink("drop").deliver(_State({}), [], 1) is None, name
        assert not any(len(c) > 1 and c[1] == "send-keys" for c in calls), name


def test_opencode_mentioning_grok_is_not_grok():
    src = Path(__file__).resolve().parent / "fixtures" / "opencode-pane-idle-footer-0710.txt"
    pane = src.read_text(encoding="utf-8").splitlines()
    pane.append("The model in that session was Grok 4.7")
    assert mesh._is_opencode_pane(pane) is True
    assert mesh._is_grok_pane(pane) is False
