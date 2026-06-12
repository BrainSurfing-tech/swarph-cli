"""Tests for `swarph spawn`'s tmux-session launch (`_launch_via_tmux`), ALL-OS.

The preferred launch path runs the claude cell inside a NAMED tmux session
rather than the bare console — on EVERY OS (`swarph spawn <name>` → create-or-
attach). tmux provides its own PTY with correct VT-input handling, so claude's
Ink TUI renders correctly inside a pane on every platform — and on Windows
specifically this also dodges the conhost/PowerShell Enter-inserts-'m' bug. A
named session is durable + supervisable everywhere: the sidecar/watchdog wake it
via `tmux send-keys -t <session>`.

The interactive attach is per-OS:
  * POSIX (Linux/mac): `os.execv` — a TRUE in-place replace (the process becomes
    `tmux attach`); never returns on success.
  * Windows: a BLOCKING `subprocess.run([tmux, "attach"])` — Windows os.exec* is
    spawn-and-exit, so a real replace is unavailable and a blocking child keeps
    ONE shared console.

The ACTUAL tmux calls need a real multiplexer (real tmux on Linux/mac, psmux on
Windows); these tests pin the DECISION logic (create vs attach vs skip,
interactive vs headless, win32 vs POSIX attach mechanism) by mocking subprocess
AND os.execv, so they run on any platform without ever firing a real exec.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import subprocess

import pytest

from swarph_cli.commands import spawn

BIN = "/usr/bin/claude"
ARGV = ["claude", "--name", "lab", "--session-id", "abc"]
CWD = Path("/home/ubuntu/lab")
SESSION = "lab"
TMUX = "/usr/bin/tmux"
WT = "C:\\Windows\\wt.exe"

# Sentinel returned by _drive when the POSIX os.execv attach fired (in reality
# os.execv never returns; the mock raises _ExecvReplaced to simulate that).
_EXECV = object()


class _ExecvReplaced(BaseException):
    """Mock side-effect for os.execv: real execv replaces the process and never
    returns, so we halt _launch_via_tmux here. BaseException (not Exception) so
    the function's `except OSError` around execv never swallows it."""


class _FakeStdout:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _drive(monkeypatch, *, platform="win32", tmux=TMUX, wt=WT, in_tmux=None,
           spawn_marker=None, isatty=True, session_exists=False,
           new_session_exc=None):
    """Drive `_launch_via_tmux` with tmux/subprocess/execv mocked.

    `run` dispatches on the subcommand: `has-session` returns rc 0/1 per
    `session_exists`; `new-session` returns rc 0 (or raises `new_session_exc`);
    `attach` (Windows path only) returns rc 0. The POSIX attach is os.execv,
    mocked to raise `_ExecvReplaced` (simulating its never-returns replace).

    Returns `(result, run, execv)`. `result` is the function's bool, or the
    `_EXECV` sentinel when the POSIX execv attach took over.
    """
    monkeypatch.setattr(spawn.sys, "platform", platform)
    monkeypatch.setattr(spawn.sys, "stdout", _FakeStdout(isatty))
    for var, val in (("TMUX", in_tmux), ("SWARPH_SPAWN", spawn_marker)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)

    def _which(name):
        return {"tmux": tmux, "wt": wt}.get(name)

    monkeypatch.setattr(spawn.shutil, "which", _which)

    def _run(cmd, **kwargs):
        if "has-session" in cmd:
            return MagicMock(returncode=0 if session_exists else 1)
        if "new-session" in cmd and new_session_exc is not None:
            raise new_session_exc
        return MagicMock(returncode=0)

    run = MagicMock(side_effect=_run)
    monkeypatch.setattr(spawn.subprocess, "run", run)

    execv = MagicMock(side_effect=_ExecvReplaced())
    monkeypatch.setattr(spawn.os, "execv", execv)

    try:
        result = spawn._launch_via_tmux(BIN, ARGV, CWD, SESSION)
    except _ExecvReplaced:
        result = _EXECV
    return result, run, execv


def _cmds(run):
    """The list of argv lists passed to subprocess.run."""
    return [c.args[0] for c in run.call_args_list]


def _attached_via_run(run):
    """True if the Windows blocking-subprocess.run attach fired."""
    return [TMUX, "attach", "-t", SESSION] in _cmds(run)


def _attached_via_execv(execv):
    """True if the POSIX os.execv attach fired with the right argv."""
    return execv.call_args is not None and execv.call_args.args == (
        TMUX, [TMUX, "attach", "-t", SESSION]
    )


