"""Tests for ``swarph onboard`` — mocks HTTP + filesystem.

Live falsifiability gate (synthetic ``onboard-smoke`` peer end-to-end
against the deployed mesh-gateway PR A) lives in
``test_smoke_phase_5_5.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from swarph_cli.commands import onboard


# ---------------------------------------------------------------------------
# _resolve_token — env / secrets.toml / prompt fallback
# ---------------------------------------------------------------------------


def test_resolve_token_from_env(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "from-env-tok")
    assert onboard._resolve_token(None) == "from-env-tok"


def test_resolve_token_from_secrets_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    secrets = tmp_path / "secrets.toml"
    secrets.write_text("MESH_GATEWAY_TOKEN=from-file-tok\n")
    secrets.chmod(0o600)
    assert onboard._resolve_token(str(secrets)) == "from-file-tok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode bits not representable on Windows; chmod(0o644) is a no-op so the loose-mode warning never fires")
def test_resolve_token_warns_on_loose_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    secrets = tmp_path / "secrets.toml"
    secrets.write_text("MESH_GATEWAY_TOKEN=tok\n")
    secrets.chmod(0o644)  # too-permissive
    onboard._resolve_token(str(secrets))
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "0o644" in err


def test_resolve_token_falls_back_to_prompt(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(onboard, "getpass", lambda _: "prompted-tok")
    fake = tmp_path / "no-such-file.toml"
    out = onboard._resolve_token(str(fake))
    assert out == "prompted-tok"
    err = capsys.readouterr().err
    assert "secrets.toml shape" in err  # operator learns the pattern


# ---------------------------------------------------------------------------
# _parse_capability — KEY=VALUE parsing
# ---------------------------------------------------------------------------


def test_parse_capability_bool():
    k, v = onboard._parse_capability("can_claim_tasks=true")
    assert k == "can_claim_tasks"
    assert v is True


def test_parse_capability_string():
    k, v = onboard._parse_capability("role=witness")
    assert k == "role"
    assert v == "witness"


def test_parse_capability_int():
    k, v = onboard._parse_capability("timeout=30")
    assert v == 30


def test_parse_capability_rejects_unkv():
    with pytest.raises(Exception):
        onboard._parse_capability("not-kv-shape")


# ---------------------------------------------------------------------------
# run_onboard — full pipeline with mocked HTTP + filesystem
# ---------------------------------------------------------------------------


def _mock_post_factory(*, register_status=200, register_body=None):
    """Return a _post_json replacement that captures calls + returns
    a scripted response."""
    captured = []
    if register_body is None:
        register_body = {
            "status": "registered",
            "name": "test-peer",
            "registered_at": "2026-05-08T20:00:00Z",
            "ratified": False,
            "registered_unratified": True,
        }

    def fake_post(url, body, token, *, method="POST"):
        captured.append({"url": url, "body": body, "method": method})
        return register_status, register_body

    return fake_post, captured


def test_run_onboard_happy_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(onboard, "_post_json", _mock_post_factory()[0])
    # Mock verify_subscription_setup so it doesn't actually probe Claude
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    state_dir = tmp_path / "state"
    rc = onboard.run_onboard(
        [
            "test-peer",
            "--gateway",
            "http://localhost:8788",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc == 0

    # Scaffold artifacts
    peer_dir = state_dir / "test-peer"
    assert peer_dir.is_dir()
    assert (peer_dir / "inbox.log").exists()
    cursor = json.loads((peer_dir / "cursor.json").read_text())
    assert cursor["last_msg_id"] == 0
    assert cursor["tasks_snapshot"] == {}
    assert (peer_dir / ".env.example").exists()
    daemon = peer_dir / "run-daemon.sh"
    assert daemon.exists()
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert oct(daemon.stat().st_mode & 0o777) == "0o755"

    # Handshake template
    handshake = Path(tempfile.gettempdir()) / "test-peer-handshake.md"
    assert handshake.exists()
    body = handshake.read_text()
    assert "DM SEMANTICS" in body
    assert "Framing-contagion" in body
    assert "Transparency-by-default" in body
    assert "Mesh-secrets out-of-band" in body
    assert "test-peer" in body
    handshake.unlink()  # cleanup

    out = capsys.readouterr().out
    assert "[1/6]" in out
    assert "[6/6]" in out
    assert "registered_unratified=true" in out
    assert "[manual]" in out


def test_run_onboard_resolves_alias(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake, captured = _mock_post_factory()
    monkeypatch.setattr(onboard, "_post_json", fake)
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    rc = onboard.run_onboard(
        ["lab-claude", "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "lab-claude" in err
    assert "lab-ovh" in err
    assert "alias" in err.lower()
    # Posted body uses the canonical name
    assert captured[0]["body"]["name"] == "lab-ovh"
    (Path(tempfile.gettempdir()) / "lab-ovh-handshake.md").unlink(missing_ok=True)


def test_run_onboard_rejects_bad_name(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    rc = onboard.run_onboard(["BAD_NAME"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "naming convention" in err


def test_run_onboard_gateway_error_returns_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(
        onboard,
        "_post_json",
        _mock_post_factory(
            register_status=500, register_body={"detail": "internal"}
        )[0],
    )
    rc = onboard.run_onboard(
        ["fail-peer", "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "register failed" in err


def test_run_onboard_subscription_check_failure_is_warning_not_fatal(
    monkeypatch, tmp_path, capsys
):
    """§15.6 #10 deferred non-Claude runtimes — subscription check
    failure shouldn't block onboard."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(onboard, "_post_json", _mock_post_factory()[0])
    import swarph_shared

    def boom():
        raise RuntimeError("no claude binary on PATH")

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", boom, raising=False
    )

    rc = onboard.run_onboard(
        ["non-claude-peer", "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0  # warning, not fatal
    err = capsys.readouterr().err
    assert "WARN" in err
    (Path(tempfile.gettempdir()) / "non-claude-peer-handshake.md").unlink(missing_ok=True)


def test_run_onboard_capability_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake, captured = _mock_post_factory()
    monkeypatch.setattr(onboard, "_post_json", fake)
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    rc = onboard.run_onboard(
        [
            "cap-peer",
            "--state-dir",
            str(tmp_path / "state"),
            "--capability",
            "can_claim_tasks=false",
            "--capability",
            "role=witness",
        ]
    )
    assert rc == 0
    caps = captured[0]["body"]["capabilities"]
    assert caps == {"can_claim_tasks": False, "role": "witness"}
    (Path(tempfile.gettempdir()) / "cap-peer-handshake.md").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Verb dispatch — main.py routes "onboard" to run_onboard
# ---------------------------------------------------------------------------


def test_main_dispatches_onboard_verb(monkeypatch):
    from swarph_cli import main as main_mod

    captured = {}

    def fake_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("swarph_cli.commands.onboard.run_onboard", fake_run)
    rc = main_mod.main(["onboard", "test-peer", "--gateway", "http://x"])
    assert rc == 0
    assert captured["argv"] == ["test-peer", "--gateway", "http://x"]
