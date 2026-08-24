"""#533: `_tmux_wake` must VERIFY the wake submitted, not gesture blindly.

The old Codex-shaped double-Enter assumed a second Enter on a single-submit
composer is a no-op. Falsified on cursor's Linux TUI: the Enters raced the
composer, the prompt never submitted, and consecutive wakes CONCATENATED —
'check meshcheck meshcheck mesh' arrived as one prompt on cursor-lin
(2026-08-24). The replacement injects, settles, then Enter-and-verifies in a
bounded loop, returning True only on an observed-clear composer so an
unconfirmed wake stays owed.
"""

from __future__ import annotations

import subprocess

import pytest

import swarph_cli.commands.mesh as mesh


@pytest.fixture
def tmux(monkeypatch):
    """Fake tmux: records send-keys calls; capture-pane output is scripted by
    the test via `captures` (a list consumed in order, last value repeated)."""
    calls: list[list[str]] = []
    state = {"captures": [""], "i": 0}

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "capture-pane":
            i = min(state["i"], len(state["captures"]) - 1)
            state["i"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout=state["captures"][i], stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    return calls, state


def enters(calls) -> int:
    return sum(1 for c in calls if c[:3] == ["tmux", "send-keys", "-t"] and "Enter" in c)


def test_first_enter_submits_single_submit_composer(tmux):
    calls, state = tmux
    state["captures"] = ["> "]  # composer clear after the first Enter
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 1


def test_second_enter_fires_only_when_the_first_did_not_submit(tmux):
    """The Codex shape, now verified instead of blind: first Enter leaves the
    prompt in the composer, second clears it."""
    calls, state = tmux
    state["captures"] = ["> check mesh|", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_concatenated_backlog_is_detected_and_drained(tmux):
    """The observed defect shape: two wakes stacked unsubmitted. The substring
    check catches the concatenation and the retry Enter drains it."""
    calls, state = tmux
    state["captures"] = ["> check meshcheck mesh|", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_never_submitting_pane_is_bounded_and_reports_false(tmux):
    """A pane that never clears gets exactly the bounded attempts, then False —
    the wake stays owed (cursor advances only inside `if _tmux_wake(...)`)."""
    calls, state = tmux
    state["captures"] = ["> check mesh|"]  # never clears
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == mesh._WAKE_SUBMIT_ATTEMPTS


def test_capture_failure_fails_closed_no_unverified_retries(tmux, monkeypatch):
    """gpt-ops, PR #306: an unreadable composer is UNKNOWN, and unknown must
    not earn another Enter — an unverified keypress can land on a human's
    half-typed line (#403's shape). The wake fails closed and stays owed."""
    calls, state = tmux

    def failing_capture(argv, **kw):
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pane")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", failing_capture)
    assert mesh._tmux_wake("pane") is False
    # exactly the one Enter whose outcome we tried to verify — no blind retries
    assert enters(calls) == 1


def test_capture_failure_mid_loop_stops_the_loop(tmux, monkeypatch):
    """A capture that fails AFTER a successful pending read still fails closed:
    one verified-pending retry, then unknown stops it."""
    calls, state = tmux
    state["captures"] = ["> check mesh|"]  # first capture: still pending

    def flaky(argv, **kw):
        if argv[1] == "capture-pane" and state["i"] > 0:
            raise OSError("tmux socket vanished")
        if argv[1] == "capture-pane":
            i = min(state["i"], len(state["captures"]) - 1)
            state["i"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout=state["captures"][i], stderr="")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", flaky)
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == 2  # the first attempt + the one verified retry


def test_send_keys_failure_returns_false_not_raise(tmux, monkeypatch):
    def boom(argv, **kw):
        raise OSError("tmux gone")

    monkeypatch.setattr(mesh.subprocess, "run", boom)
    assert mesh._tmux_wake("pane") is False


def test_empty_capture_is_unknown_not_clear(tmux):
    """gpt-ops round 2: a successful-but-empty capture proves NOTHING about
    the composer — it must not be read as 'clear'. Unknown fails closed."""
    calls, state = tmux
    state["captures"] = [""]
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == 1


def test_unrecognizable_capture_is_unknown_not_clear(tmux):
    """A capture with no composer prompt line and no wake text is
    UNRECOGNIZABLE, not submitted — fail closed, no blind retries."""
    calls, state = tmux
    state["captures"] = ["rendering…", "⠋ working"]
    assert mesh._tmux_wake("pane") is False
    assert enters(calls) == 1


def test_cursor_composer_marker_counts_as_recognizable(tmux):
    """Cursor's Linux TUI renders the composer as '→ Add a follow-up', never
    '>' (measured live on cursor-lin). Without the '→' marker every cursor
    capture would be unrecognizable — fail-closed but permanently deaf."""
    calls, state = tmux
    state["captures"] = ["→ Add a follow-up                      ctrl+c to stop"]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 1


def test_codex_composer_marker_counts_as_recognizable(tmux):
    """Codex renders the composer as `›`, not `>` (gpt-ops live capture)."""
    calls, state = tmux
    state["captures"] = ["› "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 1


def test_cursor_composer_holding_the_wake_is_pending(tmux):
    calls, state = tmux
    state["captures"] = ["→ check mesh", "→ Add a follow-up"]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


# ── Drain-edge gate (commander, 2026-08-24): one wake per DRAIN cycle ─────
#
# The old level-trigger fired once per DM batch; N batches to an undrained
# cell stacked N wakes in the composer. The gate: a wake stands until the
# gateway reports unread == 0; while it stands, never re-inject text — at
# most one verified Enter nudge when the pane observably holds the wake.


class _State:
    def __init__(self):
        self._led = {}
        self.gateway = "http://gw"
        self.token = "tok"
        self.self_name = "cell"

    def ledger(self, _name):
        return self._led


@pytest.fixture
def gate(monkeypatch):
    calls = {"wake": 0, "enter": 0}
    state = _State()
    sink = mesh.TmuxSink("pane")
    # composer: the politeness gate's four-way ("clear"/"wake"/"busy"/None);
    # pending: _wake_still_pending's three-way, still used by the False-split.
    box = {"unread": 1, "pending": False, "wake_ok": True, "composer": "clear"}
    monkeypatch.setattr(mesh, "_tmux_wake",
                        lambda t: calls.__setitem__("wake", calls["wake"] + 1) or box["wake_ok"])
    monkeypatch.setattr(mesh, "_tmux_enter",
                        lambda t: calls.__setitem__("enter", calls["enter"] + 1) or True)
    monkeypatch.setattr(mesh, "_wake_still_pending", lambda t: box["pending"])
    monkeypatch.setattr(mesh, "_composer_state", lambda t: box["composer"])
    import swarph_cli.commands.watchdog as wd
    monkeypatch.setattr(wd, "_gateway_unread_count",
                        lambda g, p, t: box["unread"])
    return sink, state, calls, box


def test_fresh_wake_injects_and_marks_outstanding(gate):
    sink, state, calls, box = gate
    assert sink.deliver(state, [], 1) is True
    assert calls == {"wake": 1, "enter": 0}
    assert state.ledger("x")["wake_outstanding"] is True


def test_outstanding_wake_is_not_stacked_while_undrained(gate):
    """THE commander's case: submitted wake, unread inbox, more DMs arrive —
    deliver succeeds (cursor advances) but NOTHING is injected."""
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)
    for _ in range(5):
        assert sink.deliver(state, [], 2) is True
    assert calls["wake"] == 1  # one wake total, not one per batch
    assert calls["enter"] == 0


def test_outstanding_unsubmitted_wake_gets_a_nudge_not_new_text(gate):
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)  # wake "succeeds"... but say it didn't:
    state.ledger("x")["wake_outstanding"] = True
    box["composer"] = "wake"  # composer observably holds ONLY the wake
    assert sink.deliver(state, [], 2) is True
    assert calls["enter"] == 1
    assert calls["wake"] == 1


def test_drain_re_arms_the_wake(gate):
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)
    box["unread"] = 0  # the cell drained
    assert sink.deliver(state, [], 2) is True
    assert calls["wake"] == 2  # a fresh cycle earns a fresh wake


