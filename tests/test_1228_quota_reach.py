"""card #1228 F2 — the pane reader has to be able to see a scrolled /usage.

The display is the verbatim Claude Code /usage stdout from card #1221:
the commander, in the lab-ovh Claude Code session, local-command-stdout
at 2026-10-19 22:42Z. It is not a transcription. A visible-only
capture-pane never includes that display once it has scrolled off, so
the reader asks tmux for the whole history.
"""
import subprocess

from swarph_cli.commands import roster
from tests.test_1221_quota_windows import (
    AS_OF,
    CAPTURED_AT,
    SESSION_RESET,
    USAGE,
    WEEKLY_RESET,
)
from swarph_cli import quota_sync as qs


def test_capture_asks_for_scrollback_and_the_stdout_display_is_what_sync_sends(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))

        class Result:
            returncode = 0
            stdout = USAGE

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    text = roster._capture_peer_pane("lab-ovh")
    assert calls == [["tmux", "capture-pane", "-p", "-S", "-", "-t", "lab-ovh"]]
    assert text == USAGE

    puts = []
    summary = qs.sync_once(
        peers=["lab-ovh"],
        capture=lambda peer: text,
        get_roster=lambda: [],
        put_roster=lambda peer, body: puts.append((peer, body)),
        send_alert=lambda peer, text: None,
        now=CAPTURED_AT,
    )
    assert summary["peers"]["lab-ovh"]["put"] is True
    assert puts[0][0] == "lab-ovh"
    assert puts[0][1]["quota_windows"] == [
        {"used_pct": 0.0, "resets_at": SESSION_RESET, "as_of": AS_OF},
        {"used_pct": 1.0, "resets_at": WEEKLY_RESET, "as_of": AS_OF},
    ]
