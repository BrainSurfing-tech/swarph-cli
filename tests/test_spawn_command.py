"""Tests for ``swarph spawn`` (Phase 7 / v0.6.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from swarph_cli.cell import SCHEMA_VERSION_V1
from swarph_cli.commands.spawn import (
    _build_claude_argv,
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


def test_run_spawn_auto_discovers_cwd_cell_yaml(
    isolated_xdg, fake_cell_yaml, monkeypatch, capsys
):
    monkeypatch.chdir(fake_cell_yaml.parent)
    rc = run_spawn(argv=["--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "lab-test" in captured.err


def test_run_spawn_new_instance_mints_fresh_without_persisting(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-A — `swarph spawn --new-instance` mints UUID fresh + does
    NOT write sidecar. Subsequent default spawn still gets a different
    (or no) UUID from sidecar (since sidecar was never written)."""
    from swarph_cli.cell import session_state_path

    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id", "--new-instance"])
    captured = capsys.readouterr()
    assert rc == 0
    sibling_uuid = captured.out.splitlines()[0]
    import uuid as _uuid
    _uuid.UUID(sibling_uuid)

    # Sidecar must NOT exist after --new-instance
    sidecar = session_state_path("lab-test")
    assert not sidecar.exists(), "sidecar wrote despite --new-instance"

    # Default spawn (no --new-instance) THEN mints a different UUID
    rc2 = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--print-id"])
    out2 = capsys.readouterr().out.splitlines()[0]
    _uuid.UUID(out2)
    assert out2 != sibling_uuid  # different sessions
    assert sidecar.exists()  # default path persists


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


def test_run_spawn_new_instance_dry_run_label(
    isolated_xdg, fake_cell_yaml, capsys
):
    """v0.7 PR-A — dry-run output flags 'sibling' state distinctly from
    the default 'minted+persisted' / 'reused' labels."""
    rc = run_spawn(argv=[str(fake_cell_yaml), "--dry-run", "--new-instance"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "sibling" in captured.err
    assert "sidecar untouched" in captured.err


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
