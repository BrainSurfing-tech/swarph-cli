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
    # First frame is the pre-inject look (prompt choice). The pending frame
    # is what the first Enter left behind.
    state["captures"] = ["> ", "> check mesh", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_concatenated_backlog_is_detected_and_drained(tmux):
    """The observed defect shape: two wakes stacked unsubmitted. The substring
    check catches the concatenation and the retry Enter drains it."""
    calls, state = tmux
    state["captures"] = ["> ", "> check meshcheck mesh", "> "]
    assert mesh._tmux_wake("pane") is True
    assert enters(calls) == 2


def test_never_submitting_pane_is_bounded_and_reports_false(tmux):
    """A pane that never clears gets exactly the bounded attempts, then False —
    the wake stays owed (cursor advances only inside `if _tmux_wake(...)`)."""
    calls, state = tmux
    state["captures"] = ["> check mesh"]  # never clears
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
    state["captures"] = ["> check mesh"]  # first capture: still pending

    def flaky(argv, **kw):
        if argv[1] == "capture-pane" and state["i"] > 1:
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
    state["captures"] = ["→ Add a follow-up", "→ check mesh", "→ Add a follow-up"]
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
    monkeypatch.setattr(mesh, "_opencode_in_progress", lambda t: False)
    monkeypatch.setattr(mesh, "_opencode_turn_finished_target", lambda t: False)
    # #1010 strand-only hermeticity (#1083): deliver() reads the pane
    # (grok-block probe), the run-state and the session age before it
    # acts — unpatched those reach the REAL tmux binary. A missing pane
    # reads unreadable at base; False/None keep the same falsy
    # fall-through these tests pin.
    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: None)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: False)
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: None)
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
    """THE commander's case: submitted wake, unread inbox, more DMs arrive.
    #1010: those later polls HOLD (None) — nothing is marked delivered
    while untyped, and NOTHING more is injected. One wake total."""
    sink, state, calls, box = gate
    sink.deliver(state, [], 1)
    for _ in range(5):
        assert sink.deliver(state, [], 2) is None
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
    # #1010: an unreadable drain does not re-arm, and the standing wake
    # inside its window holds the DM owed instead of reporting delivery.
    assert sink.deliver(state, [], 2) is None
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
    ("› Ask Codex to do anything", "clear"),  # Codex's EMPTY placeholder
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


# Cursor's REAL layout, measured live on cursor-lin 2026-08-24: the composer
# sits ~4 non-empty lines ABOVE the bottom, with chrome below it (task count,
# model/status bar, '~'). A fixed tail-3 window lands entirely in that chrome
# and reads every cursor pane as unknown — the systematic "capture failed"
# the monitor logged on every real wake. The composer must be found
# STRUCTURALLY (last marker line), never by position.
_CURSOR_LIVE_CLEAR = (
    "│ ○ check mesh                                                               │\n"
    "└────────────────────────────────────────────────────────────────────────────\n"
    "  ⠘ Running  6.5k tokens\n"
    "  → Add a follow-up\n"
    "  1 task\n"
    "  Kimi K3 Max · 83.6% · 9 files edited\n"
    "~"
)

_CURSOR_LIVE_HOLDING_WAKE = _CURSOR_LIVE_CLEAR.replace(
    "→ Add a follow-up", "→ check mesh")


def test_cursor_chrome_below_composer_reads_clear(tmux):
    """The composer is 4 lines from the bottom; the tail-3 window this
    replaces read only chrome and failed closed on every real cursor wake."""
    calls, state = tmux
    state["captures"] = [_CURSOR_LIVE_CLEAR]
    assert mesh._composer_state("pane") == "clear"
    assert mesh._wake_still_pending("pane") is False


def test_cursor_chrome_below_composer_holding_wake(tmux):
    calls, state = tmux
    state["captures"] = [_CURSOR_LIVE_HOLDING_WAKE]
    assert mesh._composer_state("pane") == "wake"
    assert mesh._wake_still_pending("pane") is True


def test_submitted_wake_echo_in_history_is_not_pending(tmux):
    """Cursor echoes a SUBMITTED wake as '│ ○ check mesh │' — no marker. A
    substring check over a tail window reads that as pending forever; reading
    only the composer line does not."""
    calls, state = tmux
    state["captures"] = [_CURSOR_LIVE_CLEAR]  # the ○ echo is in the fixture
    assert mesh._wake_still_pending("pane") is False


def test_claude_history_prompt_above_composer(tmux):
    """Claude echoes submitted prompts as '> text' in history. The LAST
    marker line is still the composer."""
    calls, state = tmux
    state["captures"] = ["> check mesh\nsome reply text\n> "]
    assert mesh._composer_state("pane") == "clear"
    assert mesh._wake_still_pending("pane") is False


