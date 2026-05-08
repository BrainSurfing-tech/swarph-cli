"""Tests for ``swarph ratify`` — mocks HTTP layer.

Live falsifiability gate (synthetic ``onboard-smoke`` peer end-to-end
against the deployed gateway PR A) lives in ``test_smoke_phase_5_5.py``.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import ratify


# ---------------------------------------------------------------------------
# _resolve_witness
# ---------------------------------------------------------------------------


def test_resolve_witness_arg_wins(monkeypatch):
    monkeypatch.setenv("SWARPH_WITNESS", "from-env")
    assert ratify._resolve_witness("from-arg") == "from-arg"


def test_resolve_witness_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SWARPH_WITNESS", "from-env")
    assert ratify._resolve_witness(None) == "from-env"


def test_resolve_witness_empty_when_neither(monkeypatch):
    monkeypatch.delenv("SWARPH_WITNESS", raising=False)
    assert ratify._resolve_witness(None) == ""


# ---------------------------------------------------------------------------
# _http_json — mocked replacement
# ---------------------------------------------------------------------------


def _http_factory(scripted: list):
    """Returns a _http_json replacement that pops scripted (status, body)
    in order and captures the calls."""
    captured = []
    it = iter(scripted)

    def fake(url, *, token, method="GET", body=None):
        captured.append({"url": url, "method": method, "body": body})
        return next(it)

    return fake, captured


# ---------------------------------------------------------------------------
# run_ratify — full pipeline
# ---------------------------------------------------------------------------


def test_run_ratify_happy_path(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "lab-ovh")

    fake, captured = _http_factory([
        # GET /peers/lab-ovh
        (200, {"name": "lab-ovh", "ratified": True}),
        # GET /peers/test-peer
        (200, {"name": "test-peer", "ratified": False}),
        # PATCH /peers/test-peer
        (200, {"name": "test-peer", "ratified": True,
               "ratified_at": "2026-05-08T20:00:00Z",
               "ratified_by": "lab-ovh"}),
        # GET /peers/test-peer/ratifications
        (200, {"peer": "test-peer", "ratifications": [
            {"id": 99, "ratified_by": "lab-ovh",
             "reason": "smoke ratify"}
        ]}),
    ])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(
        ["test-peer", "--reason", "smoke ratify",
         "--gateway", "http://localhost:8788"]
    )
    assert rc == 0

    out = capsys.readouterr().out
    assert "[1/6]" in out
    assert "[6/6]" in out
    assert "ratification complete" in out

    # PATCH body shape
    patch_call = next(c for c in captured if c["method"] == "PATCH")
    assert patch_call["body"]["ratified"] is True
    assert patch_call["body"]["ratified_by"] == "lab-ovh"
    assert patch_call["body"]["reason"] == "smoke ratify"


def test_run_ratify_rejects_bad_name(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "lab-ovh")
    rc = ratify.run_ratify(["BAD_NAME"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "naming convention" in err


def test_run_ratify_rejects_self_ratification(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    rc = ratify.run_ratify(
        ["lab-ovh", "--witness-name", "lab-ovh"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "self-ratification" in err.lower() or "self" in err.lower()


def test_run_ratify_requires_witness(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_WITNESS", raising=False)
    rc = ratify.run_ratify(["test-peer"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "witness" in err.lower()


def test_run_ratify_witness_not_registered_404(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "ghost-witness")

    fake, _ = _http_factory([(404, {"detail": "peer 'ghost-witness' not registered"})])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(["test-peer"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not registered" in err


def test_run_ratify_witness_not_ratified(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "unratified-w")

    fake, _ = _http_factory([
        (200, {"name": "unratified-w", "ratified": False}),
    ])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(["test-peer"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not ratified" in err.lower()


def test_run_ratify_target_not_found(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "lab-ovh")

    fake, _ = _http_factory([
        (200, {"name": "lab-ovh", "ratified": True}),
        (404, {"detail": "peer 'no-such' not registered"}),
    ])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(["no-such"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not registered" in err
    assert "swarph onboard" in err  # actionable hint


def test_run_ratify_target_already_ratified(monkeypatch, capsys):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "lab-ovh")

    fake, _ = _http_factory([
        (200, {"name": "lab-ovh", "ratified": True}),
        (200, {"name": "already-rat", "ratified": True,
               "ratified_by": "previous-witness",
               "ratified_at": "2026-05-08T19:00:00Z"}),
    ])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(["already-rat"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already ratified" in err.lower()


def test_run_ratify_passes_witness_dm_id(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SWARPH_WITNESS", "lab-ovh")

    fake, captured = _http_factory([
        (200, {"name": "lab-ovh", "ratified": True}),
        (200, {"name": "p", "ratified": False}),
        (200, {"ratified": True, "ratified_at": "x", "ratified_by": "lab-ovh"}),
        (200, {"ratifications": [{"id": 1, "ratified_by": "lab-ovh"}]}),
    ])
    monkeypatch.setattr(ratify, "_http_json", fake)

    rc = ratify.run_ratify(
        ["target-p", "--witness-dm-id", "42"]
    )
    assert rc == 0
    patch_call = next(c for c in captured if c["method"] == "PATCH")
    assert patch_call["body"]["witness_dm_id"] == 42


# ---------------------------------------------------------------------------
# Verb dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_ratify_verb(monkeypatch):
    from swarph_cli import main as main_mod

    captured = {}

    def fake_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("swarph_cli.commands.ratify.run_ratify", fake_run)
    rc = main_mod.main(["ratify", "test-peer", "--reason", "ok"])
    assert rc == 0
    assert captured["argv"] == ["test-peer", "--reason", "ok"]
