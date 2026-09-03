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
            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


def _membrane_for(provider: Optional[str]):
    """Look up the spawn membrane. None → caller fails closed.

    Lazy import: spawn.py does not import session_bridge, so this does not
    cycle. A parallel marker table here is the defect #682 is replacing.
    """
    if not provider:
        return None
    from swarph_cli.commands.spawn import MEMBRANES
    return MEMBRANES.get(provider)


def probe_pane(pane_id: str, provider: Optional[str] = None) -> str:
    """Pane state: "idle" | "busy" | "modal" | "unknown".

    Capture failure / empty pane → "busy" (defer, never inject).
    No provider, or a membrane that has not declared a pane predicate →
    "unknown". The daemon treats unknown exactly as busy.

    idle is NOT "a hint string is present". That hint is the empty-input
    chrome and vanishes when the box has text — the self-sealing deadlock.
    Each membrane declares its own busy marker and input-box shape;
    idle == no busy marker AND empty input.
    """
    content = _capture(pane_id)
    if content is None or not content.strip():
        return "busy"
    membrane = _membrane_for(provider)
    if membrane is None:
        return "unknown"
    return membrane.pane_state(content)


def _send_key(pane_id: str, key: str) -> bool:
    """Send a single KEY (key-name, NOT literal). Fail-safe on error."""
    mux = _mux()
    if mux is None:
        return False
    try:
        r = subprocess.run(
            [mux, "send-keys", "-t", pane_id, key],
            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="replace",
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


def _sanitize(text: str) -> str:
    """Strip ALL control bytes, then collapse whitespace runs to a space.
    Result contains no control bytes and no \\n/\\r, so even a literal `-l`
    send cannot trigger embedded-key / ANSI interpretation."""
    if not text:
        return ""
    return _WS_RUN.sub(" ", _CTRL.sub("", text)).strip()


def inject(pane_id: str, text: str) -> bool:
    """Deliver `text` into the pane: sanitize → literal `send-keys -l` →
    exactly one `Enter`. Leading `/` defanged. Two subprocess calls; True iff
    both return 0. Fail-safe: no mux / any error → False (caller re-queues)."""
    mux = _mux()
    if mux is None:
        return False
    body = _sanitize(text)
    if body.startswith("/"):
        body = " " + body
    try:
        r1 = subprocess.run(
            [mux, "send-keys", "-t", pane_id, "-l", body],
            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="replace",
        )
        if r1.returncode != 0:
            return False
        r2 = subprocess.run(
            [mux, "send-keys", "-t", pane_id, "Enter"],
            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="replace",
        )
        return r2.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return False


def resolve_session_pane(self_name: str) -> Optional[str]:
    """Resolve this cell's own agent pane.

    Convention: the tmux/psmux session hosting the cell's resident agent is
    named after the cell (`self_name`). `send-keys -t <session>` lands on the
    ACTIVE pane, which on a multi-pane cell can be a shell where an injected
    `/model ...` would run as a SHELL command — so this returns the pane-id
    ONLY when a claude/node pane is POSITIVELY identified, and None on ANY
    failure or no match. None → caller stays surface-only, NEVER injects."""
    mux = _mux()
    if mux is None:
        return None
    try:
        r = subprocess.run(
            [mux, "list-panes", "-t", self_name, "-F",
             "#{window_index} #{pane_index} #{pane_current_command}"],
            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] in ("claude", "node"):
                # >>> THIS IS A WORKAROUND FOR SOMEONE ELSE'S DEFECT, NOT A REPAIR
                # OF OUR OWN — psmux/psmux#569. <<< Stated here because the next
                # reader sees a verbose target construction, assumes it is
                # over-engineering, and simplifies it back to `parts[0]`. The
                # upstream fix (ambiguity detection) is not ours and may take a
                # while; this lands now and is under our control.
                #
                # RESOLVE FRESH BEFORE EACH USE — NEVER CACHE THE RETURNED TARGET.
                # `pane_index` is POSITIONAL (psmux format.rs:1294, fmt_pane_pos +
                # pane_base_index) with no pane equivalent of renumber-windows, so
                # killing a pane RESHUFFLES the indices of its siblings. That is the
                # trade this fix makes and it must not be silent: %N was ambiguous
                # but STABLE; session:window.pane is unambiguous but POSITIONAL.
                # Caching it converts a fixed cross-session bug into an intermittent
                # wrong-pane one. (gpu-wsl; behaviourally UNVERIFIED — the kill-pane
                # test needs a box with two live cells, and running a destructive
                # command to prove targeting is unreliable is the wrong order.)
                #
                # >>> FULLY-QUALIFIED NAME, NOT A PANE-ID. <<< This returned
                # `#{pane_id}` (%N) until 2026-08-12. On real tmux %N is unique per
                # SERVER — that is the entire point of id targeting. PSMUX ALLOCATES
                # IDS PER SESSION, so %1 exists in every session, and `list-panes -a`
                # reports both co-resident sessions as paneid=%1 winid=@1.
                #
                # The list-panes call above is correctly scoped to this cell's own
                # session. The id it RETURNED was then used UNSCOPED by capture-pane
                # and send-keys — so on a multi-session psmux box probe_pane could
                # read ANOTHER CELL'S SCREEN and inject() could deliver a DM INTO
                # ANOTHER CELL'S PANE. Silently, exit 0.
                #
                # AND IT IS NOT DETERMINISTIC BY SORT ORDER — measured: `-t %1`
                # resolves to whatever the CURRENT ROUTING DEFAULT is, so one target
                # gives two answers depending on ambient context. An intermittent
                # cross-cell misdelivery, not a stable one.
                #
                # A BARE SESSION NAME WOULD NOT DO: `send-keys -t <session>` lands on
                # the ACTIVE pane, which on a multi-pane cell can be a SHELL where an
                # injected "/model ..." RUNS AS A SHELL COMMAND. So the positive
                # claude/node identification is kept and only the TARGET FORM changes.
                #
                # VERIFIED ADVERSARIALLY by gpu-wsl with `psmux display-message -p -t`
                # (a side-effect-free resolution query): with the routing default
                # PINNED AT THE NEIGHBOURING SESSION, the fully-qualified form still
                # resolved to its own session, 4/4. Immune to routing context.
                return f"{self_name}:{parts[0]}.{parts[1]}"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None
    return None
