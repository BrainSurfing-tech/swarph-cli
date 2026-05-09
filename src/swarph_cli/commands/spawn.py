"""``swarph spawn <role-or-path>`` — Phase 7 operator-tooling layer (v0.6.0).

Wraps ``claude`` with the three R5/R7 disambiguation flags caught in
substrate-doc R7 §11.1.1 R5 + §11.1.7:

* ``--name <role>``                — display name for ``/resume`` picker
* ``--session-id <uuid>``          — pinned UUID; resume across spawns
* ``--append-system-prompt <text>``— starter prompt injected without manual paste

Exec-replaces the current process so the spawned ``claude`` session
inherits stdio + signal handling cleanly. v0.6 supports ``provider:
claude`` only; non-Claude provider spawn lands in v0.7+ alongside the
``swarph-shared`` cell.yaml format migration (per R7 §11.1.5 (O5)).

This is intentionally a thin wrapper. The substrate primitive
(R7 §11.1.7 substrate layer — S-G ``GET /peers/<peer-id>/spawn-context``)
is NOT consumed in v0.6; v0.7 will add an optional HTTP polling
fallback so cells can bootstrap without a local cell.yaml file. The
``--onboarding mesh-gateway://...`` URL form is parsed in v0.6 but
returns NotImplementedError, so v0.6→v0.7 is a no-flag-change upgrade
for users (alpha #891 D2).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Optional

from swarph_cli import __version__
from swarph_cli.cell import (
    Cell,
    CellError,
    discover_cell_in_cwd,
    is_mesh_gateway_url,
    load_cell,
    load_or_create_session_id,
    resolve_cell_path,
)


_BANNER = """\
      ╭───╮
      │ ◉ │
   ╭──┴───┴──╮
   │  swarph │  v{version}
   ╰──┬───┬──╯       spawn │ chat │ daemon
      │ ◉ │
      ╰───╯
"""


_USAGE = """\
Usage:
  swarph spawn [<role-or-path>] [--onboarding PATH-OR-URL]
               [--dry-run] [--no-starter] [--print-id]
               [-- claude-extra-args...]

Resolution (first match wins):
  --onboarding <path-or-url>      explicit override
  <role>                          ~/.config/swarph/cells/<role>.yaml
  <path>.yaml                     literal path
  ./cell.yaml                     auto-discovered if no positional given
  mesh-gateway://...              v0.7+ — returns NotImplementedError now

Flags:
  --dry-run        Print the resolved claude command + cell summary; no exec
  --no-starter     Skip starter-prompt injection even if cell.yaml sets one
  --print-id       Print resolved session-id to stdout before exec (useful
                   for shell scripts capturing the UUID for later resume)

