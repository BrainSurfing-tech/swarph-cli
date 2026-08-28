"""#650: the guide's monitor self-check must count ONE from a NON-INTERACTIVE shell.

The pre-fix guide documented `pgrep -af "swarph monitor.*--as <you>"` as the first
self-check and called two lines a fault -- but run from any `bash -c`, script, or
agent tool, the shell carrying the command matches the pattern itself, so ONE
healthy monitor counted TWO, and the stated remedy ("stop the hand-started one")
pointed the reader at their own shell. Measured live on lab-ovh 2026-08-27; this
file's leg (b) reproduces it in rig.

The accept check (#532 shape) requires BOTH legs non-interactively:
  (a) the check the guide CURRENTLY documents -- extracted from GUIDE.md, not
      retyped -- run via `bash -c` against exactly one monitor, counts ONE;
  (b) the SUPERSEDED form in the same rig counts TWO, so leg (a)'s one is earned
      by the command, not produced by a blind rig.

A human typing the command interactively is the one context where the bug hides,
so that context proves nothing and is not used.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "guide" / "GUIDE.md"
_PEER = "t650fake"


def _fake_monitor():
    """One process whose cmdline is a monitor's, with no monitor behaviour.

    `exec -a` replaces argv[0], so after the exec there is exactly ONE process
    carrying the pattern -- the wrapper shell is gone, not lingering.
    """
    return subprocess.Popen(
        ["bash", "-c",
         f'exec -a "swarph monitor start --as {_PEER} --deliver pull" sleep 30'],
    )


def _await_fake_up() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        out = subprocess.run(["pgrep", "-f", f"swarph monitor.*--as {_PEER}"],
                             capture_output=True, text=True)
        if out.stdout.strip():
            return
        time.sleep(0.1)
    raise AssertionError("fake monitor never appeared")


def _count_lines(cmd: str) -> list[str]:
    """Run the documented check EXACTLY as a cell runs it: inside a shell whose
    own argv carries the command text.

    The trailing `exit` is load-bearing: bash EXECS a sole simple `bash -c`
    command, erasing the wrapper before pgrep runs -- the one context where the
    defect hides. Any compound invocation (an agent harness's setup/teardown
    wrapper, a logging script, `cmd; rc=$?`) keeps the shell alive alongside
    pgrep, and the shell's cmdline matches the pattern. That compound context
    is the context every cell actually runs in, so it is the context tested.
    """
    out = subprocess.run(["bash", "-c", f"{cmd}\nrc=$?\nexit $rc"],
                         capture_output=True, text=True, timeout=30)
    return [line for line in out.stdout.splitlines() if line.strip()]


def _documented_pgrep() -> str:
    """The pgrep the guide currently teaches for the monitor check, extracted --
    a retyped copy in this test would let the guide drift behind its own guard."""
    guide = GUIDE.read_text(encoding="utf-8")
    m = re.search(r'`(pgrep -af "[^`]+)`', guide)
    assert m, "the guide documents no pgrep monitor check"
    return m.group(1)


def test_documented_check_counts_one_from_a_noninteractive_shell() -> None:
    """Leg (a). On the pre-fix guide this counted TWO -- the documented pattern
    matched the shell carrying it."""
    cmd = _documented_pgrep().replace("<you>", _PEER)
    mon = _fake_monitor()
    try:
        _await_fake_up()
        lines = _count_lines(cmd)
        assert len(lines) == 1, (
            f"the guide's documented check counted {len(lines)} for ONE monitor: {lines}"
        )
    finally:
        mon.terminate()


def test_the_superseded_form_counts_two_in_the_same_rig() -> None:
    """Leg (b), can-fail: the pre-fix form, kept here as the KNOWN-BAD control,
    counts two in this rig -- the rig can see the fault, so leg (a)'s one means
    the documented command is right."""
    mon = _fake_monitor()
    try:
        _await_fake_up()
        lines = _count_lines(f'pgrep -af "swarph monitor.*--as {_PEER}"')
        assert len(lines) == 2, (
            f"control rig went blind: superseded form counted {len(lines)}: {lines}"
        )
    finally:
        mon.terminate()


def test_the_guides_primary_check_is_the_property_form() -> None:
    """The table's monitor row is `swarph monitor status` (pidfile + cgroup +
    unit, the #644 properties), and any pgrep the guide still teaches must be
    self-match-excluding BY CONSTRUCTION -- the bracket form, whose own literal
    text does not match its pattern. A caller-excluding filter appended to a
    self-matching pattern (`grep -v "bash -c"`) swaps one pattern for two and
    is a patch, not a fix."""
    guide = GUIDE.read_text(encoding="utf-8")
    assert "swarph monitor status --as <you>" in guide, (
        "the guide's monitor row must be the property check, not a raw pgrep "
        "the reader has to interpret"
    )
    for m in re.finditer(r'`(pgrep[^`]*)`', guide):
        assert "[s]warph" in m.group(1), (
            f"guide teaches a self-matching pgrep: {m.group(1)}"
        )
