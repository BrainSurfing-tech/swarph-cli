"""#654: the reexec path unit + `swarph monitor reexec-on-change`.

Closes INSTALLED vs RUNNING on one box: a long-running monitor holds the
modules it imported at start, so `pipx install --force` desyncs every
running cell while `swarph --version` (disk) reports the new build.
Measured 9/10 stale on a five-day-old box.

The requirements as tests:
  R1  the .path unit watches the package __init__.py with PathChanged
      (close-after-write), NEVER PathModified (fires mid-install).
  R2  the watched path is resolved from the LIVE INTERPRETER at install
      time — a hardcoded lib/python3.14/ path never fires after an
      interpreter bump, the silent shape this family kills.
  R3  restarts are serial with a stagger, never simultaneous.
  R4  a recorded hold is SKIPPED, with its reason, and is readable from
      `monitor status`. A corrupt hold file reads as HELD, never as absent.
  R5  every action is reported: cell, running-since (the honest old-side
      coordinate — a running build is unknowable from outside, #649), and
      the new build.
  R6  monitors systemd does not own (tmux-scoped, hand-started) are NAMED
      out of scope, never omitted.

The live accept legs (touch the watched file on a real box; a held cell
stays; the report names the tmux-scoped two) run against the installed
unit — commander-gated. These tests guard everything short of systemd.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import swarph_cli.commands.monitor as monitor


# ── rig ────────────────────────────────────────────────────────────────────

def _mk_state(root: Path, peer: str, pid: int, hold: dict | None = None) -> Path:
    sd = root / peer / "mesh-sidecar"
    sd.mkdir(parents=True)
    (sd / "monitor.pid").write_text(f"{pid} {{}}\n")
    if hold is not None:
        (sd / monitor._REEXEC_HOLD).write_text(json.dumps(hold))
    return sd


@pytest.fixture
def rig(monkeypatch):
    """alpha + beta unit-owned (systemd's registry says so), gamma tmux-scoped
    (live pidfile, no unit). No real systemctl, sleep, /proc, or pidfile
    reader — every seam is stubbed."""
    calls = {"restart": [], "sleep": []}
    monkeypatch.setattr(monitor, "_systemctl_run",
                        lambda a: calls["restart"].append(a[-1]) or 0)
    monkeypatch.setattr(monitor, "_sleep",
                        lambda s: calls["sleep"].append(s))
    monkeypatch.setattr(monitor, "_unit_owned_instances", lambda: ["alpha", "beta"])
    monkeypatch.setattr(monitor, "_proc_start_iso", lambda pid: "2026-08-27T10:37:00+00:00")
    monkeypatch.setattr(monitor, "_installed_build",
                        lambda: ("0.53.1", "/x/lib/python3.14/site-packages/swarph_cli/__init__.py",
                                 "2026-08-28T01:00:00+00:00"))
    pids = {"alpha": 111, "beta": 222, "gamma": 333}

    def _pidfile_status(path):
        # the state dir is .../<peer>/mesh-sidecar/monitor.pid
        peer = path.parent.parent.name
        if peer not in pids:
            return "absent", None
        return "live_ours", {"pid": pids[peer]}
    monkeypatch.setattr(monitor.mesh, "pidfile_status", _pidfile_status)
    monkeypatch.setattr(monitor, "_read_cgroup",
                        lambda pid: {333: "0::/user.slice/tmux-spawn-f192f917.scope"}.get(pid))
    return calls


def _run(root: Path, *extra: str) -> int:
    return monitor.run_monitor(
        ["reexec-on-change", "--state-root", str(root), *extra])


# ── R1/R2: the .path unit ─────────────────────────────────────────────────

def test_oneshot_applies_not_dry_runs():
    """The path unit's oneshot must pass --apply. Without it the verb
    prints a plan and restarts nobody — the fleet reads as protected
    while INSTALLED vs RUNNING stays open. Found at accept, before
    enable: the first render was a dry-run ExecStart."""
    text = monitor._read_packaged(("systemd", "swarph-monitor-reexec.service"))
    start = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert start and "--apply" in start[0], start


def test_path_unit_uses_pathchanged_never_pathmodified():
    text = monitor._read_packaged(("systemd", "swarph-monitor-reexec.path"))
    directives = [line for line in text.splitlines()
                  if line and not line.startswith("#")]
    assert any(line.startswith("PathChanged=") for line in directives)
    assert not any(line.startswith("PathModified") for line in directives), \
        "PathModified fires on a half-written tree mid-install"
    assert "Unit=swarph-monitor-reexec.service" in directives


def test_watched_path_is_resolved_not_hardcoded(monkeypatch, capsys):
    """R2: the rendered unit carries whatever the LIVE interpreter reports —
    the test substitutes a different python version's path and the unit must
    follow it. A hardcoded path cannot pass this."""
    class _Spec:
        origin = "/other/lib/python3.99/site-packages/swarph_cli/__init__.py"
    # the verb imports importlib.util inside the call — patch the source module
    import importlib.util as iu
    monkeypatch.setattr(iu, "find_spec", lambda name: _Spec())
    rc = monitor.run_monitor(["install-reexec"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PathChanged=/other/lib/python3.99/site-packages/swarph_cli/__init__.py" in out


def test_install_reexec_names_the_interpreter_bump_caveat(capsys):
    rc = monitor.run_monitor(["install-reexec"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "interpreter bump" in out, "the R2 expiry must be printed, not implied"


def test_install_reexec_write_lands_both_units(tmp_path, capsys):
    rc = monitor.run_monitor(["install-reexec", "--write", "--dir", str(tmp_path)])
    if os.name == "nt":
        assert rc == 2
        assert "Linux-only" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []
        return
    assert rc == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["swarph-monitor-reexec.path", "swarph-monitor-reexec.service"]


# ── R3/R4/R5/R6: reexec-on-change ─────────────────────────────────────────

def test_dry_run_restarts_nothing_and_names_the_plan(tmp_path, rig, capsys):
    _mk_state(tmp_path, "alpha", 111)
    _mk_state(tmp_path, "beta", 222)
    rc = _run(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == [], "dry run must not restart"
    assert "would restart: alpha" in out and "would restart: beta" in out
    assert "DRY RUN" in out


def test_apply_restarts_serially_with_the_stagger(tmp_path, rig, capsys):
    _mk_state(tmp_path, "alpha", 111)
    _mk_state(tmp_path, "beta", 222)
    rc = _run(tmp_path, "--apply", "--stagger-s", "7")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == ["swarph-monitor@alpha.service",
                              "swarph-monitor@beta.service"]
    assert rig["sleep"] == [7.0], "exactly one stagger BETWEEN two restarts"
    assert "reexec: alpha restarted onto 0.53.1" in out
    assert "was running since 2026-08-27" in out  # R5: the old-side coordinate


def test_a_recorded_hold_is_skipped_with_its_reason(tmp_path, rig, capsys):
    """R4: drop-on-meta-edge's shape — 'pull-only cell, restart when a change
    reaches this cell' is a correct engineering decision, not drift."""
    _mk_state(tmp_path, "alpha", 111,
              hold={"reason": "pull-only cell; 0.53.x changes tmux-sink only",
                    "recorded_by": "drop-on-meta-edge",
                    "recorded_at": "2026-08-27T18:00:00+00:00"})
    _mk_state(tmp_path, "beta", 222)
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == ["swarph-monitor@beta.service"], \
        "the held cell must NOT be restarted"
    assert "HELD, skipped: alpha" in out
    assert "pull-only cell" in out, "the skip carries the recorded reason"


def test_a_corrupt_hold_reads_as_held_never_absent(tmp_path, rig, capsys):
    sd = _mk_state(tmp_path, "alpha", 111)
    (sd / monitor._REEXEC_HOLD).write_text("{not json")
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    # alpha held (corrupt => held, never absent); beta unaffected — a corrupt
    # hold on one cell must not block the fleet either
    assert rig["restart"] == ["swarph-monitor@beta.service"], rig["restart"]
    assert "UNREADABLE HOLD FILE" in out


def test_out_of_scope_cells_are_NAMED_not_omitted(tmp_path, rig, capsys):
    """R6: meta-muse/mistral's shape — tmux-scoped monitors the unit cannot
    reach must appear in the report, or the box reads fully covered while
        20% is untouched."""
    _mk_state(tmp_path, "alpha", 111)
    _mk_state(tmp_path, "gamma", 333)
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == ["swarph-monitor@alpha.service",
                              "swarph-monitor@beta.service"]
    assert "OUT OF SCOPE" in out and "gamma" in out
    assert "tmux-spawn-f192f917.scope" in out, "the scope it IS under is the evidence"


def test_a_stale_pidfile_is_reported_not_restarted(tmp_path, rig, capsys, monkeypatch):
    """A peer with NO unit and a stale pidfile has nothing to reexec — reported,
    and revival is named as the watchdog's job, not silently taken over."""
    _mk_state(tmp_path, "gamma", 333)
    monkeypatch.setattr(monitor.mesh, "pidfile_status",
                        lambda path: ("stale", {"pid": 333}))
    monkeypatch.setattr(monitor, "_unit_owned_instances", lambda: [])
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == []
    assert "not running" in out and "gamma" in out
    assert "watchdog" in out, "revival is the watchdog's job — say whose it is"


def test_a_failed_restart_is_loud_and_exits_nonzero(tmp_path, rig, capsys, monkeypatch):
    _mk_state(tmp_path, "alpha", 111)
    monkeypatch.setattr(monitor, "_systemctl_run", lambda a: 5)
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 1, "a failed restart must fail the oneshot — a green oneshot " \
                    "with a dead cell is the silent shape this card kills"
    assert "reexec FAILED: alpha" in out


# ── hold verbs + status readability (R4) ──────────────────────────────────

def test_hold_round_trip_and_status_surfaces_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "alpha")
    sd = tmp_path / "alpha" / "mesh-sidecar"
    sd.mkdir(parents=True)
    monkeypatch.setattr(monitor.mesh, "_default_sidecar_state_dir", lambda s: sd)
    rc = monitor.run_monitor(["hold-reexec", "--reason", "pull-only cell"])
    assert rc == 0
    hold = json.loads((sd / monitor._REEXEC_HOLD).read_text())
    assert hold["reason"] == "pull-only cell"
    assert hold["recorded_by"] == "alpha"

    capsys.readouterr()
    info_args = dict(self_name="alpha", state_dir=str(sd))
    # status surfaces the hold (R4: readable, not a note in memory) — call
    # _collect directly; the full status render needs sinks/gateway fixtures
    class _A:
        pass
    a = _A()
    a.self_name, a.state_dir = info_args["self_name"], info_args["state_dir"]
    a.gateway, a.token_file, a.deliver = None, None, []
    info = monitor._collect(a)
    assert info["reexec_hold"]["reason"] == "pull-only cell"

    rc = monitor.run_monitor(["clear-reexec-hold"])
    assert rc == 0
    assert not (sd / monitor._REEXEC_HOLD).exists()


def test_hold_requires_a_reason(capsys):
    rc = monitor.run_monitor(["hold-reexec", "--reason", "  "])
    assert rc == 2
    assert "reason" in capsys.readouterr().err
