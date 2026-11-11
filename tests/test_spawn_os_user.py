"""spawn honours cell.yaml ``os_user`` without changing today's argv.

Card #1573. ``os_user`` is an untyped key (``Cell.extra``). Absent, empty, or
the same uid as the caller keeps today's path: the provider argv and the
POSIX tmux attach/create argv stay what main builds for every membrane.
A different uid is reached with an absolute ``sudo -iu`` against THAT user's
tmux server. The env passed to sudo is PATH, TERM, and LANG only. A second
hand-off is refused. ``--dry-run`` and ``--print-resolved`` spawn nothing.

Identity tests do not mock getpass. The planted-sudo test does not point
shutil.which at the cwd copy. These tests do not run sudo.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

from swarph_cli.cell import load_cell
from swarph_cli.commands import spawn
from swarph_cli.commands.spawn import run_spawn

FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
# Absolute on Linux and Windows. which() is mocked to these in the matrix;
# the planted-sudo test uses the real shutil.which and must not.
SUDO = os.path.join(os.path.abspath(os.sep), "usr", "bin", "sudo")
TMUX = os.path.join(os.path.abspath(os.sep), "usr", "bin", "tmux")
HANDOFF = "--os-user-handoff"
TOKEN = "TOKEN_SENTINEL_VALUE"
CALLER = "ubuntu"
OS_USER = "cell-opencode"
CALLER_UID = 1000
OS_USER_UID = 991

_OMIT = object()


class _ExecReplaced(BaseException):
    """os.execv replaces the process. BaseException so `except OSError` does
    not swallow the mock."""


class _TTY:
    def isatty(self):
        return True

    def write(self, text):
        return len(text or "")

    def flush(self):
        return None


def _providers():
    return sorted(spawn.MEMBRANES)


@pytest.fixture(autouse=True)
def _no_inherited_spawn_env(monkeypatch):
    """A developer shell's channel or tmux must not change the argv under test."""
    for name in (
        "TMUX",
        "SWARPH_SPAWN",
        "SWARPH_CHANNEL",
        "SWARPH_CHANNEL_CELL",
        "SWARPH_WIN_ACK",
        "SWARPH_FORCE_WT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(spawn, "_opencode_prior_session", lambda _cell: None)


def _write_cell(provider, *, role=None, os_user=_OMIT, tmux_session=_OMIT):
    role = role or f"probe-{provider}"
    home = Path.home()
    cells = home / ".config" / "swarph" / "cells"
    cells.mkdir(parents=True, exist_ok=True)
    cwd = home / "cell-work" / role
    cwd.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "v1",
        "name": role,
        "role": role,
        "cwd": str(cwd),
        "provider": provider,
    }
    if os_user is not _OMIT:
        payload["os_user"] = os_user
    if tmux_session is not _OMIT:
        payload["tmux_session"] = tmux_session
    path = cells / f"{role}.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path, role


def _reference_argv(provider, role):
    """The argv main's membrane builds for this fixture. os_user is not an input."""
    cell = load_cell(
        Path.home() / ".config" / "swarph" / "cells" / f"{role}.yaml"
    )
    membrane = spawn.MEMBRANES[provider]
    if membrane.uses_pinned_session():
        session_id = FIXED_SESSION
        effective = role
    elif provider in ("antigravity", "grok", "cursor", "opencode"):
        session_id = "(fresh-session-per-spawn, no pinned id)"
        effective = role
    else:
        session_id = None
        effective = None
    return membrane.build_argv(
        cell,
        session_id=session_id,
        no_starter=False,
        passthrough=[],
        effective_role=effective,
    )


def _pin_session_mint(monkeypatch):
    monkeypatch.setattr(
        spawn,
        "load_or_create_session_id",
        lambda role, cell, new_instance=False: (FIXED_SESSION, True, role),
    )


def _which(name, path=None):
    if name == "sudo":
        return SUDO
    if name == "tmux":
        return TMUX
    return os.path.join(os.path.abspath(os.sep), "usr", "bin", name)


def _sudo_argv(argv):
    return bool(argv) and Path(argv[0]).name in {"sudo", "sudo.exe"}


