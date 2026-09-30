"""card #960: the watchdog reports a dead pull-monitor writer and does not start one.

A fresh channel heartbeat is the reader. It is not the writer of inbox.log.
The oneshot unit reaps any child it starts, so supervision stays with systemd.
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from swarph_cli.scripts.wake_watchdog import (
    enforce_writers,
    systemd_owns_monitor,
    writer_verdict,
)

SRC = Path("src/swarph_cli/scripts/wake_watchdog.py")


def _cell(root: Path, name: str, *, fresh: bool) -> str:
    side = root / name / "mesh-sidecar"
    side.mkdir(parents=True)
    inbox = side / "inbox.log"
    inbox.write_text("row\n", encoding="utf-8")
    if fresh:
        (side / "channel_heartbeat.json").write_text(
            json.dumps({"ts": time.time()}), encoding="utf-8")
    return str(inbox)


def _absent(argv, **_kwargs):
    return subprocess.CompletedProcess(argv, 4)


def test_the_watchdog_does_not_start_a_monitor_or_read_a_head_file():
    text = SRC.read_text(encoding="utf-8")
    assert "monitor start" not in text
    assert "systemd-run" not in text
    assert "gateway_head" not in text


def test_fresh_heartbeat_and_dead_monitor_reports_writer_down(tmp_path):
    """Fails on 5a9a67d: the watchdog has no writer-down report at all."""
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _name: 2,
        run=_absent,
    )
    assert reported == ["fixture-cell"]


def test_live_monitor_and_fresh_heartbeat_is_healthy(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _name: 0,
        run=_absent,
    )
    assert reported == []
    assert writer_verdict(reader_alive=True, monitor_rc=0) == "healthy"


def test_system_scope_enabled_is_still_checked(tmp_path):
    """System rc 0 and --user rc 4 is lab's gridiron measurement. Neither scope starts a process."""
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if "--user" in argv:
            return subprocess.CompletedProcess(argv, 4)
        return subprocess.CompletedProcess(argv, 0)

    reported = enforce_writers(
        {"fixture-cell": inbox}, status=lambda _n: 2, run=run)
    assert reported == ["fixture-cell"]
    assert ["systemctl", "is-enabled", "swarph-monitor@fixture-cell.service"] in calls
    assert ["systemctl", "--user", "is-enabled", "swarph-monitor@fixture-cell.service"] in calls
    assert systemd_owns_monitor("fixture-cell", run=run) is True


def test_user_scope_enabled_unit_is_still_checked(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)

    def run(argv, **_kwargs):
        code = 0 if "--user" in argv else 4
        return subprocess.CompletedProcess(argv, code)

    reported = enforce_writers(
        {"fixture-cell": inbox}, status=lambda _n: 2, run=run)
    assert reported == ["fixture-cell"]
    assert systemd_owns_monitor("fixture-cell", run=run) is True


def test_both_scopes_not_found_does_not_start_a_process(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    reported = enforce_writers(
        {"fixture-cell": inbox}, status=lambda _n: 2, run=_absent)
    assert reported == ["fixture-cell"]
    assert "monitor start" not in SRC.read_text(encoding="utf-8")


def test_three_down_runs_alert_once_and_a_recovery_arms_the_next(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    state = {}

    def once(rc):
        return enforce_writers(
            {"fixture-cell": inbox},
            status=lambda _n: rc,
            run=_absent,
            state=state,
        )

    assert once(2) == ["fixture-cell"]
    assert once(2) == []
    assert once(2) == []
    assert once(0) == []
    assert state["fixture-cell"]["writer_down"] is False
    assert once(2) == ["fixture-cell"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0; the transition count is covered above",
)
def test_main_sends_one_writer_down_dm_and_does_not_start(tmp_path):
    root = tmp_path / "state"
    _cell(root, "fixture-cell", fresh=True)
    stub = tmp_path / "swarph"
    log = tmp_path / "calls.log"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        case "$1 $2" in
          "monitor status") exit 2 ;;
          *) exit 0 ;;
        esac
    """), encoding="utf-8")
    stub.chmod(0o755)
    state_path = tmp_path / "wake.json"
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(stub),
        "STUB_LOG": str(log),
    }
    env.pop("SWARPH_SELF", None)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "lab-ovh"]
    for _ in range(3):
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        assert proc.returncode == 0
    text = log.read_text(encoding="utf-8")
    assert text.count("mesh send lab-ovh") == 1
    assert "monitor start" not in text
    assert "writer-down" in text
    assert "gridiron" not in text
