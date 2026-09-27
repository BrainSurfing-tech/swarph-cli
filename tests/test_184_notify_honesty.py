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


def test_tmux_send_keys_rc0_is_not_success_while_composer_holds_the_payload(monkeypatch):
    monkeypatch.setattr(wd, "_SUBMIT_SETTLE_S", 0, raising=False)
    payload = "watchdog wake still sitting"

    def fake_run(argv, **kwargs):
        if argv[1] == "list-panes":
            return _R(stdout="%9 claude\n")
        if argv[1] == "capture-pane":
            return _R(stdout=f"> {payload}\n")
        return _R(rc=0)

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    assert wd._tmux_send_keys("lab", payload) is False


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
