"""Tests for the cell.yaml loader (Phase 7 / v0.6.0)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterator

import pytest
import yaml

from swarph_cli.cell import (
    Cell,
    CellError,
    Lineage,
    SCHEMA_VERSION_V1,
    cells_dir,
    discover_cell_in_cwd,
    is_mesh_gateway_url,
    load_cell,
    load_or_create_session_id,
    resolve_cell_path,
    session_state_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_xdg(tmp_path, monkeypatch) -> Iterator[Path]:
    """Pin XDG_CONFIG_HOME + XDG_STATE_HOME under tmp_path so each test
    runs against fresh config + state dirs (no leakage to user home).
    """
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    config_root.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    yield tmp_path


@pytest.fixture
def cell_yaml_factory(tmp_path):
    """Factory that writes a cell.yaml file under tmp_path/<name>.yaml."""

    def make(filename: str = "cell.yaml", **fields) -> Path:
        defaults = {
            "schema_version": SCHEMA_VERSION_V1,
            "name": "lab-ovh",
            "role": "lab",
            "cwd": str(tmp_path),
            "provider": "claude",
        }
        defaults.update(fields)
        path = tmp_path / filename
        path.write_text(yaml.safe_dump(defaults), encoding="utf-8")
        return path

    return make


# ---------------------------------------------------------------------------
# Path resolution + discovery
# ---------------------------------------------------------------------------


def test_resolve_cell_path_yaml_extension(tmp_path):
    p = tmp_path / "lab.yaml"
    assert resolve_cell_path(str(p)) == p


def test_resolve_cell_path_role_uses_xdg_dir(isolated_xdg):
    expected = cells_dir() / "lab.yaml"
    assert resolve_cell_path("lab") == expected


def test_resolve_cell_path_dot_means_cwd_cell_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_cell_path(".") == tmp_path / "cell.yaml"


def test_discover_cell_in_cwd_returns_path_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cell.yaml").write_text("name: x\nrole: y\ncwd: /tmp\n")
    assert discover_cell_in_cwd() == tmp_path / "cell.yaml"


def test_discover_cell_in_cwd_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert discover_cell_in_cwd() is None


def test_is_mesh_gateway_url_true_only_for_prefix():
    assert is_mesh_gateway_url("mesh-gateway://peers/x/spawn-context") is True
    assert is_mesh_gateway_url("https://example.com/x") is False
    assert is_mesh_gateway_url("/etc/swarph/cells/x.yaml") is False


# ---------------------------------------------------------------------------
# load_cell — happy path
# ---------------------------------------------------------------------------


def test_load_cell_minimal_required_fields(cell_yaml_factory, tmp_path):
    path = cell_yaml_factory()
    cell = load_cell(path)
    assert cell.name == "lab-ovh"
    assert cell.role == "lab"
    assert cell.cwd == tmp_path
    assert cell.provider == "claude"
    assert cell.session_id is None
    assert cell.starter_prompt_path is None
    assert cell.lineage is None
    assert cell.schema_version == SCHEMA_VERSION_V1


def test_load_cell_pinned_session_id(cell_yaml_factory):
    fixed_uuid = "550e8400-e29b-41d4-a716-446655440000"
    path = cell_yaml_factory(session_id=fixed_uuid)
    cell = load_cell(path)
    assert cell.session_id == fixed_uuid


def test_load_cell_with_lineage_block(cell_yaml_factory):
    path = cell_yaml_factory(
        identity={
            "lineage": {
                "parent_peer_id": "drop",
                "spawn_manifest_signature": None,
            }
        },
    )
    cell = load_cell(path)
    assert isinstance(cell.lineage, Lineage)
    assert cell.lineage.parent_peer_id == "drop"
    assert cell.lineage.spawn_manifest_signature is None


def test_load_cell_starter_prompt_path_resolved_relative_to_cell(
    cell_yaml_factory, tmp_path
):
    starter = tmp_path / "starter.md"
    starter.write_text("you are lab-ovh; act AI-to-AI.")
    path = cell_yaml_factory(starter_prompt_path="starter.md")
    cell = load_cell(path)
    assert cell.starter_prompt_path == starter
    assert "AI-to-AI" in (cell.starter_prompt_text() or "")


def test_load_cell_unknown_keys_preserved_in_extra(cell_yaml_factory):
    path = cell_yaml_factory(mesh={"gateway": "http://x"}, custom_field="v")
    cell = load_cell(path)
    assert cell.extra["mesh"] == {"gateway": "http://x"}
    assert cell.extra["custom_field"] == "v"


# ---------------------------------------------------------------------------
# load_cell — validation errors
# ---------------------------------------------------------------------------


def test_load_cell_missing_file_raises(tmp_path):
    with pytest.raises(CellError, match="not found"):
        load_cell(tmp_path / "missing.yaml")


def test_load_cell_invalid_yaml_raises(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("not: [valid: yaml")
    with pytest.raises(CellError, match="not valid YAML"):
        load_cell(p)


def test_load_cell_top_level_must_be_mapping(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(CellError, match="must be a mapping"):
        load_cell(p)


def test_load_cell_invalid_peer_name_rejected(cell_yaml_factory):
    path = cell_yaml_factory(name="UPPER_CASE")
    with pytest.raises(CellError, match="kebab/snake-case"):
        load_cell(path)


def test_load_cell_missing_role_rejected(cell_yaml_factory):
    path = cell_yaml_factory(role="")
    with pytest.raises(CellError, match="'role' is required"):
        load_cell(path)


def test_load_cell_cwd_must_exist(cell_yaml_factory):
    path = cell_yaml_factory(cwd="/this/path/does/not/exist/xyz")
    with pytest.raises(CellError, match="not a directory"):
        load_cell(path)


def test_load_cell_invalid_session_id_rejected(cell_yaml_factory):
    path = cell_yaml_factory(session_id="not-a-uuid")
    with pytest.raises(CellError, match="not a valid UUID"):
        load_cell(path)


def test_load_cell_non_claude_provider_rejected_in_v0_6(cell_yaml_factory):
    path = cell_yaml_factory(provider="gemini")
    with pytest.raises(CellError, match="v0.7"):
        load_cell(path)


def test_load_cell_unknown_schema_version_rejected(cell_yaml_factory):
    path = cell_yaml_factory(schema_version="v999")
    with pytest.raises(CellError, match="schema_version"):
        load_cell(path)


def test_load_cell_starter_prompt_unreadable_raises_when_accessed(
    cell_yaml_factory, tmp_path
):
    path = cell_yaml_factory(starter_prompt_path="/nope/missing.md")
    cell = load_cell(path)
    with pytest.raises(CellError, match="not readable"):
        cell.starter_prompt_text()


# ---------------------------------------------------------------------------
# load_or_create_session_id — sidecar persistence + atomicity
# ---------------------------------------------------------------------------


def test_load_or_create_session_id_uses_pinned_value(
    isolated_xdg, cell_yaml_factory
):
    fixed = "550e8400-e29b-41d4-a716-446655440000"
    cell = load_cell(cell_yaml_factory(session_id=fixed))
    sid, generated = load_or_create_session_id("lab", cell)
    assert sid == fixed
    assert generated is False
    # Pinned value MUST NOT touch the sidecar.
    assert not session_state_path("lab").exists()


def test_load_or_create_session_id_mints_and_persists(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sid1, generated1 = load_or_create_session_id("lab", cell)
    assert generated1 is True
    uuid.UUID(sid1)  # raises if not a valid UUID
    sidecar = session_state_path("lab")
    assert sidecar.exists()
    assert sidecar.read_text().strip() == sid1


def test_load_or_create_session_id_reuses_persisted_value(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sid1, gen1 = load_or_create_session_id("lab", cell)
    sid2, gen2 = load_or_create_session_id("lab", cell)
    assert sid1 == sid2  # R5 fix — sibling re-spawn gets same UUID
    assert gen1 is True and gen2 is False


def test_load_or_create_session_id_corrupted_sidecar_regenerates(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sidecar = session_state_path("lab")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("not-a-uuid\n")
    sid, generated = load_or_create_session_id("lab", cell)
    uuid.UUID(sid)
    assert generated is True
    assert sidecar.read_text().strip() == sid


def test_load_or_create_session_id_atomic_no_tempfile_left_behind(
    isolated_xdg, cell_yaml_factory
):
    """Atomic-write contract: post-write, no .tmp residue beside sidecar."""
    cell = load_cell(cell_yaml_factory())
    load_or_create_session_id("lab", cell)
    sidecar = session_state_path("lab")
    siblings = list(sidecar.parent.iterdir())
    assert siblings == [sidecar], (
        f"expected only {sidecar.name}; found {[p.name for p in siblings]}"
    )
