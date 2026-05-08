"""Parser for Claude CLI session JSONL files.

Source format: ``~/.claude/projects/<workspace>/<session-id>.jsonl``,
one JSON object per line. Records have a ``"type"`` discriminator;
the ones we care about are ``"user"``, ``"assistant"``, and
``"system"``. Other types (``permission-mode``, ``file-history-
snapshot``, ``queue-operation``, ``attachment``) are internal state
that doesn't translate into the conversation turn-stream — counted
in the report but skipped in the message output.

Per PLAN.md §17.3, what ports cleanly:
- Plain user/assistant text
- Role tags (uniform across providers)
- Conversation order

What's lossy:
- ``thinking`` blocks (visible in raw Anthropic API; not portable
  to other providers — counted, kept as preamble text on the
  assistant turn so the user can see them)
- ``tool_use`` blocks (the call shape doesn't translate; we keep
  any companion text and note the dropped tool calls in the report)
- ``tool_result`` blocks (likewise — kept as text, dropped as
  structured invocations)

What doesn't port at all (counted in report, NOT included in messages):
- ``attachment`` records (file uploads — provider-side, would
  need re-upload)
- Any ``cache_control`` annotations (Anthropic-specific)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from swarph_mesh import ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    """Per-source-file accounting of what was found, kept, and dropped.

    Printed by ``--report-only`` mode and embedded in the
    swarph-native session metadata when a write happens.
    """

    source_path: str
    source_format: str = "claude"
    session_id: Optional[str] = None
    model_seen: Optional[str] = None  # primary model used in the session

    # Counts — kept and dropped both populated for honest framing
    user_turns: int = 0
    assistant_turns: int = 0
    system_messages: int = 0
    tool_use_blocks: int = 0
    tool_result_blocks: int = 0
    thinking_blocks: int = 0
    attachment_records: int = 0
    other_records: int = 0  # permission-mode, file-history-snapshot, etc.

    # Aggregate text size
    total_chars: int = 0

    # Per-line parse failures (don't kill the import; record + continue)
    parse_errors: int = 0

    # Honest framing — what would be lost on import
    notes: list[str] = field(default_factory=list)

    def total_kept_turns(self) -> int:
        return self.user_turns + self.assistant_turns + self.system_messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_format": self.source_format,
            "session_id": self.session_id,
            "model_seen": self.model_seen,
            "user_turns": self.user_turns,
            "assistant_turns": self.assistant_turns,
            "system_messages": self.system_messages,
            "tool_use_blocks": self.tool_use_blocks,
            "tool_result_blocks": self.tool_result_blocks,
            "thinking_blocks": self.thinking_blocks,
            "attachment_records": self.attachment_records,
            "other_records": self.other_records,
            "total_chars": self.total_chars,
            "parse_errors": self.parse_errors,
            "notes": list(self.notes),
        }

    def render_human(self) -> str:
        """Operator-readable report for ``--report-only`` stdout."""
        lines = [
            f"swarph import — Claude session report",
            f"  source: {self.source_path}",
            f"  format: {self.source_format}",
        ]
        if self.session_id:
            lines.append(f"  session-id: {self.session_id}")
        if self.model_seen:
            lines.append(f"  model: {self.model_seen}")
        lines.append("")
        lines.append("KEPT (will land in swarph-native session):")
        lines.append(f"  user turns:        {self.user_turns}")
        lines.append(f"  assistant turns:   {self.assistant_turns}")
        lines.append(f"  system messages:   {self.system_messages}")
        lines.append(f"  total chars:       {self.total_chars:,}")
        lines.append("")
        lines.append("DROPPED / LOSSY (not portable to other providers):")
        lines.append(f"  thinking blocks:   {self.thinking_blocks} (Anthropic-specific reasoning trace)")
        lines.append(f"  tool_use blocks:   {self.tool_use_blocks} (call shape doesn't port)")
        lines.append(f"  tool_result blks:  {self.tool_result_blocks} (companion drop with tool_use)")
        lines.append(f"  attachments:       {self.attachment_records} (file uploads, would need re-upload)")
        lines.append(f"  other records:     {self.other_records} (permission-mode, snapshots, etc.)")
        if self.parse_errors:
            lines.append(f"  parse errors:      {self.parse_errors} (skipped silently — see logs)")
        lines.append("")
        if self.notes:
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  • {n}")
            lines.append("")
        lines.append(
            "Honest framing: teleport is 'import + continue', NOT 'freeze and resume'. "
            "The first turn after import on a new provider pays cold-cache cost; vendor "
            "KV cache + attachments + tool-call shape don't port. See PLAN.md §17.3."
        )
        return "\n".join(lines)


@dataclass
class ImportResult:
    """Output of a parser run — the portable message stream + the report."""

    messages: list[ChatMessage]
    report: ImportReport
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Block extraction helpers
# ---------------------------------------------------------------------------


def _extract_text_from_content(
    content: Any, report: ImportReport
) -> str:
    """Pull plain text out of a Claude-style content field.

    Content can be:
    - str (simple turn) → return as-is
    - list of blocks → walk and concatenate text + thinking; count
      tool_use / tool_result / etc. as dropped/lossy.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                # Non-dict block — skip silently (defensive)
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text", "")))
            elif btype == "thinking":
                # Lossy — count but DO keep the text inline as
                # commentary so the imported session has the full
                # reasoning preamble. Other providers will see it as
                # plain text, which is the honest representation.
                report.thinking_blocks += 1
                t = block.get("thinking", "") or block.get("text", "")
                if t:
                    texts.append(f"[thinking]\n{t}\n[/thinking]")
            elif btype == "tool_use":
                report.tool_use_blocks += 1
                # Optionally surface the tool name + input as visible
                # text so the imported session retains decision context
                name = block.get("name", "?")
                texts.append(f"[tool_use {name} — dropped, structured invocation not ported]")
            elif btype == "tool_result":
                report.tool_result_blocks += 1
                # Keep result content as text since some downstream
                # may parse it; dropped as a structured shape.
                tr_content = block.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = " ".join(
                        str(b.get("text", "") if isinstance(b, dict) else b)
                        for b in tr_content
                    )
                texts.append(f"[tool_result]\n{tr_content}\n[/tool_result]")
            else:
                # Unknown block type — record + skip
                pass
        return "\n".join(t for t in texts if t)
    # Defensive fallback for unknown shapes
    return str(content)


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------