def test_merged_wake_and_human_text_stops_the_loop(tmux):
    """gpt-ops, PR #312 rounds 4-5: the human types DURING the settle — the
    composer holds 'check mesh half-typed human line'. Pending must mean
    wake-ONLY: merged reads False, and the verify loop sends NO further
    Enter (it would submit the human's half-typed line, #403's shape). And
    the wake must NOT be acknowledged: _tmux_wake returns None so the sink
    DEFERS and the cursor stays back — otherwise the wake is silently lost
    when the human never sends their line."""
    calls, state = tmux
    state["captures"] = ["> check mesh half-typed human line"]
    assert mesh._wake_still_pending("pane") is False
    assert mesh._tmux_wake("pane") is None  # adopted — DEFER, not delivered
    assert sum(1 for c in calls if c[-1] == "Enter") == 1  # first Enter only


def test_merged_mid_settle_defers_and_holds_the_cursor(tmux):
    """Deliver-level round 5: the gate reads clear, the inject lands, and
    the human merges into the wake during the settle. deliver() must DEFER
    (None): last_delivered_id does NOT advance, no failure is counted, and
    wake_outstanding is NOT set (nothing of ours is driving)."""
    calls, capstate = tmux
    capstate["captures"] = [
        "> ",                              # gate: observed clean
        "> check mesh half-typed human",   # verify: adopted mid-settle
    ]
    state = _State()
    sink = mesh.TmuxSink("pane")
    assert sink.deliver(state, [], 7) is None
    led = state.ledger("tmux:pane")
    assert led.get("last_delivered_id", 0) == 0   # cursor held
    assert led.get("consecutive_failures", 0) == 0
    assert led.get("wake_outstanding") is not True


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
    sink), and no DELIVERY FAILED alarm. A deferral persists only its count
    so the stall alarm survives a monitor restart; it still moves no cursor."""
    state = _EngineState(tmp_path)
    mesh._monitor_deliver(state)
    led = state.ledgers["defer-stub"]
    assert led["last_delivered_id"] == 0
    assert led["consecutive_failures"] == 0
    assert led["deferred_ticks"] == 1
    assert state.deliveries == {}
    assert state.ledgers_path.exists()  # durable count, but no delivery cursor change
    out = capsys.readouterr()
    assert "DEFERRED" in out.out
    assert "DELIVERY FAILED" not in out.err


def test_engine_defers_on_adopted_mid_settle_with_real_tmux_sink(tmp_path, tmux, capsys):
    """gpt-ops, #312 round 6: the deliver-level merged test's cursor
    assertions pass TRIVIALLY — deliver() never writes last_delivered_id,
    the ENGINE does. This pins the real path end-to-end: a REAL TmuxSink,
    scripted clear→merged captures, through _monitor_deliver. The DM stays
    owed (cursor held at 0), no failure counted, no delivery recorded, no
    delivery cursor moved. The durable deferral count is the sole write so
    the alarm survives monitor restarts."""
    calls, capstate = tmux
    capstate["captures"] = [
        "> ",                              # gate: observed clean
        "> check mesh half-typed human",   # verify: adopted mid-settle
    ]
    state = _EngineState(tmp_path)
    state.sinks = [mesh.TmuxSink("pane")]
    mesh._monitor_deliver(state)
    led = state.ledgers["tmux:pane"]
    assert led["last_delivered_id"] == 0  # id 5 still owed
    assert led["consecutive_failures"] == 0
    assert led["deferred_ticks"] == 1
    assert state.deliveries == {}
    assert state.ledgers_path.exists()
    out = capsys.readouterr()
    assert "DEFERRED" in out.out
    assert "DELIVERY FAILED" not in out.err


# ── opencode 1.18.33, measured on a sacrificial pane (card #961) ──────────
#
# The composer is a ┃ box, not a > / › / → line. A ▣ spinner sits between
# the transcript and the box; lines above it are history. `esc interrupt`
# and `Permission required` mean a keystroke is the wrong key.

_OPENCODE_IDLE = """\
┃
┃  Ask anything… "What is the tech stack of this project?"
┃
┃  Build · DeepSeek V4 Pro (New) OpenCode Go
╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  tab agents
  ctrl+p commands
"""

_OPENCODE_HOLDING = """\
┃
┃  check mesh: swarph_dm_unread
┃
┃  Build · DeepSeek V4 Pro (New) OpenCode Go
╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
"""

_OPENCODE_RUNNING = """\
┃  check mesh: swarph_dm_unread
┃  Click to expand
▣  Build · DeepSeek V4 Pro (New)
┃
┃
┃  Build · DeepSeek V4 Pro (New) OpenCode Go
  esc interrupt
"""

# 1.18.33 draws the dialog inside the box. The mode row and the bottom
# border are not on this screen (drop-on-meta-edge, card #961 post 54557).
_OPENCODE_PERMISSION = """\
┃
┃  △ Permission required
┃
  Allow once
  Allow always
  Reject
