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

_ARM_HARNESSES = ("claude", "codex", "muse")
_INJECT_HARNESSES = ("antigravity",)
_VERIFY_HARNESSES = ("cursor",)
_KNOWN_HARNESSES = _ARM_HARNESSES + _INJECT_HARNESSES + _VERIFY_HARNESSES


_FILTER_MODULE = "swarph_cli.scripts.dm_notify_filter"


def _read_stdin_payload() -> dict[str, Any]:
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            # PS 5.1 native pipes prefix a (double) UTF-8 BOM; strip before
            # parse or the hook is an invisible no-op on that membrane.
            if raw.strip():
                return json.loads(raw.lstrip("\ufeff"))
    except Exception:
        pass
    return {}


def _emit(payload: dict[str, Any], *, harness: str) -> int:
    """Emit context in the shape the calling harness expects.

    The unknown-harness refusal emits BOTH known envelope shapes in one
    JSON object — a harness reads the key it knows and ignores the other.
    This is still a guess: a harness with a third shape renders nothing,
    and the refusal is silent there. That limit is not testable from here
    (PR #254 review, finding 1), so it is stated rather than asserted away.
    """
    context = payload.get("context", "")
    if harness in _VERIFY_HARNESSES:
        # Cursor sessionStart: {"env": {...}, "additional_context": "..."}
        print(json.dumps({"additional_context": context}))
    elif harness in _INJECT_HARNESSES:
        # Antigravity PreInvocation shape: {"injectSteps": [{"ephemeralMessage": "..."}]}
        if not context:
            print(json.dumps({"injectSteps": []}))
        else:
            print(
                json.dumps(
                    {
                        "injectSteps": [
                            {
                                "ephemeralMessage": context,
                            }
                        ]
                    }
                )
            )

    elif harness in _ARM_HARNESSES:
        # Claude Code / Codex SessionStart shape.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
            )
        )
    else:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    },
                    "additional_context": context,
                }
            )
        )

    return 0


def _tmux_session_cell() -> Optional[tuple[str, str]]:
    """Session-local identity: the tmux session name, validated against
    cells_dir. Returns (name, source) or None.

    The precedent is the Stop hook in the same settings file, which resolves
    identity from `tmux display-message -p '#S'`. Copy its RESOLUTION, never
    its FALLBACK (card #527): a session outside tmux must fall THROUGH to the
    next source, never default to the box owner. An unknown session name
    falls through too — a tmux session called "scratch" must not invent a
    cell called "scratch".
    """
    if not os.environ.get("TMUX"):
        return None
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except Exception:
        return None
    session = proc.stdout.strip()
    if proc.returncode != 0 or not session:
        return None
    candidate = cells_dir() / f"{session}.yaml"
    if not candidate.is_file():
        return None
    try:
        cell = load_cell(candidate)
    except CellError:
        return None
    name = getattr(cell, "name", None) or getattr(cell, "role", None) or session
    return str(name), f"tmux session '{session}'"


def _resolve_cell(explicit: Optional[str] = None) -> tuple[Optional[str], str]:
    """Cell-name resolution, MOST-LOCAL first. Returns (name, source).

    1. tmux session name (session-local, validated against cells_dir) —
       outranks ``--cell`` BECAUSE the defect in card #527 is a box-global
       baked name lying to every session on a multi-cell box; the
       session-local signal must outrank it or the fix is void
    2. ``--cell`` baked into the installed hook command (explicit, but only
       as local as the scope it was installed at)
    3. ``$SWARPH_SELF`` — the mesh's existing self-name convention
       (``swarph monitor --as`` honors it too). On a shared box this is
       THE BOX OWNER'S identity (mesh's own --as help says so; the box-wide
       settings env may pin it, card #360), so the source is reported
       alongside the name: a verified verdict about the wrong cell is worse
       than no verdict.
    4. cwd discovery: ./cell.yaml, then cells_dir/<basename(cwd)>.yaml —
       weakest, since harness sessions do not necessarily start in a
       cell-named directory
    5. unresolved — the caller must REFUSE LOUDLY, never guess
    """
    tmux = _tmux_session_cell()
    if tmux is not None:
        name, source = tmux
        # An overridden --cell must be NAMED, never silently ignored
        # (lab-ovh on #527: same defect shape as an ignored filter
        # returning an unfiltered superset that looks filtered). The
        # precedence stands — the override is reported, not swallowed.
        if explicit and explicit != name:
            source = f"{source} (overrode install-time --cell '{explicit}')"
        return name, source
    if explicit:
        return explicit, "install-time --cell"
    env_self = os.environ.get("SWARPH_SELF", "").strip()
    if env_self:
        return env_self, "$SWARPH_SELF"
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
            return str(name), f"cwd discovery ({path})"
    return None, "unresolved"


