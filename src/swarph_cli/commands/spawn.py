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
from pathlib import Path
from typing import Optional

from swarph_cli import __version__
from swarph_cli.cell import (
    Cell,
    CellError,
    discover_cell_in_cwd,
    is_mesh_gateway_url,
    load_cell,
    load_or_create_session_id,
    read_starter_prompt,
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
        "--new-instance",
        action="store_true",
        help="Mint a fresh UUID for this spawn AND auto-allocate the "
        "next free <role>-N slot for sibling persistence (v0.7 PR-B). "
        "Use for sibling-spawn (e.g., a second alpha+beta drop-on-meta-edge "
        "instance on the same host). The base sidecar is NOT touched, so "
        "re-resume of the original session still works without this flag. "
        "The sibling persists at <role>-N.session-id and is resumable via "
        "`swarph spawn <role>-N` later. claude --name uses <role>-N for "
        "/resume picker disambiguation. Auto-suffix policy via "
        "next_free_slot_role() (slots 2-99). Per beta #892 B2 + B1.",
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


def _resolve_cell(args: argparse.Namespace) -> tuple[Cell, Optional[str]]:
    """Resolve cell.yaml + return (cell, requested_role).

    ``requested_role`` is the bare role name the user typed (or None
    if the user gave a literal path / used --onboarding / relied on
    cwd auto-discovery). Lets v0.7 PR-B distinguish ``swarph spawn
    lab-test`` from ``swarph spawn lab-test-2`` even when both
    resolve to the same cell.yaml — the former operates on slot 1,
    the latter on slot 2.
    """
    requested_role: Optional[str] = None
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
        # Track the role iff the user gave a bare role string (not a
        # literal path or `.` cwd token) — that's what slot-role
        # disambiguation cares about.
        if (
            args.role_or_path != "."
            and not args.role_or_path.endswith((".yaml", ".yml"))
            and "/" not in args.role_or_path
        ):
            requested_role = args.role_or_path
    else:
        discovered = discover_cell_in_cwd()
        if discovered is None:
            raise CellError(
                "swarph spawn: provide a role name, cell.yaml path, "
                "--onboarding PATH, or run from a directory containing "
                "./cell.yaml"
            )
        path = discovered
    return load_cell(path), requested_role


def _session_state_exists(session_id: str) -> bool:
    """True if Claude Code already has on-disk session state for this UUID.

    Closes v0.7.4 spawn-bug surfaced 2026-05-14 post-reboot (DM #1255):
    `claude --session-id <UUID>` rejects with "Session ID <UUID> is already
    in use" when session-state files exist on disk, even after reboot
    (files persist; the in-use check is filesystem-based not runtime-lock-
    based). Switching to `claude --resume <UUID>` is the correct semantic
    when the UUID's state already exists.

    Probes the three filesystem locations Claude Code stores per-session
    state in: ~/.claude/file-history/<UUID>, ~/.claude/session-env/<UUID>,
    and ~/.claude/projects/<project-hash>/<UUID>.jsonl (the latter
    discovered via glob since project-hash varies).
    """
    claude_dir = Path.home() / ".claude"
    if (claude_dir / "file-history" / session_id).exists():
        return True
    if (claude_dir / "session-env" / session_id).exists():
        return True
    projects_dir = claude_dir / "projects"
    if projects_dir.exists():
        for _ in projects_dir.glob(f"*/{session_id}.jsonl"):
            return True
    return False


def _build_claude_argv(
    cell: Cell,
    session_id: str,
    no_starter: bool,
    passthrough: list[str],
    effective_role: Optional[str] = None,
) -> list[str]:
    name_value = effective_role if effective_role is not None else cell.role
    # v0.7.5: auto-detect existing session state and switch from --session-id
    # (create-new-with-pinned-UUID semantic) to --resume (attach-to-existing
    # semantic). Both pass the same UUID; the verb determines whether claude
    # treats it as fresh-create vs resume-existing.
    if _session_state_exists(session_id):
        argv: list[str] = ["claude", "--name", name_value, "--resume", session_id]
    else:
        argv = ["claude", "--name", name_value, "--session-id", session_id]

    if not no_starter:
        starter = read_starter_prompt(cell)
        if starter:
            argv.extend(["--append-system-prompt", starter])

    argv.extend(passthrough)
    return argv


def _print_banner() -> None:
    sys.stderr.write(_BANNER.format(version=__version__))
    sys.stderr.flush()


def _print_dry_run(
    cell: Cell,
    session_id: str,
    was_generated: bool,
    argv: list[str],
    new_instance: bool = False,
    effective_role: Optional[str] = None,
) -> None:
    is_sibling = (
        new_instance
        and effective_role is not None
        and effective_role != cell.role
    )
    if is_sibling:
        sid_label = f"minted (sibling slot {effective_role!r}, persisted)"
    elif was_generated:
        sid_label = "minted+persisted"
    else:
        sid_label = "reused"
    print(f"# swarph spawn dry-run", file=sys.stderr)
    print(f"#   cell:        {cell.source_path}", file=sys.stderr)
    print(f"#   schema:      {cell.schema_version}", file=sys.stderr)
    print(f"#   name:        {cell.name}", file=sys.stderr)
    print(f"#   role:        {cell.role}", file=sys.stderr)
    print(f"#   cwd:         {cell.cwd}", file=sys.stderr)
    print(
        f"#   session_id:  {session_id} ({sid_label})",
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
        cell, requested_role = _resolve_cell(args)
    except (CellError, NotImplementedError) as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    # When user typed a slot-role (e.g. `swarph spawn drop-on-meta-edge-2`)
    # the cell.yaml resolved to the BASE file (drop-on-meta-edge.yaml) so
    # cell.role = "drop-on-meta-edge". But the operator wants slot 2's
    # sidecar + display name. Use the user's typed role for the sidecar
    # lookup; cell.role stays the base role for cell-context (cwd, starter
    # prompt, lineage, provider).
    sidecar_role = requested_role if requested_role else cell.role

    try:
        session_id, was_generated, effective_role = load_or_create_session_id(
            sidecar_role, cell, new_instance=args.new_instance
        )
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    if args.new_instance and cell.session_id:
        # Pinned cell.yaml session_id wins over --new-instance; surface
        # the conflict on stderr so the operator knows the flag was a
        # no-op for this cell.
        print(
            "swarph spawn: --new-instance ignored — cell.yaml pins "
            "session_id explicitly. Remove the pinned UUID from cell.yaml "
            "OR pass --session-id <new-uuid> on the claude command line "
            "via `-- --session-id <uuid>` passthrough to override.",
            file=sys.stderr,
        )

    if args.new_instance and effective_role == sidecar_role and not cell.session_id:
        # Degenerate case — --new-instance fired but no base sidecar
        # existed, so we minted into the BASE slot (treating as the
        # original). Surface this as a stderr note so the operator
        # understands the v0.7 PR-B auto-suffix didn't kick in.
        print(
            "swarph spawn: --new-instance fired on a role with no "
            f"existing sidecar — minted as the FIRST instance of "
            f"{cell.role!r} (base slot), not as a sibling. Spawn the "
            "original first via `swarph spawn <role>` (no --new-instance), "
            "then re-run with --new-instance to mint a true sibling.",
            file=sys.stderr,
        )

    try:
        claude_argv = _build_claude_argv(
            cell, session_id, args.no_starter, passthrough,
            effective_role=effective_role,
        )
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    if args.print_id:
        print(session_id)

    if args.dry_run:
        _print_dry_run(
            cell, session_id, was_generated, claude_argv,
            new_instance=args.new_instance,
            effective_role=effective_role,
        )
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

    # Windows-platform known-issues banner. Claude Code's TUI (Ink-based)
    # has documented input/rendering bugs on Windows native consoles
    # (conhost.exe in particular). Specific symptom commander hit
    # 2026-05-17 on workstation-lc: pressing Enter inserts literal 'm'
    # character instead of submitting. See docs/WINDOWS_KNOWN_ISSUES.md
    # for the full hypothesis chain + workarounds (Windows Terminal vs
    # conhost, WSL2 fallback, TERM env injection).
    #
    # Banner is suppressed by --no-banner OR when the operator has
    # already acknowledged via SWARPH_WIN_ACK=1 in env (set once after
    # reading the doc).
    if (
        sys.platform == "win32"
        and not args.no_banner
        and not os.environ.get("SWARPH_WIN_ACK")
    ):
        print(
            "swarph spawn: WARNING — Windows shell detected. Claude Code's "
            "TUI has documented input/rendering issues on Windows native "
            "consoles (conhost.exe). Known symptom: Enter inserts literal "
            "'m' character. See docs/WINDOWS_KNOWN_ISSUES.md for "
            "workarounds (use Windows Terminal not conhost, or WSL2). "
            "Set SWARPH_WIN_ACK=1 in env to suppress this warning.",
            file=sys.stderr,
        )

    try:
        os.chdir(cell.cwd)
    except OSError as exc:
        print(f"swarph spawn: cannot chdir to {cell.cwd}: {exc}", file=sys.stderr)
        return 1

    # v0.7 PR-C — set SWARPH_SPAWN=1 env so a SessionStart hook
    # installed via `swarph install-hook` knows the prompt was
    # already injected via --append-system-prompt and skips
    # double-injection. The env propagates through execv since we
    # don't use execve with a custom env.
    os.environ["SWARPH_SPAWN"] = "1"

    # exec-replace so the spawned claude session owns stdio +
    # signals cleanly. argv[0] is preserved as 'claude' for ps-grep.
    try:
        os.execv(claude_bin, claude_argv)
    except OSError as exc:
        # execv only returns on failure.
        print(f"swarph spawn: exec failed: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable, keeps type checker happy