def _install_ids(monkeypatch, euid, users):
    """Caller is os.geteuid(); os_user is pwd.getpwnam. getpass is not touched."""

    class _Pw:
        def __init__(self, name, uid):
            self.pw_name = name
            self.pw_uid = uid

    def getpwnam(name):
        try:
            uid = users[name]
        except KeyError:
            raise KeyError(name) from None
        return _Pw(name, uid)

    monkeypatch.setattr(spawn.os, "geteuid", lambda: euid, raising=False)
    pwd_mod = getattr(spawn, "pwd", None)
    if pwd_mod is None:
        monkeypatch.setattr(
            spawn, "pwd", types.SimpleNamespace(getpwnam=getpwnam), raising=False,
        )
    else:
        monkeypatch.setattr(pwd_mod, "getpwnam", getpwnam)


def _ids_for_variant(monkeypatch, variant):
    if variant == "inner":
        _install_ids(monkeypatch, OS_USER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID})
    else:
        _install_ids(monkeypatch, CALLER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID})


def _arm_posix_tmux(monkeypatch, *, session_exists):
    """Mock the caller tmux the way today's path uses it. Record every argv."""
    calls = []
    execs = []
    state = {"created": False}

    monkeypatch.setattr(spawn.sys, "platform", "linux")
    monkeypatch.setattr(spawn.sys, "stdout", _TTY())
    monkeypatch.setattr(spawn.shutil, "which", _which)
    monkeypatch.setattr(spawn.time, "sleep", lambda *_a, **_k: None)

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        calls.append(argv)
        if _sudo_argv(argv):
            raise AssertionError(f"sudo on today's path: {argv}")
        if "has-session" in argv:
            exists = session_exists or state["created"]
            return spawn.subprocess.CompletedProcess(argv, 0 if exists else 1, "", "")
        if "new-session" in argv:
            state["created"] = True
        return spawn.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(spawn.subprocess, "run", _run)

    def _execv(file, argv):
        execs.append((file, list(argv)))
        raise _ExecReplaced

    monkeypatch.setattr(spawn.os, "execv", _execv)

    def _execve(*args, **kwargs):
        raise AssertionError(f"caller execve: {args!r} {kwargs!r}")

    monkeypatch.setattr(spawn.os, "execve", _execve)
    return calls, execs


def _assert_no_sudo(calls, execs):
    for argv in calls:
        assert not _sudo_argv(argv), argv
        assert "-n" not in argv
    for file, argv in execs:
        assert Path(str(file)).name not in {"sudo", "sudo.exe"}
        assert not _sudo_argv(argv)
        assert "-n" not in argv


def _arm_foreign(monkeypatch, *, present, start_rc=0):
    """Mock sudo. Any caller-tmux path is a test failure.

    os.execve is recorded here, before the test body runs, so a missed
    override cannot exec a real sudo. os.execv is a failure: it would
    inherit the caller environment. getpass is not mocked. LOGNAME and
    USER are the os_user name, which is what getpass would believe.
    """
    calls = []
    seen = []
    monkeypatch.setenv("LOGNAME", OS_USER)
    monkeypatch.setenv("USER", OS_USER)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(spawn.shutil, "which", _which)
    _install_ids(
        monkeypatch, CALLER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID},
    )

    def _no_caller(*args, **kwargs):
        raise AssertionError(f"started the cell as the caller: {args!r} {kwargs!r}")

    for name in ("_launch_via_tmux", "_tmux_has_session", "_tmux_create_session"):
        monkeypatch.setattr(spawn, name, _no_caller)
    monkeypatch.setattr(spawn.subprocess, "Popen", _no_caller)

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        calls.append((argv, kwargs))
        if "has-session" in argv:
            code = 0 if present else 1
            return spawn.subprocess.CompletedProcess(argv, code, "", "")
        return spawn.subprocess.CompletedProcess(argv, start_rc, "", "")

    monkeypatch.setattr(spawn.subprocess, "run", _run)

    def _execv(file, argv):
        raise AssertionError(
            f"execv inherits the caller environment: {file} {argv}"
        )

    def _execve(file, argv, env):
        seen.append((file, list(argv), env))
        raise _ExecReplaced

    monkeypatch.setattr(spawn.os, "execv", _execv)
    monkeypatch.setattr(spawn.os, "execve", _execve)
    return calls, seen


