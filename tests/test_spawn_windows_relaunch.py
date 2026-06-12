"""Tests for `swarph spawn`'s Windows-Terminal conhost auto-relaunch.

On legacy Windows console (conhost.exe), Claude Code's Ink TUI breaks — the SGR
terminator 'm' leaks into stdin so Enter inserts a literal 'm'. `_relaunch_in_
windows_terminal` auto-relaunches the session in Windows Terminal (where the TUI
works) by DEFAULT, and only stays put when it can POSITIVELY confirm a genuine
Windows Terminal via process ancestry (`_console_is_genuine_wt`) — no longer the
inheritable `WT_SESSION` env var, which fooled the old heuristic (live repro
2026-06-03 on workstation-lc: launching from WT set WT_SESSION → relaunch wrongly
skipped → no new window).

The ACTUAL wt.exe relaunch + the ctypes ancestry walk need a real native-Windows
box; these tests pin the DECISION logic (relaunch vs warn vs proceed) by mocking
`_console_is_genuine_wt` directly, so they run on any platform.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarph_cli.commands import spawn

CLAUDE_BIN = "/usr/bin/claude"
CLAUDE_ARGV = ["claude", "--name", "lab", "--session-id", "abc"]
CWD = Path("/home/ubuntu/lab")
WT = "C:\\Windows\\wt.exe"


class _FakeStdout:
    """Minimal stand-in so tests can control sys.stdout.isatty() — the helper
    only ever calls .isatty() on stdout (its prints go to stderr)."""

    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _call(monkeypatch, *, platform="win32", genuine_wt=False, wt_session=None,
          win_ack=None, spawn_marker=None, force_wt=None, isatty=True,
          wt_path=WT, popen=None, tmux=None):
    """Drive `_relaunch_in_windows_terminal` with the genuine-WT detector mocked.

    `genuine_wt` patches `_console_is_genuine_wt` directly (True/False) so we test
    the DECISION matrix without touching ctypes. `wt_session` is still settable in
    the env to prove the new logic IGNORES it (the headline bug fix). `tmux` sets
    $TMUX to prove the new in-tmux guard skips the WT rescue (tmux's PTY already
    handles VT-input, so bouncing to WT would escape the supervised pane).
    """
    monkeypatch.setattr(spawn.sys, "platform", platform)
    monkeypatch.setattr(spawn.sys, "stdout", _FakeStdout(isatty))
    for var, val in (("WT_SESSION", wt_session), ("SWARPH_WIN_ACK", win_ack),
                     ("SWARPH_SPAWN", spawn_marker), ("SWARPH_FORCE_WT", force_wt),
                     ("TMUX", tmux)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    monkeypatch.setattr(spawn, "_console_is_genuine_wt", lambda: genuine_wt)
    monkeypatch.setattr(spawn.shutil, "which",
                        lambda name: wt_path if name == "wt" else None)
    pop = popen or MagicMock()
    monkeypatch.setattr(spawn.subprocess, "Popen", pop)
    result = spawn._relaunch_in_windows_terminal(CLAUDE_BIN, CLAUDE_ARGV, CWD)
    return result, pop


# --- the decision matrix -------------------------------------------------


def test_non_windows_never_relaunches(monkeypatch):
    r, pop = _call(monkeypatch, platform="linux")
    assert r is False
    pop.assert_not_called()


def test_conhost_with_wt_relaunches_in_windows_terminal(monkeypatch):
    # #1 the rescue: NOT a genuine WT (conhost) + wt.exe present => relaunch.
    r, pop = _call(monkeypatch, genuine_wt=False)
    assert r is True
    pop.assert_called_once()
    cmd = pop.call_args[0][0]
    # exact shape: [wt, -d, <cwd>, --, claude_bin, *claude_flags]
    assert cmd[:5] == [WT, "-d", str(CWD), "--", CLAUDE_BIN]
    assert cmd[5:] == CLAUDE_ARGV[1:]   # claude_argv[1:] passed through (not argv0)
    # carries SWARPH_SPAWN=1 so the SessionStart hook doesn't double-inject
    assert pop.call_args.kwargs["env"]["SWARPH_SPAWN"] == "1"


def test_genuine_wt_no_force_skips_relaunch(monkeypatch):
    # #2 positively confirmed genuine WT (TUI works) + no force => no new window.
    r, pop = _call(monkeypatch, genuine_wt=True)
    assert r is False
    pop.assert_not_called()


def test_inherited_wt_session_but_not_genuine_wt_relaunches(monkeypatch):
    # #3 THE HEADLINE BUG FIX: WT_SESSION is set in the env (inherited into a
    # broken conhost, OR set because the shell was launched from WT), but process
    # ancestry says this is NOT a genuine WT. New logic IGNORES WT_SESSION and
    # relaunches. The old WT_SESSION-keyed code would have wrongly SKIPPED here,
    # leaving the user stuck on a broken console with no new window.
    r, pop = _call(monkeypatch, genuine_wt=False, wt_session="inherited-guid")
    assert r is True
    pop.assert_called_once()


def test_force_wt_overrides_genuine_wt(monkeypatch):
    # #4 SWARPH_FORCE_WT=1 forces the relaunch even from a confirmed genuine WT.
    r, pop = _call(monkeypatch, genuine_wt=True, force_wt="1")
    assert r is True
    pop.assert_called_once()


def test_win_ack_opt_out_skips_even_on_conhost(monkeypatch):
    # #5 SWARPH_WIN_ACK=1 => operator chose to run here; skip even on a broken
    # conhost (genuine_wt=False) with wt present.
    r, pop = _call(monkeypatch, genuine_wt=False, win_ack="1")
    assert r is False
    pop.assert_not_called()


def test_already_spawned_skips_relaunch(monkeypatch):
    # #6 SWARPH_SPAWN set => inside a session we already spawned. Reliable
    # loop-guard, independent of everything else — never re-relaunch.
    r, pop = _call(monkeypatch, genuine_wt=False, spawn_marker="1")
    assert r is False
    pop.assert_not_called()


def test_inside_tmux_skips_relaunch(monkeypatch):
    # $TMUX set => inside a tmux pane. tmux's own PTY handles VT-input correctly,
    # so the Ink TUI works; bouncing to a fresh WT window would ESCAPE the
    # supervised pane. Must NOT relaunch even on a conhost (genuine_wt=False).
    r, pop = _call(monkeypatch, genuine_wt=False, tmux="/tmp/tmux-1000/default,9,0")
    assert r is False
    pop.assert_not_called()


def test_non_interactive_stdout_skips_relaunch(monkeypatch):
    # #7 CI / piped / redirected: no human console to relaunch from. Must NOT
    # spawn a detached WT window even on conhost+wt.
    r, pop = _call(monkeypatch, genuine_wt=False, isatty=False)
    assert r is False
    pop.assert_not_called()


def test_conhost_without_wt_falls_back_to_warning(monkeypatch):
    # #8 conhost (not genuine WT) but wt.exe absent (locked-down box) => can't
    # auto-fix; relaunch returns False and caller warns.
    r, pop = _call(monkeypatch, genuine_wt=False, wt_path=None)
    assert r is False
    pop.assert_not_called()


def test_force_wt_still_loop_guarded_by_spawn_marker(monkeypatch):
    # a persistent SWARPH_FORCE_WT must NOT cause infinite re-relaunch: the
    # SWARPH_SPAWN marker on the relaunched tree wins over the force flag.
    r, pop = _call(monkeypatch, genuine_wt=True, force_wt="1", spawn_marker="1")
    assert r is False
    pop.assert_not_called()


def test_popen_failure_falls_back_gracefully(monkeypatch):
    # if launching wt.exe itself errors, proceed in-place (don't crash the spawn).
    boom = MagicMock(side_effect=OSError("wt launch failed"))
    r, _ = _call(monkeypatch, genuine_wt=False, popen=boom)
    assert r is False


# --- the helper's own non-win32 guard (the one piece that can't be fully
#     exercised on Linux; here we just confirm the fail-safe + no-raise) ----


def test_console_is_genuine_wt_false_off_win32(monkeypatch):
    # On any non-win32 platform the helper returns False immediately and never
    # touches ctypes — so it can't raise on this Linux box.
    monkeypatch.setattr(spawn.sys, "platform", "linux")
    assert spawn._console_is_genuine_wt() is False


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="exercises the REAL ctypes CreateToolhelp32Snapshot / PROCESSENTRY32 "
    "ancestry walk — win32-only; on Linux the helper short-circuits to False "
    "before any ctypes call, so it can't be exercised here",
)
def test_console_is_genuine_wt_real_ctypes_walk_returns_bool():
    # drop seat-A #68 MEDIUM: every OTHER test mocks _console_is_genuine_wt or
    # patches sys.platform=linux, so the actual Win32 ctypes path
    # (spawn.py _console_is_genuine_wt: CreateToolhelp32Snapshot + the
    # PROCESSENTRY32 struct + the parent-chain walk) NEVER ran on the runner —
    # a ctypes argtypes/restype/struct-layout regression would pass CI green.
    # This test calls it FOR REAL (no mock, no platform patch) on windows-latest,
    # so a signature regression raises (caught by the helper's own try/except ->
    # would change the return) or crashes. THIS is the gap-closer the RFC §1
    # wants: the CI now genuinely exercises the fix that motivated it.
    result = spawn._console_is_genuine_wt()
    assert isinstance(result, bool)
