"""Card 1582: join first on a new machine, then onboard.

Scratch HOME and a fake gateway only. No live token is read or printed.
"""
from __future__ import annotations

import json
import os

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


def _home(monkeypatch, tmp_path):
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential storage; Windows is refused first")
def test_successful_join_ends_on_the_onboard_step(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
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


def _write_join(tmp_path, gateway="http://100.64.0.1:8788", token=_TOKEN, name="new-cell"):
    token_path = tmp_path / ".config" / "swarph" / f"{name}.peer_token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n")
    token_path.chmod(0o600)
    sidecar = token_path.with_name(f"{name}.join.json")
    sidecar.write_text(json.dumps({
        "name": name,
        "gateway": gateway,
        "token_file": str(token_path),
    }) + "\n")
    return token_path


def test_onboard_uses_the_join_token_and_does_not_register_again(
        monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    _write_join(tmp_path)
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
    assert "Found the peer token saved by join for new-cell." in out
    assert "not registering a second time" in out
    assert "The given gateway differs from the gateway saved by join." in out
    assert _TOKEN not in out
    env = (tmp_path / "swarph_state" / "new-cell" / ".env.example").read_text()
    assert "MESH_GATEWAY_URL=http://100.64.0.1:9\n" in env
    assert (tmp_path / "swarph_state" / "new-cell" / "inbox.log").is_file()


def test_onboard_without_a_join_token_still_registers(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
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
    assert "Found the peer token saved by join" not in out
    assert "operator-token" not in out


def test_token_file_without_a_sidecar_still_registers(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "operator-token")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    token_path = tmp_path / ".config" / "swarph" / "new-cell.peer_token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_TOKEN + "\n")
    token_path.chmod(0o600)
    calls = []

    def fake_get(url, token):
        if url.endswith("/peers/new-cell"):
            return 404, {}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": "lab-ovh", "ratified": True}]}
        return 404, {}

    def fake_post(url, payload, token):
        calls.append((url, payload, token))
        return 200, {"registered_unratified": True, "token_status": "deferred"}

    monkeypatch.setattr(onboard, "_get_json", fake_get)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard([
        "new-cell", "--gateway", "http://100.64.0.1:9",
        "--url", "http://new-cell:9", "--capability", "provider=codex",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    posted = [item for item in calls if item[0].endswith("/peers/register")]
    assert len(posted) == 1
    assert posted[0][1]["url"] == "http://new-cell:9"
    assert posted[0][1]["capabilities"]["provider"] == "codex"
    assert posted[0][2] == _TOKEN
    assert "not applied" not in out
    assert "Found the peer token saved by join" not in out
    assert _TOKEN not in out


def test_sidecar_supplies_the_gateway_when_flag_and_env_are_empty(
        monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    _write_join(tmp_path, gateway="http://100.64.0.1:8788")
    posted = []
    monkeypatch.setattr(onboard, "_post_json", lambda *a, **k: posted.append(a))
    monkeypatch.setattr(onboard, "_get_json", lambda *a, **k: (500, {}))
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard(["new-cell"])
    out = capsys.readouterr().out
    assert rc == 0
    assert posted == []
    env = (tmp_path / "swarph_state" / "new-cell" / ".env.example").read_text()
    assert "MESH_GATEWAY_URL=http://100.64.0.1:8788\n" in env
    assert "MESH_GATEWAY_URL=\n" not in env
    assert _TOKEN not in out
    assert _TOKEN not in env


def test_skipped_url_and_capability_are_named(
        monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    _write_join(tmp_path)
    monkeypatch.setattr(onboard, "_post_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post")))
    monkeypatch.setattr(onboard, "_get_json", lambda *a, **k: (500, {}))
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard([
        "new-cell", "--url", "http://new-cell:9", "--capability", "provider=codex",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--url and --capability are not applied" in out
    assert _TOKEN not in out


def test_token_file_flag_wins_over_the_saved_join_file(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    _write_join(tmp_path)
    explicit = tmp_path / "operator.token"
    explicit.write_text("operator-token\n")
    explicit.chmod(0o600)
    calls = []

    def fake_get(url, token):
        if url.endswith("/peers/new-cell"):
            return 404, {}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": "lab-ovh", "ratified": True}]}
        return 404, {}

    def fake_post(url, payload, token):
        calls.append((url, token))
        return 200, {"registered_unratified": True, "token_status": "deferred"}

    monkeypatch.setattr(onboard, "_get_json", fake_get)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard([
        "new-cell", "--gateway", "http://100.64.0.1:9",
        "--token-file", str(explicit),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    posted = [item for item in calls if item[0].endswith("/peers/register")]
    assert posted and posted[0][1] == "operator-token"
    assert "Found the peer token saved by join" not in out
    assert "operator-token" not in out
    assert _TOKEN not in out


def test_empty_token_file_is_not_a_saved_join(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "operator-token")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    token_path = _write_join(tmp_path, token="")
    token_path.write_text("\n")
    calls = []

    def fake_get(url, token):
        if url.endswith("/peers/new-cell"):
            return 404, {}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": "lab-ovh", "ratified": True}]}
        return 404, {}

    def fake_post(url, payload, token):
        calls.append((url, token))
        return 200, {"registered_unratified": True, "token_status": "deferred"}

    monkeypatch.setattr(onboard, "_get_json", fake_get)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    rc = onboard.run_onboard(["new-cell", "--gateway", "http://100.64.0.1:9"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert rc == 0
    posted = [item for item in calls if item[0].endswith("/peers/register")]
    assert posted and posted[0][1] == "operator-token"
    assert "Found the peer token saved by join" not in out
    assert "not registering a second time" not in out
    assert "operator-token" not in out


def _quiet_onboard(monkeypatch):
    monkeypatch.setattr(
        onboard, "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post")),
    )
    monkeypatch.setattr(onboard, "_get_json", lambda *a, **k: (500, {}))
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)


def test_blank_gateway_flag_uses_the_sidecar(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    _write_join(tmp_path, gateway="http://100.64.0.1:8788")
    _quiet_onboard(monkeypatch)
    rc = onboard.run_onboard(["new-cell", "--gateway", ""])
    out = capsys.readouterr().out
    assert rc == 0
    env = (tmp_path / "swarph_state" / "new-cell" / ".env.example").read_text()
    assert "MESH_GATEWAY_URL=http://100.64.0.1:8788\n" in env
    assert "MESH_GATEWAY_URL=\n" not in env
    assert "differs" not in out
    assert _TOKEN not in out


def test_abbreviated_gateway_flag_is_honoured(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    _write_join(tmp_path, gateway="http://100.64.0.1:8788")
    _quiet_onboard(monkeypatch)
    rc = onboard.run_onboard(["new-cell", "--gate", "http://100.64.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 0
    env = (tmp_path / "swarph_state" / "new-cell" / ".env.example").read_text()
    assert "MESH_GATEWAY_URL=http://100.64.0.1:9\n" in env
    assert "http://100.64.0.1:8788" not in env
    assert "The given gateway differs from the gateway saved by join." in out
    assert _TOKEN not in out


def test_sidecar_gateway_with_a_newline_is_refused(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    _write_join(tmp_path, gateway="http://100.64.0.1:8788\nplanted")
    _quiet_onboard(monkeypatch)
    rc = onboard.run_onboard(["new-cell"])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert rc == 1
    assert "not a usable http(s) URL" in text
    assert not (tmp_path / "swarph_state" / "new-cell" / ".env.example").exists()
    assert "planted" not in text
    assert _TOKEN not in text


def test_sidecar_gateway_that_is_not_a_url_is_refused(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    _write_join(tmp_path, gateway="not a url")
    _quiet_onboard(monkeypatch)
    rc = onboard.run_onboard(["new-cell"])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert rc == 1
    assert "not a usable http(s) URL" in text
    assert not (tmp_path / "swarph_state" / "new-cell" / ".env.example").exists()


def test_sidecar_name_and_token_file_must_match(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "operator-token")
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    token_path = _write_join(tmp_path)
    sidecar = token_path.with_name("new-cell.join.json")
    calls = []

    def fake_get(url, token):
        if url.endswith("/peers/new-cell"):
            return 404, {}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": "lab-ovh", "ratified": True}]}
        return 404, {}

    def fake_post(url, payload, token):
        calls.append(url)
        return 200, {"registered_unratified": True, "token_status": "deferred"}

    monkeypatch.setattr(onboard, "_get_json", fake_get)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr("swarph_shared.verify_subscription_setup", lambda: True)
    sidecar.write_text(json.dumps({
        "name": "other-cell",
        "gateway": "http://100.64.0.1:8788",
        "token_file": str(token_path),
    }))
    rc = onboard.run_onboard(["new-cell", "--gateway", "http://100.64.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(url.endswith("/peers/register") for url in calls)
    assert "Found the peer token saved by join" not in out
    calls.clear()
    sidecar.write_text(json.dumps({
        "name": "new-cell",
        "gateway": "http://100.64.0.1:8788",
        "token_file": str(token_path.with_name("other.peer_token")),
    }))
    rc = onboard.run_onboard(["new-cell", "--gateway", "http://100.64.0.1:9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(url.endswith("/peers/register") for url in calls)
    assert "Found the peer token saved by join" not in out
    assert _TOKEN not in out
    assert "operator-token" not in out
