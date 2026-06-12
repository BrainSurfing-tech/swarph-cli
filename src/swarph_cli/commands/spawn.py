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
import subprocess
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
from swarph_shared.subprocess_env import scrub_env_for_subprocess


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
               [-- provider-extra-args...]

Resolution (first match wins):
  --onboarding <path-or-url>      explicit override
  <role>                          ~/.config/swarph/cells/<role>.yaml
  <path>.yaml                     literal path
  ./cell.yaml                     auto-discovered if no positional given
  mesh-gateway://...              v0.7+ — returns NotImplementedError now

Flags:
  --dry-run        Print the resolved provider command + cell summary; no exec
  --no-starter     Skip starter-prompt injection even if cell.yaml sets one
  --print-id       Print resolved session-id to stdout before exec (useful
                   for shell scripts capturing the UUID for later resume)

Anything after a literal `--` is passed through to the provider CLI unchanged
(e.g. `swarph spawn lab -- --resume` to force the Claude resume picker).
"""


# Codex-specific org-scoping keys NOT covered by the shared billing denylist
# (scrub_env_for_subprocess already strips OPENAI_API_KEY / OPENAI_API_BASE /
# OPENAI_BASE_URL via its explicit set + *_BASE_URL/*_API_KEY suffix sweep, and
# CODEX_API_KEY via the *_API_KEY suffix). These two route billing to a specific
# org rather than redirect the endpoint, so they live as a codex-layer extra on
# top of the canonical scrub.
_CODEX_EXTRA_LEAK_KEYS = (
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
)

_CODEX_SANDBOX_VALUES = frozenset({"workspace-write", "read-only"})
_CODEX_DEFAULT_SANDBOX = "workspace-write"
_CODEX_APPROVAL = "on-request"
_CODEX_PRINT_ID_NOTE = "codex: fresh-session-per-spawn, no pinned id"


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


def _validate_routing(cell: Cell) -> None:
    """Validate optional ``cell.extra.routing`` against the spawn provider."""
    extra = cell.extra or {}
    routing = extra.get("routing")
    if routing is None:
        return
    if not isinstance(routing, dict):
        raise CellError(
            f"swarph spawn: cell.yaml `routing` must be a mapping, "
            f"got {type(routing).__name__}. See "
            f"research/swarph_cli/CELL_MEMBRANE_PHASE_0_RFC.md for the "
            f"valid v0 schema."
        )

    provider_native = {
        "claude": "anthropic",
        "codex": "codex",
        "antigravity": "antigravity",
    }.get(cell.provider)
    if provider_native is None:
        raise CellError(
            f"swarph spawn: provider {cell.provider!r} is not supported "
            "by this spawn membrane."
        )

    native = routing.get("native", provider_native)
    
    if cell.provider == "antigravity":
        if native in ("antigravity", "gemini"):
            return
        expected = "'antigravity' or 'gemini'"
    else:
        if native == provider_native:
            return
        expected = repr(provider_native)

    raise CellError(
        f"swarph spawn: cell.yaml `routing.native: {native!r}` does not "
        f"match provider {cell.provider!r}. Expected routing.native "
        f"{expected}, or omit the routing field."
    )


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


def _base_pin_uuid(role: str) -> Optional[str]:
    """Read the base role's pinned UUID (the mitosis parent_session_id)."""
    from swarph_cli.cell import _read_session_sidecar, session_state_path
    uuid_str, _cwd = _read_session_sidecar(session_state_path(role))
    return uuid_str


def _record_mitosis_safe(
    cell: Cell,
    *,
    sidecar_role: str,
    effective_role: Optional[str],
    session_id: Optional[str],
    was_generated: bool,
) -> None:
    """Append a mitosis lineage record for a freshly-minted sibling.

    Spec §6: a true sibling is `was_generated and effective_role != sidecar_role`
    (base slot reuse has effective_role == sidecar_role). NEVER raises — capture
    partial-fail must not block the claude exec (spec §7); log + degrade.
    """
    if not (was_generated and effective_role and effective_role != sidecar_role):
        return
    try:
        from swarph_cli.capture import lineage
        cursor_path = cell.extra.get("cursor_path") if cell.extra else None
        lineage.record_mitosis(
            cell,
            child_role=effective_role,
            parent_role=sidecar_role,
            child_session_id=session_id,
            parent_session_id=_base_pin_uuid(sidecar_role),
            cursor_path=cursor_path,
        )
    except Exception as exc:  # never block the exec
        print(f"swarph spawn: mitosis lineage record failed (non-fatal): {exc}",
              file=sys.stderr)


