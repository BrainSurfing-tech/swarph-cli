"""mesh inbox display sanitizes peer-authored content (0.29.2 security follow-up)."""
from swarph_cli.commands import mesh


def test_inbox_line_strips_terminal_escapes():
    dm = {"id": 7, "read_at": None, "from_node": "evil\x1b[2K",
          "kind": "question\x1b[1m", "content": "hi\x1b[1;31mINJECT\x1b[0m\nsecond"}
    line = mesh._format_inbox_line(dm)
    assert "\x1b" not in line, "no escape sequence reaches the terminal"
    assert "id=7" in line and "unread" in line and "hi" in line


def test_inbox_line_read_flag_and_newline_flattened():
    dm = {"id": 1, "read_at": "2026-07-14T00:00:00Z", "from_node": "droplet",
          "kind": "fyi", "content": "line1\nline2"}
    line = mesh._format_inbox_line(dm)
    assert "read from=droplet" in line and "line1 line2" in line
