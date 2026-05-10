"""``swarph install-hook`` — Phase 7 / v0.7 PR-C SessionStart memory injection.

Substrate-doc R7 §11.1.7.1 #1 operator-tooling sub-layer. Writes a
SessionStart hook to ``~/.claude/settings.json`` so any ``claude``
session bootstrap (NOT just ``swarph spawn``) auto-loads the
cell.yaml-derived starter prompt without operator-paste.

Why this exists (the gap PR-A + PR-B left open):
* ``swarph spawn`` injects starter prompt via ``--append-system-prompt``
  flag. Works only when the session is launched through swarph.
* Bare ``claude`` invocation (``claude --resume`` / ``claude`` from a new
  terminal / claude-code IDE integration) doesn't go through swarph and
  loses the starter prompt — operator must paste it manually each time.

The SessionStart hook closes the gap: every session bootstrap (any
launch path) calls ``swarph hook-output`` which discovers the cell.yaml
and emits the starter prompt as additionalContext.

Idempotent: rerun safe. Detects existing hook entries pointing at
``swarph hook-output`` and updates in-place rather than duplicating.

Skip when SWARPH_SPAWN=1 env (set by ``swarph spawn``) — avoids
double-injection when the spawn path already passed the prompt via
``--append-system-prompt``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from swarph_cli.cell import _atomic_write_text


_HOOK_COMMAND = "swarph hook-output"
"""The command Claude Code's SessionStart hook will invoke. Distinctive
enough to detect for idempotent install + uninstall."""


_USAGE = """\
Usage:
  swarph install-hook [--scope user|project] [--uninstall] [--dry-run]

Writes a SessionStart hook to settings.json that auto-loads the cell.yaml
starter prompt on every Claude Code session bootstrap (NOT just
`swarph spawn` invocations). Closes the operator-paste gap from PR-A/B
per substrate-doc R7 §11.1.7.1 #1.

Scopes:
  user      ~/.claude/settings.json (default)
  project   ./.claude/settings.json (current directory)

Flags:
  --uninstall   Remove the swarph SessionStart hook entry; preserves
                other hook entries the user has authored.
  --dry-run     Print resolved settings.json target + the diff that
                would be written; do not modify the file.
"""


def _settings_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project":
        return Path.cwd() / ".claude" / "settings.json"
    raise ValueError(f"install_hook: unknown scope {scope!r}")


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"swarph install-hook: settings.json is not valid JSON "
            f"({path}): {exc}. Refusing to overwrite — fix the file first."
        )


def _hook_entry() -> dict[str, Any]:
    """The hook entry shape Claude Code expects.

    SessionStart hook with a 'command' type that calls swarph
    hook-output. The empty matcher fires on both 'startup' and
    'resume' session starts.
    """
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": _HOOK_COMMAND,
                "timeout": 10,
            }
        ],
    }


def _has_swarph_hook(entry: dict[str, Any]) -> bool:
    """Detect a swarph-installed entry by command match.

    Used for idempotency: if the entry points at our command, we
    own it (update-or-replace). If it points at something else, we
    leave it alone.
    """
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and h.get("command") == _HOOK_COMMAND
        for h in hooks
    )


def _install(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert (or update) the swarph SessionStart hook entry.

    Returns ``(updated_settings, changed)`` where ``changed`` is True
    iff this call actually modified the in-memory dict (lets dry-run
    + idempotent rerun report 'no change' cleanly).
    """
    settings = dict(settings)  # shallow copy; nested dicts share refs
    hooks_block = settings.setdefault("hooks", {})
    if not isinstance(hooks_block, dict):
        raise SystemExit(
            "swarph install-hook: settings.hooks is not an object — "
            "refusing to overwrite. Fix settings.json schema first."
        )

    sessionstart_list = hooks_block.setdefault("SessionStart", [])
    if not isinstance(sessionstart_list, list):
        raise SystemExit(
            "swarph install-hook: settings.hooks.SessionStart is not an "
            "array — refusing to overwrite. Fix settings.json schema first."
        )

    swarph_owned = [
        i for i, e in enumerate(sessionstart_list)
        if isinstance(e, dict) and _has_swarph_hook(e)
    ]
    new_entry = _hook_entry()

    if swarph_owned:
        # Idempotent update: replace the first swarph-owned entry,
        # drop any duplicates (defensive — shouldn't happen but
        # protects against historical drift).
        first_idx = swarph_owned[0]
        if sessionstart_list[first_idx] == new_entry and len(swarph_owned) == 1:
            return settings, False  # no-op
        sessionstart_list[first_idx] = new_entry
        for idx in reversed(swarph_owned[1:]):
            del sessionstart_list[idx]
        return settings, True

    sessionstart_list.append(new_entry)
    return settings, True


