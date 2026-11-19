"""JWT redaction preserves its output on long and overlapping segments."""

from pathlib import Path
import subprocess
import sys

import pytest

from swarph_cli.redact import redact_text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("{token}", "{mask}"),
        ("{token}.cccc==", "{mask}=="),
        ("{token}.abc", "{mask}.abc"),
        ("{token}{token}", "{mask}"),
        ("{token}.{token}", "{mask}.{payload}"),
        ("é{token}", "é{token}"),
        ("prefix-{token}-", "prefix-{mask}-"),
    ],
)
def test_jwt_scan_preserves_existing_segment_and_boundary_rules(before, after):
    start = "ey" + "J"
    payload = start + "bbbb"
    values = {"token": start + "aaaa." + payload, "payload": payload, "mask": "[REDACTED:JWT]"}
    assert redact_text(before.format_map(values)) == after.format_map(values)


def test_jwt_after_a_long_run_without_a_dot_finishes():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from swarph_cli.redact import redact_text; "
            "start = 'ey' + 'J'; "
            "prefix = (start + 'a' * 19 + '_-') * 20_000 + ' '; "
            "token = start + 'aaaa.' + start + 'bbbb'; "
            "assert redact_text(prefix + token) == prefix + '[REDACTED:JWT]'",
        ],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=5,
        check=True,
    )
