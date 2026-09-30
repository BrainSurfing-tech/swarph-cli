"""card #960: the watchdog must notice a dead pull-monitor writer.

A fresh channel heartbeat is the reader. It is not the writer of inbox.log.
On 5a9a67d a fresh heartbeat plus monitor status 2 reports nothing.
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


def _cell(root: Path, name: str, *, fresh: bool) -> str:
    side = root / name / "mesh-sidecar"
    side.mkdir(parents=True)
    inbox = side / "inbox.log"
    inbox.write_text("row\n", encoding="utf-8")
    if fresh:
        (side / "channel_heartbeat.json").write_text(
            json.dumps({"ts": time.time()}), encoding="utf-8")
    return str(inbox)


def test_fresh_heartbeat_and_dead_monitor_reports_writer_down(tmp_path):
    """Fails on 5a9a67d: the watchdog has no writer-down report at all."""
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    started = []
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _name: 2,
        start=started.append,
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1),
    )
    assert reported == ["fixture-cell"]
    assert started == ["fixture-cell"]


def test_live_monitor_and_fresh_heartbeat_is_healthy(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    started = []
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _name: 0,
        start=started.append,
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1),
    )
    assert reported == []
    assert started == []
    assert writer_verdict(reader_alive=True, monitor_rc=0) == "healthy"


def _writers(inbox, run):
    started = []
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _n: 2,
        start=started.append,
        run=run,
    )
    return reported, started


def test_system_scope_enabled_defers_when_user_scope_is_not_found(tmp_path):
    """System rc 0 and --user rc 4 is lab's gridiron measurement.

    On b56d1f6 only --user is checked, rc 4 is not owned, and the watchdog hand-starts.
    """
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if "--user" in argv:
            return subprocess.CompletedProcess(argv, 4)
        return subprocess.CompletedProcess(argv, 0)

    reported, started = _writers(inbox, run)
    assert reported == ["fixture-cell"]
    assert started == []
    assert ["systemctl", "is-enabled", "swarph-monitor@fixture-cell.service"] in calls
    assert ["systemctl", "--user", "is-enabled", "swarph-monitor@fixture-cell.service"] in calls
    assert systemd_owns_monitor("fixture-cell", run=run) is True


def test_user_scope_enabled_unit_defers(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)

    def run(argv, **_kwargs):
        code = 0 if "--user" in argv else 4
        return subprocess.CompletedProcess(argv, code)

    reported, started = _writers(inbox, run)
    assert reported == ["fixture-cell"]
    assert started == []


def test_both_scopes_not_found_hand_starts(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)

    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 4)

    reported, started = _writers(inbox, run)
    assert reported == ["fixture-cell"]
    assert started == ["fixture-cell"]


def test_stale_inbox_with_a_newer_gateway_head_is_writer_down(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    side = Path(inbox).parent
    (side / "cursor.json").write_text(json.dumps({"last_msg_id": 10}), encoding="utf-8")
    (side / "gateway_head.json").write_text(json.dumps({"id": 11}), encoding="utf-8")
    state = {"fixture-cell": {"inbox_bytes": os.path.getsize(inbox)}}
    started = []
    reported = enforce_writers(
        {"fixture-cell": inbox},
        status=lambda _n: 0,
        start=started.append,
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1),
        state=state,
    )
    assert reported == ["fixture-cell"]
    assert writer_verdict(reader_alive=True, monitor_rc=0,
                          inbox_advanced=False, gateway_newer=True) == "writer-down"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0; the report is covered by enforce_writers",
)
def test_main_prints_writer_down_without_touching_a_live_cell(tmp_path):
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
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(tmp_path / "wake.json"),
        "SWARPH_BIN": str(stub),
        "STUB_LOG": str(log),
    }
    env.pop("SWARPH_SELF", None)
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "lab-ovh"],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "writer-down fixture-cell" in proc.stdout
    assert "monitor start --as fixture-cell --deliver pull" in log.read_text(encoding="utf-8")
    assert "gridiron" not in proc.stdout
