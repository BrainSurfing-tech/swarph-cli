"""`board cards say` must TELL the holder when their post closed an obligation.

Card #562. The gateway already returns `closed_obligations` on POST /messages;
the CLI formatter read `id` only, so a post that discharged an obligation was
indistinguishable in the terminal from one that did not.

Measured consequence: obligation #22 (card #544) closed at 2026-08-21T06:59:15Z
on a status post stating the work was NOT done, and stayed wrongly closed for
SIX HOURS while its holder said so five more times. `obligation_sweep.py`
selects `status = 'open'`, so a wrongly-closed row leaves the sweep set forever
and nothing chases it again.
"""

from __future__ import annotations

from swarph_cli.commands.board import _say_line


def test_a_post_that_closed_nothing_says_nothing_extra():
    """No obligation closed => the line must stay quiet. A warning that fires
    when nothing happened is a warning nobody reads when something does."""
    line = _say_line({"id": 25401, "closed_obligations": []}, 544, "lab-ovh")
    assert line == "posted id=25401 onto card #544 (to lab-ovh)"


def test_an_absent_key_is_not_treated_as_a_close():
    """An older gateway omits the field entirely. Absence is not evidence of a
    close, and must not manufacture a warning."""
    assert "CLOSED OBLIGATION" not in _say_line({"id": 1}, 5, "peer")


def test_a_post_that_closed_an_obligation_SAYS_SO():
    """>>> THE DEFECT, WITH ITS REAL IDS. <<< This exact response came back on
    2026-08-21 and printed only the first line."""
    line = _say_line({"id": 25401, "closed_obligations": [22]}, 544, "lab-ovh")
    assert "posted id=25401" in line
    assert "CLOSED OBLIGATION #22" in line


def test_the_warning_names_that_the_accept_check_was_NOT_evaluated():
    """The dangerous half is not the close, it is closing WITHOUT the falsifier
    #532 exists to enforce. A holder told 'closed' might assume it was checked."""
    line = _say_line({"id": 9, "closed_obligations": [22]}, 544, "peer")
    assert "accept check was NOT evaluated" in line


def test_the_warning_says_the_row_leaves_the_sweep_set():
    """Why six hours passed: obligation_sweep selects status='open', so nothing
    would ever have re-surfaced it. The holder is the last line of defence and
    must be told that."""
    line = _say_line({"id": 9, "closed_obligations": [22]}, 544, "peer")
    assert "sweep set" in line


def test_multiple_closed_obligations_are_all_named():
    line = _say_line({"id": 9, "closed_obligations": [22, 23]}, 544, "peer")
    assert "#22" in line and "#23" in line
