"""#183b — `card` is a durable anchor kind on the CLI, and a card anchor is a card ID."""
import re
from pathlib import Path

import pytest

from swarph_cli.commands.schedule import DURABLE_ANCHOR_KEYS, parse_context_anchor

VENDORED = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "gateway" / "server.py"


def test_card_is_a_durable_key():
    assert "card" in DURABLE_ANCHOR_KEYS


def test_card_shorthand_emits_an_int_id():
    assert parse_context_anchor("card=183") == {"card": 183}


@pytest.mark.parametrize("raw", ["card=abc", "card=18a", "card="])
def test_non_numeric_card_fails_locally_naming_the_shape(raw):
    with pytest.raises(ValueError) as e:
        parse_context_anchor(raw)
    assert "card" in str(e.value)


def test_the_vendored_gateway_carries_the_same_key():
    src = VENDORED.read_text(encoding="utf-8")
    m = re.search(r"_DURABLE_ANCHOR_KEYS\s*=\s*frozenset\(\{([^}]*)\}\)", src)
    assert m and "card" in set(re.findall(r'"([a-z_]+)"', m.group(1)))
