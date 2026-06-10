"""Tests for the watchdog A1.5 ``/model``-swap rung (autonomous engine-swap).

The A1.5 rung slots BETWEEN A1 (tmux send-keys wake) and A2 (full respawn).
When A1 has fired in this stale window (its marker matches the current
cursor_mtime) but the cursor has NOT advanced — the cell is genuinely stalled,
not merely idle — the watchdog injects ``/model <STABLE_MODEL>`` via the SAME
``tmux send-keys`` helper A1 uses, BEFORE escalating to A2 respawn.

Why this is a real recovery lever (the AI²-converged insight): a cell on a
launch-load frontier model can be classifier-degraded — its auto-mode
classifier model is unavailable, so Bash/Write/Edit are blocked and an A1
"check mesh" wake can't be acted on. ``/model`` is a CLI/TUI slash command,
NOT an agent tool call, so it is NOT subject to the auto-mode classifier;
injecting ``/model <stable>`` swaps the live session to a working engine and
unblocks tools — a lighter intervention than A2 respawn (preserves the session).

SAFETY (drop will trace these hardest):
  1. FIXED-TEMPLATE INJECTION — the payload is ``f"/model {STABLE_MODEL}"``
     where STABLE_MODEL is a hard-coded constant or the ``--stable-model``
     flag value. NEVER interpolated with peer DM / inbox / network input.
  2. NOT PEER-TRIGGERABLE — fires only on the local cursor-stall health signal
     (A1-exhausted + cursor not advanced), never on message content.
  3. HARMLESS IF ALREADY STABLE / IDLE — no-op on a cell already on the stable
     model; respects the existing F3 pane-activity gate + ``--no-respawn``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator
from unittest.mock import call, patch

import pytest

from swarph_cli.commands.watchdog import (
    _DEFAULT_STABLE_MODEL,
    _model_swap_marker_path,
    run_watchdog,
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch) -> Iterator[Path]:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    yield tmp_path


@pytest.fixture
def stale_cursor(isolated_state):
    """Cursor mtime 1hr in the past — session is stranded."""
    import os as _os

    cursor = isolated_state / "lab-cursor.json"
    cursor.write_text('{"last_msg_id": 100}')
    one_hour_ago = time.time() - 3600
    _os.utime(cursor, (one_hour_ago, one_hour_ago))
    return cursor


# ---------------------------------------------------------------------------
# Constant / default
# ---------------------------------------------------------------------------


def test_default_stable_model_is_known_stable():
    """The default stable model is the known-stable engine, not a frontier id."""
    assert _DEFAULT_STABLE_MODEL == "claude-opus-4-8"


def test_model_swap_marker_distinct_from_a1_marker(isolated_state):
    """A1.5 records its own marker, distinct from the A1 marker, so the two
    rungs don't clobber each other's same-window state."""
    from swarph_cli.commands.watchdog import _a1_marker_path

    log_path = isolated_state / "wd.log"
    a1 = _a1_marker_path(log_path, "lab", "lab")
    a15 = _model_swap_marker_path(log_path, "lab", "lab")
    assert a1 != a15


# ---------------------------------------------------------------------------
# Escalation ORDER: A1 → A1.5 → A2
# ---------------------------------------------------------------------------


def test_a1_fires_first_not_model_rung(isolated_state, stale_cursor):
    """On the FIRST stale tick, A1 (wake) fires — NOT the model rung. The
    model rung only fires after A1 has exhausted (its marker matches)."""
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60",
        ])
    assert rc == 1  # A1
    # Exactly one send-keys, and it's the A1 wake prompt — NOT /model.
    send_mock.assert_called_once()
    sent_text = send_mock.call_args[0][1]
    assert sent_text.startswith("watchdog wake")
    assert not sent_text.startswith("/model")


def test_a1_exhausted_cursor_stale_fires_model_rung(isolated_state, stale_cursor):
    """SECOND tick (cursor unchanged ⇒ A1 marker matches ⇒ A1 exhausted): the
    model rung fires — injects ``/model <stable>`` — instead of plain noop."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        # Tick 1 → A1
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        # Tick 2 → A1.5 model-swap (A1 already fired this window)
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab",
            "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    assert rc1 == 1   # A1
    assert rc2 == 5   # A1.5 model-swap
    # Two send-keys total: A1 wake, then the /model injection.
    assert send_mock.call_count == 2
    second = send_mock.call_args_list[1][0][1]
    assert second == f"/model {_DEFAULT_STABLE_MODEL}"


