"""The migration ladder must be REACHED, not merely present. #216's hole, third time.

REPRODUCED ON METAL by workstation-lc, 2026-08-18, migrating its own Windows box to
#250's canonical form: `swarph hooks remove NAME` reported *"removed — open /hooks once
or restart to deactivate"* for all three builtins, and the gen-2 bare-forward-slash
entries in settings.json SURVIVED UNTOUCHED. It had to hand-edit the file.

>>> AND THE HELPER THAT KNEW ABOUT THE OLDER GENERATIONS HAD ZERO PRODUCTION CALLERS. <<<
`_installed_command_variants` was consulted by NOTHING — not install, not uninstall, not
list. It was dead code wearing a docstring about a bug it was not preventing.

THE PART THAT IS LAB'S: that same morning lab extended it from two generations to three
and wrote a test asserting `len(variants) == 3`. The test called the helper DIRECTLY, so
it passed while nothing else invoked the helper at all. >>> A FUNCTION VERIFIED ONLY
THROUGH ITS OWN FRONT DOOR CANNOT REVEAL THAT IT HAS NO CALLERS. <<< Every test in this
file therefore drives the PUBLIC verb — install / uninstall / list — and never calls
`_installed_command_variants`.

THE COST OF THE GAP, measured rather than argued: remove silently no-ops on a legacy
entry, the operator reinstalls, install MERGES the canonical binding ALONGSIDE the
surviving legacy one, and the hook FIRES TWICE. Every Windows cell that installed before
2026-08-18 is in that state.

The docstring on `_installed_command_variants` says "ONE HELPER, TWO CALLERS" and names
install + list. Uninstall was the third, missed a third time — which is what says the
ladder must be reached through one path rather than remembered at each site.

>>> AND THE FIRST DRAFT OF THIS FILE WAS GREEN AGAINST THE UNFIXED CODE. <<< Reverting
uninstall to the canonical-only string — the exact defect — left all four tests passing.
Cause: on POSIX all three generations COLLAPSE TO ONE STRING, so the "legacy" entry the
fixture wrote was already the canonical one and the ladder was never exercised. The suite
read the same green whether the claim was true or false, which makes it a proxy for the
fix and not a measurement of it. The generations only differ on win32, so every test here
runs under a SIMULATED win32 (`hooks.sys.platform`, the #216 idiom) and the mutation now
fails three of the four. The POSIX collapse gets its own explicit test rather than being
the accidental condition under which nothing is checked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import hooks


_BASH = "C:/Program Files/Git/bin/bash.exe"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "hooks").mkdir()
    return tmp_path


@pytest.fixture
def win32(monkeypatch):
    """Run the body as if on Windows — the ONLY platform where the three command
    generations are distinct strings, and therefore the only one on which these tests
    can fail when the ladder is not reached. Pinning the interpreter too keeps the
    canonical form independent of whatever bash the host happens to have."""
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks, "_windows_hook_bash", lambda: _BASH)


def _settings_with(path: Path, bundle, command: str) -> None:
    """Write a settings.json holding EVERY binding of `bundle` under one command string —
    i.e. simulate a cell that installed under an older generation.

    >>> ALL of them, because _is_installed requires ALL bindings present. <<< The first
    draft wrote only the first binding and the list test failed — my bug, not the code's,
    and precisely the kind a partially-built fixture produces: a red that looks like a
    finding and is really a malformed input."""
    h = {}
    for b in bundle.bindings:
        h.setdefault(b.event, []).append(
            {"matcher": b.matcher, "hooks": [{"type": "command", "command": command}]})
    path.write_text(json.dumps({"hooks": h}), encoding="utf-8")


def _legacy_command(bundle, hooks_home) -> str:
    """The gen-2 form: the bare script path, no interpreter. Built here from pathlib
    rather than from the helper under test — an INDEPENDENT ORACLE, same discipline the
    #216 suite established."""
    return (Path(hooks_home) / bundle.script_name).resolve().as_posix()


def _canonical_command(bundle, hooks_home) -> str:
    """The gen-3 form, also built independently: interpreter, then the same path."""
    return f'"{_BASH}" "{_legacy_command(bundle, hooks_home)}"'