# --- gates that skip tmux entirely (OS-agnostic) -------------------------


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_inside_tmux_skips_is_the_loop_breaker(monkeypatch, platform):
    # $TMUX set => the inner `swarph spawn <name>` re-entry runs INSIDE the pane.
    # It must NOT re-decide attach-or-create (that's the infinite loop); it falls
    # through to launch()'s in-place exec. Primary loop-breaker, on EVERY OS —
    # this is what makes the claude-tmux@.service template compose cleanly.
    r, run, execv = _drive(monkeypatch, platform=platform,
                           in_tmux="/tmp/tmux-1000/default,9,0")
    assert r is False
    run.assert_not_called()
    execv.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_spawn_marker_skips_belt_and_suspenders(monkeypatch, platform):
    # SWARPH_SPAWN set => inside a session we already spawned. Secondary guard.
    r, run, execv = _drive(monkeypatch, platform=platform, spawn_marker="1")
    assert r is False
    run.assert_not_called()
    execv.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_no_tmux_on_path_falls_through(monkeypatch, platform):
    # tmux/psmux absent => return False so the caller drops to the standard launch
    # (Windows: the WT relaunch rescue first).
    r, run, execv = _drive(monkeypatch, platform=platform, tmux=None)
    assert r is False
    execv.assert_not_called()


# --- attach vs create: per-OS attach mechanism ---------------------------


def test_existing_session_attaches_no_create_windows(monkeypatch):
    # Windows: session exists + interactive => attach via BLOCKING subprocess.run
    # IN THIS console; DON'T create a second cell.
    r, run, execv = _drive(monkeypatch, platform="win32", session_exists=True)
    assert r is True
    cmds = _cmds(run)
    assert any("has-session" in c for c in cmds)
    assert not any("new-session" in c for c in cmds)
    assert _attached_via_run(run)
    execv.assert_not_called()  # Windows never uses os.execv


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_existing_session_attaches_no_create_posix(monkeypatch, platform):
    # POSIX: session exists + interactive => attach via os.execv TRUE replace;
    # DON'T create a second cell.
    r, run, execv = _drive(monkeypatch, platform=platform, session_exists=True)
    assert r is _EXECV  # execv took over (never-returns)
    cmds = _cmds(run)
    assert any("has-session" in c for c in cmds)
    assert not any("new-session" in c for c in cmds)
    assert _attached_via_execv(execv)
    assert not _attached_via_run(run)  # POSIX attach is execv, not subprocess.run


def test_absent_session_creates_then_attaches_windows(monkeypatch):
    r, run, execv = _drive(monkeypatch, platform="win32", session_exists=False)
    assert r is True
    cmds = _cmds(run)
    create = next(c for c in cmds if "new-session" in c)
    # detached, named, cwd-pinned, re-enters `swarph spawn <name>` with the
    # SWARPH_SPAWN guard injected into the session env.
    assert create[:5] == [TMUX, "new-session", "-d", "-s", SESSION]
    assert "-e" in create and "SWARPH_SPAWN=1" in create
    assert create[-3:] == ["swarph", "spawn", SESSION]
    assert _attached_via_run(run)  # then blocking-subprocess.run attach
    execv.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_absent_session_creates_then_attaches_posix(monkeypatch, platform):
    # The single-command Linux/mac UX the generalization unlocks: create the
    # session (via subprocess.run) then attach via os.execv true-replace.
    r, run, execv = _drive(monkeypatch, platform=platform, session_exists=False)
    assert r is _EXECV
    cmds = _cmds(run)
    create = next(c for c in cmds if "new-session" in c)
    assert create[:5] == [TMUX, "new-session", "-d", "-s", SESSION]
    assert "-e" in create and "SWARPH_SPAWN=1" in create
    assert create[-3:] == ["swarph", "spawn", SESSION]
    assert _attached_via_execv(execv)  # os.execv attach
    assert not _attached_via_run(run)


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_absent_session_headless_creates_detached_no_attach(monkeypatch, platform):
    # Non-interactive (watchdog A2 respawn / CI): create the session detached but
    # DO NOT attach — on ANY OS. The watchdog/sidecar reach it via send-keys.
    r, run, execv = _drive(monkeypatch, platform=platform,
                           session_exists=False, isatty=False)
    assert r is True
    assert any("new-session" in c for c in _cmds(run))
    assert not _attached_via_run(run)
    execv.assert_not_called()  # headless never attaches


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_existing_session_headless_is_a_noop_handoff(monkeypatch, platform):
    # Session exists + headless => nothing to do but claim the launch (True) so
    # run_spawn short-circuits; no create, no attach.
    r, run, execv = _drive(monkeypatch, platform=platform,
                           session_exists=True, isatty=False)
    assert r is True
    assert not any("new-session" in c for c in _cmds(run))
    assert not _attached_via_run(run)
    execv.assert_not_called()


# --- failure handling (OS-agnostic) --------------------------------------


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_create_failure_falls_through(monkeypatch, platform):
    # tmux new-session errors => return False so the caller drops to the standard
    # launch; never attach a half-created session.
    boom = subprocess.CalledProcessError(1, "tmux")
    r, run, execv = _drive(monkeypatch, platform=platform,
                           session_exists=False, new_session_exc=boom)
    assert r is False
    assert not _attached_via_run(run)
    execv.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_create_oserror_falls_through(monkeypatch, platform):
    r, run, execv = _drive(
        monkeypatch, platform=platform, session_exists=False,
        new_session_exc=OSError("no tmux"),
    )
    assert r is False
    assert not _attached_via_run(run)
    execv.assert_not_called()