def _current_tmux_session() -> Optional[str]:
    """Name of the tmux session this process runs inside, or None.

    $TMUX presence means we're in a pane; the session name comes from
    `tmux display-message`. Console (non-tmux) spawns return None.
    """
    if not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    name = out.stdout.strip()
    return name or None


def _set_live_pin_safe(role: str) -> None:
    """Record this tmux session as the live holder of `role`'s pinned UUID.

    Feeds `swarph cell verify`'s double-resume probe (spec §4.4b). Only fires
    when spawning INSIDE tmux (the claude-tmux@.service path) — console
    spawns are untracked, per the one-launch-path discipline. No-ops if the
    cell was never hardened (no manifest). NEVER raises into the exec path.
    """
    try:
        holder = _current_tmux_session()
        if not holder:
            return
        from swarph_cli.capture import manifest
        manifest.set_live_pin(role, holder)
    except Exception as exc:  # never block the exec
        print(f"swarph spawn: live-pin record failed (non-fatal): {exc}",
              file=sys.stderr)


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


def _codex_sandbox(cell: Cell) -> str:
    sandbox = getattr(cell, "sandbox", None) or _CODEX_DEFAULT_SANDBOX
    if sandbox not in _CODEX_SANDBOX_VALUES:
        raise CellError(
            f"cell.yaml: sandbox {sandbox!r} is not valid for provider "
            f"'codex'. Valid values: {sorted(_CODEX_SANDBOX_VALUES)}."
        )
    return sandbox


def _claude_env() -> dict[str, str]:
    """Subscription-billing env for an interactive ``claude`` session.

    The canonical billing-redirect scrub plus the SWARPH_SPAWN marker. Without
    this an ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` set in the parent
    env (an identity proxy / metered relay) would be inherited by the spawned
    ``claude`` and silently flip it off subscription auth to a metered endpoint
    while still reporting ``cost_usd`` 0.0 — the adversarial-sweep CRIT.
    """
    env = scrub_env_for_subprocess()
    env["SWARPH_SPAWN"] = "1"
    return env


def _agy_env() -> dict[str, str]:
    """Subscription-billing env for an ``agy`` (antigravity/Gemini) session.

    Delegates to the shared scrub, which strips the full billing-redirect class
    (GEMINI_API_KEY / GOOGLE_API_KEY / GEMINI_BASE_URL / GOOGLE_APPLICATION_
    CREDENTIALS / GOOGLE_CLOUD_PROJECT / VERTEX_*) — a superset of the four GCP
    keys this previously popped by hand.
    """
    env = scrub_env_for_subprocess()
    env["SWARPH_SPAWN"] = "1"
    return env


def _build_agy_argv(
    cell: Cell, no_starter: bool, passthrough: list[str]
) -> list[str]:
    argv = ["agy"]
    
    # codex is adding cell.sandbox; default ON, only off on explicit falsy
    sandbox_attr = getattr(cell, "sandbox", None)
    if sandbox_attr is not None:
        is_sandbox = sandbox_attr
    else:
        is_sandbox = cell.extra.get("sandbox", True)
        
    if is_sandbox is not False:
        argv.append("--sandbox")
    
    # Pass --add-dir <cwd> for directory setup.
    argv.extend(["--add-dir", str(cell.cwd)])
    
    if not no_starter and cell.starter_prompt_path:
        argv.extend(["--prompt-interactive", read_starter_prompt(cell)])
    
    argv.extend(passthrough)
    return argv