def test_model_rung_exhausted_escalates_to_a2(isolated_state, stale_cursor):
    """THIRD tick (cursor STILL unchanged after the model-swap was tried):
    the model rung is itself exhausted ⇒ escalate to A2 respawn."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        for _ in range(3):
            rc = run_watchdog(argv=[
                "--check", "--cell", "lab",
                "--cursor", str(stale_cursor),
                "--threshold", "60", "--log", str(log_path),
            ])
    assert rc == 2  # A2 on the third tick
    spawn_mock.assert_called_once()


def test_cursor_advance_after_model_rung_deescalates(isolated_state, stale_cursor):
    """If the cursor advances after the model-swap (the engine swap recovered
    the cell), the watchdog de-escalates — NO A2 respawn."""
    import os as _os

    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        # Tick 1 → A1
        run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        # Tick 2 → A1.5 model-swap
        run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        # Cursor advances (recovered) — still stale vs 60s threshold but a NEW window.
        new_mtime = time.time() - 480
        _os.utime(stale_cursor, (new_mtime, new_mtime))
        # Tick 3 → must NOT be A2; the new window starts the ladder over at A1.
        rc3 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    assert rc3 == 1  # back to A1 (fresh window), NOT A2
    spawn_mock.assert_not_called()


# ---------------------------------------------------------------------------
# SAFETY 1 — fixed-template injection, no peer data reaches the payload
# ---------------------------------------------------------------------------


def test_injected_payload_is_exactly_fixed_template(isolated_state, stale_cursor):
    """The injected string is EXACTLY ``/model <stable>`` — a fixed template."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        for _ in range(2):
            run_watchdog(argv=[
                "--check", "--cell", "lab", "--cursor", str(stale_cursor),
                "--threshold", "60", "--log", str(log_path),
            ])
    model_calls = [
        c for c in send_mock.call_args_list if c[0][1].startswith("/model")
    ]
    assert len(model_calls) == 1
    # clear_input=True is part of the fixed contract (drop C2): C-u precedes
    # the payload so it can never concatenate onto a half-typed buffer.
    assert model_calls[0] == call(
        "lab", f"/model {_DEFAULT_STABLE_MODEL}", clear_input=True
    )


def test_peer_message_content_cannot_reach_injection(isolated_state, stale_cursor):
    """Feed adversarial peer/DM content through the unread-count + recovery-event
    paths and assert the injected /model string is UNCHANGED. A peer must not be
    able to interpolate anything into the tmux payload by construction."""
    log_path = isolated_state / "wd.log"
    evil = "; rm -rf / ; /model evil-model"
    # Both the unread-count and the recovery-event readers return attacker-shaped
    # data — none of it may reach the injection payload.
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._gateway_recent_recovery_event",
               return_value={"event_type": evil, "time": evil}), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        for _ in range(2):
            run_watchdog(argv=[
                "--check", "--cell", "lab", "--cursor", str(stale_cursor),
                "--threshold", "60", "--log", str(log_path),
            ])
    model_calls = [
        c for c in send_mock.call_args_list if "/model" in c[0][1]
    ]
    assert len(model_calls) == 1
    # The injected payload is the fixed template ONLY — the attacker string
    # never appears in it.
    payload = model_calls[0][0][1]
    assert payload == f"/model {_DEFAULT_STABLE_MODEL}"
    assert evil not in payload
    assert "rm -rf" not in payload


# ---------------------------------------------------------------------------
# SAFETY 3 — respects the F3 pane-activity gate
# ---------------------------------------------------------------------------


def test_model_rung_skipped_when_pane_active(isolated_state, stale_cursor):
    """A1.5 must respect F3: if the pane shows recent activity (session is
    working), the model rung is suppressed just like A1 is."""
    log_path = isolated_state / "wd.log"
    # Tick 1: pane idle → A1 fires (arms the A1 marker).
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True):
        run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    # Tick 2: A1 marker matches (would be A1.5) BUT pane is now active → suppress.
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=30), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--pane-activity-threshold", "600",
            "--log", str(log_path),
        ])
    assert rc == 0  # suppressed, no rung action
    send_mock.assert_not_called()  # no /model injected while pane active


