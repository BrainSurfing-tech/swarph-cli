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
  R7  the oneshot must survive the install that triggers it: pipx deletes
      ~/.local/bin/swarph between closing __init__.py (PathChanged) and
      restoring the shim. Restart=on-failure + RestartSec retries a 203/EXEC
      into the healed state. A touch-based green does NOT prove R7.

The live accept legs (real `pipx install --force` on a real box; a held cell
stays; the report names the tmux-scoped two; a forced 203 is loud) run
against the installed unit — commander-gated. These tests guard everything
short of systemd.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
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
    monkeypatch.setattr(monitor, "_supervised_monitor_units", lambda: [
        ("alpha", "swarph-monitor@alpha.service"),
        ("beta", "swarph-monitor@beta.service"),
    ])
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


def test_oneshot_timeout_outlasts_a_full_fleet_stagger():
    """PID1's oneshot default is 90s. Nine stale monitors at the verb's
    default 15s stagger is 135s — the oneshot is killed mid-fleet
    (cursor-win on #348). The packaged unit must set TimeoutStartSec
    above that product, not inherit 90s."""
    text = monitor._read_packaged(("systemd", "swarph-monitor-reexec.service"))
    lines = [ln for ln in text.splitlines() if ln.startswith("TimeoutStartSec=")]
    assert lines, "oneshot default 90s is shorter than 9x15s stagger"
    raw = lines[0].split("=", 1)[1].strip().lower()
    if raw.endswith("min"):
        seconds = float(raw[:-3]) * 60
    elif raw.endswith("s"):
        seconds = float(raw[:-1])
    else:
        seconds = float(raw)
    assert seconds >= 180, f"{raw!r} is still shorter than a full-fleet stagger"


def test_oneshot_survives_the_reinstall_window_by_condition_not_by_budget():
    """R7 rewritten by #807. The old answer was a retry budget: 17 x 15 s inside a
    300 s window = 255 s of retries by construction, so ONE reinstall latched the
    unit `unit-start-limit-hit` (lab-ovh 2026-09-07 04:40:00Z, 18 min after v0.55.0).
    The new answer: ExecCondition= imports the package with the shim's interpreter —
    mid-swap the unit is SKIPPED, not failed — and the budget cannot be spent inside
    one window: at most two starts per StartLimitIntervalSec."""
    text = monitor._read_packaged(("systemd", "swarph-monitor-reexec.service"))
    assert "Type=oneshot" in text
    assert [ln for ln in text.splitlines() if ln.startswith("Restart=")] == ["Restart=on-failure"]
    cond = [ln for ln in text.splitlines() if ln.startswith("ExecCondition=")]
    assert cond == ['ExecCondition=/usr/bin/test -x <SWARPH_BIN>',
                     'ExecCondition=<INTERPRETER> -c "import swarph_cli"'], (
        "both artifacts pip rewrites are conditions, the shim FIRST (fourth defect, 2026-09-15)")
    def _sec(key):
        raw = [ln for ln in text.splitlines() if ln.startswith(f"{key}=")][0].split("=", 1)[1].strip().lower()
        return float(raw[:-3]) * 60 if raw.endswith("min") else float(raw.rstrip("s"))
    restart_sec, interval = _sec("RestartSec"), _sec("StartLimitIntervalSec")
    burst = int([ln for ln in text.splitlines() if ln.startswith("StartLimitBurst=")][0].split("=", 1)[1])
    assert burst >= 2 and interval <= 2 * restart_sec, (
        f"latchable: {burst} starts at RestartSec={restart_sec}s fit inside {interval}s")
    assert not [ln for ln in text.splitlines() if ln.startswith("OnFailure=")]   # drop-in, never baked in


def test_path_unit_uses_pathchanged_never_pathmodified():
    text = monitor._read_packaged(("systemd", "swarph-monitor-reexec.path"))
    directives = [line for line in text.splitlines()
                  if line and not line.startswith("#")]
    assert any(line.startswith("PathChanged=") for line in directives)
    assert not any(line.startswith("PathModified") for line in directives), \
        "PathModified fires on a half-written tree mid-install"
    assert "Unit=swarph-monitor-reexec.service" in directives


