# src/swarph_cli/capture/verify.py
"""`swarph cell verify` gate logic (spec §4.4b / §5 / §10).

Fail-LOUD pre-spawn gate, run from claude-tmux@.service ExecStart BEFORE spawn.
Two checks:
  (a) cwd-drift — the pinned session's .jsonl must live under cell.cwd's project
      dir, else `claude --resume` would die with "No conversation found" (the
      droplet re-seat incident). A pin whose .jsonl exists under a DIFFERENT
      project dir → REFUSE (code 3). A pin with NO .jsonl yet (unstarted) or no
      pin at all → OK (fresh genesis; spawn will mint/create).
  (b) per-UUID liveness — sweep EVERY capture manifest for this pinned UUID
      (cross-NAME: the footgun is per-UUID, not per-role — the renamed-cell
      incident had two roles pinning one UUID, each with a clean own-manifest)
      and PROBE each recorded live_pin_holder. Any ALIVE → REFUSE (code 4, a
      real double-resume). DEAD → that flag is a stale poison-pin (holder
      crashed without clearing it); clear it + ALLOW. Refusing on the stale
      flag would turn the durability fix into a durability TRAP (droplet).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from swarph_shared.cell import Cell, CellError

from swarph_cli.cell import _read_session_sidecar, load_cell, resolve_cell_path, session_state_path
from swarph_cli.capture import manifest
from swarph_cli.capture.liveness import probe_holder_liveness
from swarph_cli.capture.paths import CaptureRoleError, validate_role


@dataclass
class VerifyResult:
    ok: bool
    code: int
    reason: str


def expected_project_dir(cwd: Path) -> str:
    """Claude Code's projects/<sanitized-cwd>/ dir name: path separators → '-'.

    Cross-platform: replace '/', '\\' AND ':' so Windows paths (which
    str(Path(...)) renders with backslashes + a drive-letter colon) sanitize
    to the same shape Claude Code's projects dir uses. No-ops on POSIX '/' paths.
    """
    return str(cwd).replace("/", "-").replace("\\", "-").replace(":", "-")


def locate_session_jsonl(session_id: str) -> List[Path]:
    """All ~/.claude/projects/*/<session_id>.jsonl paths (across project dirs)."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.exists():
        return []
    return list(projects.glob(f"*/{session_id}.jsonl"))


# Indirection seams so tests patch without a real cell.yaml / pin store.
def _resolve_cell(role: str) -> Cell:
    return load_cell(resolve_cell_path(role))


def _read_pin(role: str) -> Tuple[Optional[str], Optional[str]]:
    return _read_session_sidecar(session_state_path(role))


def verify_cell(role: str) -> VerifyResult:
    # Charset gate FIRST — before the role touches the filesystem (pin read,
    # jsonl glob) or a shell. A traversal/metachar role is refused, not resolved.
    try:
        validate_role(role)
    except CaptureRoleError as exc:
        return VerifyResult(False, 2, str(exc))

    try:
        cell = _resolve_cell(role)
    except CellError as exc:
        return VerifyResult(False, 2, f"cell.yaml unresolved: {exc}")

    session_id, _recorded_cwd = _read_pin(role)
    if not session_id:
        return VerifyResult(True, 0, "no pin yet — fresh genesis, spawn will mint")

    # (a) cwd-drift gate
    jsonls = locate_session_jsonl(session_id)
    if jsonls:
        want = expected_project_dir(cell.cwd)
        if not any(p.parent.name == want for p in jsonls):
            found = ", ".join(sorted({p.parent.name for p in jsonls}))
            return VerifyResult(
                False, 3,
                f"cwd-drift: pin {session_id} lives under [{found}] but cell.cwd "
                f"resolves to project dir {want!r} — `claude --resume` from here "
                f"would die with 'No conversation found'. Re-pin or fix cell.cwd.",
            )
    # jsonls == [] → pin minted but session never ran; spawn will create it. OK.

    # (b) liveness probe — cross-NAME sweep over every manifest pinning this
    # UUID, not just this role's own (spec §4.3 blocking fix: two roles
    # pinning one UUID both look clean per-role).
    holders, corrupt = manifest.find_pin_holders(session_id)

    # FAIL-CLOSED on a corrupt manifest: it could hide a live holder of this
    # UUID, so we cannot prove no double-resume → REFUSE (never silently pass).
    if corrupt:
        return VerifyResult(
            False, 5,
            f"unparseable capture manifest(s) {corrupt} — cannot rule out a live "
            f"holder of pin {session_id}; refusing fail-closed. Inspect/repair "
            f"{', '.join(corrupt)} under the captures dir.",
        )

    cleared = []
    for clear_key, holder in holders:
        if probe_holder_liveness(holder):
            return VerifyResult(
                False, 4,
                f"double-resume refused: pin {session_id} is already LIVE under "
                f"holder {holder!r} (manifest {clear_key!r} — tmux session + live "
                f"pane). Attach via `tmux attach -t {holder}`, never a second "
                f"--resume.",
            )
        # poison-pin: holder dead → clear stale flag + keep sweeping
        manifest.clear_live_pin(clear_key)
        cleared.append(f"{clear_key!r}:{holder!r}")

    if cleared:
        return VerifyResult(
            True, 0,
            f"cleared stale live-pin(s) ({', '.join(cleared)}) — allow",
        )
    return VerifyResult(True, 0, "ok")
