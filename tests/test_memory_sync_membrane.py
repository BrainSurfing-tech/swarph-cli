import types
from pathlib import Path

from swarph_cli.commands import spawn


def _cell(tmp_path):
    return types.SimpleNamespace(cwd=tmp_path, provider="x")


def test_base_membrane_returns_empty(tmp_path):
    m = spawn.ProviderMembrane()
    assert m.memory_sync_files(_cell(tmp_path)) == []
    assert m.memory_restore_dest(("anything",), _cell(tmp_path)) is None
    assert m.memory_guard_file(_cell(tmp_path)) is None


def test_claude_membrane_files_and_dests(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".claude" / "memory").mkdir(parents=True)
    (home / ".claude" / "MEMORY.md").write_text("m")
    (home / ".claude" / "memory" / "a.md").write_text("a")
    (home / ".claude" / "inbox-cursor").write_text("c")
    cwd = tmp_path / "cwd"; cwd.mkdir(); (cwd / "CLAUDE.md").write_text("C")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    m = spawn.MEMBRANES["claude"]
    rels = {r for r, _ in m.memory_sync_files(types.SimpleNamespace(cwd=cwd, provider="claude"))}
    assert rels == {"CLAUDE.md", "MEMORY.md", "memory/a.md", "inbox-cursor"}
    assert m.memory_restore_dest(("MEMORY.md",), None) == home / ".claude" / "MEMORY.md"
    assert m.memory_restore_dest(("memory", "a.md"), None) == home / ".claude" / "memory" / "a.md"
    assert m.memory_restore_dest(("CLAUDE.md",), None) is None   # CLAUDE.md restores via common cwd path
    assert m.memory_guard_file(types.SimpleNamespace(cwd=cwd, provider="claude")) == cwd / "CLAUDE.md"


def test_codex_membrane(tmp_path):
    cwd = tmp_path; (cwd / "AGENTS.md").write_text("A")
    m = spawn.MEMBRANES["codex"]
    assert {r for r, _ in m.memory_sync_files(types.SimpleNamespace(cwd=cwd, provider="codex"))} == {"AGENTS.md"}
    assert m.memory_restore_dest(("AGENTS.md",), None) is None
    assert m.memory_guard_file(types.SimpleNamespace(cwd=cwd, provider="codex")) == cwd / "AGENTS.md"


def test_antigravity_restore_dest(tmp_path, monkeypatch):
    home = tmp_path / "home"; monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    m = spawn.MEMBRANES["antigravity"]
    assert m.memory_restore_dest(("tmp", "proj", "memory", "x.md"), None) == home / ".gemini" / "tmp" / "proj" / "memory" / "x.md"
    assert m.memory_restore_dest(("history", ".project_root"), None) == home / ".gemini" / "history" / ".project_root"
    assert m.memory_restore_dest(("GEMINI.md",), None) is None   # common cwd path
    c = tmp_path; (c / "GEMINI.md").write_text("G")
    assert m.memory_guard_file(types.SimpleNamespace(cwd=c, provider="antigravity")) == c / "GEMINI.md"


def test_grok_membrane_isolated_home(tmp_path):
    cwd = tmp_path
    mem = cwd / spawn._GROK_CELL_HOME_SUBDIR / ".grok" / "memory" / "proj"
    mem.mkdir(parents=True)
    (cwd / spawn._GROK_CELL_HOME_SUBDIR / ".grok" / "memory" / "MEMORY.md").write_text("m")
    (mem / "MEMORY.md").write_text("p")
    m = spawn.MEMBRANES["grok"]
    files = m.memory_sync_files(types.SimpleNamespace(cwd=cwd, provider="grok"))
    rels = {r for r, _ in files}
    assert rels == {"grok-memory/MEMORY.md", "grok-memory/proj/MEMORY.md"}
    # Verify no backslashes in keys (cross-OS restore safety)
    assert all("\\" not in r for r, _ in files)
    assert m.memory_restore_dest(("grok-memory", "proj", "MEMORY.md"), types.SimpleNamespace(cwd=cwd, provider="grok")) \
        == cwd / spawn._GROK_CELL_HOME_SUBDIR / ".grok" / "memory" / "proj" / "MEMORY.md"
    assert m.memory_guard_file(types.SimpleNamespace(cwd=cwd, provider="grok")) is None   # no cwd doc
