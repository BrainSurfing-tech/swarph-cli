"""#132: ensure_monitor.sh must pass --state-dir, or it invents a second cell.

THE DEFECT (droplet, DM #8819; lab-verified): all three call sites in
scripts/ensure_monitor.sh read the CLI DEFAULT state dir. A cell with a custom
layout (droplet: /var/lib/swarph/droplet-monitor) got "not running" from
`monitor status` against the EMPTY default path, and the script then started a
SECOND monitor replaying from id 0 — against a monitor that was healthy the
entire time. The helper written to guarantee a single supervised monitor
created a duplicate on exactly the boxes that customised their layout.

lab's verification of the divergence, reproduced as this suite's fixture:
status WITH --state-dir -> exit 2; WITHOUT -> exit 1. Two different answers
about the same cell, decided by a flag the script did not pass.

THESE TESTS DRIVE THE REAL SCRIPT under bash with a fake `swarph` on PATH that
logs its argv. The fake is the membrane: what the script ASKED is the artifact,
not what a mock framework recorded. Exit codes are chosen to walk all three
call sites in one run: status -> 2 (not running, forces the start path),
start -> 0, status --brief -> 1 (DMs pending, exercises the report line).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "swarph_cli" / "scripts" / "ensure_monitor.sh"
)

FAKE_SWARPH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" start "*)  exit 0 ;;   # the auto-start succeeds
  *" --brief "*) exit 1 ;;  # final status: running, DMs pending
  *)            exit 2 ;;   # initial status: not running -> forces start path
esac
"""


def _bash():
    """A USABLE bash, or skip. On win32, shutil.which finds the System32 WSL
    launcher — a different filesystem where this script does not exist (#201's
    finding) — so the resolver that already refuses it answers instead."""
    if sys.platform == "win32":
        from swarph_cli.commands import hooks
        found = hooks._find_windows_bash()
    else:
        found = shutil.which("bash")
    if not found:
        pytest.skip("no usable bash on this box")
    return found


def _run_script(tmp_path, *, env_extra=None, argv=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / ("swarph" if sys.platform != "win32" else "swarph")
    fake.write_text(FAKE_SWARPH, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    log = tmp_path / "argv.log"
    env = dict(os.environ)
    env["SWARPH_SELF"] = "probe"
    env["FAKE_LOG"] = str(log)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.pop("SWARPH_STATE_DIR", None)          # ambient state must not leak in
    env.update(env_extra or {})
    rc = subprocess.run(
        [_bash(), str(SCRIPT), *argv], env=env,
        capture_output=True, text=True, timeout=60,
    ).returncode
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return rc, calls


def test_state_dir_env_reaches_ALL_three_call_sites(tmp_path):
    """The finding itself. One run walks status, start, and status --brief;
    every one must carry the flag — a fix that patched two of three would pass
    a weaker test and still start the duplicate."""
    custom = tmp_path / "custom-state"
    custom.mkdir()
    rc, calls = _run_script(tmp_path, env_extra={"SWARPH_STATE_DIR": str(custom)})
    assert rc == 0, "the script's one promise: NEVER fails the caller"
    assert len(calls) == 3, f"expected status+start+status--brief, got {calls}"
    for line in calls:
        assert f"--state-dir {custom}" in line, f"call site missing the flag: {line}"


def test_positional_arg_BEATS_the_env(tmp_path):
    """An operator typing a path is being more specific than the environment.
    The ordering is asserted, not assumed — env-wins would silently ignore an
    explicit correction, the same defect class as the original bug."""
    from_env = tmp_path / "from-env"
    from_arg = tmp_path / "from-arg"
    from_env.mkdir(); from_arg.mkdir()
    rc, calls = _run_script(
        tmp_path, env_extra={"SWARPH_STATE_DIR": str(from_env)}, argv=[str(from_arg)],
    )
    assert rc == 0
    assert calls and all(f"--state-dir {from_arg}" in line for line in calls), calls


def test_default_layout_passes_NO_flag(tmp_path):
    """NON-VACUITY IN THE OTHER DIRECTION: a default-layout cell (gridiron —
    safe today by luck, not design) must see byte-identical behaviour. Adding
    the flag unconditionally would not break it, but changing the invocation
    shape for cells that were never broken is how a fix grows its own blast
    radius."""
    rc, calls = _run_script(tmp_path)
    assert rc == 0
    assert len(calls) == 3, calls
    assert all("--state-dir" not in line for line in calls), calls
