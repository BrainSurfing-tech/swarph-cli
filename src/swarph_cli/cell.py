"""``cell.yaml`` loader — Phase 7 spawn config (v0.6.0).

A *cell* is the unit of mesh participation: one peer-name, one role,
one working directory, one persistent session-id. The cell.yaml file
declaratively describes how to summon (spawn or resume) that cell as
a long-lived `claude` session.

Per substrate-doc R7 §11.1.5 (O5) the long-term home for the
universal-genome cell.yaml format is ``swarph-shared``. v0.6 ships
the parser inside ``swarph-cli`` to validate the schema in production
use; v0.7+ migrates to ``swarph-shared`` once the schema has stabilised.

Per substrate-doc R7 §11.1.7 the spawn wrapper sits at the
operator-tooling layer of the 4-layer R2 mechanism stack — it
consumes substrate primitives (S-A spawn-registration body, S-G
spawn-context endpoint when those land) but is NOT itself a substrate
primitive. v0.6 reads the local cell.yaml file only; v0.7 will add
the optional S-G HTTP polling fallback.

v0.6 schema (``schema_version: "v1"`` — minimal viable):

    schema_version: v1            # optional, defaults to v1
    name: lab-ovh                 # required — mesh peer name
    role: lab                     # required — claude --name display value
    cwd: /home/ubuntu             # required — working directory for spawn
    session_id: 550e8400-...      # optional — pinned UUID; persisted state used otherwise
    starter_prompt_path: ~/.foo   # optional — fed as ``claude --append-system-prompt``
    provider: claude              # optional — claude-only in v0.6 (errors otherwise)
    identity:                     # optional — alpha #891 (D1) reserved shape
      lineage:
        parent_peer_id: drop      # optional — null for top-level cells
        spawn_manifest_signature: # optional — null in v0.6, validated in v2 cryptographic-lineage tier

Fields not declared above are kept verbatim under ``cell.extra`` for
forward-compat; v0.7 may attach meaning to ``mesh:``, ``capabilities:``,
``memory_mirror:`` etc.

**Schema-stability commitment** (per drop-mother review #890 (C2) +
``feedback_swarph_paper_rev_bar``): v0.6 schema is FROZEN at
``schema_version: "v1"``. The v0.7 migration to ``swarph-shared`` is a
SYMBOL-RELOCATION ONLY — no field renames, no field removals, no type
changes. Any v0.7+ additions must be additive-optional. v0.6 cell.yaml
files keep working unchanged in v0.7+. Breaking changes require a
``schema_version: "v2"`` bump and parallel-supported-version window per
``swarph-mesh`` DEPRECATIONS discipline.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Conservative peer-name pattern, mirrors swarph_shared.peer_registry
# discipline — kebab/snake-case, no spaces, no leading hyphen.
_PEER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_VALID_PROVIDERS_V0_6 = {"claude"}


class CellError(ValueError):
    """Raised on cell.yaml validation or lookup failure."""


SCHEMA_VERSION_V1 = "v1"
_VALID_SCHEMA_VERSIONS = {SCHEMA_VERSION_V1}


@dataclass
class Lineage:
    """Optional lineage block — alpha #891 (D1) reserved shape.

    v0.6 accepts presence + parses; semantic validation (signature
    verification) graduates with the v2 cryptographic-lineage tier
    per substrate-doc R6 §11.1.2 candidate primitive S-B.
    """

    parent_peer_id: Optional[str] = None
    spawn_manifest_signature: Optional[str] = None


@dataclass
class Cell:
    """Parsed cell.yaml — v0.6 schema (``schema_version: "v1"``)."""

    name: str
    role: str
    cwd: Path
    schema_version: str = SCHEMA_VERSION_V1
    session_id: Optional[str] = None
    starter_prompt_path: Optional[Path] = None
    provider: str = "claude"
    lineage: Optional[Lineage] = None
    source_path: Optional[Path] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def starter_prompt_text(self) -> Optional[str]:
        """Return contents of starter_prompt_path or None.

        Raises CellError if the path is set but unreadable so spawn
        fails loudly rather than silently dropping the role-priming.
        """
        if self.starter_prompt_path is None:
            return None
        try:
            return self.starter_prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CellError(
                f"cell.yaml: starter_prompt_path "
                f"'{self.starter_prompt_path}' is not readable: {exc}"
            ) from exc


def _config_root() -> Path:
    """Return the active config root for cell lookups.

    Honours ``$XDG_CONFIG_HOME`` per the XDG Base Directory spec; falls
    back to ``~/.config/`` otherwise. The trailing ``swarph/cells/``
    segment is appended by callers, so this returns the parent.
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
    The first sibling spawn (when slot 1 is occupied) returns
    ``<base_role>-2`` — slot 1 is reserved for the cell.yaml-named
    role exactly so sibling-N=1 confusion doesn't arise.

    The base role MUST already be occupied (sidecar exists) before
    calling this; the caller is responsible for that check, since
    ``--new-instance`` on an unoccupied base role is a degenerate
    case (commander wants a sibling but has no original — should
    spawn the original first via default ``swarph spawn <role>``).

    Hard cap at slot 99 to avoid runaway loops; if 99 is full the
    operator has bigger problems than auto-suffix policy can solve.
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
         (alpha #891 D3)
      4. ``<cells_dir>/<spec>.yaml`` exists → use it
      5. v0.7 PR-B sibling-resume — strip trailing ``-N`` suffix from spec
         and try ``<cells_dir>/<base-role>.yaml`` (lets ``swarph spawn
         drop-on-meta-edge-2`` resolve back to base cell.yaml so siblings
         share cell-context). Honors operator-intent: explicit base file
         takes precedence (step 4) over slot-stripped fallback (step 5).
      6. otherwise → return ``<cells_dir>/<spec>.yaml`` (will fail on load
         with a 'not found' error)

    Mesh-gateway URL inputs (``mesh-gateway://peers/<peer-id>/spawn-context``)
    are caught by ``is_mesh_gateway_url`` BEFORE this function and return
    NotImplementedError — the S-G substrate primitive lands in v0.7+.
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
    """Return ``./cell.yaml`` if it exists in the current cwd, else None.

    Implements alpha #891 (D3) auto-discovery for the no-positional case.
    """
    candidate = Path.cwd() / "cell.yaml"
    return candidate if candidate.is_file() else None


def _validate_uuid(value: str) -> str:
    """Validate-and-normalise a UUID string; raise CellError otherwise.

    ``claude --session-id`` rejects non-UUIDs at the harness layer;
    catching it here gives a substrate-shaped error path instead of a
    bare claude-cli traceback.
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CellError(f"cell.yaml: session_id is not a valid UUID: {value!r}") from exc


def load_cell(path: Path) -> Cell:
    """Parse + validate a cell.yaml file. Raises CellError on any failure."""
    import yaml  # local import — keeps `swarph --version` PyYAML-free

    if not path.exists():
        raise CellError(f"cell.yaml not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CellError(f"cell.yaml is not valid YAML ({path}): {exc}") from exc

    if not isinstance(raw, dict):
        raise CellError(
            f"cell.yaml top-level must be a mapping ({path}); got {type(raw).__name__}"
        )

    schema_version = raw.pop("schema_version", SCHEMA_VERSION_V1)
    name = raw.pop("name", None)
    role = raw.pop("role", None)
    cwd_raw = raw.pop("cwd", None)
    session_id = raw.pop("session_id", None)
    starter_prompt_raw = raw.pop("starter_prompt_path", None)
    provider = raw.pop("provider", "claude")
    identity = raw.pop("identity", None)

    if schema_version not in _VALID_SCHEMA_VERSIONS:
        raise CellError(
            f"cell.yaml: schema_version {schema_version!r} is not supported "
            f"by this swarph-cli build. Supported: {sorted(_VALID_SCHEMA_VERSIONS)}."
        )

    if not isinstance(name, str) or not _PEER_NAME_RE.match(name):
        raise CellError(
            f"cell.yaml: 'name' must be a kebab/snake-case peer name "
            f"matching {_PEER_NAME_RE.pattern}; got {name!r}"
        )
    if not isinstance(role, str) or not role.strip():
        raise CellError("cell.yaml: 'role' is required and must be a non-empty string")
    if not isinstance(cwd_raw, str) or not cwd_raw.strip():
        raise CellError("cell.yaml: 'cwd' is required and must be a non-empty string")

    cwd = Path(cwd_raw).expanduser()
    if not cwd.is_absolute():
        # Resolve relative to cell.yaml's parent dir for ergonomic
        # author-from-anywhere config files.
        cwd = (path.parent / cwd).resolve()
    if not cwd.is_dir():
        raise CellError(f"cell.yaml: 'cwd' is not a directory: {cwd}")

    if session_id is not None:
        if not isinstance(session_id, str):
            raise CellError(
                f"cell.yaml: 'session_id' must be a string UUID, got "
                f"{type(session_id).__name__}"
            )
        session_id = _validate_uuid(session_id)

    starter_path: Optional[Path] = None
    if starter_prompt_raw is not None:
        if not isinstance(starter_prompt_raw, str) or not starter_prompt_raw.strip():
            raise CellError(
                "cell.yaml: 'starter_prompt_path' must be a non-empty string"
            )
        starter_path = Path(starter_prompt_raw).expanduser()
        if not starter_path.is_absolute():
            starter_path = (path.parent / starter_path).resolve()

    if provider not in _VALID_PROVIDERS_V0_6:
        raise CellError(
            f"cell.yaml: provider {provider!r} is not supported in v0.6 "
            f"(valid: {sorted(_VALID_PROVIDERS_V0_6)}). "
            "Non-Claude provider spawn is queued for v0.7+."
        )

    lineage_obj: Optional[Lineage] = None
    if identity is not None:
        if not isinstance(identity, dict):
            raise CellError(
                f"cell.yaml: 'identity' must be a mapping; got "
                f"{type(identity).__name__}"
            )
        lineage_raw = identity.get("lineage")
        if lineage_raw is not None:
            if not isinstance(lineage_raw, dict):
                raise CellError(
                    "cell.yaml: 'identity.lineage' must be a mapping"
                )
            lineage_obj = Lineage(
                parent_peer_id=lineage_raw.get("parent_peer_id"),
                spawn_manifest_signature=lineage_raw.get(
                    "spawn_manifest_signature"
                ),
            )

    return Cell(
        name=name,
        role=role.strip(),
        cwd=cwd,
        schema_version=schema_version,
        session_id=session_id,
        starter_prompt_path=starter_path,
        provider=provider,
        lineage=lineage_obj,
        source_path=path,
        extra=raw,  # whatever's left — preserved for forward-compat
    )


def load_or_create_session_id(
    role: str,
    cell: Cell,
    new_instance: bool = False,
) -> tuple[str, bool, str]:
    """Resolve the session-id for a spawn invocation.

    Returns ``(session_id, was_generated, effective_role)`` where:
      * ``session_id`` — UUID to pass to ``claude --session-id``
      * ``was_generated`` — True if a fresh UUID was minted on this
        call
      * ``effective_role`` — the role string to pass to ``claude
        --name`` AND the slot key for sidecar persistence. Will
        equal ``role`` for the default re-resume path; will be a
        ``<role>-<N>`` slot-suffixed string for v0.7 PR-B
        sibling-spawn case (auto-suffix policy in
        ``next_free_slot_role``).

    Resolution order:
      1. cell.session_id (cell.yaml-pinned) — never generated, never
         affected by ``new_instance`` (a pinned UUID is operator
         intent and overrides everything). effective_role = role.
      2. ``new_instance=True`` AND base sidecar already exists —
         v0.7 PR-B auto-suffix: find next free ``<role>-N`` slot,
         mint fresh UUID, persist to slot-N sidecar. Caller can
         resume this sibling later via ``swarph spawn <role>-N``.
      3. ``new_instance=True`` AND base sidecar does NOT exist —
         degenerate case (commander wants sibling but has no
         original); treat as default-spawn path with a stderr
         warning at the CLI layer (warns in spawn.py).
      4. session_state_path(role) (last-generated for this role) —
         the default re-resume path that the R5 fix is built on.
         effective_role = role.
      5. mint new uuid4 + persist to session_state_path(role).
         effective_role = role.

    Backward-compat: this function's signature changed from v0.6
    (``(role, cell)`` returning ``(sid, gen)``) to v0.7 (added
    ``new_instance`` arg + ``effective_role`` in return tuple).
    Callers must unpack the 3-tuple now; the 2-tuple shape is no
    longer returned even on the default re-resume path.
    """
    if cell.session_id:
        return cell.session_id, False, role

    if new_instance:
        base_state = session_state_path(role)
        if base_state.exists():
            # v0.7 PR-B sibling-spawn case — auto-suffix
            sibling_role = next_free_slot_role(role)
            sibling_state = session_state_path(sibling_role)
            new_id = str(uuid.uuid4())
            sibling_state.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(sibling_state, new_id + "\n")
            return new_id, True, sibling_role
        # Degenerate case — no base session to be a sibling of.
        # Fall through to default-spawn path (treats as the original);
        # CLI layer (spawn.py) prints a stderr note about this edge case.

    state_file = session_state_path(role)
    if state_file.exists():
        existing = state_file.read_text(encoding="utf-8").strip()
        if existing:
            try:
                return _validate_uuid(existing), False, role
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
        # Best-effort cleanup of the tempfile on any error path.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
