"""Card #935. The mint line names two different clocks and must not swap them.

--timeout-hours is when the row READS OVERDUE. --hours (or the menu step's
hours, when no override is passed) is the due-date clock, and it starts when
the row is taken, not when it is minted. A requested build with
--timeout-hours 240 and no --hours is due in 48h from take. Today's printer
never says that, and the function whose docstring claims to name the deadline
prints the overdue timestamp in that slot instead.
"""
from __future__ import annotations

from swarph_cli.commands import board


def _requested_build():
    """What a live mint returns once the gateway sends the clock fields:
    menu 48h, overdue set from --timeout-hours 240, no hours override."""
    return {
        "id": 316, "card_id": 498, "holder": None, "status": "open",
        "state": "requested", "step": "build",
        "timeout_at": "2026-10-13T06:28:07Z",
        "clock_hours": 48, "clock_source": "menu", "menu_hours": 48,
        "clock_anchor": "take",
        "eligible": ["friendly-coder"], "menu_version": 3,
        "menu_source": "fleet",
        "take_with": "swarph board obligations take 316",
        "accept": "PASS = the line names the menu hours from take | FAIL = it names the overdue hours",
    }


def test_requested_row_names_48h_from_take_and_not_the_overdue_hours():
    out = board._ask_line(_requested_build())
    assert "clock: 48h (menu) — STARTS ON TAKE, not now" in out
    assert "240" not in out
    assert "overdue after 2026-10-13T06:28:07Z" in out
    # the overdue stamp is not standing in for the deadline
    assert "owes it, status=open, overdue after" not in out


def test_override_names_its_multiple_of_the_menu():
    d = _requested_build()
    d.update(state="offered", holder="friendly-coder", eligible=None,
             clock_hours=192, clock_source="override")
    out = board._ask_line(d)
    assert "clock: 192h (override, 4x menu 48h) — STARTS ON TAKE, not now" in out
    assert "240" not in out


def test_format_ask_names_the_clock_when_the_gateway_sends_it():
    d = _requested_build()
    d["state"] = None  # the pre-#591 printer
    out = board._format_ask(d)
    assert "clock: 48h (menu) — STARTS ON TAKE, not now" in out
    assert "overdue after 2026-10-13T06:28:07Z" in out
    assert "240" not in out