"""

_OPENCODE_FINISHED = """\
▣  Build · DeepSeek V4 Pro (New) · 3.3s
┃
┃  Ask anything… "What is the tech stack of this project?"
┃
┃  Build · DeepSeek V4 Pro (New) OpenCode Go
╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  tab agents  ctrl+p commands
"""


def test_live_idle_footer_is_clear_and_not_running(tmux):
    """The 07:10Z pane: composer empty, transcript full of ┃ rows, footer
    cwd on the ┃ row above the mode row, finished header `· 3m 4s`.
    0.69.1 returns that path as composer text and treats the header as
    still running."""
    from pathlib import Path
    fixture = (Path(__file__).resolve().parent / "fixtures"
               / "opencode-pane-idle-footer-0710.txt")
    text = fixture.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert any("Found the real blocker" in ln and ln.strip().startswith("┃")
               for ln in lines)
    _calls, state = tmux
    state["captures"] = [text]
    assert mesh._opencode_input(lines) == ""
    assert mesh._composer_state("sac") == "clear"
    assert mesh._agent_running("sac") is False


def test_drop_six_captures_split_the_footer():
    """A wrapped cwd is a run of spaces, not a fixed number of rows.
    Measured by drop-on-meta-edge on sacW 220x55 (card #961, #612)."""
    from pathlib import Path
    fix = Path(__file__).resolve().parent / "fixtures"
    wide = (fix / "opencode-961" / "W2_turn2.txt").read_text(encoding="utf-8")
    narrow = (fix / "opencode-961" / "G3_turn2.txt").read_text(encoding="utf-8")
    live = (fix / "opencode-pane-idle-footer-0710.txt").read_text(encoding="utf-8")
    assert mesh._opencode_input(wide.splitlines()) == ""
    assert mesh._opencode_input(live.splitlines()) == ""
    assert mesh._opencode_input(narrow.splitlines()) == ""
    typed_narrow = "\n".join([
        "                       ┃",
        "                       ┃  hello from a human",
        "                       ┃",
        "                       ┃  Build · DeepSeek V4 Pro (New) OpenCode Go",
    ])
    assert mesh._opencode_input(typed_narrow.splitlines()) == "hello from a human"
    overlay = (
        "  ┃  hello from a human" + (" " * 150)
        + "meta-edge/d7cba227-9da7-4d92-92da-"
    )
    typed_wide = "\n".join([
        "  ┃",
        overlay,
        "  ┃  Build · DeepSeek V4 Pro (New) OpenCode Go" + (" " * 40)
        + "feat/sacrificial-612",
    ])
    assert mesh._opencode_input(typed_wide.splitlines()) == "hello from a human"
    holding = "\n".join([
        "┃",
        "┃  check mesh: swarph_dm_unread",
        "┃",
        "┃  Build · DeepSeek V4 Pro (New) OpenCode Go",
    ])
    assert mesh._opencode_input(holding.splitlines()) == "check mesh: swarph_dm_unread"
    # A wide pane keeps the sidebar on the ▣ row, past the duration.
    sidebar = "▣  Build · DeepSeek V4.1 Flash · 5.1s" + (" " * 40) + "LSPs are disabled"
    assert mesh._OPENCODE_DONE.search(sidebar)
    assert mesh._opencode_running([sidebar]) is False
    assert mesh._opencode_running(["▣  Build · DeepSeek V4 Pro (New)"]) is True


def test_wide_empty_box_records_the_wake_outstanding(tmux):
    """W-D1: the footer showed up after Enter and the old read called the
    box busy, so the injected wake was reported as a human's line and
    wake_outstanding stayed unset."""
    from pathlib import Path
    wide = (Path(__file__).resolve().parent / "fixtures" / "opencode-961"
            / "W2_turn2.txt").read_text(encoding="utf-8")
    calls, state = tmux
    state["captures"] = [wide]
    st = _State()
    assert mesh.TmuxSink("sacW").deliver(st, [], 1) is True
    assert st.ledger("x")["wake_outstanding"] is True
    literals = [c for c in calls if "-l" in c]
    assert literals == [[
        "tmux", "send-keys", "-t", "sacW", "-l", mesh._OPENCODE_WAKE_PROMPT]]
    assert enters(calls) == 1


def test_opencode_idle_box_is_a_clear_composer(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_IDLE]
    assert mesh._composer_state("sac") == "clear"
    assert mesh._agent_running("sac") is False
    # `--auto` rewrites the mode row to "Build auto ·" (1.18.33).
    state["captures"] = [_OPENCODE_IDLE.replace("Build ·", "Build auto ·")]
    state["i"] = 0
    assert mesh._composer_state("sac") == "clear"


def test_opencode_unsubmitted_pointer_is_wake_not_unknown(tmux):
    """Main reads this pane as no composer at all, so the sink fails closed
    and the text stays typed. The box is a composer, and this line is wake."""
    calls, state = tmux
    state["captures"] = [_OPENCODE_HOLDING]
    assert mesh._composer_state("sac") == "wake"


def test_opencode_human_text_in_the_box_is_busy(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_HOLDING.replace(
        "check mesh: swarph_dm_unread", "look at the diff")]
    assert mesh._composer_state("sac") == "busy"


def test_opencode_transcript_above_the_spinner_is_not_the_composer(tmux):
    """The submitted prompt stays on screen above ▣. Reading it as the
    composer would send another Enter into the running turn."""
    calls, state = tmux
    state["captures"] = [_OPENCODE_RUNNING]
    assert mesh._composer_state("sac") == "clear"
    assert mesh._agent_running("sac") is True


def test_opencode_permission_modal_is_not_typed_into(tmux):
    """The dialog is a ┃ row. deliver returns None from the busy branch.
    False would be the unrecognised-pane fallthrough, which also sends no
    keys and is the wrong outcome."""
    calls, state = tmux
    assert "┃  △ Permission required" in _OPENCODE_PERMISSION
    assert "╹▀" not in _OPENCODE_PERMISSION
    assert "Build ·" not in _OPENCODE_PERMISSION
    state["captures"] = [_OPENCODE_PERMISSION]
    assert mesh._composer_state("sac") == "busy"
    sink = mesh.TmuxSink("sac")
    assert sink.deliver(_State(), [], 1) is None
    assert enters(calls) == 0
    assert not any("-l" in c for c in calls)


def test_opencode_finished_header_is_not_a_running_turn(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_FINISHED]
    assert mesh._agent_running("sac") is False
    assert mesh._opencode_turn_finished_target("sac") is True
    assert mesh._composer_state("sac") == "clear"


def test_opencode_deferral_keeps_wake_outstanding(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_RUNNING]
    st = _State()
    st.ledger("x")["wake_outstanding"] = True
    assert mesh.TmuxSink("sac").deliver(st, [], 2) is None
    assert st.ledger("x")["wake_outstanding"] is True
    assert enters(calls) == 0
    assert not any("-l" in c for c in calls)


def test_opencode_finished_turn_earns_another_wake(tmux, monkeypatch):
    """Inside the trust window a standing wake would report delivery with
    no keystroke. A finished header means the cell is idle again."""
    import time
    import swarph_cli.commands.watchdog as wd
    monkeypatch.setattr(wd, "_gateway_unread_count", lambda *a, **k: 1)
    calls, state = tmux
    state["captures"] = [_OPENCODE_FINISHED]
    st = _State()
    st.ledger("x")["wake_outstanding"] = True
    st.ledger("x")["last_wake_injected_at"] = time.time()
    assert mesh.TmuxSink("sac").deliver(st, [], 3) is True
    literals = [c for c in calls if "-l" in c]
    assert literals == [[
        "tmux", "send-keys", "-t", "sac", "-l", mesh._OPENCODE_WAKE_PROMPT]]
    assert enters(calls) == 1
    assert st.ledger("x")["wake_outstanding"] is True


def test_opencode_running_turn_gets_no_second_prompt(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_RUNNING]
    sink = mesh.TmuxSink("sac")
    assert sink.deliver(_State(), [], 1) is None
    assert enters(calls) == 0
    assert not any("-l" in c for c in calls)


def test_opencode_idle_injects_the_pointer_then_one_enter(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_IDLE, _OPENCODE_RUNNING]
    assert mesh._tmux_wake("sac") is True
    literals = [c for c in calls if "-l" in c]
    assert literals == [[
        "tmux", "send-keys", "-t", "sac", "-l", mesh._OPENCODE_WAKE_PROMPT]]
    assert enters(calls) == 1


def test_opencode_holding_wake_is_nudged_not_retyped(tmux):
    calls, state = tmux
    state["captures"] = [_OPENCODE_HOLDING]
    sink = mesh.TmuxSink("sac")
    assert sink.deliver(_State(), [], 1) is True
    assert enters(calls) == 1
    assert not any("-l" in c for c in calls)


def test_opencode_dropin_names_the_tmux_sink_and_is_not_the_template():
    """install-unit renders swarph-monitor@.service only. This file is the
    review copy of the opencode drop-in; nothing in this test installs it."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    dropin = root / "deploy" / "monitor" / "swarph-monitor@opencode.conf"
    text = dropin.read_text(encoding="utf-8")
    assert "ExecStart=" in text
    assert "--deliver tmux:opencode" in text
    assert "--deliver pull" in text
    assert "--as opencode" in text
    template = (root / "src" / "swarph_cli" / "systemd"
                / "swarph-monitor@.service").read_text(encoding="utf-8")
    assert "tmux:opencode" not in template
