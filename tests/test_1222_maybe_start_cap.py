"""Card #1222 — monitor standing-wake goes deaf: a wake injected into a pane that
then dies is held as `wake_outstanding`, and the maybe-starting detector never
releases it.

MEASURED on opencode, 2026-10-18 (monitor pid 1040939): the tmux:opencode ledger
held `wake_outstanding: true` with `last_wake_injected_at` 14:16:48Z and no wake
was injected again for ~8h. The pane had been recreated, so `_agent_running()`
read False and `_opencode_in_progress()` returned "maybe-starting" (bare header,
no interrupt hint) -> deliver() DEFERRED and kept the flag. The two-poll grace is
keyed on the pane fingerprint, and a churning footer re-armed `first_seen` on
every poll, so the verdict never matured to idle. Three offers never woke the cell.

Fails on origin/main, where a churning footer defers forever.
"""
from __future__ import annotations

import json
import time

import swarph_cli.commands.mesh as mesh

FF = [
    "▣  Build · DeepSeek V4 Pro (New)",
    "┃",
    "┃  Build · DeepSeek V4 Pro (New) OpenCode Go",
]
T0 = 1_790_000_000.0


def _churned(k: int) -> list[str]:
    """The booting frame with the footer region rewritten — what a recreated
    session (or any pane whose footer keeps moving) looks like every poll."""
    return FF + [f"┃  working… {k}"]


def test_churning_footer_matures_to_idle_past_the_hard_cap():
    """The observed deafness: the footer rewrites every poll, so first_seen is
    re-armed each time and the pane NEVER matures. Past the hard cap it must
    read idle, or the standing wake stands forever."""
    prior = None
    t = T0
    verdicts = []
    for k in range(0, 8):                      # 100 s apart -> 700 s total
        v, _fp, prior = mesh._opencode_poll_verdict(_churned(k), t, prior)
        verdicts.append(v)
        t += 100
    assert verdicts[0] == "maybe-starting", "first sight still defers"
    assert verdicts[-1] == "idle", (
        "a footer that churns forever must not defer forever (card #1222): "
        f"{verdicts}")


def test_progressing_within_the_cap_still_defers():
    """The reset-on-change behaviour for a genuinely booting turn is unchanged:
    inside the hard cap a moving footer re-arms maybe-starting with a fresh
    grace, while the cap stays anchored to the FIRST sighting."""
    _v, _fp, rec = mesh._opencode_poll_verdict(FF, T0, None)
    v, _fp, rec2 = mesh._opencode_poll_verdict(_churned(1), T0 + 61, rec)
    assert v == "maybe-starting"
    assert rec2["first_seen"] == T0 + 61        # fresh grace on change
    assert rec2["capped_since"] == T0           # cap anchored, not reset


def test_the_hard_cap_record_survives_a_restart():
    """The anchor lives in ledgers.json, not module memory: it round-trips
    through JSON and still matures a churning pane to idle."""
    _v, _fp, rec = mesh._opencode_poll_verdict(FF, T0, None)
    revived = json.loads(json.dumps({"opencode_maybe_start": rec}))
    v, _fp, _r = mesh._opencode_poll_verdict(
        _churned(3), T0 + mesh._MAYBE_START_MAX_S + 1,
        revived["opencode_maybe_start"])
    assert v == "idle"


def test_corrupt_prior_still_defers_never_idle():
    """A corrupt record must defer as first sight, never silently idle — the
    hard cap must not turn a corrupt entry into a false 'idle'."""
    for bad in ({"fingerprint": "x", "first_seen": T0, "capped_since": "soon"},
                {"capped_since": None}, "garbage", None, {}):
        v, _fp, _r = mesh._opencode_poll_verdict(FF, T0, bad)
        assert v == "maybe-starting", bad


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


def test_standing_wake_reinjects_when_the_pane_churns_past_the_cap(monkeypatch):
    """deliver(): wake_outstanding + a churning pane past the cap must NOT hold
    the standing wake forever. The pane reads idle at deliver() so the #616
    stale bound re-injects and types instead of deferring indefinitely."""
    import swarph_cli.commands.watchdog as wd

    monkeypatch.setattr(mesh, "_capture_pane_lines", lambda t: _churned(9))
    monkeypatch.setattr(mesh, "_agent_running", lambda t: False)
    monkeypatch.setattr(mesh, "_composer_state", lambda t: "clear")
    monkeypatch.setattr(mesh, "_tmux_session_created", lambda t: None)
    monkeypatch.setattr(mesh, "_grok_turn_finished_target", lambda t: False)
    monkeypatch.setattr(mesh, "_opencode_turn_finished_target", lambda t: False)
    monkeypatch.setattr(wd, "_gateway_unread_count", lambda *a, **k: 1)
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    state = _StubState()
    led = state.ledger("tmux:opencode")
    led["wake_outstanding"] = True
    led["last_wake_injected_at"] = time.time() - (mesh._WAKE_STALE_S + 100)
    led["opencode_maybe_start"] = {
        "fingerprint": "old", "first_seen": time.time() - 700,
        "capped_since": time.time() - 700}

    sink = mesh.TmuxSink("opencode")
    out = sink.deliver(state, [{"id": 7}], 7)
    assert out is True, (
        "standing wake held instead of re-injecting: "
        f"out={out!r} deferred_reason={sink.deferred_reason!r}")
    assert woke, "no keystroke was sent"
