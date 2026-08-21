"""``swarph memory-emit-hook`` — emit-on-write: a memory write caches its own highlight.

Called BY a PostToolUse hook (matcher ``Write|Edit|MultiEdit|NotebookEdit``).
When the written file is a MEMORY (``<any>/.claude/projects/<proj>/memory/<slug>.md``,
excluding the ``MEMORY.md`` index), appends a highlight naming the memory's
``[[pointer]]`` to the shared timeline via the gateway ``/highlights`` path —
peer-token-by-default, zero new per-cell config, and pushed by the gateway so
every cell's next pull converges.

THE LOOP THIS CLOSES: today a memory only reaches the timeline if someone
remembers to run ``swarph highlight`` — a verb-dependent loop is a loop that
silently stops. With emit-on-write the cache is a side effect of the write
itself, and the PostCompact recall (``postcompact_hook_output``) can surface
it the same day.

A repeat write of the same memory within ``_SUPPRESS_WINDOW`` is suppressed —
the timeline is append-only, not append-always. State is a tiny JSON file,
best-effort.

Failure-mode invariant: exit 0 on EVERY path, print nothing, never raise. A
hook that fails a tool result because the timeline is unreachable inverts every
priority this system has.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from typing import Optional

from swarph_cli.commands.highlight import _log_via_gateway, _resolve_gateway

_SUPPRESS_WINDOW = dt.timedelta(minutes=30)
_SNIPPET_CHARS = 120

# <anything>/.claude/projects/<project>/memory/<slug>.md — the Claude auto-memory
# layout. The index (MEMORY.md) is not a memory. SWARPH_MEMORY_DIR names an
# extra directory for harnesses whose memories live elsewhere.


def _memory_slug(file_path: str) -> Optional[str]:
    """The memory slug if file_path is a memory write, else None."""
    p = Path(file_path)
    if p.suffix != ".md" or p.name == "MEMORY.md":
        return None
    extra = os.environ.get("SWARPH_MEMORY_DIR", "").strip()
    if extra:
        try:
            if p.parent == Path(os.path.expanduser(extra)):
                return p.stem
        except Exception:
            pass
    parts = p.parts
    for i in range(len(parts) - 3):
        if (parts[i] == ".claude" and parts[i + 1] == "projects"
                and parts[i + 3] == "memory" and i + 4 == len(parts) - 1):
            return p.stem
    return None


def _snippet(path: Path) -> str:
    """First non-empty line, de-headed, collapsed, truncated. Unreadable file →
    empty snippet (the pointer still emits — the write happened either way)."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                return " ".join(s.split())[:_SNIPPET_CHARS]
    except Exception:
        pass
    return ""


def _state_path() -> Path:
    raw = os.environ.get("SWARPH_EMIT_STATE", "").strip()
    if raw:
        return Path(raw)
    return Path(tempfile.gettempdir()) / "swarph-memory-emit.json"


def _recently_emitted(slug: str) -> bool:
    """True (suppress) if this slug was emitted within the window. Best-effort:
    a corrupt/absent state file means 'not recently emitted'."""
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
        last = dt.datetime.fromisoformat(state[slug])
        return dt.datetime.now(dt.timezone.utc) - last < _SUPPRESS_WINDOW
    except Exception:
        return False


def _mark_emitted(slug: str) -> None:
    try:
        sp = _state_path()
        state = {}
        if sp.exists():
            state = json.loads(sp.read_text(encoding="utf-8"))
        state[slug] = dt.datetime.now(dt.timezone.utc).isoformat()
        sp.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _cell() -> str:
    return (os.environ.get("SWARPH_CELL") or os.environ.get("SWARPH_SELF")
            or socket.gethostname())


def run_memory_emit_hook(argv: list[str] | None = None) -> int:
    try:
        raw = ""
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    try:
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return 0
        slug = _memory_slug(file_path)
        if slug is None or _recently_emitted(slug):
            return 0

        snippet = _snippet(Path(file_path))
        text = f"memory cached: {snippet}" if snippet else "memory cached"
        gateway = _resolve_gateway(None)
        if not gateway:
            return 0  # no gateway configured -> nowhere to emit; silent by design
        rc = _log_via_gateway(gateway, _cell(), text, f"[[{slug}]]", None, None)
        if rc == 0:
            _mark_emitted(slug)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run_memory_emit_hook(sys.argv[1:]))
