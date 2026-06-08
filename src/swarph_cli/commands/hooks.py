"""``swarph hooks`` — Claude Code hooks installer (T1: merge core only).

This module ships the PURE settings.json merge primitives that wire
Claude Code hooks into ``~/.claude/settings.json`` as installable
content. Hooks become installable artifacts (shell scripts wired into
event handlers) WITHOUT a swarph-cli version bump per hook — the
installer reads bundled hook content and merges it into the user's
settings, the same way ``watchdog --install-service`` installs systemd
units as bundled data.

Claude Code's settings.json hook shape::

    {
      "hooks": {
        "StopFailure": [
          { "matcher": "rate_limit",
            "hooks": [ { "type": "command",
                         "command": "~/.swarph/hooks/cell-resilience.sh" } ] }
        ],
        "PostToolUse": [ ... ]
      }
    }

``hooks`` maps an EVENT name → a list of entries; each entry has a
``matcher`` (string; ``""`` = match-all) and a ``hooks`` list of
``{type, command}`` actions.

T1 SCOPE: ``_load_settings`` / ``_save_settings`` / ``_merge_hook`` /
``_unmerge_hook`` only. No CLI command, no argparse — those land in a
later task. The merge functions are pure (mutate-and-return a dict) so
the eventual CLI layer is a thin read → merge → atomic-write wrapper.

Atomic-write discipline mirrors ``watchdog.py``: write a temp file in
the SAME directory, then ``os.replace`` onto the target (atomic rename
on POSIX) so a crash mid-write never leaves a truncated settings.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _load_settings(path) -> dict:
    """Load settings.json, returning ``{}`` for a not-yet-existing file.

    * Missing file → ``{}`` — a settings.json that doesn't exist yet is a
      fine empty starting point for the installer.
    * Present but CORRUPT JSON → raise ``ValueError`` (with the path in the
      message). We MUST NOT silently return ``{}`` here: a later
      ``_save_settings`` would then overwrite the user's real-but-unparseable
      settings with our merged-onto-empty result, silently destroying their
      config. Surfacing the error protects them — the caller stops and the
      user fixes (or we back up) the broken file first.
    * Valid → the parsed dict.
    """
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"settings.json at {p} is not valid JSON: {exc}"
        ) from exc


def _save_settings(path, obj) -> None:
    """Atomically write ``obj`` as JSON to ``path``.

    Creates parent dirs as needed. Writes to a NamedTemporaryFile in the
    same directory (so ``os.replace`` is a same-filesystem atomic rename),
    json.dump with ``indent=2``, flushes + fsyncs, then replaces the target.
    Mirrors watchdog.py's atomic-write discipline: a crash mid-write leaves
    either the old complete file or the new complete file, never a truncated
    one.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        # Clean up the temp file on any failure so we don't leak it.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _merge_hook(settings: dict, event: str, matcher: str, command: str) -> dict:
    """Merge one ``{event, matcher, command}`` hook into ``settings``.

    Mutate-and-return. Idempotent: dedups on ``command`` within the
    matching-matcher entry so re-running the installer never stacks
    duplicate actions. Preserves every other top-level key, every other
    event, and every other matcher-entry untouched.
    """
    hooks = settings.setdefault("hooks", {})
    event_list = hooks.setdefault(event, [])

    action = {"type": "command", "command": command}

    for entry in event_list:
        if entry.get("matcher", "") == matcher:
            actions = entry.setdefault("hooks", [])
            # Dedup on command — idempotent re-install.
            if not any(a.get("command") == command for a in actions):
                actions.append(action)
            return settings

    # No entry for this matcher yet — append a fresh one.
    event_list.append({"matcher": matcher, "hooks": [action]})
    return settings


def _unmerge_hook(settings: dict, event: str, matcher: str, command: str) -> dict:
    """Reverse of ``_merge_hook``. Mutate-and-return, idempotent.

    No-op (never raises) when the hooks key, the event, the matcher, or the
    command is absent. Removes the matching action; prunes the entry when its
    ``hooks`` list becomes empty; prunes the event key when its list becomes
    empty. Preserves all siblings (other events, matchers, commands, and
    top-level keys).
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    event_list = hooks.get(event)
    if not isinstance(event_list, list):
        return settings

    for entry in event_list:
        if entry.get("matcher", "") == matcher:
            actions = entry.get("hooks", [])
            entry["hooks"] = [
                a for a in actions if a.get("command") != command
            ]
            if not entry["hooks"]:
                event_list.remove(entry)
            break

    if not event_list:
        del hooks[event]

    return settings
