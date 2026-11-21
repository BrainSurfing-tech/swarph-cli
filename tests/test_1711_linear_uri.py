"""Tests for Card #1711 (Obligation #6172): Linear-time URI scheme redaction.

Verifies that ``_replace_scheme_credentials`` and ``redact_text`` preserve
``_URI_SCHEME_RE`` match spans and output identically across the normal path,
fast-path guards, and the length-preserving masked multi-start run path, while
avoiding quadratic backtracking on long multi-start scheme-character runs.

All URI-shaped test inputs use synthetic values constructed at runtime.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import swarph_cli.redact as redact_mod
from swarph_cli.redact import (
    _URI_SCHEME_RE,
    _URI_SCHEME_TRIGGER_RE,
    _replace_scheme_credentials,
    redact_text,
)

TEMPLATE = r"\1[REDACTED:URI_CREDENTIAL]\3"
MASK = "[REDACTED:URI_CREDENTIAL]"
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _assert_matches_uri_re_and_expected(src: str, expected: str) -> None:
    assert _URI_SCHEME_RE.sub(TEMPLATE, src) == expected
    assert _replace_scheme_credentials(src) == expected
    assert redact_text(src) == expected


def _assert_matches_uri_re(src: str) -> None:
    expected = _URI_SCHEME_RE.sub(TEMPLATE, src)
    assert _replace_scheme_credentials(src) == expected


@pytest.mark.parametrize(
    ("scheme", "lead"),
    [
        ("s3", ""),
        ("neo4j", ""),
        ("h2", ""),
        ("db2", ""),
        ("amqp0-9-1", ""),
        ("a.b", ""),
        ("a.9", ""),
        ("a-b", ""),
        ("a-9", ""),
        ("git+https", ""),
        ("git+7https", ""),
        ("s+9", ""),
        ("git+ssh", ""),
        ("S3+x.y-9", ""),
        ("s3", "a-" * 20),
        ("neo4j", "a-" * 20),
        ("h2", "a-" * 20),
        ("db2", "a-" * 20),
        ("amqp0-9-1", "a-" * 20),
        ("a.b", "a-" * 20),
        ("a.9", "a-" * 20),
        ("a-b", "a." * 20),
        ("a-9", "a." * 20),
        ("git+https", "a-" * 20),
        ("git+7https", "a-" * 20),
        ("s+9", "a-" * 20),
        ("s3", "7" * 40 + "-"),
        ("9-b", "a-" * 20),
        ("s3", "a." * 20),
        ("s3", "a+" * 20),
    ],
)
def test_scheme_character_classes_normal_and_long_run_paths(
    scheme: str, lead: str
) -> None:
    """Pins digits (0-9), dot (.), dash (-), and plus (+) on both paths."""
    src = f"note {lead}{scheme}://user1:pw_secret@host.example.com/path tail"
    expected = f"note {lead}{scheme}://user1:{MASK}@host.example.com/path tail"
    _assert_matches_uri_re_and_expected(src, expected)


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # ASCII and Unicode word boundaries before scheme / run
        ("_s://u:p@h", "_s://u:p@h"),
        ("7s://u:p@h", "7s://u:p@h"),
        ("\u00e9s://u:p@h", "\u00e9s://u:p@h"),
        ("\u00e9" + "a-" * 15 + "://u:p@h", "\u00e9" + "a-" * 15 + f"://u:{MASK}@h"),
        ("\u00e9" + "a-" * 20 + "://u:p@h", "\u00e9" + "a-" * 20 + f"://u:{MASK}@h"),
        ("\u00e9" + "a" * 25 + "-b-c://u:p@h", "\u00e9" + "a" * 25 + f"-b-c://u:{MASK}@h"),
        ("\u00e9" + "a" * 25 + "-b" + "c-" * 12 + "://u:p@h", "\u00e9" + "a" * 25 + "-b" + "c-" * 12 + f"://u:{MASK}@h"),
        ("\u00e9" + "-a" + "b-" * 12 + "://u:p@h", "\u00e9" + "-a" + "b-" * 12 + f"://u:{MASK}@h"),
        ("\u00e9" + "a" * 35 + "-b://u:p@h", "\u00e9" + "a" * 35 + f"-b://u:{MASK}@h"),
        ("7" + "a-" * 20 + "://u:p@h", "7" + "a-" * 20 + f"://u:{MASK}@h"),
        ("_" + "a-" * 20 + "://u:p@h", "_" + "a-" * 20 + f"://u:{MASK}@h"),
        ("-" + "a-" * 20 + "://u:p@h", "-" + "a-" * 20 + f"://u:{MASK}@h"),
        # Trailing separator at e - 1 (no internal separator in [s, e - 1))
        ("-a" + "b" * 18 + "-://u:p@h", "-a" + "b" * 18 + f"-://u:{MASK}@h"),
        ("-a" + "b" * 18 + "+://u:p@h", "-a" + "b" * 18 + f"+://u:{MASK}@h"),
        ("-a" + "b" * 18 + ".://u:p@h", "-a" + "b" * 18 + f".://u:{MASK}@h"),
        ("-a" + "b" * 25 + "-://x u:p@h", "-a" + "b" * 25 + "-://x u:p@h"),
        ("a-b" + "c" * 18 + "-://u:p@h", "a-b" + "c" * 18 + f"-://u:{MASK}@h"),
        ("a-b" + "c" * 18 + "d-://u:p@h", "a-b" + "c" * 18 + f"d-://u:{MASK}@h"),
        # Threshold boundaries around 20..32 chars
        ("a-" * 15 + "a://u:p@h", "a-" * 15 + f"a://u:{MASK}@h"),
        ("a-" * 16 + "://u:p@h", "a-" * 16 + f"://u:{MASK}@h"),
        ("a-" * 16 + "a://u:p@h", "a-" * 16 + f"a://u:{MASK}@h"),
        # [REDACTED negative lookahead
        ("s://u:[REDACTED:URI_CREDENTIAL]@h", "s://u:[REDACTED:URI_CREDENTIAL]@h"),
        ("s://u:[REDACTED:X]@h", "s://u:[REDACTED:X]@h"),
        ("a-" * 20 + "://u:[REDACTED:X]@h", "a-" * 20 + "://u:[REDACTED:X]@h"),
        ("a-" * 20 + "://u:[REDACTED@h", "a-" * 20 + "://u:[REDACTED@h"),
        # Host consuming into a subsequent long masked run and vice versa
        (
            "s://u:p@" + "a-" * 20 + "://v:q@w",
            f"s://u:{MASK}@" + "a-" * 20 + "://v:q@w",
        ),
        (
            "a-" * 20 + "://u:p@" + "b." * 20 + "://v:q@w",
            "a-" * 20 + f"://u:{MASK}@" + "b." * 20 + "://v:q@w",
        ),
        # Pre-existing zeros alongside masked + / . / -
        (
            "a0-" * 15 + "://u:p@h " + "b0." * 15 + "://v:q@w",
            "a0-" * 15 + f"://u:{MASK}@h " + "b0." * 15 + f"://v:{MASK}@w",
        ),
        # Glued / adjacent URIs and userinfo edge cases
        ("s://u:p@h/a://v:q@w", f"s://u:{MASK}@h/a://v:{MASK}@w"),
        ("s://u:p:q@h", f"s://u:{MASK}@h"),
        ("s://u:@h", "s://u:@h"),
        ("s://:p@h", "s://:p@h"),
        ("s://u@h", "s://u@h"),
        ("s://u:p/@h", "s://u:p/@h"),
        ("s://u/:p@h", "s://u/:p@h"),
        # Literal backslashes inside groups must not be treated as regex escapes
        (
            "a-" * 20 + "://u\\1:p\\2@h\\3",
            "a-" * 20 + f"://u\\1:{MASK}@h\\3",
        ),
        # Long '-' run with no letters before a real URI (tests bounded search)
        ("-" * 40 + " a://u:p@h", "-" * 40 + f" a://u:{MASK}@h"),
    ],
)
def test_exact_equivalence_edge_cases(src: str, expected: str) -> None:
    _assert_matches_uri_re_and_expected(src, expected)


def test_long_run_with_urischeme_only_equivalence() -> None:
    cases = [
        "-" * 50 + "://u:p@h",
        "\u00e9" + "a" * 50 + "://u:p@h",
        "\u00e9" + "a-" * 15 + "://x u:p@h",
        "7" * 50 + "-a://u:p@h",
        "a" * 40 + "-://u:p@h",
        "a-" * 20 + "0" * 40 + "://u:p@h",
        "s://u:p@h " + "a-" * 30 + " x://v:q@w",
        "a-" * 30 + "://x " + "b." * 30 + "://u:p@h",
    ]
    for src in cases:
        _assert_matches_uri_re(src)


@pytest.mark.parametrize("tail_len", range(16, 23))
def test_trigger_threshold_boundary_equivalence(tail_len: int) -> None:
    """Sweeps 16..22 scheme chars after ``[+.-][a-zA-Z]`` across the {18,} threshold."""
    alt_dot = "b." * (tail_len // 2) + "b" * (tail_len % 2)
    alt_dash = "b-" * (tail_len // 2) + "b" * (tail_len % 2)
    cases = [
        ("a-" + "b" * tail_len + "://u:p@h", "a-" + "b" * tail_len + f"://u:{MASK}@h"),
        ("-a" + "-" * tail_len + "://u:p@h", "-a" + "-" * tail_len + f"://u:{MASK}@h"),
        ("a-" + alt_dot + "://u:p@h", "a-" + alt_dot + f"://u:{MASK}@h"),
        ("-a" + alt_dash + "://u:p@h", "-a" + alt_dash + f"://u:{MASK}@h"),
        ("-a" + alt_dash + "://x u:p@h", "-a" + alt_dash + "://x u:p@h"),
        ("\u00e9a-" + alt_dot + "://u:p@h", "\u00e9a-" + alt_dot + f"://u:{MASK}@h"),
        ("-a" + "b" * tail_len + "-://u:p@h", "-a" + "b" * tail_len + f"-://u:{MASK}@h"),
    ]
    for src, expected in cases:
        _assert_matches_uri_re_and_expected(src, expected)


def test_fast_path_guards_and_1x_length(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaiseAll:
        def __getattr__(self, name: str):
            def _boom(*_args, **_kwargs):
                raise AssertionError(f"Unexpected regex call: .{name}")

            return _boom

    seen_lengths: list[tuple[str, str, int]] = []

    class _Proxy:
        def __init__(self, name: str, real, *, allow_sub: bool, allow_finditer: bool):
            self._name = name
            self._real = real
            self._allow_sub = allow_sub
            self._allow_finditer = allow_finditer

        def sub(self, repl, string: str, *args, **kwargs):
            if not self._allow_sub:
                raise AssertionError(f"Unexpected {self._name}.sub()")
            seen_lengths.append((self._name, "sub", len(string)))
            return self._real.sub(repl, string, *args, **kwargs)

        def finditer(self, string: str, *args, **kwargs):
            if not self._allow_finditer:
                raise AssertionError(f"Unexpected {self._name}.finditer()")
            seen_lengths.append((self._name, "finditer", len(string)))
            return self._real.finditer(string, *args, **kwargs)

    # Guard 1: missing '://' or '@' skips all regexes
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_URI_SCHEME_RE", _RaiseAll())
        mp.setattr(redact_mod, "_URI_SCHEME_TRIGGER_RE", _RaiseAll())
        assert _replace_scheme_credentials("a-" * 40 + "://u:p") == "a-" * 40 + "://u:p"
        assert _replace_scheme_credentials("a-" * 40 + "@host") == "a-" * 40 + "@host"

    # Guard 2: no '-', '.', or '+' skips _URI_SCHEME_TRIGGER_RE
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_URI_SCHEME_TRIGGER_RE", _RaiseAll())
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, allow_sub=True, allow_finditer=False),
        )
        seen_lengths.clear()
        s = "a" * 40 + "://u:p@h"
        assert _replace_scheme_credentials(s) == "a" * 40 + f"://u:{MASK}@h"
        assert seen_lengths == [("uri", "sub", len(s))]

    # Guard 3: trigger fires with no '+', '.', '-' inside [s, e - 1) falls back to _URI_SCHEME_RE.sub
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_TRIGGER_RE",
            _Proxy("trig", _URI_SCHEME_TRIGGER_RE, allow_sub=False, allow_finditer=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, allow_sub=True, allow_finditer=False),
        )
        for s in (
            "-a" + "b" * 25 + "://u:p@h",
            "-a" + "b" * 25 + "-://u:p@h",
        ):
            seen_lengths.clear()
            assert _replace_scheme_credentials(s) == s.replace("://u:p@h", f"://u:{MASK}@h")
            assert seen_lengths == [("trig", "finditer", len(s)), ("uri", "sub", len(s))]

    # Guard 4: multi-separator trigger runs _URI_SCHEME_RE.finditer(masked) with len(masked) == len(text)
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_TRIGGER_RE",
            _Proxy("trig", _URI_SCHEME_TRIGGER_RE, allow_sub=False, allow_finditer=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, allow_sub=False, allow_finditer=True),
        )
        seen_lengths.clear()
        s = "a-" * 25 + "://u:p@h"
        assert _replace_scheme_credentials(s) == "a-" * 25 + f"://u:{MASK}@h"
        assert seen_lengths == [("trig", "finditer", len(s)), ("uri", "finditer", len(s))]


def _run_deadline_script(body: str) -> None:
    subprocess.run(
        [sys.executable, "-c", body],
        cwd=SRC_DIR,
        check=True,
        timeout=10,
    )


def test_deadline_dash_flood_not_before_sep() -> None:
    """Kills old scan restored, dash dropped from trigger/mask, and every-start retried."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials, redact_text\n"
        "s = 'a-' * 120_000 + ' x://u:p@h'\n"
        "exp = 'a-' * 120_000 + ' x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
        "assert _replace_scheme_credentials(s) == exp\n"
        "assert redact_text(s) == exp\n"
    )