def _assert_minimal_env(env):
    home = str(Path.home())
    assert isinstance(env, dict)
    assert set(env) <= {"PATH", "TERM", "LANG"}
    assert "HOME" not in env
    joined = "\n".join(f"{key}={value}" for key, value in env.items())
    assert TOKEN not in joined
    assert "ANTHROPIC" not in joined
    assert "API_KEY" not in joined
    assert home not in env.get("HOME", "")
    for key in env:
        assert "TOKEN" not in key
        assert "SECRET" not in key
        assert not key.endswith("_KEY")


def _assert_sudo_boundary(argv, env):
    """Absolute sudo, no -n, no token, no HOME, and a minimal env."""
    home = str(Path.home())
    assert isinstance(argv, list)
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).name == "sudo"
    assert argv[0] == SUDO
    assert "-iu" in argv
    assert "-n" not in argv
    blob = "\n".join(argv)
    assert TOKEN not in blob
    assert home not in blob
    assert "ANTHROPIC" not in blob
    assert "API_KEY" not in blob
    assert "HOME=" not in blob
    assert "HOME" not in argv
    for element in argv:
        # tmux's exact-match target is `=session`, which is not NAME=value.
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", element) is None, element
    if "has-session" in argv or "attach" in argv:
        assert argv[3] == TMUX
        assert Path(argv[3]).is_absolute()
    if "spawn" in argv:
        assert argv[3] == "swarph"
        assert HANDOFF in argv
    _assert_minimal_env(env)


# --- (1) unset / empty / self: byte-identical to today's argv ---------------


