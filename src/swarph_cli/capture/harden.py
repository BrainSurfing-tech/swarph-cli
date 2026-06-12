# src/swarph_cli/capture/harden.py
"""`swarph cell harden` — emit a cell's durable revival kit (spec §4.2 / §9 step 1).

EMITS artifacts; never installs. `systemctl enable` stays commander-gated. Writes:
  - launch-<role>.sh wrapper (exec swarph spawn <role>)
  - the capture manifest (recipe/pin/service/lineage pointers + reserved HEAD)
  - a genesis lineage record IF none exists (idempotent)
and prints (returns) the enable instructions for the claude-tmux@<role> unit.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from swarph_shared.cell import Cell, CellError

from swarph_cli.cell import (
    _read_session_sidecar,
    cells_dir,
    load_cell,
    resolve_cell_path,
    session_state_path,
    _config_root,
)
from swarph_cli.capture import lineage, manifest, paths
from swarph_cli.capture.paths import validate_role


@dataclass
class HardenResult:
    role: str
    launch_script: str
    manifest_path: str
    lineage_path: str
    service: str
    enable_instructions: List[str] = field(default_factory=list)


def _resolve_cell(role: str) -> Cell:
    return load_cell(resolve_cell_path(role))


def _read_pin_uuid(role: str) -> Optional[str]:
    uuid_str, _cwd = _read_session_sidecar(session_state_path(role))
    return uuid_str


def _launch_dir() -> Path:
    return _config_root() / "swarph"


def _write_launch_wrapper(role: str) -> Path:
    # role is PEER_NAME_RE-validated upstream, so it can't carry a path
    # separator or shell metacharacter — `exec swarph spawn <role>` is safe.
    validate_role(role)
    parent = _launch_dir()
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"launch-{role}.sh"
    body = (
        "#!/bin/bash -l\n"
        f"# Auto-emitted by `swarph cell harden {role}`. Runs as the tmux\n"
        "# session command; exec-replaces with the resumed cell session.\n"
        f"exec swarph spawn {role}\n"
    )
    # Atomic + symlink-safe: write a fresh mkstemp file (no O_NOFOLLOW needed —
    # mkstemp creates a brand-new name) at mode 0o700, then os.replace over any
    # pre-planted target. The old write_text-then-chmod followed a planted
    # symlink and left a world-exec (0o755+) wrapper — the file systemd execs.
    fd, tmp = tempfile.mkstemp(prefix=f".launch-{role}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(body)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o700)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def harden_cell(role: str) -> HardenResult:
    validate_role(role)  # CLI arg charset gate — before any fs/path build
    cell = _resolve_cell(role)  # raises CellError if recipe missing — fail loud
    service = f"claude-tmux@{role}.service"

    launch = _write_launch_wrapper(role)
    pin_uuid = _read_pin_uuid(role)

    # Lineage is keyed by the CELL IDENTITY (cell.role), NOT the requested role
    # arg. When they diverge (e.g. `harden drop-2` slot-strips to drop.yaml so
    # cell.role=drop), record_genesis writes drop.jsonl — so the idempotency
    # guard + the manifest pointer MUST also use cell.role, else every re-harden
    # appends a DUPLICATE genesis and the manifest points at a phantom file.
    lineage_role = cell.role
    lineage_file = paths.lineage_path(lineage_role)

    # Re-harden on a LIVE cell must not clobber its live-pin: write_manifest
    # is a full overwrite, and a None holder here would blind the verify
    # gate's double-resume probe (the exact footgun this primitive prevents).
    existing = manifest.read_manifest(role)
    existing_holder = (existing or {}).get("head", {}).get("live_pin_holder")

    manifest.write_manifest(
        role,
        recipe=str(cell.source_path) if cell.source_path else str(cells_dir() / f"{role}.yaml"),
        pin=str(session_state_path(role)),
        service=service,
        lineage=str(lineage_file),
        session_id=pin_uuid,
        live_pin_holder=existing_holder,
    )

    if not lineage.lineage_exists(lineage_role) and pin_uuid:
        cursor_path = cell.extra.get("cursor_path") if cell.extra else None
        lineage.record_genesis(cell, session_id=pin_uuid, cursor_path=cursor_path)

    instructions = [
        f"# Revival kit emitted for {role!r} — install is commander-gated.",
        f"# 1. Drop the template (once per host): cp deploy/sidecar/claude-tmux@.service "
        f"~/.config/systemd/user/   (or /etc/systemd/system/ for a root cell)",
        f"# 2. Enable this cell:  systemctl --user enable --now {service}",
        f"#    (root cell:        sudo systemctl enable --now {service})",
        f"# 3. Non-root cells need linger:  loginctl enable-linger $USER",
    ]
    return HardenResult(
        role=role,
        launch_script=str(launch),
        manifest_path=str(paths.manifest_path(role)),
        lineage_path=str(lineage_file),  # the REAL lineage file (cell.role), not a phantom
        service=service,
        enable_instructions=instructions,
    )
