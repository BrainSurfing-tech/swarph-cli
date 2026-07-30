"""Card #146 — `swarph schedule create` could never succeed.

>>> THE CLI SENT --context VALUES AS RAW STRINGS. THE GATEWAY REQUIRES EACH ANCHOR TO
    BE A DICT CARRYING A DURABLE KEY, AND REQUIRES THE LIST TO BE NON-EMPTY. SO EVERY
    INVOCATION 400'd, UNCONDITIONALLY. <<<

The cost was not the broken verb. It was that the verb which makes a DATE FIRE was
itself broken, so arming a reminder always cost a hand-written raw-API call — and the
cheapest path was therefore to tell a peer in a DM and hope. Board cards #129/#168
(stale dependency gates) and the graduation register are downstream of exactly this:
dates lived in conversations because the mechanism for dates did not work.

This file pins the CONTRACT, not the implementation:
  · the parser produces the dict shape the gateway accepts
  · a non-durable / malformed anchor fails LOCALLY, naming the valid keys
  · an EMPTY context list fails locally rather than as a server 400
  · the CLI's copy of the durable-key set MATCHES THE GATEWAY'S — the drift check,
    because a duplicated constant nobody compares is how the two sides diverged
"""
import json
import re
from pathlib import Path

import pytest

from swarph_cli.commands.schedule import DURABLE_ANCHOR_KEYS, parse_context_anchor


# ── the shape the gateway actually wants ────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("memory=project_graduation_register", {"memory": "project_graduation_register"}),
    ("repo=swarph-cli",                    {"repo": "swarph-cli"}),
    ("channel=System",                     {"channel": "System"}),
    ("feature=scheduler",                  {"feature": "scheduler"}),
    ("file=docs/PLAN.md",                  {"file": "docs/PLAN.md"}),
    ('{"memory": "x"}',                    {"memory": "x"}),
])
def test_shorthand_and_json_both_produce_anchor_dicts(raw, want):
    assert parse_context_anchor(raw) == want


def test_the_old_behaviour_is_exactly_what_the_gateway_rejects():
    """>>> THE REGRESSION PIN. The pre-fix CLI passed the raw string through. <<<

    The gateway's check is `not isinstance(anchor, dict) or not (set(anchor) &
    DURABLE_ANCHOR_KEYS)`, so a bare string failed the FIRST clause. If someone
    "simplifies" the parser back to a pass-through, this fails instead of the verb
    silently 400-ing again for another two months.
    """
    got = parse_context_anchor("memory=x")
    assert isinstance(got, dict), "a raw string is what made this verb unusable"
    assert set(got) & DURABLE_ANCHOR_KEYS


# ── failing LOCALLY, with the input the user is holding ─────────────────────

@pytest.mark.parametrize("bad", [
    "project_x",                 # the natural thing to type — no key at all
    "session=abc",               # a real key name, but not a durable one
    "memory=",                   # key with no value
    "",                          # empty
    "{not json}",                # looks like JSON, is not
    '["memory"]',                # JSON, but not an object
    '{"note": "x"}',             # object, but no durable key
])
def test_bad_anchors_raise_locally_and_name_the_valid_keys(bad):
    with pytest.raises(ValueError) as e:
        parse_context_anchor(bad)
    msg = str(e.value)
    assert bad[:20] in msg or "durable" in msg or "empty" in msg, msg


def test_the_error_lists_the_actual_keys_not_a_vague_rule():
    """A 400 from the gateway names the RULE; it does not name the INPUT that broke
    it, and the user is the one holding the input. The local error must do both."""
    with pytest.raises(ValueError) as e:
        parse_context_anchor("session=abc")
    msg = str(e.value)
    assert "session" in msg, "the error must quote the offending key"
    for k in ("memory", "repo", "channel", "feature", "file"):
        assert k in msg, f"the error must list {k!r} as a valid alternative"


# ── the drift check: the two sides must agree ───────────────────────────────

GATEWAY = Path("/home/ubuntu/mesh-gateway/server.py")


@pytest.mark.skipif(not GATEWAY.exists(), reason="mesh-gateway source not on this box")
def test_the_cli_durable_keys_match_the_gateway_the_client_talks_to():
    """>>> THE CHECK THAT WOULD HAVE CAUGHT #146 AT BIRTH. <<<

    Two codebases, one contract, and no test comparing them — so the CLI could send a
    shape the gateway had never accepted and nothing said a word until a human tried
    to arm a real event and got a 400 they had to reverse-engineer.

    Skipped where mesh-gateway is absent (most CI runners), and SKIPPED MEANS NOT
    VERIFIED, not verified-OK — pytest counts and prints it. On the lab host, where
    both live, it runs and is authoritative.
    """
    src = GATEWAY.read_text(encoding="utf-8")
    m = re.search(r"_DURABLE_ANCHOR_KEYS\s*=\s*frozenset\(\{([^}]*)\}\)", src)
    assert m, "could not locate _DURABLE_ANCHOR_KEYS in the gateway — the drift check is blind"
    gateway_keys = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert gateway_keys == set(DURABLE_ANCHOR_KEYS), (
        f"CLI and gateway disagree on durable anchor keys: "
        f"cli={sorted(DURABLE_ANCHOR_KEYS)} gateway={sorted(gateway_keys)}")


@pytest.mark.skipif(not GATEWAY.exists(), reason="mesh-gateway source not on this box")
def test_anchors_pass_the_GATEWAYS_OWN_validator_not_a_local_copy():
    """>>> THE END-TO-END CHECK, AND THE ONLY ONE THAT ACTUALLY PROVES THE FIX. <<<

    A live POST cannot prove it: the gateway checks AUTHZ (server.py ~3707) BEFORE it
    validates the payload (~3728), so a non-operator caller gets an identical 403 for
    a well-formed and a malformed body. The measurement returns the same value whether
    the fix works or not — which is exactly the proxy this card's whole family is about,
    and it means the CLI's correctness is UNTESTABLE end-to-end by most callers.

    So instead of a mock or a local re-implementation, this executes the DEPLOYED
    gateway's own `_anchor_is_durable` against what the parser actually emits. Real
    function, real contract, no server and no token.
    """
    import re as _re
    src = GATEWAY.read_text(encoding="utf-8")
    ns = {}
    keys = _re.search(r"_DURABLE_ANCHOR_KEYS\s*=\s*frozenset\(\{[^}]*\}\)", src)
    fn = _re.search(r"def _anchor_is_durable\(anchor: dict\) -> bool:.*?\n    return True\n", src, _re.S)
    assert keys and fn, "could not extract the gateway validator — this check is blind"
    exec(keys.group(0), ns)
    exec(fn.group(0), ns)
    is_durable = ns["_anchor_is_durable"]

    for raw in ("memory=project_x", "repo=swarph-cli", "channel=System",
                "feature=scheduler", "file=docs/PLAN.md", '{"memory": "x"}'):
        assert is_durable(parse_context_anchor(raw)), f"gateway would reject {raw!r}"

    # and the pre-fix behaviour must still be rejected by that same validator
    assert not is_durable("memory=x"), (
        "a bare string is what the unpatched CLI sent; if the gateway now accepts it, "
        "this test is measuring the wrong contract")
