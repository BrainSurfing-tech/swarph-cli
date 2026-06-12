# tests/test_cell_harden.py
from pathlib import Path

import pytest

from swarph_cli.capture import harden, manifest, lineage, paths
from swarph_shared.cell import Cell


def _cell(tmp_path: Path, extra=None) -> Cell:
    cwd = tmp_path / "work"; cwd.mkdir()
    yaml = tmp_path / "droplet.yaml"
    yaml.write_text("schema_version: '1'\nname: droplet\nrole: droplet\n")
    c = Cell(schema_version="1", name="droplet", role="droplet", cwd=cwd,
             provider="claude", session_id=None, starter_prompt_path=None,
             extra=extra or {})
    c.source_path = yaml
    return c


def test_harden_emits_kit_without_installing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cell = _cell(tmp_path, extra={"cursor_path": "/c/cursor.json"})
    monkeypatch.setattr(harden, "_resolve_cell", lambda role: cell)
    monkeypatch.setattr(harden, "_read_pin_uuid", lambda role: "uuid-1")
    res = harden.harden_cell("droplet")
    # launch wrapper emitted + executable
    launch = Path(res.launch_script)
    assert launch.exists()
    assert "swarph spawn droplet" in launch.read_text()
    assert launch.stat().st_mode & 0o100  # owner-exec bit set
    # manifest written with the cursor + service ref
    m = manifest.read_manifest("droplet")
    assert m["service"] == "claude-tmux@droplet.service"
    assert m["head"]["session_id"] == "uuid-1"
    # genesis lineage written
    assert lineage.lineage_exists("droplet")
    rows = paths.lineage_path("droplet").read_text().splitlines()
    assert len(rows) == 1
    # NO systemctl/loginctl was invoked (instructions are strings only)
    assert any("systemctl" in line for line in res.enable_instructions)


def test_harden_is_idempotent_no_duplicate_genesis(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cell = _cell(tmp_path)
    monkeypatch.setattr(harden, "_resolve_cell", lambda role: cell)
    monkeypatch.setattr(harden, "_read_pin_uuid", lambda role: "uuid-1")
    harden.harden_cell("droplet")
    harden.harden_cell("droplet")
    rows = paths.lineage_path("droplet").read_text().splitlines()
    assert len(rows) == 1  # genesis recorded once, not twice
