"""#533: `_tmux_wake` must VERIFY the wake submitted, not gesture blindly.

The old Codex-shaped double-Enter assumed a second Enter on a single-submit
composer is a no-op. Falsified on cursor's Linux TUI: the Enters raced the
composer, the prompt never submitted, and consecutive wakes CONCATENATED —
'check meshcheck meshcheck mesh' arrived as one prompt on cursor-lin
(2026-08-24). The replacement injects, settles, then Enter-and-verifies in a
bounded loop, returning True only on an observed-clear composer so an
unconfirmed wake stays owed.
"""

from __future__ import annotations

import subprocess

import pytest

import swarph_cli.commands.mesh as mesh


@pytest.fixture
def tmux(monkeypatch):
    """Fake tmux: records send-keys calls; capture-pane output is scripted by
    the test via `captures` (a list consumed in order, last value repeated)."""
    calls: list[list[str]] = []
    state = {"captures": [""], "i": 0}

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "capture-pane":
            i = min(state["i"], len(state["captures"]) - 1)
            state["i"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout=state["captures"][i], stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    return calls, state


def enters(calls) -> int:
    return sum(1 for c in calls if c[:3] == ["tmux", "send-keys", "-t"] and "Enter" in c)


def test_first_enter_submits_single_submit_composer(tmux):
    calls, state = tmux
    state["captures"] = ["> "]  # composer clear after the first Enter
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 1


def test_second_enter_fires_only_when_the_first_did_not_submit(tmux):
    """The Codex shape, now verified instead of blind: first Enter leaves the
    prompt in the composer, second clears it."""
    calls, state = tmux
    state["captures"] = ["> check mesh|", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_concatenated_backlog_is_detected_and_drained(tmux):
    """The observed defect shape: two wakes stacked unsubmitted. The substring
    check catches the concatenation and the retry Enter drains it."""
    calls, state = tmux
    state["captures"] = ["> check meshcheck mesh|", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_never_submitting_pane_is_bounded_and_reports_false(tmux):
    """A pane that never clears gets exactly the bounded attempts, then False —
    the wake stays owed (cursor advances only inside `if _tmux_wake(...)`)."""
    calls, state = tmux
    state["captures"] = ["> check mesh|"]  # never clears
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == mesh._WAKE_SUBMIT_ATTEMPTS


def test_capture_failure_biases_toward_retry_not_false_success(tmux, monkeypatch):
    """Unknown composer state must not masquerade as submitted: a failing
    capture-pane keeps the loop retrying (a spare Enter is a no-op; a missing
    Enter is the defect)."""
    calls, state = tmux

    def failing_capture(argv, **kw):
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pane")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", failing_capture)
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == mesh._WAKE_SUBMIT_ATTEMPTS


def test_send_keys_failure_returns_false_not_raise(tmux, monkeypatch):
    def boom(argv, **kw):
        raise OSError("tmux gone")

    monkeypatch.setattr(mesh.subprocess, "run", boom)
    assert mesh._tmux_wake("pane") is False
