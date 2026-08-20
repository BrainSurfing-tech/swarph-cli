"""``swarph install-wake-hook`` — board card #482 silent-wake hook bundle.

Installs a session-start hook that gives the cell a DM wake path, with the
product decided by WHERE THE WAKE LIVES (the card's accepted two-valued
design), not by which harness happens to be detected:

* ``claude`` / ``codex`` → ARM-INSTRUCTION: the session-start hook emits
  the watch pipeline (``tail -F inbox.log | dm_notify_filter``) as session
  context so the agent arms it as a background watch.
* ``cursor`` → VERIFY-AND-REPORT: the wake already lives in swarph (the
  monitor's push sink); the hook verifies it at every session start and
  says loudly when the cell has no wake path.
* unknown / undetectable harness → LOUD REFUSAL: nonzero exit, nothing
  written. A silent no-op here would manufacture exactly the
  armed-looking-but-deaf cell this card exists to eliminate.

Hook config targets:
* claude  → ``~/.claude/settings.json``  ``hooks.SessionStart``
* codex   → ``~/.codex/hooks.json``      ``SessionStart``
* cursor  → ``~/.cursor/hooks.json``     ``sessionStart``

Idempotent, ``--uninstall``, ``--dry-run`` — same operator contract as
``swarph install-hook``. The installed command pins the current Python
interpreter (Windows PATH may retain an obsolete ``swarph`` launcher after
a pipx migration; presence on PATH is not a liveness proof).
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from swarph_cli.cell import _atomic_write_text
from swarph_cli.commands.wake_hook_output import _tmux_session_cell


_VERB = "wake-hook-output"
_KNOWN_HARNESSES = ("claude", "codex", "cursor", "muse", "antigravity")


def _command(harness: str, cell: Optional[str] = None) -> str:
    interpreter = str(Path(sys.executable).resolve())
    cmd = f"{shlex.quote(interpreter)} -m swarph_cli {_VERB} --harness {harness}"
    if cell:
        cmd += f" --cell {shlex.quote(cell)}"
    return cmd


def _config_path(harness: str, scope: str) -> Path:
    if scope == "project":
        base = Path.cwd()
    else:
        base = Path.home()
    if harness == "claude":
        return base / ".claude" / "settings.json"
    if harness == "codex":
        return base / ".codex" / "hooks.json"
    if harness == "cursor":
        return base / ".cursor" / "hooks.json"
    if harness == "muse":
        return base / ".config" / "muse" / "settings.json"
    if harness == "antigravity":
        return base / ".gemini" / "config" / "hooks.json"
    raise ValueError(f"install-wake-hook: unknown harness {harness!r}")



def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"swarph install-wake-hook: {path} is not valid JSON: {exc}. "
            "Refusing to overwrite — fix the file first."
        )
    if not isinstance(value, dict):
        raise SystemExit(
            f"swarph install-wake-hook: {path} must contain a JSON object — "
            "refusing to overwrite."
        )
    return value


def _event_key(harness: str) -> tuple[str, ...]:
    """Path to the hook list inside the config, per harness schema."""
    if harness in ("claude", "muse"):
        return ("hooks", "SessionStart")
    if harness == "codex":
        return ("SessionStart",)
    if harness == "antigravity":
        return ("swarph-wake-hook", "PreInvocation")
    return ("sessionStart",)


def _is_owned_entry(entry: Any) -> bool:
    """Detect a swarph wake-hook entry by the baked-in verb."""
    if not isinstance(entry, dict):
        return False
    if _VERB in str(entry.get("command", "")):
        return True
    # claude/codex shape: {"matcher": ..., "hooks": [{"type": "command", ...}]}
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        return any(
            isinstance(h, dict) and _VERB in str(h.get("command", ""))
            for h in hooks
        )
    return False


def _new_entry(harness: str, cell: Optional[str] = None) -> dict[str, Any]:
    cmd = _command(harness, cell)
    if harness == "cursor":
        return {"command": cmd}
    if harness == "antigravity":
        return {"type": "command", "command": cmd, "timeout": 10}
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": cmd, "timeout": 10}],
    }



def _get_list(config: dict[str, Any], harness: str) -> list[Any]:
    node: Any = config
    keys = _event_key(harness)
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise SystemExit(
                f"swarph install-wake-hook: config key {key!r} is not an "
                "object — refusing to overwrite. Fix the file first."
            )
    entries = node.setdefault(keys[-1], [])
    if not isinstance(entries, list):
        raise SystemExit(
            f"swarph install-wake-hook: config key {keys[-1]!r} is not an "
            "array — refusing to overwrite. Fix the file first."
        )
    return entries


def _install(
    config: dict[str, Any], harness: str, cell: Optional[str] = None
) -> tuple[dict[str, Any], bool]:
    config = dict(config)
    entries = _get_list(config, harness)
    owned = [i for i, e in enumerate(entries) if _is_owned_entry(e)]
    new_entry = _new_entry(harness, cell)
    if owned:
        first = owned[0]
        if entries[first] == new_entry and len(owned) == 1:
            return config, False
        entries[first] = new_entry
        for idx in reversed(owned[1:]):
            del entries[idx]
        return config, True
    entries.append(new_entry)
    return config, True


def _uninstall(config: dict[str, Any], harness: str) -> tuple[dict[str, Any], bool]:
    config = dict(config)
    keys = _event_key(harness)
    node: Any = config
    for key in keys[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            return config, False
    entries = node.get(keys[-1])
    if not isinstance(entries, list):
        return config, False
    owned = [i for i, e in enumerate(entries) if _is_owned_entry(e)]
    if not owned:
        return config, False
    for idx in reversed(owned):
        del entries[idx]
    return config, True


def _detect_harness() -> Optional[str]:
    """Best-effort detection from the environment the installer runs in.

    Explicit ``--harness`` always wins; detection exists so the common
    invocation (run from inside the harness being armed) needs no flag.
    Ambiguous or empty detection returns None → loud refusal.
    """
    import os

    env = os.environ
    if env.get("CURSOR_DATA_DIR") or env.get("CURSOR_SESSION_ID"):
        return "cursor"
    if env.get("CLAUDE_CODE_ENTRYPOINT") or env.get("CLAUDECODE"):
        return "claude"
    if env.get("CODEX_CI") or env.get("CODEX_SANDBOX"):
        return "codex"
    if env.get("MUSE_CODE") or env.get("MUSE_SESSION_ID") or env.get("MUSE"):
        return "muse"
    if (
        env.get("ANTIGRAVITY_AGENT")
        or env.get("ANTIGRAVITY_CLI")
        or env.get("ANTIGRAVITY_WORKSPACE")
        or env.get("GEMINI_CLI_SESSION")
    ):
        return "antigravity"
    return None



_USAGE = """\
Usage:
  swarph install-wake-hook [--harness claude|codex|cursor|muse|antigravity]
                           [--scope user|project] [--uninstall] [--dry-run]


