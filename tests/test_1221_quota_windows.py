"""card #1221 — quota_sync sends every provider window, not one scalar.

A reader that holds a 5h window and a weekly window must PUT both as
{used_pct, resets_at, as_of}. On main, decide_put drops the windows and
the assertion on quota_windows fails.
"""
from datetime import datetime, timezone

from swarph_cli import quota_sync as qs

NOW = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()
FIVE_H = "2026-10-19T11:30:00+00:00"
WEEKLY = "2026-10-26T06:30:00+00:00"

PANE = (
    f"quota window 5h: 10% used, resets {FIVE_H}\n"
    f"quota window weekly: 80% used, resets {WEEKLY}\n"
)


def test_reader_with_5h_and_weekly_sends_both_windows():
    record = qs.parse_pane_capture(PANE, AS_OF)
    assert record is not None
    body = qs.decide_put(None, record, NOW)
    assert body is not None
    assert body["quota_windows"] == [
        {"used_pct": 10.0, "resets_at": FIVE_H, "as_of": AS_OF},
        {"used_pct": 80.0, "resets_at": WEEKLY, "as_of": AS_OF},
    ]
    assert "quota_as_of" in body
