"""Card #1256: a dead --as cell alerts the commander, again after 6 h,
and a live heartbeat pid with the wrong parent reads as dead.

The three cases fail on the parent commit by assertion.
"""
import datetime
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from swarph_cli.scripts.wake_watchdog import _channel_alive, scan


def _stub(tmp_path: Path) -> Path:
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        exit "$STUB_RC"
    """))
    stub.chmod(0o755)
    return stub


def _lab_yaml(config: Path) -> None:
    directory = config / "swarph" / "cells"
    directory.mkdir(parents=True)
    (directory / "lab.yaml").write_text(
        "schema_version: v1\n"
        "name: lab-ovh\n"
        "role: lab\n"
        "cwd: /tmp\n"
        "provider: claude\n",
        encoding="utf-8",
    )


def _cell(root: Path, name: str) -> Path:
    inbox = root / name / "mesh-sidecar" / "inbox.log"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    return inbox


def _env(tmp_path: Path, root: Path, state_path: Path, log: Path) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(_stub(tmp_path)),
        "STUB_LOG": str(log),
        "STUB_RC": "0",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    env.pop("SWARPH_SELF", None)
    env.pop("WAKE_WATCHDOG_NOW", None)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, check=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_ten_minute_stale_heartbeat_for_the_sender_mints_one_commander_row(tmp_path):
    """A heartbeat dated 10 min ago for the --as cell: exactly one
    commander-held row and no self DM. A second pass sends nothing."""
    root = tmp_path / "state"
    inbox = _cell(root, "lab-ovh")
    ts = 1_700_000_000.0
    (inbox.parent / "channel_heartbeat.json").write_text(
        json.dumps({"pid": 4242, "ts": ts}), encoding="utf-8")
    _lab_yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    log = tmp_path / "stub.log"
    env = _env(tmp_path, root, state_path, log)
    _run(env)
    saved = json.loads(state_path.read_text())
    saved["lab-ovh"]["missing_since"] = 0
    state_path.write_text(json.dumps(saved))
    log.write_text("")
    _run(env)
    text = log.read_text()
    since = datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    assert "mesh send lab-ovh" not in text
    assert text.count("--holder commander") == 1
    assert f"lab-ovh DM wake dead since {since}" in text
    assert "fix: /mcp reconnect in the lab pane, or swarph spawn lab" in text
    assert "board cards ask 729" in text
    assert json.loads(state_path.read_text())["lab-ovh"]["outage"] is True
    log.write_text("")
    _run(env)
    assert "--holder commander" not in log.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_clock_moved_six_hours_sends_exactly_one_more_alert(tmp_path):
    """The same dead sender, with the clock 6 h past the last alert,
    sends exactly one more commander row and not a second one."""
    root = tmp_path / "state"
    inbox = _cell(root, "lab-ovh")
    (inbox.parent / "channel_heartbeat.json").write_text(
        json.dumps({"pid": 4242, "ts": 1_700_000_000.0}), encoding="utf-8")
    _lab_yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    alerted_at = 1_000.0
    state_path.write_text(json.dumps({
        "lab-ovh": {"missing_since": 0, "outage": True, "alerted_at": alerted_at},
    }))
    log = tmp_path / "stub.log"
    log.write_text("")
    env = _env(tmp_path, root, state_path, log)
    env["WAKE_WATCHDOG_NOW"] = str(alerted_at + 6 * 60 * 60 + 1)
    _run(env)
    text = log.read_text()
    assert text.count("--holder commander") == 1
    assert "mesh send lab-ovh" not in text
    log.write_text("")
    _run(env)
    assert "--holder commander" not in log.read_text()


def test_live_pid_whose_parent_is_not_the_cell_session_reads_as_dead(tmp_path):
    """The heartbeat pid is alive, but its parent is this test, not the
    cell's claude session. Where /proc can be read, the channel is dead.
    """
    if not Path("/proc").is_dir():
        pytest.skip("parent rule needs a proc tree; the no-proc pin covers this host")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        inbox = tmp_path / "lab-ovh" / "mesh-sidecar" / "inbox.log"
        inbox.parent.mkdir(parents=True)
        inbox.write_text("")
        (inbox.parent / "channel_heartbeat.json").write_text(
            json.dumps({"pid": child.pid, "ts": time.time()}), encoding="utf-8")
        assert _channel_alive(str(inbox)) is False
        cells = {"lab-ovh": str(inbox)}
        state = {}
        assert scan(cells, [], state, now=0) == []
        assert scan(cells, [], state, now=200) == ["lab-ovh"]
    finally:
        child.kill()
        child.wait(timeout=5)


def test_fresh_heartbeat_with_a_live_pid_is_alive_when_proc_cannot_be_read(tmp_path):
    """Card #2733: the parent rule applies only where the parent can be read.
    A missing proc tree does not require a claude parent. A fresh heartbeat
    whose pid is live counts as alive; a dead pid does not."""
    inbox = tmp_path / "drop" / "mesh-sidecar" / "inbox.log"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("")
    hb = inbox.parent / "channel_heartbeat.json"
    missing = tmp_path / "no-such-proc"
    hb.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}), encoding="utf-8")
    assert _channel_alive(str(inbox), cell="drop", proc_root=missing) is True
    hb.write_text(json.dumps({"ts": time.time(), "pid": 1 << 30}), encoding="utf-8")
    assert _channel_alive(str(inbox), cell="drop", proc_root=missing) is False
    hb.write_text(json.dumps({"ts": time.time() - 600, "pid": os.getpid()}), encoding="utf-8")
    assert _channel_alive(str(inbox), cell="drop", proc_root=missing) is False
