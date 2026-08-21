"""The relayed rows and the local rows are NOT the same shape.

MEASURED 2026-08-21, on the live gateway, minutes after #552 merged:

    swarph codegraph _resolve_gateway --index /tmp/nope.db
    -> KeyError: 'docstring'  in format_human

>>> EVERY TEST IN #552 STUBBED THE GATEWAY WITH {"results": []}. <<< An EMPTY list
never reaches the formatter, so the one thing the relay actually changed -- the
SHAPE of a row -- was never exercised. 2389 tests passed, the reviewer approved, it
merged, and the first real invocation raised.

The stub agreed with the code about a contract neither had checked against the
producer. A fixture that cannot reach the failure is not evidence about it.

THE ROW SHAPES, both measured rather than assumed:

    gateway  repo name kind file_path start_line callers signature
    local    repo name kind file_path start_line callers signature docstring

So `docstring` is the ONLY divergence, and the fix is `.get` on that key alone.
Every other key stays subscripted on purpose: a missing `file_path` is a broken
payload, and formatting it as blank would convert a fault into a quiet half-answer.
"""

import pytest

from swarph_cli.commands import codegraph as cg

# Copied verbatim from a live POST /codegraph response. NOT hand-written -- the
# whole defect above came from inventing a fixture instead of capturing one.
GATEWAY_ROW = {
    "repo": "swarph-cli",
    "name": "_resolve_gateway",
    "kind": "function",
    "file_path": "src/swarph_cli/commands/highlight.py",
    "start_line": 35,
    "callers": 1,
    "signature": "(arg: str | None) -> str",
}


def test_format_human_accepts_a_real_relayed_row():
    """The regression itself: this raised KeyError('docstring') at 761e172."""
    out = cg.format_human([GATEWAY_ROW], "_resolve_gateway")
    assert "_resolve_gateway" in out
    assert "highlight.py:35" in out
    assert "(1 caller)" in out
    assert "(arg: str | None) -> str" in out


def test_local_rows_still_render_their_docstring():
    """The relay must not cost the local path its richer output."""
    local = dict(GATEWAY_ROW, docstring="Resolve the gateway.\nMore detail here.")
    out = cg.format_human([local], "_resolve_gateway")
    assert "Resolve the gateway." in out
    assert "More detail here." not in out, "only the first line is shown"


def test_a_structurally_broken_row_still_raises():
    """>>> THE LOOSENING IS DELIBERATELY ONE KEY WIDE. <<<

    Making every access forgiving would have fixed this crash and hidden the next
    one: a payload missing `file_path` would format as a blank location and read as
    a result. The refusal is the product -- a row that cannot be described must not
    be described badly.
    """
    broken = {k: v for k, v in GATEWAY_ROW.items() if k != "file_path"}
    with pytest.raises(KeyError):
        cg.format_human([broken], "x")