# ---------------------------------------------------------------------------
# Flags — --stable-model override + --no-model-rung disable
# ---------------------------------------------------------------------------


def test_stable_model_flag_overrides_default(isolated_state, stale_cursor):
    """``--stable-model <id>`` overrides the default; the injected payload uses
    the override, still as a fixed ``/model <id>`` template."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        for _ in range(2):
            run_watchdog(argv=[
                "--check", "--cell", "lab", "--cursor", str(stale_cursor),
                "--threshold", "60", "--stable-model", "claude-haiku-4-6",
                "--log", str(log_path),
            ])
    model_calls = [c for c in send_mock.call_args_list if c[0][1].startswith("/model")]
    assert len(model_calls) == 1
    assert model_calls[0][0][1] == "/model claude-haiku-4-6"


def test_no_model_rung_falls_straight_a1_to_a2(isolated_state, stale_cursor):
    """``--no-model-rung`` disables the rung: A1 → (A1 exhausted) → A2 directly,
    with no ``/model`` injection in between. This is the pre-A1.5 behavior."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        # Tick 1 → A1.
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--no-model-rung", "--log", str(log_path),
        ])
        # Tick 2 → with the rung disabled, A1-already-fired is a plain noop
        # (the legacy same-window suppression), NOT a model-swap.
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--no-model-rung", "--log", str(log_path),
        ])
    assert rc1 == 1
    assert rc2 == 0  # legacy F1 noop, NOT 5 (model-swap)
    # No /model was ever injected.
    assert all(not c[0][1].startswith("/model") for c in send_mock.call_args_list)


# ---------------------------------------------------------------------------
# SAFETY 4 — fail-safe-to-A2 (drop seat-A BLOCK-1 / BLOCK-2, PR #58 review)
# A failure in the rung's BOUNDING mechanism must fail toward respawn,
# never open toward unbounded /model re-injection.
# ---------------------------------------------------------------------------


def test_send_failure_escalates_to_a2_same_tick(isolated_state, stale_cursor):
    """BLOCK-1: a FAILED /model send (wedged / timing-out pane) escalates to
    A2 in the SAME tick. The wedged pane IS the respawn case — re-looping
    A1.5 every tick would leave A2 permanently unreachable."""
    log_path = isolated_state / "wd.log"

    def send(session, text, **kwargs):
        # A1 wake goes through; the /model inject fails (wedged pane).
        return not text.startswith("/model")

    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", side_effect=send) as send_mock, \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    assert rc1 == 1   # A1
    assert rc2 == 2   # A2 — same tick as the failed inject, NOT rc=4 re-loop
    spawn_mock.assert_called_once()
    model_attempts = [
        c for c in send_mock.call_args_list if c[0][1].startswith("/model")
    ]
    assert len(model_attempts) == 1  # exactly one inject attempt, no re-loop


def test_marker_write_failure_escalates_to_a2(isolated_state, stale_cursor):
    """BLOCK-2: send SUCCEEDS but the A1.5 marker stamp does not persist —
    the once-per-window bound is gone, so escalate to A2 in the SAME tick
    rather than re-injecting /model unboundedly on subsequent ticks."""
    from swarph_cli.commands import watchdog as wd

    log_path = isolated_state / "wd.log"
    model_marker = _model_swap_marker_path(log_path, "lab", "lab")
    real_record = wd._record_a1_fired

    def record(marker, mtime):
        if marker == model_marker:
            return  # the swallowed-OSError shape: stamp silently lost
        real_record(marker, mtime)

    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock, \
         patch("swarph_cli.commands.watchdog._record_a1_fired", side_effect=record), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    assert rc1 == 1
    assert rc2 == 2   # verify-after-write failed -> A2 same tick, NOT rc=5
    spawn_mock.assert_called_once()
    model_attempts = [
        c for c in send_mock.call_args_list if c[0][1].startswith("/model")
    ]
    assert len(model_attempts) == 1  # the bound held despite the lost stamp


