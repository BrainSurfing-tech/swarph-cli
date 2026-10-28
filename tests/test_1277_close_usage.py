"""card #1277 — one closing session, TokenUsage fields, USD when priced.

Red at eeb9d745: two session files and no session id sum to the combined total.
"""
import json
from pathlib import Path

from swarph_shared.llm_cost import PriceRow

from swarph_cli.commands import board
from swarph_cli.commands.board_cost import format_cost
from swarph_cli.commands.board_usage import read_close_usage


def _line(ts, tokens_in, cache_read, tokens_out, thinking, *,
          one_h=None, five=None, flat=None, model="claude-sonnet-5"):
    usage = {
        "input_tokens": tokens_in,
        "cache_read_input_tokens": cache_read,
        "output_tokens": tokens_out,
        "output_tokens_details": {"thinking_tokens": thinking},
    }
    if one_h is not None or five is not None:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": one_h,
            "ephemeral_5m_input_tokens": five,
        }
    elif flat is not None:
        usage["cache_creation_input_tokens"] = flat
    return json.dumps({
        "timestamp": ts,
        "message": {"model": model, "usage": usage},
    })


def _layout(tmp_path, files):
    home = tmp_path / "home"
    root = home / ".claude" / "projects" / "-work-cell"
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text)
    return {
        "HOME": str(home),
        "PWD": "/work/cell",
        "SWARPH_USAGE_TAKEN_AT": "2026-10-28T00:00:00+00:00",
        "SWARPH_USAGE_NOW": "2026-10-28T12:00:00+00:00",
        "CLAUDECODE": "1",
    }


def test_two_session_files_and_no_id_are_not_summed(tmp_path):
    env = _layout(tmp_path, {
        "own.jsonl": _line("2026-10-28T01:00:00Z", 100, 1, 1, 1, one_h=1, five=1) + "\n",
        "other.jsonl": _line("2026-10-28T01:00:00Z", 1000, 1, 1, 1, one_h=1, five=1) + "\n",
    })
    usage = read_close_usage(agent="cursor-lin", env=env,
                             taken_at=env["SWARPH_USAGE_TAKEN_AT"],
                             now=env["SWARPH_USAGE_NOW"])
    got = usage.get("input_tokens", usage.get("tokens_in"))
    # eeb9d745 returns 1100, the sum of both files.
    assert got != 1100
    assert got in ("unknown", 100)


def test_a_pinned_session_id_sums_only_that_file(tmp_path):
    env = _layout(tmp_path, {
        "own-session.jsonl": _line("2026-10-28T01:00:00Z", 100, 4, 3, 2, one_h=5, five=6) + "\n",
        "other.jsonl": _line("2026-10-28T01:00:00Z", 1000, 1, 1, 1, one_h=1, five=1) + "\n",
    })
    cells = tmp_path / "xdg" / "swarph" / "cells"
    cells.mkdir(parents=True)
    (cells / "cursor-lin.yaml").write_text("session_id: own-session\n")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    usage = read_close_usage(agent="cursor-lin", env=env,
                             taken_at=env["SWARPH_USAGE_TAKEN_AT"],
                             now=env["SWARPH_USAGE_NOW"])
    assert usage["input_tokens"] == 100
    assert usage["cache_creation_1h_tokens"] == 5
    assert usage["cache_creation_5m_tokens"] == 6
    assert usage["cache_creation_unsplit_tokens"] == "unknown"
    assert usage["thinking"] == 2
    assert usage["cache_read_tokens"] == 4


def test_a_session_env_sums_only_that_file_and_keeps_the_split(tmp_path, monkeypatch):
    env = _layout(tmp_path, {
        "own-session.jsonl": "\n".join([
            _line("2026-10-27T01:00:00Z", 100, 100, 100, 100, one_h=100, five=100),
            _line("2026-10-28T01:00:00Z", 2, 9, 3, 4, one_h=5, five=1),
            _line("2026-10-28T02:00:00Z", 1, 8, 1, 2, one_h=0, five=0),
        ]) + "\n",
        "other.jsonl": _line("2026-10-28T01:00:00Z", 1000, 1, 1, 1, one_h=1, five=1) + "\n",
    })
    env["CLAUDE_SESSION_ID"] = "own-session"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("SWARPH_LANE_USAGE", raising=False)
    payload = board._close_payload("pass", "saw the row", None)
    usage = payload.get("usage") or {}
    assert usage.get("input_tokens") == 3
    assert usage.get("cache_read_tokens") == 17
    assert usage.get("cache_creation_1h_tokens") == 5
    assert usage.get("cache_creation_5m_tokens") == 1
    assert usage.get("cache_creation_unsplit_tokens") == "unknown"
    assert usage.get("thinking") == 6
    assert usage.get("output_tokens") == 4


