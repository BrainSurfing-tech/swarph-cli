"""card #961 — the limit check must be the limit screen, not a quote.

These assertions fail on 1d55233: that head treats any line containing the
limit sentences as a grok block, including a cursor transcript and grok
scrollback above an idle screen.
"""
from pathlib import Path

from swarph_cli.commands import mesh

FIX723 = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "grok-723"
FIX726 = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "grok-726"


def lines(root, name):
    return (root / name).read_text(encoding="utf-8").splitlines()


def test_cursor_transcript_quoting_both_limits_is_not_blocked():
    pane = lines(FIX726, "cursor-transcript.txt")
    text = "\n".join(pane)
    assert "rate limit" in text
    assert "You hit your free usage limit" in text
    assert "Models matching" in text
    assert mesh._is_grok_pane(pane) is False
    assert mesh._grok_block_reason(pane) is None


def test_opencode_transcript_quoting_the_usage_limit_is_not_blocked():
    src = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "opencode-pane-idle-footer-0710.txt"
    pane = src.read_text(encoding="utf-8").splitlines()
    # Inside the current screen, on a ┃ transcript row, not a grok modal.
    pane.insert(len(pane) - 8, "  ┃  You hit your free usage limit")
    assert mesh._is_opencode_pane(pane) is True
    assert mesh._is_grok_pane(pane) is False
    assert mesh._grok_block_reason(pane) is None


def test_grok_rate_limit_and_usage_modal_still_defer():
    rate = lines(FIX723, "rate-limit.txt")
    usage = lines(FIX723, "usage-limit.txt")
    assert mesh._grok_block_reason(rate) == "grok-rate-limit"
    assert mesh._grok_turn_finished(rate) is False
    assert mesh._is_grok_pane(usage) is False
    assert mesh._grok_block_reason(usage) == "grok-usage-limit"


def test_old_scrollback_limit_on_an_idle_grok_screen_reads_idle():
    idle = lines(FIX723, "idle.txt")
    scroll = [
        "     ❯ an earlier turn",
        "     Retry failed: You’ve hit the rate limit for your plan.",
        "     You hit your free usage limit",
    ]
    # Push the quote above one screen. The idle composer stays.
    pane = (scroll * 20) + idle
    assert mesh._is_grok_pane(pane)
    assert mesh._grok_block_reason(pane) is None
    assert mesh._grok_composer_state(pane) == "clear"
    assert mesh._grok_running(pane) is False
