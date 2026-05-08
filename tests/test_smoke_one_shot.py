"""Live smoke test for Phase 2 one-shot CLI — falsifiability gate
per PLAN.md §13:

    swarph "hello" --provider gemini  →  text on stdout, attribution
                                          footer on stderr, exit 0

Gated on ``GEMINI_API_KEY`` being set in the environment. Skipped
on CI without the key.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set; live smoke test skipped",
)


def test_phase_2_falsifiability_gate():
    """Run the actual ``swarph`` binary as a subprocess against real
    Gemini API. Verifies the entry-point is wired end-to-end:

    - argparse → SwarphCall → GeminiAdapter → real API
    - response text printed to stdout
    - attribution footer printed to stderr
    - exit code 0
    """
    result = subprocess.run(
        [sys.executable, "-m", "swarph_cli.main",
         "--provider", "gemini",
         "say 'pong' and nothing else"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},  # preserve GEMINI_API_KEY
    )

    assert result.returncode == 0, (
        f"swarph exited {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    # Response text on stdout
    assert result.stdout.strip()  # non-empty
    assert "pong" in result.stdout.lower()

    # Attribution footer on stderr
    assert "+" in result.stderr  # the input+output token marker
    assert "$" in result.stderr  # cost
    assert "caller=cli.oneshot." in result.stderr
    assert "provider=gemini" in result.stderr
