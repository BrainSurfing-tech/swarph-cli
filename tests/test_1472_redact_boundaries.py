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


def _assert_scrub_and_redact(text: str, *secrets: str) -> None:
    scrubbed = scrub_text(text)
    assert scrubbed is not None
    redacted = redact_text(text, caller="t")
    for secret in secrets:
        assert secret not in scrubbed
        assert secret not in redacted
    assert "REDACTED" in scrubbed
    assert "REDACTED" in redacted


def test_aiza_dash_key_followed_by_word_char_is_redacted():
    """A 35th character of '-' then x, _, or 'abc def' (drop #4429).

    {35}(?![0-9A-Za-z_]) refuses that follower, so the key stays visible.
    Both scrub_text and redact_text have to take the dash.
    """
    secret = _aiza_trailing_dash()
    for suffix in ("x", "_", "abc def"):
        _assert_scrub_and_redact(secret + suffix, secret)


def test_dash_ending_aiza_then_api_key_hides_key_and_secret():
    """:<dash-ending key>api_key=<secret> leaves neither (drop #4437).

    The match stops on the key. A greedy tail would swallow api_key, and
    the secret would stay where the keyword-value rule should have put
    [REDACTED:DSN_CREDENTIAL].
    """
    key = _aiza_trailing_dash()
    secret = "plain-" + "secret-value"
    text = f":{key}api_key={secret}"
    scrubbed = scrub_text(text)
    assert scrubbed is not None
    assert key not in scrubbed
    assert secret not in scrubbed
    assert "api_key=[REDACTED]" in scrubbed
    redacted = redact_text(text, caller="t")
    assert key not in redacted
    assert secret not in redacted
    assert "api_key=[REDACTED:DSN_CREDENTIAL]" in redacted


def test_dash_ending_aiza_glued_through_chain_token_redacts_both_keys():
    """token=<dash-ending key>--chain_token<full key> leaves neither key.

    A match that stops on the first key leaves the second glued to the
    word character at the end of chain_token. Values are built here, with
    no digits, so the entropy layer cannot hide a miss.
    """
    dash_key = _aiza_trailing_dash()
    full_key = "AI" + "za" + ("n" * 35)
    assert len(full_key) == 39
    assert "-" not in full_key
    text = f"token={dash_key}--chain_token{full_key}"
    _assert_scrub_and_redact(text, dash_key, full_key)


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
