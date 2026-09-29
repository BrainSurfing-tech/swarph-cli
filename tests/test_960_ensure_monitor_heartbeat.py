"""#960: a fresh channel heartbeat is a reader. Do not start a monitor beside it.

channel-serve writes channel_heartbeat.json beside the inbox and puts nothing
on its command line, so monitor status reports "not running" while the channel
is alive. On main, that status is enough to start a second reader. The head
starts only when the file is absent or older than 180s.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "swarph_cli" / "scripts" / "ensure_monitor.sh"
)

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
exit 2
"""


def _bash():
    if sys.platform == "win32":
        pytest.skip("windows-latest bash is the WSL stub")
    found = shutil.which("bash")
    if not found:
        pytest.skip("no bash")
    return found


def _run(tmp_path: Path, *, age: float | None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "swarph"
    shim.write_text(FAKE, encoding="utf-8")
    shim.chmod(0o755)
    log = tmp_path / "calls.log"
    state = tmp_path / "state"
    side = state / "mesh-sidecar"
    side.mkdir(parents=True)
    if age is not None:
        (side / "channel_heartbeat.json").write_text(
            json.dumps({"ts": time.time() - age}), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["SWARPH_SELF"] = "cursor-lin"
    env["SWARPH_STATE_DIR"] = str(state)
    env["FAKE_LOG"] = str(log)
    proc = subprocess.run(
        [_bash(), str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, calls


def test_fresh_heartbeat_does_not_start(tmp_path):
    proc, calls = _run(tmp_path, age=10)
    assert proc.returncode == 0
    assert "monitor start" not in calls
    assert "under 180s" in proc.stdout


def test_stale_heartbeat_still_starts(tmp_path):
    proc, calls = _run(tmp_path, age=200)
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_absent_heartbeat_still_starts(tmp_path):
    proc, calls = _run(tmp_path, age=None)
    assert proc.returncode == 0
    assert "monitor start" in calls
