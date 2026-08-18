"""A guard that rejects a path must not re-select it by name. drop's #250 finding.

`_windows_hook_bash` refused `shutil.which("bash")` when it resolved into System32 —
correctly, since System32\\bash.exe is the WSL launcher on a different filesystem where
the hook script does not exist — and then fell through to `return "bash"`, which Windows
resolves to EXACTLY THAT LAUNCHER. The guard and its fallback disagreed, and the fallback
won.

>>> THE DOCSTRING ASSERTED THE OPPOSITE OF WHAT THE CODE DID. <<< It claimed the bare
name "fails LOUDLY at fire time rather than launching an IDE". It does not fail loudly:
WSL starts and reports a missing file from inside a filesystem the operator was never
thinking about. Same shape as #467b — I reasoned about the branch I wrote and stated a
conclusion covering the branch I did not.

WHAT THIS SUITE PINS IS THE ASYMMETRY, NOT JUST THE REFUSAL. Refusing everywhere would
be the easier fix and the worse one: uninstall and list build the same command string to
FIND existing entries, so a box whose Git install was removed after installing hooks
could no longer remove them. The fix would strand the operator harder than the bug did.
Fail closed on the verb that CREATES a broken binding; fail open on the ones that clean
it up. Both directions are tested, because a one-way test proves only its own branch.
"""
from __future__ import annotations

import json

import pytest

from swarph_cli.commands import hooks


@pytest.fixture
def home(tmp_path):
    (tmp_path / "hooks").mkdir()
    return tmp_path


@pytest.fixture
def no_git_bash(monkeypatch):
    """Windows with no usable bash — stated at the RESOLVER, not by blanking the disk.

    >>> THE FIRST DRAFT PATCHED `Path.exists` TO RETURN FALSE FOR EVERYTHING. <<< That
    is global, so it also answered this suite's own `assert not settings.exists()` —
    the refusal tests would have passed against code that wrote the file. A fixture
    that satisfies the assertion it is supposed to challenge is the vacuous-green
    pattern twice in one morning.
    """
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks, "_find_windows_bash", lambda: None)


@pytest.fixture
def win32_without_bash(monkeypatch):
    """The RESOLVER's own view: nothing on disk, and PATH offering only the WSL
    launcher. Narrow on purpose — only the resolver test uses it."""
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks.Path, "exists", lambda self: False)
    monkeypatch.setattr(hooks.shutil, "which",
                        lambda _n: r"C:\WINDOWS\system32\bash.exe")


def test_the_System32_launcher_is_never_selected(win32_without_bash):
    """>>> THE FINDING ITSELF. <<< Not "install refuses" — that is downstream. The
    defect is that the resolver's own guard was undone by its own fallback, so the
    check is that no answer at all beats the wrong answer."""
    assert hooks._find_windows_bash() is None


def test_install_REFUSES_and_writes_nothing(home, no_git_bash):
    """A binding written here fires on every matching tool call and fails from inside
    WSL. Refusing is the more useful output — and it must be a real abort, not a
    warning followed by the write."""
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"
    lines = []

    rc = hooks.install_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                            assume_yes=True, out=lines.append)

    assert rc != 0
    assert not sp.exists(), "settings.json was written despite the refusal"
    assert not (home / "hooks" / bundle.script_name).exists(), "script was written"


def test_the_refusal_names_the_remedy(home, no_git_bash):
    """A refusal that does not say what to do is a dead end wearing a safety check.
    It must name the missing thing, not merely decline."""
    bundle = hooks.resolve_builtin("cell-resilience")
    lines = []
    hooks.install_hook(bundle, settings_path=home / "settings.json",
                       hooks_home=home / "hooks", assume_yes=True, out=lines.append)
    text = "\n".join(lines)
    assert "Git for Windows" in text
    assert "nothing was written" in text


def test_UNINSTALL_does_not_INHERIT_the_refusal(home, monkeypatch):
    """>>> THE TRAP THE OBVIOUS FIX FALLS INTO. <<< Refusing in the resolver would be
    the smaller diff and the worse fix: uninstall and list build the same command
    string to FIND existing entries, so a box whose Git install disappeared after
    installing hooks could no longer remove them — stranding the operator harder than
    the bug did. The refusal belongs at the verb that CREATES a broken binding.

    What this pins is that removal still RUNS and still reports success. Whether it
    actually removes the binding is a SEPARATE, MEASURED defect — see the test below.
    """
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks, "_find_windows_bash", lambda: None)
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"
    sp.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    rc = hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                              remove_script=False, out=lambda *a, **k: None)
    assert rc == 0


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT, board card filed. The command string embeds the ABSOLUTE "
    "INTERPRETER PATH, so a binding installed under one bash is unmatchable after Git "
    "moves, upgrades, or is removed. Reproduced identically on main, on #251, and here "
    "— #251's ladder enumerates GENERATIONS, and this is a different dimension. The "
    "root-cause fix is to key removal on the SCRIPT PATH (unique to us, stable across "
    "every interpreter), which would retire the generation ladder entirely; that is a "
    "separate change from drop's refusal finding and must not ride in on it."))
def test_interpreter_DRIFT_makes_a_binding_unremovable(home, monkeypatch):
    """Install while Git is present, uninstall after it is gone. Marked xfail(strict)
    rather than deleted so the day someone fixes the match key, this FAILS as unexpected
    pass and tells them — a finding parked as a comment decays; parked as a strict xfail
    it announces its own repair."""
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks, "_find_windows_bash",
                        lambda: "C:/Program Files/Git/bin/bash.exe")
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"
    hooks.install_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                       assume_yes=True, out=lambda *a, **k: None)

    monkeypatch.setattr(hooks, "_find_windows_bash", lambda: None)
    hooks.uninstall_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                         remove_script=False, out=lambda *a, **k: None)

    left = [a["command"]
            for e in json.loads(sp.read_text()).get("hooks", {}).get(
                bundle.bindings[0].event, [])
            for a in e.get("hooks", [])]
    assert left == [], f"binding unremovable after interpreter drift: {left}"


def test_POSIX_never_reaches_the_refusal(home, monkeypatch):
    """NON-VACUITY IN THE OTHER DIRECTION: the check is win32-only, and POSIX has no
    bash-resolution step at all. A refusal leaking onto Linux would break every cell
    carrying today's traffic."""
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    # NOT patching Path.exists here: it is global, so the first draft's blanket
    # `lambda self: False` also answered the assertion at the bottom of this test and
    # reported a failure that was purely the fixture lying to itself. POSIX never
    # reaches bash resolution anyway, so the patch was decoration with a side effect.
    monkeypatch.setattr(hooks.shutil, "which", lambda _n: None)
    bundle = hooks.resolve_builtin("cell-resilience")
    sp = home / "settings.json"

    rc = hooks.install_hook(bundle, settings_path=sp, hooks_home=home / "hooks",
                            assume_yes=True, out=lambda *a, **k: None)

    assert rc == 0 and sp.exists()