def _fake_shim(tmp_path: Path, reported: str) -> Path:
    """A swarph shim whose shebang names a fake interpreter that reports `reported`
    as the swarph_cli __init__ — the tree THAT binary would load."""
    interp = tmp_path / "fake-python"
    interp.write_text(f"#!/bin/sh\necho '{reported}'\n")
    interp.chmod(0o755)
    shim = tmp_path / "swarph"
    shim.write_text(f"#!{interp}\n# fake shim\n")
    shim.chmod(0o755)
    return shim


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807_watched_tree_comes_from_the_binary_the_monitors_execute(tmp_path, capsys, monkeypatch):
    """The installer's own interpreter is the wrong witness (a pipx-run installer watched
    the pipx tree while the 8 monitors loaded the pip --user tree). The shim's interpreter
    is asked for the ExecStart tree; the RESIDENTS' trees are watched as well."""
    shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {
        "/consumed/site-packages/swarph_cli/__init__.py": {"cells": ["cell-a"], "interpreter": "/usr/bin/python3"},
        "_unreadable": []})
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PathChanged=/consumed/site-packages/swarph_cli/__init__.py" in out
    assert "loaded by: cell-a; the shim's tree (ExecStart runs it)" in out
    assert "/pipx/venv/" not in out and "the monitors do not load it" not in out


@pytest.mark.skipif(os.name == "nt", reason="drop-ins are systemd")
def test_807_on_failure_writes_a_dropin_for_both_units(tmp_path, capsys, monkeypatch):
    shim = _fake_shim(tmp_path, "/consumed/swarph_cli/__init__.py")
    # The unit listing needs systemctl; macOS has none and the verb abstains with
    # rc 2. This test is about the OnFailure drop-ins, so stub the listing.
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    target = tmp_path / "units"; target.mkdir()
    rc = monitor.run_monitor(["install-reexec", "--write", "--dir", str(target), "--swarph-bin", str(shim),
                              "--on-failure", "mercury-alert@%n.service"])
    assert rc == 0, capsys.readouterr()
    for unit in ("swarph-monitor-reexec.path", "swarph-monitor-reexec.service"):
        conf = (target / f"{unit}.d" / "10-onfailure.conf").read_text()
        assert "[Unit]\nOnFailure=mercury-alert@%n.service" in conf
    assert "next: systemctl daemon-reload && systemctl reset-failed" in capsys.readouterr().out