class ClaudeParser:
    """Parse a Claude CLI session JSONL into :class:`ImportResult`.

    Usage:
        result = ClaudeParser().parse(Path("~/.claude/projects/.../X.jsonl"))
        print(result.report.render_human())
        # if not --report-only:
        #   write swarph-native session from result.messages + result.report
    """

    SOURCE_FORMAT = "claude"

    def parse(self, path: Path) -> ImportResult:
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"source file not found: {path}")
        if not path.is_file():
            raise ValueError(f"source is not a regular file: {path}")

        report = ImportReport(source_path=str(path))
        messages: list[ChatMessage] = []
        first_session_id: Optional[str] = None
        models_seen: dict[str, int] = {}

        with path.open(encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    report.parse_errors += 1
                    logger.warning(
                        "claude parser: line %d JSON decode failed: %s",
                        line_no,
                        exc,
                    )
                    continue
                if not isinstance(rec, dict):
                    report.other_records += 1
                    continue

                # Capture session_id once (first record that has one)
                sid = rec.get("sessionId") or rec.get("session_id")
                if sid and not first_session_id:
                    first_session_id = sid

                rtype = rec.get("type")

                if rtype == "user":
                    msg = rec.get("message") or {}
                    text = _extract_text_from_content(
                        msg.get("content"), report
                    )
                    if text:
                        messages.append(ChatMessage(role="user", content=text))
                        report.user_turns += 1
                        report.total_chars += len(text)

                elif rtype == "assistant":
                    msg = rec.get("message") or {}
                    text = _extract_text_from_content(
                        msg.get("content"), report
                    )
                    model = msg.get("model")
                    if model:
                        models_seen[model] = models_seen.get(model, 0) + 1
                    if text:
                        messages.append(ChatMessage(role="assistant", content=text))
                        report.assistant_turns += 1
                        report.total_chars += len(text)

                elif rtype == "system":
                    msg = rec.get("message") or {}
                    text = _extract_text_from_content(
                        msg.get("content"), report
                    )
                    if not text:
                        # Some system records carry top-level "content"
                        text = _extract_text_from_content(
                            rec.get("content"), report
                        )
                    if text:
                        messages.append(ChatMessage(role="system", content=text))
                        report.system_messages += 1
                        report.total_chars += len(text)

                elif rtype == "attachment":
                    report.attachment_records += 1

                else:
                    # permission-mode, file-history-snapshot, queue-operation, etc.
                    report.other_records += 1

        report.session_id = first_session_id
        if models_seen:
            # Pick the most-used model as the session's primary
            report.model_seen = max(models_seen, key=models_seen.get)
            if len(models_seen) > 1:
                report.notes.append(
                    f"session used {len(models_seen)} models "
                    f"({', '.join(sorted(models_seen.keys()))}); "
                    f"primary={report.model_seen}"
                )

        if report.attachment_records > 0:
            report.notes.append(
                f"{report.attachment_records} attachment(s) skipped — "
                "re-upload required to make them addressable on the new provider"
            )
        if report.tool_use_blocks > 0:
            report.notes.append(
                f"{report.tool_use_blocks} tool_use block(s) preserved as "
                "visible text only; structured invocations not ported "
                "(see PLAN.md §17.3 documented loss)"
            )

        return ImportResult(
            messages=messages,
            report=report,
            metadata={
                "format": self.SOURCE_FORMAT,
                "session_id": first_session_id,
                "model": report.model_seen,
            },
        )
