"""Card #1556 — each documented watchdog --check exit has one ledger outcome.

The table is the Exit status (--check) list in watchdog._USAGE, lines 195-203.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import watchdog

# Lines 195-203 of src/swarph_cli/commands/watchdog.py.
DOCUMENTED = {
    0: "ok",
    1: "alerted",
    2: "alerted",
    3: "error",
    4: "error",
    5: "alerted",
    6: "error",
    7: "error",
}


def _old_expression(rc: int) -> str:
    return "error" if rc >= 4 else ("alerted" if rc in (1, 2, 3) else "ok")


def _recorded(rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    ledger = tmp_path / f"ledger-{rc}"
    monkeypatch.setenv("SWARPH_SHIM_LEDGER_DIR", str(ledger))
    monkeypatch.setattr(watchdog, "run_check", lambda args: rc)
    got = watchdog.run_watchdog(["--check", "--cell", "grok-researcher"])
    assert got == rc
    row = json.loads((ledger / "firings.jsonl").read_text(encoding="utf-8").strip())
    assert row["shim"] == "swarph-watchdog"
    assert row["cell"] == "grok-researcher"
    assert list(row) == ["ts", "shim", "cell", "outcome"]
    return row["outcome"]


@pytest.mark.parametrize("rc,outcome", sorted(DOCUMENTED.items()))
def test_documented_check_exit_records_its_ledger_outcome(
    rc: int, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _recorded(rc, tmp_path, monkeypatch) == outcome


@pytest.mark.parametrize("rc", [8, -1, 99])
def test_undocumented_check_exit_records_error(
    rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _recorded(rc, tmp_path, monkeypatch) == "error"


def test_restoring_the_old_expression_misses_rc_3_and_rc_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-table expression alerts on rc 3 and calls rc 5 an error."""
    assert _recorded(3, tmp_path, monkeypatch) != _old_expression(3)
    assert _recorded(5, tmp_path, monkeypatch) != _old_expression(5)
    assert _old_expression(3) == "alerted"
    assert _old_expression(5) == "error"
