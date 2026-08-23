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
from swarph_cli.tokens import read_token_file


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uses ACLs, not POSIX file modes")
def test_read_token_file_does_not_emit_posix_chmod_warning_on_windows(tmp_path, capsys):
    token = tmp_path / "peer_token"
    token.write_text("token-value\n", encoding="utf-8")

    assert read_token_file(token) == "token-value"
    assert "WARNING" not in capsys.readouterr().err


def test_resolve_token_REFUSES_rather_than_prompting(monkeypatch, tmp_path):
    """>>> THIS TEST USED TO ASSERT THE PROMPT, AND THE PROMPT WAS THE BUG (#243).
    <<<

    It pinned `falls_back_to_prompt` — correct when a shared MESH_GATEWAY_TOKEN
    was the only credential. After the R1 per-peer migration neither
    $MESH_GATEWAY_TOKEN nor ~/.swarph/secrets.toml exists on any cell, so the
    prompt became the DEFAULT path — and getpass on a non-tty is a guaranteed
    EOFError. `swarph onboard` was reported as a broken verb; it was a missing
    credential wearing a traceback.

    The expectation is flipped DELIBERATELY: refuse, naming every place it
    looked, so the operator learns which credential is absent instead of meeting
    a prompt that cannot be answered.
    """
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.setattr(onboard.Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError) as e:
        onboard._resolve_token(str(tmp_path / "no-such-file.toml"))
    msg = str(e.value)
    assert "MESH_GATEWAY_TOKEN" in msg and "peer_token" in msg, (
        "the refusal must name every place it looked — a bare failure leaves the "
        "operator guessing which of four credentials is missing")


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


def test_run_onboard_merges_stored_capabilities_into_reregister(
    monkeypatch, tmp_path, capsys
):
    """The 2026-08-23 cursor-test-postcompact case, verbatim: the operator's
    out-of-band mint wrote provider=cursor; the cell's own onboard then
    re-registered with the DEFAULT blob (can_claim_tasks only) and the
    gateway's #124 guard 409'd, stopping the ladder at rung 4. `mesh
    register` has done the GET-first merge since #294; onboard now does
    the same instead of letting the gateway teach the lesson."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake_post, captured = _mock_post_factory()
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr(
        onboard, "_get_json",
        lambda url, token: (200, {"name": "test-peer",
                                  "capabilities": {"provider": "cursor",
                                                   "can_claim_tasks": True}}))
    import swarph_shared
    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    reg = [c for c in captured if c["url"].endswith("/peers/register")][0]
    assert reg["body"]["capabilities"] == {
        "provider": "cursor", "can_claim_tasks": True}
    assert "merged stored capability keys" in capsys.readouterr().out


def test_run_onboard_merge_submitted_keys_override_stored(
    monkeypatch, tmp_path, capsys
):
    """The merge direction matters: an explicit --capability is a decision,
    not a hint — it overrides the stored value for the SAME key while
    stored-only keys survive."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake_post, captured = _mock_post_factory()
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr(
        onboard, "_get_json",
        lambda url, token: (200, {"name": "test-peer",
                                  "capabilities": {"provider": "cursor",
                                                   "can_claim_tasks": True}}))
    import swarph_shared
    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state"),
         "--capability", "provider=claude"]
    )
    assert rc == 0
    reg = [c for c in captured if c["url"].endswith("/peers/register")][0]
    assert reg["body"]["capabilities"] == {
        "provider": "claude", "can_claim_tasks": True}


def test_run_onboard_unreadable_registry_never_blocks_the_register(
    monkeypatch, tmp_path, capsys
):
    """First registration, or a GET that fails outright: nothing exists to
    destroy, so the write proceeds with what the invocation carries. The
    read is in service of the write, never a gate on it."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    fake_post, captured = _mock_post_factory()
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr(onboard, "_get_json", lambda url, token: (0, {}))
    import swarph_shared
    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    reg = [c for c in captured if c["url"].endswith("/peers/register")][0]
    assert reg["body"]["capabilities"] == {"can_claim_tasks": True}


# ---------------------------------------------------------------------------
# #564-C — defer_token_mint: who is this register FOR?
# ---------------------------------------------------------------------------


def _stub_subscription(monkeypatch):
    import swarph_shared
    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )


def test_operator_context_DEFERS_the_mint(monkeypatch, tmp_path, capsys):
    """SWARPH_SELF names a DIFFERENT cell (or nothing): the register asserts
    operator context — defer_token_mint=True — and a 'deferred' response
    prints the cell-side next step. The trap from the card is unreachable."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_SELF", "workstation-lc")
    deferred_body = {
        "status": "registered", "name": "test-peer",
        "registered_at": "2026-08-23T09:00:00Z", "ratified": False,
        "registered_unratified": True,
        "peer_token": None, "token_status": "deferred",
    }
    fake_post, captured = _mock_post_factory(register_body=deferred_body)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    _stub_subscription(monkeypatch)
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    reg = [c for c in captured if c["url"].endswith("/peers/register")][0]
    assert reg["body"]["defer_token_mint"] is True
    out = capsys.readouterr().out
    assert "mint DEFERRED" in out
    assert "swarph onboard test-peer" in out
    (Path(tempfile.gettempdir()) / "test-peer-handshake.md").unlink(missing_ok=True)