def _sidecar_dir(cell_name: str) -> Path:
    # Mirrors mesh._default_sidecar_state_dir without importing the mesh
    # command module into the hook's startup path.
    return Path.home() / "swarph_state" / cell_name / "mesh-sidecar"


def _provenance_note(source: str) -> str:
    """Name the resolution source so a wrong-cell outcome is VISIBLE. The
    $SWARPH_SELF case gets the full warning: on a shared box it is the box
    owner's identity (card #360), and a confident line about the wrong cell
    is worse than no line."""
    if source == "$SWARPH_SELF":
        return (
            " (identity from $SWARPH_SELF — on a shared box that is the box "
            "owner's cell; if this session is not that cell, this line is "
            "about the wrong wake)"
        )
    return f" (identity from {source})"


def _arm_instruction(cell_name: Optional[str], source: str = "unresolved") -> str:
    interpreter = sys.executable
    if not cell_name:
        # CANNOT-RESOLVE is a LOUD REFUSAL, never a placeholder (card #527
        # task 1): an instruction that says "substitute <cell> yourself"
        # invites the session to guess — and on a shared box the guess is
        # the box-wide $SWARPH_SELF, i.e. the box owner, i.e. the F3
        # wrong-cell hazard armed by the session's own hand.
        return (
            "[swarph silent-wake] CANNOT RESOLVE which cell this session is: "
            "no tmux session name matching a cell, no install-time --cell, "
            "no $SWARPH_SELF, no cell.yaml discoverable from this cwd. "
            "DO NOT arm a DM watch for a guessed cell — a watch on the wrong "
            "inbox is worse than none: it looks armed while your own DMs "
            "arrive unnoticed. Once you know which cell this session is, arm "
            "it yourself: `tail -n 0 -F \"$HOME/swarph_state/<cell>/mesh-sidecar/"
            f"inbox.log\" | \"{interpreter}\" -u -m {_FILTER_MODULE}` and verify "
            "with `swarph monitor status --as <cell>`."
        )
    inbox = _sidecar_dir(cell_name) / "inbox.log"
    tail = f'tail -n 0 -F "{inbox}"'
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
        "`swarph monitor status`.\n"
        "ARM EXACTLY ONE WAKE SOURCE: if a swarph monitor push sink "
        "(tmux:<cell>) or another watch already wakes this cell, do NOT "
        "arm a second one. Overlapping wakes can abort an in-flight turn "
        "(grok-researcher transcript probe, 2026-08-18: 13 historical "
        "mid_turn_aborts from watchdog+tmux wake overlap), and the abort "
        "is invisible in every success counter."
        + _provenance_note(source)
    )