def test_matrix_covers_every_membrane():
    assert set(_providers()) >= {
        "claude", "codex", "antigravity", "grok", "vibe",
        "muse", "cursor", "opencode", "copilot",
    }


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize(
    "variant",
    ["absent", "empty", "null", "self", "inner"],
)
def test_os_user_unset_dry_run_argv_matches_main(provider, variant, monkeypatch, capsys):
    _pin_session_mint(monkeypatch)
    caller = OS_USER if variant == "inner" else CALLER
    _ids_for_variant(monkeypatch, variant)
    os_user = {
        "absent": _OMIT,
        "empty": "",
        "null": None,
        "self": caller,
        "inner": caller,
    }[variant]
    role = f"probe-{provider}"
    # Reference is the membrane argv of a cell with no os_user key — main's argv.
    _write_cell(provider)
    reference = _reference_argv(provider, role)
    _write_cell(provider, os_user=os_user)

    rc = run_spawn([role, "--dry-run", "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.out.strip() == " ".join(reference)
    assert "sudo" not in captured.out
    assert reference[0] != "sudo"


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize("variant", ["absent", "empty", "null", "self", "inner"])
def test_os_user_unset_posix_attach_argv_matches_main(provider, variant, monkeypatch):
    caller = OS_USER if variant == "inner" else CALLER
    os_user = {
        "absent": _OMIT,
        "empty": "",
        "null": None,
        "self": caller,
        "inner": caller,
    }[variant]
    _path, role = _write_cell(provider, os_user=os_user)
    calls, execs = _arm_posix_tmux(monkeypatch, session_exists=True)
    _ids_for_variant(monkeypatch, variant)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    _assert_no_sudo(calls, execs)
    assert execs == [(TMUX, [TMUX, "attach", "-t", role])]
    assert not any("new-session" in argv for argv in calls)
    assert [TMUX, "has-session", "-t", f"={role}"] in calls


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize("variant", ["absent", "empty", "self"])
def test_os_user_unset_posix_create_argv_matches_main(provider, variant, monkeypatch):
    caller = CALLER
    os_user = {"absent": _OMIT, "empty": "", "self": caller}[variant]
    _path, role = _write_cell(provider, os_user=os_user)
    cell = load_cell(_path)
    calls, execs = _arm_posix_tmux(monkeypatch, session_exists=False)
    _ids_for_variant(monkeypatch, variant)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    _assert_no_sudo(calls, execs)
    creates = [argv for argv in calls if "new-session" in argv]
    assert creates == [spawn._tmux_session_command(TMUX, role, cell.cwd)]
    assert execs == [(TMUX, [TMUX, "attach", "-t", role])]


# --- (2)(3)(4)(5) foreign os_user: sudo, never the caller -------------------


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize("tty", [True, False])
def test_foreign_session_present_execs_exact_sudo_attach(provider, tty, monkeypatch):
    _path, role = _write_cell(provider, os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=True)
    if tty:
        monkeypatch.setattr(spawn.sys, "stdout", _TTY())

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    attach = [SUDO, "-iu", OS_USER, TMUX, "attach", "-t", role]
    probe = [SUDO, "-iu", OS_USER, TMUX, "has-session", "-t", f"={role}"]
    assert [(file, argv) for file, argv, _env in seen] == [(SUDO, attach)]
    assert [argv for argv, _kwargs in calls] == [probe]
    _assert_sudo_boundary(attach, seen[0][2])
    _assert_sudo_boundary(probe, calls[0][1]["env"])
    assert all(argv[0] == SUDO for argv, _kwargs in calls)
    assert not any("new-session" in argv for argv, _kwargs in calls)
    assert not any("swarph" in argv for argv, _kwargs in calls)


@pytest.mark.parametrize("provider", _providers())
def test_foreign_session_absent_starts_as_that_user_then_attaches(provider, monkeypatch):
    _path, role = _write_cell(provider, os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=False)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    probe = [SUDO, "-iu", OS_USER, TMUX, "has-session", "-t", f"={role}"]
    start = [SUDO, "-iu", OS_USER, "swarph", "spawn", role, HANDOFF]
    attach = [SUDO, "-iu", OS_USER, TMUX, "attach", "-t", role]
    assert [argv for argv, _kwargs in calls] == [probe, start]
    assert [(file, argv) for file, argv, _env in seen] == [(SUDO, attach)]
    for argv, kwargs in calls:
        _assert_sudo_boundary(argv, kwargs["env"])
    _assert_sudo_boundary(attach, seen[0][2])
    # The start is not a swarph spawn in the caller's uid: sudo -iu is in front.
    # swarph stays a bare name. The hand-off flag rides on the argv.
    assert start[:4] == [SUDO, "-iu", OS_USER, "swarph"]
    assert all(Path(argv[0]).is_absolute() for argv in (probe, start, attach))


def test_measured_opencode_session_present(monkeypatch):
    """The pilot: uid's session name is opencode, the cell user is cell-opencode."""
    _write_cell("opencode", role="opencode", os_user=OS_USER, tmux_session="opencode")
    cell = load_cell(Path.home() / ".config" / "swarph" / "cells" / "opencode.yaml")
    assert cell.extra.get("os_user") == OS_USER
    assert cell.tmux_session == "opencode"
    calls, seen = _arm_foreign(monkeypatch, present=True)
    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    assert [(file, argv) for file, argv, _env in seen] == [(
        SUDO,
        [SUDO, "-iu", "cell-opencode", TMUX, "attach", "-t", "opencode"],
    )]
    assert calls[0][0] == [
        SUDO, "-iu", "cell-opencode", TMUX, "has-session", "-t", "=opencode",
    ]
    _assert_minimal_env(seen[0][2])
    _assert_minimal_env(calls[0][1]["env"])


def test_tmux_session_pin_is_the_attach_target_and_role_is_the_start(monkeypatch):
    _write_cell(
        "opencode", role="opencode", os_user=OS_USER, tmux_session="pilot",
    )
    calls, seen = _arm_foreign(monkeypatch, present=False)
    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    assert calls[0][0] == [
        SUDO, "-iu", OS_USER, TMUX, "has-session", "-t", "=pilot",
    ]
    assert calls[1][0] == [
        SUDO, "-iu", OS_USER, "swarph", "spawn", "opencode", HANDOFF,
    ]
    assert [(file, argv) for file, argv, _env in seen] == [(
        SUDO,
        [SUDO, "-iu", OS_USER, TMUX, "attach", "-t", "pilot"],
    )]


def test_inner_spawn_as_os_user_does_not_sudo_again(monkeypatch):
    """Inside the target uid, os_user's pw_uid matches. Today's path runs.

    getpass is not mocked. LOGNAME is a decoy so a name comparison would sudo.
    """
    _path, role = _write_cell("opencode", role="opencode", os_user=OS_USER)
    calls, execs = _arm_posix_tmux(monkeypatch, session_exists=True)
    _install_ids(monkeypatch, OS_USER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID})
    monkeypatch.setenv("LOGNAME", "decoy-logname")
    monkeypatch.setenv("USER", "decoy-logname")

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    _assert_no_sudo(calls, execs)
    assert execs == [(TMUX, [TMUX, "attach", "-t", role])]


@pytest.mark.parametrize("flag", ["--dry-run", "--print-resolved"])
@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("provider", _providers())
def test_capture_flags_print_commands_and_spawn_nothing(
    provider, present, flag, monkeypatch, capsys,
):
    _path, role = _write_cell(provider, os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=present)

    def _exec(*args, **kwargs):
        raise AssertionError(f"capture flag exec'd: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _exec)
    monkeypatch.setattr(spawn.os, "execve", _exec)
    rc = run_spawn([role, flag, "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    # shlex.join quotes a Windows path that contains a backslash. That quoted
    # line is the command the operator would run; compare argv, not a raw join.
    start = [SUDO, "-iu", OS_USER, "swarph", "spawn", role, HANDOFF]
    attach = [SUDO, "-iu", OS_USER, TMUX, "attach", "-t", role]
    assert [shlex.split(line) for line in captured.out.splitlines()] == [start, attach]
    assert calls == []
    assert seen == []
    assert TOKEN not in captured.out
    assert str(Path.home()) not in captured.out
    assert "-n" not in captured.out.split()


def test_start_failure_does_not_attach_or_fall_through(monkeypatch):
    _write_cell("opencode", role="opencode", os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=False, start_rc=7)

    def _exec(*args, **kwargs):
        raise AssertionError(f"attached after a failed start: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _exec)
    monkeypatch.setattr(spawn.os, "execve", _exec)
    rc = run_spawn(["opencode", "--no-banner"])
    assert rc == 7
    assert [argv for argv, _kwargs in calls][1] == [
        SUDO, "-iu", OS_USER, "swarph", "spawn", "opencode", HANDOFF,
    ]
    _assert_minimal_env(calls[0][1]["env"])
    _assert_minimal_env(calls[1][1]["env"])
    assert seen == []
    assert len(calls) == 2


def test_sudo_exec_oserror_does_not_fall_through_to_the_caller(monkeypatch):
    _write_cell("opencode", role="opencode", os_user=OS_USER)
    _calls, seen = _arm_foreign(monkeypatch, present=True)

    def _execve(*args, **kwargs):
        raise OSError("sudo not found")

    monkeypatch.setattr(spawn.os, "execve", _execve)
    rc = run_spawn(["opencode", "--no-banner"])
    assert rc == 1
    assert seen == []


@pytest.mark.parametrize(
    "value,reason",
    [
        ("cell opencode", "whitespace"),
        (" cell-opencode", "whitespace"),
        ("cell-opencode\n", "whitespace"),
        ("cell/opencode", "slash"),
        ("../cell-opencode", "slash"),
        ("-cell-opencode", "leading dash"),
        ("cell-opencode -n", "whitespace"),
        ("'cell-opencode'", "would add an argv element"),
        ("a" * 33, "not a username"),
        ("1opencode", "not a username"),
        (["cell-opencode", "extra"], "not a string"),
        ("cell-opencode;token", "not a username"),
    ],
)
def test_invalid_os_user_fails_without_sudo_or_a_caller_spawn(
    value, reason, monkeypatch, capsys,
):
    _write_cell("opencode", role="opencode", os_user=value)
    calls, _seen = _arm_foreign(monkeypatch, present=True)

    def _exec(*args, **kwargs):
        raise AssertionError(f"invalid os_user exec'd: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _exec)
    monkeypatch.setattr(spawn.os, "execve", _exec)
    for extra in ([],):
        rc = run_spawn(["opencode", "--dry-run", "--no-banner", *extra])
        captured = capsys.readouterr()
        assert rc == 1
        assert reason in captured.err
        assert "os_user" in captured.err
    rc = run_spawn(["opencode", "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 1
    assert reason in captured.err
    assert calls == []


def test_logname_set_to_os_user_still_takes_the_sudo_branch(monkeypatch):
    """LOGNAME and USER name the os_user. The uid does not. getpass is not mocked."""
    _write_cell("opencode", role="opencode", os_user=OS_USER)
    monkeypatch.setenv("LOGNAME", OS_USER)
    monkeypatch.setenv("USER", OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=True)

    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    attach = [SUDO, "-iu", OS_USER, TMUX, "attach", "-t", "opencode"]
    assert [(file, argv) for file, argv, _env in seen] == [(SUDO, attach)]
    assert calls and calls[0][0][0] == SUDO
    _assert_minimal_env(seen[0][2])
    _assert_minimal_env(calls[0][1]["env"])
    assert TOKEN not in calls[0][1]["env"]
    assert "HOME" not in calls[0][1]["env"]


def test_same_uid_alias_takes_todays_path(monkeypatch):
    """A different name with the same pw_uid is today's path. getpass is not mocked."""
    _path, role = _write_cell("opencode", role="opencode", os_user=OS_USER)
    calls, execs = _arm_posix_tmux(monkeypatch, session_exists=True)
    _install_ids(
        monkeypatch, CALLER_UID, {CALLER: CALLER_UID, OS_USER: CALLER_UID},
    )
    monkeypatch.setenv("LOGNAME", CALLER)
    monkeypatch.setenv("USER", CALLER)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    _assert_no_sudo(calls, execs)
    assert execs == [(TMUX, [TMUX, "attach", "-t", role])]


def test_unknown_os_user_is_refused(monkeypatch, capsys):
    _write_cell("opencode", role="opencode", os_user="swarph-nosuch-user")
    calls, _seen = _arm_foreign(monkeypatch, present=True)
    rc = run_spawn(["opencode", "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not a user" in captured.err
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX which and a planted ./sudo")
def test_planted_sudo_in_cwd_is_not_the_path_that_is_execd(monkeypatch, tmp_path):
    """A cwd ./sudo that which() would find first is not exec'd or spawned.

    shutil.which is the real one. It is not pointed at the planted copy.
    """
    work = tmp_path / "work"
    bindir = tmp_path / "bin"
    work.mkdir()
    bindir.mkdir()
    planted = work / "sudo"
    planted_tmux = work / "tmux"
    real_sudo = bindir / "sudo"
    real_tmux = bindir / "tmux"
    for path in (planted, planted_tmux, real_sudo, real_tmux):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    _write_cell("opencode", role="opencode", os_user=OS_USER)
    _install_ids(
        monkeypatch, CALLER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID},
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(work), str(bindir)]))
    monkeypatch.setenv("LOGNAME", CALLER)
    monkeypatch.setenv("USER", CALLER)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)

    import shutil
    assert spawn.shutil.which is shutil.which
    naive = shutil.which("sudo")
    assert naive is not None
    assert Path(naive).resolve() == planted.resolve()

    spawned = []

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        spawned.append((argv, kwargs.get("env")))
        if "has-session" in argv:
            return spawn.subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected subprocess: {argv}")

    def _execve(file, argv, env):
        spawned.append((list(argv), env))
        raise _ExecReplaced

    def _execv(file, argv):
        spawned.append((list(argv), None))
        raise _ExecReplaced

    monkeypatch.setattr(spawn.subprocess, "run", _run)
    monkeypatch.setattr(spawn.os, "execve", _execve)
    monkeypatch.setattr(spawn.os, "execv", _execv)

    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    assert len(spawned) == 2
    for argv, env in spawned:
        assert Path(argv[0]).is_absolute()
        assert Path(argv[0]).resolve() == real_sudo.resolve()
        assert Path(argv[0]).resolve() != planted.resolve()
        assert str(planted) not in argv
        if "attach" in argv or "has-session" in argv:
            assert Path(argv[3]).resolve() == real_tmux.resolve()
            assert Path(argv[3]).resolve() != planted_tmux.resolve()
        _assert_minimal_env(env)
        assert "HOME" not in env
        assert all(TOKEN not in value for value in env.values())


def test_two_yamls_that_name_each_other_refuse_the_second_handoff(monkeypatch, tmp_path, capsys):
    alice_home = tmp_path / "alice"
    bob_home = tmp_path / "bob"
    homes = {"alice": alice_home, "bob": bob_home}
    users = {"alice": 10, "bob": 20}
    for name, other in (("alice", "bob"), ("bob", "alice")):
        cells = homes[name] / ".config" / "swarph" / "cells"
        cells.mkdir(parents=True)
        cwd = homes[name] / "work"
        cwd.mkdir()
        payload = {
            "schema_version": "v1",
            "name": "opencode",
            "role": "opencode",
            "cwd": str(cwd),
            "provider": "opencode",
            "os_user": other,
            "tmux_session": "opencode",
        }
        (cells / "opencode.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    if not hasattr(os, "geteuid"):
        # Windows has no geteuid. Do not install one. The hand-off refuses.
        monkeypatch.setenv("HOME", str(alice_home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        calls = []

        def _run(*args, **kwargs):
            calls.append(args)
            return spawn.subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(spawn.subprocess, "run", _run)
        rc = run_spawn(["opencode", "--no-banner"])
        captured = capsys.readouterr()
        assert rc != 0
        assert calls == []
        assert "POSIX" in captured.err
        return

    euid = {"v": 10}
    monkeypatch.setattr(spawn.os, "geteuid", lambda: euid["v"], raising=False)

    class _Pw:
        def __init__(self, name, uid):
            self.pw_name = name
            self.pw_uid = uid

    def _getpwnam(name):
        return _Pw(name, users[name])

    pwd_mod = getattr(spawn, "pwd", None)
    if pwd_mod is None:
        monkeypatch.setattr(
            spawn, "pwd", types.SimpleNamespace(getpwnam=_getpwnam), raising=False,
        )
    else:
        monkeypatch.setattr(pwd_mod, "getpwnam", _getpwnam)
    monkeypatch.setattr(spawn.shutil, "which", _which)
    monkeypatch.setenv("HOME", str(alice_home))
    monkeypatch.setenv("LOGNAME", "alice")
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    spawns = []
    execs = []

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        _assert_sudo_boundary(argv, kwargs.get("env"))
        if "has-session" in argv:
            return spawn.subprocess.CompletedProcess(argv, 1, "", "")
        if "spawn" in argv:
            spawns.append(argv)
            if len(spawns) > 1:
                return spawn.subprocess.CompletedProcess(argv, 99, "", "")
            target = argv[2]
            euid["v"] = users[target]
            monkeypatch.setenv("HOME", str(homes[target]))
            monkeypatch.setenv("LOGNAME", target)
            monkeypatch.setenv("USER", target)
            inner = argv[argv.index("spawn") + 1:]
            rc = run_spawn(inner)
            return spawn.subprocess.CompletedProcess(argv, rc, "", "")
        raise AssertionError(argv)

    def _exec(*args, **kwargs):
        execs.append(args)
        raise AssertionError(f"exec during a refused hand-off: {args!r}")

    monkeypatch.setattr(spawn.subprocess, "run", _run)
    monkeypatch.setattr(spawn.os, "execve", _exec)
    monkeypatch.setattr(spawn.os, "execv", _exec)

    rc = run_spawn(["opencode", "--no-banner"])
    captured = capsys.readouterr()
    assert rc != 0
    assert len(spawns) == 1
    assert HANDOFF in spawns[0]
    assert execs == []
    assert "second" in captured.err


def _real_bin_path(monkeypatch, tmp_path, *names):
    """Put real executables on PATH and leave shutil.which unpatched."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in names:
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert spawn.shutil.which is shutil.which
    return bindir


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX which resolution")
@pytest.mark.parametrize("flag", ["--dry-run", "--print-resolved"])
def test_os_user_forwards_spawn_flags_without_spawning(flag, monkeypatch, tmp_path, capsys):
    """--new-instance, --no-starter, and passthrough survive onto the sudo argv.

    shutil.which is the real one. The printed sudo path is that result.
    """
    _real_bin_path(monkeypatch, tmp_path, "sudo", "tmux")
    _write_cell("opencode", role="opencode", os_user=OS_USER, tmux_session="opencode")
    _install_ids(monkeypatch, CALLER_UID, {CALLER: CALLER_UID, OS_USER: OS_USER_UID})
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"capture flag spawned: {args!r}")

    monkeypatch.setattr(spawn.subprocess, "run", _run)
    monkeypatch.setattr(spawn.os, "execve", _run)
    monkeypatch.setattr(spawn.os, "execv", _run)

    rc = run_spawn([
        "opencode", flag, "--new-instance", "--no-starter", "--no-banner",
        "--", "--resume",
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert calls == []
    sudo = shutil.which("sudo")
    tmux = shutil.which("tmux")
    assert sudo and Path(sudo).is_absolute() and Path(sudo).name == "sudo"
    assert tmux and Path(tmux).is_absolute()
    start, attach = captured.out.splitlines()
    assert start == (
        f"{sudo} -iu {OS_USER} swarph spawn opencode "
        f"--new-instance --no-starter {HANDOFF} -- --resume"
    )
    assert attach == f"{sudo} -iu {OS_USER} {tmux} attach -t opencode"
    assert start.split()[0] != "sudo"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX which resolution")
def test_handoff_inner_does_not_attach_and_outer_attaches_once(monkeypatch, tmp_path):
    """A tty with no session starts detached. Only the outer sudo attach runs.

    Real shutil.which. The recorded exec path is that absolute sudo, not the
    bare name.
    """
    _real_bin_path(monkeypatch, tmp_path, "sudo", "tmux", "opencode")
    _write_cell("opencode", role="opencode", os_user=OS_USER, tmux_session="opencode")
    euid = {"v": CALLER_UID}
    users = {CALLER: CALLER_UID, OS_USER: OS_USER_UID}

    class _Pw:
        def __init__(self, name, uid):
            self.pw_name = name
            self.pw_uid = uid

    def getpwnam(name):
        try:
            uid = users[name]
        except KeyError:
            raise KeyError(name) from None
        return _Pw(name, uid)

    monkeypatch.setattr(spawn.os, "geteuid", lambda: euid["v"], raising=False)
    pwd_mod = getattr(spawn, "pwd", None)
    if pwd_mod is None:
        monkeypatch.setattr(
            spawn, "pwd", types.SimpleNamespace(getpwnam=getpwnam), raising=False,
        )
    else:
        monkeypatch.setattr(pwd_mod, "getpwnam", getpwnam)
    monkeypatch.setattr(spawn.sys, "platform", "linux")
    monkeypatch.setattr(spawn.sys, "stdout", _TTY())
    monkeypatch.setattr(spawn.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("LOGNAME", OS_USER)
    monkeypatch.setenv("USER", OS_USER)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)

    sudo = shutil.which("sudo")
    tmux = shutil.which("tmux")
    assert Path(sudo).is_absolute() and sudo != "sudo"
    created = []
    attaches = []

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        base = Path(argv[0]).name
        if base == "sudo" and "has-session" in argv:
            assert Path(argv[0]).resolve() == Path(sudo).resolve()
            _assert_minimal_env(kwargs.get("env"))
            return spawn.subprocess.CompletedProcess(argv, 1, "", "")
        if base == "sudo" and "spawn" in argv:
            assert Path(argv[0]).resolve() == Path(sudo).resolve()
            assert argv[0] != "sudo"
            _assert_minimal_env(kwargs.get("env"))
            euid["v"] = OS_USER_UID
            inner = argv[argv.index("spawn") + 1:]
            try:
                rc = run_spawn(inner)
            except _ExecReplaced:
                rc = 0
            return spawn.subprocess.CompletedProcess(argv, rc, "", "")
        if base == "tmux" and "new-session" in argv:
            created.append(argv)
            return spawn.subprocess.CompletedProcess(argv, 0, "", "")
        if base == "tmux" and "has-session" in argv:
            code = 0 if created else 1
            return spawn.subprocess.CompletedProcess(argv, code, "", "")
        if base == "tmux" and "kill-session" in argv:
            return spawn.subprocess.CompletedProcess(argv, 0, "", "")
        if "attach" in argv:
            attaches.append(("run", argv))
            return spawn.subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    def _execv(file, argv):
        attaches.append(("execv", file, list(argv)))
        raise _ExecReplaced

    def _execve(file, argv, env):
        attaches.append(("execve", file, list(argv), env))
        raise _ExecReplaced

    monkeypatch.setattr(spawn.subprocess, "run", _run)
    monkeypatch.setattr(spawn.os, "execv", _execv)
    monkeypatch.setattr(spawn.os, "execve", _execve)

    with pytest.raises(_ExecReplaced):
        run_spawn([
            "opencode", "--new-instance", "--no-starter", "--no-banner",
            "--", "--resume",
        ])

    execvs = [item for item in attaches if item[0] == "execv"]
    execves = [item for item in attaches if item[0] == "execve"]
    assert execvs == []
    assert [item[0] for item in attaches if item[0] == "run"] == []
    assert len(execves) == 1
    _kind, _file, argv, env = execves[0]
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).resolve() == Path(sudo).resolve()
    assert argv[0] != "sudo"
    assert argv[3] == tmux
    assert "attach" in argv
    _assert_minimal_env(env)
    assert len(created) == 1
    pane = created[0]
    assert Path(pane[0]).resolve() == Path(tmux).resolve()
    assert "--new-instance" in pane
    assert "--no-starter" in pane
    assert "--resume" in pane
    assert HANDOFF not in pane
    spawn_at = pane.index("spawn")
    assert pane[spawn_at + 1] == "opencode"
