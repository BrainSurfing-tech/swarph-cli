"""Tests for Card #1713 (Obligation #6077): Standalone token redaction boundary pins.

Pins the five unpinned paths in ``_STANDALONE_CANDIDATE_RE`` and
``_replace_standalone_tokens``:
1. Two or more closed ``[REDACTED:...]`` markers before a candidate.
2. One-character gaps between replaced candidates, including ``=``-backtracking.
3. Empty string input.
4. Non-Latin-1 (BMP > U+00FF and astral > U+FFFF) boundaries and markers.
5. Whole-string single candidate at index 0.

All high-entropy test tokens are generated programmatically at runtime.
"""
from __future__ import annotations

import pytest

from swarph_cli.redact import _replace_standalone_tokens, redact_text

MASK = "[REDACTED:HIGH_ENTROPY]"

_ALPHABETS = (
    "abcdefgh",
    "ijklmnop",
    "qrstuvwx",
    "yzabcdef",
)


def _synthetic_tokens() -> dict[str, str]:
    """Generate distinct synthetic high-entropy tokens with no provider prefixes."""
    tokens = {
        f"k{idx}": "".join(
            f"{c}{c.upper()}{(n + idx - 1) % 10}"
            for n, c in enumerate(alphabet, 1)
        )
        for idx, alphabet in enumerate(_ALPHABETS, 1)
    }
    tokens["mask"] = MASK
    return tokens


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Kills `while` -> `if` at redact.py:246 and `next_marker = -1` at :252.
        ("[REDACTED:a] [REDACTED:{k1}] {k2}", "[REDACTED:a] [REDACTED:{k1}] {mask}"),
        # Zero-gap adjacent closed markers; pins `marker_end + 1` at redact.py:252.
        ("[REDACTED:a][REDACTED:{k1}] {k2}", "[REDACTED:a][REDACTED:{k1}] {mask}"),
        # Zero-gap between marker close and following candidate; pins `start > marker_end`.
        ("[REDACTED:a]{k1}", "[REDACTED:a]{mask}"),
        # Sequential markers separated by a replaced candidate.
        ("[REDACTED:{k1}] {k2} [REDACTED:{k3}] {k4}", "[REDACTED:{k1}] {mask} [REDACTED:{k3}] {mask}"),
        # Three sequential closed markers before a candidate.
        ("[REDACTED:a] [REDACTED:b] [REDACTED:{k1}] {k2}", "[REDACTED:a] [REDACTED:b] [REDACTED:{k1}] {mask}"),
    ],
)
def test_multiple_closed_markers_before_candidate(before: str, after: str) -> None:
    values = _synthetic_tokens()
    src = before.format_map(values)
    expected = after.format_map(values)
    assert redact_text(src) == expected
    assert _replace_standalone_tokens(src) == expected


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("{k1} {k2}", "{mask} {mask}"),
        ("{k1}:{k2}", "{mask}:{mask}"),
        # `=*` backtracks to 0 `=` on k1, leaving `=` as separator.
        ("{k1}={k2}", "{mask}={mask}"),
        # `=*` consumes first `=` into `k1=`, leaving second `=` as separator.
        ("{k1}=={k2}", "{mask}={mask}"),
        # `=*` consumes two `=` into `k1==`, leaving third `=` as separator.
        ("{k1}==={k2}", "{mask}={mask}"),
        # Chained 1-char `=` gaps across three candidates.
        ("{k1}={k2}={k3}", "{mask}={mask}={mask}"),
    ],
)
def test_one_char_gaps_and_equals_backtracking(before: str, after: str) -> None:
    values = _synthetic_tokens()
    src = before.format_map(values)
    expected = after.format_map(values)
    assert redact_text(src) == expected
    assert _replace_standalone_tokens(src) == expected


def test_empty_string_returns_empty() -> None:
    assert redact_text("") == ""
    assert _replace_standalone_tokens("") == ""


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # BMP punctuation (> U+00FF): em-dash U+2014.
        ("{k1}\u2014{k2}", "{mask}\u2014{mask}"),
        # CJK (> U+4E00) around and inside [REDACTED:...].
        (
            "\u79d8\u5bc6[REDACTED:\u9375{k1}\u4e2d]\u5024{k2}\u7d42",
            "\u79d8\u5bc6[REDACTED:\u9375{k1}\u4e2d]\u5024{mask}\u7d42",
        ),
        # Astral emoji (> U+FFFF) around and inside [REDACTED:...].
        (
            "\U0001f511{k1}\U0001f512[REDACTED:\U0001f9ea{k2}\U0001f9ea]\U0001f680{k3}\U0001f3af",
            "\U0001f511{mask}\U0001f512[REDACTED:\U0001f9ea{k2}\U0001f9ea]\U0001f680{mask}\U0001f3af",
        ),
    ],
)
def test_non_latin1_unicode_boundaries_and_markers(before: str, after: str) -> None:
    values = _synthetic_tokens()
    src = before.format_map(values)
    expected = after.format_map(values)
    assert redact_text(src) == expected
    assert _replace_standalone_tokens(src) == expected


def test_whole_string_single_candidate() -> None:
    k1 = _synthetic_tokens()["k1"]
    assert redact_text(k1) == MASK
    assert _replace_standalone_tokens(k1) == MASK
    assert redact_text(f"{k1}==") == MASK
    assert _replace_standalone_tokens(f"{k1}==") == MASK
