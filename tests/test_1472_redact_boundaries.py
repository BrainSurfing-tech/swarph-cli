"""Card #1472: pasted chain_token text stays scrubbed, and a trailing-dash
AIza key is scrubbed too.

Every secret is built at runtime. No realistic key literal.
"""
from __future__ import annotations

import json

from swarph_cli.redact import encode_outbound_json, redact_text
from swarph_cli.scrub import scrub_text


def _sk_proj() -> str:
    return "sk-proj-" + ("n7" * 12)


def _ghp() -> str:
    return "ghp_" + ("Ab" * 16)


def _aiza_trailing_dash() -> str:
    # AIza + 35 chars, last character '-'. No digits, so the entropy
    # layer does not also eat it: only the AIza rule can.
    secret = "AI" + "za" + ("m" * 34) + "-"
    assert len(secret) == 39
    assert secret.endswith("-")
    return secret


def test_dm_body_pasted_chain_token_line_is_fully_redacted():
    """Drop's measured leak: keys sit inside a pasted "chain_token": "..." value.

    encode_outbound_json is what mesh._post_json sends. Both keys must be
    gone from those bytes. A shield that puts the quoted span back fails this.
    """
    sk = _sk_proj()
    ghp = _ghp()
    content = (
        'pasted log "chain_token": "\n'
        f"export OPENAI_API_KEY={sk}\n"
        f"GITHUB={ghp}\n"
        '" end'
    )
    followed = f"chain_token: note\n{sk}\n{ghp}"
    for text in (content, followed):
        body = {
            "from_node": "friendly-coder",
            "to_node": "lab-ovh",
            "kind": "answer",
            "content": text,
        }
        raw = encode_outbound_json(body, caller="t").decode("utf-8")
        assert sk not in raw
        assert ghp not in raw
        assert "REDACTED" in raw


def test_redact_text_redacts_chain_token_json_holding_an_sk_proj_key():
    sk = _sk_proj()
    blob = '{"chain_token": "' + sk + '"}'
    out = redact_text(blob, caller="t")
    assert sk not in out
    assert "REDACTED" in out


def test_chain_placeholder_literal_is_left_alone():
    literal = "SWARPH.CHAIN.0.END"
    assert redact_text(f"see {literal} here", caller="t") == f"see {literal} here"
    sk = _sk_proj()
    text = literal + '\n{"chain_token": "' + sk + '"}'
    out = redact_text(text, caller="t")
    assert out.startswith(literal)
    assert sk not in out
    assert json.loads(out.split("\n", 1)[1])["chain_token"] != sk


def test_aiza_key_then_hyphen_suffix_is_redacted():
    """A '-' after the 35-character body is a boundary, not a longer tail.

    key-x and key-- must be scrubbed. Putting '-' back into the lookahead
    (?![0-9A-Za-z\\-_]) hides both, because the next character is '-'.
    """
    secret = "AI" + "za" + ("m" * 35)
    assert len(secret) == 39
    assert not secret.endswith("-")
    for suffix in ("-x", "--"):
        text = secret + suffix
        scrubbed = scrub_text(text)
        assert scrubbed is not None
        assert secret not in scrubbed
        assert "REDACTED" in scrubbed
        redacted = redact_text(text, caller="t")
        assert secret not in redacted
        assert "REDACTED" in redacted


def test_aiza_key_ending_in_dash_is_redacted_before_space_quote_or_punctuation():
    secret = _aiza_trailing_dash()
    samples = [
        f"prefix {secret} suffix",
        f'"{secret}"',
        f"'{secret}'",
        secret,
        f"{secret}.",
        f"{secret},",
        f"({secret})",
        f"{secret}\nnext",
    ]
    for text in samples:
        scrubbed = scrub_text(text)
        assert scrubbed is not None
        assert secret not in scrubbed
        assert "REDACTED" in scrubbed
        redacted = redact_text(text, caller="t")
        assert secret not in redacted
        assert "REDACTED" in redacted