def _verify_report(cell_name: Optional[str], source: str = "unresolved") -> str:
    if not cell_name:
        return (
            "[swarph silent-wake] CANNOT VERIFY the DM wake: every "
            "resolution source failed — no tmux session name matching a "
            "cell, no install-time --cell, no $SWARPH_SELF, no cell.yaml "
            "discoverable from this cwd — so I do not know which cell this "
            "session is. If this session is a mesh cell, DMs may arrive "
            "unnoticed. Run `swarph monitor status --as <cell>` yourself."
        )
    # Finding 3 (PR #254 review): a name resolved from the ambient
    # environment can be the BOX OWNER'S identity on a shared box. Name
    # the source in the verdict so a wrong-cell verification is visible
    # in the message rather than hidden behind a confident ARMED.
    provenance = (
        f" (identity from {source} — on a shared box that is the box "
        "owner's cell; if this session is not that cell, this verdict "
        "is about the wrong wake)"
        if source == "$SWARPH_SELF"
        else f" (identity from {source})"
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
    # Finding 2 (PR #254 review): an absent field is schema drift, not a
    # negative — route it to CANNOT VERIFY rather than reporting a
    # specific wrong diagnosis with a specific wrong remedy.
    if proc.returncode == 2:
        return (
            f"[swarph silent-wake] WAKE NOT ARMED for {cell_name}: the "
            "swarph monitor is not running. Start it (`swarph monitor "
            f"start --as {cell_name} --sink tmux:{cell_name}`) or DMs "
            "arrive unnoticed." + provenance
        )
    if "running" not in status:
        return (
            f"[swarph silent-wake] CANNOT VERIFY the DM wake for "
            f"{cell_name}: `monitor status` output has no 'running' field "
            "(schema drift?). Treat the wake as UNARMED until proven "
            "otherwise." + provenance
        )
    if not status["running"]:
        return (
            f"[swarph silent-wake] WAKE NOT ARMED for {cell_name}: the "
            "swarph monitor is not running. Start it (`swarph monitor "
            f"start --as {cell_name} --sink tmux:{cell_name}`) or DMs "
            "arrive unnoticed." + provenance
        )
    if "sinks" not in status:
        return (
            f"[swarph silent-wake] CANNOT VERIFY the DM wake for "
            f"{cell_name}: `monitor status` output has no 'sinks' field "
            "(schema drift?). Treat the wake as UNARMED until proven "
            "otherwise." + provenance
        )
    sinks = [s for s in status["sinks"] if isinstance(s, dict)]
    push_sinks = [
        s["name"] for s in sinks if s.get("is_push") is True and s.get("name")
    ]
    if push_sinks:
        return (
            f"[swarph silent-wake] DM wake ARMED for {cell_name}: swarph "
            f"monitor push sink(s) {', '.join(push_sinks)} deliver DMs into "
            "this session. Mid-session silence detection is the monitor's "
            "job (card #487), not this hook's — this verification covers "
            "session start only." + provenance
        )
    if any("is_push" not in s for s in sinks):
        return (
            f"[swarph silent-wake] CANNOT VERIFY the DM wake for "
            f"{cell_name}: sink entries lack the 'is_push' field (schema "
            "drift?), so push coverage cannot be determined. Treat the "
            "wake as UNARMED until proven otherwise." + provenance
        )
    configured = status.get("configured_sinks") or []
    return (
        f"[swarph silent-wake] WAKE NOT ARMED for {cell_name}: the monitor "
        "is running but has NO push sink "
        f"(configured: {', '.join(str(c) for c in configured) or 'none'}). "
        "DMs will land in the inbox with nothing to wake this session. "
        f"Add one, e.g. `swarph monitor start --as {cell_name} --sink "
        f"tmux:{cell_name}`." + provenance
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
        help="cell name baked in at install time (outranked by the tmux "
        "session name, wins over $SWARPH_SELF and cwd discovery)",
    )
    args = p.parse_args(argv)

    stdin_data = _read_stdin_payload()

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
            harness="unknown",  # dual-envelope refusal; see _emit
        )

    # Antigravity PreInvocation fires per invocation. Only inject on the first turn (invocationNum == 1)
    # to prevent turn-by-turn context spam.
    if harness == "antigravity":
        inv_num = stdin_data.get("invocationNum")
        if isinstance(inv_num, int) and inv_num > 1:
            return _emit({"context": ""}, harness=harness)

    cell_name, cell_source = _resolve_cell(args.cell)

    if harness in _ARM_HARNESSES:
        return _emit(
            {"context": _arm_instruction(cell_name, cell_source)}, harness=harness
        )
    return _emit(
        {"context": _verify_report(cell_name, cell_source)}, harness=harness
    )

