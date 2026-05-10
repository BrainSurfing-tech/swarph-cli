"""``swarph_cli.cell`` — file I/O + sidecar + slot allocation (v0.7 PR-D step 2).

After substrate-doc R7 §11.1.5 (O5) cell.yaml universal-genome relocation,
the data shapes + schema validation live in ``swarph-shared`` (v0.3.0+).
This module is the swarph-cli operator-tooling layer that consumes the
shared schema + adds:

* **File discovery**: ``cells_dir()``, ``discover_cell_in_cwd()``,
  ``resolve_cell_path()``, ``is_mesh_gateway_url()``
* **File I/O**: ``load_cell()`` (YAML read → ``parse_cell_dict()``),
  ``_atomic_write_text()``
* **Sidecar persistence**: ``session_state_path()``,
  ``load_or_create_session_id()``
* **Slot allocation** (substrate-doc R7 §11.1.7 operator-tooling layer
  per beta #892 B1): ``next_free_slot_role()``,
  ``base_role_from_slot_role()``

Symbol-relocation only — the v0.6 + v0.7-pre-PR-D + v0.7-post-PR-D-step-2
APIs are byte-for-byte identical at the swarph_cli.cell import level.
v0.6 cell.yaml files keep working unchanged in v0.7+ per drop-mother
review #890 (C2) schema-stability commitment.

Re-export shim for backward-compat — historical imports still work:

    from swarph_cli.cell import Cell, CellError, parse_cell_dict

is equivalent to (and recommended going forward):

    from swarph_shared.cell import Cell, CellError, parse_cell_dict

Internal swarph-cli code uses the swarph-shared imports directly to
avoid two-hop indirection. External consumers of swarph-cli's cell
module continue to work unchanged.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

# Re-export data shapes + schema validation from swarph-shared 0.3.0+
# (substrate-doc R7 §11.1.5 (O5) cell.yaml-format-home RESOLVED).
from swarph_shared.cell import (
    Cell,
    CellError,
    Lineage,
    PEER_NAME_RE,
    SCHEMA_VERSION_V1,
    VALID_PROVIDERS,
    VALID_SCHEMA_VERSIONS,
    parse_cell_dict,
    validate_uuid_str,
)

# Backward-compat aliases for v0.6 + v0.7-pre-PR-D-step-2 imports
# (a few internal swarph-cli call sites used the leading-underscore shape).
_PEER_NAME_RE = PEER_NAME_RE
_VALID_SCHEMA_VERSIONS = VALID_SCHEMA_VERSIONS
_VALID_PROVIDERS_V0_6 = VALID_PROVIDERS  # historical name for the v0.6 frozenset
_validate_uuid = validate_uuid_str  # historical helper name


def _config_root() -> Path:
    """Return the active config root for cell lookups.

    Honours ``$XDG_CONFIG_HOME`` per the XDG Base Directory spec; falls
    back to ``~/.config/`` otherwise.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def cells_dir() -> Path:
    """Default lookup directory for ``<role>.yaml`` files."""
    return _config_root() / "swarph" / "cells"


def _state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "state"


def session_state_path(role: str) -> Path:
    """Return the per-role persisted-session-id file path.

    v0.6 persists generated UUIDs OUTSIDE cell.yaml so the cell file
    stays purely declarative and is safe to commit to git. Roles
    re-spawned with the same role name resume the same session via
    this state file.

    v0.7 PR-B (beta #892 B1) extends this to per-slot sidecars for
    sibling instances: ``<role>.session-id`` is slot 1; ``<role>-2``,
    ``<role>-3`` etc. are siblings minted via ``--new-instance``.
    See ``next_free_slot_role()`` for the slot allocation policy.
    """
    return _state_root() / "swarph" / "sessions" / f"{role}.session-id"


