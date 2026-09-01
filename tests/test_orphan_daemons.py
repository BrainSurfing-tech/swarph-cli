"""#666 T1/T2 — orphan ``claude daemon run`` detector (read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarph_cli.orphan_daemons import (
    STATE_LIVE,
    STATE_ORPHANED,
    STATE_UNKNOWN,
    classify_daemon,
    format_report,
    is_claude_daemon_cmdline,
    parse_spawned_by,
    scan_orphan_daemons,
    self_related,
)


def test_is_claude_daemon_cmdline_requires_daemon_run():
    assert is_claude_daemon_cmdline(
        "/home/u/.local/bin/claude daemon run --origin transient "
        '--spawned-by {"label":"claude","pid":123}'
    )
    assert not is_claude_daemon_cmdline("bash -c claude something")
    assert not is_claude_daemon_cmdline("claude chat")
    assert not is_claude_daemon_cmdline("claude daemon")  # no `run`


def test_parse_spawned_by_ok_and_missing():
    cmd = (
        'claude daemon run --json-path /x --origin transient '
        '--spawned-by {"label":"claude","cwd":"/home/u","pid":700337}'
    )
    assert parse_spawned_by(cmd) == {
        "label": "claude", "cwd": "/home/u", "pid": 700337,
    }
    assert parse_spawned_by("claude daemon run --origin transient") is None
    assert parse_spawned_by(
        "claude daemon run --spawned-by {not-json}"
    ) is None


def test_classify_live_when_spawner_alive():
    state, _ = classify_daemon(
        spawner_pid=1, spawner_alive=True,
        scope="tmux-spawn-abc.scope", scope_live=False,
    )
    assert state == STATE_LIVE


def test_classify_orphaned_when_spawner_dead_and_scope_dead():
    state, _ = classify_daemon(
        spawner_pid=99, spawner_alive=False,
        scope="tmux-spawn-abc.scope", scope_live=False,
    )
    assert state == STATE_ORPHANED


def test_classify_unknown_never_orphaned_without_spawned_by():
    """T2 accept: synthetic cmdline with no --spawned-by → UNKNOWN, never ORPHANED."""
    state, reason = classify_daemon(
        spawner_pid=None, spawner_alive=None,
        scope="tmux-spawn-abc.scope", scope_live=False,
    )
    assert state == STATE_UNKNOWN
    assert "missing" in reason or "unparseable" in reason


def test_classify_unknown_when_scope_evidence_incomplete():
    state, _ = classify_daemon(
        spawner_pid=99, spawner_alive=False,
        scope=None, scope_live=None,
    )
    assert state == STATE_UNKNOWN


def test_classify_live_when_spawner_dead_but_scope_live():
    state, _ = classify_daemon(
        spawner_pid=99, spawner_alive=False,
        scope="tmux-spawn-abc.scope", scope_live=True,
    )
    assert state == STATE_LIVE


def test_classify_self_exclusion_never_orphaned():
    """A3: caller's own daemon must never be ORPHANED."""
    state, reason = classify_daemon(
        spawner_pid=99, spawner_alive=False,
        scope="tmux-spawn-abc.scope", scope_live=False,
        self_related=True,
    )
    assert state == STATE_LIVE
    assert "self-exclusion" in reason


