"""Hook command paths must be BASH-SAFE on win32 — forward slashes, not backslashes.

REPORTED AND VERIFIED ON METAL by razorpeter (the win32 reference box,
2026-08-11, swarph-cli 0.42.5). Claude Code runs hook commands through bash,
where backslash is an ESCAPE character, so a Windows path written by
``str(WindowsPath)`` collapses:

    C:\\Users\\pierr\\.swarph\\hooks\\cell-resilience.sh
      -> bash: C:Userspierr.swarphhookscell-resilience.sh: No such file or directory

Every hook a Windows cell installs fails that way, silently, at every fire.
Rewriting the same paths with forward slashes made both the activity-marker and
cell-resilience hooks exit 0 and cell-resilience write idle_since.json.

>>> WHY THIS IS A TWO-SITE FIX AND THE INVARIANT IS THE REAL TEST. <<<
``_installed_command``'s own docstring states the contract: it uses the SAME
construction as ``install_hook`` "so unmerge/list match what install merged".
Fixing only the install side would leave uninstall searching for a backslash
string that is no longer written — it would find nothing, remove nothing, and
report success. A HALF-APPLIED FIX HERE IS WORSE THAN THE BUG, because the bug
at least fails loudly at hook-fire time.
"""
from __future__ import annotations

from pathlib import Path

from swarph_cli.commands import hooks

WINDOWS_STYLE = r"C:\Users\pierr\.swarph\hooks\cell-resilience.sh"


def test_backslashes_become_forward_slashes_on_win32(monkeypatch):
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    out = hooks._hook_script_path(WINDOWS_STYLE)
    assert out == "C:/Users/pierr/.swarph/hooks/cell-resilience.sh"


def test_no_backslash_survives_on_win32(monkeypatch):
    """The property, stated directly: bash must never see a backslash.

    Asserted separately from the exact-string test because THIS is the thing
    that breaks at runtime — an implementation that converted some separators
    and not others would pass a laxer equality check.
    """
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    assert "\\" not in hooks._hook_script_path(WINDOWS_STYLE)


def test_drive_letter_is_preserved(monkeypatch):
    """Forward-slash conversion must not damage the absolute-path anchor.

    bash on Windows resolves ``C:/Users/...`` fine; it cannot resolve a path
    whose drive letter was mangled, so a "fix" that produced ``/Users/...``
    would trade a loud failure for a wrong one.
    """
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    assert hooks._hook_script_path(WINDOWS_STYLE).startswith("C:/")


def test_posix_paths_are_untouched(monkeypatch):
    """>>> THE CONTROL. <<< Without it, an implementation that rewrote separators
    unconditionally would pass every assertion above while corrupting the POSIX
    path that carries 100% of today's traffic."""
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    posix = "/home/ubuntu/.swarph/hooks/cell-resilience.sh"
    assert hooks._hook_command_path(posix) == posix


def test_posix_backslash_is_left_alone(monkeypatch):
    """On POSIX a backslash is a LEGAL FILENAME CHARACTER, not a separator.

    Rewriting it there would corrupt a real (if unusual) path. This pins the
    conversion to the platform where it is correct, rather than to the presence
    of the character.
    """
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    odd = "/home/ubuntu/weird\\name.sh"
    assert hooks._hook_command_path(odd) == odd


def test_install_and_uninstall_construct_the_same_string(monkeypatch, tmp_path):
    """>>> THE LOAD-BEARING INVARIANT. <<<

    ``_installed_command`` is what uninstall/list match against; ``install_hook``
    writes ``_hook_command_path(script_dst)``. If those two diverge, uninstall
    silently removes nothing and reports success. Asserted on BOTH platforms,
    because a platform-gated helper is exactly the shape that can agree on one
    OS and disagree on the other.
    """
    # The registry is BUILTIN_HOOKS. Named explicitly and asserted non-empty:
    # the first version guessed `BUNDLES`, found nothing, took an early return,
    # and PASSED WHILE ASSERTING NOTHING.
    bundles = list(hooks.BUILTIN_HOOKS.values())
    assert bundles, "BUILTIN_HOOKS is empty — this test has no subject"

    # >>> AND THE SECOND VERSION WAS ALSO VACUOUS, FOR A SUBTLER REASON. <<<
    # It built the path with pathlib on a LINUX runner, where Path never yields
    # a backslash — so `str(p)` and `_hook_command_path(p)` were identical even
    # under the win32 monkeypatch, and reverting the uninstall site left the
    # suite GREEN. Mutation-checked; that is how it was caught.
    #
    # A NEGATIVE TEST WHOSE SUBJECT CANNOT EXHIBIT THE POSITIVE IS NOT A TEST.
    # The hooks_home below therefore carries real backslashes, so the two
    # constructions CAN diverge — which is the only condition under which their
    # agreement means anything.
    windows_home = r"C:\Users\pierr\.swarph\hooks"
    checked = 0
    for bundle in bundles:
        for platform in ("linux", "win32"):
            monkeypatch.setattr(hooks.sys, "platform", platform)
            via_installed = hooks._installed_command(bundle, windows_home)
            via_install = hooks._hook_command_path(
                (Path(windows_home).expanduser() / bundle.script_name).resolve()
            )
            assert via_installed == via_install, (
                f"install and uninstall disagree for {bundle.script_name} on "
                f"{platform}: {via_install!r} != {via_installed!r}"
            )
            checked += 1
    assert checked == len(bundles) * 2, "the loop did not run over every bundle"

    # The precondition, asserted rather than assumed: under win32 the subject
    # MUST actually be transformed, or the equality above is trivially true.
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    sample = hooks._installed_command(bundles[0], windows_home)
    assert "\\" not in sample, "the win32 subject was never transformed"