def test_watched_path_is_resolved_not_hardcoded(tmp_path, monkeypatch, capsys):
    """R2 (#654, tightened by #807): the rendered unit carries whatever the swarph
    BINARY's interpreter reports — never a literal baked into the template."""
    if os.name == "nt":
        pytest.skip("shebang resolution is POSIX")
    shim = _fake_shim(tmp_path, "/fake/live/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PathChanged=/fake/live/site-packages/swarph_cli/__init__.py" in out
    assert "<SITE_PACKAGES_INIT>" not in out and "<INTERPRETER>" not in out

@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_install_reexec_names_the_interpreter_bump_caveat(tmp_path, capsys, monkeypatch):
    shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "interpreter bump" in out, "the R2 expiry must be printed, not implied"


def test_install_reexec_write_lands_both_units(tmp_path, capsys, monkeypatch):
    units = tmp_path / "units"; units.mkdir()
    if os.name != "nt":
        shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
        monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
        monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
        argv = ["install-reexec", "--write", "--dir", str(units), "--swarph-bin", str(shim)]
    else:
        argv = ["install-reexec", "--write", "--dir", str(units)]
    tmp_path = units
    rc = monitor.run_monitor(argv)
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


def test_dry_run_uses_the_default_state_root_when_omitted(tmp_path, rig, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "swarph_state"
    _mk_state(root, "alpha", 111)
    rc = monitor.run_monitor(["reexec-on-change"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would restart: alpha" in out


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


def test_a_non_utf8_hold_reads_as_held_never_absent(tmp_path, rig, capsys):
    sd = _mk_state(tmp_path, "alpha", 111)
    (sd / monitor._REEXEC_HOLD).write_bytes(b"\xff")
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == ["swarph-monitor@beta.service"], rig["restart"]
    assert "UNREADABLE HOLD FILE (UnicodeDecodeError)" in out


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
    monkeypatch.setattr(monitor, "_supervised_monitor_units", lambda: [])
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


@pytest.mark.parametrize(
    "exc",
    [OSError("missing systemctl"), subprocess.CalledProcessError(1, ["systemctl"])],
)
def test_unit_owned_instances_translates_probe_failures_to_runtimeerror(monkeypatch, exc):
    def _boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(monitor.subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="systemctl cannot list running"):
        monitor._supervised_monitor_units()


def test_bespoke_named_monitor_units_restarted_with_actual_unit_name(
    tmp_path, rig, capsys, monkeypatch,
):
    """#665: lab-ovh / gemini-researcher / gridiron bespoke units must reexec
    via their REAL unit name, not be misreported OUT OF SCOPE."""
    monkeypatch.setattr(monitor, "_supervised_monitor_units", lambda: [
        ("alpha", "swarph-monitor@alpha.service"),
        ("gemini-researcher", "swarph-monitor-gemini-researcher.service"),
        ("lab-ovh", "swarph-monitor.service"),
    ])
    _mk_state(tmp_path, "alpha", 111)
    _mk_state(tmp_path, "gemini-researcher", 555)
    _mk_state(tmp_path, "lab-ovh", 444)
    rc = _run(tmp_path, "--apply")
    out = capsys.readouterr().out
    assert rc == 0
    assert rig["restart"] == [
        "swarph-monitor@alpha.service",
        "swarph-monitor-gemini-researcher.service",
        "swarph-monitor.service",
    ]
    assert "OUT OF SCOPE" not in out or "lab-ovh" not in out
    assert "3 unit-supervised" in out
    assert "3 state dirs with pidfile" in out


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


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807_resolver_ignores_the_installers_pythonpath(tmp_path, monkeypatch):
    """A dev shell's PYTHONPATH must not be reported as the tree the monitors load."""
    interp = tmp_path / "fake-python"
    interp.write_text("#!/bin/sh\necho \"pythonpath=${PYTHONPATH:-unset}\"\n"); interp.chmod(0o755)
    shim = tmp_path / "swarph"; shim.write_text(f"#!{interp}\n"); shim.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", "/dev/shell/src")
    interp_out, init_path, how = monitor._resolve_consumed_tree(str(shim))
    assert init_path == "pythonpath=unset", (init_path, how)


# ── #807 follow-up (2026-09-15): root cannot see a pip --user tree ────────────────

@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807b_user_site_tree_renders_the_pythonpath_root_needs(tmp_path, capsys, monkeypatch):
    """The unit runs as root; a pip --user tree under ~owner/.local is invisible to
    root's interpreter. Measured 2026-09-15: ExecCondition failed on EVERY fire and the
    unit was SKIPPED silently. The render hands the site to the unit explicitly."""
    shim = _fake_shim(tmp_path, "/home/ubuntu/.local/lib/python3.14/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Environment=PYTHONPATH=/home/ubuntu/.local/lib/python3.14/site-packages\n" in out
    assert "<ENV_PYTHONPATH>" not in out


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807b_a_venv_tree_renders_no_pythonpath_line(tmp_path, capsys, monkeypatch):
    shim = _fake_shim(tmp_path, "/home/u/.local/share/pipx/venvs/swarph-cli/lib/python3.14/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0 and "\nEnvironment=PYTHONPATH" not in out and "<ENV_PYTHONPATH>" not in out


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807b_write_refuses_a_condition_that_fails_for_the_writing_user(tmp_path, capsys, monkeypatch):
    """lab-ovh's discriminator as a gate: if `<interp> -c 'import swarph_cli'` fails NOW
    for whoever runs --write (root), the unit would be skipped on every fire with no
    OnFailure — so nothing is written and the reason names the command."""
    shim = _fake_shim(tmp_path, "/home/ubuntu/.local/lib/python3.14/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe",
                        lambda i, p=None: (False, "rc=1: ModuleNotFoundError: No module named 'swarph_cli'"))
    target = tmp_path / "units"; target.mkdir()
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim), "--write", "--dir", str(target)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSED" in err and "import swarph_cli" in err and "ModuleNotFoundError" in err
    assert list(target.iterdir()) == [], "nothing may be written when the condition fails"


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807b_root_resolves_the_tree_through_the_owners_user_site(tmp_path, capsys, monkeypatch):
    """As root the bare import fails, so the resolver finds the shim owner's user site by
    construction and asks again with PYTHONPATH — the tree the monitors load."""
    site = tmp_path / "site"; (site / "swarph_cli").mkdir(parents=True)
    (site / "swarph_cli" / "__init__.py").write_text("")
    interp = tmp_path / "fake-python"
    interp.write_text('#!/bin/sh\n[ -n "$PYTHONPATH" ] || exit 1\necho "$PYTHONPATH/swarph_cli/__init__.py"\n')
    interp.chmod(0o755)
    shim = tmp_path / "swarph"; shim.write_text(f"#!{interp}\n"); shim.chmod(0o755)
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_owner_user_site", lambda b, i: str(site))
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"PathChanged={site}/swarph_cli/__init__.py" in out
    assert "via the shim owner's user site" in out


def test_807b_user_site_regex_matches_only_user_sites():
    assert monitor._user_site_of("/home/ubuntu/.local/lib/python3.14/site-packages/swarph_cli/__init__.py") \
        == "/home/ubuntu/.local/lib/python3.14/site-packages"
    assert monitor._user_site_of("/home/u/.local/share/pipx/venvs/x/lib/python3.14/site-packages/swarph_cli/__init__.py") is None
    assert monitor._user_site_of("/usr/lib/python3/dist-packages/swarph_cli/__init__.py") is None


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807c_rendered_units_carry_their_producer(tmp_path, capsys, monkeypatch):
    """A unit on disk must say what produced it: package version, interpreter, UTC time.
    Without it a render from an unreleased checkout is indistinguishable from a release."""
    import swarph_cli
    shim = _fake_shim(tmp_path, "/home/ubuntu/.local/lib/python3.14/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    pkg = str(swarph_cli.__file__).replace("\\", "/")
    assert out.count(f"# rendered-by: swarph-cli {swarph_cli.__version__} ({pkg}) install-reexec, ") == 2
    assert "<RENDERED_BY>" not in out


# ── #807 third defect (droplet, 2026-09-15): watch what the RESIDENTS load ──────────

@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807d_droplet_shape_residents_system_tree_is_watched_and_the_shim_tree_is_labelled(tmp_path, capsys, monkeypatch):
    """droplet: the shim is pipx, the three residents run /usr/bin/python3.10 on the SYSTEM
    tree. Before: only the pipx tree was watched — a system-only upgrade never fired.
    Now: one PathChanged per distinct tree the residents load, each labelled, and the
    shim's tree is labelled as loaded by NO resident rather than asserted either way."""
    shim = _fake_shim(tmp_path, "/root/.local/pipx/venvs/swarph-cli/lib/python3.10/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {
        "/usr/local/lib/python3.10/dist-packages/swarph_cli/__init__.py": {
            "cells": ["cell-x", "cell-y", "cell-z"], "interpreter": "/usr/bin/python3.10"},
        "_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PathChanged=/usr/local/lib/python3.10/dist-packages/swarph_cli/__init__.py" in out
    assert "PathChanged=/root/.local/pipx/venvs/swarph-cli/lib/python3.10/site-packages/swarph_cli/__init__.py" in out
    assert "loaded by: cell-x, cell-y, cell-z" in out
    assert "NO resident loads it; the shim's tree (ExecStart runs it)" in out
    assert "the monitors do not load it" not in out


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807d_the_installers_own_import_is_never_a_watch_source(tmp_path, capsys, monkeypatch):
    """R2's rule, now on BOTH paths: nothing the installer's interpreter imports enters
    the watched set unless a resident loads it."""
    import swarph_cli
    shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    mine = str(swarph_cli.__file__).replace("\\", "/")
    assert f"PathChanged={mine}" not in out
    import re as _re
    assert len(_re.findall(r"^PathChanged=", out, _re.M)) == 1


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807d_unreadable_residents_are_named_not_skipped(tmp_path, capsys, monkeypatch):
    shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {
        "_unreadable": [("cell-q", "pid 4242: /proc/4242/exe unreadable (PermissionError) — run as root to read it")]})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0 and "resident NOT inspected: cell-q" in out and "run as root" in out


def test_807d_resident_trees_reads_exe_and_environ_of_each_live_resident(tmp_path, monkeypatch):
    """Real mechanics with fake processes: the pid's /proc exe is the interpreter, its
    PYTHONPATH is honoured, and cells sharing a tree are grouped."""
    interp_a = tmp_path / "py-a"; interp_a.write_text('#!/bin/sh\necho "/tree-a/swarph_cli/__init__.py"\n'); interp_a.chmod(0o755)
    interp_b = tmp_path / "py-b"; interp_b.write_text('#!/bin/sh\n[ "$PYTHONPATH" = "/pp" ] || exit 1\necho "/tree-b/swarph_cli/__init__.py"\n'); interp_b.chmod(0o755)
    units = [("cell-a", "u-a.service"), ("cell-b", "u-b.service"), ("cell-c", "u-c.service"), ("cell-d", "u-d.service")]
    monkeypatch.setattr(monitor, "_supervised_monitor_units", lambda: units)
    pids = {"cell-a": 101, "cell-b": 102, "cell-c": 103, "cell-d": 104}
    monkeypatch.setattr(monitor.mesh, "pidfile_status",
                        lambda p: ("live_ours", {"pid": pids[p.parts[-3]]}) if p.parts[-3] != "cell-d" else ("stale", {"pid": 104}))
    exes = {101: str(interp_a), 102: str(interp_b), 103: str(interp_a)}
    monkeypatch.setattr(monitor.os, "readlink", lambda p: exes[int(p.split("/")[2])])
    monkeypatch.setattr(monitor, "_proc_pythonpath", lambda pid: "/pp" if pid == 102 else None)
    trees = monitor._resident_trees(tmp_path / "state")
    if sys.platform.startswith("win"):
        # The fake interpreters are sh scripts, which Windows cannot execute. The
        # verb must ABSTAIN and name each resident it could not ask -- never return
        # silence, which a reader cannot tell from "no resident loads a tree"
        # (lab-ovh review on PR 422). Same shape as pack_stale_resident's
        # CANDIDATE-UNKNOWABLE: a tree that could not be interrogated is not a tree
        # that does not exist.
        assert trees.pop("_unreadable") == [
            ("cell-a", f"pid 101: {interp_a} could not be asked (OSError)"),
            ("cell-b", f"pid 102: {interp_b} could not be asked (OSError)"),
            ("cell-c", f"pid 103: {interp_a} could not be asked (OSError)"),
        ]
        assert trees == {}
        return
    assert trees.pop("_unreadable") == []
    assert trees == {
        "/tree-a/swarph_cli/__init__.py": {"cells": ["cell-a", "cell-c"], "interpreter": str(interp_a)},
        "/tree-b/swarph_cli/__init__.py": {"cells": ["cell-b"], "interpreter": str(interp_b)},
    }


def test_807d_no_shim_and_no_readable_resident_refuses_loudly(tmp_path, capsys, monkeypatch):
    """The old fallback — watch whatever the INSTALLER imports — is gone: a render with
    no attested tree names both failures instead of guessing (R2, both paths)."""
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": [("cell-q", "pid 7: unreadable")]})
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(tmp_path / "no-such-shim")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no-such-shim" in err and "no resident" in err


@pytest.mark.skipif(os.name == "nt", reason="shebang resolution is POSIX")
def test_807e_the_condition_tests_the_shim_the_command_runs_before_the_module(tmp_path, capsys, monkeypatch):
    """Fourth defect (2026-09-15, first real install after the repair): pip rewrote
    site-packages before the console script, the import condition passed, and
    ExecStart 203/EXEC'd on the missing shim — one spurious page per upgrade. Both
    artifacts are now conditions, shim first, and systemd requires all to pass."""
    shim = _fake_shim(tmp_path, "/consumed/site-packages/swarph_cli/__init__.py")
    monkeypatch.setattr(monitor, "_resident_trees", lambda root: {"_unreadable": []})
    monkeypatch.setattr(monitor, "_condition_probe", lambda i, p=None: (True, "OK"))
    rc = monitor.run_monitor(["install-reexec", "--swarph-bin", str(shim)])
    out = capsys.readouterr().out
    assert rc == 0
    conds = [l for l in out.splitlines() if l.startswith("ExecCondition=")]
    assert conds == [f"ExecCondition=/usr/bin/test -x {shim}",
                     f'ExecCondition={tmp_path / "fake-python"} -c "import swarph_cli"'], conds
    start = [l for l in out.splitlines() if l.startswith("ExecStart=")]
    assert start and start[0].startswith(f"ExecStart={shim} "), "the condition must test the artifact ExecStart runs"
