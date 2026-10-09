"""#1052 — opencode wake DEAF 32h: _opencode_running read a FINISHED
'▣ ... · 4h 59m' header as a live turn (the done-regex needs trailing
seconds), so the monitor deferred forever, logging 'composer holds human
text' on a CLEAR composer.

Seams: the pure helpers on the two real captures (lab-ovh's 01:31Z and
17:18Z fixtures), plus the TmuxSink deferral rigged at the deliver()
boundary exactly like test_1010_strand_only's. Items: (1) the done-regex
accepts h/m-only durations; (2) only the LAST ▣ header judges FINISHED;
(3) the opencode-in-progress deferral names its real reason.

Card #989 (2026-10-09) redefines liveness itself: only the 'esc
interrupt' footer judges a RUNNING turn. A duration-less header with no
interrupt hint is an aborted turn, not a live one.

The committed fixtures are the real captures with one customer
identifier redacted (a 6-letter tenant prefix -> acme, 7 spots each);
headers, composer state and control lines are byte-identical to the
originals.
"""
from __future__ import annotations

import pathlib

import pytest

import swarph_cli.commands.mesh as mesh

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


def _lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").splitlines()


CAPTURES = ("opencode-deaf-0131Z.txt", "opencode-deaf-1718Z.txt")


@pytest.mark.parametrize("name", CAPTURES)
def test_finished_captures_are_not_running(name: str):
    """Both real captures: the old scrollback '4h 59m' header is finished
    and the newest '16.5s' header is finished — no live turn anywhere."""
    lines = _lines(name)
    assert any("4h 59m" in ln for ln in lines), "capture must hold the old header"
    assert mesh._opencode_running(lines) is False
    assert mesh._opencode_turn_finished(lines) is True


def test_aborted_turn_pane_without_interrupt_is_not_running():
    """Card #989, live case 2026-10-09 17:05Z: an ABORTED/ERRORED turn
    prints its ▣ header and never adds a duration or the 'esc interrupt'
    footer. The old last-header rule read it as running forever (60
    deferred ticks); only the interrupt hint judges a live turn."""
    lines = ["▣  Build · DeepSeek V4 Pro (New)",
             "┃",
             "┃  Build · DeepSeek V4 Pro (New) OpenCode Go"]
    assert mesh._opencode_running(lines) is False
    assert mesh._opencode_turn_finished(lines) is False


FIRST_FRAME = [
    "▣  Build · DeepSeek V4 Pro (New)",
    "┃",
    "┃  Build · DeepSeek V4 Pro (New) OpenCode Go",
]
T0 = 1_790_000_000.0


def test_first_frame_defers_on_first_poll(monkeypatch):
    """(a) #1428: bare header, no hint, first sight. The stateless guard
    reads idle, but deliver defers via maybe-starting — and the wake
    stays owed with the fingerprint recorded in the ledger."""
    monkeypatch.setattr(mesh, "_capture_pane_lines",
                        lambda t: list(FIRST_FRAME))
    monkeypatch.setattr(mesh, "_agent_running", lambda t: False)
    state = _StubState()
    led = state.ledger("tmux:opencode")
    led["wake_outstanding"] = True
    sink = mesh.TmuxSink("opencode")
    assert sink.deliver(state, [{"id": 7}], 7) is None
    assert sink.deferred_reason == "opencode maybe starting"
    assert led.get("wake_outstanding") is True
    record = led.get("opencode_maybe_start")
    assert isinstance(record, dict)
    assert isinstance(record.get("first_seen"), float)
    assert record.get("fingerprint") == mesh._opencode_pane_fingerprint(
        FIRST_FRAME)


def test_aborted_pane_idle_on_unchanged_second_poll():
    """(b) #1428: today's aborted pane defers on first sight, then reads
    IDLE on the next poll when unchanged past the grace — the wake is
    delivered within about 2 polls."""
    verdict, _fp, record = mesh._opencode_poll_verdict(
        FIRST_FRAME, T0, None)
    assert verdict == "maybe-starting"
    verdict2, _fp2, record2 = mesh._opencode_poll_verdict(
        FIRST_FRAME, T0 + 61, record)
    assert (verdict2, record2) == ("idle", None)


def test_progressing_pane_keeps_deferring():
    """(c) #1428: a pane that changes between polls (turn progressing,
    footer still not rendered) re-arms maybe-starting with a fresh
    first_seen — it never matures to idle while moving."""
    _v, _fp, record = mesh._opencode_poll_verdict(FIRST_FRAME, T0, None)
    moved = list(FIRST_FRAME) + ["┃  working…"]
    verdict, _fp2, record2 = mesh._opencode_poll_verdict(moved, T0 + 61,
                                                        record)
    assert verdict == "maybe-starting"
    assert record2["first_seen"] == T0 + 61


