"""Tests for `swarph init` (cell.yaml scaffolder). Non-interactive (-y / flag)
path + helpers; cells_dir() isolated via XDG_CONFIG_HOME."""
from __future__ import annotations

from pathlib import Path

import pytest

from swarph_cli.commands.init import run_init, _https_normalize
from swarph_cli.cell import cells_dir, load_cell


@pytest.fixture(autouse=True)
def isolated_cells(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def _run(*argv):
    return run_init(list(argv))


def test_https_normalize():
    assert _https_normalize("git@github.com:darw007d/x.git") == ("https://github.com/darw007d/x.git", True)
    assert _https_normalize("https://github.com/darw007d/x.git") == ("https://github.com/darw007d/x.git", False)


def test_init_writes_validated_cell(tmp_path):
    rc = _run("gpt-test", "--provider", "codex", "--cwd", str(tmp_path), "-y")
    assert rc == 0
    dest = cells_dir() / "gpt-test.yaml"
    assert dest.exists()
    c = load_cell(dest)
    assert c.name == "gpt-test" and c.provider == "codex"
    assert c.role == "gpt-test"                      # default = name
    assert c.sandbox == "workspace-write"            # codex default
    assert c.extra["tmux_session"] == "gpt-test"     # default = name
    assert c.extra["cursor_path"] == "/tmp/gpt-test-cursor.json"


def test_init_rejects_underscore_name(tmp_path):
    assert _run("bad_name", "--provider", "codex", "--cwd", str(tmp_path), "-y") == 2
    assert not (cells_dir() / "bad_name.yaml").exists()


def test_init_requires_provider_when_noninteractive(tmp_path):
    assert _run("c1", "--cwd", str(tmp_path), "-y") == 2


def test_init_refuses_existing_without_force(tmp_path):
    assert _run("dup", "--provider", "codex", "--cwd", str(tmp_path), "-y") == 0
    assert _run("dup", "--provider", "codex", "--cwd", str(tmp_path), "-y") == 2   # exists
    assert _run("dup", "--provider", "codex", "--cwd", str(tmp_path), "-y", "--force") == 0


def test_init_assisted_memory_https_normalized(tmp_path):
    rc = _run("mem", "--provider", "antigravity", "--cwd", str(tmp_path), "-y",
              "--assisted-memory", "git@github.com:darw007d/mem.git")
    assert rc == 0
    c = load_cell(cells_dir() / "mem.yaml")
    assert c.assisted_memory["enabled"] is True
    assert c.assisted_memory["repo"] == "https://github.com/darw007d/mem.git"
    assert c.assisted_memory["interval_min"] == 15


def test_init_antigravity_omits_sandbox(tmp_path):
    rc = _run("agy-cell", "--provider", "antigravity", "--cwd", str(tmp_path), "-y")
    assert rc == 0
    c = load_cell(cells_dir() / "agy-cell.yaml")
    assert c.sandbox is None                          # default-on, not written


def test_init_codex_bad_sandbox_rejected(tmp_path):
    assert _run("c", "--provider", "codex", "--sandbox", "bogus", "--cwd", str(tmp_path), "-y") == 2


def test_init_symlink_cwd(tmp_path):
    cwd = tmp_path / "cellwd"
    rc = _run("linked", "--provider", "codex", "--cwd", str(cwd), "-y", "--symlink-cwd")
    assert rc == 0
    link = cwd / "cell.yaml"
    assert link.is_symlink()
    assert link.resolve() == (cells_dir() / "linked.yaml").resolve()
