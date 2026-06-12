"""Tests for `swarph spawn`'s Windows tmux-session launch (`_launch_via_tmux`).

On Windows, the preferred launch path runs the claude cell inside a NAMED tmux
session rather than the bare console. tmux provides its own PTY with correct
VT-input handling, so claude's Ink TUI works inside a pane exactly as in Windows
Terminal — and UNLIKE raw conhost / a PowerShell console, where the SGR 'm'
terminator leaks into stdin (Enter inserts a literal 'm'). A named session is
also durable + supervisable: the sidecar/watchdog wake it via `tmux send-keys
-t <session>`.

The interactive attach is a BLOCKING `subprocess.run([tmux, "attach"])`, NOT
`os.execv`: this path is win32-only, and on Windows os.exec* is emulated as
spawn-and-exit, which lets the parent PowerShell regain the console and fight the
attaching tmux for it (garbled render, observed on workstation-lc). A blocking
child shares one console down parent->child, exactly like the operator typing
`tmux attach` by hand.

The ACTUAL tmux calls need a real native-Windows box; these tests pin the
DECISION logic (create vs attach vs skip, interactive vs headless) by mocking
subprocess, so they run on any platform.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import subprocess

from swarph_cli.commands import spawn

BIN = "/usr/bin/claude"
ARGV = ["claude", "--name", "lab", "--session-id", "abc"]
CWD = Path("/home/ubuntu/lab")
SESSION = "lab"
TMUX = "/usr/bin/tmux"
WT = "C:\\Windows\\wt.exe"


class _FakeStdout:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _drive(monkeypatch, *, platform="win32", tmux=TMUX, wt=WT, in_tmux=None,
           spawn_marker=None, isatty=True, session_exists=False,
           new_session_exc=None):
    """Drive `_launch_via_tmux` with tmux/subprocess mocked.

    `run` dispatches on the subcommand: `has-session` returns rc 0/1 per
    `session_exists`; `new-session` returns rc 0 (or raises `new_session_exc`);
    `attach` returns rc 0. Every tmux invocation goes through subprocess.run
    (including the interactive attach — a blocking child, NOT os.execv).
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
    result = spawn._launch_via_tmux(BIN, ARGV, CWD, SESSION)
    return result, run


def _cmds(run):
    """The list of argv lists passed to subprocess.run."""
    return [c.args[0] for c in run.call_args_list]


def _attached(run):
    return [TMUX, "attach", "-t", SESSION] in _cmds(run)


# --- gates that skip tmux entirely ---------------------------------------


def test_non_windows_never_uses_tmux(monkeypatch):
    r, run = _drive(monkeypatch, platform="linux")
    assert r is False
    run.assert_not_called()


def test_inside_tmux_skips_is_the_loop_breaker(monkeypatch):
    # $TMUX set => the inner `swarph spawn <name>` re-entry runs INSIDE the pane.
    # It must NOT re-decide attach-or-create (that's the infinite loop); it falls
    # through to launch()'s in-place exec. This is the primary loop-breaker.
    r, run = _drive(monkeypatch, in_tmux="/tmp/tmux-1000/default,9,0")
    assert r is False
    run.assert_not_called()


def test_spawn_marker_skips_belt_and_suspenders(monkeypatch):
    # SWARPH_SPAWN set => inside a session we already spawned. Secondary guard.
    r, run = _drive(monkeypatch, spawn_marker="1")
    assert r is False
    run.assert_not_called()


def test_no_tmux_on_path_falls_through(monkeypatch):
    # tmux absent => return False so the caller drops to the WT relaunch rescue.
    r, run = _drive(monkeypatch, tmux=None)
    assert r is False


# --- attach vs create -----------------------------------------------------


def test_existing_session_attaches_in_place_no_create(monkeypatch):
    # Session already exists + interactive => attach IN PLACE (this console, as a
    # blocking child), DON'T create a second cell. claude is already in session.
    r, run = _drive(monkeypatch, session_exists=True)
    assert r is True
    cmds = _cmds(run)
    assert any("has-session" in c for c in cmds)
    assert not any("new-session" in c for c in cmds)  # no second cell
    assert _attached(run)  # blocking subprocess.run attach


def test_absent_session_creates_then_attaches_in_place(monkeypatch):
    r, run = _drive(monkeypatch, session_exists=False)
    assert r is True
    cmds = _cmds(run)
    create = next(c for c in cmds if "new-session" in c)
    # detached, named, cwd-pinned, re-enters `swarph spawn <name>` with the
    # SWARPH_SPAWN guard injected into the session env.
    assert create[:5] == [TMUX, "new-session", "-d", "-s", SESSION]
    assert "-e" in create and "SWARPH_SPAWN=1" in create
    assert create[-3:] == ["swarph", "spawn", SESSION]
    assert _attached(run)  # then attach in place


def test_absent_session_headless_creates_detached_no_attach(monkeypatch):
    # Non-interactive (watchdog A2 respawn / CI): create the session detached but
    # DO NOT attach. The watchdog/sidecar reach it via send-keys.
    r, run = _drive(monkeypatch, session_exists=False, isatty=False)
    assert r is True
    assert any("new-session" in c for c in _cmds(run))
    assert not _attached(run)  # detached, no attach


def test_existing_session_headless_is_a_noop_handoff(monkeypatch):
    # Session exists + headless => nothing to do but claim the launch (True) so
    # run_spawn short-circuits; no create, no attach.
    r, run = _drive(monkeypatch, session_exists=True, isatty=False)
    assert r is True
    assert not any("new-session" in c for c in _cmds(run))
    assert not _attached(run)


# --- failure handling -----------------------------------------------------


def test_create_failure_falls_through(monkeypatch):
    # tmux new-session errors => return False so the caller drops to WT relaunch;
    # never attach a half-created session.
    boom = subprocess.CalledProcessError(1, "tmux")
    r, run = _drive(monkeypatch, session_exists=False, new_session_exc=boom)
    assert r is False
    assert not _attached(run)


def test_create_oserror_falls_through(monkeypatch):
    r, run = _drive(
        monkeypatch, session_exists=False, new_session_exc=OSError("no tmux")
    )
    assert r is False
    assert not _attached(run)
