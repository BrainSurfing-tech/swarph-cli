"""``swarph hook-output`` — Phase 7 / v0.7 PR-C SessionStart hook callback.

Called BY the SessionStart hook configured via ``swarph install-hook``.
Receives Claude Code's hook-input JSON on stdin, discovers the
applicable cell.yaml, and emits a hook-output JSON to stdout
containing the starter prompt as ``additionalContext``.

Skipped (no-op) when ``SWARPH_SPAWN=1`` env is set — that environment
marker is set by ``swarph spawn`` to indicate the spawn path already
injected the prompt via ``--append-system-prompt``. Avoids
double-injection when the session was launched through swarph.

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
)


def _emit_no_op() -> int:
    """Emit empty additionalContext + exit 0.

    Used when there's no cell.yaml to load OR when SWARPH_SPAWN=1 OR
    on any error path. Hook MUST NOT block session startup.
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


def run_hook_output(argv: list[str] | None = None) -> int:
    # Skip when launched-via-swarph-spawn (already injected via
    # --append-system-prompt; double-injection would duplicate context).
    if os.environ.get("SWARPH_SPAWN", "").strip() == "1":
        return _emit_no_op()

    # Drain hook-input from stdin (we don't actually use any field
    # currently, but Claude Code's protocol expects us to consume it
    # without blocking on TTY-detection edge cases).
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass  # swallow stdin-read errors; hook output is the only thing that matters

    cell_path = _discover_cell_path()
    if cell_path is None:
        return _emit_no_op()

    try:
        cell = load_cell(cell_path)
    except CellError:
        # cell.yaml exists but is malformed — emit no-op + skip rather
        # than blocking session startup. Operator can fix cell.yaml
        # offline; meanwhile sessions still start.
        return _emit_no_op()

    try:
        starter = cell.starter_prompt_text()
    except CellError:
        # starter_prompt_path set but unreadable — emit no-op.
        return _emit_no_op()

    if not starter:
        return _emit_no_op()

    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": starter,
        }
    }
    print(json.dumps(output))
    return 0
