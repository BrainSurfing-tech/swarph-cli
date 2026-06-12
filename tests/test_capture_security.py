"""Adversarial tests for the capture role/path boundary (drop seat-A PR #65 review).

Covers the BLOCKER/HIGH/MEDIUM findings: role path-traversal, shell-metachar
roles, launch-wrapper injection + mode, role-divergence dup-genesis, and the
fail-closed-on-corrupt-manifest gate. These replace the patterns the original
tests ratified."""
import json
import os
import stat
from pathlib import Path

import pytest

from swarph_cli.capture import paths, lineage, manifest, harden, verify
from swarph_cli.capture.paths import CaptureRoleError, validate_role
from swarph_shared.cell import Cell


# --- role charset gate (BLOCKER 1 root cause) --------------------------------

@pytest.mark.parametrize("bad", [
    "../../../../tmp/x/forged",
    "../../etc/cron.d/evil",
    "a/b",
    "a$(touch X)",
    "a`touch X`",
    "a;touch X",
    "a b",
    "UPPER",
    "",
    "-leading-hyphen",
    "trailing-hyphen-",
])
def test_validate_role_rejects_unsafe(bad):
    with pytest.raises(CaptureRoleError):
        validate_role(bad)


@pytest.mark.parametrize("good", ["lab", "drop-on-meta-edge", "gridiron", "drop-2", "a1"])
def test_validate_role_accepts_kebab(good):
    assert validate_role(good) == good


def test_lineage_path_refuses_traversal_role():
    with pytest.raises(CaptureRoleError):
        paths.lineage_path("../../../../tmp/x/forged")


def test_manifest_path_refuses_traversal_role():
    with pytest.raises(CaptureRoleError):
        paths.manifest_path("../../etc/evil")


# --- BLOCKER 1: poisoned cell.yaml role field can't forge lineage ------------

def test_record_genesis_with_traversal_role_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "work"; cwd.mkdir()
    evil = Cell(schema_version="v1", name="evil-cell",
                role="../../../../../../tmp/forged", cwd=cwd, provider="claude",
                session_id=None, starter_prompt_path=None, extra={})
    evil.source_path = tmp_path / "evil.yaml"
    # record_genesis builds lineage_path(cell.role) → validate_role raises;
    # NOTHING is written outside the state tree.
    with pytest.raises(CaptureRoleError):
        lineage.record_genesis(evil, session_id="x", cursor_path=None)
    assert not (Path("/tmp") / "forged.jsonl").exists()


def test_harden_with_traversal_cli_arg_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(CaptureRoleError):
        harden.harden_cell("../../../../tmp/x/forged")


# --- HIGH 2: role-divergence must not duplicate genesis ----------------------

def test_role_divergence_keys_lineage_off_cell_role(tmp_path, monkeypatch):
    # `harden drop-2` slot-strips to drop.yaml (cell.role=drop). Lineage must
    # key off cell.role, so re-harden does NOT append a duplicate genesis and
    # the manifest points at the REAL lineage file (drop.jsonl), not drop-2.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cwd = tmp_path / "work"; cwd.mkdir()
    base = Cell(schema_version="v1", name="drop", role="drop", cwd=cwd,
                provider="claude", session_id=None, starter_prompt_path=None, extra={})
    base.source_path = tmp_path / "drop.yaml"
    monkeypatch.setattr(harden, "_resolve_cell", lambda role: base)  # any arg → base cell
    monkeypatch.setattr(harden, "_read_pin_uuid", lambda role: "uuid-1")
    res1 = harden.harden_cell("drop-2")
    res2 = harden.harden_cell("drop-2")
    # lineage pointer is the real drop.jsonl, not a phantom drop-2.jsonl
    assert res1.lineage_path == str(paths.lineage_path("drop"))
    assert not paths.lineage_path("drop-2").exists()
    # exactly ONE genesis, no duplicate from the second harden
    rows = paths.lineage_path("drop").read_text().splitlines()
    assert len(rows) == 1


# --- MEDIUM: launch wrapper mode + atomicity ---------------------------------

def test_launch_wrapper_is_not_world_writable_or_world_exec(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    target = harden._write_launch_wrapper("droplet")
    mode = stat.S_IMODE(target.stat().st_mode)
    # 0o700: owner rwx only — no group/other bits
    assert mode == 0o700, oct(mode)
    assert "exec swarph spawn droplet" in target.read_text()


# --- MEDIUM: verify fails CLOSED on a corrupt manifest -----------------------

def test_verify_fails_closed_on_corrupt_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"; cwd.mkdir()
    c = Cell(schema_version="v1", name="droplet", role="droplet", cwd=cwd,
             provider="claude", session_id=None, starter_prompt_path=None, extra={})
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: c)
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [])
    paths.captures_dir().mkdir(parents=True, exist_ok=True)
    (paths.captures_dir() / "broken.json").write_text("{corrupt")
    r = verify.verify_cell("droplet")
    assert not r.ok and r.code == 5  # fail-closed, NOT a silent pass


# --- CLI-layer rejection -----------------------------------------------------

def test_cli_verify_rejects_metachar_role():
    from swarph_cli.commands import cell as cell_cmd
    assert cell_cmd.run_cell(["verify", "a$(touch X)"]) == 2


def test_cli_harden_rejects_traversal_role():
    from swarph_cli.commands import cell as cell_cmd
    assert cell_cmd.run_cell(["harden", "../../etc/evil"]) == 2