def test_marker_oserror_real_path_escalates_to_a2(isolated_state, stale_cursor):
    """BLOCK-2 end-to-end: drive the GENUINE _record_a1_fired OSError-swallow
    path (read-only state dir — no mocks on the marker helpers) and assert
    the rung still fails safe to A2 with exactly one inject."""
    import os as _os

    state_dir = isolated_state / "wdstate"
    state_dir.mkdir()
    log_path = state_dir / "wd.log"
    common = ["--check", "--cell", "lab", "--cursor", str(stale_cursor),
              "--threshold", "60", "--log", str(log_path)]
    try:
        with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
             patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
             patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
             patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
             patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock, \
             patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
            rc1 = run_watchdog(argv=common)   # A1 — markers/log created writable
            _os.chmod(state_dir, 0o555)       # now the a15 stamp CANNOT persist
            rc2 = run_watchdog(argv=common)
    finally:
        _os.chmod(state_dir, 0o755)
    assert rc1 == 1
    assert rc2 == 2   # real OSError swallowed in stamp -> verify fails -> A2
    spawn_mock.assert_called_once()
    model_attempts = [
        c for c in send_mock.call_args_list if c[0][1].startswith("/model")
    ]
    assert len(model_attempts) == 1


def test_malformed_stable_model_falls_back_to_default(isolated_state, stale_cursor):
    """Allowlist (drop nit 2): a --stable-model value that does not look like
    a model id never reaches the TUI — the payload falls back to the
    known-good default (typo'd config must not block recovery either)."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        for _ in range(2):
            run_watchdog(argv=[
                "--check", "--cell", "lab", "--cursor", str(stale_cursor),
                "--threshold", "60", "--log", str(log_path),
                "--stable-model", "evil; rm -rf /",
            ])
    model_calls = [c for c in send_mock.call_args_list if c[0][1].startswith("/model")]
    assert len(model_calls) == 1
    assert model_calls[0][0][1] == f"/model {_DEFAULT_STABLE_MODEL}"
    assert "evil" not in model_calls[0][0][1]


# ---------------------------------------------------------------------------
# SAFETY 3b — pane-state precondition for the slash-command rung (drop C2)
# ---------------------------------------------------------------------------


def test_a15_skipped_when_pane_state_unreadable(isolated_state, stale_cursor):
    """When tmux cannot report pane_activity (None — missing/ancient tmux),
    A1's prose wake still falls through, but the SLASH-COMMAND rung must NOT
    inject into an unverifiable pane: the window behaves as --no-model-rung."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        rc1 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
        rc2 = run_watchdog(argv=[
            "--check", "--cell", "lab", "--cursor", str(stale_cursor),
            "--threshold", "60", "--log", str(log_path),
        ])
    assert rc1 == 1   # A1 wake still fires on unreadable pane state (prose)
    assert rc2 == 0   # A1.5 SKIPPED — no blind slash-command inject
    assert all(
        not c[0][1].startswith("/model") for c in send_mock.call_args_list
    )