def test_unreadable_drain_signal_does_not_rearm(gate):
    """unread=None (gateway error) must read as NOT-drained — re-arming on an
    unreadable signal re-opens the stack."""
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)
    box["unread"] = None
    assert sink.deliver(state, [], 2) is True
    assert calls["wake"] == 1


def test_unreadable_pane_keeps_the_failure_loud(gate):
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)
    box["composer"] = None  # capture failing
    assert sink.deliver(state, [], 2) is False


def test_failed_wake_with_text_stuck_marks_outstanding_without_stacking(gate):
    """Submit-unverified: text is IN the composer. The flag goes up so the
    next poll nudges (Enter) instead of injecting a second copy."""
    sink, state, calls, box = gate
    box["wake_ok"] = False
    box["pending"] = True
    assert sink.deliver(state, [], 1) is False
    assert state.ledger("x")["wake_outstanding"] is True
    box["composer"] = "wake"  # the stuck text, observed on the composer line
    assert sink.deliver(state, [], 1) is True  # gate nudges, no re-inject
    assert calls["wake"] == 1
    assert calls["enter"] == 1


def test_failed_wake_with_nothing_landed_retries_next_poll(gate):
    """Inject failure (pane gone): no text to stack, flag stays down so the
    next poll retries the inject — and the failure stays loud."""
    sink, state, calls, box = gate
    box["wake_ok"] = False
    box["pending"] = None
    assert sink.deliver(state, [], 1) is False
    assert "wake_outstanding" not in state.ledger("x")
    assert sink.deliver(state, [], 1) is False
    assert calls["wake"] == 2


