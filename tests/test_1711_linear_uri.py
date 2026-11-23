"""Tests for Card #1711 (Obligation #6304): Linear-time URI scheme redaction.

Verifies that ``_replace_scheme_credentials`` and ``redact_text`` preserve
``_URI_SCHEME_RE`` match spans and output identically across the normal path,
fast-path guards, and the length-preserving masked maximal-run path, while
avoiding quadratic backtracking on long or dense multi-start scheme runs.

All URI-shaped test inputs use synthetic values constructed at runtime.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import swarph_cli.redact as redact_mod
from swarph_cli.redact import (
    _URI_SCHEME_LONG_RUN_RE,
    _URI_SCHEME_RE,
    _URI_SCHEME_START_RE,
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
        ("s3", "a-" * 70),
        ("neo4j", "a-" * 70),
        ("h2", "a-" * 70),
        ("db2", "a-" * 70),
        ("amqp0-9-1", "a-" * 70),
        ("a.b", "a-" * 70),
        ("a.9", "a-" * 70),
        ("a-b", "a." * 70),
        ("a-9", "a." * 70),
        ("git+https", "a-" * 70),
        ("git+7https", "a-" * 70),
        ("s+9", "a-" * 70),
        ("s3", "7" * 130 + "-"),
        ("9-b", "a-" * 70),
        ("s3", "a." * 70),
        ("s3", "a+" * 70),
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
        ("\u00e9" + "a-" * 70 + "://u:p@h", "\u00e9" + "a-" * 70 + f"://u:{MASK}@h"),
        ("\u00e9" + "a" * 126 + "-b-c://u:p@h", "\u00e9" + "a" * 126 + f"-b-c://u:{MASK}@h"),
        ("\u00e9" + "a" * 126 + "-b" + "c-" * 12 + "://u:p@h", "\u00e9" + "a" * 126 + "-b" + "c-" * 12 + f"://u:{MASK}@h"),
        ("\u00e9" + "-a" + "b-" * 70 + "://u:p@h", "\u00e9" + "-a" + "b-" * 70 + f"://u:{MASK}@h"),
        ("\u00e9" + "a" * 130 + "-b://u:p@h", "\u00e9" + "a" * 130 + f"-b://u:{MASK}@h"),
        ("7" + "a-" * 70 + "://u:p@h", "7" + "a-" * 70 + f"://u:{MASK}@h"),
        ("_" + "a-" * 70 + "://u:p@h", "_" + "a-" * 70 + f"://u:{MASK}@h"),
        ("-" + "a-" * 70 + "://u:p@h", "-" + "a-" * 70 + f"://u:{MASK}@h"),
        # Trailing separator at rend - 1 (with and without internal separator)
        ("-a" + "b" * 126 + "-://u:p@h", "-a" + "b" * 126 + f"-://u:{MASK}@h"),
        ("-a" + "b" * 126 + "+://u:p@h", "-a" + "b" * 126 + f"+://u:{MASK}@h"),
        ("-a" + "b" * 126 + ".://u:p@h", "-a" + "b" * 126 + f".://u:{MASK}@h"),
        ("-a" + "b" * 130 + "-://x u:p@h", "-a" + "b" * 130 + "-://x u:p@h"),
        ("a" * 127 + "-://u:p@h", "a" * 127 + f"-://u:{MASK}@h"),
        ("a-b" + "c" * 125 + "-://u:p@h", "a-b" + "c" * 125 + f"-://u:{MASK}@h"),
        ("a-b" + "c" * 125 + "d-://u:p@h", "a-b" + "c" * 125 + f"d-://u:{MASK}@h"),
        ("7" * 130 + "-a://u:p@h", "7" * 130 + f"-a://u:{MASK}@h"),
        # Trailing separator right before underscore or Unicode word character
        ("a-" * 65 + "_x://u:p@h", "a-" * 65 + "_x://u:p@h"),
        ("a-" * 65 + "\u00e9://u:p@h", "a-" * 65 + "\u00e9://u:p@h"),
        ("a-b" + "0" * 124 + "-_x://u:p@h", "a-b" + "0" * 124 + "-_x://u:p@h"),
        # Threshold boundaries around 20..130 chars
        ("a-" * 15 + "a://u:p@h", "a-" * 15 + f"a://u:{MASK}@h"),
        ("a-" * 64 + "://u:p@h", "a-" * 64 + f"://u:{MASK}@h"),
        ("a-" * 64 + "a://u:p@h", "a-" * 64 + f"a://u:{MASK}@h"),
        # [REDACTED negative lookahead
        ("s://u:[REDACTED:URI_CREDENTIAL]@h", "s://u:[REDACTED:URI_CREDENTIAL]@h"),
        ("s://u:[REDACTED:X]@h", "s://u:[REDACTED:X]@h"),
        ("a-" * 70 + "://u:[REDACTED:X]@h", "a-" * 70 + "://u:[REDACTED:X]@h"),
        ("a-" * 70 + "://u:[REDACTED@h", "a-" * 70 + "://u:[REDACTED@h"),
        # Host consuming into a subsequent long masked run and vice versa
        (
            "s://u:p@" + "a-" * 70 + "://v:q@w",
            f"s://u:{MASK}@" + "a-" * 70 + "://v:q@w",
        ),
        (
            "a-" * 70 + "://u:p@" + "b." * 70 + "://v:q@w",
            "a-" * 70 + f"://u:{MASK}@" + "b." * 70 + "://v:q@w",
        ),
        # Pre-existing zeros alongside masked + / . / -
        (
            "a0-" * 50 + "://u:p@h " + "b0." * 50 + "://v:q@w",
            "a0-" * 50 + f"://u:{MASK}@h " + "b0." * 50 + f"://v:{MASK}@w",
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
            "a-" * 70 + "://u\\1:p\\2@h\\3",
            "a-" * 70 + f"://u\\1:{MASK}@h\\3",
        ),
        # Long '-' run with no letters before a real URI (tests bounded search)
        ("-" * 130 + " a://u:p@h", "-" * 130 + f" a://u:{MASK}@h"),
    ],
)
def test_exact_equivalence_edge_cases(src: str, expected: str) -> None:
    _assert_matches_uri_re_and_expected(src, expected)


def test_long_run_with_urischeme_only_equivalence() -> None:
    cases = [
        "-" * 140 + "://u:p@h",
        "\u00e9" + "a" * 140 + "://u:p@h",
        "\u00e9" + "a-" * 70 + "://x u:p@h",
        "\u00e9" + "a" * 126 + "-b-c://u:p@h",
        "-a" + "b" * 126 + "-://u:p@h",
        "a" * 127 + "-://u:p@h",
        "a-b" + "c" * 125 + "-://u:p@h",
        "7" * 130 + "-a://u:p@h",
        "a-" * 65 + "_x://u:p@h",
        "a-" * 65 + "\u00e9://u:p@h",
        "a-b" + "0" * 124 + "-_x://u:p@h",
        "a-" * 70 + "0" * 40 + "://u:p@h",
        "s://u:p@h " + "a-" * 70 + " x://v:q@w",
        "a-" * 70 + "://x " + "b." * 70 + "://u:p@h",
    ]
    for src in cases:
        _assert_matches_uri_re(src)


@pytest.mark.parametrize("run_len", range(125, 132))
def test_long_run_threshold_boundary_equivalence(run_len: int) -> None:
    """Sweeps maximal run lengths 125..131 across the {128,} threshold."""
    alt_dash = "a-" * (run_len // 2) + "a" * (run_len % 2)
    alt_dot = "b." * (run_len // 2) + "b" * (run_len % 2)
    cases = [
        ("a" * run_len + "://u:p@h", "a" * run_len + f"://u:{MASK}@h"),
        ("7" * run_len + "://u:p@h", "7" * run_len + "://u:p@h"),
        ("-a" + "b" * (run_len - 2) + "://u:p@h", "-a" + "b" * (run_len - 2) + f"://u:{MASK}@h"),
        ("-a" + "-" * (run_len - 2) + "://u:p@h", "-a" + "-" * (run_len - 2) + f"://u:{MASK}@h"),
        (alt_dash + "://u:p@h", alt_dash + f"://u:{MASK}@h"),
        (alt_dash + "://x u:p@h", alt_dash + "://x u:p@h"),
        (alt_dot + "://u:p@h", alt_dot + f"://u:{MASK}@h"),
        (alt_dot + "://x u:p@h", alt_dot + "://x u:p@h"),
        ("a" * (run_len - 1) + "-://u:p@h", "a" * (run_len - 1) + f"-://u:{MASK}@h"),
        ("a-b" + "c" * (run_len - 4) + "-://u:p@h", "a-b" + "c" * (run_len - 4) + f"-://u:{MASK}@h"),
        ("a" * (run_len - 3) + "c-d://u:p@h", "a" * (run_len - 3) + f"c-d://u:{MASK}@h"),
        ("-a" + "b" * (run_len - 5) + "c-d://u:p@h", "-a" + "b" * (run_len - 5) + f"c-d://u:{MASK}@h"),
        ("\u00e9" + alt_dash + "://u:p@h", "\u00e9" + alt_dash + f"://u:{MASK}@h"),
        ("\u00e9" + alt_dash + "://x u:p@h", "\u00e9" + alt_dash + "://x u:p@h"),
    ]
    for src, expected in cases:
        _assert_matches_uri_re_and_expected(src, expected)


class _RaiseAll:
    def __getattr__(self, name: str):
        def _boom(*_args, **_kwargs):
            raise AssertionError(f"Unexpected regex call: .{name}")

        return _boom


class _Proxy:
    def __init__(
        self,
        name: str,
        real,
        seen_lengths: list[tuple[str, str, int]],
        *,
        allow_sub: bool = False,
        allow_finditer: bool = False,
        allow_search: bool = False,
    ):
        self._name = name
        self._real = real
        self._seen_lengths = seen_lengths
        self._allow_sub = allow_sub
        self._allow_finditer = allow_finditer
        self._allow_search = allow_search

    def sub(self, repl, string: str, *args, **kwargs):
        if not self._allow_sub:
            raise AssertionError(f"Unexpected {self._name}.sub()")
        self._seen_lengths.append((self._name, "sub", len(string)))
        return self._real.sub(repl, string, *args, **kwargs)

    def finditer(self, string: str, *args, **kwargs):
        if not self._allow_finditer:
            raise AssertionError(f"Unexpected {self._name}.finditer()")
        self._seen_lengths.append((self._name, "finditer", len(string)))
        return self._real.finditer(string, *args, **kwargs)

    def search(self, string: str, *args, **kwargs):
        if not self._allow_search:
            raise AssertionError(f"Unexpected {self._name}.search()")
        self._seen_lengths.append((self._name, "search", len(string)))
        return self._real.search(string, *args, **kwargs)


def test_fast_path_guards_and_1x_length(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_lengths: list[tuple[str, str, int]] = []

    # Guard 1: missing '://' or '@' skips all regexes
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_URI_SCHEME_RE", _RaiseAll())
        mp.setattr(redact_mod, "_URI_SCHEME_LONG_RUN_RE", _RaiseAll())
        mp.setattr(redact_mod, "_URI_SCHEME_START_RE", _RaiseAll())
        assert _replace_scheme_credentials("a-" * 70 + "://u:p") == "a-" * 70 + "://u:p"
        assert _replace_scheme_credentials("a-" * 70 + "@host") == "a-" * 70 + "@host"

    # Guard 2: no '-', '.', or '+' skips _URI_SCHEME_LONG_RUN_RE and _URI_SCHEME_START_RE
    with monkeypatch.context() as mp:
        mp.setattr(redact_mod, "_URI_SCHEME_LONG_RUN_RE", _RaiseAll())
        mp.setattr(redact_mod, "_URI_SCHEME_START_RE", _RaiseAll())
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_sub=True),
        )
        seen_lengths.clear()
        s = "a" * 140 + "://u:p@h"
        assert _replace_scheme_credentials(s) == "a" * 140 + f"://u:{MASK}@h"
        assert seen_lengths == [("uri", "sub", len(s))]

    # Guard 3a: long run >= 128 with no separator in [rstart, rend - 1) skips _URI_SCHEME_START_RE
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_LONG_RUN_RE",
            _Proxy("long", _URI_SCHEME_LONG_RUN_RE, seen_lengths, allow_finditer=True),
        )
        mp.setattr(redact_mod, "_URI_SCHEME_START_RE", _RaiseAll())
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_sub=True),
        )
        for s in (
            "a" * 130 + "://u:p@h-",
            "a" * 130 + "://u:p@h.",
            "a" * 130 + "://u:p@h+",
            "a" * 129 + "-://u:p@h",
        ):
            seen_lengths.clear()
            assert _replace_scheme_credentials(s) == s.replace("://u:p@h", f"://u:{MASK}@h")
            assert seen_lengths == [("long", "finditer", len(s)), ("uri", "sub", len(s))]

    # Guard 3b: long run >= 128 with separator in [rstart, rend - 1) but no \b[a-zA-Z]
    # or no separator in [sm.start() + 1, rend - 1) falls back to _URI_SCHEME_RE.sub
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_LONG_RUN_RE",
            _Proxy("long", _URI_SCHEME_LONG_RUN_RE, seen_lengths, allow_finditer=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_START_RE",
            _Proxy("start", _URI_SCHEME_START_RE, seen_lengths, allow_search=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_sub=True),
        )
        for s in (
            "-" * 130 + " x://u:p@h",
            "-a" + "b" * 126 + "://u:p@h",
            "-a" + "b" * 126 + "://u:p@h.",
            "-a" + "b" * 126 + "://u:p@h+",
            "-a" + "b" * 126 + "-://u:p@h",
        ):
            seen_lengths.clear()
            assert _replace_scheme_credentials(s) == s.replace("://u:p@h", f"://u:{MASK}@h")
            assert seen_lengths == [
                ("long", "finditer", len(s)),
                ("start", "search", len(s)),
                ("uri", "sub", len(s)),
            ]

    # Guard 4: qualifying multi-separator long run >= 128 runs _URI_SCHEME_RE.finditer(masked)
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_LONG_RUN_RE",
            _Proxy("long", _URI_SCHEME_LONG_RUN_RE, seen_lengths, allow_finditer=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_START_RE",
            _Proxy("start", _URI_SCHEME_START_RE, seen_lengths, allow_search=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_finditer=True),
        )
        seen_lengths.clear()
        s = "a-" * 70 + "://u:p@h"
        assert _replace_scheme_credentials(s) == "a-" * 70 + f"://u:{MASK}@h"
        assert seen_lengths == [
            ("long", "finditer", len(s)),
            ("start", "search", len(s)),
            ("uri", "finditer", len(s)),
        ]


def _run_deadline_script(body: str) -> None:
    subprocess.run(
        [sys.executable, "-c", body],
        cwd=SRC_DIR,
        check=True,
        timeout=10,
    )


def test_deadline_dash_flood_not_before_sep() -> None:
    """Kills old scan restored, dash dropped from run/mask, and every-start retried."""
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
    """Kills dropping '.', '+', or '0-9' from the run class or mask translation."""
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
        "s2 = ('a-' * 70 + '://x ') * 1_800 + '@'\n"
        "assert _replace_scheme_credentials(s2) == s2\n"
    )


def test_deadline_start_search_bounded_by_run() -> None:
    """Kills dropping ', rend' from _URI_SCHEME_START_RE.search(text, rstart, rend)."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('-' * 128 + ' ') * 40_000 + ' ' * 3_000_000\n"
        "s = prefix + 'x://u:p@h'\n"
        "assert _replace_scheme_credentials(s) == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )


def test_deadline_pre_separator_search_bounded_by_run() -> None:
    """Kills dropping ', lim' from pre-start find('-', rstart, lim) / find('.') / find('+')."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('0' * 128 + ' ') * 70_000 + ' ' * 8_000_000\n"
        "s = prefix + 'x://u:p@h-'\n"
        "assert _replace_scheme_credentials(s) == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h-'\n"
    )


def test_deadline_post_separator_search_bounded_by_run_plus() -> None:
    """Kills dropping ', lim' from post-start find('-', s, lim) and find('.', s, lim)."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('+a' + '7' * 126 + ' ') * 70_000 + ' ' * 8_000_000\n"
        "s = prefix + 'x://u:p@h'\n"
        "assert _replace_scheme_credentials(s) == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )


def test_deadline_post_separator_search_bounded_by_run_dash() -> None:
    """Kills dropping ', lim' from post-start find('+', s, lim) and find('.', s, lim)."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('-a' + '7' * 126 + ' ') * 70_000 + ' ' * 8_000_000\n"
        "s = prefix + 'x://u:p@h'\n"
        "assert _replace_scheme_credentials(s) == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )


def test_deadline_not_only_first_run_masked() -> None:
    """Kills masking only the first qualifying run."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "s = 'a-' * 65 + ' ' + 'a-' * 120_000 + ' x://u:p@h'\n"
        "exp = 'a-' * 65 + ' ' + 'a-' * 120_000 + ' x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
        "assert _replace_scheme_credentials(s) == exp\n"
    )


