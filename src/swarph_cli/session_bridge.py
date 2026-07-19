"""session_bridge — hardened primitives to deliver a mesh DM INTO a cell's
live agent TUI pane. Ported from lab-orchestrator/workers/cell_wake.py (the
incident-proven original); hardcoded "tmux" replaced by the cross-platform
multiplexer binary. Fail-safe throughout: never inject into a busy/ambiguous
pane; any error → defer, never raise.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from swarph_cli.multiplexer import find_multiplexer

# Busy / dialog / approval markers in a Claude TUI pane. ANY (case-insensitive)
# means mid-turn or a non-idle prompt → not safe to inject.
_BUSY_MARKERS = (
    "esc to interrupt", "thinking…", "compacting", "(esc)",
    "(y/n)", "❯ 1.", "do you want", "approve", "│ >",
)
# Positive idle sentinel — the Claude REPL footer hint. Presence (with NO busy
# marker) POSITIVELY confirms an idle input prompt.
_IDLE_SENTINEL = "? for shortcuts"
# Modals ALWAYS safe to Escape-dismiss — pure telemetry the cell never answers
# but which trips a busy marker and stalls a wake forever.
_SAFE_DISMISSABLE_MODALS = (
    "how is claude doing this session",
)

_WS_RUN = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _mux() -> Optional[str]:
    """The resolved tmux-compatible binary (tmux/psmux), or None."""
    return find_multiplexer()


def _capture(pane_id: str) -> Optional[str]:
    """`capture-pane -p` the pane. None on any failure (fail-safe)."""
    mux = _mux()
    if mux is None:
        return None
    try:
        r = subprocess.run(
            [mux, "capture-pane", "-p", "-t", pane_id],
            capture_output=True, timeout=5, text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


def probe_pane(pane_id: str) -> str:
    """Three-way pane state: "idle" | "busy" | "modal".

    "modal" (a known-safe dismissable telemetry popup) is checked BEFORE
    "busy" so it routes to the dismiss path. Positive-idle only: an idle
    footer/bare-">" prompt with NO busy marker. Everything else, incl. any
    capture failure, → "busy" (defer, never inject)."""
    content = _capture(pane_id)
    if content is None or not content.strip():
        return "busy"
    low = content.lower()
    if any(m in low for m in _SAFE_DISMISSABLE_MODALS):
        return "modal"
    if any(m in low for m in _BUSY_MARKERS):
        return "busy"
    if _IDLE_SENTINEL in low:
        return "idle"
    non_empty = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    if non_empty and non_empty[-1].strip() == ">":
        return "idle"
    return "busy"


def _send_key(pane_id: str, key: str) -> bool:
    """Send a single KEY (key-name, NOT literal). Fail-safe on error."""
    mux = _mux()
    if mux is None:
        return False
    try:
        r = subprocess.run(
            [mux, "send-keys", "-t", pane_id, key],
            capture_output=True, timeout=5, text=True,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return False


def try_dismiss_safe_modal(pane_id: str) -> bool:
    """If the pane shows a KNOWN-SAFE dismissable modal, Escape it + return
    True. False when none present (caller never Escapes a real busy state)."""
    content = _capture(pane_id)
    if content is None:
        return False
    low = content.lower()
    if not any(m in low for m in _SAFE_DISMISSABLE_MODALS):
        return False
    if not _send_key(pane_id, "Escape"):
        return False
    time.sleep(0.4)  # let the TUI re-render before the caller re-probes
    return True
