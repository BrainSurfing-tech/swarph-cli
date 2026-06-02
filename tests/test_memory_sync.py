import pytest
import subprocess
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

def test_perform_restore_returns_none_on_pull_failure(tmp_path):
    cell = Cell(
        name="test", role="test", cwd=tmp_path, schema_version="v1", session_id=None,
        starter_prompt_path=None, provider="claude", sandbox=None, lineage=None,
        source_path=None, assisted_memory={"enabled": True, "repo": "test/repo"}, extra={},
    )
    with patch("swarph_cli.commands.memory_sync.get_memory_repo_path", return_value=tmp_path):
        with patch("swarph_cli.commands.memory_sync._clone_if_missing", return_value=True):
            with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git pull")):
                assert perform_restore(cell) is None

def test_perform_restore_empty_guard(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "CLAUDE.md").write_text("")
    (repo_dir / "CURRENT_TASK.md").write_text("Hello")
    
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    (cell_dir / "CLAUDE.md").write_text("Old contents")
    
    cell = Cell(
        name="test", role="test", cwd=cell_dir, schema_version="v1", session_id=None,
        starter_prompt_path=None, provider="claude", sandbox=None, lineage=None,
        source_path=None, assisted_memory={"enabled": True, "repo": "test/repo"}, extra={},
    )
    with patch("swarph_cli.commands.memory_sync.get_memory_repo_path", return_value=repo_dir):
        with patch("swarph_cli.commands.memory_sync._clone_if_missing", return_value=True):
            with patch("subprocess.run"):
                res = perform_restore(cell)
                assert res == "Hello"
                assert (cell_dir / "CLAUDE.md").read_text() == "Old contents"
                assert (cell_dir / "CURRENT_TASK.md").read_text() == "Hello"
