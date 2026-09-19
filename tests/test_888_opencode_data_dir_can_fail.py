"""#888 -- the can-fail EXECUTED, and the migration drops the self-referential snapshot repo.

The accept check asks for a test that fails if the data dir ever resolves under the cell's
cwd again, asserting the RELATIONSHIP, with the can-fail executed: point it back inside the
tree, show it red, revert. `test_DATA_is_NEVER_inside_the_work_tree` (test_opencode_membrane)
is the guard; this file runs the guard's own failure.
"""
import types
from pathlib import Path

import pytest

import swarph_cli.commands.spawn as spawn


def _cell(cwd):
    return types.SimpleNamespace(cwd=cwd, name="opencode-888", provider="opencode",
                                 role="worker", starter_prompt_path=None, sandbox=None, extra={})


def _assert_data_outside_cwd(cell):
    data = Path(spawn._opencode_data_dir(cell)).resolve()
    cwd = Path(cell.cwd).resolve()
    assert not data.is_relative_to(cwd), f"opencode data dir {data} resolves under the cell cwd {cwd}"


def test_the_guard_holds_on_main(tmp_path):
    cwd = tmp_path / "repo"; cwd.mkdir()
    _assert_data_outside_cwd(_cell(cwd))


def test_can_fail_pointing_the_data_dir_back_inside_the_tree_reds_the_guard(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"; cwd.mkdir()
    cell = _cell(cwd)
    monkeypatch.setattr(spawn, "_opencode_data_dir",
                        lambda c: Path(c.cwd) / ".opencode-cell" / "data")   # the pre-fix shape
    with pytest.raises(AssertionError) as red:
        _assert_data_outside_cwd(cell)
    assert "resolves under the cell cwd" in str(red.value)
    monkeypatch.undo()                                                    # revert
    _assert_data_outside_cwd(cell)


def test_migration_moves_sessions_out_and_drops_the_snapshot_repo(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"; fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    cwd = tmp_path / "repo"; cwd.mkdir()
    cell = _cell(cwd)
    old = cwd / ".opencode-cell" / "data" / "opencode"
    (old / "snapshot" / "proj" / "objects").mkdir(parents=True)
    (old / "snapshot" / "proj" / "objects" / "blob").write_text("self-referential")
    (old / "opencode.db").write_text("sessions")
    spawn._migrate_opencode_data(cell)
    new = spawn._opencode_data_dir(cell)
    assert (new / "opencode" / "opencode.db").read_text() == "sessions"
    assert not (new / "opencode" / "snapshot").exists()
    assert not (cwd / ".opencode-cell" / "data").exists()