Anything after a literal `--` is passed through to claude unchanged
(e.g. `swarph spawn lab -- --resume` to force the resume picker).
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph spawn",
        description=(
            "Spawn a long-lived claude session as a named mesh cell. "
            "Pins display-name + session-id + starter prompt per R7 §11.1.7."
        ),
        add_help=True,
    )
    p.add_argument(
        "role_or_path",
        nargs="?",
        default=None,
        help="Role name (resolved against ~/.config/swarph/cells/) "
        "or explicit cell.yaml path. Omit to auto-discover ./cell.yaml.",
    )
    p.add_argument(
        "--onboarding",
        default=None,
        help="Explicit cell.yaml path or v0.7+ "
        "mesh-gateway://peers/<peer-id>/spawn-context URL. Overrides the "
        "positional argument and auto-discovery.",
    )
    p.add_argument(
        "--cell",
        default=None,
        help="Alias for --onboarding kept for ergonomic shell use.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved claude command + cell summary; do not exec.",
    )
    p.add_argument(
        "--no-starter",
        action="store_true",
        help="Skip --append-system-prompt injection even if cell.yaml sets "
        "starter_prompt_path. Useful when commander wants a clean "
        "starter prompt for one specific spawn.",
    )
    p.add_argument(
        "--print-id",
        action="store_true",
        help="Print resolved session-id to stdout before exec.",
    )
    p.add_argument(
        "--no-banner",
        action="store_true",
        help="Suppress the swarph startup banner on stderr.",
    )
    return p


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv on the first literal ``--`` separator."""
    if "--" not in argv:
        return argv, []
    idx = argv.index("--")
    return argv[:idx], argv[idx + 1:]


def _resolve_cell(args: argparse.Namespace) -> Cell:
    explicit = args.onboarding or args.cell
    if explicit:
        if is_mesh_gateway_url(explicit):
            raise NotImplementedError(
                f"swarph spawn: --onboarding URL form ({explicit!r}) requires "
                "the v0.7+ S-G spawn-context endpoint integration. "
                "Use a local cell.yaml path for v0.6."
            )
        path = resolve_cell_path(explicit)
    elif args.role_or_path:
        if is_mesh_gateway_url(args.role_or_path):
            raise NotImplementedError(
                f"swarph spawn: mesh-gateway:// URL form requires v0.7+ "
                "S-G spawn-context endpoint integration. Use a local "
                "cell.yaml path for v0.6."
            )
        path = resolve_cell_path(args.role_or_path)
    else:
        discovered = discover_cell_in_cwd()
        if discovered is None:
            raise CellError(
                "swarph spawn: provide a role name, cell.yaml path, "
                "--onboarding PATH, or run from a directory containing "
                "./cell.yaml"
            )
        path = discovered
    return load_cell(path)


def _build_claude_argv(
    cell: Cell,
    session_id: str,
    no_starter: bool,
    passthrough: list[str],
) -> list[str]:
    argv: list[str] = ["claude", "--name", cell.role, "--session-id", session_id]

    if not no_starter:
        starter = cell.starter_prompt_text()
        if starter:
            argv.extend(["--append-system-prompt", starter])

    argv.extend(passthrough)
    return argv


def _print_banner() -> None:
    sys.stderr.write(_BANNER.format(version=__version__))
    sys.stderr.flush()


def _print_dry_run(cell: Cell, session_id: str, was_generated: bool, argv: list[str]) -> None:
    print(f"# swarph spawn dry-run", file=sys.stderr)
    print(f"#   cell:        {cell.source_path}", file=sys.stderr)
    print(f"#   schema:      {cell.schema_version}", file=sys.stderr)
    print(f"#   name:        {cell.name}", file=sys.stderr)
    print(f"#   role:        {cell.role}", file=sys.stderr)
    print(f"#   cwd:         {cell.cwd}", file=sys.stderr)
    print(
        f"#   session_id:  {session_id} "
        f"({'minted+persisted' if was_generated else 'reused'})",
        file=sys.stderr,
    )
    print(
        f"#   starter:     "
        f"{cell.starter_prompt_path or '(none)'}",
        file=sys.stderr,
    )
    print(f"#   provider:    {cell.provider}", file=sys.stderr)
    if cell.lineage is not None:
        print(
            f"#   lineage:     parent_peer_id="
            f"{cell.lineage.parent_peer_id!r} "
            f"signature={cell.lineage.spawn_manifest_signature!r}",
            file=sys.stderr,
        )
    # Redact the (potentially long) starter prompt from the printed
    # command so the dry-run output stays scannable.
    redacted = []
    skip_next = False
    for tok in argv:
        if skip_next:
            redacted.append(f"<{len(tok)}-char starter prompt>")
            skip_next = False
            continue
        if tok == "--append-system-prompt":
            skip_next = True
        redacted.append(tok)
    print(" ".join(redacted))


def run_spawn(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph spawn"

    own_argv, passthrough = _split_passthrough(list(argv))

    parser = _build_parser()
    try:
        args = parser.parse_args(own_argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if (
        args.role_or_path is None
        and args.onboarding is None
        and args.cell is None
        and discover_cell_in_cwd() is None
    ):
        print(_USAGE, file=sys.stderr)
        return 0

    if not args.no_banner and not args.dry_run:
        _print_banner()

    try:
        cell = _resolve_cell(args)
    except (CellError, NotImplementedError) as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    try:
        session_id, was_generated = load_or_create_session_id(cell.role, cell)
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    try:
        claude_argv = _build_claude_argv(
            cell, session_id, args.no_starter, passthrough
        )
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    if args.print_id:
        print(session_id)

    if args.dry_run:
        _print_dry_run(cell, session_id, was_generated, claude_argv)
        return 0

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        print(
            "swarph spawn: 'claude' binary not found on PATH. "
            "Install Claude Code (https://docs.anthropic.com/claude/claude-code) "
            "or set PATH explicitly.",
            file=sys.stderr,
        )
        return 127

    try:
        os.chdir(cell.cwd)
    except OSError as exc:
        print(f"swarph spawn: cannot chdir to {cell.cwd}: {exc}", file=sys.stderr)
        return 1

    # exec-replace so the spawned claude session owns stdio +
    # signals cleanly. argv[0] is preserved as 'claude' for ps-grep.
    try:
        os.execv(claude_bin, claude_argv)
    except OSError as exc:
        # execv only returns on failure.
        print(f"swarph spawn: exec failed: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable, keeps type checker happy
