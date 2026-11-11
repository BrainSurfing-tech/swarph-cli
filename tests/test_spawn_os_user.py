"""spawn honours cell.yaml ``os_user`` without changing today's argv.

Card #1573. ``os_user`` is an untyped key (``Cell.extra``). Absent, empty, or
equal to ``getpass.getuser()`` keeps the caller path: the provider argv and
the POSIX tmux attach/create argv stay what main builds for every membrane.
A different user is reached with ``sudo -iu`` against THAT user's tmux server.
The cell is never started as the caller, and the sudo argv carries no token,
no env assignment, no caller HOME, and never ``-n``.

subprocess and os.execv are mocked. These tests do not run sudo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from swarph_cli.cell import load_cell
from swarph_cli.commands import spawn
from swarph_cli.commands.spawn import run_spawn

FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
TMUX = "/usr/bin/tmux"
TOKEN = "TOKEN_SENTINEL_VALUE"
CALLER = "ubuntu"
OS_USER = "cell-opencode"

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


def _which(name):
    if name == "tmux":
        return TMUX
    return f"/usr/bin/{name}"


def _arm_posix_tmux(monkeypatch, *, session_exists):
    """Mock the caller tmux the way today's path uses it. Record every argv."""
    calls = []
    execs = []
    state = {"created": False}

    monkeypatch.setattr(spawn.sys, "platform", "linux")
    monkeypatch.setattr(spawn.sys, "stdout", _TTY())
    monkeypatch.setattr(spawn.shutil, "which", _which)
    monkeypatch.setattr(spawn.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: CALLER)

    def _run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        calls.append(argv)
        if argv and argv[0] == "sudo":
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
        assert not argv or argv[0] != "sudo", argv
        assert "-n" not in argv
    for file, argv in execs:
        assert file != "sudo"
        assert not argv or argv[0] != "sudo"
        assert "-n" not in argv


def _arm_foreign(monkeypatch, *, present, start_rc=0, caller=CALLER):
    """Mock sudo. Any caller-tmux or caller-exec path is a test failure.

    os.execv is mocked here, before the test body runs, so a missed override
    cannot exec a real sudo.
    """
    calls = []
    seen = []
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: caller)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", TOKEN)

    def _no_caller(*args, **kwargs):
        raise AssertionError(f"started the cell as the caller: {args!r} {kwargs!r}")

    for name in ("_launch_via_tmux", "_tmux_has_session", "_tmux_create_session"):
        monkeypatch.setattr(spawn, name, _no_caller)
    monkeypatch.setattr(spawn.os, "execve", _no_caller)
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
        seen.append((file, list(argv)))
        raise _ExecReplaced

    monkeypatch.setattr(spawn.os, "execv", _execv)
    return calls, seen


def _assert_sudo_boundary(argv, kwargs):
    """A sudo argv that contains -n, a token, or the caller HOME is a failure."""
    home = str(Path.home())
    assert isinstance(argv, list)
    assert argv[0] == "sudo"
    assert "-iu" in argv
    assert "-n" not in argv
    blob = "\n".join(argv)
    assert TOKEN not in blob
    assert home not in blob
    assert "ANTHROPIC" not in blob
    assert "API_KEY" not in blob
    assert "HOME=" not in blob
    for element in argv:
        # tmux's exact-match target is `=session`, which is not NAME=value.
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", element) is None, element
    assert "env" not in kwargs
    assert kwargs.get("shell", False) is False


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
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: caller)
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
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: caller)

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
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: caller)

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

    attach = ["sudo", "-iu", OS_USER, "tmux", "attach", "-t", role]
    probe = ["sudo", "-iu", OS_USER, "tmux", "has-session", "-t", f"={role}"]
    assert seen == [("sudo", attach)]
    assert [argv for argv, _kwargs in calls] == [probe]
    _assert_sudo_boundary(attach, {})
    _assert_sudo_boundary(probe, calls[0][1])
    assert all(argv[0] == "sudo" for argv, _kwargs in calls)
    assert not any("new-session" in argv for argv, _kwargs in calls)
    assert not any("swarph" in argv for argv, _kwargs in calls)


@pytest.mark.parametrize("provider", _providers())
def test_foreign_session_absent_starts_as_that_user_then_attaches(provider, monkeypatch):
    _path, role = _write_cell(provider, os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=False)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    probe = ["sudo", "-iu", OS_USER, "tmux", "has-session", "-t", f"={role}"]
    start = ["sudo", "-iu", OS_USER, "swarph", "spawn", role]
    attach = ["sudo", "-iu", OS_USER, "tmux", "attach", "-t", role]
    assert [argv for argv, _kwargs in calls] == [probe, start]
    assert seen == [("sudo", attach)]
    for argv, kwargs in calls:
        _assert_sudo_boundary(argv, kwargs)
    _assert_sudo_boundary(attach, {})
    # The start is not a swarph spawn in the caller's uid: sudo -iu is in front,
    # and nothing in the argv is the caller's tmux or a bare swarph.
    assert start[:4] == ["sudo", "-iu", OS_USER, "swarph"]
    assert all(argv[0] == "sudo" for argv in (probe, start, attach))


