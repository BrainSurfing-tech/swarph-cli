"""#960: a fresh channel heartbeat is the reader, not the writer.

channel-serve writes channel_heartbeat.json and only reads inbox.log. The
pull monitor is the only writer. A fresh heartbeat must not stop that
monitor from starting. A live monitor (status 0) still starts nothing.
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


def test_fresh_heartbeat_with_no_monitor_starts(tmp_path):
    """The shim exits 2, so no monitor is running. A 10s-old heartbeat
    still has to start the pull monitor. On main this asserts the opposite.
    """
    proc, calls = _run(tmp_path, age=10)
    assert proc.returncode == 0
    assert "monitor start" in calls
    assert "under 180s" not in proc.stdout


def test_stale_heartbeat_still_starts(tmp_path):
    proc, calls = _run(tmp_path, age=200)
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_absent_heartbeat_still_starts(tmp_path):
    proc, calls = _run(tmp_path, age=None)
    assert proc.returncode == 0
    assert "monitor start" in calls


def _run_payload(tmp_path: Path, payload: dict):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "swarph"
    shim.write_text(FAKE, encoding="utf-8")
    shim.chmod(0o755)
    log = tmp_path / "calls.log"
    side = tmp_path / "state" / "mesh-sidecar"
    side.mkdir(parents=True)
    (side / "channel_heartbeat.json").write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["SWARPH_SELF"] = "cursor-lin"
    env["SWARPH_STATE_DIR"] = str(tmp_path / "state")
    env["FAKE_LOG"] = str(log)
    proc = subprocess.run(
        [_bash(), str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, calls


def test_a_future_timestamp_an_hour_ahead_starts(tmp_path):
    proc, calls = _run_payload(tmp_path, {"ts": time.time() + 3600})
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_a_timestamp_within_skew_still_starts_when_no_monitor(tmp_path):
    proc, calls = _run_payload(tmp_path, {"ts": time.time() + 10})
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_a_fresh_heartbeat_with_a_dead_pid_starts(tmp_path):
    # Above any host pid_max. macOS has no /proc, so the dead pid cannot come from there.
    proc, calls = _run_payload(tmp_path, {"ts": time.time(), "pid": 2**31 - 1})
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_a_fresh_heartbeat_with_a_live_reader_pid_still_starts(tmp_path):
    """A live channel-serve pid is the reader. It does not stand in for the monitor."""
    child = None
    if os.path.isdir("/proc/self"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "channel-serve"])
        pid = child.pid
    else:
        pid = os.getpid()
    try:
        proc, calls = _run_payload(tmp_path, {"ts": time.time(), "pid": pid})
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_fresh_heartbeat_and_a_live_monitor_starts_nothing(tmp_path):
    """Status 0 is the writer. A fresh heartbeat beside it must not start a second one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "swarph"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
        "case \"$*\" in\n"
        "  *'monitor status'*) exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    log = tmp_path / "calls.log"
    side = tmp_path / "state" / "mesh-sidecar"
    side.mkdir(parents=True)
    (side / "channel_heartbeat.json").write_text(
        json.dumps({"ts": time.time()}), encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["SWARPH_SELF"] = "cursor-lin"
    env["SWARPH_STATE_DIR"] = str(tmp_path / "state")
    env["FAKE_LOG"] = str(log)
    proc = subprocess.run(
        [_bash(), str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    assert proc.returncode == 0
    assert "monitor start" not in calls
    assert "monitor status" in calls


def test_pid_zero_starts(tmp_path):
    proc, calls = _run_payload(tmp_path, {"ts": time.time(), "pid": 0})
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_pid_minus_one_starts(tmp_path):
    proc, calls = _run_payload(tmp_path, {"ts": time.time(), "pid": -1})
    assert proc.returncode == 0
    assert "monitor start" in calls


def test_an_unrelated_live_pid_starts_when_its_command_line_is_readable(tmp_path):
    if not os.path.isdir("/proc/self"):
        pytest.skip("command-line check is not available")
    parent = os.getppid()
    text = open(f"/proc/{parent}/cmdline", "rb").read().replace(b"\x00", b" ").decode()
    assert "channel-serve" not in text
    proc, calls = _run_payload(tmp_path, {"ts": time.time(), "pid": parent})
    assert proc.returncode == 0
    assert "monitor start" in calls
