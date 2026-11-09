"""Card #1521 — One-line append-only JSONL shim firing ledger helper for swarph-cli.

Writes `{ts, shim, cell, outcome}` lines under `~/swarph_state/shim-ledger/firings.jsonl`
(or `$SWARPH_SHIM_LEDGER_DIR/firings.jsonl`).
NEVER raises into a caller shim: any exception is swallowed and returns False.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Optional

from swarph_cli import identity

_IMPORT_TIME_HOME = Path.home()
DEFAULT_LEDGER_DIR = _IMPORT_TIME_HOME / "swarph_state" / "shim-ledger"
DEFAULT_LEDGER_FILE = "firings.jsonl"
LEDGER_FIELD_ORDER = ("ts", "shim", "cell", "outcome")


def _resolve_default_ledger_dir() -> Path:
    current_home = Path.home()
    if current_home != _IMPORT_TIME_HOME:
        return current_home / "swarph_state" / "shim-ledger"
    if DEFAULT_LEDGER_DIR != _IMPORT_TIME_HOME / "swarph_state" / "shim-ledger":
        return DEFAULT_LEDGER_DIR
    return current_home / "swarph_state" / "shim-ledger"


def _write_firing_line(target_file: Path, record: dict[str, str]) -> None:
    """Low-level file append separated so tests can fault-inject I/O errors."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: record[k] for k in LEDGER_FIELD_ORDER}
    line = json.dumps(ordered) + "\n"
    with open(target_file, "a", encoding="utf-8") as fp:
        fp.write(line)


def record_shim_firing(
    shim: str,
    cell: Optional[str] = None,
    outcome: str = "fired",
    *,
    ledger_dir: Optional[Path | str] = None,
    ts: Optional[str] = None,
) -> bool:
    """Append one firing line `{ts, shim, cell, outcome}` to the shim ledger.

    NEVER raises into the caller shim: any exception is swallowed (`return False`).
    """
    try:
        if ledger_dir is not None:
            resolved_dir = Path(ledger_dir).expanduser()
        else:
            env_dir = os.environ.get("SWARPH_SHIM_LEDGER_DIR", "").strip()
            resolved_dir = (
                Path(env_dir).expanduser() if env_dir else _resolve_default_ledger_dir()
            )
        resolved_cell = (
            (cell or "").strip()
            or (identity.declared(env=identity.SELF_ONLY)[0] or "").strip()
            or "unknown"
        )
        resolved_ts = (ts or "").strip() or datetime.now(timezone.utc).isoformat()
        record = {
            "ts": str(resolved_ts),
            "shim": str(shim),
            "cell": str(resolved_cell),
            "outcome": str(outcome),
        }
        target_file = (
            resolved_dir
            if resolved_dir.suffix == ".jsonl"
            else resolved_dir / DEFAULT_LEDGER_FILE
        )
        _write_firing_line(target_file, record)
        return True
    except Exception:
        return False
