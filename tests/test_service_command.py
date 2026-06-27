"""Tests for the ``swarph service`` verb.

The dep-light cases (registered / help / unknown-provider / missing-deps) run
everywhere; the uvicorn-start case skips when the server extra isn't installed.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import service as sv


def test_service_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["service"] == "swarph_cli.commands.service.run_service"


def test_service_no_subcommand_prints_help():
    assert sv.run_service([]) == 0


def test_service_unknown_provider_returns_2(capsys):
    rc = sv.run_service(["serve", "--provider", "nope"])
    assert rc == 2
    assert "supported" in capsys.readouterr().err.lower()


def test_service_serve_missing_deps_returns_2(capsys, monkeypatch):
    monkeypatch.setattr(sv, "_have_server_deps", lambda: False)
    rc = sv.run_service(["serve", "--provider", "claude"])
    assert rc == 2
    assert "swarph-cli[service]" in capsys.readouterr().err


def test_service_serve_starts_uvicorn(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    monkeypatch.setattr(sv, "_have_server_deps", lambda: True)
    monkeypatch.setenv("SWARPH_SERVICE_TOKEN", "tok")

    import swarph_cli.service.app as appmod
    sentinel_app = object()
    monkeypatch.setattr(appmod, "build_app", lambda *a, **k: sentinel_app)

    import uvicorn
    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: calls.update(app=app, kw=k))

    rc = sv.run_service(["serve", "--provider", "claude", "--port", "9000"])
    assert rc == 0
    assert calls["app"] is sentinel_app
    assert calls["kw"]["port"] == 9000
