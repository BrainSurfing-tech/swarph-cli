"""#650 — the guide's self-check must not teach a check that self-matches.

The first diagnostic in "Check your own setup" was
``pgrep -af "swarph monitor.*--as <you>"`` with "exactly one line" as the
expected answer. Run non-interactively — an agent's shell tool, a script, any
``bash -c`` where the pgrep is not the LAST command (bash execs the last one,
which is why an interactive typing of it reads fine) — the checking shell's own
cmdline carries the pattern and matches itself: ONE healthy monitor returns TWO
lines, and the guide's remedy ("stop the hand-started one") points the reader
at their own shell.

These tests pin the property form (#644's lesson: match the world, not your
expectation of it) so the next guide rewrite cannot silently reintroduce the
pattern form. The can-fail evidence (both directions, on metal, WSL Ubuntu):

    bash -lc "pgrep -af 'swarph monitor.*--as gpu-wsl'"                  -> 1 line (exec'd)
    bash -lc "cd /tmp; pgrep -af 'swarph monitor.*--as gpu-wsl'; echo -" -> 2 lines (SELF-MATCH)
    bash -lc "swarph monitor status --as cw650test --state-dir /tmp/cw650"
                                                                         -> running pid=947820
"""

from __future__ import annotations

import re

from swarph_cli.commands import guide


def _selfcheck_rows() -> "list[str]":
    """The table rows of the 'Check your own setup' section — the commands a
    reader will actually run. Prose mentions of pgrep (the warning paragraph)
    are fine; a table ROW teaching it is the defect."""
    text = guide._load_guide()
    section = guide._split_topics(text).get("check-your-own-setup", "")
    assert section, "guide must keep a 'Check your own setup' section"
    return [ln for ln in section.splitlines() if ln.startswith("| `")]


def test_selfcheck_teaches_monitor_status_as_the_liveness_check():
    rows = _selfcheck_rows()
    assert any("swarph monitor status --as <you>" in r for r in rows), (
        "the property check must be the documented liveness row"
    )


def test_no_selfcheck_row_is_a_bare_pgrep_on_the_monitor_pattern():
    for row in _selfcheck_rows():
        assert not re.search(r"\|\s*`pgrep .*swarph monitor", row), (
            f"self-check row teaches the self-matching pattern form: {row}"
        )


def test_the_self_match_warning_is_kept():
    """The warning paragraph is the 'why' — dropping it while keeping the
    property row would let the NEXT rewrite re-add pgrep as 'simpler'."""
    text = guide._load_guide()
    assert "matches itself" in text
    assert "#650" in text


def test_the_cliless_fallback_is_the_bracket_form_and_it_is_sound():
    """cursor-lin's bracket form (from the superseded #346, adopted): the regex
    ``[s]warph`` matches a real monitor's literal ``swarph`` but NOT the literal
    text ``[s]warph`` in the checker's own cmdline — self-match excluded by
    construction, at any wrapper depth. Pin both the doc and the construction."""
    text = guide._load_guide()
    assert 'pgrep -af "[s]warph monitor.*--as <you>"' in text, (
        "the CLI-less fallback must be the bracket form, not a bare pattern"
    )
    pattern = r"[s]warph monitor"
    checker_cmdline = 'bash -c "pgrep -af "[s]warph monitor.*--as <you>"; echo -"'
    assert re.search(pattern, checker_cmdline) is None, (
        "the bracket form must not match a shell carrying it"
    )
    real_monitor = "/usr/bin/python3 /usr/local/bin/swarph monitor start --as gpu-wsl"
    assert re.search(pattern, real_monitor), (
        "the bracket form must still match a real monitor's cmdline"
    )
