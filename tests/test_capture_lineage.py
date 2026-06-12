import json
from pathlib import Path

import pytest

from swarph_cli.capture import lineage, paths
from swarph_shared.cell import Cell


def _make_cell(tmp_path: Path) -> Cell:
    cwd = tmp_path / "work"
    cwd.mkdir()
    yaml = tmp_path / "droplet.yaml"
    yaml.write_text("schema_version: '1'\nname: droplet\nrole: droplet\n")
    starter = tmp_path / "starter.md"
    starter.write_text("you are droplet")
    cell = Cell(
        schema_version="1", name="droplet", role="droplet",
        cwd=cwd, provider="claude",
        session_id=None, starter_prompt_path=starter, extra={},
    )
    cell.source_path = yaml
    return cell


def test_workspace_fingerprint_is_deterministic(tmp_path):
    cell = _make_cell(tmp_path)
    fp1 = lineage.workspace_fingerprint(cell)
    fp2 = lineage.workspace_fingerprint(cell)
    assert fp1 == fp2
    assert fp1.startswith("sha256:")


def test_record_genesis_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cell = _make_cell(tmp_path)
    lineage.record_genesis(cell, session_id="uuid-1", cursor_path="/c/cursor.json")
    rows = [json.loads(l) for l in paths.lineage_path("droplet").read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "genesis"
    assert r["parent"] is None and r["parent_session_id"] is None
    assert r["session_id"] == "uuid-1"
    assert r["cursor_path"] == "/c/cursor.json"
    assert r["signed"] is False and r["sig"] is None and r["parent_sig"] is None
    assert r["workspace_fingerprint"].startswith("sha256:")
    assert r["where"]["cwd"] == str(cell.cwd)


def test_record_mitosis_carries_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cell = _make_cell(tmp_path)
    lineage.record_mitosis(
        cell, child_role="droplet-2", parent_role="droplet",
        child_session_id="uuid-2", parent_session_id="uuid-1", cursor_path=None,
    )
    rows = [json.loads(l) for l in paths.lineage_path("droplet-2").read_text().splitlines()]
    r = rows[0]
    assert r["cell"] == "droplet-2"
    assert r["kind"] == "mitosis"
    assert r["parent"] == "droplet"
    assert r["parent_session_id"] == "uuid-1"
    assert r["session_id"] == "uuid-2"
    assert r["signed"] is False


def test_append_is_append_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cell = _make_cell(tmp_path)
    lineage.record_genesis(cell, session_id="uuid-1", cursor_path=None)
    lineage.append_lineage_event("droplet", {"kind": "reseat", "when": "later"})
    rows = paths.lineage_path("droplet").read_text().splitlines()
    assert len(rows) == 2


def test_lineage_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cell = _make_cell(tmp_path)
    assert lineage.lineage_exists("droplet") is False
    lineage.record_genesis(cell, session_id="uuid-1", cursor_path=None)
    assert lineage.lineage_exists("droplet") is True
