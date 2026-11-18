"""Card 1582: join first on a new machine, then onboard.

Scratch HOME and a fake gateway only. No live token is read or printed.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import join, onboard


_ORDER = "Join first on a new machine, then onboard."
_TOKEN = "a" * 43
_DEVICE = "b" * 43


def _help(runner, argv):
    with pytest.raises(SystemExit) as caught:
        runner(argv)
    assert caught.value.code == 0


def test_join_help_states_the_order(capsys):
    _help(join.run_join, ["--help"])
    assert _ORDER in capsys.readouterr().out


def test_onboard_help_states_the_order(capsys):
    _help(onboard.run_onboard, ["--help"])
    assert _ORDER in capsys.readouterr().out


def test_successful_join_ends_on_the_onboard_step(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)

    def fake_request(url, method, path, body=None, token=None, timeout=15):
        if path == "/join/requests":
            return 201, {
                "device_code": _DEVICE,
                "user_code": "ABCD-EFGH",
                "expires_in": 900,
                "interval": 5,
            }, 5
        if path == "/join/token":
            return 200, {
                "peer_token": _TOKEN,
                "name": "new-cell",
                "registered_unratified": True,
                "witness": "lab-ovh",
            }, 5
        if path == "/whoami":
            assert token == _TOKEN
            return 200, {"peer": "new-cell"}, 5
        raise AssertionError(path)

    monkeypatch.setattr(join, "_request", fake_request)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = join.run_join(["--name", "new-cell", "http://100.64.0.1:8788"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-2] == "swarph onboard new-cell"
    assert lines[-1] == (
        "Onboard adds the cell scaffold and the handshake to a witness, then ratify."
    )
    assert _TOKEN not in out
    saved = (tmp_path / ".config" / "swarph" / "new-cell.peer_token").read_text()
    assert saved.strip() == _TOKEN


def test_onboard_uses_the_join_token_and_does_not_register_again(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    token_path = tmp_path / ".config" / "swarph" / "new-cell.peer_token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_TOKEN + "\n")
    token_path.chmod(0o600)
    posted = []

    def forbid_post(*args, **kwargs):
        posted.append(args)
        raise AssertionError("register must not run when join saved the token")

    monkeypatch.setattr(onboard, "_post_json", forbid_post)
    monkeypatch.setattr(onboard, "_get_json", lambda *a, **k: (500, {}))
    monkeypatch.setattr(
        "swarph_shared.verify_subscription_setup",
        lambda: True,
    )
    rc = onboard.run_onboard(["new-cell", "--gateway", "http://100.64.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert posted == []
    assert "Using the peer token saved by join for new-cell." in out
    assert "not registering a second time" in out
    assert _TOKEN not in out
    assert (tmp_path / "swarph_state" / "new-cell" / "inbox.log").is_file()


def test_onboard_without_a_join_token_still_registers(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "operator-token")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    calls = []

    def fake_get(url, token):
        calls.append(("GET", url))
        if url.endswith("/peers/new-cell"):
            return 404, {}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": "lab-ovh", "ratified": True}]}
        return 404, {}

    def fake_post(url, payload, token):
        calls.append(("POST", url))
        assert token == "operator-token"
        assert payload["name"] == "new-cell"
        return 200, {"registered_unratified": True, "token_status": "deferred"}

    monkeypatch.setattr(onboard, "_get_json", fake_get)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard(["new-cell", "--gateway", "http://100.64.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(method == "POST" and url.endswith("/peers/register")
               for method, url in calls)
    assert "Using the peer token saved by join" not in out
    assert "operator-token" not in out