Installs the silent-wake session-start hook (board card #482). The product
depends on where the wake lives: claude/codex get an arm-instruction (the
tail -F inbox.log | dm_notify_filter watch), cursor gets verify-and-report
of the swarph monitor's push sink. Unknown harnesses are refused loudly.
--cell is refused with --scope user (card #527): a baked cell name in a
box-global file arms one cell's wake for every session on the box and
silently evicts the last installed cell's. Omit --cell and the hook
resolves the cell at runtime from the tmux session name.
"""


def run_install_wake_hook(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]

    p = argparse.ArgumentParser(prog="swarph install-wake-hook")
    p.add_argument("--harness", default=None, help="target harness (default: detect)")
    p.add_argument(
        "--cell",
        default=None,
        help="cell name to bake into the hook command (default: the hook "
        "resolves the tmux session name, then $SWARPH_SELF, then cwd "
        "discovery, at fire time). Valid only with --scope project — "
        "a baked name in a box-global file is refused (card #527)",
    )
    p.add_argument("--scope", choices=("user", "project"), default="user")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    if not argv and not sys.stdin.isatty():
        print(_USAGE, file=sys.stderr)
        return 0

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    harness = (args.harness or "").strip().lower() or _detect_harness()
    if harness not in _KNOWN_HARNESSES:
        print(
            "swarph install-wake-hook: LOUD REFUSAL — "
            + (
                f"unsupported harness {args.harness!r}. "
                if args.harness
                else "could not detect the harness from this environment. "
            )
            + f"Known harnesses: {', '.join(_KNOWN_HARNESSES)}. "
            "Nothing was written. A silent no-op here would manufacture an "
            "armed-looking-but-deaf cell, which is the failure this card "
            "exists to eliminate. Pass --harness explicitly if detection "
            "missed, or arm a watch manually.",
            file=sys.stderr,
        )
        return 2

    target = _config_path(harness, args.scope)

    # Card #527 task 2: the verb has BOTH facts at install time and must
    # check the pair. A per-cell name baked into a BOX-GLOBAL file is a
    # fleet-wide misconfiguration with a single-cell author: every session
    # on the box is told to tail one arbitrary cell's inbox, and the next
    # cell's install silently evicts the last (measured live: gpt-ops
    # 14:03, cursor-lin 14:48, mistral 08:55 — three evictions, zero
    # notifications). Compare PATHS, not flags: --scope project run from
    # the box's home directory lands on the SAME box-global file (a cell
    # whose cell.yaml cwd IS the home directory would otherwise become
    # the next evictor while following the guide's own advice). Runtime
    # resolution (task 1) makes box-global installs correct WITHOUT a
    # baked name.
    if (
        args.cell
        and not args.uninstall
        and target == _config_path(harness, "user")
    ):
        print(
            "swarph install-wake-hook: LOUD REFUSAL — --cell with a "
            f"box-global target ({target}) arms ONE cell's wake in a file "
            "EVERY session on the box reads, and silently evicts the "
            "previously installed cell's (card #527). The two valid "
            "combinations: a box-global install WITHOUT --cell (the hook "
            "resolves the cell at runtime from the tmux session name), or "
            "--scope project WITH --cell run from a directory that is NOT "
            "the box's home (a per-project file only that project's "
            "sessions read). Nothing was written.",
            file=sys.stderr,
        )
        return 2

    # Card #527 review (lab-ovh): a baked --cell that disagrees with the
    # installer's own resolving tmux session is very likely a
    # self-misconfiguration — at runtime a resolving session OUTRANKS the
    # baked name, so the request can never take effect for those sessions.
    # WARNING, not refusal: an orchestrator installing another cell's
    # project hook from its own session is legitimate (readers in the
    # target cell's tmux resolve themselves and agree), so refusing here
    # would block a valid use.
    if args.cell and not args.uninstall:
        tmux = _tmux_session_cell()
        if tmux is not None and tmux[0] != args.cell:
            print(
                f"swarph install-wake-hook: WARNING — you are installing "
                f"--cell {args.cell} from a session that resolves to "
                f"{tmux[0]} ({tmux[1]}). At runtime a resolving tmux "
                "session outranks the baked name (card #527): sessions "
                "whose tmux name is a known cell will run as THEMSELVES, "
                f"not as {args.cell}. If you are installing your own hook, "
                "the name you baked is not yours.",
                file=sys.stderr,
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    before = _read_config(target)

    if args.uninstall:
        after, changed = _uninstall(before, harness)
        action = "uninstall"
    else:
        after, changed = _install(before, harness, cell=args.cell)
        action = "install"

    if args.dry_run:
        print("# swarph install-wake-hook --dry-run", file=sys.stderr)
        print(f"#   harness: {harness}", file=sys.stderr)
        print(f"#   target:  {target}", file=sys.stderr)
        print(f"#   action:  {action}", file=sys.stderr)
        print(f"#   changed: {changed}", file=sys.stderr)
        if changed:
            print(json.dumps(after, indent=2, sort_keys=True))
        return 0

    if not changed:
        print(
            f"swarph install-wake-hook: no change needed at {target} "
            "(wake hook already in desired state).",
            file=sys.stderr,
        )
        return 0

    payload = json.dumps(after, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(target, payload)
    # ASSERT THE ARTIFACT CHANGED AT THE STEP THAT CHANGES IT (card #527
    # task 3): "installed" is a claim about the filesystem and must be
    # checked against it. Three specimens of report-success-change-nothing
    # shipped before this assert existed, and cursor-lin's ran in the
    # RELEASED 0.44.0 wheel — version skew cannot explain it. A slow
    # external revert can still escape a check taken this close to the
    # write; what this catches is the write that never landed at all.
    try:
        landed = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"swarph install-wake-hook: WRITE DID NOT LAND — cannot re-read "
            f"{target} after writing ({exc}). The {action} is NOT in effect. "
            "Investigate what owns that file before retrying.",
            file=sys.stderr,
        )
        return 2
    if landed != payload:
        print(
            f"swarph install-wake-hook: WRITE DID NOT LAND — {target} "
            "re-reads as different content than was written. Something on "
            f"this box reverted or swallowed the {action}; it is NOT in "
            "effect. Investigate what owns that file before retrying.",
            file=sys.stderr,
        )
        return 2
    print(f"swarph install-wake-hook: {action}ed at {target}.", file=sys.stderr)
    if action == "install":
        product = (
            "arm-instruction (the session-start hook emits the "
            "tail -F inbox.log | dm_notify_filter watch as session context)"
            if harness in ("claude", "codex", "muse")
            else "verify-and-report (the session-start hook checks the "
            "swarph monitor's push sink and says loudly if none exists)"
        )
        print(f"Product for {harness}: {product}.", file=sys.stderr)
        print(
            "Complement: card #487 covers the armed watch going silent "
            "MID-SESSION; this hook covers session start only. Neither "
            "substitutes for the other.",
            file=sys.stderr,
        )
    return 0
