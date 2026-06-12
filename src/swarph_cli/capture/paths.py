"""State paths for capture artifacts, mirroring cell.session_state_path's XDG layout.

Lives beside sessions/ under $XDG_STATE_HOME/swarph (or ~/.local/state/swarph):
  sessions/<role>.session-id   (existing R5 pin store)
  lineage/<role>.jsonl         (append-only provenance log)
  captures/<role>.json         (revival-kit manifest + live-pin + reserved HEAD)
"""
from __future__ import annotations

import os
from pathlib import Path

from swarph_shared.cell import PEER_NAME_RE


class CaptureRoleError(ValueError):
    """A role string is not a safe, mesh-addressable cell name."""


def validate_role(role: str) -> str:
    """Reject any role that is not a kebab-case mesh peer name (PEER_NAME_RE).

    The single charset choke-point for every capture path (lineage / manifest)
    AND the harden/verify CLI arg. Closes the role path-traversal + injection
    class at the source: a role like ``../../../../tmp/x/forged`` or ``a$(touch
    X)`` cannot match ``^[a-z][a-z0-9-]{0,62}[a-z0-9]$``, so no path is ever
    built from it and no metacharacter ever reaches a shell. Mirrors how the
    cell.yaml ``name`` field is already validated in parse_cell_dict — the
    ``role`` field + CLI arg were the missed instances.
    """
    if not isinstance(role, str) or not PEER_NAME_RE.match(role):
        raise CaptureRoleError(
            f"unsafe role {role!r}: must be a kebab-case, mesh-addressable cell "
            f"name matching {PEER_NAME_RE.pattern} (no path separators, no shell "
            f"metacharacters)."
        )
    return role


def _assert_contained(path: Path, base: Path) -> Path:
    """Defense in depth (mirrors import_session.py:148): the resolved write
    target must be a DIRECT child of ``base``. Belt to validate_role's
    suspenders — a future un-validated caller still can't escape the dir."""
    if path.resolve().parent != base.resolve():
        raise CaptureRoleError(
            f"refusing to build a capture path outside {base} (got {path})"
        )
    return path


def _state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "state"


def _swarph_state() -> Path:
    return _state_root() / "swarph"


def lineage_dir() -> Path:
    return _swarph_state() / "lineage"


def captures_dir() -> Path:
    return _swarph_state() / "captures"


def lineage_path(role: str) -> Path:
    validate_role(role)
    base = lineage_dir()
    return _assert_contained(base / f"{role}.jsonl", base)


def manifest_path(role: str) -> Path:
    validate_role(role)
    base = captures_dir()
    return _assert_contained(base / f"{role}.json", base)
