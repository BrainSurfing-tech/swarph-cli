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
    # The installer's --cell/session disagreement warning probes the tmux
    # session — strip the ambient one so tests are hermetic on a box where
    # pytest itself runs inside tmux.
    monkeypatch.delenv("TMUX", raising=False)
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


def test_antigravity_entry_shape_matches_hooks_schema(isolated_home):
    entry = iwh._new_entry("antigravity")
    assert entry["type"] == "command"
    assert "--harness antigravity" in entry["command"]
    assert iwh._config_path("antigravity", "user") == isolated_home / ".gemini" / "config" / "hooks.json"


def test_install_antigravity_writes_swarph_wake_hook_sessionstart(isolated_home):
    config, changed = iwh._install({}, "antigravity")
    assert changed is True
    entries = config["swarph-wake-hook"]["SessionStart"]
    assert len(entries) == 1
    assert iwh._is_owned_entry(entries[0])


def test_install_antigravity_preserves_preexisting_named_hooks(isolated_home):
    existing = {
        "retry-agent-execution-error": {
            "Stop": [
                {
                    "command": "./hooks/retry-agent-execution-error.py",
                    "timeout": 45,
                    "type": "command"
                }
            ]
        }
    }
    config, changed = iwh._install(existing, "antigravity")
    assert changed is True
    assert "retry-agent-execution-error" in config
    assert "swarph-wake-hook" in config
    assert config["retry-agent-execution-error"]["Stop"][0]["command"] == "./hooks/retry-agent-execution-error.py"
    assert len(config["swarph-wake-hook"]["SessionStart"]) == 1




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
    for var in (
        "CURSOR_DATA_DIR", "CURSOR_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDECODE", "CODEX_CI", "CODEX_SANDBOX", "MUSE_CODE", "MUSE_SESSION_ID", "MUSE",
        "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CLI", "ANTIGRAVITY_WORKSPACE", "GEMINI_CLI_SESSION",
    ):
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


