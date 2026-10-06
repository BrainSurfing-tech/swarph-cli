"""#1077 (#1073 round 2; #1083; #1100 clauses 2–3): an unrecognised busy
render — or an unknown run state — must not be typed into, whatever the
ledger says.

science-claude FAILED #1073 at 6c4180b: the #1010 spent rule read a clear
placeholder composer as positively idle, but the pane was answering under
a busy render the detector does not know — '✻ Pondering… (esc to cancel)',
no braille spinner, no 'ctrl+c to stop' on the composer — and the second
DM was typed straight into the running turn. Base (0.72.3) typed 0 there:
its STANDING claim, for all its strand defects, sent no keys into a pane
it could not read.

THE RULE (#1077): a pane whose busy state the detector does NOT
positively recognise gets ZERO keystrokes — inject and nudge both —
while a wake is outstanding, with a loud log naming the unrecognised
state. The hold is BOUNDED: past _WAKE_TURN_BOUND_S (600s) after the
FIRST SIGHT of the unknown state (#1083: not after the injection), the
strand risk (a DM never typed, friendly-coder #1010) outweighs the
mid-turn risk and the sink types, loudly stating the bound.

#1100 clause (2) CLOSED the last door: the hold applies with NO wake of
ours in play too. At 74bfa09 the no-wake path typed 'check mesh'+Enter
into an unknown render at once (science-claude's wiped-state-dir probe).
A claude cell — unknown its steady state — pays the bound ONCE per
unknown episode and then receives its DM, loudly.

#1100 clause (3) is the ONE exception: OUR OWN unsubmitted wake text
sitting in the composer is our keystroke already on screen — recognised,
and the Enter nudge goes PROMPTLY, never held to the bound (at 74bfa09 it
sat ~600s behind a wrong-cause hold log).

Tests marked RED fail on 6c4180b or 74bfa09 as noted; the boundary tests
pin the shapes that keep typing.
"""

from __future__ import annotations

import subprocess
import time

import pytest

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog


# ── the fixture renders ──────────────────────────────────────────────────
# science-claude's shape: a busy render the spinner rule does not cover,
# above a placeholder composer. '│ ○ check mesh │' is the submitted
# prompt echo cursor draws in history (measured live, cursor-lin).

_PONDERING = [
    "│ ○ check mesh                                                          │",
    "✻ Pondering… (esc to cancel)",
    "→ Add a follow-up",
    "1 task",
    "Kimi K3 Max · 83.6% · 9 files edited",
    "~",
]

_IDLE_POSITIVE = [
    "mesh: nothing new, inbox clean",
    "",
    "Tip: mesh is quiet",
    "",
    "",
    "→ Add a follow-up",
    "",
    "",
    "1 task",
    "Kimi K3 Max · 83.6% · 9 files edited",
    "~",
]

#: #1100: two real raw idle shapes (measured): a fully BLANK gap
#: (idle-lin.txt / idle-lin-794-capture.txt), and a gap carrying only
#: Tip chrome (#783's verified-idle capture: the Tip row at composer−3,
#: blanks around it). Mid-turn the spinner renders in the gap too —
#: never at idle.
_IDLE_RAW = [
    "mesh: nothing new, inbox clean",
    "",
    "",
    "",
    "",
    "→ Add a follow-up",
    "",
    "",
    "1 task",
    "Kimi K3 Max · 83.6% · 9 files edited",
    "~",
]

_SPINNER_BUSY = [
    "mesh: asked",
    "⠋ Working  6.5k tokens",
    "Tip: Use subagents to parallelize work.",
    "→ Add a follow-up                                           ctrl+c to stop",
]


class _StubState:
    def __init__(self):
        self.gateway = "http://gw:8788"
        self.self_name = "cursor-lin"
        self.token = "tok"
        self._ledgers: dict = {}

    def ledger(self, name: str) -> dict:
        return self._ledgers.setdefault(
            name, {"last_delivered_id": 0, "last_delivery_at": 0.0,
                   "consecutive_failures": 0})


def _owed(injected_at=None):
    state = _StubState()
    led = state.ledger("tmux:sac")
    led["wake_outstanding"] = True
    if injected_at is not None:
        led["last_wake_injected_at"] = injected_at
    return state


@pytest.fixture
def pane(monkeypatch):
    """A scripted tmux: every capture returns the fixture, send-keys are
    recorded, sleeps are free. The REAL _composer_state/_agent_running/
    _unknown_runstate_hold run against the fixture — the classifier is
    under test, not stubbed."""
    calls = []
    holder = {"lines": _PONDERING}

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(
                argv, 0, stdout="\n".join(holder["lines"]) + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: 1)
    return calls, holder