def _write_proc(root: Path, pid: int, *, cmdline: str, ppid: int = 1,
                cgroup: str = "", rss_kb: int = 100) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\x00").encode() + b"\x00")
    # /proc/<pid>/stat: pid (comm) state ppid ...
    (d / "stat").write_text(f"{pid} (claude) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    (d / "status").write_text(f"Name:\tclaude\nPPid:\t{ppid}\nVmRSS:\t{rss_kb} kB\n")
    if cgroup:
        (d / "cgroup").write_text(cgroup)


def test_scan_explicit_none_when_empty(tmp_path):
    """T1 accept: no daemons → explicit 'none', not silent empty."""
    result = scan_orphan_daemons(
        proc_root=tmp_path,
        caller_pid=1,
        run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert result.daemons == []
    text = format_report(result)
    assert "orphan-daemons: none" in text
    assert "no `claude daemon run`" in text


def test_scan_orphaned_positive(tmp_path):
    """A1 shape: real orphan named with dead spawner pid."""
    scope = "tmux-spawn-deadbeef.scope"
    daemon_cmd = (
        "/home/u/.local/bin/claude daemon run --json-path /x "
        f"--origin transient --spawned-by {json.dumps({'label': 'claude', 'pid': 700337})}"
    )
    _write_proc(
        tmp_path, 5000, cmdline=daemon_cmd, ppid=1,
        cgroup=f"0::/user.slice/{scope}", rss_kb=168000,
    )
    # spawner 700337 deliberately absent → dead
    # a live tmux session exists but under a DIFFERENT scope
    _write_proc(
        tmp_path, 42, cmdline="/bin/bash", ppid=1,
        cgroup="0::/user.slice/tmux-spawn-alive.scope",
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return SimpleNamespace(returncode=0, stdout="lab-ovh\n", stderr="")
        if cmd[:2] == ["tmux", "list-panes"]:
            return SimpleNamespace(returncode=0, stdout="42\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    result = scan_orphan_daemons(
        proc_root=tmp_path, caller_pid=99999, run=fake_run,
    )
    assert len(result.daemons) == 1
    d = result.daemons[0]
    assert d.state == STATE_ORPHANED
    assert d.spawner_pid == 700337
    assert d.spawner_alive is False
    assert d.scope == scope
    assert "orphans: none" not in format_report(result)


def test_scan_live_control_zero_orphans(tmp_path):
    """A2: only LIVE daemons → zero orphans (not 'every daemon is orphan')."""
    scope = "tmux-spawn-alive.scope"
    daemon_cmd = (
        "/home/u/.local/bin/claude daemon run --origin transient "
        f"--spawned-by {json.dumps({'label': 'claude', 'pid': 100})}"
    )
    _write_proc(tmp_path, 100, cmdline="claude", ppid=1,
                cgroup=f"0::/user.slice/{scope}")
    _write_proc(
        tmp_path, 5000, cmdline=daemon_cmd, ppid=100,
        cgroup=f"0::/user.slice/{scope}",
    )
    _write_proc(
        tmp_path, 42, cmdline="/bin/bash", ppid=1,
        cgroup=f"0::/user.slice/{scope}",
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return SimpleNamespace(returncode=0, stdout="lab-ovh\n", stderr="")
        if cmd[:2] == ["tmux", "list-panes"]:
            return SimpleNamespace(returncode=0, stdout="42\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    result = scan_orphan_daemons(
        proc_root=tmp_path, caller_pid=99999, run=fake_run,
    )
    assert len(result.daemons) == 1
    assert result.daemons[0].state == STATE_LIVE
    assert result.orphans == []
    assert "orphans: none" in format_report(result)


def test_scan_unknown_cmdline_without_spawned_by(tmp_path):
    _write_proc(
        tmp_path, 5000,
        cmdline="/home/u/.local/bin/claude daemon run --origin transient",
        ppid=1,
        cgroup="0::/user.slice/tmux-spawn-x.scope",
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    result = scan_orphan_daemons(
        proc_root=tmp_path, caller_pid=99999, run=fake_run,
    )
    assert result.daemons[0].state == STATE_UNKNOWN
    assert result.orphans == []


def test_self_related_via_ancestry(tmp_path):
    # caller 200 ← ppid 5000 (daemon)
    _write_proc(tmp_path, 5000, cmdline="claude daemon run --origin transient", ppid=1)
    _write_proc(tmp_path, 200, cmdline="swarph", ppid=5000)
    assert self_related(5000, 200, proc_root=tmp_path) is True
    assert self_related(5000, 999, proc_root=tmp_path) is False


def test_watchdog_orphan_daemons_flag_dispatches(tmp_path, monkeypatch, capsys):
    from swarph_cli.commands import watchdog

    monkeypatch.setattr(
        "swarph_cli.orphan_daemons.scan_orphan_daemons",
        lambda **kw: scan_orphan_daemons(
            proc_root=tmp_path, caller_pid=1,
            run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
    )
    rc = watchdog.run_watchdog(["--orphan-daemons"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan-daemons: none" in out
