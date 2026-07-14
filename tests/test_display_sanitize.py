"""Terminal-escape sanitization of untrusted mesh content (security fix)."""
from swarph_cli.commands._display import sanitize_terminal


def test_strips_esc_and_ansi_csi():
    # a hostile ANSI sequence: clear-line + reposition + fake text
    hostile = "hello\x1b[2K\x1b[1;31mFAKE\x1b[0m"
    out = sanitize_terminal(hostile)
    assert "\x1b" not in out, "ESC must be gone"
    assert "hello" in out and "FAKE" in out, "printable text survives (harmless)"


def test_strips_c0_c1_del_keeps_tab_newline():
    assert sanitize_terminal("a\x00b\x07c\x7fd\x9be") == "abcde"
    assert sanitize_terminal("keep\tthis\nline") == "keep\tthis\nline"


def test_handles_none_and_nonstr():
    assert sanitize_terminal(None) == ""
    assert sanitize_terminal(4834) == "4834"
