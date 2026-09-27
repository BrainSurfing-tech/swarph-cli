"""Card #184 — a local step is not a delivery.

On current main, tmux rc==0 is success even when the composer still holds
the payload, and a 2xx from POST /messages is recorded as a sent wake.
"""
from __future__ import annotations

from pathlib import Path

from swarph_cli.commands import watchdog as wd


class _R:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout


def _claude_pane(composer: str) -> str:
    """Live science-claude shape, 2026-09-27: ❯ composer, rule, footer below."""
    return (
        "────────────────────────────────────────── cell ─\n"
        f"{composer}\n"
        "───────────────────────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on · 1 shell, 1 monitor\n"
        "                   ✔ Update installed · Restart to update\n"
    )


def _send(monkeypatch, pane, payload, capture_rc=0):
    monkeypatch.setattr(wd, "_SUBMIT_SETTLE_S", 0, raising=False)

    def fake_run(argv, **kwargs):
        if argv[1] == "list-panes":
            return _R(stdout="%9 claude\n")
        if argv[1] == "capture-pane":
            return _R(rc=capture_rc, stdout=pane)
        return _R(rc=0)

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    return wd._tmux_send_keys("lab", payload)


def test_payload_still_in_composer_is_not_success(monkeypatch):
    payload = "watchdog wake still sitting"
    pane = _claude_pane(f"❯ {payload}")
    assert "✔ Update installed" in pane
    assert _send(monkeypatch, pane, payload) is False


def test_empty_composer_after_submit_is_success(monkeypatch):
    # The measured footer contains these words. The last-line check treats
    # that footer as the composer and reports the wake still held.
    payload = "Update installed"
    assert _send(monkeypatch, _claude_pane("❯"), payload) is True


def test_payload_wrapped_over_two_composer_lines_is_not_success(monkeypatch):
    payload = "watchdog wake still sitting in the composer"
    pane = _claude_pane("❯ watchdog wake still\n  sitting in the composer")
    assert _send(monkeypatch, pane, payload) is False


def test_payload_wrapped_inside_a_token_is_not_success(monkeypatch):
    # A terminal wraps at the column, so 'lab-ovh' can split as 'la' / 'b-ovh'.
    # Joining those lines with a space hides the token and reads as delivered.
    payload = "lab-ovh"
    pane = _claude_pane("❯ la\n  b-ovh")
    assert "✔ Update installed" in pane
    assert _send(monkeypatch, pane, payload) is False


def test_payload_echoed_in_history_with_empty_composer_is_success(monkeypatch):
    payload = "watchdog wake still sitting"
    pane = _claude_pane(f"echoed {payload} above the prompt\n❯")
    assert _send(monkeypatch, pane, payload) is True


def test_unreadable_pane_is_not_success(monkeypatch):
    assert _send(monkeypatch, "", "watchdog wake still sitting", capture_rc=1) is False


def test_dm_wake_2xx_is_not_a_delivery(monkeypatch):
    monkeypatch.setattr(wd, "_post_json", lambda *a, **k: (200, {"id": 1}))
    result = wd._dm_wake("http://gw:8788", "lab", "gpu-wsl", "tok", "wake")
    assert result is not True


def test_notify_peer_does_not_record_sent_on_gateway_accept(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(wd, "_post_json", lambda *a, **k: (200, {"id": 1}))
    args = wd._build_parser().parse_args(
        ["--check", "--cell", "lab", "--notify-peer", "gpt-ops"]
    )
    diag: dict = {}
    wd._notify_peer_event(args, "lab", "a15_model_swap", "engine swapped", diag)
    assert diag.get("notify_sent") in (None, False)
    assert diag.get("notify_accepted") is True


def test_dm_scan_does_not_count_gateway_accept_as_fired(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(
        wd, "_fetch_peers",
        lambda *a, **k: [{"name": "droplet", "last_health": "2020-01-01T00:00:00+00:00"}],
    )
    monkeypatch.setattr(wd, "_post_json", lambda *a, **k: (200, {"id": 9}))
    args = wd._build_parser().parse_args(
        ["--check", "--cell", "lab", "--dm-wake", "--threshold", "60"]
    )
    args.dm_wake_state_path = str(Path(tmp_path) / "dmstate.json")
    log_path = Path(tmp_path) / "wd.log"
    assert wd._dm_wake_scan(args, log_path, now_epoch=1_700_000_000) == 0
