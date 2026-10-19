"""card #1221 — quota_sync reads a real Claude Code /usage display.

Source: Claude Code /usage, run by the commander in the lab-ovh Claude
Code session. local-command-stdout at 2026-10-19 22:42Z. The text below
is that display, verbatim.

Kept windows, in order: Current session (the 5h window) and Current week
(all models). Current week (Fable) is the same weekly reset for one model
and is not sent. On the previous head this display parses as None.
"""
from datetime import datetime, timezone

from swarph_cli import quota_sync as qs

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