def next_free_slot_role(base_role: str) -> str:
    """Find the next free `<base_role>-N` slot for a sibling spawn.

    v0.7 PR-B (beta #892 B1). Auto-suffix policy: siblings beyond
    the first slot append ``-2``, ``-3``, ``-4`` etc. The naming
    suffix matches the slot index, so:

    * Slot 1 (the original): ``<base_role>``
    * Slot 2 (first sibling): ``<base_role>-2``
    * Slot 3 (second sibling): ``<base_role>-3``

    Returns the synthesised role string for the next free slot.

    Hard cap at slot 99 to avoid runaway loops.

    Slot-reuse on unclean exit (beta iter-1 #987): a sibling instance
    that dies without removing its sidecar leaves the slot
    sidecar-occupied. Operator workaround at v0.7: ``rm`` the stale
    sidecar. v0.8+ may ship ``swarph cleanup-sessions``.
    """
    for n in range(2, 100):
        candidate = f"{base_role}-{n}"
        if not session_state_path(candidate).exists():
            return candidate
    raise CellError(
        f"next_free_slot_role: 99 sibling slots already occupied for "
        f"base role {base_role!r}. Manual cleanup needed at "
        f"{_state_root() / 'swarph' / 'sessions'}/."
    )


def base_role_from_slot_role(role: str) -> str:
    """Strip a trailing ``-N`` slot suffix from a role string.

    v0.7 PR-B. Lets ``swarph spawn <base-role>-2`` resolve back to
    the cell.yaml at ``<base-role>.yaml`` for shared cell-context
    (same cwd, starter-prompt, lineage, etc.) while the sidecar
    + display name use the slot-suffixed role.

    Returns ``role`` unchanged if no trailing ``-<N>`` suffix is
    present (preserves v0.6 behavior for non-sibling spawns).
    """
    parts = role.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and 2 <= int(parts[1]) <= 99:
        return parts[0]
    return role


_MESH_GATEWAY_URL_PREFIX = "mesh-gateway://"


def is_mesh_gateway_url(spec: str) -> bool:
    """True for v0.7+ ``mesh-gateway://...`` URL inputs (alpha #891 D2)."""
    return spec.startswith(_MESH_GATEWAY_URL_PREFIX)


def resolve_cell_path(spec: str) -> Path:
    """Resolve a ``swarph spawn`` positional/flag value to a cell file.

    Precedence:
      1. spec ends in ``.yaml`` or ``.yml`` → treated as a literal path
      2. spec contains a path separator → treated as a literal path
      3. ``./cell.yaml`` exists in current cwd AND spec equals current cwd's
         basename OR equals a special token ``.`` → use ``./cell.yaml``
      4. ``<cells_dir>/<spec>.yaml`` exists → use it
      5. v0.7 PR-B sibling-resume — strip trailing ``-N`` suffix from spec
         and try ``<cells_dir>/<base-role>.yaml`` (lets ``swarph spawn
         drop-on-meta-edge-2`` resolve back to base cell.yaml so siblings
         share cell-context). Honors operator-intent: explicit base file
         takes precedence (step 4) over slot-stripped fallback (step 5).
      6. otherwise → return ``<cells_dir>/<spec>.yaml`` (will fail on load
         with a 'not found' error)
    """
    if spec == ".":
        return Path.cwd() / "cell.yaml"
    if spec.endswith((".yaml", ".yml")) or os.sep in spec:
        return Path(spec).expanduser()

    direct = cells_dir() / f"{spec}.yaml"
    if direct.is_file():
        return direct

    # v0.7 PR-B sibling-resume fallback
    base = base_role_from_slot_role(spec)
    if base != spec:
        base_path = cells_dir() / f"{base}.yaml"
        if base_path.is_file():
            return base_path

    return direct  # not found; let load_cell raise with a clear error


def discover_cell_in_cwd() -> Optional[Path]:
    """Return ``./cell.yaml`` if it exists in the current cwd, else None."""
    candidate = Path.cwd() / "cell.yaml"
    return candidate if candidate.is_file() else None


def read_starter_prompt(cell: Cell) -> Optional[str]:
    """Return the starter-prompt text for ``cell``, or None.

    Free function rather than ``Cell.starter_prompt_text()`` method —
    swarph-shared Cell is intentionally pure-stdlib + no I/O, so the
    file-read lives at the swarph-cli operator-tooling layer. Callers
    that previously did ``cell.starter_prompt_text()`` now do
    ``read_starter_prompt(cell)``.

    Raises CellError if starter_prompt_path is set but unreadable, so
    spawn fails loudly rather than silently dropping role-priming.
    """
    if cell.starter_prompt_path is None:
        return None
    try:
        return cell.starter_prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CellError(
            f"cell.yaml: starter_prompt_path "
            f"'{cell.starter_prompt_path}' is not readable: {exc}"
        ) from exc