def _uninstall(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Remove swarph-owned SessionStart hook entries.

    Preserves other (operator-authored) entries the user may have
    added — defensive design per ``feedback_settings_json_via_skill``
    (don't trample on operator config).
    """
    settings = dict(settings)
    hooks_block = settings.get("hooks")
    if not isinstance(hooks_block, dict):
        return settings, False
    sessionstart_list = hooks_block.get("SessionStart")
    if not isinstance(sessionstart_list, list):
        return settings, False

    swarph_owned_idx = [
        i for i, e in enumerate(sessionstart_list)
        if isinstance(e, dict) and _has_swarph_hook(e)
    ]
    if not swarph_owned_idx:
        return settings, False

    for idx in reversed(swarph_owned_idx):
        del sessionstart_list[idx]

    # Clean up empty SessionStart array + empty hooks object so the
    # uninstall leaves no swarph residue.
    if not sessionstart_list:
        del hooks_block["SessionStart"]
    if not hooks_block:
        del settings["hooks"]
    return settings, True


def run_install_hook(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph install-hook"

    p = argparse.ArgumentParser(prog="swarph install-hook", add_help=True)
    p.add_argument(
        "--scope", choices=("user", "project"), default="user",
        help="Where to write the hook (default: user / ~/.claude/settings.json).",
    )
    p.add_argument(
        "--uninstall", action="store_true",
        help="Remove the swarph SessionStart hook entry instead of installing.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print target path + planned change; do not modify the file.",
    )

    if not argv and not sys.stdin.isatty():
        # No args + not a TTY = probably a piped invocation; show usage
        # rather than guess.
        print(_USAGE, file=sys.stderr)
        return 0

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    target = _settings_path(args.scope)
    target.parent.mkdir(parents=True, exist_ok=True)

    settings_before = _read_settings(target)

    if args.uninstall:
        settings_after, changed = _uninstall(settings_before)
        action = "uninstall"
    else:
        settings_after, changed = _install(settings_before)
        action = "install"

    if args.dry_run:
        print(f"# swarph install-hook --dry-run", file=sys.stderr)
        print(f"#   scope:  {args.scope}", file=sys.stderr)
        print(f"#   target: {target}", file=sys.stderr)
        print(f"#   action: {action}", file=sys.stderr)
        print(f"#   changed: {changed}", file=sys.stderr)
        if changed:
            print("# planned settings.json:", file=sys.stderr)
            print(json.dumps(settings_after, indent=2, sort_keys=True))
        return 0

    if not changed:
        print(
            f"swarph install-hook: no change needed at {target} "
            f"(swarph SessionStart hook already in desired state).",
            file=sys.stderr,
        )
        return 0

    rendered = json.dumps(settings_after, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(target, rendered)
    print(
        f"swarph install-hook: {action}ed at {target}.",
        file=sys.stderr,
    )
    if action == "install":
        print(
            "Hook fires on every Claude Code session bootstrap; "
            "calls `swarph hook-output` which discovers cell.yaml "
            "and emits the starter prompt as session context.",
            file=sys.stderr,
        )
        print(
            "\nDiscovery order at hook fire time:\n"
            "  1. ./cell.yaml in current working directory\n"
            "  2. $XDG_CONFIG_HOME/swarph/cells/<basename(cwd)>.yaml\n"
            "If your cwd is /root then the fallback name resolves to "
            "'root.yaml' — author cell.yaml at cwd OR rename per the\n"
            "basename rule. v0.8+ may add a `SWARPH_CELL` env var "
            "override for explicit cell selection (drop-mother #996).\n",
            file=sys.stderr,
        )
    return 0