def test_deadline_dash_flood_before_failing_sep() -> None:
    """Kills every-start retried when '://' immediately follows the run and fails."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials, redact_text\n"
        "s = 'a-' * 120_000 + '://x u:p@h'\n"
        "assert _replace_scheme_credentials(s) == s\n"
        "assert redact_text(s) == s\n"
    )


def test_deadline_dot_plus_and_digit_interleaved_floods() -> None:
    """Kills dropping '.', '+', or '0-9' from the trigger class or mask translation."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "for sep in ('.', '+', '0-', '9.'):\n"
        "    s = ('a' + sep) * 120_000 + ' x://u:p@h'\n"
        "    exp = ('a' + sep) * 120_000 + ' x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
        "    assert _replace_scheme_credentials(s) == exp\n"
    )


def test_deadline_unicode_prefix_and_multi_run_flood() -> None:
    """Kills every-start retried with non-ASCII word boundary prefix and dense runs."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "s1 = '\\u00e9' + 'a-' * 120_000 + '://x u:p@h'\n"
        "assert _replace_scheme_credentials(s1) == s1\n"
        "s2 = ('a-' * 50 + '://x ') * 1_800 + '@'\n"
        "assert _replace_scheme_credentials(s2) == s2\n"
    )


def test_deadline_separator_search_bounded_by_run_plus() -> None:
    """Kills unbounded find('-', s) and find('.', s) without lim = e - 1."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('a+b' + '7' * 18 + ' ') * 250_000 + ' ' * 3_000_000\n"
        "assert _replace_scheme_credentials(prefix + 'x://u:p@h') == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )


def test_deadline_separator_search_bounded_by_run_dash() -> None:
    """Kills unbounded find('+', s) and find('.', s) without lim = e - 1."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('a-b' + '7' * 18 + ' ') * 250_000 + ' ' * 3_000_000\n"
        "assert _replace_scheme_credentials(prefix + 'x://u:p@h') == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )
