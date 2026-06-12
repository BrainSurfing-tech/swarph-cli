# tests/test_spawn_mitosis_lineage.py
from pathlib import Path

import pytest

from swarph_cli.commands import spawn
from swarph_shared.cell import Cell


def _cell(tmp_path: Path) -> Cell:
    cwd = tmp_path / "work"; cwd.mkdir()
    c = Cell(schema_version="1", name="drop", role="drop", cwd=cwd,
             provider="claude", session_id=None, starter_prompt_path=None, extra={})
    c.source_path = tmp_path / "drop.yaml"
    return c


def test_sibling_mint_records_mitosis(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cell = _cell(tmp_path)
    calls = {}

    def fake_record_mitosis(c, *, child_role, parent_role, child_session_id,
                            parent_session_id, cursor_path):
        calls.update(child_role=child_role, parent_role=parent_role,
                     child_session_id=child_session_id,
                     parent_session_id=parent_session_id)

    monkeypatch.setattr(spawn, "_record_mitosis_safe", spawn._record_mitosis_safe)
    monkeypatch.setattr("swarph_cli.capture.lineage.record_mitosis", fake_record_mitosis)
    monkeypatch.setattr(spawn, "_base_pin_uuid", lambda role: "parent-uuid")

    spawn._record_mitosis_safe(
        cell, sidecar_role="drop", effective_role="drop-2",
        session_id="child-uuid", was_generated=True,
    )
    assert calls["child_role"] == "drop-2"
    assert calls["parent_role"] == "drop"
    assert calls["child_session_id"] == "child-uuid"
    assert calls["parent_session_id"] == "parent-uuid"


def test_non_sibling_spawn_records_nothing(tmp_path, monkeypatch):
    cell = _cell(tmp_path)
    called = []
    monkeypatch.setattr("swarph_cli.capture.lineage.record_mitosis",
                        lambda *a, **k: called.append(1))
    # effective_role == sidecar_role → base slot, not a sibling
    spawn._record_mitosis_safe(cell, sidecar_role="drop", effective_role="drop",
                               session_id="x", was_generated=True)
    assert called == []


def test_record_failure_never_raises(tmp_path, monkeypatch, capsys):
    cell = _cell(tmp_path)
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("swarph_cli.capture.lineage.record_mitosis", boom)
    monkeypatch.setattr(spawn, "_base_pin_uuid", lambda role: "p")
    # must swallow + warn, not propagate (spec §7: never block the exec)
    spawn._record_mitosis_safe(cell, sidecar_role="drop", effective_role="drop-2",
                               session_id="c", was_generated=True)
    assert "lineage" in capsys.readouterr().err.lower()
