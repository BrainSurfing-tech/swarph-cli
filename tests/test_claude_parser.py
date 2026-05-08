"""Tests for the Claude session JSONL parser.

Synthetic JSONL fixtures cover the canonical record types
(user/assistant/system/attachment/permission-mode) plus the
edge cases that bit during initial development:

  - assistant content as list of blocks (thinking + text + tool_use)
  - user content as plain string vs as list
  - tool_result blocks
  - parse errors mid-file (don't kill the import)
  - multiple model versions in one session
  - missing session_id
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.parsers.claude import (
    ClaudeParser,
    ImportReport,
    ImportResult,
    _extract_text_from_content,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Block extraction unit tests
# ---------------------------------------------------------------------------


def test_extract_text_from_plain_string():
    rep = ImportReport(source_path="x")
    assert _extract_text_from_content("hello", rep) == "hello"
    assert rep.thinking_blocks == 0
    assert rep.tool_use_blocks == 0


def test_extract_text_from_text_block_list():
    rep = ImportReport(source_path="x")
    blocks = [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]
    assert _extract_text_from_content(blocks, rep) == "hi\nthere"


def test_extract_text_counts_thinking_blocks():
    rep = ImportReport(source_path="x")
    blocks = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    out = _extract_text_from_content(blocks, rep)
    assert rep.thinking_blocks == 1
    assert "internal reasoning" in out
    assert "final answer" in out


def test_extract_text_counts_tool_use():
    rep = ImportReport(source_path="x")
    blocks = [
        {"type": "tool_use", "name": "search_web", "input": {"q": "foo"}},
        {"type": "text", "text": "found nothing"},
    ]
    out = _extract_text_from_content(blocks, rep)
    assert rep.tool_use_blocks == 1
    assert "search_web" in out
    assert "found nothing" in out


def test_extract_text_counts_tool_result():
    rep = ImportReport(source_path="x")
    blocks = [{"type": "tool_result", "content": "200 OK"}]
    out = _extract_text_from_content(blocks, rep)
    assert rep.tool_result_blocks == 1
    assert "200 OK" in out


def test_extract_text_handles_none():
    rep = ImportReport(source_path="x")
    assert _extract_text_from_content(None, rep) == ""


def test_extract_text_skips_unknown_block_types():
    rep = ImportReport(source_path="x")
    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "unknown_future_type", "data": "ignore me"},
    ]
    out = _extract_text_from_content(blocks, rep)
    assert "hello" in out
    assert "ignore me" not in out


# ---------------------------------------------------------------------------
# Top-level parse tests
# ---------------------------------------------------------------------------


def test_parse_canonical_session(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "S-1"},
        {"type": "user", "sessionId": "S-1",
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "sessionId": "S-1",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "hi back"}]}},
        {"type": "user", "sessionId": "S-1",
         "message": {"role": "user", "content": "what's 2+2"}},
        {"type": "assistant", "sessionId": "S-1",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "4"}]}},
    ])
    result = ClaudeParser().parse(src)

    assert isinstance(result, ImportResult)
    assert len(result.messages) == 4
    assert result.messages[0].role == "user"
    assert result.messages[0].content == "hello"
    assert result.messages[1].role == "assistant"
    assert result.messages[1].content == "hi back"
    assert result.report.user_turns == 2
    assert result.report.assistant_turns == 2
    assert result.report.system_messages == 0
    assert result.report.session_id == "S-1"
    assert result.report.model_seen == "claude-opus-4-7"
    assert result.report.other_records == 1  # the permission-mode record
    assert result.report.parse_errors == 0


def test_parse_counts_attachments(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user",
         "message": {"role": "user", "content": "see attached"}},
        {"type": "attachment", "filename": "report.pdf"},
        {"type": "attachment", "filename": "image.png"},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m1",
                     "content": [{"type": "text", "text": "got it"}]}},
    ])
    result = ClaudeParser().parse(src)
    assert result.report.attachment_records == 2
    # Attachments are NOT in messages — they're counted-and-dropped
    assert len(result.messages) == 2
    # And the report includes a note
    assert any("attachment" in n.lower() for n in result.report.notes)


def test_parse_counts_thinking_blocks(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user",
         "message": {"role": "user", "content": "solve"}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m1", "content": [
             {"type": "thinking", "thinking": "let me think"},
             {"type": "text", "text": "answer is 42"},
         ]}},
    ])
    result = ClaudeParser().parse(src)
    assert result.report.thinking_blocks == 1
    # Thinking block content is preserved as preamble text
    assert "let me think" in result.messages[1].content
    assert "answer is 42" in result.messages[1].content


def test_parse_counts_tool_use_with_dropped_note(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user",
         "message": {"role": "user", "content": "search for X"}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "m1", "content": [
             {"type": "tool_use", "name": "web_search", "input": {"q": "X"}},
         ]}},
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "found 3 results"},
         ]}},
    ])
    result = ClaudeParser().parse(src)
    assert result.report.tool_use_blocks == 1
    assert result.report.tool_result_blocks == 1
    # tool_use note is in the report
    assert any("tool_use" in n.lower() for n in result.report.notes)


def test_parse_handles_parse_errors_without_dying(tmp_path):
    """Bad JSON line in the middle is recorded but doesn't kill the parse."""
    src = tmp_path / "session.jsonl"
    src.write_text(
        json.dumps({"type": "user",
                    "message": {"role": "user", "content": "ok line"}}) + "\n"
        + "{bad json line\n"
        + json.dumps({"type": "assistant",
                      "message": {"role": "assistant", "model": "m1",
                                  "content": [{"type": "text", "text": "still ok"}]}}) + "\n",
        encoding="utf-8",
    )
    result = ClaudeParser().parse(src)
    assert result.report.parse_errors == 1
    assert result.report.user_turns == 1
    assert result.report.assistant_turns == 1
    assert len(result.messages) == 2