def test_measured_opencode_session_present(monkeypatch):
    """The pilot: uid's session name is opencode, the cell user is cell-opencode."""
    _write_cell("opencode", role="opencode", os_user=OS_USER, tmux_session="opencode")
    cell = load_cell(Path.home() / ".config" / "swarph" / "cells" / "opencode.yaml")
    assert cell.extra.get("os_user") == OS_USER
    assert cell.tmux_session == "opencode"
    calls, seen = _arm_foreign(monkeypatch, present=True)
    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    assert seen == [(
        "sudo",
        ["sudo", "-iu", "cell-opencode", "tmux", "attach", "-t", "opencode"],
    )]
    assert calls[0][0] == [
        "sudo", "-iu", "cell-opencode", "tmux", "has-session", "-t", "=opencode",
    ]


def test_tmux_session_pin_is_the_attach_target_and_role_is_the_start(monkeypatch):
    _write_cell(
        "opencode", role="opencode", os_user=OS_USER, tmux_session="pilot",
    )
    calls, seen = _arm_foreign(monkeypatch, present=False)
    with pytest.raises(_ExecReplaced):
        run_spawn(["opencode", "--no-banner"])

    assert calls[0][0] == [
        "sudo", "-iu", OS_USER, "tmux", "has-session", "-t", "=pilot",
    ]
    assert calls[1][0] == ["sudo", "-iu", OS_USER, "swarph", "spawn", "opencode"]
    assert seen == [(
        "sudo",
        ["sudo", "-iu", OS_USER, "tmux", "attach", "-t", "pilot"],
    )]


def test_inner_spawn_as_os_user_does_not_sudo_again(monkeypatch):
    """Inside the target user, os_user equals the caller, so today's path runs."""
    _path, role = _write_cell("opencode", role="opencode", os_user=OS_USER)
    calls, execs = _arm_posix_tmux(monkeypatch, session_exists=True)
    monkeypatch.setattr(spawn.getpass, "getuser", lambda: OS_USER)

    with pytest.raises(_ExecReplaced):
        run_spawn([role, "--no-banner"])

    _assert_no_sudo(calls, execs)
    assert execs == [(TMUX, [TMUX, "attach", "-t", role])]


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("provider", _providers())
def test_foreign_dry_run_prints_the_exact_sudo_command_and_does_not_exec(
    provider, present, monkeypatch, capsys,
):
    _path, role = _write_cell(provider, os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=present)

    def _execv(*args, **kwargs):
        raise AssertionError(f"dry-run exec'd: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _execv)
    assert seen == []
    rc = run_spawn([role, "--dry-run", "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    start = f"sudo -iu {OS_USER} swarph spawn {role}"
    attach = f"sudo -iu {OS_USER} tmux attach -t {role}"
    if present:
        assert captured.out.splitlines() == [attach]
    else:
        assert captured.out.splitlines() == [start, attach]
    assert [argv for argv, _kwargs in calls] == [[
        "sudo", "-iu", OS_USER, "tmux", "has-session", "-t", f"={role}",
    ]]
    _assert_sudo_boundary(calls[0][0], calls[0][1])
    assert TOKEN not in captured.out
    assert str(Path.home()) not in captured.out
    assert "-n" not in captured.out.split()


def test_start_failure_does_not_attach_or_fall_through(monkeypatch):
    _write_cell("opencode", role="opencode", os_user=OS_USER)
    calls, seen = _arm_foreign(monkeypatch, present=False, start_rc=7)

    def _execv(*args, **kwargs):
        raise AssertionError(f"attached after a failed start: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _execv)
    rc = run_spawn(["opencode", "--no-banner"])
    assert rc == 7
    assert [argv for argv, _kwargs in calls][1] == [
        "sudo", "-iu", OS_USER, "swarph", "spawn", "opencode",
    ]
    assert seen == []
    assert len(calls) == 2


def test_sudo_exec_oserror_does_not_fall_through_to_the_caller(monkeypatch):
    _write_cell("opencode", role="opencode", os_user=OS_USER)
    _calls, seen = _arm_foreign(monkeypatch, present=True)

    def _execv(*args, **kwargs):
        raise OSError("sudo not found")

    monkeypatch.setattr(spawn.os, "execv", _execv)
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

    def _execv(*args, **kwargs):
        raise AssertionError(f"invalid os_user exec'd: {args!r}")

    monkeypatch.setattr(spawn.os, "execv", _execv)
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