def _sent(calls) -> int:
    return sum(1 for c in calls if c[:2] == ["tmux", "send-keys"])


# ── the classifier ────────────────────────────────────────────────────────

def test_pondering_render_is_unknown_not_idle(monkeypatch):
    """>>> RED on 6c4180b: the detector read this pane as False (idle) —
    no spinner, no composer hint — and the sink typed into mid-turn. <<<
    The '✻ Pondering…' row sits INSIDE the gap: not a spinner the table
    measured, not a blank idle gap — the allowlist never sees a captured
    idle form: None."""
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t: _PONDERING)
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _PONDERING)
    assert mesh._agent_running("sac") is None
    assert mesh._composer_state("sac") == "clear"  # the trap: clear composer


def test_spinner_slot_is_positively_busy(monkeypatch):
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t: _SPINNER_BUSY)
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _SPINNER_BUSY)
    assert mesh._agent_running("sac") is True


def test_real_raw_idle_shape_is_positively_idle(monkeypatch):
    """The #1100 allowlist's positive arm: a pane whose EVERY
    bottom-region row matches a real idle capture reads False."""
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t: _IDLE_RAW)
    assert mesh._agent_running("sac") is False


def test_tip_row_in_the_gap_is_captured_idle_chrome(monkeypatch):
    """The Tip row is MEASURED chrome, not a status: the #783 hunt's
    verified-idle capture carries 'Tip: Use /plan…' inside the gap at
    composer−3, and mid-turn panes show it above the spinner (pane_2_dm1,
    live chains 2026-10-06). No busy render measured — #1078's, #1084's
    ten — ever takes the 'Tip: ' form, so the row is allowlisted EXACTLY:
    a Tip row in the gap keeps the pane idle when every other row is a
    captured form — the allowlist's positive arm, not a denylist's."""
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t: _IDLE_POSITIVE)
    assert mesh._agent_running("sac") is False


def test_hint_quoted_without_parens_stays_idle(monkeypatch):
    """cursor-lin's own scrollback has discussed 'ctrl+c to stop'
    mid-output, and that history must not read as a running turn. #1100:
    the quote row lands INSIDE the gap window — an off-form row, so the
    pane reads UNKNOWN: not busy, never asserted idle."""
    lines = ["user: why does it say ctrl+c to stop?", "→ Add a follow-up"]
    monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t: lines)
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: lines)
    assert mesh._agent_running("sac") is None


# ── deliver: the unrecognised busy pane holds ZERO keystrokes ────────────

def test_unrecognised_busy_render_holds_zero_keystrokes_while_outstanding(
        pane, capsys):
    """>>> RED on 6c4180b: with the flag standing and the pane answering
    under '✻ Pondering… (esc to cancel)', deliver spent the wake and TYPED
    the DM into the running turn. <<< The hold: zero keystrokes, a loud
    log naming the render, the flag untouched."""
    calls, holder = pane
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None  # held, not delivered
    assert _sent(calls) == 0                            # ZERO keystrokes
    led = state.ledger("tmux:sac")
    assert led["wake_outstanding"] is True              # neither spent nor claimed
    out = capsys.readouterr().out
    assert "not positively recognised" in out
    assert "Pondering" in out                           # the log NAMES the state
    assert "zero keys" in out


def test_own_unsubmitted_wake_text_is_nudged_promptly(pane, capsys):
    """>>> RED on 74bfa09: our own wake text sitting in the composer read
    UNKNOWN, and the Enter nudge sat ~600s behind the bound with a
    wrong-cause hold log. <<< #1100 clause (3): the composer holding
    exactly our wake prompt — even under an unrecognised busy render —
    is OUR keystroke already on screen: recognised, and the ONE verified
    Enter nudge goes PROMPTLY."""
    calls, holder = pane
    holder["lines"] = list(_PONDERING)
    holder["lines"][2] = "→ check mesh"  # wake text unsubmitted in the composer
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1                     # the Enter went NOW
    out = capsys.readouterr().out
    assert "not positively recognised" not in out  # no wrong-cause hold line
    led = state.ledger("tmux:sac")
    assert led["wake_outstanding"] is True          # the nudge re-armed it


def test_own_stacked_wake_text_is_nudged_promptly_too(pane):
    """>>> RED on 74bfa09. <<< The measured pre-#533 stacked
    concatenation 'check meshcheck mesh' is the same own-text shape:
    recognised, nudged promptly."""
    calls, holder = pane
    holder["lines"] = list(_PONDERING)
    holder["lines"][2] = "→ check meshcheck mesh"
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1


