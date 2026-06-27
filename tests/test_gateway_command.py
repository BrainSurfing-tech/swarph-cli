"""Tests for ``swarph gateway`` — offline, mocked server deps.

The gateway server stack (fastapi/uvicorn) is an optional extra; these
cover the verb wiring, the missing-dependency hint path, and the serve
dispatch (uvicorn.run mocked, no socket bound). A real server boot is
out of scope here.
"""

from __future__ import annotations

import builtins

from unittest.mock import MagicMock, patch

from swarph_cli.commands import gateway as gw


# --- verb wiring -----------------------------------------------------------

def test_gateway_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS

    assert "gateway" in _VERB_HANDLERS
    assert _VERB_HANDLERS["gateway"] == "swarph_cli.commands.gateway.run_gateway"


# --- missing-dependency hint path ------------------------------------------

def test_serve_missing_deps_returns_2_with_hint(capsys, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("fastapi", "uvicorn"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = gw.run_gateway(["serve"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "swarph-cli[gateway]" in err


# --- serve dispatch (uvicorn.run mocked) -----------------------------------

def test_serve_calls_uvicorn_with_app_host_port(monkeypatch):
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    fake_uvicorn = MagicMock()
    fake_fastapi = MagicMock()
    with patch.dict(
        "sys.modules", {"uvicorn": fake_uvicorn, "fastapi": fake_fastapi}
    ):
        rc = gw.run_gateway(["serve", "--host", "127.0.0.1", "--port", "9999"])

    assert rc == 0
    fake_uvicorn.run.assert_called_once()
    args, kwargs = fake_uvicorn.run.call_args
    assert args[0] == "swarph_cli.gateway.server:app"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9999
    # A token was minted + exported for the served process.
    import os

    assert os.environ.get("MESH_GATEWAY_TOKEN")


def test_serve_honors_explicit_token_and_db(monkeypatch):
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("MESH_DB_PATH", raising=False)
    fake_uvicorn = MagicMock()
    fake_fastapi = MagicMock()
    with patch.dict(
        "sys.modules", {"uvicorn": fake_uvicorn, "fastapi": fake_fastapi}
    ):
        rc = gw.run_gateway(
            ["serve", "--token", "tok_explicit", "--db", "/tmp/gw.db"]
        )

    import os

    assert rc == 0
    assert os.environ["MESH_GATEWAY_TOKEN"] == "tok_explicit"
    assert os.environ["MESH_DB_PATH"] == "/tmp/gw.db"


# --- bare verb prints help -------------------------------------------------

def test_bare_gateway_prints_help(capsys):
    rc = gw.run_gateway([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "serve" in out