# --- THE MIGRATION, raised in review by Copilot on PR #216 -------------------
# The forward-slash fix OPENED A HOLE it did not close: every Windows cell that
# ran `hooks add` before it has a BACKSLASH command in settings.json. Matching
# only the new canonical form would leave those entries orphaned forever while
# uninstall reported success -- a dead binding nobody can remove and nothing
# reports. STRICTLY WORSE THAN THE BUG, which at least fails loudly at fire time.
LEGACY = r"C:\Users\pierr\.swarph\hooks\cell-resilience.sh"
CANONICAL = "C:/Users/pierr/.swarph/hooks/cell-resilience.sh"


def _settings_with(command):
    return {"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": command}]}]}}


def test_uninstall_removes_a_LEGACY_backslash_entry():
    """The migration itself: uninstall must find what the OLD code wrote."""
    settings = _settings_with(LEGACY)
    hooks._unmerge_hook(settings, "Stop", "", CANONICAL)
    assert settings["hooks"].get("Stop", []) == [], (
        "a legacy backslash entry survived uninstall — orphaned forever"
    )


def test_uninstall_still_removes_the_CANONICAL_entry():
    """>>> CONTROL. <<< Without it, an implementation that removed only legacy
    forms — or removed everything indiscriminately — would pass the test above."""
    settings = _settings_with(CANONICAL)
    hooks._unmerge_hook(settings, "Stop", "", CANONICAL)
    assert settings["hooks"].get("Stop", []) == []


def test_uninstall_does_NOT_remove_an_UNRELATED_entry():
    """The other control, and the one that matters for a variant-matching rule:
    broadening what uninstall matches must not broaden it to other people's
    hooks. A migration that eats unrelated bindings is a worse defect than the
    orphan it fixes."""
    other = "C:/Users/pierr/.swarph/hooks/some-other-hook.sh"
    settings = _settings_with(other)
    hooks._unmerge_hook(settings, "Stop", "", CANONICAL)
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == other


def test_reinstall_MIGRATES_a_legacy_entry_instead_of_duplicating_it():
    """Re-installing over a legacy entry must leave ONE binding, not two.

    Two bindings for the same script both fire, so the hook runs twice per
    event — and the operator sees a working hook, which is why nobody would
    report it.
    """
    settings = _settings_with(LEGACY)
    hooks._merge_hook(settings, "Stop", "", CANONICAL)
    actions = settings["hooks"]["Stop"][0]["hooks"]
    assert len(actions) == 1, f"expected one binding after migration, got {actions}"
    assert actions[0]["command"] == CANONICAL


def test_list_reports_a_LEGACY_install_as_INSTALLED():
    """#216 review, 2nd pass: THE THIRD MATCH SITE.

    `list`/`status` asks "is this bundle installed?" by comparing commands. With
    exact matching a legacy backslash binding reads as NOT installed, so a
    Windows cell whose hooks are present and working is told nothing is there —
    and the operator's natural next step is to install again, producing the
    duplicate binding the merge fix exists to prevent.
    """
    bundle = hooks.BUILTIN_HOOKS["cell-resilience"]
    settings = {"hooks": {}}
    for b in bundle.bindings:
        settings["hooks"].setdefault(b.event, []).append(
            {"matcher": b.matcher, "hooks": [{"type": "command", "command": LEGACY}]})
    # THE COMMAND ASKED ABOUT IS THE CANONICAL ONE — that is what the caller
    # derives from _installed_command today — while what is STORED is the
    # legacy form. Passing LEGACY here (the first version of this test) made
    # both sides identical, so exact matching succeeded and the test passed
    # under the mutation. A SUBJECT THAT CANNOT EXHIBIT THE POSITIVE IS NOT A
    # SUBJECT; the two strings must differ for the comparison to mean anything.
    assert LEGACY != CANONICAL, "precondition: the two forms must differ"
    assert hooks._is_installed(settings, CANONICAL, bundle) is True, (
        "a legacy backslash binding reported as NOT installed"
    )


def test_list_still_reports_an_ABSENT_bundle_as_absent():
    """>>> THE CONTROL. <<< Broadened matching must not make everything look
    installed — a status check that always says yes is worse than one that says
    no, because it removes the operator's reason to look."""
    bundle = hooks.BUILTIN_HOOKS["cell-resilience"]
    assert hooks._is_installed({"hooks": {}}, CANONICAL, bundle) is False
