"""card #1277 — close reads a transcript, and cost is not an estimate.

Red at eab2a3f: the close payload has no usage, so cache_read is absent.
"""
import json

from swarph_cli.commands import board
from swarph_cli.commands.board_cost import format_cost
from swarph_cli.commands.board_usage import read_close_usage


def _line(ts, tokens_in, cache_read, cache_write, tokens_out, thinking, model="claude-sonnet-5"):
    usage = {
        "input_tokens": tokens_in,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": tokens_out,
        "output_tokens_details": {"thinking_tokens": thinking},
    }
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    return json.dumps({
        "timestamp": ts,
        "message": {"model": model, "usage": usage},
    })


def test_close_payload_carries_the_transcript_sum(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([
        _line("2026-10-27T01:00:00Z", 100, 100, 100, 100, 100),
        _line("2026-10-28T01:00:00Z", 2, 9, 5, 3, 4),
        _line("2026-10-28T02:00:00Z", 1, 8, 0, 1, 2),
    ]) + "\n")
    monkeypatch.setenv("SWARPH_CLAUDE_TRANSCRIPT", str(path))
    monkeypatch.setenv("SWARPH_USAGE_TAKEN_AT", "2026-10-28T00:00:00+00:00")
    monkeypatch.setenv("SWARPH_USAGE_NOW", "2026-10-28T12:00:00+00:00")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("SWARPH_LANE_USAGE", raising=False)
    payload = board._close_payload("pass", "saw the row", None)
    usage = payload.get("usage") or {}
    # eab2a3f returns no usage key, so both of these are None there.
    assert usage.get("cache_read") == 17
    assert usage.get("thinking") == 6
    assert usage.get("cache_write") == 5
    assert usage.get("tokens_in") == 3
    assert usage.get("tokens_out") == 4
    assert usage.get("model") == "claude-sonnet-5"


def test_a_missing_transcript_field_stays_unknown(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(_line("2026-10-28T01:00:00Z", 2, None, 5, 3, 4) + "\n")
    usage = read_close_usage(
        agent="lab-ovh",
        taken_at="2026-10-28T00:00:00+00:00",
        now="2026-10-28T12:00:00+00:00",
        env={"SWARPH_CLAUDE_TRANSCRIPT": str(path), "CLAUDECODE": "1"},
    )
    assert usage["cache_read"] == "unknown"
    assert usage["tokens_in"] == 2
    assert usage["thinking"] == 4
    assert usage["agent"] == "lab-ovh"


def test_without_a_transcript_every_token_field_is_unknown():
    usage = read_close_usage(
        agent="cursor-lin",
        taken_at="2026-10-28T00:00:00+00:00",
        now="2026-10-28T12:00:00+00:00",
        env={},
    )
    assert usage["tokens_in"] == "unknown"
    assert usage["model"] == "unknown"
    assert usage["cache_read"] == "unknown"
    assert usage["thinking"] == "unknown"


def test_a_lane_line_names_the_model_and_does_not_invent_tokens():
    line = ('CURSOR_LANE_USAGE {"ts":"2026-10-28T01:00:00Z",'
            '"model":"grok-4.7-high-fast","duration_ms":1200}')
    usage = read_close_usage(
        agent="cursor-lin",
        taken_at="2026-10-28T00:00:00+00:00",
        now="2026-10-28T12:00:00+00:00",
        env={"SWARPH_LANE_USAGE": line},
    )
    assert usage["model"] == "grok-4.7-high-fast"
    assert usage["tokens_in"] == "unknown"
    assert usage["tokens_out"] == "unknown"
    assert usage["thinking"] == "unknown"


def _known(tokens_in, tokens_out, cache_read, cache_write, thinking):
    return {
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cache_read": cache_read, "cache_write": cache_write,
        "thinking": thinking,
    }


def test_cost_on_a_card_with_a_fail_and_a_rework():
    report = {
        "window": {"card": 10, "since": "2026-10-28", "until": "2026-10-28T16:00:00+00:00"},
        "later_fix": {"10": [12]},
        "closes": [
            {"card_id": 10, "step": "build", "outcome": "fail", "rework": False,
             "stage": "done", "usage": _known(100, 20, 5, 5, 0)},
            {"card_id": 10, "step": "build", "outcome": "pass", "rework": True,
             "stage": "done", "usage": _known(10, 10, 10, 10, 10)},
            {"card_id": 10, "step": "validate", "outcome": "pass", "rework": False,
             "stage": "done", "usage": _known(4, 3, 2, 1, 0)},
        ],
    }
    text = format_cost(report)
    assert "execute: 130 tokens" in text
    assert "grade: 10 tokens" in text
    assert "advise: 0 tokens" in text
    assert "rework: 50 tokens" in text
    assert "FAIL: 1" in text
    assert "reworks: 1" in text
    assert "perfect-run rate: 0/1" in text
    assert "cost per accepted outcome: 190/1" in text
    assert "cost per perfect run: no perfect runs (0)" in text
    assert "unknown rows: 0" in text
    assert "later-fix=#12" in text
    assert "window: card #10 since 2026-10-28" in text


def test_an_unknown_row_is_excluded_from_the_cost_not_counted_as_zero():
    report = {
        "window": {"card": 10, "since": None, "until": "2026-10-28T16:00:00+00:00"},
        "later_fix": {},
        "closes": [
            {"card_id": 10, "step": "build", "outcome": "pass", "rework": False,
             "stage": "done", "usage": {"tokens_in": "unknown", "tokens_out": 1,
                                        "cache_read": 1, "cache_write": 1, "thinking": 1}},
        ],
    }
    text = format_cost(report)
    assert "unknown rows: 1" in text
    assert "cost per accepted outcome: unknown" in text
    assert "perfect-run rate: 1/1" in text
    assert "0 tokens/card" not in text


def test_cost_command_parses():
    try:
        args = board._build_parser().parse_args(["cost", "--card", "10", "--since", "2026-10-28"])
    except SystemExit:
        args = None
    assert getattr(args, "group", None) == "cost"
    assert getattr(args, "card", None) == 10