def test_a15_send_includes_clear_input(isolated_state, stale_cursor):
    """The /model inject always carries clear_input=True (C-u prefix) so a
    half-typed input buffer can never corrupt the slash command (drop C2)."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock:
        for _ in range(2):
            run_watchdog(argv=[
                "--check", "--cell", "lab", "--cursor", str(stale_cursor),
                "--threshold", "60", "--log", str(log_path),
            ])
    model_calls = [
        c for c in send_mock.call_args_list if c[0][1].startswith("/model")
    ]
    assert len(model_calls) == 1
    assert model_calls[0].kwargs.get("clear_input") is True
    # The A1 wake (prose) does NOT need the clear prefix — unchanged contract.
    wake_calls = [
        c for c in send_mock.call_args_list if c[0][1].startswith("watchdog wake")
    ]
    assert wake_calls and all(
        not c.kwargs.get("clear_input") for c in wake_calls
    )


# ---------------------------------------------------------------------------
# Pane targeting — send to the claude pane, never a shell pane (drop N3)
# ---------------------------------------------------------------------------


def _panes_result(stdout, rc=0):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.returncode = rc
    m.stdout = stdout
    return m


def test_resolve_send_target_prefers_claude_pane():
    """Multi-pane session with a bash pane listed first: the claude/node pane
    wins — /model must never land in a shell."""
    from swarph_cli.commands.watchdog import _resolve_send_target

    with patch(
        "swarph_cli.commands.watchdog.subprocess.run",
        return_value=_panes_result("%1 bash\n%2 node\n%3 tail\n"),
    ):
        assert _resolve_send_target("lab") == "%2"


def test_resolve_send_target_falls_back_to_session():
    """No claude-shaped pane (or tmux failure) → the session name passes
    through unchanged: behavior identical to pre-N3."""
    from swarph_cli.commands.watchdog import _resolve_send_target

    with patch(
        "swarph_cli.commands.watchdog.subprocess.run",
        return_value=_panes_result("%1 bash\n%2 vim\n"),
    ):
        assert _resolve_send_target("lab") == "lab"
    with patch(
        "swarph_cli.commands.watchdog.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        assert _resolve_send_target("lab") == "lab"
    with patch(
        "swarph_cli.commands.watchdog.subprocess.run",
        return_value=_panes_result("", rc=1),
    ):
        assert _resolve_send_target("lab") == "lab"


def test_send_keys_targets_resolved_pane():
    """_tmux_send_keys sends to the RESOLVED pane id, with C-u preceding the
    payload when clear_input=True."""
    from swarph_cli.commands import watchdog as wd

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "list-panes":
            return _panes_result("%1 bash\n%2 claude\n")
        return _panes_result("", rc=0)

    with patch("swarph_cli.commands.watchdog.subprocess.run", side_effect=fake_run):
        ok = wd._tmux_send_keys("lab", "/model claude-opus-4-8", clear_input=True)
    assert ok
    send = [c for c in calls if c[1] == "send-keys"][0]
    target = send[send.index("-t") + 1]
    assert target == "%2"                       # the claude pane, not the session
    assert send[send.index("-t") + 2] == "C-u"  # clear precedes the payload


# ---------------------------------------------------------------------------
# Circuit breakers — C1 thrash + C3 respawn cap (drop seat-A, cross-window)
# ---------------------------------------------------------------------------


def _tick(cursor, log_path, *extra):
    return run_watchdog(argv=[
        "--check", "--cell", "lab", "--cursor", str(cursor),
        "--threshold", "60", "--log", str(log_path), *extra,
    ])


def _set_window(cursor, mtime):
    import os as _os
    _os.utime(cursor, (mtime, mtime))


def test_flapping_cursor_trips_thrash_circuit(isolated_state, stale_cursor):
    """C1: a cursor that ADVANCES each window without real progress restarts
    the ladder forever — per-window bounds alone let A1.5 fire once per
    window, unbounded, with A2 never engaging. After max-swaps within the
    window, the next would-be swap escalates to A2 instead."""
    log_path = isolated_state / "wd.log"
    base = time.time() - 3600
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True) as send_mock, \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        rcs = []
        for w in range(3):                      # three flapping windows
            _set_window(stale_cursor, base + w * 10)
            rcs.append(_tick(stale_cursor, log_path))   # A1
            rcs.append(_tick(stale_cursor, log_path))   # A1.5 or circuit
    # W1, W2: swap fired. W3: thrash circuit -> A2 instead of a third swap.
    assert rcs == [1, 5, 1, 5, 1, 2]
    model_count = sum(
        1 for c in send_mock.call_args_list if c[0][1].startswith("/model")
    )
    assert model_count == 2                     # never a third swap
    spawn_mock.assert_called_once()             # A2 engaged exactly once


def test_a2_circuit_opens_after_max_respawns(isolated_state, stale_cursor):
    """C3: when respawns themselves don't recover the cell (both engines
    degraded), the ladder must not respawn-churn forever — after max
    respawns within the window the circuit OPENS (exit 6, no spawn)."""
    log_path = isolated_state / "wd.log"
    base = time.time() - 3600
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        finals = []
        for w in range(4):                      # four degraded windows
            _set_window(stale_cursor, base + w * 10)
            last = 0
            for _ in range(3):                  # drive each window to its end
                last = _tick(stale_cursor, log_path)
                if last in (2, 6):
                    break
            finals.append(last)
    # W1-W3 end in A2 respawns; W4 the circuit is open: exit 6, NO 4th spawn.
    assert finals[:3] == [2, 2, 2]
    assert finals[3] == 6
    assert spawn_mock.call_count == 3


def test_respawn_circuit_resets_outside_window(isolated_state, stale_cursor):
    """The circuit is a WINDOW, not a lifetime cap: respawns older than the
    window age out and A2 becomes available again."""
    log_path = isolated_state / "wd.log"
    base = time.time() - 3600
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True) as spawn_mock:
        # Tight window (1s) so prior respawns age out between windows.
        for w in range(4):
            _set_window(stale_cursor, base + w * 10)
            last = 0
            for _ in range(3):
                last = _tick(stale_cursor, log_path,
                             "--a2-respawn-window-sec", "1")
                if last in (2, 6):
                    break
            time.sleep(1.1)                     # age the respawn out
            assert last == 2                    # never opens: history expired
    assert spawn_mock.call_count == 4


# ---------------------------------------------------------------------------
# N1 observability — --notify-peer mesh emit (drop seat-A)
# ---------------------------------------------------------------------------


def test_notify_peer_default_off_no_dm(isolated_state, stale_cursor):
    """Without --notify-peer, NO mesh DM fires on a swap — current behavior
    unchanged by default."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._dm_wake", return_value=True) as dm_mock:
        for _ in range(2):
            _tick(stale_cursor, log_path)
    dm_mock.assert_not_called()


