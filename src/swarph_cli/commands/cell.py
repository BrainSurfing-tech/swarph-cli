# src/swarph_cli/commands/cell.py
"""`swarph cell <subcommand>` — capture-at-birth operator surface.

Subcommands:
  harden <cell>   Emit the durable revival kit (launch wrapper + manifest +
                  genesis lineage). Never installs systemd units.
  verify <cell>   Fail-loud pre-spawn gate (cwd-drift + per-UUID liveness PROBE).
                  Exit 0 = safe to spawn; non-zero = refuse. Run from
                  claude-tmux@.service ExecStart before `swarph spawn`.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from swarph_shared.cell import CellError

from swarph_cli.capture import harden as _harden
from swarph_cli.capture import verify as _verify

_USAGE = """\
Usage:
  swarph cell harden <cell>    Emit the durable revival kit (no install)
  swarph cell verify <cell>    Pre-spawn gate (exit 0 = ok, non-zero = refuse)
"""


def run_cell(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph cell"
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "verify":
        return _run_verify(rest)
    if sub == "harden":
        return _run_harden(rest)
    print(f"swarph cell: unknown subcommand {sub!r}\n\n{_USAGE}", file=sys.stderr)
    return 2


def _run_verify(rest: List[str]) -> int:
    if not rest:
        print("swarph cell verify: missing <cell>", file=sys.stderr)
        return 2
    role = rest[0]
    result = _verify.verify_cell(role)
    stream = sys.stdout if result.ok else sys.stderr
    print(f"[verify {role}] {'OK' if result.ok else 'REFUSE'}: {result.reason}", file=stream)
    return result.code


def _run_harden(rest: List[str]) -> int:
    if not rest:
        print("swarph cell harden: missing <cell>", file=sys.stderr)
        return 2
    role = rest[0]
    try:
        res = _harden.harden_cell(role)
    except CellError as exc:
        print(f"swarph cell harden: {exc}", file=sys.stderr)
        return 1
    print(f"[harden {res.role}] revival kit emitted:")
    print(f"  launch:   {res.launch_script}")
    print(f"  manifest: {res.manifest_path}")
    print(f"  lineage:  {res.lineage_path}")
    print(f"  service:  {res.service}")
    print("\nEnable (commander-gated — harden does NOT install):")
    for line in res.enable_instructions:
        print(f"  {line}")
    return 0
