"""Regression (#83): `_default_repl_caller` must emit a convention-valid caller for
ANY OS username — including the adversarial cases the old inline slug logic broke on.

#80 fixed a hyphenated STATIC caller constant. This is the same crash-class on the
DYNAMIC repl-default path: `_default_repl_caller` rolled its own slug
(`"".join(c if c.isalnum() else "_" ...)`) that — unlike `caller._sanitize_username`
which `default_caller` uses — did NOT guarantee a leading letter, collapse underscores,
or fall back on empty. So a leading-digit username produced `cli.repl.3bob`, which
fails `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$` and would crash SwarphCall at runtime.
Fix: reuse `_sanitize_username` (single sanitizer, no divergence).
"""
import pytest

from swarph_shared.caller_convention import validate_caller

from swarph_cli.commands.chat import _default_repl_caller


@pytest.mark.parametrize("username", [
    "3bob",      # leading digit — the reported crash
    "9",         # all-digit
    "!!!",       # all-special -> empty slug -> must fall back, not emit `cli.repl.`
    "ALICE",     # uppercase
    "bob.smith", # dot (would otherwise split the segment)
    "a-b-c",     # hyphens (the #80 char)
    "_",         # underscore-only -> empty after strip
    "  ",        # whitespace-only
])
def test_repl_caller_conforms_for_adversarial_usernames(monkeypatch, username):
    """The repl default caller validates clean for every adversarial username."""
    monkeypatch.setenv("USER", username)
    monkeypatch.delenv("LOGNAME", raising=False)
    # raises ValueError if the produced tag violates the convention
    validate_caller(_default_repl_caller())


def test_repl_caller_stays_distinct_from_oneshot(monkeypatch):
    """Still `cli.repl.*` (not `cli.oneshot.*`) — the separate producer exists so
    attribution distinguishes REPL turns from one-shot calls; the fix must not
    collapse them."""
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("LOGNAME", raising=False)
    assert _default_repl_caller().startswith("cli.repl.")
