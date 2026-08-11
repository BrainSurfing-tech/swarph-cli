"""Install native Codex lifecycle hooks for a Swarph cell.

The installer deliberately pins the Python interpreter that owns Swarph rather
than emitting a bare ``swarph`` command.  Windows PATH may retain an obsolete
launcher after a pipx migration, so presence on PATH is not a liveness proof.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from swarph_cli.cell import _atomic_write_text


_OWNED_MODULE = "swarph_cli"
_EVENTS = ("SessionStart", "PreToolUse", "Stop")
_OWNED_VERBS = ("codex-hook-output", "hooks touch-activity")


def _hooks_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".codex" / "hooks.json"
    if scope == "project":
        return Path.cwd() / ".codex" / "hooks.json"
    raise ValueError(f"install_codex_hooks: unknown scope {scope!r}")


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"swarph install-codex-hooks: hooks.json is not valid JSON ({path}): {exc}")
    if not isinstance(value, dict):
        raise SystemExit("swarph install-codex-hooks: hooks.json must contain an object")
    return value


def _command(verb: str, *, windows: bool) -> str:
    interpreter = str(Path(sys.executable).resolve())
    if windows:
        return f'"{interpreter}" -m {_OWNED_MODULE} {verb}'
    return f"{shlex.quote(interpreter)} -m {_OWNED_MODULE} {verb}"


def _handler(verb: str, *, context_limit: int | None = None) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": _command(verb, windows=False),
        "commandWindows": _command(verb, windows=True),
        "timeout": 10,
    }
    if context_limit is not None:
        handler["additionalContextLimit"] = context_limit
    return handler


def _desired_hooks() -> dict[str, list[dict[str, Any]]]:
    return {
        "SessionStart": [{"matcher": "", "hooks": [_handler("codex-hook-output", context_limit=5000)]}],
        "PreToolUse": [{"matcher": "", "hooks": [_handler("hooks touch-activity")]}],
        "Stop": [{"matcher": "", "hooks": [_handler("hooks touch-activity")]}],
    }


def _is_owned_handler(handler: object) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    return isinstance(command, str) and any(
        f"-m {_OWNED_MODULE} {verb}" in command for verb in _OWNED_VERBS
    )


def _without_owned(entries: list[object]) -> list[object]:
    kept: list[object] = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        handlers = entry.get("hooks")
        if not isinstance(handlers, list):
            kept.append(entry)
            continue
        remaining = [handler for handler in handlers if not _is_owned_handler(handler)]
        if remaining:
            updated = dict(entry)
            updated["hooks"] = remaining
            kept.append(updated)
    return kept


def _install(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("swarph install-codex-hooks: hooks.json hooks must be an object")
    for event, desired in _desired_hooks().items():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise SystemExit(f"swarph install-codex-hooks: hooks.{event} must be an array")
        hooks[event] = _without_owned(entries) + desired
    return updated, updated != config


def _uninstall(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated, False
    for event in _EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        remaining = _without_owned(entries)
        if remaining:
            hooks[event] = remaining
        else:
            hooks.pop(event, None)
    if not hooks:
        updated.pop("hooks", None)
    return updated, updated != config


def run_install_codex_hooks(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarph install-codex-hooks")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[2:])

    target = _hooks_path(args.scope)
    before = _read_hooks(target)
    after, changed = _uninstall(before) if args.uninstall else _install(before)
    action = "uninstall" if args.uninstall else "install"
    if args.dry_run:
        print(json.dumps(after, indent=2, sort_keys=True))
        return 0
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, json.dumps(after, indent=2, sort_keys=True) + "\n")
    print(f"swarph install-codex-hooks: {action} {'updated' if changed else 'already current'} at {target}.", file=sys.stderr)
    if not args.uninstall:
        print("Review and trust the new commands with Codex /hooks before they run.", file=sys.stderr)
    return 0
