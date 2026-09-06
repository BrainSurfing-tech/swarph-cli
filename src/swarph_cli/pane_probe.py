"""Per-provider pane idle predicates — daemon-reachable, spawn-free.

#682: session_bridge must classify a TUI pane without importing
``commands.spawn``. spawn.py carries unguarded ``print``s; pulling it into
the daemon import graph fails ``test_no_unguarded_print_anywhere_the_daemon_can_reach``.

This module is the single table. spawn membranes copy the tuples onto the
class for readers; ``pane_state`` here is the algorithm both sides use.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PanePredicate:
    busy_markers: tuple = ()
    composer_prefixes: tuple = ()
    empty_placeholders: tuple = ()
    modal_markers: tuple = ()

    def state(self, content: str) -> str:
        """idle | busy | modal | unknown.

        idle == no busy marker AND the input box is empty. The empty-input
        hint string ("? for shortcuts") is NOT this test: that hint
        vanishes under the exact condition we need to detect.
        """
        if not self.busy_markers and not self.composer_prefixes:
            return "unknown"
        low = content.lower()
        if any(m in low for m in self.modal_markers):
            return "modal"
        if any(m in low for m in self.busy_markers):
            return "busy"
        line = self._composer_line(content)
        if line is None:
            return "unknown"
        if self._composer_body(line):
            return "busy"
        return "idle"

    def _composer_line(self, content: str):
        found = None
        for raw in content.splitlines():
            s = raw.strip()
            for prefix in self.composer_prefixes:
                if s.startswith(prefix):
                    found = s
                    break
        return found

    def _composer_body(self, line: str) -> str:
        for prefix in self.composer_prefixes:
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                for ph in self.empty_placeholders:
                    if body.lower().startswith(ph.lower()):
                        return ""
                return body
        return line.strip()


# Claude TUI is also muse (MuseMembrane subclasses ClaudeMembrane).
_CLAUDE = PanePredicate(
    busy_markers=("esc to interrupt",),
    composer_prefixes=(">",),
    modal_markers=("how is claude doing this session",),
)
_CODEX = PanePredicate(
    busy_markers=("esc to interrupt",),
    composer_prefixes=("›",),
)
_CURSOR = PanePredicate(
    busy_markers=("ctrl+c to stop",),
    composer_prefixes=("→",),
    empty_placeholders=("add a follow-up",),
)

PANE_PREDICATES: dict[str, PanePredicate] = {
    "claude": _CLAUDE,
    "muse": _CLAUDE,
    "codex": _CODEX,
    "cursor": _CURSOR,
}


def pane_state(provider: Optional[str], content: str) -> str:
    """Classify captured pane text. Unknown / missing provider → unknown."""
    if not provider:
        return "unknown"
    pred = PANE_PREDICATES.get(provider)
    if pred is None:
        return "unknown"
    return pred.state(content)
