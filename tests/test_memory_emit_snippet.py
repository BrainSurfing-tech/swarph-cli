"""A memory's timeline snippet must summarise the memory, not its frontmatter delimiter."""
from pathlib import Path
from swarph_cli.commands.memory_emit_hook import _snippet

FRONTMATTER = """---
name: some-memory
description: The one-line summary that exists to be used here.
metadata:
  type: feedback
---

Body first line which is not the summary.
"""


def test_delimiter_is_never_the_snippet(tmp_path):
    """THE REGRESSION. '---' is a non-empty string, so the caller's `if snippet` guard passed
    and 21 timeline entries between 2026-08-25 and 2026-09-15 read 'memory cached: ---'."""
    f = tmp_path / "m.md"; f.write_text(FRONTMATTER)
    s = _snippet(f)
    assert s != "---" and not s.startswith("---"), f"delimiter leaked into the snippet: {s!r}"
    assert s == "The one-line summary that exists to be used here."


def test_body_used_when_there_is_no_description(tmp_path):
    f = tmp_path / "m.md"
    f.write_text("---\nname: x\n---\n\n# Heading\nThe body line.\n")
    assert _snippet(f) == "Heading"


def test_no_frontmatter_still_works(tmp_path):
    f = tmp_path / "m.md"; f.write_text("\n\nJust a body line.\n")
    assert _snippet(f) == "Just a body line."


def test_unterminated_frontmatter_does_not_return_the_delimiter(tmp_path):
    f = tmp_path / "m.md"; f.write_text("---\nname: x\nstill going\n")
    s = _snippet(f)
    assert s != "---" and s != ""


def test_unreadable_file_returns_empty_so_the_caller_falls_back(tmp_path):
    assert _snippet(tmp_path / "nope.md") == ""