def load_cell(path: Path) -> Cell:
    """Parse + validate a cell.yaml file. Raises CellError on any failure.

    Reads YAML from disk, then delegates to ``swarph_shared.cell.parse_cell_dict``
    for the actual schema validation. Sets ``cell.source_path`` to the
    file path (a swarph-cli-specific provenance bit; swarph-shared
    leaves it None since it doesn't know about the file).
    """
    import yaml  # local import — keeps `swarph --version` PyYAML-free

    if not path.exists():
        raise CellError(f"cell.yaml not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CellError(f"cell.yaml is not valid YAML ({path}): {exc}") from exc

    cell = parse_cell_dict(raw, source=str(path), base_dir=path.parent)
    # File-I/O wrapper concern: post-validate cwd reachability + tag source
    # path. swarph-shared parse_cell_dict intentionally doesn't touch the
    # filesystem (kernel-tier discipline); the live cwd.is_dir() check is
    # the swarph-cli operator-tooling concern.
    if not cell.cwd.is_dir():
        raise CellError(f"cell.yaml: 'cwd' is not a directory: {cell.cwd}")
    cell.source_path = path
    return cell


def load_or_create_session_id(
    role: str,
    cell: Cell,
    new_instance: bool = False,
) -> tuple[str, bool, str]:
    """Resolve the session-id for a spawn invocation.

    Returns ``(session_id, was_generated, effective_role)``.

    Resolution order:
      1. cell.session_id (cell.yaml-pinned) — never generated
      2. ``new_instance=True`` AND base sidecar exists — auto-suffix slot
      3. ``new_instance=True`` AND no base sidecar — fall through (degenerate)
      4. session_state_path(role) reused — default re-resume path
      5. mint new uuid4 + persist

    Caller-side discipline (mother iter-1 #986): the ``effective_role``
    value is the authoritative source for ``claude --name`` AND
    sidecar persistence — NOT ``cell.role`` (which stays the BASE role
    for shared cell-context: cwd, starter prompt, lineage, provider).
    """
    if cell.session_id:
        return cell.session_id, False, role

    if new_instance:
        base_state = session_state_path(role)
        if base_state.exists():
            sibling_role = next_free_slot_role(role)
            sibling_state = session_state_path(sibling_role)
            new_id = str(uuid.uuid4())
            sibling_state.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(sibling_state, new_id + "\n")
            return new_id, True, sibling_role

    state_file = session_state_path(role)
    if state_file.exists():
        existing = state_file.read_text(encoding="utf-8").strip()
        if existing:
            try:
                return validate_uuid_str(existing), False, role
            except CellError:
                # Corrupted state — fall through and regenerate.
                pass

    new_id = str(uuid.uuid4())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(state_file, new_id + "\n")
    return new_id, True, role


def _atomic_write_text(target: Path, content: str) -> None:
    """Write text atomically: tempfile in the same dir, fsync, rename.

    Per drop-mother review #890 (C1) — UUID writes are load-bearing for
    R5 (session-resume identity disambiguation). A torn write that left
    half a UUID in the state file would silently regenerate on next
    spawn, defeating the disambiguation primitive entirely.
    """
    import tempfile

    parent = target.parent
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = [
    # Re-exports from swarph_shared.cell (v0.3.0+)
    "Cell",
    "CellError",
    "Lineage",
    "PEER_NAME_RE",
    "SCHEMA_VERSION_V1",
    "VALID_PROVIDERS",
    "VALID_SCHEMA_VERSIONS",
    "parse_cell_dict",
    "validate_uuid_str",
    # swarph-cli-local file I/O + sidecar + slot allocation
    "cells_dir",
    "session_state_path",
    "next_free_slot_role",
    "base_role_from_slot_role",
    "is_mesh_gateway_url",
    "resolve_cell_path",
    "discover_cell_in_cwd",
    "read_starter_prompt",
    "load_cell",
    "load_or_create_session_id",
    # Backward-compat aliases
    "_PEER_NAME_RE",
    "_VALID_SCHEMA_VERSIONS",
    "_VALID_PROVIDERS_V0_6",
    "_validate_uuid",
]
