"""``swarph add`` (T2) — dispatcher + handler registry + hook handler.

Exercises ``run_add`` / ``dispatch_add`` directly with tmp
``settings_path`` / ``hooks_home`` (no CLI shell-out). Covers:

* builtin hook installs (script written + bindings merged into settings)
* published hook fails closed (returns non-zero, mutates NOTHING)
* tool class (real handler) records a swarph-mesh adapter as a mesh lane;
  an unknown builtin tool adapter surfaces a clean ValueError (rc 2)
* unknown builtin name surfaces the resolve_builtin ValueError, nothing written
* bad URI returns 2, nothing written
* the ``add`` verb is registered in main._VERB_HANDLERS (round-trip wiring)
"""

from __future__ import annotations

import os
import sys

from swarph_cli.commands.add import dispatch_add, parse_uri, run_add
from swarph_cli.commands.hooks import _load_settings


# --------------------------------------------------------------------------- #
# builtin hook installs
# --------------------------------------------------------------------------- #


def test_builtin_hook_installs(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        ["swarph://hook/swarph-builtin/cell-resilience", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 0

    script = hooks_home / "cell-resilience.sh"
    assert script.exists()
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert os.stat(script).st_mode & 0o111

    # #216: install now writes a BASH-SAFE command (forward slashes on win32,
    # because Claude Code runs hooks through bash and backslash is an escape
    # char). `Path.as_posix()` is pathlib's OWN normalisation, used here as an
    # INDEPENDENT ORACLE — asserting against `hooks._hook_command_path` would
    # compare the implementation with itself and pass however wrong it was.
    # win32 now wraps that path in an EXPLICIT INTERPRETER (cursor-win, 2026-08-18:
    # a bare .sh path hit the file association and launched the IDE on every tool
    # call). The oracle stays INDEPENDENT by asserting the STRUCTURE rather than
    # re-deriving which bash — a quoted interpreter ending in bash.exe, never the
    # System32 WSL launcher, then pathlib's own as_posix() script path.
    _script_posix = script.resolve().as_posix()

    def _assert_command(cmd):
        if sys.platform != "win32":
            assert cmd == _script_posix
            return
        interp, sep, tail = cmd.partition('" "')
        assert sep, f"win32 command must name an interpreter: {cmd!r}"
        assert interp.startswith('"') and interp.lower().endswith("bash.exe"), interp
        assert "system32" not in interp.lower(), "must never be the WSL launcher"
        assert tail == _script_posix + '"', tail
    settings = _load_settings(settings_path)
    hooks = settings["hooks"]

    sf = hooks["StopFailure"]
    assert sf[0]["matcher"] == "rate_limit"
    _assert_command(sf[0]["hooks"][0]["command"])

    st = hooks["Stop"]
    assert st[0]["matcher"] == ""
    _assert_command(st[0]["hooks"][0]["command"])


# --------------------------------------------------------------------------- #
# published hook fails closed — mutate NOTHING
# --------------------------------------------------------------------------- #


def test_published_hook_fails_closed(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        ["swarph://hook/lab-ovh/cell-resilience", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc != 0

    # nothing written: no settings file, no script, empty/absent hooks dir
    assert not settings_path.exists()
    assert not (hooks_home / "cell-resilience.sh").exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not yet trusted" in combined


# --------------------------------------------------------------------------- #
# stub class — not yet implemented
# --------------------------------------------------------------------------- #


def test_unknown_builtin_tool_clean_error(tmp_path, capsys):
    # The ``tool`` class is now a real handler (bridges to swarph-mesh's
    # adapter registry). ``openrouter`` is NOT a swarph-mesh builtin adapter,
    # so this surfaces a clean ValueError (rc 2) and writes nothing.
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    lanes_path = tmp_path / "tool_lanes.json"

    rc = run_add(
        ["swarph://tool/swarph-builtin/openrouter", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
        lanes_path=lanes_path,
    )
    assert rc == 2

    assert not settings_path.exists()
    assert not lanes_path.exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "swarph add:" in combined
    assert "openrouter" in combined


# --------------------------------------------------------------------------- #
# unknown builtin name — resolve_builtin ValueError caught at CLI layer
# --------------------------------------------------------------------------- #


def test_unknown_builtin_name(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        ["swarph://hook/swarph-builtin/does-not-exist", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc != 0

    assert not settings_path.exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "swarph add:" in combined
    assert "does-not-exist" in combined


# --------------------------------------------------------------------------- #
# bad URI
# --------------------------------------------------------------------------- #


def test_bad_uri_returns_2(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        ["http://nope"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 2

    assert not settings_path.exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []

    captured = capsys.readouterr()
    assert "swarph add:" in (captured.out + captured.err)


# --------------------------------------------------------------------------- #
# dispatch_add directly — stub handler path
# --------------------------------------------------------------------------- #


def test_dispatch_add_routes_by_class(tmp_path):
    from swarph_cli.commands.add import build_registry

    registry = build_registry(
        settings_path=tmp_path / "settings.json",
        hooks_home=tmp_path / "hooks",
        lanes_path=tmp_path / "tool_lanes.json",
    )
    # Routes a ``tool`` URI to the real ToolHandler (4th class), which records
    # a swarph-mesh adapter as a mesh lane. "gemini" is a real builtin adapter.
    ref = parse_uri("swarph://tool/swarph-builtin/gemini")
    lines: list[str] = []
    rc = dispatch_add(ref, assume_yes=True, out=lines.append, registry=registry)
    assert rc == 0
    assert any("tool lane" in x for x in lines)


# --------------------------------------------------------------------------- #
# round-trip wiring
# --------------------------------------------------------------------------- #


def test_add_verb_registered_in_main():
    from swarph_cli import main

    assert "add" in main._VERB_HANDLERS
    assert main._VERB_HANDLERS["add"] == "swarph_cli.commands.add.run_add"
