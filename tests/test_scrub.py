"""Shared outbound scrub (card #1073, lab decision msg 65982).

swarph_cli.scrub is the one source: the triage router imports it and
hippocampus imports it from the installed package. Fake secrets must
never survive it; unscrubbable input reads as None (fail closed).
"""
from __future__ import annotations

import pytest

from swarph_cli.scrub import scrub_text


@pytest.mark.parametrize("secret", [
    "sk-ant-aaaa-bbbb-cccc-dddd",
    "sk-abcdefghijklmnop123456",
    "xai-abcdefghijklmnop123456",
    # Built at runtime: the literal full shape trips gitleaks gcp-api-key.
    "AIza" + "SyDdI0T9mbB4y7KzQ3wX2vU5tS8rR1pP6oO",
    "ghp_abcdefghijklmnopqrstuvwx1234567890",
    # Built at runtime: the literal full shape trips push protection.
    "xoxb-" + "123456789012-abcdefghijklmnop",
    "Bearer abcDEF123-456_789.~+/==",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dummy-sig",
    "AKIAIOSFODNN7EXAMPLE",
    "password=hunter2-super-secret",
])
def test_known_secret_shapes_do_not_survive(secret: str):
    out = scrub_text(f"prefix {secret} suffix")
    assert out is not None
    assert secret not in out
    assert "REDACTED" in out


def test_pem_block_does_not_survive():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIBogus\n"
           "-----END RSA PRIVATE KEY-----")
    out = scrub_text(pem)
    assert "MIIBogus" not in (out or "")


def test_plain_text_passes_through():
    assert scrub_text("Build the router, no secrets here.") == \
        "Build the router, no secrets here."


@pytest.mark.parametrize("bad", [None, b"\x00\x01", {"k": "v"}, 42])
def test_unscrubbable_input_is_none(bad):
    assert scrub_text(bad) is None


def test_router_uses_the_shared_source():
    """No copy: triage.router.scrub_payload IS swarph_cli.scrub.scrub_text."""
    from swarph_cli.triage import router
    assert router.scrub_payload is scrub_text
