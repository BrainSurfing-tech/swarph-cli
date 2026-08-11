"""#360: ensure_monitor.sh must not start a monitor under a GUESSED identity.

Line 18 shipped `SELF="${SWARPH_SELF:-lab-ovh}"` in every install. That takes the
state UNKNOWN and renders it as a DETERMINATE, SPECIFIC, WRONG PEER: a cell that
never set the variable started a monitor AS lab-ovh, draining lab's DMs and
marking them read. A wrong identity behaves identically to a right one until an
audit, which is why it survived — it fails in the reassuring direction.

Found on a peer box by gridiron, confirmed by science-claude (msgs 22344/22346).
It is a FOURTH default; card #360 catalogued three.

>>> THE TWO CONSTRAINTS LOOK OPPOSED AND ARE NOT. <<< The script promises in its
own header that it NEVER FAILS THE CALLER — "a hook that can block a session is
worse than the deafness it prevents". So `${SWARPH_SELF:?...}`, the obvious fix
and the one proposed in review, is WRONG HERE: it exits non-zero and hands a
SessionStart hook the power to wedge a session. REFUSE THE ACTION, NOT THE
CALLER: say so loudly, do nothing, exit 0.

THESE TESTS STUB `swarph` ON PATH. An earlier hand-check ran the real script with
the variable set and ACTUALLY STARTED A MONITOR on the live box — a "control"
that exercises the real side-effecting path is a live action, not a test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# >>> WINDOWS CI HAS NO USABLE `bash`. <<< On the GitHub windows-latest runner
# `bash` resolves to the WSL STUB, which prints (in UTF-16)
#   "Windows Subsystem for Linux has no installed distributions..."
# and never runs the script. The tests below then compare that banner against
# the refusal text and fail — measured on run 31492002559, both py3.11 and 3.12.
#
# SKIPPED, NOT DELETED, AND NOT SILENTLY: ensure_monitor.sh is a POSIX shell
# hook. A Windows cell that has git-bash runs it fine; the RUNNER is what lacks
# an interpreter. Skipping states "this environment cannot evaluate the claim",
# which is the honest verdict — deleting the tests would remove the claim, and
# asserting on the stub's banner would be a test of the stub.
_BASH = shutil.which("bash")
_NEEDS_POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32" or _BASH is None,
    reason="no POSIX shell to run ensure_monitor.sh (windows-latest's `bash` is "
           "the WSL stub); the script itself is unchanged and is exercised on POSIX",
)

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "scripts" / "ensure_monitor.sh"


def _fake_swarph(tmp_path: Path) -> Path:
    """A `swarph` that records its argv instead of doing anything."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    shim = bindir / "swarph"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        "exit 2\n",  # 2 = 'not running', drives the start branch
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return log


def _run(tmp_path: Path, *, self_name: str | None):
    log = _fake_swarph(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.pop("SWARPH_SELF", None)
    if self_name is not None:
        env["SWARPH_SELF"] = self_name
    proc = subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, calls


@_NEEDS_POSIX_SHELL
def test_unset_identity_refuses_and_says_so(tmp_path):
    proc, _ = _run(tmp_path, self_name=None)
    assert "REFUSING" in proc.stdout
    assert "SWARPH_SELF" in proc.stdout


@_NEEDS_POSIX_SHELL
def test_unset_identity_STARTS_NOTHING(tmp_path):
    """>>> THE LOAD-BEARING ASSERTION. <<< Printing a warning and starting the
    monitor anyway would pass the test above while leaving the defect intact.
    The refusal must prevent the ACTION, so `swarph` is never invoked at all."""
    _, calls = _run(tmp_path, self_name=None)
    assert calls == "", f"swarph was invoked despite an unknown identity: {calls!r}"


@_NEEDS_POSIX_SHELL
def test_the_caller_is_never_failed(tmp_path):
    """The script's own contract, which the obvious fix would have broken.

    `${SWARPH_SELF:?}` exits non-zero; from a SessionStart hook that can wedge a
    session, which is worse than the deafness this script exists to prevent.
    """
    proc, _ = _run(tmp_path, self_name=None)
    assert proc.returncode == 0, f"refusal must still exit 0, got {proc.returncode}"


@_NEEDS_POSIX_SHELL
def test_a_KNOWN_identity_still_starts_the_monitor(tmp_path):
    """>>> THE CONTROL. <<< Without it, a script that refused unconditionally —
    or one that had simply stopped working — passes every assertion above."""
    proc, calls = _run(tmp_path, self_name="probe-cell-259")
    assert proc.returncode == 0
    assert calls, "swarph was never invoked for a KNOWN identity"
    assert "probe-cell-259" in calls, f"the cell's own name was not passed: {calls!r}"


def test_no_peer_name_is_hardcoded_as_a_fallback(tmp_path):
    """The regression that reintroduces the defect is a NEW default, not a
    changed one. Asserted against the source so a future `${SWARPH_SELF:-...}`
    of any value fails here rather than in production six weeks later."""
    # COMMENT LINES ARE STRIPPED FIRST. The fix's own comment QUOTES the old
    # expression to explain it, so a whole-file text match finds the defect in
    # the prose that documents its removal — a matcher that cannot tell code
    # from commentary reports a defect that is not there. (Caught by running it:
    # this assertion failed on the very commit that fixes the bug.)
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "${SWARPH_SELF:-lab-ovh}" not in code, "a peer default is back in the CODE"
    assert 'SELF="${SWARPH_SELF:-}"' in code, "the empty-default form is required"