def test_deadline_copy_does_not_restart_from_zero() -> None:
    """Kills restarting the slice copy from index 0 (text[:s][copied_until:])."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials\n"
        "prefix = ('a-' * 65 + ' ') * 85_000\n"
        "s = prefix + 'x://u:p@h'\n"
        "assert _replace_scheme_credentials(s) == prefix + 'x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
    )


def test_deadline_uppercase_flood() -> None:
    """Kills dropping A-Z from _URI_SCHEME_LONG_RUN_RE or _URI_SCHEME_START_RE."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials, redact_text\n"
        "s1 = 'A-' * 120_000 + ' x://u:p@h'\n"
        "exp1 = 'A-' * 120_000 + ' x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
        "assert _replace_scheme_credentials(s1) == exp1\n"
        "assert redact_text(s1) == exp1\n"
        "s2 = 'A-' * 120_000 + '://x u:p@h'\n"
        "assert _replace_scheme_credentials(s2) == s2\n"
        "assert redact_text(s2) == s2\n"
    )


def test_deadline_not_only_last_run_masked() -> None:
    """Kills masking only the last qualifying run (e.g. runs[-1:] or overwriting runs)."""
    _run_deadline_script(
        "from swarph_cli.redact import _replace_scheme_credentials, redact_text\n"
        "s = 'a-' * 120_000 + ' ' + 'a-' * 65 + ' x://u:p@h'\n"
        "exp = 'a-' * 120_000 + ' ' + 'a-' * 65 + ' x://u:[REDACTED:URI_CREDENTIAL]@h'\n"
        "assert _replace_scheme_credentials(s) == exp\n"
        "assert redact_text(s) == exp\n"
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "_b-c",
        "_-c",
        "_b.c",
        "_.c",
        "_b+c",
        "_+c",
        "_b-c.d+e",
    ],
)
def test_underscore_breaks_long_run_and_preserves_scheme_start(suffix: str) -> None:
    """Kills adding '_' to _URI_SCHEME_LONG_RUN_RE."""
    lead = "a-" * 70
    src = f"{lead}{suffix}://user1:pw_secret@host.example.com/path"
    expected = f"{lead}{suffix}://user1:{MASK}@host.example.com/path"
    _assert_matches_uri_re_and_expected(src, expected)


