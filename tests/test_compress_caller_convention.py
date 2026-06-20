"""Regression: the compress model-call caller tags MUST satisfy swarph_shared's
caller convention (dotted `role.subrole`, not hyphenated). The existing
levers/verify tests inject a fake `chat` and never construct a real SwarphCall,
so they passed while the production path crashed at SwarphCall(...) on a
hyphenated caller ('swarph-compress'). This test checks the tags directly so
the break can't silently regress again."""
from swarph_shared.caller_convention import validate_caller

from swarph_cli.compress.levers import SHORTHAND_CALLER
from swarph_cli.compress.verify import VERIFY_EXPAND_CALLER


def test_shorthand_caller_matches_convention():
    # raises ValueError if it doesn't match ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$
    validate_caller(SHORTHAND_CALLER)
    assert SHORTHAND_CALLER == "swarph.compress"


def test_verify_expand_caller_matches_convention():
    validate_caller(VERIFY_EXPAND_CALLER)
    assert VERIFY_EXPAND_CALLER == "swarph.compress.verify"
