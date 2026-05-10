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
    from swarph_cli.cell import read_starter_prompt
    assert "AI-to-AI" in (read_starter_prompt(cell) or "")


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
    with pytest.raises(CellError, match="v0.8"):
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
    from swarph_cli.cell import read_starter_prompt
    with pytest.raises(CellError, match="not readable"):
        read_starter_prompt(cell)


# ---------------------------------------------------------------------------
# load_or_create_session_id — sidecar persistence + atomicity
# ---------------------------------------------------------------------------


def test_load_or_create_session_id_uses_pinned_value(
    isolated_xdg, cell_yaml_factory
):
    fixed = "550e8400-e29b-41d4-a716-446655440000"
    cell = load_cell(cell_yaml_factory(session_id=fixed))
    sid, generated, _role = load_or_create_session_id("lab", cell)
    assert sid == fixed
    assert generated is False
    # Pinned value MUST NOT touch the sidecar.
    assert not session_state_path("lab").exists()


def test_load_or_create_session_id_mints_and_persists(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sid1, generated1, _ = load_or_create_session_id("lab", cell)
    assert generated1 is True
    uuid.UUID(sid1)  # raises if not a valid UUID
    sidecar = session_state_path("lab")
    assert sidecar.exists()
    assert sidecar.read_text().strip() == sid1


def test_load_or_create_session_id_reuses_persisted_value(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sid1, gen1, _ = load_or_create_session_id("lab", cell)
    sid2, gen2, _ = load_or_create_session_id("lab", cell)
    assert sid1 == sid2  # R5 fix — sibling re-spawn gets same UUID
    assert gen1 is True and gen2 is False


def test_load_or_create_session_id_corrupted_sidecar_regenerates(
    isolated_xdg, cell_yaml_factory
):
    cell = load_cell(cell_yaml_factory())
    sidecar = session_state_path("lab")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("not-a-uuid\n")
    sid, generated, _role = load_or_create_session_id("lab", cell)
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


# ---------------------------------------------------------------------------
# v0.7 PR-A — `--new-instance` flag (beta #892 B2)
# ---------------------------------------------------------------------------


def test_new_instance_mints_fresh_uuid_without_touching_sidecar(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-A — sibling-spawn case mints fresh UUID + leaves sidecar
    untouched so re-resume of the original session still works."""
    cell = load_cell(cell_yaml_factory())
    # First call without new_instance — establishes sidecar
    sid_orig, gen_orig, _ = load_or_create_session_id("lab", cell, new_instance=False)
    assert gen_orig is True
    sidecar = session_state_path("lab")
    sidecar_content_after_first = sidecar.read_text()

    # Sibling call — fresh UUID, sidecar untouched
    sid_sibling, gen_sibling, _ = load_or_create_session_id(
        "lab", cell, new_instance=True
    )
    assert gen_sibling is True
    assert sid_sibling != sid_orig  # genuine fresh UUID
    uuid.UUID(sid_sibling)
    assert sidecar.read_text() == sidecar_content_after_first  # untouched

    # Third call without new_instance — recovers the ORIGINAL UUID via sidecar
    sid_resume, gen_resume, _ = load_or_create_session_id("lab", cell, new_instance=False)
    assert gen_resume is False
    assert sid_resume == sid_orig  # sibling-spawn didn't break re-resume of original


def test_new_instance_respects_pinned_session_id_in_cell_yaml(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-A — cell.yaml-pinned session_id is operator intent and
    overrides --new-instance. Pinned UUID returned + was_generated=False
    + sidecar untouched. The CLI surfaces the conflict as a stderr
    warning (tested in spawn-command tests)."""
    fixed = "550e8400-e29b-41d4-a716-446655440000"
    cell = load_cell(cell_yaml_factory(session_id=fixed))
    sid, generated, _ = load_or_create_session_id("lab", cell, new_instance=True)
    assert sid == fixed
    assert generated is False
    assert not session_state_path("lab").exists()


def test_new_instance_default_false_preserves_v0_6_behavior(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-A — default new_instance=False keeps v0.6 sidecar-resume
    behavior intact. Regression guard."""
    cell = load_cell(cell_yaml_factory())
    sid1, _, role1 = load_or_create_session_id("lab", cell)  # default param
    sid2, gen2, role2 = load_or_create_session_id("lab", cell)
    assert sid1 == sid2  # v0.6 R5 invariant — same UUID on re-spawn
    assert gen2 is False
    assert role1 == role2 == "lab"  # effective_role unchanged for non-sibling


# ---------------------------------------------------------------------------
# v0.7 PR-B — auto-suffix on sibling-collision (beta #892 B1)
# ---------------------------------------------------------------------------


def test_next_free_slot_role_returns_2_when_only_base_occupied(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — first sibling uses slot 2 (slot 1 reserved for base)."""
    from swarph_cli.cell import next_free_slot_role
    cell = load_cell(cell_yaml_factory())
    load_or_create_session_id("drop", cell)  # occupies base slot
    assert next_free_slot_role("drop") == "drop-2"


def test_next_free_slot_role_skips_occupied_slots(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — slot allocation walks linearly to next free."""
    from swarph_cli.cell import next_free_slot_role
    cell = load_cell(cell_yaml_factory())
    load_or_create_session_id("drop", cell)  # base
    # Manually create 2 + 3 sidecars to simulate prior siblings
    for n in (2, 3):
        path = session_state_path(f"drop-{n}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"550e8400-e29b-41d4-a716-44665544000{n}\n")
    assert next_free_slot_role("drop") == "drop-4"


def test_next_free_slot_role_caps_at_99(isolated_xdg, cell_yaml_factory):
    """v0.7 PR-B — runaway-loop guard at slot 99."""
    from swarph_cli.cell import next_free_slot_role
    cell = load_cell(cell_yaml_factory())
    load_or_create_session_id("drop", cell)
    # Fill slots 2..99
    for n in range(2, 100):
        path = session_state_path(f"drop-{n}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"550e8400-e29b-41d4-a716-44665544{n:04d}\n")
    with pytest.raises(CellError, match="99 sibling slots"):
        next_free_slot_role("drop")


def test_base_role_from_slot_role_strips_n_suffix():
    """v0.7 PR-B — slot-stripping for cell.yaml resolution fallback."""
    from swarph_cli.cell import base_role_from_slot_role
    assert base_role_from_slot_role("drop-2") == "drop"
    assert base_role_from_slot_role("drop-on-meta-edge-3") == "drop-on-meta-edge"
    assert base_role_from_slot_role("drop") == "drop"  # no suffix unchanged
    assert base_role_from_slot_role("drop-100") == "drop-100"  # >99 untouched
    assert base_role_from_slot_role("drop-1") == "drop-1"  # slot 1 reserved, untouched
    assert base_role_from_slot_role("drop-abc") == "drop-abc"  # non-numeric untouched


def test_new_instance_with_existing_base_mints_into_slot_2(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — sibling spawn auto-allocates slot 2 + persists."""
    cell = load_cell(cell_yaml_factory())
    sid_base, _, role_base = load_or_create_session_id("drop", cell)
    sid_sib, gen_sib, role_sib = load_or_create_session_id(
        "drop", cell, new_instance=True
    )
    assert role_base == "drop"
    assert role_sib == "drop-2"
    assert gen_sib is True
    assert sid_sib != sid_base
    # Both sidecars now exist + are independently resumable
    base_path = session_state_path("drop")
    sib_path = session_state_path("drop-2")
    assert base_path.exists() and sib_path.exists()
    assert base_path.read_text().strip() == sid_base
    assert sib_path.read_text().strip() == sid_sib


def test_new_instance_third_call_lands_at_slot_3(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — multiple sibling spawns walk through slots."""
    cell = load_cell(cell_yaml_factory())
    load_or_create_session_id("drop", cell)
    _, _, role_2 = load_or_create_session_id("drop", cell, new_instance=True)
    _, _, role_3 = load_or_create_session_id("drop", cell, new_instance=True)
    assert role_2 == "drop-2"
    assert role_3 == "drop-3"


def test_new_instance_with_no_base_falls_through_to_default(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — degenerate case: --new-instance fired with no base
    sidecar. Falls through to default-spawn path (mints into base slot,
    NOT a sibling slot). CLI surfaces the edge case as stderr note."""
    cell = load_cell(cell_yaml_factory())
    sid, gen, role = load_or_create_session_id(
        "drop", cell, new_instance=True
    )
    assert role == "drop"  # base slot, not drop-2
    assert gen is True
    assert session_state_path("drop").exists()
    assert not session_state_path("drop-2").exists()


def test_resume_sibling_via_slot_role_resolves_base_cell_yaml(
    isolated_xdg, cell_yaml_factory, tmp_path
):
    """v0.7 PR-B — `swarph spawn <role>-2` falls back to base cell.yaml
    so siblings inherit cell-context (cwd, starter prompt, lineage)."""
    from swarph_cli.cell import resolve_cell_path
    # Manually place cell.yaml at $XDG_CONFIG_HOME/swarph/cells/drop.yaml
    yaml_path = cells_dir() / "drop.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        "name: drop\nrole: drop\ncwd: /tmp\nprovider: claude\n"
    )
    # Sibling resume by slot-role resolves to base file
    resolved = resolve_cell_path("drop-2")
    assert resolved == yaml_path
    # Direct spawn still works
    direct = resolve_cell_path("drop")
    assert direct == yaml_path
    # Non-existent role falls through (returns the default path; load_cell raises)
    missing = resolve_cell_path("nonexistent-2")
    assert missing.name == "nonexistent-2.yaml"  # not stripped because base also missing


def test_explicit_slot_yaml_takes_precedence_over_base_fallback(
    isolated_xdg, cell_yaml_factory
):
    """v0.7 PR-B — if `<role>-2.yaml` actually exists, use it directly
    rather than falling back to base. Operator can override the
    sibling-cell-context by authoring an explicit slot file."""
    from swarph_cli.cell import resolve_cell_path
    base_yaml = cells_dir() / "drop.yaml"
    sib_yaml = cells_dir() / "drop-2.yaml"
    base_yaml.parent.mkdir(parents=True, exist_ok=True)
    base_yaml.write_text("name: drop\nrole: drop\ncwd: /tmp\nprovider: claude\n")
    sib_yaml.write_text(
        "name: drop\nrole: drop-2\ncwd: /tmp\nprovider: claude\n"
    )
    # Explicit slot file wins
    assert resolve_cell_path("drop-2") == sib_yaml
