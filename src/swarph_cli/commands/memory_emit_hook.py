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

Failure-mode invariant: exit 0 on EVERY path, never raise. The hook prints
nothing OF ITS OWN; the shared ``_log_via_gateway`` prints its one-line
"logged -> TIMELINE.md" on success (cursor-win, 2026-08-22: the invariant was
misdescribed, not violated). A hook that fails a tool result because the
timeline is unreachable inverts every priority this system has.
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
    """One-line summary of a memory: its frontmatter `description`, else the first body
    line. Unreadable file → empty snippet (the pointer still emits — the write happened).

    Every memory file opens with YAML frontmatter, so "first non-empty line" returned the
    literal `---` delimiter. That is a NON-EMPTY STRING, so the caller's `if snippet` guard
    passed and the timeline took `memory cached: ---`. MEASURED 2026-09-15: 21 such entries
    on the shared timeline between 2026-08-25 and 2026-09-15, every one of them useless.
    An emptiness guard cannot catch a delimiter; the fix has to be here, not at the guard.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    body = lines
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                # `description:` exists precisely to be a one-line summary — prefer it.
                for fm in lines[1:i]:
                    if fm.strip().lower().startswith("description:"):
                        d = fm.split(":", 1)[1].strip().strip('"').strip("'")
                        if d:
                            return " ".join(d.split())[:_SNIPPET_CHARS]
                body = lines[i + 1:]
                break
        else:
            body = lines[1:]          # unterminated frontmatter: skip the opening delimiter
    for line in body:
        s = line.strip().lstrip("#").strip()
        if s and s != "---":          # a stray delimiter is never a summary
            return " ".join(s.split())[:_SNIPPET_CHARS]
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
    # SWARPH_SELF outranks SWARPH_CELL — the house order (#332: a self-identity
    # statement outranks an ambient one), and the live reason is #538: psmux
    # leaks SWARPH_CELL from the spawning environment, so a CELL-first order
    # posts this cell's highlight under ANOTHER cell's name and token
    # (measured by cursor-win on the Windows membrane, 2026-08-22).
    return (os.environ.get("SWARPH_SELF") or os.environ.get("SWARPH_CELL")
            or socket.gethostname())


def run_memory_emit_hook(argv: list[str] | None = None) -> int:
    try:
        raw = ""
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
        # PS 5.1 native pipes prefix a (double) UTF-8 BOM — json.loads raises,
        # and the failure invariant converts that into an INVISIBLE no-op
        # (measured by cursor-win, 2026-08-22). Strip before parse.
        payload = json.loads(raw.lstrip("\ufeff")) if raw.strip() else {}
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
