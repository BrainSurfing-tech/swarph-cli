"""Tests for ``swarph hook-output`` (v0.7 PR-C SessionStart callback)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Iterator

import pytest
import yaml

from swarph_cli.cell import SCHEMA_VERSION_V1, cells_dir
from swarph_cli.commands.hook_output import (
    _discover_cell_path,
    run_hook_output,
)


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
def cell_with_starter(tmp_path):
    """Create a cell.yaml + starter prompt file under tmp_path."""
    starter = tmp_path / "starter.md"
    starter.write_text("you are lab; act AI-to-AI.")
    cell_yaml = tmp_path / "cell.yaml"
    cell_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test-cell",
        "role": "test",
        "cwd": str(tmp_path),
        "starter_prompt_path": "starter.md",
        "provider": "claude",
    }))
    return cell_yaml


def _capture_hook_output(capsys, *, stdin_input: str = "{}"):
    """Helper: invoke run_hook_output + parse stdout JSON."""
    captured = capsys.readouterr()  # clear pre-existing buffer
    rc = run_hook_output(argv=[])
    captured = capsys.readouterr()
    return rc, captured.out


def test_hook_output_skips_when_swarph_spawn_env_is_set(
    isolated_xdg, cell_with_starter, monkeypatch, capsys
):
    """SWARPH_SPAWN=1 means swarph already injected via spawn path —
    hook must skip (emit empty additionalContext) to avoid
    double-injection."""
    monkeypatch.chdir(cell_with_starter.parent)
    monkeypatch.setenv("SWARPH_SPAWN", "1")
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_output_emits_starter_prompt_when_cell_yaml_in_cwd(
    isolated_xdg, cell_with_starter, monkeypatch, capsys
):
    monkeypatch.chdir(cell_with_starter.parent)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "AI-to-AI" in parsed["hookSpecificOutput"]["additionalContext"]


def test_hook_output_no_op_when_no_cell_yaml(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_output_no_op_when_starter_prompt_path_missing(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    """cell.yaml with NO starter_prompt_path key → no-op."""
    cell_yaml = tmp_path / "cell.yaml"
    cell_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test-cell",
        "role": "test",
        "cwd": str(tmp_path),
        "provider": "claude",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_output_no_op_when_starter_file_unreadable(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    """cell.yaml points at a starter file that doesn't exist → no-op
    (gracefully degrade; hook MUST NOT block session startup)."""
    cell_yaml = tmp_path / "cell.yaml"
    cell_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test-cell",
        "role": "test",
        "cwd": str(tmp_path),
        "starter_prompt_path": "/no/such/file.md",
        "provider": "claude",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_output_no_op_when_cell_yaml_malformed(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    """Malformed YAML → no-op + exit 0 (don't block session startup)."""
    cell_yaml = tmp_path / "cell.yaml"
    cell_yaml.write_text("not: [valid: yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_output_falls_back_to_xdg_cells_dir_keyed_on_cwd(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    """If no ./cell.yaml in cwd, look up <cells_dir>/<basename(cwd)>.yaml."""
    starter = tmp_path / "starter.md"
    starter.write_text("xdg-fallback starter prompt")
    spawn_dir = tmp_path / "lab-test"  # basename will become 'lab-test'
    spawn_dir.mkdir()
    cells_yaml = cells_dir() / "lab-test.yaml"
    cells_yaml.parent.mkdir(parents=True, exist_ok=True)
    cells_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "lab-test",
        "role": "lab-test",
        "cwd": str(spawn_dir),
        "starter_prompt_path": str(starter),
        "provider": "claude",
    }))
    monkeypatch.chdir(spawn_dir)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert "xdg-fallback" in parsed["hookSpecificOutput"]["additionalContext"]


def test_hook_output_cwd_local_cell_yaml_wins_over_xdg_fallback(
    isolated_xdg, tmp_path, monkeypatch, capsys
):
    """./cell.yaml takes precedence over ~/.config/swarph/cells/* keyed on cwd."""
    cwd_starter = tmp_path / "cwd-starter.md"
    cwd_starter.write_text("CWD WINS")
    xdg_starter = tmp_path / "xdg-starter.md"
    xdg_starter.write_text("XDG LOSES")
    cwd_yaml = tmp_path / "cell.yaml"
    cwd_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test", "role": "test",
        "cwd": str(tmp_path), "starter_prompt_path": str(cwd_starter),
        "provider": "claude",
    }))
    xdg_yaml = cells_dir() / f"{tmp_path.name}.yaml"
    xdg_yaml.parent.mkdir(parents=True, exist_ok=True)
    xdg_yaml.write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION_V1,
        "name": "test", "role": "test",
        "cwd": str(tmp_path), "starter_prompt_path": str(xdg_starter),
        "provider": "claude",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWARPH_SPAWN", raising=False)
    rc, out = _capture_hook_output(capsys)
    parsed = json.loads(out)
    assert "CWD WINS" in parsed["hookSpecificOutput"]["additionalContext"]


def test_discover_cell_path_returns_none_when_neither_path_exists(
    isolated_xdg, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert _discover_cell_path() is None
