"""Tests for board card #482: the silent-wake hook bundle.

Covers both renderings (arm-instruction for claude/codex, verify-and-report
for cursor), the loud-refusal branch, and non-vacuity partners that prove
the assertions can actually fail.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Iterator

import pytest

from swarph_cli.commands import install_wake_hook as iwh
from swarph_cli.commands import wake_hook_output as who
from swarph_cli.scripts import dm_notify_filter as dmf


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive)
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):])
    yield tmp_path


# ---------------------------------------------------------------------------
# Installer: config shapes per harness
# ---------------------------------------------------------------------------


def test_cursor_entry_shape_matches_hooks_json_schema():
    entry = iwh._new_entry("cursor")
    assert set(entry) == {"command"}
    assert "wake-hook-output" in entry["command"]
    assert "--harness cursor" in entry["command"]


def test_claude_entry_shape_matches_settings_schema():
    entry = iwh._new_entry("claude")
    assert entry["matcher"] == ""
    hook = entry["hooks"][0]
    assert hook["type"] == "command"
    assert "--harness claude" in hook["command"]


def test_install_cursor_writes_sessionStart_list(isolated_home):
    config, changed = iwh._install({}, "cursor")
    assert changed is True
    entries = config["sessionStart"]
    assert len(entries) == 1
    assert iwh._is_owned_entry(entries[0])


def test_install_claude_writes_hooks_sessionstart(isolated_home):
    config, changed = iwh._install({}, "claude")
    assert changed is True
    entries = config["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert iwh._is_owned_entry(entries[0])


def test_install_is_idempotent(isolated_home):
    config, _ = iwh._install({}, "cursor")
    config2, changed2 = iwh._install(config, "cursor")
    assert changed2 is False
    assert config2 == config


def test_install_preserves_foreign_entries(isolated_home):
    foreign = {"command": "./operator-owned.sh"}
    config, changed = iwh._install({"sessionStart": [foreign]}, "cursor")
    assert changed is True
    assert config["sessionStart"][0] == foreign
    assert len(config["sessionStart"]) == 2


def test_uninstall_removes_only_owned(isolated_home):
    foreign = {"command": "./operator-owned.sh"}
    config, _ = iwh._install({"sessionStart": [foreign]}, "cursor")
    config, changed = iwh._uninstall(config, "cursor")
    assert changed is True
    assert config["sessionStart"] == [foreign]


def test_uninstall_without_owned_entry_is_noop(isolated_home):
    config, changed = iwh._uninstall({"sessionStart": [{"command": "x"}]}, "cursor")
    assert changed is False


# ---------------------------------------------------------------------------
# Installer: loud refusal
# ---------------------------------------------------------------------------


def test_unknown_harness_refuses_loudly_and_writes_nothing(isolated_home, capsys):
    rc = iwh.run_install_wake_hook(["--harness", "vim"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LOUD REFUSAL" in err
    assert "vim" in err
    assert not (isolated_home / ".cursor" / "hooks.json").exists()
    assert not (isolated_home / ".claude" / "settings.json").exists()


def test_undetectable_harness_refuses_loudly(isolated_home, monkeypatch, capsys):
    for var in ("CURSOR_DATA_DIR", "CURSOR_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDECODE", "CODEX_CI", "CODEX_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    # --dry-run keeps argv non-empty so the piped-invocation usage guard
    # (not-a-TTY stdin in pytest) doesn't short-circuit before detection.
    rc = iwh.run_install_wake_hook(["--dry-run"])
    assert rc == 2
    assert "LOUD REFUSAL" in capsys.readouterr().err


def test_refusal_branch_is_not_vacuous(isolated_home, capsys):
    """Partner: a KNOWN harness must NOT hit the refusal path."""
    rc = iwh.run_install_wake_hook(["--harness", "cursor", "--dry-run"])
    assert rc == 0
    assert "LOUD REFUSAL" not in capsys.readouterr().err


def test_detection_prefers_cursor_env(isolated_home, monkeypatch):
    monkeypatch.setenv("CURSOR_DATA_DIR", "/tmp/x")
    assert iwh._detect_harness() == "cursor"


# ---------------------------------------------------------------------------
# Callback: both renderings
# ---------------------------------------------------------------------------


def _run_output(argv, monkeypatch, cell_name=None, monitor_json=None, monitor_rc=0):
    monkeypatch.setattr(
        who, "_discover_cell_name", lambda explicit=None: cell_name
    )
    if monitor_json is not None or monitor_rc != 0:

        class _Proc:
            returncode = monitor_rc
            stdout = json.dumps(monitor_json) if monitor_json is not None else ""
            stderr = "error" if monitor_rc == 2 else ""

        monkeypatch.setattr(
            who.subprocess, "run", lambda *a, **k: _Proc()
        )
    return who.run_wake_hook_output(argv)


def test_arm_rendering_claude_shape_and_content(monkeypatch, capsys):
    rc = _run_output(["--harness", "claude"], monkeypatch, cell_name="cursor-lin")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "tail -n 0 -F" in ctx
    assert "dm_notify_filter" in ctx
    assert "cursor-lin" in ctx
    assert "-u" in ctx  # unbuffered is load-bearing; keep it visible
    # grok probe (2026-08-18): overlapping wake sources abort in-flight
    # turns. The instruction must tell the agent to arm exactly one.
    assert "EXACTLY ONE WAKE SOURCE" in ctx


def test_arm_rendering_codex_uses_same_shape(monkeypatch, capsys):
    rc = _run_output(["--harness", "codex"], monkeypatch, cell_name="cursor-lin")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "hookSpecificOutput" in out


def test_arm_rendering_without_cell_still_instructs(monkeypatch, capsys):
    rc = _run_output(["--harness", "claude"], monkeypatch, cell_name=None)
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "tail -n 0 -F" in ctx
    assert "<cell>" in ctx


def test_verify_rendering_armed_when_push_sink(monkeypatch, capsys):
    status = {
        "sinks": [
            {"name": "tmux:cursor-lin", "is_push": True},
            {"name": "pull", "is_push": False},
        ]
    }
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json=status,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "additional_context" in out  # cursor shape, NOT hookSpecificOutput
    assert "hookSpecificOutput" not in out
    assert "ARMED" in out["additional_context"]
    assert "tmux:cursor-lin" in out["additional_context"]


def test_verify_rendering_not_armed_when_no_push_sink(monkeypatch, capsys):
    status = {"running": True,
              "sinks": [{"name": "pull", "is_push": False}],
              "configured_sinks": ["pull"]}
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json=status,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "WAKE NOT ARMED" in ctx
    assert "pull" in ctx


def test_verify_rendering_not_armed_when_monitor_down(monkeypatch, capsys):
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json={"running": False}, monitor_rc=2,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "WAKE NOT ARMED" in ctx


def test_verify_rendering_rc1_means_pending_not_down(monkeypatch, capsys):
    """`monitor status` exits 1 when DMs are PENDING — that is not a
    monitor-down signal. Regression guard: the first live fire of this
    hook misread rc=1 as 'not running' and reported WAKE NOT ARMED in
    front of a healthy monitor."""
    status = {"running": True,
              "sinks": [{"name": "tmux:cursor-lin", "is_push": True}]}
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json=status, monitor_rc=1,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "ARMED" in ctx
    assert "WAKE NOT ARMED" not in ctx


def test_verify_rendering_cannot_verify_on_garbage_output(monkeypatch, capsys):
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_rc=2,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "CANNOT VERIFY" in ctx


def test_verify_rendering_is_not_vacuous(monkeypatch, capsys):
    """Partner: the ARMED verdict must require an actual push sink."""
    status = {"running": True,
              "sinks": [{"name": "tmux:cursor-lin", "is_push": True}]}
    _run_output(["--harness", "cursor"], monkeypatch,
                cell_name="cursor-lin", monitor_json=status)
    armed_ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "WAKE NOT ARMED" not in armed_ctx


def test_unknown_harness_output_is_loud_refusal(monkeypatch, capsys):
    rc = _run_output(["--harness", "ed"], monkeypatch)
    assert rc == 0  # never block session start, even while refusing
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "UNSUPPORTED HARNESS" in ctx
    assert "ed" in ctx


# ---------------------------------------------------------------------------
# Bundled filter script (ws-lc's portable pattern)
# ---------------------------------------------------------------------------


def _feed(lines, idle_seconds=3600):
    stdin = io.StringIO("".join(lines))
    stdout = io.StringIO()
    rc = dmf.run_filter(stdin, stdout, idle_seconds=idle_seconds)
    return rc, stdout.getvalue()


def test_filter_prints_one_line_per_real_dm():
    rc, out = _feed([
        json.dumps({"dm": {"id": 1, "from_node": "lab-ovh", "kind": "fyi",
                           "content": "hello"}}) + "\n",
    ])
    assert rc == 1  # StringIO hits EOF after the lines
    assert "[MESH DM] id=1 from=lab-ovh kind=fyi | hello" in out


def test_filter_drops_receipts_and_noise():
    rc, out = _feed([
        json.dumps({"dm": {"id": 2, "from_node": "lab-ovh", "kind": "answer",
                           "content": "receipt: got it"}}) + "\n",
        "not json at all\n",
        json.dumps({"unrelated": True}) + "\n",
    ])
    assert "[MESH DM]" not in out
    assert "EOF" in out  # stream end is always announced


def test_filter_eof_exits_nonzero_and_loud():
    rc, out = _feed([])
    assert rc == 1
    assert "DEAF" in out


def test_filter_dm_branch_is_not_vacuous():
    """Partner: a real DM must produce output a receipt does not."""
    _, dm_out = _feed([
        json.dumps({"dm": {"id": 3, "from_node": "a", "kind": "fyi",
                           "content": "real"}}) + "\n",
    ])
    _, receipt_out = _feed([
        json.dumps({"dm": {"id": 3, "from_node": "a", "kind": "fyi",
                           "content": "receipt: real"}}) + "\n",
    ])
    assert "[MESH DM]" in dm_out
    assert "[MESH DM]" not in receipt_out
