"""Scheme URI redaction preserves the existing output without rescanning runs."""

from pathlib import Path
import subprocess
import sys

import pytest

from swarph_cli.redact import redact_text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("{long_uri}", "{long_masked}"),
        ("éprefix+{uri}/path {uri}", "éprefix+{masked}/path {masked}"),
        ("é{uri}", "é{uri}"),
        ("outer:" + "//v:x:{uri}", "outer:" + "//v:x:{masked}"),
        # Preserve the original regex's overlapping-host behavior.
        ("{uri}{uri}", "{masked}{uri}"),
        ("{uri} {uri}", "{masked} {masked}"),
    ],
)
def test_scheme_scan_preserves_long_and_glued_uri_output(before, after):
    parts = {"uri": "s:" + "//u%2b:", "long": "s" * 8193 + ":" + "//u:"}
    values = {}
    for kind, path in parts.items():
        values["long_uri" if kind == "long" else "uri"] = path + "p:a" + "@host"
        values["long_masked" if kind == "long" else "masked"] = path + "[REDACTED:URI_CREDENTIAL]@host"
    assert redact_text(before.format_map(values)) == after.format_map(values)


def test_one_uri_after_a_long_dashed_run_finishes():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarph_cli.redact import redact_text; "
            "prefix = 'a-' * 125_000 + ' '; "
            "uri = 'demo:' + '//u:'; "
            "assert redact_text(prefix + uri + 'pw' + '@host') == "
            "prefix + uri + '[REDACTED:URI_CREDENTIAL]' + '@host'",
        ],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=5,
        check=True,
    )
