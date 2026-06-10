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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
    assert model_calls[0] == call("lab", f"/model {_DEFAULT_STABLE_MODEL}")


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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
         patch("swarph_cli.commands.watchdog._pane_activity_age_sec", return_value=None), \
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
