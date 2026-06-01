"""Tests for ``swarph spawn`` (Phase 7 / v0.6.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from swarph_cli.cell import SCHEMA_VERSION_V1, CellError
from swarph_cli.commands.spawn import (
    _build_claude_argv,
    _build_codex_argv,
    _scrubbed_codex_env,
    _split_passthrough,
    run_spawn,
)
from swarph_cli.cell import load_cell


@pytest.fixture
def isolated_xdg(tmp_path, monkeypatch) -> Iterator[Path]:
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    config_root.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    yield tmp_path


@pytest.fixture
def fake_cell_yaml(tmp_path):
    """Drop a valid cell.yaml under tmp_path/cell.yaml and return its path."""
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-ovh",
        "role": "lab-test",
        "cwd": str(tmp_path),
        "provider": "claude",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_split_passthrough_no_separator():
    own, passthrough = _split_passthrough(["lab", "--dry-run"])
    assert own == ["lab", "--dry-run"]
    assert passthrough == []


def test_split_passthrough_with_separator():
    own, passthrough = _split_passthrough(
        ["lab", "--", "--resume", "--profile", "x"]
    )
    assert own == ["lab"]
    assert passthrough == ["--resume", "--profile", "x"]


def test_split_passthrough_separator_at_start():
    own, passthrough = _split_passthrough(["--", "--bare"])
    assert own == []
    assert passthrough == ["--bare"]


def test_build_claude_argv_minimal(fake_cell_yaml):
    cell = load_cell(fake_cell_yaml)
    argv = _build_claude_argv(
        cell, "550e8400-e29b-41d4-a716-446655440000", no_starter=False, passthrough=[]
    )
    assert argv == [
        "claude",
        "--name",
        "lab-test",
        "--session-id",
        "550e8400-e29b-41d4-a716-446655440000",
    ]


def test_build_claude_argv_with_starter(fake_cell_yaml, tmp_path):
    starter = tmp_path / "starter.md"
    starter.write_text("you are lab; act AI-to-AI.")
    payload = yaml.safe_load(fake_cell_yaml.read_text())
    payload["starter_prompt_path"] = "starter.md"
    fake_cell_yaml.write_text(yaml.safe_dump(payload))
    cell = load_cell(fake_cell_yaml)

    argv = _build_claude_argv(
        cell, "550e8400-e29b-41d4-a716-446655440000",
        no_starter=False, passthrough=[],
    )
    assert "--append-system-prompt" in argv
    idx = argv.index("--append-system-prompt")
    assert "AI-to-AI" in argv[idx + 1]


def test_build_claude_argv_no_starter_flag_skips_injection(
    fake_cell_yaml, tmp_path
):
    starter = tmp_path / "starter.md"
    starter.write_text("hello")
    payload = yaml.safe_load(fake_cell_yaml.read_text())
    payload["starter_prompt_path"] = "starter.md"
    fake_cell_yaml.write_text(yaml.safe_dump(payload))
    cell = load_cell(fake_cell_yaml)

    argv = _build_claude_argv(
        cell, "550e8400-e29b-41d4-a716-446655440000",
        no_starter=True, passthrough=[],
    )
    assert "--append-system-prompt" not in argv


def test_build_claude_argv_passthrough_appended_in_order(fake_cell_yaml):
    cell = load_cell(fake_cell_yaml)
    argv = _build_claude_argv(
        cell, "550e8400-e29b-41d4-a716-446655440000",
        no_starter=False, passthrough=["--resume", "--profile", "x"],
    )
    assert argv[-3:] == ["--resume", "--profile", "x"]


def test_build_codex_argv_default_sandbox(tmp_path):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)

    argv = _build_codex_argv(cell, passthrough=[])
    assert argv == [
        "codex",
        "-C",
        str(tmp_path),
        "-s",
        "workspace-write",
        "-a",
        "on-request",
    ]
    assert "--append-system-prompt" not in argv
    assert "--session-id" not in argv
    assert "--resume" not in argv


def test_build_codex_argv_explicit_sandbox_and_passthrough(tmp_path):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "sandbox": "read-only",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)

    argv = _build_codex_argv(cell, passthrough=["--model", "gpt-5"])
    assert argv == [
        "codex",
        "-C",
        str(tmp_path),
        "-s",
        "read-only",
        "-a",
        "on-request",
        "--model",
        "gpt-5",
    ]


def test_build_codex_argv_rejects_unknown_sandbox(tmp_path):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "sandbox": "danger-full-access",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)

    with pytest.raises(CellError, match="sandbox"):
        _build_codex_argv(cell, passthrough=[])


def test_scrubbed_codex_env_drops_openai_billing_keys(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
    ):
        monkeypatch.setenv(key, f"leak-{key}")
    monkeypatch.setenv("KEEP_ME", "ok")

    env = _scrubbed_codex_env()
    assert "KEEP_ME" in env
    assert env["SWARPH_SPAWN"] == "1"
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
    ):
        assert key not in env


# ---------------------------------------------------------------------------
# run_spawn — argparse + dispatch (without exec)
# ---------------------------------------------------------------------------


def test_run_spawn_no_args_prints_usage(isolated_xdg, capsys, monkeypatch):
    monkeypatch.chdir(isolated_xdg)  # no ./cell.yaml here
    rc = run_spawn(argv=[])
    captured = capsys.readouterr()
    assert rc == 0
    assert "swarph spawn" in captured.err.lower()


def test_run_spawn_dry_run_explicit_path(
    isolated_xdg, fake_cell_yaml, capsys
):
    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "dry-run" in captured.err
    assert "lab-test" in captured.err
    assert captured.out.startswith("claude --name lab-test")


def test_run_spawn_dry_run_via_onboarding_flag(
    isolated_xdg, fake_cell_yaml, capsys
):
    rc = run_spawn(argv=["--onboarding", str(fake_cell_yaml), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "lab-test" in captured.err


def test_run_spawn_mesh_gateway_url_returns_not_implemented(
    isolated_xdg, capsys
):
    rc = run_spawn(
        argv=[
            "--onboarding",
            "mesh-gateway://peers/x/spawn-context",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "v0.7" in captured.err


def test_run_spawn_print_id_emits_uuid_to_stdout(
    isolated_xdg, fake_cell_yaml, capsys
):
    rc = run_spawn(
        argv=[str(fake_cell_yaml), "--dry-run", "--print-id"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    # First line of stdout is the UUID, second is the dry-run command.
    first_line = captured.out.splitlines()[0]
    import uuid as _uuid
    _uuid.UUID(first_line)


def test_run_spawn_invalid_cell_returns_1(isolated_xdg, capsys):
    rc = run_spawn(argv=["/no/such/file.yaml"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.err


def test_run_spawn_persists_session_id_across_invocations(
    isolated_xdg, fake_cell_yaml, capsys
):
    """R5 invariant — same role re-spawned reuses the same session-id."""
    run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id"])
    out1 = capsys.readouterr().out.splitlines()[0]
    run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id"])
    out2 = capsys.readouterr().out.splitlines()[0]
    assert out1 == out2


def test_run_spawn_passthrough_args_after_double_dash(
    isolated_xdg, fake_cell_yaml, capsys
):
    rc = run_spawn(
        argv=[str(fake_cell_yaml), "--dry-run", "--", "--resume"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "--resume" in captured.out


def test_run_spawn_codex_dry_run_prints_fresh_session_note(
    isolated_xdg, tmp_path, capsys
):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")

    rc = run_spawn(argv=[str(p), "--dry-run", "--print-id"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.splitlines()[0] == "codex: fresh-session-per-spawn, no pinned id"
    assert captured.out.splitlines()[1].startswith(f"codex -C {tmp_path}")
    assert "-s workspace-write" in captured.out
    assert "-a on-request" in captured.out
    assert "--append-system-prompt" not in captured.out
    assert "session_id:  codex: fresh-session-per-spawn" in captured.err
    assert "cell.yaml session_id ignored" in captured.err


def test_run_spawn_codex_execve_scrubs_env(
    isolated_xdg, tmp_path, monkeypatch
):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "sandbox": "read-only",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")

    captured = {}
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/codex" if name == "codex" else None,
    )

    def fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr("os.execve", fake_execve)
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
    ):
        monkeypatch.setenv(key, f"leak-{key}")

    rc = run_spawn(argv=[str(p), "--no-banner"])
    assert rc == 0
    assert captured["path"] == "/usr/bin/codex"
    assert captured["argv"] == [
        "codex",
        "-C",
        str(tmp_path),
        "-s",
        "read-only",
        "-a",
        "on-request",
    ]
    assert captured["env"]["SWARPH_SPAWN"] == "1"
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
    ):
        assert key not in captured["env"]


def test_run_spawn_codex_missing_binary_returns_127(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: None)

    rc = run_spawn(argv=[str(p), "--no-banner"])
    captured = capsys.readouterr()
    assert rc == 127
    assert "'codex' binary not found" in captured.err


def test_run_spawn_auto_discovers_cwd_cell_yaml(
    isolated_xdg, fake_cell_yaml, monkeypatch, capsys
):
    monkeypatch.chdir(fake_cell_yaml.parent)
    rc = run_spawn(argv=["--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "lab-test" in captured.err


def test_run_spawn_new_instance_no_base_falls_through(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-B — `--new-instance` with NO base sidecar lands in the
    base slot (degenerate-case fallthrough) AND surfaces a stderr note
    explaining the operator should spawn the original first."""
    from swarph_cli.cell import session_state_path

    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id", "--new-instance"])
    captured = capsys.readouterr()
    assert rc == 0

    # Sidecar IS written to base slot (degenerate case fallthrough)
    base_sidecar = session_state_path("lab-test")
    assert base_sidecar.exists()
    # Sibling slot NOT written (no sibling created)
    sibling_sidecar = session_state_path("lab-test-2")
    assert not sibling_sidecar.exists()
    # Stderr surfaces the degenerate-case note
    assert "no existing sidecar" in captured.err
    assert "FIRST instance" in captured.err


