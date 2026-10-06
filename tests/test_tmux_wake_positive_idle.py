"""#1083 (#1078 rework) — IDLE IS A POSITIVE SIGNATURE, AND THE BOUND
COUNTS FROM FIRST SIGHT.

Measured by science-claude at 4e920ff: four NOVEL busy renders read as
positively idle (``_agent_running`` False) — '⠘⠆ Editing  97.46k tokens'
(a spinner verb the table did not carry), streaming partial text with no
status row, 'Running tool: bash (press esc to interrupt)' (a verb before
the key inside the parens), and the French '◐ Réflexion en cours… (Échap
pour annuler)' — and the sink typed DMs into running turns. Two rules now
hold:

1. A pane the detector does not POSITIVELY recognise as idle — a BARE
   placeholder composer under a plain, COMPLETED slot row, chrome below
   (the measured idle captures, tests/pane_renders/cursor/, including
   the real #794-hunt capture idle-lin-794-capture.txt) — is UNKNOWN,
   never idle.
2. The hold's bound counts from the FIRST SIGHT of the unknown state
   (``unknown_seen_at``), not from the last injection (#1078: an old
   injection fired the bound AT first sight). No ledger anchor at all —
   the monitor-restart wipe — holds too: OBSERVED wake text or a
   standing flag is the wake-in-play proof.

The renders are FILES, one per capture (lab-ovh's ask): a new harness
render (cursor-win's Windows idle, when it lands) is a new file here and
a signature decision, not an inline edit in every test. Real captures:
busy-lin-midturn (cursor-lin, live, 2026-10-06 08:25Z), busy-win-midturn
(cursor-win's Windows pane via science-claude, msg 62281), idle-lin (the
measured idle signature) and idle-lin-794-capture (a real cursor-lin
idle pane from the #794 hunt, byte-for-byte).
"""
import pathlib
import subprocess
import time

import pytest

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog

_RENDER_DIR = pathlib.Path(__file__).parent / "pane_renders" / "cursor"

#: The four #1078 replays — each red on 4e920ff (typed), zero keystrokes
#: here. The three the detector still does not positively know read
#: UNKNOWN; the Editing spinner is a measured verb (#1083) and reads
#: positively busy — mid-turn, zero keys either way.
_NOVEL_BUSY = (
    "busy-editing-spinner",
    "busy-streaming-partial",
    "busy-running-tool",
    "busy-reflexion-fr",
)

#: The subset the detector cannot positively name: UNKNOWN, never idle.
_NOVEL_UNKNOWN = (
    "busy-streaming-partial",
    "busy-running-tool",
    "busy-reflexion-fr",
)


def _pane(name: str) -> list[str]:
    return [
        ln for ln in (_RENDER_DIR / f"{name}.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
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
    """A scripted tmux: captures return the fixture render, send-keys are
    recorded, sleeps are free. The REAL classifier and deliver run — the
    detector is under test, not stubbed."""
    calls = []
    holder = {"lines": _pane("busy-pondering")}

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


# ── clause (1): the four #1078 replays are unknown, and hold ─────────────

@pytest.mark.parametrize("render", _NOVEL_UNKNOWN)
def test_novel_busy_render_reads_unknown_not_idle(monkeypatch, render):
    """>>> RED on 4e920ff: all three read False (positively idle). <<< A
    render the detector does not positively know is UNKNOWN — never
    idle."""
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _pane(render))
    assert mesh._agent_running("sac") is None


@pytest.mark.parametrize("render", _NOVEL_BUSY)
def test_novel_busy_render_gets_zero_keystrokes_before_the_bound(
        pane, capsys, render):
    """>>> RED on 4e920ff: the DM was typed into the running turn. <<<
    With a wake outstanding, every #1078 replay gets 0 keystrokes — the
    unmeasured renders hold on the unknown runstate, the measured
    '⠘⠆ Editing' spinner defers as positively mid-turn."""
    calls, holder = pane
    holder["lines"] = _pane(render)
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0


def test_unknown_hold_keeps_the_flag_and_names_the_render(pane, capsys):
    """The unknown hold's shape: flag untouched (neither spent nor
    claimed), and the log NAMES the unrecognised render."""
    calls, holder = pane
    holder["lines"] = _pane("busy-streaming-partial")
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert state.ledger("tmux:sac")["wake_outstanding"] is True
    out = capsys.readouterr().out
    assert "not positively recognised" in out
    assert "zero keys" in out


# ── real captures: busy is positively busy, idle is positively idle ─────

def test_lin_midturn_real_capture_is_positively_busy(monkeypatch):
    """cursor-lin's own live mid-turn capture (2026-10-06 08:25Z): the
    composer hint and the '⠰⠳ Running' spinner both read positively
    busy — a green regression guard on both heads."""
    monkeypatch.setattr(mesh, "_capture_pane_lines",
                        lambda t: _pane("busy-lin-midturn"))
    assert mesh._agent_running("sac") is True


def test_win_midturn_real_capture_is_positively_busy(monkeypatch):
    """cursor-win's real Windows mid-turn render (msg 62281): the hint
    has NO space before it ('→ Add a follow-upctrl+c to stop') — the
    substring match, not a column or whitespace anchor, is what catches
    it. The status row reads '⠘⠆ Running  <n> tokens' (no Linux verb
    set difference: Running is measured)."""
    monkeypatch.setattr(mesh, "_capture_pane_lines",
                        lambda t: _pane("busy-win-midturn"))
    assert mesh._agent_running("sac") is True


def test_editing_spinner_is_measured_busy(monkeypatch):
    """The 'Editing' verb — measured live on cursor-lin (lab-ovh) — is a
    positively-known spinner row, not a glyph-shaped unknown: True."""
    monkeypatch.setattr(mesh, "_capture_pane_lines",
                        lambda t: _pane("busy-editing-spinner"))
    assert mesh._agent_running("sac") is True


def test_unmeasured_spinner_verb_is_unknown(monkeypatch):
    """A braille glyph with a verb NO real capture has shown
    ('⠋ Fetching…') is busy-SHAPED, not positively running: None."""
    lines = ["⠋ Fetching…", "→ Add a follow-up", "1 task", "~"]
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: lines)
    assert mesh._agent_running("sac") is None