def test_monitor_restart_holds_too(pane):
    """The restart shape: a brand-new state whose persisted ledger carries
    the flag and a fresh anchor. The hold is anchored to the injection
    time, which persists with the ledger — the restart does not get to
    type into an unrecognised pane either."""
    calls, holder = pane
    persisted = {"last_delivered_id": 0, "last_delivery_at": 0.0,
                 "consecutive_failures": 0, "wake_outstanding": True,
                 "last_wake_injected_at": time.time() - 30}
    state = _StubState()
    state._ledgers["tmux:sac"] = dict(persisted)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0


# ── the bound: stated, loud, and tested ──────────────────────────────────

def test_bound_fires_after_ten_minutes_and_types(pane, capsys):
    """>>> RED on 6c4180b via the log: past the bound the sink must TYPE —
    and must say it did, naming the bound. <<< _WAKE_TURN_BOUND_S is
    600s, and #1083 re-anchored the clock: it counts from FIRST SIGHT of
    the unknown state (unknown_seen_at), not from the injection. This
    fixture models the pane unknown since 601s ago (first seen while the
    wake injected 700s ago was already turning); within the bound the
    strand risk stays subordinate to the mid-turn risk; past it the DM
    must not sit undelivered forever (an answered idle wake left untyped
    is the other FAIL arm)."""
    calls, holder = pane
    now = time.time()
    state = _owed(injected_at=now - 700)
    state.ledger("tmux:sac")["unknown_seen_at"] = now - 601
    assert mesh._WAKE_TURN_BOUND_S == 600.0  # the bound is stated and pinned
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1                   # typed: the -l inject (plus Enter)
    out = capsys.readouterr().out
    assert "bound" in out and "600" in out    # loud, bound stated
    assert "first sight" in out                # #1083: the anchor is named


def test_no_wake_in_play_unknown_holds_to_the_bound(pane, capsys):
    """>>> RED on 74bfa09: the no-wake path typed 'check mesh'+Enter into
    the unknown render AT ONCE, silently (science-claude's wiped-state
    -dir probe). <<< #1100 clause (2): the hold applies with NO wake of
    ours in play too — first DM after a monitor start (no flag, no
    anchor) gets ZERO keystrokes at the first sight of an unknown
    render, a loud log naming it, and the first-sight clock stamped."""
    calls, holder = pane
    state = _StubState()  # no flag, no anchor — the wiped-state-dir shape
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    out = capsys.readouterr().out
    assert "not positively recognised" in out  # loud, no silent typing
    assert "zero keys" in out
    assert "unknown_seen_at" in state.ledger("tmux:sac")  # clock stamped


def test_no_wake_in_play_unknown_types_loudly_past_the_bound(pane, capsys):
    """The bounded arm of clause (2): past _WAKE_TURN_BOUND_S from FIRST
    SIGHT the strand risk wins and the sink types — loudly, naming the
    bound — so a claude cell (unknown its steady state) still receives
    its DM. A cell pays the bound ONCE per unknown episode."""
    calls, holder = pane
    now = time.time()
    state = _StubState()
    led = state.ledger("tmux:sac")
    led["unknown_seen_at"] = now - 601  # first sight past the bound
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1
    out = capsys.readouterr().out
    assert "bound" in out and "600" in out and "first sight" in out
    # The injection starts a NEW episode: _stamp_wake clears the unknown
    # markers by design (#1083) — a still-unknown next poll takes a FRESH
    # first sight with a fresh clock, which is the once-per-EPISODE rule.


# ── the boundaries that keep typing (accept clauses 2 and 3) ─────────────

def test_positively_idle_after_answered_wake_still_types(pane):
    """Clause (2): a pane POSITIVELY idle — every bottom-region row a
    captured idle form, the answered wake's output above a BLANK gap —
    spends the wake and the DM is typed. #1010's rule survives #1077 and
    #1100 unchanged on this shape."""
    calls, holder = pane
    holder["lines"] = list(_IDLE_RAW)
    state = _owed(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1
    assert state.ledger("tmux:sac")["wake_outstanding"] is True  # re-armed


def test_known_busy_spinner_defers_zero_keys(pane):
    """Clause (3), unchanged: a positively recognised busy pane defers
    with zero keystrokes (the #619/#620 paths)."""
    calls, holder = pane
    holder["lines"] = list(_SPINNER_BUSY)
    state = _owed(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    assert state.ledger("tmux:sac")["wake_outstanding"] is False  # #620 cleared