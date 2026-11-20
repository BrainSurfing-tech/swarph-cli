"""Tests for Card #1714 (Obligation #6099, Rework 2): Linear-time JWT redaction.

Verifies that ``_replace_jwts`` and ``redact_text`` preserve ``_JWT_RE``
match spans identically across the normal path, the sparse shadowed ``-eYJ``
path (original-text slicing), and the dense ``_JWT_ONCE_RE`` path, while
bounding regex input length to ``1 * len(text)`` and avoiding quadratic
backtracking on long multi-``-eyJ`` base64url runs.

All JWT-shaped test tokens are generated programmatically at runtime.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import swarph_cli.redact as redact_mod
from swarph_cli.redact import (
    _JWT_LONG_DASH_RE,
    _JWT_RE,
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


def _masked_run_chars(text: str) -> int:
    total = 0
    for m in _JWT_LONG_DASH_RE.finditer(text):
        start = m.start()
        end = m.end()
        if text.find("-eyJ", start + 4, end) != -1:
            total += end - start
    return total


def _assert_sparse_path(text: str) -> None:
    masked_chars = _masked_run_chars(text)
    assert 0 < masked_chars <= (len(text) >> 1)


def _assert_dense_path(text: str) -> None:
    masked_chars = _masked_run_chars(text)
    assert masked_chars > (len(text) >> 1)


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

    long_run = vals["long_run"]
    pad = "x" * 160

    # Sparse shadowed path (masked_chars <= len(text) >> 1)
    sparse_src = f"{src} {long_run} {src} {pad} tail"
    sparse_expected = f"{expected} {long_run} {expected} {pad} tail"
    _assert_sparse_path(sparse_src)
    _assert_matches_jwt_re_and_expected(sparse_src, sparse_expected)

    # Dense _JWT_ONCE_RE path (masked_chars > len(text) >> 1)
    dense_src = f"{src} {long_run} {long_run} {src} tail"
    dense_expected = f"{expected} {long_run} {long_run} {expected} tail"
    _assert_dense_path(dense_src)
    _assert_matches_jwt_re_and_expected(dense_src, dense_expected)


def test_tokens_before_inside_and_after_multi_start_runs() -> None:
    vals = _make_tokens()
    e = vals["e"]
    tok3 = vals["tok3"]
    long_run = vals["long_run"]
    sparse_pad = " " * 250 + "tail"

    # Token before, unredacted long run in middle, token after, plus trailing text
    src = f"{tok3} {long_run} {tok3} tail"
    expected = f"{MASK} {long_run} {MASK} tail"
    _assert_dense_path(src)
    _assert_matches_jwt_re_and_expected(src, expected)
    sparse_src = f"{src} {sparse_pad}"
    _assert_sparse_path(sparse_src)
    _assert_matches_jwt_re_and_expected(sparse_src, f"{expected} {sparse_pad}")

    # Heavy multi--eyJ run inside segment 2 (dense and sparse)
    seg2_heavy = f"{e}aaaa." + (e + "a" * 14 + "-") * 6 + e + "a"
    _assert_dense_path(seg2_heavy)
    _assert_matches_jwt_re_and_expected(seg2_heavy, MASK)
    _assert_sparse_path(f"{seg2_heavy} {sparse_pad}")
    _assert_matches_jwt_re_and_expected(f"{seg2_heavy} {sparse_pad}", f"{MASK} {sparse_pad}")

    # Heavy multi--eyJ run inside segment 3 (dense and sparse)
    seg3_heavy = f"{e}aaaa.{e}bbbb." + (e + "a" * 14 + "-") * 6 + e + "a"
    _assert_dense_path(seg3_heavy)
    _assert_matches_jwt_re_and_expected(seg3_heavy, MASK)
    _assert_sparse_path(f"{seg3_heavy} {sparse_pad}")
    _assert_matches_jwt_re_and_expected(f"{seg3_heavy} {sparse_pad}", f"{MASK} {sparse_pad}")

    # Multi-start run whose first eligible start is after "aaaa-" (dense and sparse)
    valid_prefixed_run = "aaaa-" + (e + "a-") * 15 + e + "a." + e + "bbbb"
    _assert_dense_path(valid_prefixed_run)
    _assert_matches_jwt_re_and_expected(valid_prefixed_run, f"aaaa-{MASK}")
    _assert_sparse_path(f"{valid_prefixed_run} {sparse_pad}")
    _assert_matches_jwt_re_and_expected(
        f"{valid_prefixed_run} {sparse_pad}", f"aaaa-{MASK} {sparse_pad}"
    )

    # Critical Unicode boundary case: ".eyJ----\u00e9" matches _JWT_RE (\b holds
    # between '-' and '\u00e9') on both sparse and dense paths.
    shadowed_boundary_match = "aaaa-" + (e + "a-") * 15 + e + "a." + e + "----\u00e9"
    _assert_dense_path(shadowed_boundary_match)
    _assert_matches_jwt_re_and_expected(shadowed_boundary_match, f"aaaa-{MASK}\u00e9")
    _assert_sparse_path(f"{shadowed_boundary_match} {sparse_pad}")
    _assert_matches_jwt_re_and_expected(
        f"{shadowed_boundary_match} {sparse_pad}", f"aaaa-{MASK}\u00e9 {sparse_pad}"
    )

    # Explicit two-start runs with tokens before, inside, and after, plus non-first
    # start ("aaaa-"), across all 3 boundary suffixes (kills Mutant 3 and Mutant 4
    # on BOTH sparse and dense paths).
    two_start_prefix = f"aaaa-{e}{'a' * 40}-{e}{'a' * 36}"
    for suffix, trail_after_mask in (
        (f".{e}----\u00e9", "\u00e9"),
        (f".{e}----.cccc", ""),
        (f".{e}bbbb", ""),
    ):
        dense_two_start = f"{tok3} {two_start_prefix}{suffix} {tok3} tail"
        dense_two_expected = f"{MASK} aaaa-{MASK}{trail_after_mask} {MASK} tail"
        _assert_dense_path(dense_two_start)
        _assert_matches_jwt_re_and_expected(dense_two_start, dense_two_expected)

        sparse_two_start = f"{dense_two_start} {sparse_pad}"
        sparse_two_expected = f"{dense_two_expected} {sparse_pad}"
        _assert_sparse_path(sparse_two_start)
        _assert_matches_jwt_re_and_expected(sparse_two_start, sparse_two_expected)

    # Dash, underscore, and digit in the first segment of a shadowed multi-start run
    for suffix, trail_after_mask in (
        (f".{e}bbbb", ""),
        (f".{e}----\u00e9", "\u00e9"),
    ):
        mixed_seg1 = f"aaaa-{e}a-b_7{'a' * 30}-{e}9_c-d{'a' * 30}{suffix}"
        mixed_expected = f"aaaa-{MASK}{trail_after_mask}"
        _assert_dense_path(mixed_seg1)
        _assert_matches_jwt_re_and_expected(mixed_seg1, mixed_expected)

        sparse_mixed = f"{mixed_seg1} {sparse_pad}"
        _assert_sparse_path(sparse_mixed)
        _assert_matches_jwt_re_and_expected(sparse_mixed, f"{mixed_expected} {sparse_pad}")

    # Combined failing seg2 run followed by valid multi-start run (dense and sparse)
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
    _assert_dense_path(combined)
    _assert_matches_jwt_re_and_expected(combined, expected_combined)
    _assert_sparse_path(f"{combined} {sparse_pad}")
    _assert_matches_jwt_re_and_expected(
        f"{combined} {sparse_pad}", f"{expected_combined} {sparse_pad}"
    )

    # Pre-existing "eYJ", all 81 "<a-char><b-char>Z" tags, and "X"*128+"Z" alongside
    # tokens and runs on BOTH sparse and dense paths (lossless preservation of "eYJ").
    tier2_tags = " ".join(f"{a}{b}Z" for a in "QXWKVBPFJ" for b in "QXWKVBPFJ")
    pre_existing = f"eYJ {tier2_tags} {'X' * 128}Z"

    sparse_tagged = f"{pre_existing} {long_run} {tok3} eYJ"
    sparse_tagged_expected = f"{pre_existing} {long_run} {MASK} eYJ"
    _assert_sparse_path(sparse_tagged)
    _assert_matches_jwt_re_and_expected(sparse_tagged, sparse_tagged_expected)

    dense_long_run = (e + "a" * 14 + "-") * 40 + e + "a"
    dense_tagged = f"{pre_existing} {dense_long_run} {tok3} eYJ"
    dense_tagged_expected = f"{pre_existing} {dense_long_run} {MASK} eYJ"
    _assert_dense_path(dense_tagged)
    _assert_matches_jwt_re_and_expected(dense_tagged, dense_tagged_expected)


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
    sparse_pad = " " * 250 + "tail"

    sparse_src = f"{long_run} {src} {long_run} {sparse_pad}"
    sparse_expected = f"{long_run} {expected} {long_run} {sparse_pad}"
    _assert_sparse_path(sparse_src)
    _assert_matches_jwt_re_and_expected(sparse_src, sparse_expected)

    dense_src = f"{long_run} {long_run} {src} {long_run} {long_run}"
    dense_expected = f"{long_run} {long_run} {expected} {long_run} {long_run}"
    _assert_dense_path(dense_src)
    _assert_matches_jwt_re_and_expected(dense_src, dense_expected)


def test_shadowed_seg2_seg3_trailing_unicode_backtrack() -> None:
    e = _E
    # Multi--eyJ run (>= 64 chars after first -eyJ) ending in -eyJX8w\u00e9,
    # forcing trailing \b to backtrack to between '-' and 'eyJX8w\u00e9'.
    tail_run = f"{e}a-" + f"{e}X8w-" * 10 + f"{e}X8w\u00e9"
    seg2_run = f"{e}aaaa.{tail_run}"
    seg3_run = f"{e}aaaa.{e}bbbb.{tail_run}"
    expected_tail = f"{MASK}{e}X8w\u00e9"
    sparse_pad = " " * 250 + "tail"

    for sample in (seg2_run, seg3_run):
        _assert_dense_path(sample)
        _assert_matches_jwt_re_and_expected(sample, expected_tail)

        sparse_sample = f"{sample} {sparse_pad}"
        _assert_sparse_path(sparse_sample)
        _assert_matches_jwt_re_and_expected(sparse_sample, f"{expected_tail} {sparse_pad}")

    # Pre-existing "eYJ", all 81 two-letter tags, and "XXXXZ" on BOTH sparse and dense
    tier12 = "eYJ " + " ".join(f"{a}{b}Z" for a in "QXWKVBPFJ" for b in "QXWKVBPFJ") + " XXXXZ"
    long_tail_run = f"{e}a-" + f"{e}X8w-" * 70 + f"{e}X8w\u00e9"
    dense_seg2 = f"{e}aaaa.{long_tail_run}"
    dense_seg3 = f"{e}aaaa.{e}bbbb.{long_tail_run}"

    for sparse_sample in (seg2_run, seg3_run):
        s = f"{tier12} {sparse_sample} eYJ"
        _assert_sparse_path(s)
        _assert_matches_jwt_re_and_expected(s, f"{tier12} {expected_tail} eYJ")

    for dense_sample in (dense_seg2, dense_seg3):
        s = f"{tier12} {dense_sample} eYJ"
        _assert_dense_path(s)
        _assert_matches_jwt_re_and_expected(s, f"{tier12} {expected_tail} eYJ")


def test_fast_path_guards_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaiseAll:
        def __getattr__(self, name: str):
            def _boom(*_args, **_kwargs):
                raise AssertionError(f"Unexpected regex call: .{name}")

            return _boom

    class _RegexProxy:
        def __init__(self, real, *, allow_sub: bool, allow_finditer: bool) -> None:
            self._real = real
            self._allow_sub = allow_sub
            self._allow_finditer = allow_finditer

        def sub(self, *args, **kwargs):
            if not self._allow_sub:
                raise AssertionError("Unexpected .sub() call")
            return self._real.sub(*args, **kwargs)

        def finditer(self, *args, **kwargs):
            if not self._allow_finditer:
                raise AssertionError("Unexpected .finditer() call")
            return self._real.finditer(*args, **kwargs)

    # Guard 1: if ".eyJ" not in text: return text
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_JWT_RE", _RaiseAll())
        mp.setattr(redact_mod, "_JWT_LONG_DASH_RE", _RaiseAll())
        mp.setattr(redact_mod, "_JWT_ONCE_RE", _RaiseAll())
        no_dot = f"{_E}aaaa-{_E}bbbb" * 5
        assert _replace_jwts(no_dot) == no_dot

    # Guard 2: if "-eyJ" not in text: return _JWT_RE.sub("[REDACTED:JWT]", text)
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_JWT_LONG_DASH_RE", _RaiseAll())
        mp.setattr(redact_mod, "_JWT_ONCE_RE", _RaiseAll())
        no_dash = f"{_E}aaaa.{_E}bbbb"
        assert _replace_jwts(no_dash) == MASK

    # Guard 3: if find("-eyJ", start + 4, end) == -1: continue (single -eyJ >= 40 chars)
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_JWT_RE",
            _RegexProxy(_JWT_RE, allow_sub=True, allow_finditer=False),
        )
        mp.setattr(redact_mod, "_JWT_ONCE_RE", _RaiseAll())
        single_long = f"-{_E}" + "a" * 50 + f" .{_E}bbbb"
        assert _replace_jwts(single_long) == single_long

    # Guard 4 (dense): if masked_chars > dense_limit: return _JWT_ONCE_RE.sub(...)
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_JWT_RE",
            _RegexProxy(_JWT_RE, allow_sub=False, allow_finditer=False),
        )
        dense_input = f"-{_E}-{_E}" + "a" * 40 + f" .{_E} x"
        _assert_dense_path(dense_input)
        assert _replace_jwts(dense_input) == dense_input

    # Guard 4 (sparse): masked_chars <= dense_limit enters _JWT_RE.finditer(masked)
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_JWT_ONCE_RE", _RaiseAll())
        mp.setattr(
            redact_mod,
            "_JWT_RE",
            _RegexProxy(_JWT_RE, allow_sub=False, allow_finditer=True),
        )
        sparse_input = f"-{_E}-{_E}" + "a" * 40 + " " * 120 + f".{_E} x"
        _assert_sparse_path(sparse_input)
        assert _replace_jwts(sparse_input) == sparse_input

    # Guard 5: if not runs: return _JWT_RE.sub(...) (short "-eyJ" < 36 chars)
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_JWT_RE",
            _RegexProxy(_JWT_RE, allow_sub=True, allow_finditer=False),
        )
        mp.setattr(redact_mod, "_JWT_ONCE_RE", _RaiseAll())
        short_dash = f"{_E}aaaa-{_E}bbbb.{_E}cccc"
        assert _replace_jwts(short_dash) == MASK


def test_regex_input_length_bounded_1x(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_lengths: list[tuple[str, str, int]] = []

    class _Recorder:
        def __init__(self, name: str, real) -> None:
            self._name = name
            self._real = real

        def finditer(self, string: str, *args, **kwargs):
            seen_lengths.append((self._name, "finditer", len(string)))
            return self._real.finditer(string, *args, **kwargs)

        def sub(self, repl, string: str, *args, **kwargs):
            seen_lengths.append((self._name, "sub", len(string)))
            return self._real.sub(repl, string, *args, **kwargs)

    monkeypatch.setattr(redact_mod, "_JWT_LONG_DASH_RE", _Recorder("long_dash", _JWT_LONG_DASH_RE))
    monkeypatch.setattr(redact_mod, "_JWT_RE", _Recorder("jwt", _JWT_RE))
    monkeypatch.setattr(redact_mod, "_JWT_ONCE_RE", _Recorder("once", redact_mod._JWT_ONCE_RE))

    e = _E
    tok3 = f"{e}aaaa.{e}bbbb.{e}cccc"
    tags = "eYJ " + " ".join(f"{a}{b}Z" for a in "QXWKVBPFJ" for b in "QXWKVBPFJ") + " "
    long_run = (e + "a" * 14 + "-") * 6 + e + "a"

    # Synthetic T1 (sparse with eYJ, 81 tags, X*4096+Z, and masked runs)
    t1_sparse = (
        tags
        + "X" * 4096
        + "Z "
        + (f"-{e}-{e}" + "a" * 32 + " ") * 10
        + f".{e} {tok3}"
    )
    _assert_sparse_path(t1_sparse)

    # Synthetic T2 (dense multi-unit with tags)
    t2_dense = f".{e} " + (f"-{e}-{e}" + "a" * 32 + " ") * 25 + tags + "X" * 128 + "Z"
    _assert_dense_path(t2_dense)

    # Synthetic T3 (sparse multi-start run with trailing Unicode backtrack)
    t3_sparse = (
        f"{tok3} "
        + (e + "a" * 14 + "-") * 10
        + e
        + f"a.{e}----\u00e9 "
        + " " * 300
        + tok3
    )
    _assert_sparse_path(t3_sparse)

    # Additional explicit sparse and dense cases
    sparse_case = f"{tok3} {long_run} {tok3} " + " " * 200 + "tail"
    _assert_sparse_path(sparse_case)
    dense_case = f"{tok3} {long_run} {long_run} {tok3}"
    _assert_dense_path(dense_case)

    for label, src in (
        ("T1", t1_sparse),
        ("T2", t2_dense),
        ("T3", t3_sparse),
        ("sparse", sparse_case),
        ("dense", dense_case),
    ):
        seen_lengths.clear()
        expected = _JWT_RE.sub(MASK, src)
        actual = _replace_jwts(src)
        assert actual == expected, f"Mismatch on {label}"
        assert seen_lengths, f"No regex calls recorded for {label}"
        max_seen = max(length for _, _, length in seen_lengths)
        assert max_seen <= len(src), (
            f"{label} exceeded 1x len(src)={len(src)}: {seen_lengths}"
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


def test_deadline_tag_exhaustion_t2_6097() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "n = 1_200_000\n"
        "tags = 'eYJ ' + ' '.join(a + b + 'Z' for a in 'QXWKVBPFJ' for b in 'QXWKVBPFJ') + ' '\n"
        "unit = f'-{e}-{e}' + 'a' * 32 + ' '\n"
        "tail = tags + 'X' * (n // 64) + 'Z'\n"
        "s = f'.{e} ' + unit * ((n - len(tail) - 5) // len(unit)) + tail\n"
        "assert _replace_jwts(s) == s\n"
    )


def test_deadline_sparse_multi_start_run() -> None:
    _run_deadline_script(
        "from swarph_cli.redact import _replace_jwts\n"
        "e = 'ey' + 'J'\n"
        "run = (e + 'a' * 19 + '-') * 18_000 + 'aaaa.' + e + ' x'\n"
        "s = run + ' ' * (len(run) + 10_000)\n"
        "assert _replace_jwts(s) == s\n"
    )
