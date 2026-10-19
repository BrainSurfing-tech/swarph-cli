"""card #1221 — quota_sync reads a real Claude Code /usage display.

Source: Claude Code /usage, run by the commander in the lab-ovh Claude
Code session. local-command-stdout at 2026-10-19 22:42Z. The text below
is that display, verbatim.

Kept windows, in order: Current session (the 5h window) and Current week
(all models). Current week (Fable) is the same weekly reset for one model
and is not sent. On the previous head this display parses as None.
"""
import logging
from datetime import datetime, timezone

from swarph_cli import quota_sync as qs

DOT = "\u00b7"

CAPTURED_AT = datetime(2026, 10, 19, 22, 42, tzinfo=timezone.utc)
AS_OF = CAPTURED_AT.isoformat()
SESSION_RESET = "2026-10-20T02:49:00+00:00"
WEEKLY_RESET = "2026-10-26T16:59:00+00:00"

USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 0% used · resets Oct 20, 2:49am (UTC)
Current week (all models): 1% used · resets Oct 26, 4:59pm (UTC)
Current week (Fable): 0% used · resets Oct 26, 4:59pm (UTC)
"""


def test_usage_display_sends_the_session_and_the_weekly_window():
    record = qs.parse_pane_capture(USAGE, AS_OF)
    assert record is not None
    assert record["windows"] == [
        {"used_pct": 0.0, "resets_at": SESSION_RESET, "as_of": AS_OF},
        {"used_pct": 1.0, "resets_at": WEEKLY_RESET, "as_of": AS_OF},
    ]
    assert "Fable" not in str(record["windows"])
    body = qs.decide_put(None, record, CAPTURED_AT)
    assert body is not None
    assert body["quota_windows"] == record["windows"]
    assert body["quota_as_of"] == AS_OF


def _session(pct: int, when: str) -> str:
    return f"Current session: {pct}% used {DOT} resets {when} (UTC)\n"


def _weekly(pct: int, when: str) -> str:
    return (
        f"Current week (all models): {pct}% used {DOT} resets {when} (UTC)\n"
    )


def _lines(session_when: str, weekly_when: str) -> str:
    return _session(0, session_when) + _weekly(1, weekly_when)


def test_hour_only_and_minute_resets_both_parse():
    record = qs.parse_pane_capture(
        _lines("Oct 20, 4:59pm", "Oct 26, 5pm"), AS_OF)
    assert record is not None and len(record["windows"]) == 2
    assert record["windows"][0]["resets_at"] == "2026-10-20T16:59:00+00:00"
    assert record["windows"][1]["resets_at"] == "2026-10-26T17:00:00+00:00"


def test_session_reset_without_a_date_is_the_next_one_after_as_of():
    record = qs.parse_pane_capture(_lines("2:49am", "Oct 26, 4:59pm"), AS_OF)
    assert record is not None and len(record["windows"]) == 2
    assert record["windows"][0]["resets_at"] == "2026-10-20T02:49:00+00:00"
    later = "2026-10-20T03:00:00+00:00"
    after = qs.parse_pane_capture(_lines("2:49am", "Oct 26, 4:59pm"), later)
    assert after is not None
    assert after["windows"][0]["resets_at"] == "2026-10-21T02:49:00+00:00"


def test_one_parsed_window_sends_no_quota_windows(caplog):
    caplog.set_level(logging.WARNING, logger="swarph_cli.quota_sync")
    record = qs.parse_pane_capture(_lines("Oct 20, 2:49am", "not-a-time"), AS_OF)
    assert record is None or "windows" not in record
    body = qs.decide_put(None, record, CAPTURED_AT)
    assert body is None or "quota_windows" not in body
    assert any(
        "sending no quota_windows" in rec.message for rec in caplog.records)
    scalar = qs.decide_put(
        None,
        {"used_pct": 40.0, "credits": None, "reset_at": None,
         "blocked": False, "source": "pane", "as_of": AS_OF},
        CAPTURED_AT)
    assert scalar is not None
    assert "quota_windows" not in scalar
    assert scalar["quota_used_pct"] == 40.0


def test_two_session_lines_and_no_weekly_send_no_quota_windows():
    text = _session(0, "Oct 20, 2:49am") + _session(5, "Oct 20, 3:49am")
    record = qs.parse_pane_capture(text, AS_OF)
    assert record is None or "windows" not in record
    body = qs.decide_put(None, record, CAPTURED_AT)
    assert body is None or "quota_windows" not in body


def test_newer_display_replaces_the_older_one_on_the_same_capture():
    older = _session(10, "Oct 20, 1:00am") + _weekly(20, "Oct 26, 3:00pm")
    newer = _session(0, "Oct 20, 2:49am") + _weekly(1, "Oct 26, 4:59pm")
    record = qs.parse_pane_capture(older + newer, AS_OF)
    assert record is not None and len(record["windows"]) == 2
    assert record["windows"] == [
        {"used_pct": 0.0, "resets_at": "2026-10-20T02:49:00+00:00", "as_of": AS_OF},
        {"used_pct": 1.0, "resets_at": WEEKLY_RESET, "as_of": AS_OF},
    ]
    body = qs.decide_put(None, record, CAPTURED_AT)
    assert body is not None
    assert body["quota_windows"] == record["windows"]