def _run_output(argv, monkeypatch, cell_name=None, monitor_json=None,
                monitor_rc=0, cell_source="install-time --cell"):
    monkeypatch.setattr(
        who, "_resolve_cell",
        lambda explicit=None: (cell_name, cell_source if cell_name else "unresolved"),
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


def test_arm_rendering_without_cell_refuses_loudly(monkeypatch, capsys):
    """Card #527 task 1: CANNOT-RESOLVE is a loud refusal, never a
    placeholder. The old rendering said "substitute <cell> yourself" —
    on a shared box the session's guess is the box-wide $SWARPH_SELF,
    i.e. the box owner, i.e. the wrong-cell hazard armed by the
    session's own hand."""
    rc = _run_output(["--harness", "claude"], monkeypatch, cell_name=None)
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "CANNOT RESOLVE" in ctx
    assert "DO NOT arm a DM watch for a guessed cell" in ctx
    assert "substitute" not in ctx


def test_verify_rendering_armed_when_push_sink(monkeypatch, capsys):
    status = {
        "running": True,
        "sinks": [
            {"name": "tmux:cursor-lin", "is_push": True},
            {"name": "pull", "is_push": False},
        ],
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
    out = json.loads(capsys.readouterr().out)
    # Finding 1 (PR #254 review): the refusal goes out in BOTH known
    # envelope shapes — an unknown harness reads the key it knows.
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["additional_context"] == ctx
    assert "UNSUPPORTED HARNESS" in ctx
    assert "ed" in ctx


def test_verify_missing_running_field_is_cannot_verify(monkeypatch, capsys):
    """Finding 2: schema drift must not report a specific wrong diagnosis."""
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json={"sinks": []},
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "CANNOT VERIFY" in ctx
    assert "running" in ctx
    assert "WAKE NOT ARMED" not in ctx


def test_verify_missing_is_push_field_is_cannot_verify(monkeypatch, capsys):
    status = {"running": True, "sinks": [{"name": "tmux:cursor-lin"}]}
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="cursor-lin", monitor_json=status,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "CANNOT VERIFY" in ctx
    assert "is_push" in ctx


def test_verify_env_fallback_names_shared_box_risk(monkeypatch, capsys):
    """Finding 3: a $SWARPH_SELF-resolved identity must carry its
    provenance — on a shared box that name is the box owner's cell."""
    status = {"running": True,
              "sinks": [{"name": "tmux:lab-ovh", "is_push": True}]}
    rc = _run_output(
        ["--harness", "cursor"], monkeypatch,
        cell_name="lab-ovh", cell_source="$SWARPH_SELF",
        monitor_json=status,
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "ARMED" in ctx
    assert "$SWARPH_SELF" in ctx
    assert "box owner" in ctx


# ---------------------------------------------------------------------------
# Card #527 task 1: runtime cell resolution (most-local first)
# ---------------------------------------------------------------------------


class _TmuxProc:
    def __init__(self, session: str, rc: int = 0):
        self.returncode = rc
        self.stdout = (session + "\n") if session else ""
        self.stderr = ""


@pytest.fixture
def resolution_env(tmp_path, monkeypatch):
    """Isolate the REAL _resolve_cell from the ambient box: no TMUX, no
    SWARPH_SELF, no cwd discovery, cells_dir redirected to tmp, cwd in a
    directory whose basename matches no cell."""
    from types import SimpleNamespace

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.setattr(who, "discover_cell_in_cwd", lambda: None)
    cells = tmp_path / "cells"
    cells.mkdir()
    monkeypatch.setattr(who, "cells_dir", lambda: cells)
    work = tmp_path / "not-a-cell"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        who, "load_cell",
        lambda path: SimpleNamespace(name=path.stem, role=None),
    )

    def in_tmux(session: str, rc: int = 0):
        monkeypatch.setenv("TMUX", f"/tmp/tmux-fake/default,1,0")
        monkeypatch.setattr(
            who.subprocess, "run",
            lambda *a, **k: _TmuxProc(session, rc),
        )

    return cells, in_tmux


def test_tmux_session_resolves_to_cell(resolution_env):
    cells, in_tmux = resolution_env
    (cells / "cursor-lin.yaml").write_text("", encoding="utf-8")
    in_tmux("cursor-lin")
    name, source = who._resolve_cell()
    assert name == "cursor-lin"
    assert source == "tmux session 'cursor-lin'"


def test_tmux_outranks_a_baked_cell(resolution_env):
    """THE defect repair: a box-global baked --cell must not lie to a
    session whose tmux name says it is a different cell."""
    cells, in_tmux = resolution_env
    (cells / "drop-on-meta-edge.yaml").write_text("", encoding="utf-8")
    in_tmux("drop-on-meta-edge")
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert name == "drop-on-meta-edge"
    assert "tmux" in source


def test_unknown_tmux_session_falls_through_never_invents(resolution_env):
    """A tmux session named 'scratch' must not invent a cell named
    'scratch' — fall through to the next source."""
    cells, in_tmux = resolution_env
    in_tmux("scratch")
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert (name, source) == ("gpt-ops", "install-time --cell")


def test_no_tmux_env_falls_through(resolution_env):
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert (name, source) == ("gpt-ops", "install-time --cell")


def test_tmux_command_failure_falls_through(resolution_env):
    cells, in_tmux = resolution_env
    (cells / "cursor-lin.yaml").write_text("", encoding="utf-8")
    in_tmux("cursor-lin", rc=1)
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert (name, source) == ("gpt-ops", "install-time --cell")


def test_tmux_exception_falls_through(resolution_env, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-fake/default,1,0")

    def boom(*a, **k):
        raise FileNotFoundError("tmux not installed")

    monkeypatch.setattr(who.subprocess, "run", boom)
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert (name, source) == ("gpt-ops", "install-time --cell")


def test_env_self_still_resolves_with_provenance(resolution_env, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    name, source = who._resolve_cell()
    assert (name, source) == ("lab-ovh", "$SWARPH_SELF")


def test_nothing_resolves_is_unresolved(resolution_env):
    assert who._resolve_cell() == (None, "unresolved")


def test_tmux_overriding_a_baked_cell_is_named_not_swallowed(resolution_env):
    """lab-ovh on #527: when tmux and --cell disagree, resolving to tmux
    must REPORT the override — an ignored --cell is the same defect shape
    as an ignored filter returning an unfiltered superset that looks
    filtered."""
    cells, in_tmux = resolution_env
    (cells / "cursor-lin.yaml").write_text("", encoding="utf-8")
    in_tmux("cursor-lin")
    name, source = who._resolve_cell(explicit="gpt-ops")
    assert name == "cursor-lin"
    assert "overrode install-time --cell 'gpt-ops'" in source


def test_tmux_agreeing_with_baked_cell_reports_no_override(resolution_env):
    """Non-vacuity partner: agreement must NOT manufacture an override
    note."""
    cells, in_tmux = resolution_env
    (cells / "cursor-lin.yaml").write_text("", encoding="utf-8")
    in_tmux("cursor-lin")
    name, source = who._resolve_cell(explicit="cursor-lin")
    assert name == "cursor-lin"
    assert "overrode" not in source


def test_override_note_reaches_the_arm_envelope(monkeypatch, capsys):
    rc = _run_output(
        ["--harness", "claude"], monkeypatch, cell_name="cursor-lin",
        cell_source="tmux session 'cursor-lin' (overrode install-time --cell 'gpt-ops')",
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "overrode install-time --cell 'gpt-ops'" in ctx


def test_arm_instruction_names_tmux_provenance(monkeypatch, capsys):
    rc = _run_output(["--harness", "claude"], monkeypatch,
                     cell_name="cursor-lin",
                     cell_source="tmux session 'cursor-lin'")
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "identity from tmux session 'cursor-lin'" in ctx


def test_arm_instruction_names_env_provenance_as_shared_box_risk(monkeypatch, capsys):
    """The arm path gets the same F3 warning the verify path has: an ARM
    instruction for the wrong cell is as hazardous as a wrong verdict."""
    rc = _run_output(["--harness", "claude"], monkeypatch,
                     cell_name="lab-ovh", cell_source="$SWARPH_SELF")
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "$SWARPH_SELF" in ctx
    assert "box owner" in ctx


def test_verify_refusal_names_every_failed_source(monkeypatch, capsys):
    rc = _run_output(["--harness", "cursor"], monkeypatch, cell_name=None)
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert "CANNOT VERIFY" in ctx
    for source in ("tmux", "--cell", "$SWARPH_SELF", "cwd"):
        assert source in ctx


# ---------------------------------------------------------------------------
# Card #527 task 2: refuse --cell + a box-global target
# ---------------------------------------------------------------------------


def test_cell_with_user_scope_refuses_and_writes_nothing(isolated_home, capsys):
    rc = iwh.run_install_wake_hook(["--harness", "claude", "--cell", "gpt-ops"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LOUD REFUSAL" in err
    assert "--scope project" in err
    assert "gpt-ops" not in err.split("LOUD REFUSAL")[0]  # refusal, not install
    assert not (isolated_home / ".claude" / "settings.json").exists()


def test_cell_with_project_scope_installs(isolated_home, monkeypatch, capsys):
    """Non-vacuity partner: the refusal must not swallow the VALID
    per-cell combination."""
    proj = isolated_home / "gpt-ops"
    proj.mkdir()
    monkeypatch.chdir(proj)
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "gpt-ops", "--scope", "project"]
    )
    assert rc == 0
    settings = json.loads(
        (proj / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--cell gpt-ops" in cmd


def test_project_scope_from_home_is_the_same_box_global_file(
    isolated_home, monkeypatch, capsys
):
    """The guard compares PATHS, not flags: --scope project run from the
    box's home directory lands on ~/.claude/settings.json — the same
    box-global file — and must be refused with --cell. cursor-lin's own
    cell.yaml cwd IS the box home; following the guide's advice literally
    would have made that cell the fourth evictor in the same slot."""
    monkeypatch.chdir(isolated_home)
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "cursor-lin", "--scope", "project"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "LOUD REFUSAL" in err
    assert "NOT the box's home" in err
    assert not (isolated_home / ".claude" / "settings.json").exists()


def test_user_scope_without_cell_installs(isolated_home, capsys):
    """Non-vacuity partner: the combination runtime resolution makes
    correct — box-global file, no baked name."""
    rc = iwh.run_install_wake_hook(["--harness", "claude"])
    assert rc == 0
    settings = json.loads(
        (isolated_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--cell" not in cmd


def test_uninstall_is_exempt_from_the_pair_guard(isolated_home, capsys):
    iwh.run_install_wake_hook(["--harness", "claude"])
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "gpt-ops", "--uninstall"]
    )
    assert rc == 0
    settings = json.loads(
        (isolated_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["hooks"]["SessionStart"] == []


# ---------------------------------------------------------------------------
# Card #527 review: install-time warning when the installer's own session
# disagrees with the baked --cell
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(isolated_home, monkeypatch):
    """A project directory that is NOT the box home, so the box-global
    path guard does not fire."""
    proj = isolated_home / "some-project"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def test_baked_cell_disagreeing_with_own_session_warns(
    project_dir, monkeypatch, capsys
):
    monkeypatch.setattr(
        iwh, "_tmux_session_cell",
        lambda: ("cursor-lin", "tmux session 'cursor-lin'"),
    )
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "gpt-ops", "--scope", "project"]
    )
    assert rc == 0  # warning, not refusal — the install proceeds
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "cursor-lin" in err and "gpt-ops" in err
    assert "outranks the baked name" in err


def test_baked_cell_agreeing_with_own_session_is_quiet(
    project_dir, monkeypatch, capsys
):
    """Non-vacuity partner: agreement must not warn."""
    monkeypatch.setattr(
        iwh, "_tmux_session_cell",
        lambda: ("gpt-ops", "tmux session 'gpt-ops'"),
    )
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "gpt-ops", "--scope", "project"]
    )
    assert rc == 0
    assert "WARNING" not in capsys.readouterr().err


def test_unresolvable_own_session_is_quiet(project_dir, monkeypatch, capsys):
    """Non-vacuity partner: an installer outside tmux (or in an unknown
    session) has no disagreement to report."""
    monkeypatch.setattr(iwh, "_tmux_session_cell", lambda: None)
    rc = iwh.run_install_wake_hook(
        ["--harness", "claude", "--cell", "gpt-ops", "--scope", "project"]
    )
    assert rc == 0
    assert "WARNING" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Card #527 task 3: the installer asserts it wrote
# ---------------------------------------------------------------------------


def test_install_re_reads_what_it_wrote(isolated_home, capsys):
    rc = iwh.run_install_wake_hook(["--harness", "claude"])
    assert rc == 0
    assert "installed at" in capsys.readouterr().err
    landed = (isolated_home / ".claude" / "settings.json").read_text(
        encoding="utf-8"
    )
    assert "wake-hook-output" in landed


def test_swallowed_write_is_a_loud_failure(isolated_home, monkeypatch, capsys):
    """A write that lands but re-reads as different content (an external
    reconciler reverting between write and check) must NOT print a
    success claim."""
    def revert(target, payload):
        target.write_text('{"hooks": {"SessionStart": []}}\n', encoding="utf-8")

    monkeypatch.setattr(iwh, "_atomic_write_text", revert)
    rc = iwh.run_install_wake_hook(["--harness", "claude"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "WRITE DID NOT LAND" in err
    assert "installed at" not in err


def test_write_that_never_landed_is_a_loud_failure(isolated_home, monkeypatch, capsys):
    """The 14:47 specimen's shape: the write step ran, the success line
    would have printed, and the file on disk never changed."""
    monkeypatch.setattr(iwh, "_atomic_write_text", lambda target, payload: None)
    rc = iwh.run_install_wake_hook(["--harness", "claude"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "WRITE DID NOT LAND" in err
    assert "installed at" not in err


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
