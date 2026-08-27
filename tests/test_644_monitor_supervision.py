"""Card #644 — Windows monitor supervision: ownership convention + hold semantics.

Windows has no pid→task reverse map, so "what supervises this pid" is a
CONVENTION: the supervisor names itself (SWARPH_SUPERVISOR / --supervisor),
the monitor records it in the pidfile, and `status` reads it back. An absent
claim is an ORPHAN — a fact, said plainly, never omitted.

The supervision hold exists because a watchdog that revives a
deliberately-stopped monitor is the same class of surprise as a second
instance beside a hand-started one. `stop` writes it, `start` clears it,
heartbeat-check reports HELD (a third state — not OK, not DEGRADED).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from swarph_cli.commands import mesh, monitor


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "src" / "swarph_cli" / "scripts" / "install_monitor_task_windows.ps1"


@pytest.fixture(autouse=True)
def _clean_composer(monkeypatch):
    monkeypatch.setattr(mesh, "_composer_state", lambda t: "clear")


def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_SUPERVISOR", raising=False)


def _own_record(**over):
    rec = {
        "pid": os.getpid(),
        "self": "cursor-win",
        "sinks": ["pull"],
        "started_at": 0.0,
        "cmdline": mesh._proc_cmdline(os.getpid()),
    }
    rec.update(over)
    return rec


def _write_pidfile(tmp_path, record):
    (tmp_path / "monitor.pid").write_text(json.dumps(record), encoding="utf-8")


def _run(args, tmp_path):
    return monitor.run_monitor(args[:1] + ["--state-dir", str(tmp_path)] + args[1:])


# ── the ownership convention ────────────────────────────────────────────────

def test_pidfile_records_supervisor_when_given(tmp_path):
    mesh.write_pidfile(tmp_path / "monitor.pid", self_name="cursor-win",
                       sinks=[], poll_s=30, supervisor="task:Swarph cursor-win Monitor")
    rec = json.loads((tmp_path / "monitor.pid").read_text(encoding="utf-8"))
    assert rec["supervisor"] == "task:Swarph cursor-win Monitor"


def test_pidfile_omits_supervisor_key_when_not_given(tmp_path):
    """Absent means ORPHAN — a MISSING key, not an empty string, so a reader
    never has to guess whether '' is a claim."""
    mesh.write_pidfile(tmp_path / "monitor.pid", self_name="cursor-win",
                       sinks=[], poll_s=30)
    rec = json.loads((tmp_path / "monitor.pid").read_text(encoding="utf-8"))
    assert "supervisor" not in rec


def test_supervisor_flag_beats_env_and_env_is_the_default(monkeypatch):
    """Resolution happens at RUN time: a parser-built default would freeze
    whatever environment the parser's BUILDER held (caught by this test's
    first draft)."""
    import argparse
    monkeypatch.setenv("SWARPH_SUPERVISOR", "task:FromEnv")
    assert monitor._resolve_supervisor(argparse.Namespace(supervisor=None)) == "task:FromEnv"
    assert monitor._resolve_supervisor(
        argparse.Namespace(supervisor="task:FromFlag")) == "task:FromFlag"
    monkeypatch.delenv("SWARPH_SUPERVISOR")
    assert monitor._resolve_supervisor(argparse.Namespace(supervisor=None)) is None


def test_status_names_the_supervisor(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record(supervisor="task:Swarph cursor-win Monitor"))
    assert _run(["status"], tmp_path) in (0, 1)
    assert "supervised by: task:Swarph cursor-win Monitor" in capsys.readouterr().out


def test_status_says_ORPHAN_when_nothing_claims_the_pid(monkeypatch, tmp_path, capsys):
    """The ownership query must answer for a hand-started monitor too — the
    answer is ORPHAN. Omitting the line is how 4 orphaned monitors hid on
    lab-ovh."""
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    assert _run(["status"], tmp_path) in (0, 1)
    out = capsys.readouterr().out
    assert "ORPHAN" in out
    assert "supervised by: NOTHING ON RECORD" in out


# ── the supervision hold ────────────────────────────────────────────────────

def test_stop_writes_the_hold(monkeypatch, tmp_path):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    monkeypatch.setattr(mesh, "_terminate", lambda pid: None)
    assert _run(["stop"], tmp_path) == 0
    hold = json.loads((tmp_path / "supervision_hold.json").read_text(encoding="utf-8"))
    assert hold["by"] == "swarph monitor stop"
    assert hold["since"] > 0


def test_start_clears_the_hold(monkeypatch, tmp_path):
    _env(monkeypatch)
    (tmp_path / "supervision_hold.json").write_text(
        json.dumps({"since": 1.0, "by": "swarph monitor stop"}), encoding="utf-8")
    monkeypatch.setattr(monitor, "_verify_self_is_registered",
                        lambda *a, **k: (True, "registered"))
    monkeypatch.setattr(mesh, "_monitor_iteration", lambda state: None)
    assert _run(["start", "--once"], tmp_path) == 0
    assert not (tmp_path / "supervision_hold.json").exists()


def test_status_names_the_hold_when_not_running(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    (tmp_path / "supervision_hold.json").write_text(
        json.dumps({"since": 1.0, "by": "swarph monitor stop"}), encoding="utf-8")
    assert _run(["status"], tmp_path) == 2
    out = capsys.readouterr().out
    assert "supervision HOLD" in out
    assert "watchdog will not revive" in out


def test_heartbeat_check_reports_HELD_not_DEGRADED(monkeypatch, tmp_path, capsys):
    """A deliberate stop is not an outage: a third state, so the gateway row
    is neither green (not draining) nor red (nothing is wrong)."""
    _env(monkeypatch)
    (tmp_path / "supervision_hold.json").write_text(
        json.dumps({"since": 1.0, "by": "swarph monitor stop"}), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(monitor, "_register_capabilities",
                        lambda self_name, gateway, token_file, caps: captured.update(caps) or 0)
    rc = _run(["heartbeat-check", "--as", "cursor-win"], tmp_path)
    assert rc == 0
    assert captured["drain_status"] == "HELD"
    assert captured["degraded_cause"] == "deliberately_stopped"
    assert "HELD" in capsys.readouterr().out


# ── the installer ───────────────────────────────────────────────────────────

def test_installer_is_packaged():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"scripts/*.ps1"' in text
    assert INSTALLER.is_file()


def test_install_task_print_path(capsys):
    assert monitor.run_monitor(["install-task", "--print-path"]) == 0
    assert Path(capsys.readouterr().out.strip()).samefile(INSTALLER)


def test_install_task_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    rc = monitor.run_monitor(["install-task", "--as", "cursor-win"])
    assert rc == 2


def test_installer_content_pins_the_six_properties():
    """Static pins for the load-bearing semantics — the metal demo proves them
    live, these keep a later edit from quietly dropping one."""
    text = INSTALLER.read_text(encoding="utf-8")
    # restart policy + second-instance guard + boot survival
    assert "-MultipleInstances IgnoreNew" in text
    assert "-RestartCount" in text and "-RestartInterval" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    # the runner owns the process lifetime
    assert "'--foreground'" in text
    # THE EXIT CODE IS THE RESTART SIGNAL — metal-found 2026-08-27: without
    # this, powershell -File exits 0 even when the monitor was killed, and
    # restart-on-failure never fires (event log read "code de retour 0")
    assert "exit `$LASTEXITCODE" in text
    # the watchdog is load-bearing: heartbeat-check always, revive only when down
    assert "heartbeat-check" in text
    assert "$LASTEXITCODE -eq 2" in text
    assert "Start-Process" in text
    # the hold is respected
    assert "supervision_hold.json" in text
    # ownership convention
    assert "SWARPH_SUPERVISOR" in text
    # the counter/journal leg is enabled or the installer REFUSES
    assert "wevtutil" in text
    assert "TaskScheduler/Operational" in text


def test_installer_keeps_tokens_out_of_task_content():
    """The monitor resolves its own token by convention; the installer must
    not take one — a task definition is readable by any local process."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "TokenFile" not in text
    assert "Get-Content $Token" not in text
