"""Terminal-safe rendering of UNTRUSTED (peer-authored) text.

Mesh content — channel messages, card titles/bodies, peer names — is authored by
other cells and humans and printed to the operator's terminal by `swarph channel
read`, `swarph board cards show`, etc. Raw content can carry ANSI/OSC escape
sequences (`\\x1b[…`) that hijack the terminal: overwrite lines, hide text,
retitle the window, or (on some terminals) worse. Strip the control range before
display — the graphify `sanitize_label` lesson.

Keeps `\\t` and `\\n` (legitimate layout); removes C0 controls incl. ESC, C1, and
DEL, so no escape sequence can survive to reach the terminal emulator.
"""
from __future__ import annotations

import re

# C0 (0x00-0x08, 0x0b-0x1f — includes ESC 0x1b; keeps TAB 0x09 + LF 0x0a),
# DEL (0x7f), and C1 (0x80-0x9f).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_terminal(text) -> str:
    """Return ``text`` with terminal control/escape characters removed."""
    if not text:
        return ""
    return _CTRL_RE.sub("", str(text))
