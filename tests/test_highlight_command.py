"""Tests for ``swarph highlight`` — append to a git-backed timeline.

Offline: each test uses a fresh temp timeline dir with no remote (``--no-push``),
so the real git mechanics run (init + append + commit) without a network.
"""

from __future__ import annotations

import subprocess

from swarph_cli.commands import highlight as hl


# --- pure helpers ----------------------------------------------------------

def test_collapse_newlines():
    assert hl._collapse("a\nb\rc") == "a b c"


def test_format_line_with_memory():
    line = hl._format_line("2026-06-27T00:00Z", "lab-ovh", "shipped X", "[[mem-x]]")
    assert line == "- 2026-06-27T00:00Z · **lab-ovh** · shipped X · → [[mem-x]]"


def test_format_line_without_memory():
    line = hl._format_line("2026-06-27T00:00Z", "lab-ovh", "shipped X", "")
    assert line == "- 2026-06-27T00:00Z · **lab-ovh** · shipped X"


# --- the verb (real git, no remote) ----------------------------------------

def test_highlight_inits_appends_commits(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "test-cell")
    rc = hl.run_highlight(["my highlight", "[[mem-x]]", "--no-push"])
    assert rc == 0
    tl = (d / "TIMELINE.md").read_text(encoding="utf-8")
    assert "**test-cell**" in tl and "my highlight" in tl and "→ [[mem-x]]" in tl
    log = subprocess.run(["git", "-C", str(d), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "highlight" in log.lower()
    # union-merge attribute set so concurrent cell appends auto-merge
    assert "merge=union" in (d / ".gitattributes").read_text(encoding="utf-8")


def test_highlight_collapses_multiline_anti_spoof(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "c")
    rc = hl.run_highlight(["line one\nFORGED **other** entry", "--no-push"])
    assert rc == 0
    entry_lines = [l for l in (d / "TIMELINE.md").read_text(encoding="utf-8").splitlines()
                   if l.startswith("- ")]
    assert len(entry_lines) == 1  # the newline can't forge a second attributed entry
    assert "line one FORGED" in entry_lines[0]


def test_highlight_second_append_keeps_both(tmp_path, monkeypatch):
    d = tmp_path / "tl"
    monkeypatch.setenv("SWARPH_TIMELINE_DIR", str(d))
    monkeypatch.setenv("SWARPH_CELL", "c")
    hl.run_highlight(["first", "--no-push"])
    hl.run_highlight(["second", "--no-push"])
    entry_lines = [l for l in (d / "TIMELINE.md").read_text(encoding="utf-8").splitlines()
                   if l.startswith("- ")]
    assert len(entry_lines) == 2