def test_run_spawn_new_instance_with_base_mints_sibling_slot(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-B — `--new-instance` with base sidecar present allocates
    slot 2 AND persists. Sibling resumable via `swarph spawn <role>-2`."""
    from swarph_cli.cell import session_state_path

    # First spawn establishes base slot
    run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id"])
    base_uuid = capsys.readouterr().out.splitlines()[0]

    # Second spawn with --new-instance allocates slot 2
    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id", "--new-instance"])
    captured = capsys.readouterr()
    assert rc == 0
    sibling_uuid = captured.out.splitlines()[0]
    assert sibling_uuid != base_uuid

    base_sidecar = session_state_path("lab-test")
    sibling_sidecar = session_state_path("lab-test-2")
    assert base_sidecar.read_text().strip() == base_uuid
    assert sibling_sidecar.read_text().strip() == sibling_uuid

    # Dry-run output shows the sibling slot label
    assert "lab-test-2" in captured.err
    assert "sibling slot" in captured.err
    # claude --name uses slot-suffixed role
    assert "claude --name lab-test-2" in captured.out


def test_run_spawn_new_instance_warns_when_cell_yaml_pins_session_id(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-A — pinned cell.yaml session_id wins over --new-instance;
    surface the conflict as a stderr warning."""
    fixed = "550e8400-e29b-41d4-a716-446655440000"
    payload = yaml.safe_load(fake_cell_yaml.read_text())
    payload["session_id"] = fixed
    fake_cell_yaml.write_text(yaml.safe_dump(payload))

    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id", "--new-instance"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.splitlines()[0] == fixed  # pinned UUID wins
    assert "--new-instance ignored" in captured.err
    assert "cell.yaml pins session_id" in captured.err


def test_run_spawn_resume_sibling_via_slot_role(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-B — `swarph spawn <role>-2` resumes the sibling created
    via prior `--new-instance`, using base cell.yaml for cell-context."""
    from swarph_cli.cell import cells_dir
    # Place cell.yaml under XDG cells dir so role-name resolution works
    base_yaml = cells_dir() / "lab-test.yaml"
    base_yaml.parent.mkdir(parents=True, exist_ok=True)
    base_yaml.write_text(fake_cell_yaml.read_text())

    # 1) Spawn base
    run_spawn(argv=["lab-test", "--dry-run", "--print-id"])
    base_uuid = capsys.readouterr().out.splitlines()[0]
    # 2) Spawn sibling slot 2
    run_spawn(argv=["lab-test", "--dry-run", "--print-id", "--new-instance"])
    sibling_uuid = capsys.readouterr().out.splitlines()[0]
    # 3) Resume sibling via slot-role
    rc = run_spawn(argv=["lab-test-2", "--dry-run", "--print-id"])
    captured = capsys.readouterr()
    assert rc == 0
    resumed_uuid = captured.out.splitlines()[0]
    assert resumed_uuid == sibling_uuid  # same UUID; sidecar resume worked
    assert "claude --name lab-test-2" in captured.out  # display uses slot-role


def test_run_spawn_dry_run_redacts_starter_prompt_in_command(
    isolated_xdg, fake_cell_yaml, capsys
):
    starter = fake_cell_yaml.parent / "starter.md"
    starter.write_text(
        "this is a long starter prompt that should be redacted in dry-run output"
    )
    payload = yaml.safe_load(fake_cell_yaml.read_text())
    payload["starter_prompt_path"] = "starter.md"
    fake_cell_yaml.write_text(yaml.safe_dump(payload))

    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "redacted" not in captured.out  # the literal word from the prompt
    assert "starter prompt>" in captured.out  # the redaction marker


# ---------------------------------------------------------------------------
# v0.7.5 — _session_state_exists + --resume on existing session
# ---------------------------------------------------------------------------
#
# Closes the bug surfaced 2026-05-14 post-reboot: claude --session-id <UUID>
# rejects with "Session ID <UUID> is already in use" when on-disk session
# state exists, even after host reboot (files persist; check is filesystem-
# based not runtime-lock-based). Fix: detect existing state + switch from
# --session-id (create-new semantic) to --resume (attach-existing semantic).


def test_session_state_exists_false_for_fresh_uuid(tmp_path, monkeypatch):
    """No filesystem state for the UUID = fresh; _build_claude_argv uses
    --session-id (create-new semantic)."""
    from swarph_cli.commands.spawn import _session_state_exists

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    fresh_uuid = "00000000-0000-0000-0000-000000000000"
    assert _session_state_exists(fresh_uuid) is False


def test_session_state_exists_true_when_file_history_present(tmp_path, monkeypatch):
    """File-history dir alone is enough to flip detection (any one of the
    three location signals triggers)."""
    from swarph_cli.commands.spawn import _session_state_exists

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    uuid = "a30e406c-8bae-4ea2-8cb2-fb0dff35a6f0"
    (tmp_path / ".claude" / "file-history" / uuid).mkdir(parents=True)
    assert _session_state_exists(uuid) is True


def test_session_state_exists_true_when_session_env_present(tmp_path, monkeypatch):
    from swarph_cli.commands.spawn import _session_state_exists

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    uuid = "a30e406c-8bae-4ea2-8cb2-fb0dff35a6f0"
    (tmp_path / ".claude" / "session-env").mkdir(parents=True)
    (tmp_path / ".claude" / "session-env" / uuid).write_text("")
    assert _session_state_exists(uuid) is True


def test_session_state_exists_true_when_project_jsonl_present(tmp_path, monkeypatch):
    """Projects path varies by project-hash; glob discovers any match."""
    from swarph_cli.commands.spawn import _session_state_exists

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    uuid = "a30e406c-8bae-4ea2-8cb2-fb0dff35a6f0"
    proj = tmp_path / ".claude" / "projects" / "-some-project-hash"
    proj.mkdir(parents=True)
    (proj / f"{uuid}.jsonl").write_text("{}\n")
    assert _session_state_exists(uuid) is True


def test_build_claude_argv_uses_session_id_when_fresh(fake_cell_yaml, tmp_path, monkeypatch):
    """No prior session state → --session-id (create-new) verb."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    cell = load_cell(fake_cell_yaml)
    argv = _build_claude_argv(
        cell=cell,
        session_id="00000000-0000-0000-0000-000000000000",
        no_starter=True,
        passthrough=[],
    )
    assert "--session-id" in argv
    assert "--resume" not in argv


def test_build_claude_argv_uses_resume_when_state_exists(fake_cell_yaml, tmp_path, monkeypatch):
    """Prior session state exists → --resume (attach-existing) verb.

    Closes the v0.7.4 spawn-after-reboot rejection class.
    """
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    uuid = "a30e406c-8bae-4ea2-8cb2-fb0dff35a6f0"
    (tmp_path / ".claude" / "file-history" / uuid).mkdir(parents=True)
    cell = load_cell(fake_cell_yaml)
    argv = _build_claude_argv(
        cell=cell,
        session_id=uuid,
        no_starter=True,
        passthrough=[],
    )
    assert "--resume" in argv
    assert "--session-id" not in argv
    # UUID still passed (just as --resume's value not --session-id's)
    assert uuid in argv


# ---------------------------------------------------------------------------
# Phase 1B v0 — cell.yaml routing field (2026-05-19)
# ---------------------------------------------------------------------------


def test_validate_routing_absent_allows(fake_cell_yaml):
    """No `routing` field → default Anthropic, _validate_routing returns silently."""
    from swarph_cli.commands.spawn import _validate_routing
    cell = load_cell(fake_cell_yaml)
    # fake_cell_yaml has no routing field → should pass
    _validate_routing(cell)  # no exception = pass


def test_validate_routing_explicit_anthropic_allows(tmp_path):
    """`routing.native: anthropic` → allowed (explicit form)."""
    from swarph_cli.commands.spawn import _validate_routing
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-ovh",
        "role": "lab-test",
        "cwd": str(tmp_path),
        "provider": "claude",
        "routing": {"native": "anthropic"},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    _validate_routing(cell)  # no exception = pass


def test_validate_routing_non_anthropic_rejects(tmp_path):
    """`routing.native: openrouter` → rejected for claude provider."""
    from swarph_cli.commands.spawn import _validate_routing
    from swarph_cli.cell import CellError
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-ovh",
        "role": "lab-test",
        "cwd": str(tmp_path),
        "provider": "claude",
        "routing": {"native": "openrouter"},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    with pytest.raises(CellError) as exc_info:
        _validate_routing(cell)
    err = str(exc_info.value)
    assert "openrouter" in err
    assert "claude" in err
    assert "anthropic" in err


def test_validate_routing_non_dict_rejects(tmp_path):
    """`routing: "anthropic"` (string instead of dict) → schema error."""
    from swarph_cli.commands.spawn import _validate_routing
    from swarph_cli.cell import CellError
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-ovh",
        "role": "lab-test",
        "cwd": str(tmp_path),
        "provider": "claude",
        "routing": "anthropic",  # WRONG — should be dict
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    with pytest.raises(CellError) as exc_info:
        _validate_routing(cell)
    assert "mapping" in str(exc_info.value)


def test_validate_routing_omitted_native_allows(tmp_path):
    """`routing: {}` (empty dict, no native key) → defaults to anthropic, allows."""
    from swarph_cli.commands.spawn import _validate_routing
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-ovh",
        "role": "lab-test",
        "cwd": str(tmp_path),
        "provider": "claude",
        "routing": {},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    _validate_routing(cell)  # default anthropic = allowed


def test_run_spawn_rejects_non_anthropic_routing_in_dry_run(
    fake_cell_yaml, isolated_xdg, capsys
):
    """End-to-end: `swarph spawn --dry-run` with non-anthropic routing → exit 1 + error message."""
    payload = yaml.safe_load(fake_cell_yaml.read_text())
    payload["routing"] = {"native": "gemini"}
    fake_cell_yaml.write_text(yaml.safe_dump(payload))
    rc = run_spawn(["--dry-run", str(fake_cell_yaml)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "gemini" in captured.err
    assert "claude" in captured.err
    assert "anthropic" in captured.err


def test_validate_routing_codex_absent_allows(tmp_path):
    from swarph_cli.commands.spawn import _validate_routing
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    _validate_routing(cell)


def test_validate_routing_codex_native_allows(tmp_path):
    from swarph_cli.commands.spawn import _validate_routing
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "routing": {"native": "codex"},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    _validate_routing(cell)


def test_validate_routing_codex_rejects_openai_alias(tmp_path):
    from swarph_cli.commands.spawn import _validate_routing
    from swarph_cli.cell import CellError
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gpt-ops",
        "role": "gpt-ops",
        "cwd": str(tmp_path),
        "provider": "codex",
        "routing": {"native": "openai"},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    with pytest.raises(CellError) as exc_info:
        _validate_routing(cell)
    err = str(exc_info.value)
    assert "openai" in err
    assert "codex" in err


# ---------------------------------------------------------------------------
# Antigravity Spawn Tests
# ---------------------------------------------------------------------------


def test_validate_routing_antigravity_allows(tmp_path):
    """`routing.native: antigravity` or `gemini` → allowed for antigravity provider."""
    from swarph_cli.commands.spawn import _validate_routing
    for native in ("antigravity", "gemini"):
        payload = {
            "schema_version": SCHEMA_VERSION_V1,
            "name": "gemini-researcher",
            "role": "gemini-researcher",
            "cwd": str(tmp_path),
            "provider": "antigravity",
            "routing": {"native": native},
        }
        p = tmp_path / f"cell_{native}.yaml"
        p.write_text(yaml.safe_dump(payload), encoding="utf-8")
        cell = load_cell(p)
        _validate_routing(cell)  # passes silently


def test_validate_routing_antigravity_rejects_anthropic(tmp_path):
    """`routing.native: anthropic` → rejected for antigravity provider."""
    from swarph_cli.commands.spawn import _validate_routing
    from swarph_cli.cell import CellError
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gemini-researcher",
        "role": "gemini-researcher",
        "cwd": str(tmp_path),
        "provider": "antigravity",
        "routing": {"native": "anthropic"},
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    with pytest.raises(CellError) as exc_info:
        _validate_routing(cell)
    assert "anthropic" in str(exc_info.value)
    assert "antigravity" in str(exc_info.value)


def test_build_agy_argv_fresh(tmp_path):
    """Verify agy argv when no session state exists on disk."""
    from swarph_cli.commands.spawn import _build_agy_argv
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gemini-researcher",
        "role": "gemini-researcher",
        "cwd": str(tmp_path),
        "provider": "antigravity",
        "starter_prompt_path": str(tmp_path / "starter.txt"),
    }
    (tmp_path / "starter.txt").write_text("Hello starter!", encoding="utf-8")
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)

    argv = _build_agy_argv(cell, no_starter=False, passthrough=["--extra"])
    assert argv == [
        "agy",
        "--sandbox",
        "--add-dir",
        str(tmp_path),
        "--prompt-interactive",
        "Hello starter!",
        "--extra",
    ]


def test_build_agy_argv_sandbox_none_defaults_true(tmp_path):
    from swarph_cli.commands.spawn import _build_agy_argv
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gemini-researcher",
        "role": "gemini-researcher",
        "cwd": str(tmp_path),
        "provider": "antigravity",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    # Simulate codex adding cell.sandbox = None
    cell.sandbox = None
    argv = _build_agy_argv(cell, no_starter=True, passthrough=[])
    assert "--sandbox" in argv


def test_build_agy_argv_sandbox_false(tmp_path):
    from swarph_cli.commands.spawn import _build_agy_argv
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gemini-researcher",
        "role": "gemini-researcher",
        "cwd": str(tmp_path),
        "provider": "antigravity",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cell = load_cell(p)
    # Simulate cell.sandbox = False
    cell.sandbox = False
    argv = _build_agy_argv(cell, no_starter=True, passthrough=[])
    assert "--sandbox" not in argv


def test_agy_env_scrub(monkeypatch):
    """Verify _agy_env correctly scrubs billing environment variables."""
    from swarph_cli.commands.spawn import _agy_env

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/some/path.json")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-123")
    monkeypatch.setenv("VERTEX_PROJECT", "v-project")
    monkeypatch.setenv("VERTEX_LOCATION", "us-central1")

    env = _agy_env()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "GOOGLE_CLOUD_PROJECT" not in env
    assert "VERTEX_PROJECT" not in env
    assert "VERTEX_LOCATION" not in env


def test_run_spawn_antigravity_dry_run(tmp_path, isolated_xdg, capsys):
    from swarph_cli.commands.spawn import run_spawn

    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "gemini-researcher",
        "role": "gemini-researcher",
        "cwd": str(tmp_path),
        "provider": "antigravity",
    }
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")

    import os
    os.chdir(tmp_path)
    run_spawn(["--dry-run"])
    captured = capsys.readouterr()
    assert "agy --sandbox --add-dir" in captured.out
    assert "provider:    antigravity" in captured.err
