"""Source-format parsers for ``swarph import``.

v0.1.x ships only the Claude parser (Phase 2.5 build target per
PLAN.md §17.6). Phase 3.5+ adds gemini-cli + Simon Willison ``llm``;
Phase 6+ adds chatgpt-export.

Each parser accepts a path and returns a normalized
:class:`ImportResult` containing:

  - ``messages``: list[swarph_mesh.ChatMessage] — portable turns
  - ``report``: ImportReport — what was kept, what was dropped, why
  - ``metadata``: dict — source format, original session id, model, etc.

Parsers are pure (no side effects beyond filesystem read). The
write step (swarph-native JSONL emission) lives separately so
``--report-only`` skips it cleanly.
"""

from __future__ import annotations

from swarph_cli.parsers.claude import (
    ClaudeParser,
    ImportReport,
    ImportResult,
)

__all__ = ["ClaudeParser", "ImportReport", "ImportResult"]
