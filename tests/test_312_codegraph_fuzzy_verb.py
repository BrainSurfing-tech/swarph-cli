"""#312: the codegraph VERB served fuzzy rows as answers; the hook stopped in 2026-08-01.

THE DEFECT (science-claude, on INSTALLED 0.49.0 — not the tree): `swarph
codegraph <nonexistent-symbol>` printed "Structural matches (8)" and exit 0,
presenting unrelated symbols as an answer. The index's query sanitiser
OR-joins tokens, so a query for `command_beta_executor` matches anything
containing "command" — plausible structure from the wrong repository, with
caller counts that make it read authoritative.

THE FIX IS A PORT, NOT NEW LOGIC: codegraph_hook._match_quality has labelled
exactly this since 2026-08-01 (">>> FUZZY MATCH — NOT AN ANSWER TO YOUR
QUERY"). The verb's format_human had no quality check. The hook's function is
imported, not reimplemented — a second copy is how the verb ended up without
the check in the first place.

ROWS COME IN TWO SHAPES, and both are pinned: local-query dicts (ten keys,
incl. docstring) and relayed dicts (#552: seven keys, NO docstring). The
normalisation `dict(r)` at the call site also covers the historical
sqlite3.Row producer without loosening _match_quality for this caller.
"""
from __future__ import annotations

from swarph_cli.commands.codegraph import format_human

LOCAL_ROW = {
    "repo": "swarph-cli", "name": "command_router", "kind": "function",
    "file_path": "src/swarph_cli/commands/board.py", "start_line": 42,
    "callers": 3, "score": 1.5, "qualified_name": "board.command_router",
    "docstring": "Route the command.", "signature": "def command_router(x)",
}

# The #552 relayed shape: seven keys, NO docstring — the endpoint does not
# project it. format_human's lone `.get` exists for exactly this row.
RELAYED_ROW = {
    "repo": "swarph-cli", "name": "command_router", "kind": "function",
    "file_path": "src/swarph_cli/commands/board.py", "start_line": 42,
    "callers": 3, "signature": "def command_router(x)",
}


def test_fuzzy_rows_are_LABELLED_not_served():
    """The finding itself: every row matched a common token ("command"),
    none contains the distinctive token — the output must say so."""
    out = format_human([dict(LOCAL_ROW)], "def command_beta_executor")
    assert "FUZZY MATCH" in out
    assert "NOT AN ANSWER TO YOUR QUERY" in out
    assert "'command_beta_executor'" in out      # the distinctive token named
    assert "swarph-cli" in out                   # the source repos named


def test_real_matches_carry_NO_warning():
    """Non-vacuity: a row whose name contains the distinctive token is a
    real answer and must NOT be labelled — a warning that fires on
    everything is noise training the reader to ignore it."""
    out = format_human([dict(LOCAL_ROW)], "command_router")
    assert "FUZZY MATCH" not in out
    assert "Structural matches" in out


def test_relayed_shape_without_docstring_still_gets_the_check():
    """The seven-key relayed row must reach the warning path intact —
    the port normalises with dict(r) precisely so neither producer's
    shape decides whether honesty is applied."""
    out = format_human([dict(RELAYED_ROW)], "zzz_nonexistent_symbol")
    assert "FUZZY MATCH" in out


def test_empty_answer_unchanged():
    """The honest negative predates the port and must not move."""
    assert format_human([], "anything") == "No structural matches for: 'anything'"
