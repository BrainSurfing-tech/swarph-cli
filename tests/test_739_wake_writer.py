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

from swarph_cli.scripts import wake_watchdog
from swarph_cli.scripts.wake_watchdog import (
    enforce_writers,
    mesh_send,
    recorded_supervisor,
    supervisor_from_cgroup,
    systemd_owns_monitor,
    writer_alert,
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
    state["fixture-cell"]["writer_down"] = True
    assert once(2) == []
    assert once(2) == []
    assert once(0) == []
    assert state["fixture-cell"]["writer_down"] is False
    assert once(2) == ["fixture-cell"]


def test_a_lost_send_is_not_marked_alerted(tmp_path):
    root = tmp_path / "state"
    inbox = _cell(root, "fixture-cell", fresh=True)
    state = {}
    first = enforce_writers(
        {"fixture-cell": inbox}, status=lambda _n: 2, run=_absent, state=state)
    assert first == ["fixture-cell"]
    assert state["fixture-cell"].get("writer_down") is not True
    again = enforce_writers(
        {"fixture-cell": inbox}, status=lambda _n: 2, run=_absent, state=state)
    assert again == ["fixture-cell"]


def test_fake_cgroup_files_read_the_non_template_units_as_supervised(tmp_path):
    """Fails on 0237e7b, which has no cgroup read."""
    lab = tmp_path / "swarph-monitor.service.cgroup"
    gemini = tmp_path / "swarph-monitor-gemini-researcher.service.cgroup"
    lab.write_text("0::/system.slice/swarph-monitor.service\n", encoding="utf-8")
    gemini.write_text(
        "0::/system.slice/swarph-monitor-gemini-researcher.service\n",
        encoding="utf-8")
    assert supervisor_from_cgroup(lab.read_text(encoding="utf-8")) == "swarph-monitor.service"
    assert supervisor_from_cgroup(gemini.read_text(encoding="utf-8")) == (
        "swarph-monitor-gemini-researcher.service")
    assert "supervised by swarph-monitor.service" in writer_alert(
        "lab-ovh", lab.read_text(encoding="utf-8"))
    assert "supervised by swarph-monitor-gemini-researcher.service" in writer_alert(
        "gemini-researcher", gemini.read_text(encoding="utf-8"))
    session = tmp_path / "user.cgroup"
    session.write_text("0::/user.slice/user@1000.service\n", encoding="utf-8")
    assert supervisor_from_cgroup(session.read_text(encoding="utf-8")) == "unsupervised"
    assert writer_alert("fixture-cell", None).endswith("unsupervised")


def test_the_alert_names_the_supervisor_recorded_in_the_pidfile(tmp_path):
    """Fails when the recorded-supervisor read is removed.

    The pid in the file is 1. Opening that process's control group would
    name init, not these units. The file is what survives the monitor.
    """
    root = tmp_path / "state"
    units = {
        "lab-ovh": "swarph-monitor.service",
        "gemini-researcher": "swarph-monitor-gemini-researcher.service",
        "fixture-cell": "scratch-761.service",
    }
    for cell, unit in units.items():
        side = root / cell / "mesh-sidecar"
        side.mkdir(parents=True)
        (side / "monitor.pid").write_text(json.dumps({
            "pid": 1,
            "supervisor": unit,
        }), encoding="utf-8")
        assert recorded_supervisor(root, cell) == unit
        assert f"supervised by {unit}" in writer_alert(cell, recorded_supervisor(root, cell))
    assert "/proc/" not in SRC.read_text(encoding="utf-8")
    bare = root / "nobody" / "mesh-sidecar"
    bare.mkdir(parents=True)
    (bare / "monitor.pid").write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert recorded_supervisor(root, "nobody") is None
    assert writer_alert("nobody", recorded_supervisor(root, "nobody")).endswith("unsupervised")


def test_ownership_changes_the_alert_when_the_cgroup_names_nothing():
    plain = writer_alert("fixture-cell", None, owned=False)
    owned = writer_alert("fixture-cell", None, owned=True)
    assert plain.endswith("unsupervised")
    assert "supervised by swarph-monitor@fixture-cell.service" in owned
    assert plain != owned
    assert "hand-start" not in SRC.read_text(encoding="utf-8")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0",
)
def test_a_failed_send_is_retried_on_the_next_run(tmp_path):
    root = tmp_path / "state"
    _cell(root, "fixture-cell", fresh=True)
    stub = tmp_path / "swarph"
    log = tmp_path / "calls.log"
    count = tmp_path / "sends"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        if [ "$1 $2" = "mesh send" ]; then
          n=0
          [ -f "{count}" ] && n=$(cat "{count}")
          n=$((n+1))
          echo "$n" > "{count}"
          [ "$n" = "1" ] && exit 1
          exit 0
        fi
        exit 2
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
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]
    first = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert first.returncode == 0
    assert json.loads(state_path.read_text())["fixture-cell"].get("writer_down") is not True
    second = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert second.returncode == 0
    assert json.loads(state_path.read_text())["fixture-cell"]["writer_down"] is True
    text = log.read_text(encoding="utf-8")
    assert text.count("mesh send drop-on-meta-edge") == 2
    assert "mesh send lab-ovh" not in text


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0",
)
def test_main_does_not_mark_before_send_or_ignore_a_failed_rc(tmp_path):
    root = tmp_path / "state"
    _cell(root, "fixture-cell", fresh=True)
    stub = tmp_path / "swarph"
    log = tmp_path / "calls.log"
    state_path = tmp_path / "wake.json"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        if [ "$1 $2" = "mesh send" ]; then
          if grep -q '"writer_down": true' "{state_path}" 2>/dev/null; then
            echo MARKED_BEFORE_SEND >> "$STUB_LOG"
          fi
          if grep -q '"writer_down":true' "{state_path}" 2>/dev/null; then
            echo MARKED_BEFORE_SEND >> "$STUB_LOG"
          fi
          exit 1
        fi
        exit 2
    """), encoding="utf-8")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(stub),
        "STUB_LOG": str(log),
    }
    env.pop("SWARPH_SELF", None)
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    text = log.read_text(encoding="utf-8")
    assert "MARKED_BEFORE_SEND" not in text
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["fixture-cell"].get("writer_down") is not True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute the POSIX swarph stub as argv0",
)
def test_outage_cards_say_carries_the_token_and_is_marked_once(tmp_path):
    root = tmp_path / "state"
    side = root / "nobody" / "mesh-sidecar"
    side.mkdir(parents=True)
    (side / "inbox.log").write_text("", encoding="utf-8")
    stub = tmp_path / "swarph"
    log = tmp_path / "calls.log"
    state_path = tmp_path / "wake.json"
    token = tmp_path / "service-wake-watchdog.token"
    secret = "sentinel-token-value-not-a-real-token"
    token.write_text(secret + "\n", encoding="utf-8")
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$STUB_LOG"
        exit 0
    """), encoding="utf-8")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "SWARPH_BIN": str(stub),
        "STUB_LOG": str(log),
    }
    env.pop("SWARPH_SELF", None)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "wake-watchdog", "--token-file", str(token),
           "--escalate", "drop-on-meta-edge"]
    subprocess.run(cmd, env=env, check=True)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    saved["nobody"]["missing_since"] = 0
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    log.write_text("", encoding="utf-8")
    for _ in range(3):
        subprocess.run(cmd, env=env, check=True)
    text = log.read_text(encoding="utf-8")
    assert text.count("cards say 729") == 1
    assert f"--token-file {token}" in text
    assert secret not in text
    assert json.loads(state_path.read_text(encoding="utf-8"))["nobody"]["outage"] is True


