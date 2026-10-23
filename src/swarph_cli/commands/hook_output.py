"""``swarph hook-output`` — Phase 7 / v0.7 PR-C SessionStart hook callback.

Called BY the SessionStart hook configured via ``swarph install-hook``.
Receives Claude Code's hook-input JSON on stdin, discovers the
applicable cell.yaml, and emits a hook-output JSON to stdout
containing the starter prompt as ``additionalContext``.

``SWARPH_SPAWN=1`` means ``swarph spawn`` already put the starter on
``--append-system-prompt``. This hook then emits the ten and only the
ten, so the starter is not doubled. With no ten to read, that path is
still a no-op.

Cell discovery:
  1. Cwd-local ``./cell.yaml`` (alpha #891 D3 auto-discovery)
  2. ``$XDG_CONFIG_HOME/swarph/cells/<basename(cwd)>.yaml`` —
     fallback to user config dir keyed on cwd basename
  3. No cell found → no-op (empty additionalContext, exit 0)

Failure mode philosophy: if anything goes wrong (cell.yaml not
parseable, starter prompt unreadable, hook-input JSON malformed),
emit empty additionalContext + exit 0. The hook MUST NOT block
session startup on swarph-side issues — gracefully degrade to
"no auto-injection happened" rather than "session refuses to start."
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from swarph_cli.cell import (
    Cell,
    CellError,
    cells_dir,
    discover_cell_in_cwd,
    load_cell,
    read_starter_prompt,
)
from swarph_cli.the_ten import session_block


def _emit_no_op() -> int:
    """Emit empty additionalContext + exit 0.

    Used when there is nothing to add: no starter and no ten, or
    ``SWARPH_SPAWN=1`` with no ten. Hook MUST NOT block session startup.
    """
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }
    print(json.dumps(output))
    return 0


def _discover_cell_path() -> Path | None:
    """Two-tier cell.yaml discovery for hook context.

    1. ``./cell.yaml`` in current working directory
    2. ``<cells_dir>/<basename(cwd)>.yaml`` keyed on cwd's last segment

    Returns None if neither exists; caller emits no-op then.
    """
    cwd_local = discover_cell_in_cwd()
    if cwd_local is not None:
        return cwd_local

    cwd_basename = Path.cwd().name
    if cwd_basename:
        candidate = cells_dir() / f"{cwd_basename}.yaml"
        if candidate.is_file():
            return candidate
    return None


def _emit(additional: str) -> int:
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional,
        }
    }
    print(json.dumps(output))
    return 0


def _starter_text() -> str:
    """The cell's starter prompt, or ``""`` when there is nothing to add.

    A missing cell, a malformed cell, or an unreadable starter is an
    empty starter. The hook still emits the ten when that block exists.
    """
    cell_path = _discover_cell_path()
    if cell_path is None:
        return ""
    try:
        cell = load_cell(cell_path)
        return read_starter_prompt(cell) or ""
    except CellError:
        return ""


def run_hook_output(argv: list[str] | None = None) -> int:
    # Drain hook-input from stdin (we don't actually use any field
    # currently, but Claude Code's protocol expects us to consume it
    # without blocking on TTY-detection edge cases).
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass  # swallow stdin-read errors; hook output is the only thing that matters

    # Commit-addressed. Empty when the repo or the ref is missing, and
    # that emptiness must not change the starter-only behaviour.
    block = session_block()

    # SWARPH_SPAWN=1 means the starter is already on --append-system-prompt.
    # The ten is not part of that flag, so this path still emits the block
    # and only the block.
    if os.environ.get("SWARPH_SPAWN", "").strip() == "1":
        return _emit(block) if block else _emit_no_op()

    parts = [part for part in (_starter_text(), block) if part]
    if not parts:
        return _emit_no_op()
    return _emit("\n\n".join(parts))
