"""``swarph wake-hook-output`` — board card #482 silent-wake SessionStart callback.

Called BY the session-start hook installed via ``swarph install-wake-hook``.
The harness is baked into the installed command (``--harness``), so this
callback never has to guess which output shape the caller expects.

Per-provider product (the card's accepted two-valued design — the real
distinction is not WHICH harness, it is WHERE THE WAKE LIVES):

* ``claude`` / ``codex`` → ARM-INSTRUCTION. The wake lives in the harness:
  emit the watch pipeline (tail -F inbox.log | dm_notify_filter) as
  additionalContext so the agent arms it as a background watch.
* ``cursor`` → VERIFY-AND-REPORT. The wake lives in swarph (the monitor's
  push sink, e.g. ``tmux:<cell>``). Query ``swarph monitor status --json``
  and report armed / NOT armed. Cursor's harness has no persistent-Monitor
  primitive for a bundle to instruct, so the honest product is verification.
* anything else → LOUD REFUSAL: say in the session context that this
  harness has no supported wake path, so a silent gap cannot masquerade
  as armed.

Failure-mode invariant (same as ``swarph hook-output``): exit 0 on every
error path. The hook MUST NOT block session startup — worst case is a
loud context line, never a refused session.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from swarph_cli.cell import (
    CellError,
    cells_dir,
    discover_cell_in_cwd,
    load_cell,
)

_ARM_HARNESSES = ("claude", "codex")
_VERIFY_HARNESSES = ("cursor",)
_KNOWN_HARNESSES = _ARM_HARNESSES + _VERIFY_HARNESSES

_FILTER_MODULE = "swarph_cli.scripts.dm_notify_filter"


def _drain_stdin() -> None:
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass


def _emit(payload: dict[str, Any], *, harness: str) -> int:
    """Emit context in the shape the calling harness expects."""
    if harness in _VERIFY_HARNESSES:
        # Cursor sessionStart: {"env": {...}, "additional_context": "..."}
        print(json.dumps({"additional_context": payload.get("context", "")}))
    else:
        # Claude Code / Codex SessionStart shape.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": payload.get("context", ""),
                    }
                }
            )
        )
    return 0


def _discover_cell_name(explicit: Optional[str] = None) -> Optional[str]:
    """Cell-name resolution, most-explicit first.

    1. ``--cell`` baked into the installed hook command
    2. ``$SWARPH_SELF`` — the mesh's existing self-name convention
       (``swarph monitor --as`` honors it too)
    3. cwd discovery: ./cell.yaml, then cells_dir/<basename(cwd)>.yaml —
       weakest, since harness sessions do not necessarily start in a
       cell-named directory
    """
    if explicit:
        return explicit
    env_self = os.environ.get("SWARPH_SELF", "").strip()
    if env_self:
        return env_self
    cwd_local = discover_cell_in_cwd()
    candidates = [cwd_local] if cwd_local is not None else []
    cwd_basename = Path.cwd().name
    if cwd_basename:
        fallback = cells_dir() / f"{cwd_basename}.yaml"
        if fallback.is_file():
            candidates.append(fallback)
    for path in candidates:
        try:
            cell = load_cell(path)
        except CellError:
            continue
        name = getattr(cell, "name", None) or getattr(cell, "role", None)
        if name:
            return str(name)
    return None


def _sidecar_dir(cell_name: str) -> Path:
    # Mirrors mesh._default_sidecar_state_dir without importing the mesh
    # command module into the hook's startup path.
    return Path.home() / "swarph_state" / cell_name / "mesh-sidecar"


def _arm_instruction(cell_name: Optional[str]) -> str:
    interpreter = sys.executable
    if cell_name:
        inbox = _sidecar_dir(cell_name) / "inbox.log"
        tail = f'tail -n 0 -F "{inbox}"'
    else:
        tail = 'tail -n 0 -F "$HOME/swarph_state/<cell>/mesh-sidecar/inbox.log"'
    pipeline = f'{tail} | "{interpreter}" -u -m {_FILTER_MODULE}'
    return (
        "[swarph silent-wake] This cell's DM wake lives in THIS harness. "
        "Arm it now as a persistent background watch:\n"
        f"  {pipeline}\n"
        "The filter prints one [MESH DM] line per real DM (receipts and "
        "monitor chatter are dropped), alerts once per quiet period after "
        "--idle-seconds of silence, and exits loudly on inbox EOF. "
        "Run it unbuffered (-u is in the command) — a buffered watch is "
        "silent while looking armed, which is the failure this exists to "
        "prevent. Verify the swarph monitor is running with "
        "`swarph monitor status`."
        + (
            ""
            if cell_name
            else "\n(No cell.yaml discovered from this cwd — substitute this "
            "cell's name for <cell> in the inbox path.)"
        )
    )


def _verify_report(cell_name: Optional[str]) -> str:
    if not cell_name:
        return (
            "[swarph silent-wake] CANNOT VERIFY the DM wake: no cell.yaml "
            "discovered from this cwd, so I do not know which cell this "
            "session is. If this session is a mesh cell, DMs may arrive "
            "unnoticed. Run `swarph monitor status --as <cell>` yourself."
        )
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "swarph_cli",
                "monitor",
                "status",
                "--as",
                cell_name,
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception as exc:
        return (
            f"[swarph silent-wake] CANNOT VERIFY the DM wake for "
            f"{cell_name}: `swarph monitor status` failed to run ({exc}). "
            "Treat the wake as UNARMED until proven otherwise."
        )
    # Exit-code semantics of `monitor status`: 2 = error/refusal, 1 = DMs
    # pending (NOT a failure), 0 = clean. So parse the JSON first and only
    # treat rc==2 or unparseable output as "cannot verify".
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no detail"
        return (
            f"[swarph silent-wake] CANNOT VERIFY the DM wake for "
            f"{cell_name}: `swarph monitor status` failed ({detail}). "
            "Treat the wake as UNARMED until proven otherwise."
        )
    if proc.returncode == 2 or not status.get("running"):
        return (
            f"[swarph silent-wake] WAKE NOT ARMED for {cell_name}: the "
            "swarph monitor is not running. Start it (`swarph monitor "
            f"start --as {cell_name} --sink tmux:{cell_name}`) or DMs "
            "arrive unnoticed."
        )
    push_sinks = [
        s.get("name")
        for s in status.get("sinks", [])
        if isinstance(s, dict) and s.get("is_push")
    ]
    push_sinks = [n for n in push_sinks if n]
    if push_sinks:
        return (
            f"[swarph silent-wake] DM wake ARMED for {cell_name}: swarph "
            f"monitor push sink(s) {', '.join(push_sinks)} deliver DMs into "
            "this session. Mid-session silence detection is the monitor's "
            "job (card #487), not this hook's — this verification covers "
            "session start only."
        )
    configured = status.get("configured_sinks") or []
    return (
        f"[swarph silent-wake] WAKE NOT ARMED for {cell_name}: the monitor "
        "is running but has NO push sink "
        f"(configured: {', '.join(configured) or 'none'}). DMs will land in "
        "the inbox with nothing to wake this session. Add one, e.g. "
        f"`swarph monitor start --as {cell_name} --sink tmux:{cell_name}`."
    )


def run_wake_hook_output(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="swarph wake-hook-output")
    p.add_argument(
        "--harness",
        required=True,
        help="harness that invoked this hook (baked in at install time)",
    )
    p.add_argument(
        "--cell",
        default=None,
        help="cell name baked in at install time (wins over $SWARPH_SELF "
        "and cwd discovery)",
    )
    args = p.parse_args(argv)

    _drain_stdin()

    harness = args.harness.strip().lower()
    if harness not in _KNOWN_HARNESSES:
        return _emit(
            {
                "context": (
                    f"[swarph silent-wake] UNSUPPORTED HARNESS {args.harness!r}: "
                    "swarph has no wake product for it. This session has NO "
                    "DM wake path — DMs will arrive unnoticed unless you arm "
                    "one manually. Known harnesses: "
                    + ", ".join(_KNOWN_HARNESSES)
                )
            },
            harness="claude",  # loudest portable shape for an unknown caller
        )

    cell_name = _discover_cell_name(args.cell)

    if harness in _ARM_HARNESSES:
        return _emit({"context": _arm_instruction(cell_name)}, harness=harness)
    return _emit({"context": _verify_report(cell_name)}, harness=harness)