def test_a_self_send_is_refused_and_logged(capsys):
    rc = mesh_send("swarph", "lab-ovh", "lab-ovh", "fixture-cell: writer-down",
                   "/tmp/service-wake-watchdog.token")
    assert rc == 2
    assert "refusing self-send to lab-ovh" in capsys.readouterr().err


def test_the_token_argv_carries_the_path_and_not_the_value(tmp_path, monkeypatch):
    secret = "sentinel-token-value-not-a-real-token"
    path = tmp_path / "service-wake-watchdog.token"
    path.write_text(secret + "\n", encoding="utf-8")
    recorded = []

    def fake_run(argv, **_kwargs):
        recorded.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(wake_watchdog.subprocess, "run", fake_run)
    rc = mesh_send("swarph", "lab-ovh", "wake-watchdog", "fixture-cell: writer-down",
                   str(path))
    assert rc == 0
    argv = recorded[0]
    assert argv[0:4] == ["swarph", "mesh", "send", "lab-ovh"]
    assert "--as" in argv and "wake-watchdog" in argv
    assert "--token-file" in argv
    assert str(path) in argv
    assert secret not in argv


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
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]
    for _ in range(3):
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        assert proc.returncode == 0
    text = log.read_text(encoding="utf-8")
    assert text.count("mesh send drop-on-meta-edge") == 1
    assert "mesh send lab-ovh" not in text
    assert "--as wake-watchdog" not in text
    assert "token-file" not in text
    assert "monitor start" not in text
    assert "gridiron" not in text
