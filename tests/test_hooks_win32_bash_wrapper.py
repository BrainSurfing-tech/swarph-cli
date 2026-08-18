"""win32 hook commands must name an INTERPRETER, not just a path.

REPORTED AND MEASURED ON METAL by cursor-win, 2026-08-18, on the commander's
Windows laptop.

>>> A PATH-ONLY HOOK COMMAND DOES NOT ALWAYS REACH BASH ON WINDOWS. <<< Claude Code
does not invariably exec through a shell there; a bare path goes to ShellExecute,
which resolves the `.sh` FILE ASSOCIATION. On that box the association was the IDE:

    Antigravity.exe "C:\\Users\\...\\.swarph\\hooks\\activity-marker.sh"

`activity-marker` binds PreToolUse with matcher "" — EVERY TOOL CALL — so every tool
call launched the IDE. The file association was the symptom that got noticed; the
installer writing a bare path is the defect, and it had been shipping since the
forward-slash fix looked like it had finished the job.

TWO WAYS TO GET THE INTERPRETER WRONG, BOTH MEASURED THERE:
  `bash` on PATH  -> C:\\WINDOWS\\system32\\bash.exe, the WSL launcher. Different
                     filesystem; the script does not exist in it. Fails in a way that
                     reads like a missing file rather than a wrong shell.
  `git-bash.exe`  -> spawns a mintty WINDOW instead of running inline.
Git's `bin/bash.exe` is the one that behaves.

>>> AND THE FIX RE-OPENS #216's MIGRATION HOLE, ONE GENERATION LATER. <<< Every
Windows cell that installed between the forward-slash fix and this one has a bare
forward-slash command in settings.json. Matching only the newest form orphans them
exactly as matching only forward-slash would have orphaned the backslash ones — a
dead binding nobody can remove, while uninstall reports success.
"""
from __future__ import annotations

import pathlib
from pathlib import Path
from unittest import mock

import pytest

from swarph_cli.commands import hooks


@pytest.fixture
def win32():
    """Pretend to be win32 with Git-for-Windows installed."""
    with mock.patch.object(hooks.sys, "platform", "win32"), \
         mock.patch.object(hooks.Path, "exists",
                           lambda self: "Program Files/Git" in str(self)):
        yield


def test_win32_command_names_an_interpreter(win32):
    """>>> THE REGRESSION. <<< Without an explicit interpreter, ShellExecute picks
    the .sh file association — on the reference box, the IDE, once per tool call."""
    cmd = hooks._hook_command_path(
        pathlib.PureWindowsPath(r"C:\Users\p\.swarph\hooks\activity-marker.sh"))
    assert cmd == ('"C:/Program Files/Git/bin/bash.exe" '
                   '"C:/Users/p/.swarph/hooks/activity-marker.sh"')


def test_win32_never_selects_the_WSL_launcher():
    """`bash` on PATH is System32's WSL launcher. Selecting it moves the failure from
    'wrong program' to 'right program, wrong filesystem' — which is HARDER to
    diagnose, not easier, because the error is a plausible missing-file."""
    with mock.patch.object(hooks.sys, "platform", "win32"), \
         mock.patch.object(hooks.Path, "exists", lambda self: False), \
         mock.patch.object(hooks.shutil, "which",
                           lambda n: r"C:\WINDOWS\system32\bash.exe"):
        assert hooks._windows_hook_bash() == "bash"


def test_win32_accepts_a_NON_system32_bash_from_PATH():
    """NON-VACUITY for the guard above: it must reject System32 specifically, not
    every PATH result, or a correctly-installed non-Git bash would be discarded."""
    with mock.patch.object(hooks.sys, "platform", "win32"), \
         mock.patch.object(hooks.Path, "exists", lambda self: False), \
         mock.patch.object(hooks.shutil, "which", lambda n: r"C:\msys64\usr\bin\bash.exe"):
        assert hooks._windows_hook_bash() == "C:/msys64/usr/bin/bash.exe"


def test_POSIX_is_completely_unchanged():
    """>>> THE BLAST-RADIUS GUARD. <<< Every cell in this mesh except one is POSIX.
    A win32 fix that alters the Linux command string would rewrite settings.json on
    the whole fleet and orphan every existing binding at once."""
    p = Path("/home/ubuntu/.swarph/hooks/x.sh")
    assert hooks._hook_command_path(p) == "/home/ubuntu/.swarph/hooks/x.sh"


def test_uninstall_matches_ALL_THREE_generations(win32):
    """#216's hole, re-opened by this fix and closed the same way. install/uninstall
    must agree not only GOING FORWARD but with whatever is already on disk."""
    bundle = hooks.resolve_builtin("activity-marker")
    variants = hooks._installed_command_variants(bundle, r"C:\Users\p\.swarph\hooks")

    # Asserted STRUCTURALLY, not by literal prefix: this suite runs on Linux, where
    # Path.resolve() prepends the cwd to a Windows-shaped path, so no variant can
    # literally start "C:/". The property under test is that all THREE generations
    # are offered, which is what keeps an older install findable by uninstall.
    assert len(variants) == 3, variants
    assert variants[0].startswith('"'), "gen 3 (bash-wrapped) must come FIRST"
    bare = [v for v in variants if not v.startswith('"')]
    assert any("\\" not in v for v in bare), "gen 2: a bare forward-slash-only form"
    assert any("\\" in v for v in bare), "gen 1: a form still carrying backslashes"


def test_POSIX_still_gets_exactly_one_variant():
    """The dedup must collapse identical forms, or POSIX uninstall would scan three
    copies of one string and any future 'did it match more than once' logic would
    read a duplicate as a conflict."""
    bundle = hooks.resolve_builtin("activity-marker")
    assert len(hooks._installed_command_variants(bundle, "/home/ubuntu/.swarph/hooks")) == 1


def test_the_NEW_gh_router_bundle_inherits_the_fix(win32):
    """>>> COMPOSITION, AND THE REASON THIS TEST EXISTS. <<< The #397 router bundle
    was added HOURS BEFORE this bug was reported, and it binds PreToolUse/Bash — so
    on Windows it would have launched the IDE on every Bash call, exactly like
    activity-marker. It inherits the fix only because it goes through the shared
    helper. If someone later hand-rolls a command string for a new bundle, this
    fails."""
    bundle = hooks.resolve_builtin("gh-identity-router")
    cmd = hooks._installed_command(bundle, r"C:\Users\p\.swarph\hooks")
    assert cmd.startswith('"C:/Program Files/Git/bin/bash.exe" "')
    assert cmd.endswith('gh-identity-router.sh"')
