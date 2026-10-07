"""``swarph rowclear-hook`` — SessionStart hook for card #432.

Claude Code runs this on SessionStart. When the payload's ``source`` is
``clear``, the new ``session_id`` is written into the role's
``<role>.session-id`` pin (the role is ``SWARPH_SELF``). Spawn then
resumes that id instead of the pre-clear session.

Exit 0 on every path. A hook that fails closed refuses the session.
"""

from __future__ import annotations

import json
import sys

from swarph_cli import identity
from swarph_cli.rowclear import apply_sessionstart_clear


def _read_stdin() -> dict:
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                return json.loads(raw.lstrip("\ufeff"))
    except Exception:
        return {}
    return {}


def run_rowclear_hook(argv: list[str] | None = None) -> int:
    del argv
    role = identity.declared(env=identity.SELF_ONLY)[0] or ""
    if not role:
        print("rowclear-hook: SWARPH_SELF unset — pin not rewritten",
              file=sys.stderr)
        return 0
    try:
        apply_sessionstart_clear(_read_stdin(), role)
    except Exception as exc:
        print(f"rowclear-hook: {exc}", file=sys.stderr)
    return 0