def test_cell_context_mints_and_CAPTURES(monkeypatch, tmp_path, capsys):
    """SWARPH_SELF == the target: the register is the cell's own bootstrap —
    no defer — and a minted token is written to the target's peer-token file
    mode 600, the same capture `mesh register` has always done. The cell-
    first order through `onboard` no longer discards the token either."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_SELF", "test-peer")
    monkeypatch.setenv("HOME", str(tmp_path))
    mint_body = {
        "status": "registered", "name": "test-peer",
        "registered_at": "2026-08-23T09:00:00Z", "ratified": False,
        "registered_unratified": True,
        "peer_token": "once-only-secret", "token_status": "minted",
    }
    fake_post, captured = _mock_post_factory(register_body=mint_body)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    _stub_subscription(monkeypatch)
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    reg = [c for c in captured if c["url"].endswith("/peers/register")][0]
    assert reg["body"]["defer_token_mint"] is False
    tok = tmp_path / ".config" / "swarph" / "test-peer.peer_token"
    assert tok.read_text().strip() == "once-only-secret"
    if sys.platform != "win32":
        assert oct(tok.stat().st_mode & 0o777) == "0o600"
    assert "once-only token captured" in capsys.readouterr().out
    (Path(tempfile.gettempdir()) / "test-peer-handshake.md").unlink(missing_ok=True)


def test_pre_defer_gateway_mint_is_SURFACED_not_discarded(
    monkeypatch, tmp_path, capsys
):
    """A gateway that pre-dates #564-C mints on ANY register — the defer
    flag is unknown to it. An operator-context process holding the cell's
    once-only token IS the original trap, so the token is printed once with
    delivery instructions instead of evaporating into the exit."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_SELF", "workstation-lc")
    monkeypatch.setenv("HOME", str(tmp_path))
    mint_body = {
        "status": "registered", "name": "test-peer",
        "registered_at": "2026-08-23T09:00:00Z", "ratified": False,
        "registered_unratified": True,
        "peer_token": "leaked-into-operator-process", "token_status": "minted",
    }
    fake_post, captured = _mock_post_factory(register_body=mint_body)
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    _stub_subscription(monkeypatch)
    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    assert captured[0]["body"]["defer_token_mint"] is True
    err = capsys.readouterr().err
    assert "leaked-into-operator-process" in err
    assert "pre-dates defer_token_mint" in err
    assert "test-peer.peer_token" in err
    # ...and it is NOT written anywhere by this command
    assert not (tmp_path / ".config" / "swarph" / "test-peer.peer_token").exists()
    (Path(tempfile.gettempdir()) / "test-peer-handshake.md").unlink(missing_ok=True)


def test_run_onboard_reports_reregister_as_existing_not_fresh_mint(
    monkeypatch, tmp_path, capsys
):
    """>>> JOURNEY-WALKTHROUGH FINDING, 2026-08-21. <<< Onboarding an ALREADY-
    REGISTERED peer printed "ok (registered_unratified=true)" — the fresh-mint
    line — for what the gateway answered as token_status=existing: no token
    minted, nothing changed. The operator reading that line believes a once-only
    token was just delivered to somebody. It was not; the cell-first order (cell
    self-registers, THEN operator onboards) is exactly the flow that hits this,
    and the ladder's own doctrine applies: a success-shaped line that hides
    "nothing happened" trains operators to look for a token that does not exist.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    reregister_body = {
        "status": "registered",
        "name": "test-peer",
        "registered_at": "2026-05-08T20:00:00Z",
        "ratified": False,
        "registered_unratified": True,
        "peer_token": None,
        "token_status": "existing",
    }
    monkeypatch.setattr(
        onboard, "_post_json", _mock_post_factory(register_body=reregister_body)[0]
    )
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "token_status=existing" in out, (
        "a re-register must SAY it minted nothing — the fresh-mint line is a lie "
        "about a once-only credential"
    )
    (Path(tempfile.gettempdir()) / "test-peer-handshake.md").unlink(missing_ok=True)


def test_run_onboard_already_ratified_reregister_STILL_names_the_no_mint(
    monkeypatch, tmp_path, capsys
):
    """>>> gpt-ops' BLOCKER ON #286. <<< token_status was checked AFTER
    registered_unratified, so an already-RATIFIED re-register took the first
    branch and printed only "already ratified" — the no-token-minted fact was
    swallowed by branch order. The ratification state and the mint state are
    TWO facts; a re-register must report the mint one regardless of which
    ratification branch it lands in."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    ratified_reregister_body = {
        "status": "registered",
        "name": "test-peer",
        "registered_at": "2026-05-08T20:00:00Z",
        "ratified": True,
        "registered_unratified": False,
        "peer_token": None,
        "token_status": "existing",
    }
    monkeypatch.setattr(
        onboard, "_post_json",
        _mock_post_factory(register_body=ratified_reregister_body)[0],
    )
    import swarph_shared

    monkeypatch.setattr(
        swarph_shared, "verify_subscription_setup", lambda: True, raising=False
    )

    rc = onboard.run_onboard(
        ["test-peer", "--gateway", "http://localhost:8788",
         "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "already ratified" in out
    assert "token_status=existing" in out, (
        "the no-mint fact must survive the already-ratified branch — branch "
        "order must not decide which fact the operator sees"
    )
    (Path(tempfile.gettempdir()) / "test-peer-handshake.md").unlink(missing_ok=True)


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