def test_a_missing_cache_read_stays_unknown(tmp_path):
    env = _layout(tmp_path, {
        "own-session.jsonl": _line("2026-10-28T01:00:00Z", 2, None, 3, 4, one_h=5, five=1) + "\n",
    })
    # cache_read None omits the key. The line helper always writes the key.
    text = _line("2026-10-28T01:00:00Z", 2, 0, 3, 4, one_h=5, five=1)
    rec = json.loads(text)
    del rec["message"]["usage"]["cache_read_input_tokens"]
    home = env["HOME"]
    path = Path(home) / ".claude" / "projects" / "-work-cell" / "own-session.jsonl"
    path.write_text(json.dumps(rec) + "\n")
    env["CLAUDE_SESSION_ID"] = "own-session"
    usage = read_close_usage(agent="lab-ovh", env=env,
                             taken_at=env["SWARPH_USAGE_TAKEN_AT"],
                             now=env["SWARPH_USAGE_NOW"])
    assert usage["cache_read_tokens"] == "unknown"
    assert usage["input_tokens"] == 2
    assert usage["thinking"] == 4
    assert usage["cache_read_tokens"] != 0


def test_without_a_session_every_token_field_is_unknown():
    usage = read_close_usage(
        agent="cursor-lin",
        taken_at="2026-10-28T00:00:00+00:00",
        now="2026-10-28T12:00:00+00:00",
        env={"HOME": "/no/such", "PWD": "/no/such"},
    )
    assert usage["input_tokens"] == "unknown"
    assert usage["model"] == "unknown"
    assert usage["cache_read_tokens"] == "unknown"
    assert usage["thinking"] == "unknown"
    assert usage["cache_creation_1h_tokens"] == "unknown"


def test_a_lane_line_names_the_model_and_does_not_invent_tokens():
    line = ('CURSOR_LANE_USAGE {"ts":"2026-10-28T01:00:00Z",'
            '"model":"grok-4.7-high-fast","duration_ms":1200}')
    usage = read_close_usage(
        agent="cursor-lin",
        taken_at="2026-10-28T00:00:00+00:00",
        now="2026-10-28T12:00:00+00:00",
        env={"SWARPH_LANE_USAGE": line, "HOME": "/no/such", "PWD": "/no/such"},
    )
    assert usage["model"] == "grok-4.7-high-fast"
    assert usage["input_tokens"] == "unknown"
    assert usage["output_tokens"] == "unknown"
    assert usage["thinking"] == "unknown"


def _known(**fields):
    base = {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_creation_1h_tokens": 0, "cache_creation_5m_tokens": 0,
        "cache_creation_unsplit_tokens": "unknown", "thinking": 0,
        "model": "claude-sonnet-5",
    }
    base.update(fields)
    return base


def test_cost_on_a_card_with_a_fail_and_a_rework():
    rates = {"claude-sonnet-5": PriceRow(
        input=1.0, output=1.0, cache_read=1.0,
        cache_creation=1.0, cache_creation_above_1hr=1.0)}
    report = {
        "window": {"card": 10, "since": "2026-10-28", "until": "2026-10-28T16:00:00+00:00"},
        "later_fix": {"10": [12]},
        "closes": [
            {"card_id": 10, "step": "build", "outcome": "fail", "rework": False,
             "stage": "done", "usage": _known(
                 input_tokens=100, output_tokens=20, cache_read_tokens=5,
                 cache_creation_1h_tokens=3, cache_creation_5m_tokens=2, thinking=0)},
            {"card_id": 10, "step": "build", "outcome": "pass", "rework": True,
             "stage": "done", "usage": _known(
                 input_tokens=10, output_tokens=10, cache_read_tokens=10,
                 cache_creation_1h_tokens=10, cache_creation_5m_tokens=10, thinking=10)},
            {"card_id": 10, "step": "validate", "outcome": "pass", "rework": False,
             "stage": "done", "usage": _known(
                 input_tokens=4, output_tokens=3, cache_read_tokens=2,
                 cache_creation_1h_tokens=1, cache_creation_5m_tokens=0, thinking=0)},
        ],
    }
    text = format_cost(report, rates)
    assert "execute: 130 tokens" in text
    assert "grade: 10 tokens" in text
    assert "advise: 0 tokens" in text
    assert "rework: 60 tokens" in text
    assert "FAIL: 1" in text
    assert "reworks: 1" in text
    assert "perfect-run rate: 0/1" in text
    assert "cost per accepted outcome: 200/1" in text
    assert "cost per perfect run: no perfect runs (0)" in text
    assert "unknown rows: 0" in text
    assert "later-fix=#12" in text
    assert "window: card #10 since 2026-10-28" in text
    assert "usd: 190" in text


def test_an_unknown_row_is_excluded_from_the_cost_not_counted_as_zero():
    report = {
        "window": {"card": 10, "since": None, "until": "2026-10-28T16:00:00+00:00"},
        "later_fix": {},
        "closes": [
            {"card_id": 10, "step": "build", "outcome": "pass", "rework": False,
             "stage": "done", "usage": _known(input_tokens="unknown")},
        ],
    }
    text = format_cost(report)
    assert "unknown rows: 1" in text
    assert "cost per accepted outcome: unknown" in text
    assert "perfect-run rate: 1/1" in text
    assert "usd: unknown" in text
    assert "0 tokens/card" not in text


def test_cost_command_parses():
    try:
        args = board._build_parser().parse_args(["cost", "--card", "10", "--since", "2026-10-28"])
    except SystemExit:
        args = None
    assert getattr(args, "group", None) == "cost"
    assert getattr(args, "card", None) == 10