# ── Politeness gate (commander, 2026-08-24): never type into a busy composer ─
#
# A verified wake still INJECTED blind: '-l' appends to whatever sits in the
# composer, so a wake landing mid-keystroke merged into the human's line.
# The gate reads the composer BEFORE acting: inject only into "clear", nudge
# only "wake"-only, defer (None) on "busy" — owed, not failed. Unreadable
# FAILS loud with zero keystrokes (a dead pane must keep ringing the alarm).


@pytest.mark.parametrize("captured,expected", [
    ("> ", "clear"),                          # bare claude composer
    ("› ", "clear"),                          # bare codex composer (gpt-ops live capture)
    ("→ ", "clear"),                          # bare cursor composer
    ("› half-typed human line", "busy"),
    ("› check mesh", "wake"),
    ("→ Add a follow-up", "clear"),           # cursor's EMPTY placeholder
    ("> half-typed human line", "busy"),
    ("→ half-typed human line", "busy"),
    ("→ check mesh", "wake"),                 # our wake, unsubmitted
    ("→ check meshcheck mesh", "wake"),       # pre-fix stack, still drainable
    ("→ check meshwait no", "busy"),          # human text MERGED into our wake
    ("→ waitcheck mesh", "busy"),             # wake text not alone = human's line
    ("some output\nno composer here", None),  # unrecognizable → unknown
    ("", None),                               # empty capture → unknown
])
def test_composer_state_matrix(tmux, captured, expected):
    calls, state = tmux
    state["captures"] = [captured]
    assert mesh._composer_state("pane") == expected


