"""Standalone redaction preserves gaps and stays fast on token-dense lines."""

from pathlib import Path
import subprocess
import sys

import pytest

from swarph_cli.redact import redact_text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("\t{key}, ordinary\n{key}  ", "\t{mask}, ordinary\n{mask}  "),
        ("[REDACTED:{key}] {key}", "[REDACTED:{key}] {mask}"),
        ("[REDACTED:note [REDACTED:{key}] {key}]", "[REDACTED:note [REDACTED:{key}] {mask}]"),
        ("{key} [REDACTED:unclosed {key} {key}", "{mask} [REDACTED:unclosed {key} {key}"),
        ("[OTHER:{key}]\u00a0{key}]", "[OTHER:{mask}]\u00a0{mask}]"),
    ],
)
def test_join_preserves_original_gaps_and_marker_boundaries(before, after):
    values = {"key": "".join(f"{c}{c.upper()}{n}" for n, c in enumerate("abcdefgh", 1)),
              "mask": "[REDACTED:HIGH_ENTROPY]"}
    assert redact_text(before.format_map(values)) == after.format_map(values)


def test_many_candidate_words_dont_recopy_the_line():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarph_cli.redact import redact_text; "
            "line = ('a' * 30 + ' ') * 60_000; "
            "assert redact_text(line) == line",
        ],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=5,
        check=True,
    )
