"""``swarph add`` (T2) — dispatcher + handler registry + hook handler.

Exercises ``run_add`` / ``dispatch_add`` directly with tmp
``settings_path`` / ``hooks_home`` (no CLI shell-out). Covers:

* builtin hook installs (script written + bindings merged into settings)
* published hook fails closed (returns non-zero, mutates NOTHING)
* stub class (mcp) returns 3 with a "not yet implemented" message
* unknown builtin name surfaces the resolve_builtin ValueError, nothing written
* bad URI returns 2, nothing written
* the ``add`` verb is registered in main._VERB_HANDLERS (round-trip wiring)
"""

from __future__ import annotations

import os

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
    assert os.stat(script).st_mode & 0o111

    expected_command = str(script.resolve())
    settings = _load_settings(settings_path)
    hooks = settings["hooks"]

    sf = hooks["StopFailure"]
    assert sf[0]["matcher"] == "rate_limit"
    assert sf[0]["hooks"][0]["command"] == expected_command

    st = hooks["Stop"]
    assert st[0]["matcher"] == ""
    assert st[0]["hooks"][0]["command"] == expected_command


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


def test_stub_class_clean_error(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        ["swarph://mcp/swarph-builtin/fmp", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 3

    assert not settings_path.exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not yet implemented" in combined


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
    )
    ref = parse_uri("swarph://skill/swarph-builtin/pdf")
    lines: list[str] = []
    rc = dispatch_add(ref, assume_yes=True, out=lines.append, registry=registry)
    assert rc == 3
    assert any("not yet implemented" in x for x in lines)


# --------------------------------------------------------------------------- #
# round-trip wiring
# --------------------------------------------------------------------------- #


def test_add_verb_registered_in_main():
    from swarph_cli import main

    assert "add" in main._VERB_HANDLERS
    assert main._VERB_HANDLERS["add"] == "swarph_cli.commands.add.run_add"
