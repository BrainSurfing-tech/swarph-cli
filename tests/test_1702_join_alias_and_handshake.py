"""Card #1702: join refuses onboard aliases, and a failed handshake still names onboard."""
from __future__ import annotations

import os
import sys

import pytest

from swarph_cli.commands.join import run_join
from swarph_shared.peer_registry import KNOWN_ALIASES

_DEVICE = "D" * 43
_TOKEN = "T" * 43


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.mark.parametrize("name", list(KNOWN_ALIASES) + ["new-cell"])
def test_join_refuses_onboard_aliases_before_any_request(name, monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args)
        return 500, {}, None

    monkeypatch.setattr("swarph_cli.commands.join._request", fake_request)
    rc = run_join(["http://100.64.0.1:8788", "--name", name])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    mapped = KNOWN_ALIASES.get(name)
    if isinstance(mapped, str) and mapped != name:
        assert rc == 1
        assert calls == []
        assert name in captured.err
        assert mapped in captured.err
        assert "onboard alias" in captured.err
    else:
        assert "onboard alias" not in text
        if os.name != "nt":
            assert calls
    assert _TOKEN not in text


def test_handshake_send_failure_prints_the_onboard_line(monkeypatch, tmp_path, capsys):
    if os.name == "nt":
        pytest.skip("POSIX credential storage")
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["north", "east", "south", "west"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def fake_request(url, method, path, body=None, token=None, timeout=15):
        if path == "/join/requests":
            return 201, {
                "device_code": _DEVICE,
                "user_code": "ABCD-EFGH",
                "expires_in": 900,
                "interval": 5,
            }, None
        if path == "/join/token":
            return 200, {
                "name": "new-cell",
                "peer_token": _TOKEN,
                "witness": "fixture-witness",
                "registered_unratified": True,
            }, None
        if path == "/whoami":
            return 200, {"peer": "new-cell"}, None
        if path == "/messages":
            return 500, {"error": "down"}, None
        return 404, {}, None

    monkeypatch.setattr("swarph_cli.commands.join._request", fake_request)
    rc = run_join(["http://100.64.0.1:8788", "--name", "new-cell"])
    captured = capsys.readouterr()
    token_path = tmp_path / ".config" / "swarph" / "new-cell.peer_token"
    line = (
        "swarph onboard new-cell --gateway http://100.64.0.1:8788 --token-file "
        + str(token_path)
    )
    assert rc == 1
    assert "Next, on this machine:" in captured.out
    assert line in captured.out
    assert "handshake send failed" in captured.err
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err
    assert token_path.is_file()
