"""Tests for `swarph spawn`'s Windows-Terminal conhost auto-relaunch.

On legacy Windows console (conhost.exe), Claude Code's Ink TUI breaks — the SGR
terminator 'm' leaks into stdin so Enter inserts a literal 'm'. `_relaunch_in_
windows_terminal` auto-relaunches the session in Windows Terminal (where the TUI
works) when on conhost + wt.exe is present. The ACTUAL wt.exe relaunch needs a real
native-Windows box (workstation-lc seat-B); these tests pin the DECISION logic
(relaunch vs warn vs proceed) via mocking, runnable on any platform.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from swarph_cli.commands import spawn

CLAUDE_BIN = "/usr/bin/claude"
CLAUDE_ARGV = ["claude", "--name", "lab", "--session-id", "abc"]
CWD = Path("/home/ubuntu/lab")
WT = "C:\\Windows\\wt.exe"


def _call(monkeypatch, *, platform="win32", wt_session=None, win_ack=None,
          wt_path=WT, popen=None):
    monkeypatch.setattr(spawn.sys, "platform", platform)
    for var, val in (("WT_SESSION", wt_session), ("SWARPH_WIN_ACK", win_ack)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    monkeypatch.setattr(spawn.shutil, "which",
                        lambda name: wt_path if name == "wt" else None)
    pop = popen or MagicMock()
    monkeypatch.setattr(spawn.subprocess, "Popen", pop)
    result = spawn._relaunch_in_windows_terminal(CLAUDE_BIN, CLAUDE_ARGV, CWD)
    return result, pop


def test_non_windows_never_relaunches(monkeypatch):
    r, pop = _call(monkeypatch, platform="linux")
    assert r is False
    pop.assert_not_called()


def test_already_in_windows_terminal_no_relaunch(monkeypatch):
    # WT_SESSION set => already in Windows Terminal (TUI works); also the
    # loop-guard — a relaunched session can never re-relaunch.
    r, pop = _call(monkeypatch, wt_session="dead-beef-0001")
    assert r is False
    pop.assert_not_called()


def test_operator_acked_stays_in_conhost(monkeypatch):
    # SWARPH_WIN_ACK=1 => operator chose conhost; don't interfere.
    r, pop = _call(monkeypatch, win_ack="1")
    assert r is False
    pop.assert_not_called()


def test_conhost_without_wt_falls_back_to_warning(monkeypatch):
    # conhost but wt.exe absent (locked-down box) => can't auto-fix; caller warns.
    r, pop = _call(monkeypatch, wt_path=None)
    assert r is False
    pop.assert_not_called()


def test_conhost_with_wt_relaunches_in_windows_terminal(monkeypatch):
    r, pop = _call(monkeypatch)
    assert r is True
    pop.assert_called_once()
    cmd = pop.call_args[0][0]
    # exact shape: [wt, -d, <cwd>, --, claude_bin, *claude_flags]
    assert cmd[:5] == [WT, "-d", str(CWD), "--", CLAUDE_BIN]
    assert cmd[5:] == CLAUDE_ARGV[1:]   # claude_argv[1:] passed through (not argv0)
    # carries SWARPH_SPAWN=1 so the SessionStart hook doesn't double-inject
    assert pop.call_args.kwargs["env"]["SWARPH_SPAWN"] == "1"


def test_popen_failure_falls_back_gracefully(monkeypatch):
    # if launching wt.exe itself errors, proceed in-place (don't crash the spawn).
    boom = MagicMock(side_effect=OSError("wt launch failed"))
    r, _ = _call(monkeypatch, popen=boom)
    assert r is False