def test_notify_peer_emits_fixed_template_on_swap(isolated_state, stale_cursor, monkeypatch):
    """--notify-peer + token: the swap emits ONE mesh DM whose content is the
    fixed template (config-validated stable model only — adversarial
    recovery-event content can never reach it)."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    log_path = isolated_state / "wd.log"
    evil = "; rm -rf / ; injected"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._gateway_recent_recovery_event",
               return_value={"event_type": evil, "time": evil}), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._dm_wake", return_value=True) as dm_mock:
        for _ in range(2):
            _tick(stale_cursor, log_path, "--notify-peer", "lab-ovh")
    assert dm_mock.call_count == 1
    content = dm_mock.call_args[0][4]
    assert "event=a15_model_swap" in content
    assert _DEFAULT_STABLE_MODEL in content
    assert evil not in content                  # fixed-template discipline
    assert dm_mock.call_args[0][2] == "lab-ovh"  # target peer from the flag


def test_notify_peer_without_token_skips_quietly(isolated_state, stale_cursor):
    """--notify-peer but no MESH_GATEWAY_TOKEN: skip the emit, never raise,
    ladder behavior unchanged (best-effort by construction)."""
    log_path = isolated_state / "wd.log"
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._dm_wake", return_value=True) as dm_mock:
        rc1 = _tick(stale_cursor, log_path, "--notify-peer", "lab-ovh")
        rc2 = _tick(stale_cursor, log_path, "--notify-peer", "lab-ovh")
    assert (rc1, rc2) == (1, 5)                 # ladder unchanged
    dm_mock.assert_not_called()


def test_notify_peer_emits_on_circuit_open(isolated_state, stale_cursor, monkeypatch):
    """The circuit-open HOLD is precisely the event an operator must see —
    assert the emit fires there too."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    log_path = isolated_state / "wd.log"
    base = time.time() - 3600
    with patch("swarph_cli.commands.watchdog._process_alive", return_value=True), \
         patch("swarph_cli.commands.watchdog._gateway_unread_count", return_value=3), \
         patch("swarph_cli.commands.watchdog._tmux_session_exists", return_value=True), \
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=99999), \
         patch("swarph_cli.commands.watchdog._tmux_send_keys", return_value=True), \
         patch("swarph_cli.commands.watchdog._spawn_via_swarph", return_value=True), \
         patch("swarph_cli.commands.watchdog._dm_wake", return_value=True) as dm_mock:
        last = 0
        for w in range(4):
            _set_window(stale_cursor, base + w * 10)
            for _ in range(3):
                last = _tick(stale_cursor, log_path, "--notify-peer", "lab-ovh")
                if last in (2, 6):
                    break
    assert last == 6
    circuit_emits = [
        c for c in dm_mock.call_args_list if "event=a2_circuit_open" in c[0][4]
    ]
    assert len(circuit_emits) == 1