def _commands_in(settings_path: Path, event: str) -> list:
    s = json.loads(settings_path.read_text(encoding="utf-8"))
    return [a["command"]
            for e in s.get("hooks", {}).get(event, [])
            for a in e.get("hooks", [])]


def test_remove_clears_a_LEGACY_generation_binding(home, win32):
    """>>> THE REGRESSION workstation-lc HIT. <<< Remove reported success and left the
    entry in place, because uninstall consulted only the canonical string."""
    bundle = hooks.resolve_builtin("cell-resilience")
    b0 = bundle.bindings[0]
    sp = home / "settings.json"
    legacy = _legacy_command(bundle, home / "hooks")
    _settings_with(sp, bundle, legacy)

    hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                         remove_script=False, out=lambda *a, **k: None)

    assert legacy not in _commands_in(sp, b0.event), (
        "a legacy-generation binding survived `remove` — this is the silent no-op that "
        "makes reinstall produce a DUPLICATE binding and fire the hook twice"
    )


def test_reinstall_after_remove_leaves_exactly_ONE_binding(home, win32):
    """The whole point, end to end: the migration an operator actually performs.
    Legacy entry -> remove -> install -> exactly one binding, the canonical one."""
    bundle = hooks.resolve_builtin("cell-resilience")
    b0 = bundle.bindings[0]
    sp = home / "settings.json"
    _settings_with(sp, bundle, _legacy_command(bundle, home / "hooks"))

    hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                         remove_script=False, out=lambda *a, **k: None)
    hooks.install_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                       assume_yes=True, out=lambda *a, **k: None)

    cmds = _commands_in(sp, b0.event)
    assert len(cmds) == 1, f"expected ONE binding after migration, got {cmds}"
    assert cmds[0] == _canonical_command(bundle, home / "hooks"), (
        "the surviving binding must be the CANONICAL (interpreter-prefixed) one — a "
        "lone legacy survivor is the double-fire bug wearing a passing count"
    )


def test_list_reports_a_LEGACY_install_as_INSTALLED(home, win32):
    """Read side of the same ladder. Reporting a legacy install as 'available' invites
    the reinstall that creates the duplicate — so the gap on the read side is what makes
    the gap on the write side dangerous.
    >>> AND THE FIRST VERSION OF THIS TEST CALLED `_is_installed` AND
    `_installed_command_variants` DIRECTLY. <<< It passed with the list-side fix
    REVERTED — it re-implemented the read path instead of driving it, so it graded my
    own inline expression rather than the shipped verb. That is the same defect as the
    bug: a helper verified through its own front door. It drives `list_hooks` now.
    """
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"
    _settings_with(sp, bundle, _legacy_command(bundle, home / "hooks"))

    lines = []
    hooks.list_hooks(settings_path=sp, hooks_home=home / "hooks", out=lines.append)

    row = next(l for l in lines if "cell-resilience" in l)
    assert "installed" in row and "available" not in row, (
        f"a legacy-generation install must read as INSTALLED, got: {row!r}")


def test_removing_a_NOT_INSTALLED_hook_is_still_a_no_op(home, win32):
    """NON-VACUITY: unmerging across three candidate strings must not start deleting
    bindings that were never ours. An over-broad remove is worse than the under-broad one
    it replaces, because it silently disables somebody else's hook."""
    bundle = hooks.resolve_builtin("cell-resilience")
    b0 = bundle.bindings[0]
    sp = home / "settings.json"
    other = "/opt/somebody-else/their-hook.sh"
    _settings_with(sp, bundle, other)

    rc = hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                              remove_script=False, out=lambda *a, **k: None)
    assert rc == 0
    assert other in _commands_in(sp, b0.event), "an unrelated binding must survive"


def test_POSIX_still_collapses_to_ONE_candidate(home, monkeypatch):
    """The property that makes the ladder free on Linux/macOS — and the reason the
    first draft of this file could not fail. Asserted DELIBERATELY here so it is a
    checked fact rather than the silent condition under which nothing is checked."""
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"

    hooks.install_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                       assume_yes=True, out=lambda *a, **k: None)
    hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                         remove_script=False, out=lambda *a, **k: None)

    assert _commands_in(sp, bundle.bindings[0].event) == []
