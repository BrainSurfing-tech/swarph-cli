"""`board cards edit --priority` — the flag `add` has had since #256 and `edit` never got.

The gateway has accepted `priority` on PATCH /board/cards/{id} the whole time
(BoardCardPatch.priority, added by #256 for exactly this reason: "PRIORITIES WERE
WRITE-ONCE AT CREATE ... every stale priority on this board was a field nobody COULD
move"). Only the CLI could not send it, so a card filed at the wrong priority was
stuck there for anyone driving the board through swarph.

RANGE IS DELIBERATELY NOT TESTED HERE. `_validate_priority` on the gateway is the
single definition; a range assertion in this file would be the second copy #256b is
about. What is tested is that the value REACHES the wire unaltered, including one the
gateway will reject — because a CLI that silently clamped would hide the 400.
"""
import pytest
from swarph_cli.commands.board import _card_edit_payload


def test_priority_reaches_the_patch():
    p = _card_edit_payload("lab-ovh", None, None, priority=3)
    assert p == {"actor": "lab-ovh", "priority": 3}


def test_priority_zero_is_a_value_not_an_absence():
    # 0 is falsy and is a REAL priority; a truthiness test here would drop it.
    p = _card_edit_payload("lab-ovh", None, None, priority=0)
    assert p["priority"] == 0


def test_priority_absent_is_not_mentioned():
    # None means "not mentioned" — the key must not appear at all, or the PATCH
    # would rewrite priority on every title-only edit.
    p = _card_edit_payload("lab-ovh", "t", None)
    assert "priority" not in p


def test_priority_alone_is_not_an_empty_patch():
    # Before this change, `--priority` alone would have raised "nothing to edit".
    _card_edit_payload("lab-ovh", None, None, priority=7)  # must not raise


def test_out_of_range_is_passed_THROUGH_not_clamped():
    # The gateway owns 0..13 and answers 400. A CLI that clamped or refused here
    # would be a second definition of the range, which is #256b's defect.
    assert _card_edit_payload("lab-ovh", None, None, priority=99)["priority"] == 99


def test_still_refuses_the_truly_empty_patch():
    with pytest.raises(ValueError, match="--priority"):
        _card_edit_payload("lab-ovh", None, None)


def test_priority_composes_with_the_other_fields():
    p = _card_edit_payload("lab-ovh", "T", "B", priority=2)
    assert p == {"actor": "lab-ovh", "title": "T", "body": "B", "priority": 2}