def test_composer_state_capture_failure_is_unknown(tmux, monkeypatch):
    def boom(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pane")
    monkeypatch.setattr(mesh.subprocess, "run", boom)
    assert mesh._composer_state("pane") is None


def test_busy_composer_defers_the_inject(gate):
    """THE commander's case: mid-write, a wake arrives. No text lands, the
    wake stays owed, and NOTHING is counted as failed."""
    sink, state, calls, box = gate
    box["composer"] = "busy"
    assert sink.deliver(state, [], 1) is None
    assert calls == {"wake": 0, "enter": 0}
    assert "wake_outstanding" not in state.ledger("x")


def test_unreadable_composer_fails_without_typing(gate):
    """Unknown fails closed BEFORE typing: pre-gate, an unreadable pane got
    the blind inject (then one Enter). Now: zero keystrokes — but a LOUD
    failure, not a deferral, because a dead pane must keep ringing the
    dead-sink alarm (deferring it would silently freeze the ledger)."""
    sink, state, calls, box = gate
    box["composer"] = None
    assert sink.deliver(state, [], 1) is False
    assert calls == {"wake": 0, "enter": 0}
    assert "wake_outstanding" not in state.ledger("x")


def test_busy_composer_defers_the_nudge(gate):
    """Wake text sits in the composer but human text merged into it: an Enter
    would submit THEIR line (#403's shape). Defer, don't nudge."""
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)  # fresh wake, composer clear
    box["composer"] = "busy"    # human started typing into the wake's line
    assert sink.deliver(state, [], 2) is None
    assert calls["enter"] == 0
    assert calls["wake"] == 1


def test_fresh_path_nudges_a_wake_only_composer(gate):
    """gpt-ops REVISE on 7b009eb: a monitor restart loses wake_outstanding,
    but the wake TEXT still sits alone in the composer. The fresh path must
    NUDGE it (one Enter, flag re-armed) — not stack a second copy, and not
    fail loud against a pane that holds exactly what we would have typed."""
    sink, state, calls, box = gate
    box["composer"] = "wake"  # flag lost, text present — the restart shape
    assert sink.deliver(state, [], 1) is True
    assert calls == {"wake": 0, "enter": 1}  # nudged, never injected
    assert state.ledger("x")["wake_outstanding"] is True


def test_deferral_does_not_rearm_and_recovers(gate):
    """A deferral leaves the gate exactly as armed: next poll with a clean
    composer delivers the owed wake normally."""
    sink, state, calls, box = gate
    box["composer"] = "busy"
    assert sink.deliver(state, [], 1) is None
    box["composer"] = "clear"
    assert sink.deliver(state, [], 1) is True
    assert calls["wake"] == 1
    assert state.ledger("x")["wake_outstanding"] is True


class _DeferSink(mesh.Sink):
    is_push = True

    def __init__(self):
        super().__init__("defer-stub")

    def deliver(self, state, dms, up_to_id):
        return None


class _EngineState:
    """The slices of MonitorState that _monitor_deliver touches."""

    def __init__(self, tmp_path):
        self.observed = {"last_msg_id": 5}
        self.min_interval_s = 0.0
        self.replay_limit = 50
        self.inbox_log_path = tmp_path / "inbox.log"
        self.inbox_log_path.write_text("", encoding="utf-8")
        self.log_prefix = "[test]"
        self.deliveries = {}
        self.ledgers = {}
        self.ledgers_path = tmp_path / "ledgers.json"
        self.sinks = [_DeferSink()]

    def ledger(self, name):
        return self.ledgers.setdefault(name, mesh._new_ledger())


def test_engine_counts_deferral_as_neither_success_nor_failure(tmp_path, capsys):
    """The engine contract for None: cursor NOT advanced (wake stays owed),
    consecutive_failures NOT incremented (a busy composer is not a dead
    sink), no DELIVERY FAILED alarm, no ledger write."""
    state = _EngineState(tmp_path)
    mesh._monitor_deliver(state)
    led = state.ledgers["defer-stub"]
    assert led["last_delivered_id"] == 0
    assert led["consecutive_failures"] == 0
    assert state.deliveries == {}
    assert not state.ledgers_path.exists()  # nothing changed, nothing written
    out = capsys.readouterr()
    assert "DEFERRED" in out.out
    assert "DELIVERY FAILED" not in out.err
