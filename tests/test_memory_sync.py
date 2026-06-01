import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from swarph_cli.commands.memory_sync import run_memory_sync, perform_restore
from swarph_cli.cell import Cell

def test_run_memory_sync_no_op_when_not_enabled(tmp_path):
    cell = Cell(
        name="test",
        role="test",
        cwd=tmp_path,
        schema_version="v1",
        session_id=None,
        starter_prompt_path=None,
        provider="claude",
        sandbox=None,
        lineage=None,
        source_path=None,
        assisted_memory={"enabled": False, "interval_min": 15},
        extra={},
    )
    with patch("swarph_cli.commands.memory_sync.load_cell", return_value=cell):
        assert run_memory_sync(["fake.yaml"]) == 0

def test_perform_restore_returns_none_when_not_enabled(tmp_path):
    cell = Cell(
        name="test",
        role="test",
        cwd=tmp_path,
        schema_version="v1",
        session_id=None,
        starter_prompt_path=None,
        provider="claude",
        sandbox=None,
        lineage=None,
        source_path=None,
        assisted_memory={"enabled": False, "interval_min": 15},
        extra={},
    )
    assert perform_restore(cell) is None

def test_perform_restore_absent(tmp_path):
    cell = Cell(
        name="test",
        role="test",
        cwd=tmp_path,
        schema_version="v1",
        session_id=None,
        starter_prompt_path=None,
        provider="claude",
        sandbox=None,
        lineage=None,
        source_path=None,
        assisted_memory=None,
        extra={},
    )
    assert perform_restore(cell) is None