def test_parse_records_multiple_models_seen(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user",
         "message": {"role": "user", "content": "first"}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "a"}]}},
        {"type": "user",
         "message": {"role": "user", "content": "second"}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                     "content": [{"type": "text", "text": "b"}]}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                     "content": [{"type": "text", "text": "c"}]}},
    ])
    result = ClaudeParser().parse(src)
    # primary = most-used = sonnet
    assert result.report.model_seen == "claude-sonnet-4-6"
    # And the multi-model note is surfaced
    assert any("models" in n.lower() for n in result.report.notes)


def test_parse_handles_missing_session_id(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "no sessionid"}},
    ])
    result = ClaudeParser().parse(src)
    assert result.report.session_id is None
    assert result.report.user_turns == 1


def test_parse_skips_blank_lines(tmp_path):
    src = tmp_path / "session.jsonl"
    src.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n"
        + "\n"
        + "   \n"
        + json.dumps({"type": "assistant",
                      "message": {"role": "assistant", "model": "m1",
                                  "content": [{"type": "text", "text": "y"}]}}) + "\n",
        encoding="utf-8",
    )
    result = ClaudeParser().parse(src)
    assert result.report.parse_errors == 0
    assert len(result.messages) == 2


def test_parse_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ClaudeParser().parse(tmp_path / "does-not-exist.jsonl")


def test_parse_raises_on_directory(tmp_path):
    with pytest.raises(ValueError, match="not a regular file"):
        ClaudeParser().parse(tmp_path)


# ---------------------------------------------------------------------------
# ImportReport rendering
# ---------------------------------------------------------------------------


def test_report_render_human_includes_kept_dropped_sections(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "yo"}]}},
    ])
    result = ClaudeParser().parse(src)
    text = result.report.render_human()
    assert "KEPT" in text
    assert "DROPPED" in text
    assert "user turns:        1" in text
    assert "claude-opus-4-7" in text
    assert "Honest framing" in text  # the §17.3 reminder line


def test_report_to_dict_round_trips_via_json(tmp_path):
    src = _write_jsonl(tmp_path / "session.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "x"}},
    ])
    result = ClaudeParser().parse(src)
    d = result.report.to_dict()
    # Must be JSON-serializable for the swarph-native session header
    s = json.dumps(d)
    assert "user_turns" in s
    assert json.loads(s) == d


# ---------------------------------------------------------------------------
# Real Claude session smoke (skipped if no real sessions on this host)
# ---------------------------------------------------------------------------


def test_parse_real_claude_session_if_available():
    """Smoke test against an actual Claude JSONL on this host. Skipped
    on hosts without ~/.claude/projects."""
    candidates = list(Path.home().glob(".claude/projects/*/*.jsonl"))
    if not candidates:
        pytest.skip("no real Claude sessions on this host")
    # Use the smallest one to keep the test fast
    sample = min(candidates, key=lambda p: p.stat().st_size)
    if sample.stat().st_size > 5_000_000:
        pytest.skip("smallest claude session > 5MB — skipping smoke")
    result = ClaudeParser().parse(sample)
    # We don't assert specific counts (real sessions vary). Just
    # verify the parser doesn't crash + produces SOME structure.
    assert isinstance(result, ImportResult)
    # Real sessions usually have at least one turn — but the smallest
    # might be a tiny one. Loose assertion: parse_errors should be
    # near-zero (single-digit at most) on a real, non-corrupt file.
    assert result.report.parse_errors < 20
