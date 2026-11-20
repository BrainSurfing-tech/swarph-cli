"""Tests for Card #1714 (Obligation #6095): Linear-time JWT redaction rework.

Verifies that ``_replace_jwts`` and ``redact_text`` preserve ``_JWT_RE``
match spans identically on both the normal path and the shadowed path, while
avoiding quadratic backtracking on long multi-``-eyJ`` base64url runs.

All JWT-shaped test tokens are generated programmatically at runtime.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.redact import (
    _JWT_LONG_DASH_RE,
    _JWT_RE,
    _jwt_shadow_tag,
    _replace_jwts,
    redact_text,
)

MASK = "[REDACTED:JWT]"
_E = "ey" + "J"


def _make_tokens() -> dict[str, str]:
    e = _E
    tok2 = f"{e}aaaa.{e}bbbb"
    tok3 = f"{e}aaaa.{e}bbbb.{e}cccc"
    long_run = (e + "a" * 14 + "-") * 6 + e + "a"
    return {
        "e": e,
        "tok2": tok2,
        "tok3": tok3,
        "mask": MASK,
        "long_run": long_run,
    }


def _assert_matches_jwt_re_and_expected(src: str, expected: str) -> None:
    assert _JWT_RE.sub(MASK, src) == expected
    assert _replace_jwts(src) == expected
    assert redact_text(src) == expected


@pytest.mark.parametrize(
    ("template", "expected_template"),
    [
        # Standard 2-segment and 3-segment shapes
        ("{tok2}", "{mask}"),
        ("{tok3}", "{mask}"),
        # Short 3rd segment (< 4 chars) leaves trailing .abc unredacted
        ("{tok2}.abc", "{mask}.abc"),
        # Base64 padding '=' is outside [A-Za-z0-9_-] so \b matches before ==
        ("{tok2}==", "{mask}=="),
        ("{tok3}==", "{mask}=="),
        # Glued/overlapping tokens
        ("{tok2}{tok2}", "{mask}"),
        ("{tok3}{tok3}", "{mask}.{mask}"),
        # Chained dot-separated tokens
        ("{tok2}.{tok2}", "{mask}.{e}bbbb"),
        ("{tok3}.{tok3}", "{mask}.{mask}"),
        # Dash, underscore, and digit in first segment
        ("{e}a-b_7.{e}bbbb", "{mask}"),
        ("{e}1234_5678-90.{e}bbbb", "{mask}"),
    ],
)
def test_jwt_shapes_normal_and_shadowed_paths(
    template: str, expected_template: str
) -> None:
    vals = _make_tokens()
    src = template.format_map(vals)
    expected = expected_template.format_map(vals)
    _assert_matches_jwt_re_and_expected(src, expected)

    # Execute the exact same case alongside a long multi--eyJ run so the
    # shadowed path in _replace_jwts actively runs.
    long_run = vals["long_run"]
    shadowed_src = f"{src} {long_run} {src} tail"
    shadowed_expected = f"{expected} {long_run} {expected} tail"
    _assert_matches_jwt_re_and_expected(shadowed_src, shadowed_expected)


def test_tokens_before_inside_and_after_multi_start_runs() -> None:
    vals = _make_tokens()
    e = vals["e"]
    tok3 = vals["tok3"]
    long_run = vals["long_run"]

    # Token before, unredacted long run in middle, token after, plus trailing text
    src = f"{tok3} {long_run} {tok3} tail"
    expected = f"{MASK} {long_run} {MASK} tail"
    _assert_matches_jwt_re_and_expected(src, expected)

    # Heavy multi--eyJ run inside segment 2
    seg2_heavy = f"{e}aaaa." + (e + "a" * 14 + "-") * 6 + e + "a"
    _assert_matches_jwt_re_and_expected(seg2_heavy, MASK)

    # Heavy multi--eyJ run inside segment 3
    seg3_heavy = f"{e}aaaa.{e}bbbb." + (e + "a" * 14 + "-") * 6 + e + "a"
    _assert_matches_jwt_re_and_expected(seg3_heavy, MASK)

    # Multi-start run whose first eligible start is after "aaaa-" and whose
    # valid seg2 matches _JWT_SEG2_OK_RE.
    valid_prefixed_run = "aaaa-" + (e + "a-") * 15 + e + "a." + e + "bbbb"
    _assert_matches_jwt_re_and_expected(valid_prefixed_run, f"aaaa-{MASK}")

    # Critical boundary case: ".eyJ----\u00e9" matches _JWT_RE (\b holds between
    # '-' and '\u00e9'), while _JWT_SEG2_OK_RE does NOT skip it, forcing the
    # shadowed path to mask 2nd+ -eyJ while keeping the first -eyJ intact.
    shadowed_boundary_match = "aaaa-" + (e + "a-") * 15 + e + "a." + e + "----\u00e9"
    _assert_matches_jwt_re_and_expected(
        shadowed_boundary_match, f"aaaa-{MASK}\u00e9"
    )

    # Explicit two-start shadowed run (>= 68 chars, exactly two -eyJ starts) with
    # tokens before, inside (via .eyJ----\u00e9), and after, plus non-first start.
    two_start_shadowed = (
        f"{tok3} aaaa-{e}{'a' * 36}-{e}{'a' * 32}.{e}----\u00e9 {tok3} tail"
    )
    _assert_matches_jwt_re_and_expected(
        two_start_shadowed, f"{MASK} aaaa-{MASK}\u00e9 {MASK} tail"
    )

    # Dash, underscore, and digit in the first segment of a shadowed multi-start run
    mixed_seg1_shadowed = (
        f"aaaa-{e}a-b_7{'a' * 30}-{e}9_c-d{'a' * 30}.{e}----\u00e9"
    )
    _assert_matches_jwt_re_and_expected(mixed_seg1_shadowed, f"aaaa-{MASK}\u00e9")

    # Sanity check: verify long_run and two_start_shadowed enter the shadowed path
    for sample in (long_run, two_start_shadowed):
        m = _JWT_LONG_DASH_RE.search(sample)
        assert m is not None
        assert sample.find("-eyJ", m.start() + 4, m.end()) != -1

    # Combined failing seg2 run followed by valid multi-start run
    combined = (
        "aaaa-"
        + e
        + "a"
        + ("-" + e + "a") * 15
        + "."
        + e
        + "---- "
        + "aaaa-"
        + e
        + "aaaa-"
        + e
        + "bbbb."
        + e
        + "cccc"
    )
    expected_combined = (
        "aaaa-"
        + e
        + "a"
        + ("-" + e + "a") * 15
        + "."
        + e
        + "---- "
        + f"aaaa-{MASK}"
    )
    _assert_matches_jwt_re_and_expected(combined, expected_combined)


@pytest.mark.parametrize(
    ("template", "expected_template"),
    [
        # Word-character prefix blocks \b on tok2; on tok3 seg2+seg3 matches
        ("x{tok2}", "x{tok2}"),
        ("x{tok3}", "x{e}aaaa.{mask}"),
        # Non-ASCII \w character (\u00e9) blocks \b (kills ASCII-only lookbehind)
        ("\u00e9{tok2}", "\u00e9{tok2}"),
        ("\u00e9{tok3}", "\u00e9{e}aaaa.{mask}"),
        # First start blocked by \u00e9, second start after '-' matches \beyJ
        ("\u00e9{e}aaaa-{e}bbbb.{e}cccc", "\u00e9{e}aaaa-{mask}"),
        # Trailing \u00e9 blocks \b after word char, but allows \b after '-'
        ("{e}aaaa.{e}bbbb\u00e9", "{e}aaaa.{e}bbbb\u00e9"),
        ("{e}aaaa.{e}bbbb-\u00e9", "{mask}\u00e9"),
    ],
)
def test_word_and_non_ascii_boundaries_normal_and_shadowed(
    template: str, expected_template: str
) -> None:
    vals = _make_tokens()
    src = template.format_map(vals)
    expected = expected_template.format_map(vals)
    _assert_matches_jwt_re_and_expected(src, expected)

    long_run = vals["long_run"]
    shadowed_src = f"{long_run} {src} {long_run}"
    shadowed_expected = f"{long_run} {expected} {long_run}"
    _assert_matches_jwt_re_and_expected(shadowed_src, shadowed_expected)


def test_shadow_tag_collision_fallbacks() -> None:
    vals = _make_tokens()
    tok3 = vals["tok3"]
    long_run = vals["long_run"]

    tier2_list = [f"{a}{b}Z" for a in "QXWKVBPFJ" for b in "QXWKVBPFJ"]
    assert len(tier2_list) == 81
    tier2_tags = " ".join(tier2_list)

    # Direct unit checks on pure-word shadow tag tiers (without leading hyphens)
    assert _jwt_shadow_tag("clean text") == "eYJ"
    assert _jwt_shadow_tag("eYJ") == "QQZ"
    assert _jwt_shadow_tag("eYJ QQZ") == "QXZ"
    assert _jwt_shadow_tag(f"eYJ {tier2_tags}") == "XXXXZ"
    assert _jwt_shadow_tag(f"eYJ {tier2_tags} XXXXZ") == "XXXXXXXXZ"

    # Tier 1 collision: literal "eYJ" (without leading hyphen) already in text
    src_tier1 = f"eYJ {long_run} {tok3} eYJ"
    expected_tier1 = f"eYJ {long_run} {MASK} eYJ"
    _assert_matches_jwt_re_and_expected(src_tier1, expected_tier1)

    # Tier 2 collision: "eYJ" and "QQZ" (without leading hyphens) already in text
    src_tier2 = f"eYJ QQZ {long_run} {tok3} QQZ"
    expected_tier2 = f"eYJ QQZ {long_run} {MASK} QQZ"
    _assert_matches_jwt_re_and_expected(src_tier2, expected_tier2)

    # Tier 2 exhaustion -> Tier 3 fallback ("XXXXZ"): literal "eYJ" and all
    # 81 "<a-char><b-char>Z" tags already present in text.
    src_tier3 = f"eYJ {tier2_tags} {long_run} {tok3}"
    expected_tier3 = f"eYJ {tier2_tags} {long_run} {MASK}"
    _assert_matches_jwt_re_and_expected(src_tier3, expected_tier3)

    # Tier 3 width doubling -> "XXXXXXXXZ": "XXXXZ" also present in text
    src_tier3_wide = f"eYJ {tier2_tags} XXXXZ {long_run} {tok3}"
    expected_tier3_wide = f"eYJ {tier2_tags} XXXXZ {long_run} {MASK}"
    _assert_matches_jwt_re_and_expected(src_tier3_wide, expected_tier3_wide)


def test_shadowed_seg2_seg3_trailing_unicode_backtrack() -> None:
    e = _E
    # Multi--eyJ run (>= 64 chars after first -eyJ) ending in -eyJX8w\u00e9,
    # forcing _JWT_RE to backtrack trailing \b to between '-' and 'eyJX8w\u00e9'.
    tail_run = f"{e}a-" + f"{e}X8w-" * 10 + f"{e}X8w\u00e9"
    seg2_run = f"{e}aaaa.{tail_run}"
    seg3_run = f"{e}aaaa.{e}bbbb.{tail_run}"

    for sample in (seg2_run, seg3_run):
        m = _JWT_LONG_DASH_RE.search(sample)
        assert m is not None
        assert sample.find("-eyJ", m.start() + 4, m.end()) != -1

    expected_tail = f"{MASK}{e}X8w\u00e9"

    # Tier 1 ("eYJ")
    _assert_matches_jwt_re_and_expected(seg2_run, expected_tail)
    _assert_matches_jwt_re_and_expected(seg3_run, expected_tail)

    # Tier 1 collision -> Tier 2 tag ("QQZ")
    _assert_matches_jwt_re_and_expected(f"eYJ {seg2_run} eYJ", f"eYJ {expected_tail} eYJ")
    _assert_matches_jwt_re_and_expected(f"eYJ {seg3_run} eYJ", f"eYJ {expected_tail} eYJ")

    # Tier 1 + all 81 Tier 2 tags -> Tier 3 ("XXXXZ")
    tier12 = "eYJ " + " ".join(f"{a}{b}Z" for a in "QXWKVBPFJ" for b in "QXWKVBPFJ")
    _assert_matches_jwt_re_and_expected(f"{tier12} {seg2_run}", f"{tier12} {expected_tail}")
    _assert_matches_jwt_re_and_expected(f"{tier12} {seg3_run}", f"{tier12} {expected_tail}")

    # Tier 3 width doubling ("XXXXXXXXZ")
    _assert_matches_jwt_re_and_expected(
        f"{tier12} XXXXZ {seg2_run}", f"{tier12} XXXXZ {expected_tail}"
    )
    _assert_matches_jwt_re_and_expected(
        f"{tier12} XXXXZ {seg3_run}", f"{tier12} XXXXZ {expected_tail}"
    )


def _run_deadline_script(body: str) -> None:
    subprocess.run(
        [sys.executable, "-c", body],
        cwd=Path(__file__).resolve().parents[1] / "src",
        check=True,
        timeout=5,
    )


def test_deadline_plain_dash_joiner_j02() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "s = (e + 'a' * 19 + '-') * 18_000 + 'aaaa.' + e + ' x'\n"
        "assert _replace_jwts(s) == s\n"
    )


def test_deadline_underscore_dash_joiner_j01_full_redact() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import redact_text\n"
        "e = 'ey' + 'J'\n"
        "prefix = (e + 'a' * 19 + '_-') * 20_000 + ' '\n"
        "tok = f'{e}aaaa.{e}bbbb.{e}cccc'\n"
        "assert redact_text(prefix + tok) == prefix + '[REDACTED:JWT]'\n"
    )


def test_deadline_digit_separated_run() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "s = (e + '1234567890' * 2 + '-') * 18_000 + '1234.' + e + ' x'\n"
        "assert _replace_jwts(s) == s\n"
    )


def test_deadline_run_not_first_in_text_j27() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "s = 'word ' + (e + 'a' * 19 + '-') * 18_000 + 'aaaa.' + e + ' x'\n"
        "assert _replace_jwts(s) == s\n"
    )


def test_deadline_no_dot_flood_j00() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "s = (e + 'a' * 19 + '-') * 20_000\n"
        "assert _replace_jwts(s) == s\n"
    )


def test_deadline_failing_seg2_boundaries() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "s1 = (e + 'a' * 19 + '-') * 18_000 + 'aaaa.' + e + '---- '\n"
        "assert _replace_jwts(s1) == s1\n"
        "s2 = (e + 'a' * 19 + '-') * 18_000 + 'aaaa.' + e + 'bbbb\\u00e9 '\n"
        "assert _replace_jwts(s2) == s2\n"
    )
