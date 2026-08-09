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
           new_session_exc=None, create_succeeds=True, create_succeeds_after=1,
           genuine_wt=True):
    """Drive `_launch_via_tmux` with tmux/subprocess/execv mocked.

    `run` dispatches on the subcommand and is STATEFUL to model reality after the
    stale-registration fix: `has-session` returns rc 0 once the session exists —
    either pre-existing (`session_exists`) OR after a `new-session` "takes".
    `_tmux_create_session` clears a stale reg (`kill-session`, rc 0 here) then
    creates + VERIFIES via `has-session`, retrying because psmux clears the stale
    lock asynchronously. `create_succeeds_after` models that delay: the session
    only materialises on the Nth `new-session` call (1 = first try, the happy
    path). `create_succeeds=False` models a create that never materialises (e.g. a
    stale lock that won't clear) → the loop exhausts and returns False.
    `new-session` raises `new_session_exc` when set. `attach` (Windows) returns rc
    0; POSIX attach is os.execv, mocked to raise `_ExecvReplaced`.

    Returns `(result, run, execv)`. `result` is the function's bool, or the
    `_EXECV` sentinel when the POSIX execv attach took over.
    """
    monkeypatch.setattr(spawn.sys, "platform", platform)
    monkeypatch.setattr(spawn.sys, "stdout", _FakeStdout(isatty))
    monkeypatch.setattr(spawn.time, "sleep", lambda *_a, **_k: None)  # no real wait
    for var, val in (("TMUX", in_tmux), ("SWARPH_SPAWN", spawn_marker)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    monkeypatch.delenv("SWARPH_WIN_ACK", raising=False)
    monkeypatch.delenv("SWARPH_FORCE_WT", raising=False)

    def _which(name):
        return {"tmux": tmux, "wt": wt}.get(name)

    monkeypatch.setattr(spawn.shutil, "which", _which)
    monkeypatch.setattr(spawn, "_console_is_genuine_wt", lambda: genuine_wt)
    monkeypatch.setattr(spawn, "_swarph_reentry_binary", lambda: "swarph")

    state = {"new_calls": 0, "created": False}

    def _run(cmd, **kwargs):
        if "has-session" in cmd:
            exists = session_exists or state["created"]
            return MagicMock(returncode=0 if exists else 1)
        if "new-session" in cmd:
            if new_session_exc is not None:
                raise new_session_exc
            state["new_calls"] += 1
            if create_succeeds and state["new_calls"] >= create_succeeds_after:
                state["created"] = True
            return MagicMock(returncode=0)
        return MagicMock(returncode=0)  # kill-session, attach, ...

    run = MagicMock(side_effect=_run)
    monkeypatch.setattr(spawn.subprocess, "run", run)
    run.popen = MagicMock()
    monkeypatch.setattr(spawn.subprocess, "Popen", run.popen)

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


def _killed_stale(run):
    """True if the stale-registration clear (`kill-session`) was issued."""
    return any("kill-session" in c for c in _cmds(run))


def _attached_via_run(run):
    """True if the Windows blocking-subprocess.run attach fired."""
    return [TMUX, "attach", "-t", SESSION] in _cmds(run)


def _attached_via_execv(execv):
    """True if the POSIX os.execv attach fired with the right argv."""
    return execv.call_args is not None and execv.call_args.args == (
        TMUX, [TMUX, "attach", "-t", SESSION]
    )


def _assert_create_command(create, platform):
    assert create[:5] == [TMUX, "new-session", "-d", "-s", SESSION]
    if platform == "win32":
        assert create[5:11] == [
            "--", "powershell.exe", "-NoProfile", "-ExecutionPolicy",
            "Bypass", "-Command",
        ]
        command = create[-1]
        assert "SWARPH_SPAWN='1'" in command
        assert str(CWD) in command
        assert "swarph" in command and "spawn" in command
    else:
        assert "-c" in create and str(CWD) in create
        assert "-e" in create and "SWARPH_SPAWN=1" in create
        assert create[-3:] == ["swarph", "spawn", SESSION]


def test_tmux_reentry_uses_active_windows_venv_entrypoint(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    reentry = scripts / "swarph.exe"
    python.touch()
    reentry.touch()
    monkeypatch.setattr(spawn.sys, "platform", "win32")
    monkeypatch.setattr(spawn.sys, "executable", str(python))

    assert spawn._swarph_reentry_binary() == str(reentry)


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


def test_windows_conhost_attaches_psmux_in_fresh_windows_terminal(monkeypatch):
    # An unverified console may be a corporate conhost even when WT_SESSION was
    # inherited. Move only the viewport to fresh WT; the durable cell is unchanged.
    r, run, execv = _drive(
        monkeypatch,
        platform="win32",
        session_exists=True,
        genuine_wt=False,
    )
    assert r is True
    run.popen.assert_called_once_with(
        [WT, "-d", str(CWD), "--", TMUX, "attach", "-t", SESSION],
    )
    assert not _attached_via_run(run)
    execv.assert_not_called()


def test_windows_conhost_falls_back_to_blocking_attach_without_wt(monkeypatch):
    r, run, execv = _drive(
        monkeypatch,
        platform="win32",
        wt=None,
        session_exists=True,
        genuine_wt=False,
    )
    assert r is True
    run.popen.assert_not_called()
    assert _attached_via_run(run)
    execv.assert_not_called()


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
    _assert_create_command(create, "win32")
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
    _assert_create_command(create, platform)
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
def test_create_never_materialises_falls_through(monkeypatch, platform):
    # The session never appears (e.g. a psmux stale registration that won't clear:
    # new-session returns rc 0 but creates nothing). The verify+retry loop exhausts
    # => return False so the caller drops to the standard launch; never attach a
    # phantom session. We also issued the stale-clear kill-session first.
    r, run, execv = _drive(monkeypatch, platform=platform,
                           session_exists=False, create_succeeds=False)
    assert r is False
    assert _killed_stale(run)
    assert not _attached_via_run(run)
    execv.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_stale_registration_cleared_then_create_retried(monkeypatch, platform):
    # The poisoned-name recovery path: kill-session clears the stale reg, but psmux
    # clears the lock async so the FIRST new-session is a silent no-op; the session
    # only materialises on the 2nd attempt. The verify loop must retry and succeed.
    r, run, execv = _drive(monkeypatch, platform=platform, session_exists=False,
                           isatty=False, create_succeeds_after=2)
    assert r is True                              # headless: created, claimed launch
    assert _killed_stale(run)                     # stale reg cleared first
    new_calls = [c for c in _cmds(run) if "new-session" in c]
    assert len(new_calls) >= 2                    # retried past the silent no-op
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