def test_interrupt_hint_is_running_at_poll_level():
    """(d) #1428: esc interrupt = running at the verdict level too, with
    no record to persist."""
    lines = list(FIRST_FRAME) + ["  esc interrupt"]
    verdict, _fp, record = mesh._opencode_poll_verdict(lines, T0, None)
    assert (verdict, record) == ("running", None)


def test_poll_verdict_keeps_1052_fixture_verdicts():
    """(e) #1428: the #1052 captures (finished headers, no hint) read
    idle at the verdict level — no behavior change for finished panes."""
    for name in CAPTURES:
        verdict, _fp, record = mesh._opencode_poll_verdict(
            _lines(name), T0, None)
        assert (verdict, record) == ("idle", None)


def test_maybe_start_record_survives_a_restart():
    """The fingerprint record must live in ledgers.json, not module
    memory: prove it round-trips through JSON and still matures."""
    import json
    _v, _fp, record = mesh._opencode_poll_verdict(FIRST_FRAME, T0, None)
    revived = json.loads(json.dumps({"opencode_maybe_start": record}))
    verdict, _fp2, _r2 = mesh._opencode_poll_verdict(
        FIRST_FRAME, T0 + 61, revived["opencode_maybe_start"])
    assert verdict == "idle"


def test_corrupt_prior_reads_as_first_sight_never_idle():
    """A corrupt ledger entry must defer, never silently idle."""
    for bad in ({"fingerprint": "bogus", "first_seen": T0},
                {"fingerprint": mesh._opencode_pane_fingerprint(
                    FIRST_FRAME), "first_seen": "soon"},
                "garbage", None):
        verdict, _fp, _r = mesh._opencode_poll_verdict(
            FIRST_FRAME, T0, bad)
        assert verdict == "maybe-starting", bad


def test_scrollback_done_does_not_mask_tail_without_interrupt():
    """Card #989 redefines the rule these lines once pinned: with no
    interrupt hint anywhere, a duration-less tail header is an aborted
    turn, NOT a live one — scrollback must not flip that verdict."""
    lines = ["▣  Build · x · 4h 59m", "some output",
             "▣  Build · x (no duration yet)"]
    assert mesh._opencode_running(lines) is False
    assert mesh._opencode_turn_finished(lines) is False


@pytest.mark.parametrize("name", CAPTURES)
def test_h_m_only_durations_count_as_done(name: str):
    """Item 1 directly: '4h 59m', '12m', '1h' all match the done-regex."""
    assert mesh._OPENCODE_DONE.search("▣  Build · x · 4h 59m")
    assert mesh._OPENCODE_DONE.search("▣  Build · x · 12m")
    assert mesh._OPENCODE_DONE.search("▣  Build · x · 1h")
    assert not mesh._OPENCODE_DONE.search("▣  Build · x (New)")


class _StubState:
    def __init__(self):
        self.gateway = "http://gw:8788"
        self.self_name = "opencode"
        self.token = "tok"
        self._ledgers: dict = {}

    def ledger(self, name: str) -> dict:
        return self._ledgers.setdefault(
            name, {"last_delivered_id": 0, "last_delivery_at": 0.0,
                   "consecutive_failures": 0})


def test_opencode_deferral_names_its_real_reason(monkeypatch):
    """Item 3: the opencode-in-progress deferral sets deferred_reason, so
    the monitor's DEFERRED line stops saying 'composer holds human text'.
    Rigged at deliver(): wake outstanding, agent running, and the real
    17:18Z capture with a LIVE tail (bare header + interrupt hint — the
    documented mid-turn screen) so the real reader judges in-progress."""
    lines = _lines("opencode-deaf-1718Z.txt") + [
        "     ▣  Build · DeepSeek V4.1 Flash",
        "  esc interrupt",
    ]
    assert mesh._opencode_running(lines) is True, "rig must read live"
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: lines)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: True)
    state = _StubState()
    led = state.ledger("tmux:opencode")
    led["wake_outstanding"] = True
    sink = mesh.TmuxSink("opencode")
    assert sink.deliver(state, [{"id": 7}], 7) is None
    assert "human text" not in (sink.deferred_reason or ""), \
        sink.deferred_reason
    assert "opencode" in (sink.deferred_reason or "").lower(), \
        sink.deferred_reason