def _build_codex_argv(cell: Cell, passthrough: list[str]) -> list[str]:
    argv = [
        "codex",
        "-C",
        str(cell.cwd),
        "-s",
        _codex_sandbox(cell),
        "-a",
        _CODEX_APPROVAL,
    ]
    argv.extend(passthrough)
    return argv


def _scrubbed_codex_env() -> dict[str, str]:
    """Subscription-billing env for a ``codex`` (GPT) session.

    The shared billing-redirect scrub plus the codex-specific org-scoping keys
    (see ``_CODEX_EXTRA_LEAK_KEYS``) that the shared denylist does not cover.
    """
    env = scrub_env_for_subprocess()
    for key in _CODEX_EXTRA_LEAK_KEYS:
        env.pop(key, None)
    env["SWARPH_SPAWN"] = "1"
    return env


def _print_banner() -> None:
    sys.stderr.write(_BANNER.format(version=__version__))
    sys.stderr.flush()


def _print_dry_run(
    cell: Cell,
    session_id: Optional[str],
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
    if cell.provider == "codex":
        print(
            "#   session_id:  codex: fresh-session-per-spawn, no pinned id "
            "(cell.yaml session_id ignored)",
            file=sys.stderr,
        )
    else:
        print(
            f"#   session_id:  {session_id} ({sid_label})",
            file=sys.stderr,
        )
    if cell.provider == "codex":
        print(
            "#   starter:     cwd AGENTS.md auto-read by codex; no "
            "--append-system-prompt injection",
            file=sys.stderr,
        )
    else:
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


def _console_is_genuine_wt() -> bool:
    """True ONLY if the controlling terminal is positively a real Windows Terminal.

    Confirms a genuine Windows Terminal by walking the parent-process chain (via the
    Win32 toolhelp snapshot API) and checking whether any ancestor process executable
    is ``WindowsTerminal.exe`` (case-insensitive). This is GROUND TRUTH, unlike the
    ``WT_SESSION`` environment variable — which is inherited into child ``conhost``
    consoles on corporate setups, and is set whenever a shell is launched from WT at
    all, so it cannot distinguish "this console is a real WT" from "an ancestor once
    was, but I'm now in a broken conhost".

    Returns False on any non-win32 platform (the whole relaunch is win32-gated). The
    ENTIRE Win32 body is wrapped in ``try/except Exception: return False`` so any
    error — missing API, weird process state, access denied — fails SAFE toward
    "not confirmed", which makes the caller RELAUNCH (the foolproof direction).

    This helper is the ONLY piece that touches ctypes/Win32 and cannot be exercised
    on a non-Windows box; the decision logic around it is fully unit-tested via mocks.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        MAX_PATH = 260

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH),
            ]

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return False
        try:
            # Build {pid: (ppid, exe_name_lower)} for every live process.
            procs: dict[int, tuple[int, str]] = {}
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while ok:
                try:
                    exe = entry.szExeFile.decode("utf-8", "replace").lower()
                except Exception:
                    exe = ""
                procs[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    exe,
                )
                ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

        # Walk from our own pid up through ppids; bounded + cycle-guarded.
        pid = os.getpid()
        seen: set[int] = set()
        for _ in range(12):
            if pid in seen or pid == 0:
                break
            seen.add(pid)
            info = procs.get(pid)
            if info is None:
                break
            ppid, _exe = info
            parent = procs.get(ppid)
            if parent is not None and parent[1] == "windowsterminal.exe":
                return True
            pid = ppid
        return False
    except Exception:
        return False


def _relaunch_in_windows_terminal(
    claude_bin: str, claude_argv: list[str], cwd: Path,
) -> bool:
    """Auto-fix the conhost TUI bug by relaunching the session in Windows Terminal.

    On legacy Windows console (``conhost.exe``), Claude Code's Ink TUI breaks: the
    SGR terminator ``m`` leaks from the output stream into stdin, so pressing Enter
    inserts a literal ``m`` instead of submitting (see docs/WINDOWS_KNOWN_ISSUES.md).
    Windows Terminal handles VT-input correctly. The DEFAULT is to relaunch (rescue):
    unless we can POSITIVELY confirm we're already in a genuine Windows Terminal, we
    pop a fresh WT window and return True (the caller should exit this console).

    Genuine-WT detection is via process ancestry (``_console_is_genuine_wt``), NOT the
    inheritable ``WT_SESSION`` env var — which wrongly looked like "already in good WT"
    on corporate conhosts and on any shell launched from WT, leaving users stuck on a
    broken console with no new window (live repro 2026-06-03 on workstation-lc).

    Two env overrides:
      * ``SWARPH_FORCE_WT=1`` — ALWAYS relaunch, even from a genuine Windows Terminal;
      * ``SWARPH_WIN_ACK=1``  — NEVER relaunch (explicit "run here anyway" opt-out).

    No-op (returns False) when:
      * not Windows;
      * stdout is not an interactive TTY (CI / piped / redirected) — there is no
        human console to relaunch from, and a detached WT window would be wrong;
      * we are already inside a session WE spawned (``SWARPH_SPAWN`` set) — the
        reliable loop-guard: a relaunched session can never re-relaunch;
      * operator opted to stay put (``SWARPH_WIN_ACK=1``);
      * we positively confirm a genuine Windows Terminal via ancestry AND not
        force-requested — the TUI works there, no redundant window;
      * ``wt.exe`` is not installed (e.g. locked-down corporate box) — caller warns.
    """
    if sys.platform != "win32":
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("SWARPH_SPAWN"):
        return False
    if os.environ.get("SWARPH_WIN_ACK"):
        return False
    # Skip the relaunch ONLY when we can positively confirm we're already in a
    # genuine Windows Terminal (TUI works there) — verified by process ancestry,
    # NOT the inheritable WT_SESSION env var. Default is to relaunch (rescue):
    # if we can't confirm a good WT, we pop a fresh one. SWARPH_FORCE_WT forces
    # relaunch even from a genuine WT.
    if not os.environ.get("SWARPH_FORCE_WT") and _console_is_genuine_wt():
        return False
    wt = shutil.which("wt")
    if not wt:
        return False
    # Relaunch claude inside Windows Terminal, in the cell's cwd, carrying
    # SWARPH_SPAWN=1 so a SessionStart hook doesn't double-inject the starter.
    # claude_argv[0] is the "claude" argv0; the real flags are claude_argv[1:].
    wt_cmd = [wt, "-d", str(cwd), "--", claude_bin, *claude_argv[1:]]
    env = {**os.environ, "SWARPH_SPAWN": "1"}
    try:
        subprocess.Popen(wt_cmd, env=env)
    except OSError as exc:
        print(
            f"swarph spawn: Windows Terminal relaunch failed ({exc}); continuing "
            f"in this console (TUI may misbehave — see docs/WINDOWS_KNOWN_ISSUES.md).",
            file=sys.stderr,
        )
        return False
    print(
        "swarph spawn: relaunched the session in Windows Terminal (avoids the "
        "conhost Enter-inserts-'m' TUI bug). This console can be closed.",
        file=sys.stderr,
    )
    return True


class ProviderMembrane:
    """Per-provider divergence boundary for ``swarph spawn``.

    Each concrete membrane encapsulates exactly how one provider differs:
    argv construction, binary resolution (+ not-found message), whether a
    pinned session UUID is used, any pre-launch step (e.g. the claude-only
    Windows Terminal relaunch + conhost warning), and the final
    chdir/env/exec sequence.

    Membranes are thin: they DELEGATE to the existing module-level helper
    functions rather than reimplementing them, so this refactor is purely a
    re-organization of the dispatch that previously lived inline in
    ``run_spawn`` as ``if cell.provider == ...`` blocks.
    """

    name: str = ""

    def uses_pinned_session(self) -> bool:
        """True if ``run_spawn`` should mint/resume a pinned UUID.

        Claude pins a UUID via ``load_or_create_session_id``; codex and
        antigravity are fresh-session-per-spawn (``session_id = None`` /
        a human-readable placeholder).
        """
        return False

    def build_argv(
        self,
        cell: Cell,
        *,
        session_id: Optional[str],
        no_starter: bool,
        passthrough: list[str],
        effective_role: Optional[str],
    ) -> list[str]:
        raise NotImplementedError

    def resolve_binary(self) -> Optional[str]:
        """Return the provider binary path, or None if not found."""
        raise NotImplementedError

    def binary_not_found_message(self) -> str:
        raise NotImplementedError

    def pre_launch(
        self, cell: Cell, binary: str, argv: list[str], *, no_banner: bool
    ) -> Optional[int]:
        """Hook run after binary resolution, before launch.

        Return an int exit code to short-circuit ``run_spawn`` (claude uses
        this for the Windows Terminal relaunch). Return None to proceed.
        """
        return None

    def launch(self, cell: Cell, binary: str, argv: list[str]) -> int:
        """chdir + env setup + exec-replace. Only returns on exec failure."""
        raise NotImplementedError


class ClaudeMembrane(ProviderMembrane):
    name = "claude"

    def uses_pinned_session(self) -> bool:
        return True

    def build_argv(
        self,
        cell: Cell,
        *,
        session_id: Optional[str],
        no_starter: bool,
        passthrough: list[str],
        effective_role: Optional[str],
    ) -> list[str]:
        assert session_id is not None  # claude always pins a UUID
        return _build_claude_argv(
            cell, session_id, no_starter, passthrough,
            effective_role=effective_role,
        )

    def resolve_binary(self) -> Optional[str]:
        return shutil.which("claude")

    def binary_not_found_message(self) -> str:
        return (
            "swarph spawn: 'claude' binary not found on PATH. "
            "Install Claude Code (https://docs.anthropic.com/claude/claude-code) "
            "or set PATH explicitly."
        )

    def pre_launch(
        self, cell: Cell, binary: str, argv: list[str], *, no_banner: bool
    ) -> Optional[int]:
        # conhost TUI auto-fix (CLAUDE only — codex/agy don't use the claude
        # TUI): on legacy Windows console (not Windows Terminal), relaunch the
        # claude session in Windows Terminal where the Ink TUI works. Returns 0
        # (and we exit this console) only when it actually relaunched.
        if _relaunch_in_windows_terminal(binary, argv, cell.cwd):
            return 0

        # Still in a broken console (conhost with no wt.exe, or operator acked).
        # Warn unless suppressed. Inside a genuine Windows Terminal (confirmed by
        # ancestry, NOT the inheritable WT_SESSION) the TUI works, so no warning
        # fires there.
        if (
            sys.platform == "win32"
            and not no_banner
            and not os.environ.get("SWARPH_WIN_ACK")
            and not _console_is_genuine_wt()
        ):
            print(
                "swarph spawn: WARNING — legacy Windows console (conhost) and Windows "
                "Terminal (wt.exe) was not found, so the session couldn't be "
                "auto-relaunched. Claude Code's TUI mis-handles input here (Enter "
                "inserts literal 'm'). Install Windows Terminal (Microsoft Store) and "
                "re-run, or use WSL2. See docs/WINDOWS_KNOWN_ISSUES.md. Set "
                "SWARPH_WIN_ACK=1 to suppress and run here anyway.",
                file=sys.stderr,
            )
        return None

    def launch(self, cell: Cell, binary: str, argv: list[str]) -> int:
        try:
            os.chdir(cell.cwd)
        except OSError as exc:
            print(f"swarph spawn: cannot chdir to {cell.cwd}: {exc}", file=sys.stderr)
            return 1

        # Exec with the billing-redirect-scrubbed env (NOT raw inherited env) so
        # a parent-set ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN can't silently
        # flip the spawned claude off subscription billing. SWARPH_SPAWN=1 (set
        # in _claude_env) tells a `swarph install-hook` SessionStart hook the
        # prompt was already injected via --append-system-prompt, so it skips
        # double-injection. execve carries exactly this env to the child.
        try:
            os.execve(binary, argv, _claude_env())
        except OSError as exc:
            print(f"swarph spawn: exec failed: {exc}", file=sys.stderr)
            return 1
        return 0  # unreachable, keeps type checker happy


class CodexMembrane(ProviderMembrane):
    name = "codex"

    def build_argv(
        self,
        cell: Cell,
        *,
        session_id: Optional[str],
        no_starter: bool,
        passthrough: list[str],
        effective_role: Optional[str],
    ) -> list[str]:
        return _build_codex_argv(cell, passthrough)

    def resolve_binary(self) -> Optional[str]:
        return shutil.which("codex")

    def binary_not_found_message(self) -> str:
        return (
            "swarph spawn: 'codex' binary not found on PATH. "
            "Install Codex CLI or set PATH explicitly."
        )

    def launch(self, cell: Cell, binary: str, argv: list[str]) -> int:
        try:
            os.execve(binary, argv, _scrubbed_codex_env())
        except OSError as exc:
            print(f"swarph spawn: exec failed: {exc}", file=sys.stderr)
            return 1
        return 0  # unreachable, keeps type checker happy


class AntigravityMembrane(ProviderMembrane):
    name = "antigravity"

    def build_argv(
        self,
        cell: Cell,
        *,
        session_id: Optional[str],
        no_starter: bool,
        passthrough: list[str],
        effective_role: Optional[str],
    ) -> list[str]:
        return _build_agy_argv(cell, no_starter, passthrough)

    def resolve_binary(self) -> Optional[str]:
        provider_bin = shutil.which("agy")
        if provider_bin is None:
            home_local = Path.home() / ".local" / "bin" / "agy"
            if home_local.exists():
                provider_bin = str(home_local)
        return provider_bin

    def binary_not_found_message(self) -> str:
        return (
            "swarph spawn: 'agy' binary not found on PATH. "
            "Install Antigravity CLI or set PATH explicitly."
        )

    def launch(self, cell: Cell, binary: str, argv: list[str]) -> int:
        # execve carries exactly the scrubbed env to the child without mutating
        # this process's os.environ first (so a failed exec leaves us intact).
        try:
            os.execve(binary, argv, _agy_env())
        except OSError as exc:
            print(f"swarph spawn: exec failed: {exc}", file=sys.stderr)
            return 1
        return 0  # unreachable, keeps type checker happy


MEMBRANES: dict[str, ProviderMembrane] = {
    "claude": ClaudeMembrane(),
    "codex": CodexMembrane(),
    "antigravity": AntigravityMembrane(),
}

# Defensive coupling: MEMBRANES must stay in lockstep with the shared provider
# whitelist. A future VALID_PROVIDERS entry without a matching membrane would
# otherwise surface as a raw KeyError at spawn time — fail loud at import instead.
from swarph_shared.cell import VALID_PROVIDERS as _VALID_PROVIDERS  # noqa: E402

if set(MEMBRANES) != _VALID_PROVIDERS:
    raise RuntimeError(
        f"MEMBRANES {sorted(MEMBRANES)} out of sync with VALID_PROVIDERS "
        f"{sorted(_VALID_PROVIDERS)} — add the missing provider membrane."
    )


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

    # Phase 1B v0 (2026-05-19): validate cell.yaml routing field.
    # In v0 only `routing.native: anthropic` (or absent) is accepted.
    # Future non-Anthropic dispatch is Phase 1B v1+ scope.
    try:
        _validate_routing(cell)
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    membrane = MEMBRANES.get(cell.provider)
    if membrane is None:
        # Defense-in-depth: VALID_PROVIDERS (in load_cell) normally rejects
        # unknown providers first, and _validate_routing covers the routing-field
        # path — but a routing-less cell of an unmembraned provider would have hit
        # a raw KeyError here. Fail clean instead (silent-failure-hunter #4).
        print(
            f"swarph spawn: provider {cell.provider!r} is not supported by this "
            f"spawn membrane (have: {', '.join(sorted(MEMBRANES))}).",
            file=sys.stderr,
        )
        return 1

    session_id: Optional[str]
    was_generated = False
    effective_role: Optional[str] = None

    if membrane.uses_pinned_session():
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

        _record_mitosis_safe(
            cell,
            sidecar_role=sidecar_role,
            effective_role=effective_role,
            session_id=session_id,
            was_generated=was_generated,
        )
    elif cell.provider == "antigravity":
        session_id = "(fresh-session-per-spawn, no pinned id)"
        was_generated = True
        sidecar_role = requested_role if requested_role else cell.role
        effective_role = sidecar_role
    else:  # codex
        session_id = None

    try:
        spawn_argv = membrane.build_argv(
            cell,
            session_id=session_id,
            no_starter=args.no_starter,
            passthrough=passthrough,
            effective_role=effective_role,
        )
    except CellError as exc:
        print(f"swarph spawn: {exc}", file=sys.stderr)
        return 1

    if args.print_id:
        if cell.provider == "codex":
            print(_CODEX_PRINT_ID_NOTE)
        else:
            print(session_id)

    if args.dry_run:
        _print_dry_run(
            cell, session_id, was_generated, spawn_argv,
            new_instance=args.new_instance,
            effective_role=effective_role,
        )
        return 0

    provider_bin = membrane.resolve_binary()
    if provider_bin is None:
        print(membrane.binary_not_found_message(), file=sys.stderr)
        return 127

    # Windows-platform known-issues handling. Claude Code's TUI (Ink-based)
    # has documented input/rendering bugs on Windows native consoles
    # (conhost.exe in particular). Specific symptom commander hit
    # 2026-05-17 on workstation-lc: pressing Enter inserts literal 'm'
    # character instead of submitting. See docs/WINDOWS_KNOWN_ISSUES.md
    # for the full hypothesis chain + workarounds (Windows Terminal vs
    # conhost, WSL2 fallback, TERM env injection).
    #
    # The claude membrane's pre_launch handles the conhost auto-fix
    # (relaunch in Windows Terminal) + the fallback warning; codex/agy
    # no-op. A non-None return short-circuits run_spawn (claude returns 0
    # when it relaunched and this console should exit).
    pre = membrane.pre_launch(
        cell, provider_bin, spawn_argv, no_banner=args.no_banner
    )
    if pre is not None:
        return pre

    if cell.assisted_memory and cell.assisted_memory.get("enabled"):
        try:
            from swarph_cli.commands.memory_sync import perform_restore
            current_task_text = perform_restore(cell)
            if current_task_text:
                lines = current_task_text.splitlines()
                first_line = lines[0] if lines else "(empty)"
                print(f"swarph spawn: restored current-task: {first_line}", file=sys.stderr)
                
                inject_text = f"Your active task is in CURRENT_TASK.md — read it first:\n\n{current_task_text}"
                if cell.provider == "claude":
                    spawn_argv.extend(["--append-system-prompt", inject_text])
                elif cell.provider == "antigravity":
                    spawn_argv.extend(["--prompt-interactive", inject_text])
                elif cell.provider == "codex":
                    agents_md = cell.cwd / "AGENTS.md"
                    if agents_md.exists():
                        content = agents_md.read_text(encoding="utf-8")
                        if "CURRENT_TASK.md" not in content:
                            agents_md.write_text(inject_text + "\n\n" + content, encoding="utf-8")
                    else:
                        agents_md.write_text(inject_text, encoding="utf-8")
        except Exception as exc:
            print(f"swarph spawn: restore failed: {exc}", file=sys.stderr)

    # Record this tmux session as the pin's live holder (feeds the verify
    # gate's double-resume probe). After dry-run/print-id so a dry-run never
    # mutates state; before exec so the flag exists while claude is live.
    if membrane.uses_pinned_session():
        _set_live_pin_safe(effective_role if effective_role else cell.role)

    # exec-replace so the spawned provider session owns stdio +
    # signals cleanly. argv[0] is preserved for ps-grep. launch()
    # encapsulates the per-provider chdir + env setup + exec and only
    # returns on failure.
    return membrane.launch(cell, provider_bin, spawn_argv)
