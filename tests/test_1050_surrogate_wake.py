"""#1050 — a DM whose content holds a lone surrogate escape must not kill
the wake filter.

Found by drop-on-meta-edge in #1298 (card #1049 Go A/B): a valid JSON line
carrying e.g. \\ud83d makes the filter raise UnicodeEncodeError on print —
exit 1, NO deaf-watch line, and every later DM is never surfaced. This is
the fleet's silent-wake filter (#482): any peer's DM can deafen a cell's
wake without the deaf alarm firing.

The escape MUST be outside Python's surrogateescape round-trip range
(DC80-DCFF): \\udcff survives on UTF-8-mode boxes (it round-trips raw
byte 0xFF through a pipe) and only kills on strict-errors stdouts, while
\\ud83d (lead) and \\udfff (trail, above DCFF) kill everywhere — measured
dead on the pre-fix filter under both default and strict stdouts (#1302).

Seam: the process boundary — stdin lines in, stdout lines out — exactly
what the accept check names.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

MOD = [sys.executable, "-u", "-m", "swarph_cli.scripts.dm_notify_filter"]

# Lethal on every box (see module docstring for why these, not \\udcff).
LETHAL_ESCAPES = ("\\ud83d", "\\udfff")

GOOD = '{"id":12,"from_node":"b","kind":"fyi","content":"after bad"}'


def _run(payload: bytes) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        MOD + ["--idle-seconds", "3600"],
        input=payload,
        capture_output=True,
        timeout=60,
        env=dict(os.environ),
    )


def _bad(mid: int, esc: str) -> str:
    return ('{"id":%d,"from_node":"a","kind":"fyi","content":"AB%scCD"}'
            % (mid, esc))


@pytest.mark.parametrize("esc", LETHAL_ESCAPES)
def test_lone_surrogate_then_good_dm_survives(esc: str):
    """The good DM after a lone-surrogate line is printed; the process
    keeps running to EOF (deaf line) instead of dying on the bad line."""
    p = _run((_bad(11, esc) + "\n" + GOOD + "\n").encode("ascii"))
    assert b"id=12" in p.stdout, (
        f"good DM lost after lone-surrogate line: rc={p.returncode} "
        f"stdout={p.stdout!r} stderr={p.stderr[-300:]!r}"
    )
    assert b"DEAF" in p.stdout, (
        f"watch died without its deaf-watch line: rc={p.returncode} "
        f"stderr={p.stderr[-300:]!r}"
    )


def test_lone_surrogate_never_kills_process():
    """No per-line content may exit the filter: a whole stream of hostile
    lines still ends at the EOF deaf line, not a traceback."""
    lines = ([_bad(i, LETHAL_ESCAPES[i % 2]) for i in range(20)]
             + [GOOD])
    p = _run(("\n".join(lines) + "\n").encode("ascii"))
    assert b"id=12" in p.stdout
    assert b"DEAF" in p.stdout
    assert b"Traceback" not in p.stderr