def test_long_run_lookbehind_blocks_all_run_chars_and_allows_delimiters() -> None:
    """Kills removing or narrowing (?<![a-zA-Z0-9+.\\-]) on _URI_SCHEME_LONG_RUN_RE."""
    for ch in ("a", "m", "z", "A", "M", "Z", "0", "5", "9", "+", ".", "-"):
        sample = ch + "a" * 128
        assert _URI_SCHEME_LONG_RUN_RE.match(sample, 1) is None
        assert _URI_SCHEME_LONG_RUN_RE.search(sample, 1) is None
    for delim in (" ", "_", ":", "\u00e9"):
        sample = delim + "a" * 128
        m_match = _URI_SCHEME_LONG_RUN_RE.match(sample, 1)
        assert m_match is not None and m_match.span() == (1, 129)
        m_search = _URI_SCHEME_LONG_RUN_RE.search(sample, 1)
        assert m_search is not None and m_search.span() == (1, 129)


def test_long_run_threshold_128_pinned_direct_and_behavioral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the exact {128,} threshold directly and via _replace_scheme_credentials."""
    for run_127 in ("a" * 127, "a-" * 63 + "a", "a." * 63 + "a", "a+" * 63 + "a"):
        assert _URI_SCHEME_LONG_RUN_RE.match(run_127) is None
        assert _URI_SCHEME_LONG_RUN_RE.search(run_127) is None
    for run_128 in ("a" * 128, "a-" * 64, "a." * 64, "a+" * 64):
        m = _URI_SCHEME_LONG_RUN_RE.match(run_128)
        assert m is not None and m.span() == (0, 128)

    seen_lengths: list[tuple[str, str, int]] = []
    # 127-char multi-separator run takes direct _URI_SCHEME_RE.sub with zero _URI_SCHEME_START_RE.search calls
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_LONG_RUN_RE",
            _Proxy("long", _URI_SCHEME_LONG_RUN_RE, seen_lengths, allow_finditer=True),
        )
        mp.setattr(redact_mod, "_URI_SCHEME_START_RE", _RaiseAll())
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_sub=True),
        )
        s127 = "a-" * 63 + "a://u:p@h"
        assert _replace_scheme_credentials(s127) == "a-" * 63 + f"a://u:{MASK}@h"
        assert seen_lengths == [("long", "finditer", len(s127)), ("uri", "sub", len(s127))]

    # 128-char multi-separator run takes _URI_SCHEME_START_RE.search + _URI_SCHEME_RE.finditer
    with monkeypatch.context() as mp:
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_LONG_RUN_RE",
            _Proxy("long", _URI_SCHEME_LONG_RUN_RE, seen_lengths, allow_finditer=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_START_RE",
            _Proxy("start", _URI_SCHEME_START_RE, seen_lengths, allow_search=True),
        )
        mp.setattr(
            redact_mod,
            "_URI_SCHEME_RE",
            _Proxy("uri", _URI_SCHEME_RE, seen_lengths, allow_finditer=True),
        )
        seen_lengths.clear()
        s128 = "a-" * 64 + "://u:p@h"
        assert _replace_scheme_credentials(s128) == "a-" * 64 + f"://u:{MASK}@h"
        assert seen_lengths == [
            ("long", "finditer", len(s128)),
            ("start", "search", len(s128)),
            ("uri", "finditer", len(s128)),
        ]