# ── clause (2): real idle renders still type promptly ────────────────────

#: Real idle captures: the measured signature shape, and the #794 hunt's
#: real cursor-lin idle pane (byte-for-byte). Both must type promptly.
_REAL_IDLE = ("idle-lin", "idle-lin-794-capture")


@pytest.mark.parametrize("render", _REAL_IDLE)
def test_real_idle_signature_types_promptly_after_answered_wake(pane, render):
    """The measured idle render: a BARE placeholder composer under a
    plain, COMPLETED slot row, chrome below — positively idle, the
    wake spends, the DM types. No hold, no bound wait."""
    calls, holder = pane
    holder["lines"] = _pane(render)
    state = _owed(injected_at=time.time() - 60)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1


@pytest.mark.parametrize("render", _REAL_IDLE)
def test_real_idle_types_after_a_restart_too(pane, render):
    """Clause (2), restart shape: a persisted ledger with the flag
    standing and an old anchor, pane positively idle — the #1010 spent
    rule fires on the OBSERVED idle and the DM types (the restart does
    not get to hold a positively-idle pane)."""
    calls, holder = pane
    holder["lines"] = _pane(render)
    persisted = {"last_delivered_id": 0, "last_delivery_at": 0.0,
                 "consecutive_failures": 0, "wake_outstanding": True,
                 "last_wake_injected_at": time.time() - 3600}
    state = _StubState()
    state._ledgers["tmux:sac"] = dict(persisted)
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1


# ── clause (3): the bound counts from FIRST SIGHT ────────────────────────

def test_old_injection_does_not_fire_the_bound_at_first_sight(pane):
    """>>> RED on 4e920ff: the injection-anchor clock read 700s old as
    bound-fired and the DM typed at the first sight of the render. <<<
    The bound counts from FIRST SIGHT: an anchor older than the bound
    still gives 0 keys at the first observation."""
    calls, holder = pane
    state = _owed(injected_at=time.time() - 700)  # NO unknown_seen_at
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    # the first sight is STAMPED — the next poll holds on it, not on the
    # injection
    assert "unknown_seen_at" in state.ledger("tmux:sac")


def test_no_anchor_wiped_state_holds_too(pane):
    """>>> RED on 4e920ff: a wiped injection anchor read as 'no wake of
    ours in play' and the DM typed. <<< The monitor-restart wipe: the
    flag stands but the anchor is gone — the flag IS the wake-in-play
    proof, and the first sight of the unknown render gets 0 keys."""
    calls, holder = pane
    state = _owed(injected_at=None)  # flag standing, anchor wiped
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    assert state.ledger("tmux:sac")["wake_outstanding"] is True


def test_first_sight_clock_advances_with_holds(pane):
    """Within the bound the hold repeats on the SAME first-sight stamp:
    the clock does not restart per poll, and no keystroke lands."""
    calls, holder = pane
    state = _owed(injected_at=time.time() - 30)
    sink = mesh.TmuxSink("sac")
    assert sink.deliver(state, [{"id": 9}], 9) is None
    first = state.ledger("tmux:sac").get("unknown_seen_at")
    assert first is not None

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert state.ledger("tmux:sac")["unknown_seen_at"] == first
    assert _sent(calls) == 0


def test_positive_read_resets_the_first_sight_clock(pane):
    """The pane turns positively idle (the turn ended), the DM types —
    and a LATER unknown render is a NEW first sight with a fresh clock,
    not the old stamp firing an already-elapsed bound."""
    calls, holder = pane
    state = _owed(injected_at=time.time() - 700)
    sink = mesh.TmuxSink("sac")

    # first sight under the old anchor: held, stamp taken
    assert sink.deliver(state, [{"id": 9}], 9) is None
    # the turn ends: positively idle — types, and the stamp is cleared
    holder["lines"] = _pane("idle-lin")
    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert "unknown_seen_at" not in state.ledger("tmux:sac")
    # the pane goes unknown again: a FRESH first sight, zero keys
    holder["lines"] = _pane("busy-pondering")
    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) >= 1  # only the idle episode typed


def test_bound_fires_once_per_wake_episode(pane, capsys):
    """Past the bound from first sight the sink types — loudly — and the
    same episode does not hold twice (the flag must go through, not be
    re-held on the very next site)."""
    calls, holder = pane
    now = time.time()
    state = _owed(injected_at=now - 700)
    state.ledger("tmux:sac")["unknown_seen_at"] = now - 601
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1
    out = capsys.readouterr().out
    assert "bound" in out and "first sight" in out