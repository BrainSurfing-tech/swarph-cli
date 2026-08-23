"""Tests for ``swarph gateway`` — offline, mocked server deps.

The gateway server stack (fastapi/uvicorn) is an optional extra; these
cover the verb wiring, the missing-dependency hint path, and the serve
dispatch (uvicorn.run mocked, no socket bound). A real server boot is
out of scope here.
"""

from __future__ import annotations

import builtins
import sqlite3

from pathlib import Path

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


# --- bootstrap-ratify (#565-B) ----------------------------------------------

_SCHEMA = (
    Path(__file__).parent.parent
    / "src" / "swarph_cli" / "gateway" / "schema.sql"
).read_text()


def _fresh_db(tmp_path, peers=()):
    """A gateway DB as a fresh deployment leaves it: schema applied, peers
    registered, ZERO ratified."""
    db = tmp_path / "mesh.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for name, ratified in peers:
        conn.execute(
            "INSERT INTO claude_peers (name, url, capabilities, "
            "registered_at, ratified) VALUES (?, ?, '{}', '2026-08-23', ?)",
            (name, f"http://{name}:8787", ratified),
        )
    conn.commit()
    conn.close()
    return str(db)


def _confirm_yes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "yes")
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))


def test_bootstrap_ratify_happy_path(monkeypatch, tmp_path, capsys):
    db = _fresh_db(tmp_path, peers=[("lab-ovh", 0)])
    _confirm_yes(monkeypatch)
    rc = gw.run_gateway(["bootstrap-ratify", "lab-ovh", "--db", db])
    assert rc == 0
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ratified, ratified_by, ratification_reason FROM "
        "claude_peers WHERE name='lab-ovh'").fetchone()
    assert row[0] == 1 and row[1] == "bootstrap"
    assert "bootstrap by" in row[2] and "#565" in row[2]
    audit = conn.execute(
        "SELECT ratified_by, binding_regime, reason FROM peer_ratifications "
        "WHERE peer='lab-ovh'").fetchone()
    assert audit[0] == "bootstrap" and audit[1] == "bootstrap"
    conn.close()
    out = capsys.readouterr().out
    assert "first rung" in out
    assert "swarph ratify <cell>" in out  # the hand-in-hand next step


def test_bootstrap_ratify_refuses_when_a_rung_exists(
    monkeypatch, tmp_path, capsys
):
    """Self-destroying: with ANY ratified peer, the normal witness path is
    the answer and bootstrap must refuse."""
    db = _fresh_db(tmp_path, peers=[("lab-ovh", 1), ("new-cell", 0)])
    _confirm_yes(monkeypatch)
    rc = gw.run_gateway(["bootstrap-ratify", "new-cell", "--db", db])
    assert rc == 2
    err = capsys.readouterr().err
    assert "lab-ovh" in err and "witness" in err
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT ratified FROM claude_peers WHERE name='new-cell'"
    ).fetchone()[0] == 0
    conn.close()


def test_bootstrap_ratify_unknown_peer_points_at_onboard(
    monkeypatch, tmp_path, capsys
):
    db = _fresh_db(tmp_path)
    _confirm_yes(monkeypatch)
    rc = gw.run_gateway(["bootstrap-ratify", "ghost", "--db", db])
    assert rc == 2
    assert "swarph onboard ghost" in capsys.readouterr().err


def test_bootstrap_ratify_already_ratified_is_a_noop(
    monkeypatch, tmp_path, capsys
):
    db = _fresh_db(tmp_path, peers=[("lab-ovh", 1)])
    _confirm_yes(monkeypatch)
    rc = gw.run_gateway(["bootstrap-ratify", "lab-ovh", "--db", db])
    assert rc == 0
    assert "already ratified" in capsys.readouterr().out


def test_bootstrap_ratify_refuses_a_non_tty(monkeypatch, tmp_path, capsys):
    """The human-only gate: no interactive tty, no bootstrap. A cell's
    scripted context dies here."""
    db = _fresh_db(tmp_path, peers=[("lab-ovh", 0)])
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    monkeypatch.setattr(
        builtins, "input",
        lambda prompt="": (_ for _ in ()).throw(AssertionError(
            "input() must not be reached without a tty")))
    rc = gw.run_gateway(["bootstrap-ratify", "lab-ovh", "--db", db])
    assert rc == 2
    assert "human commander" in capsys.readouterr().err
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT ratified FROM claude_peers WHERE name='lab-ovh'"
    ).fetchone()[0] == 0
    conn.close()


def test_bootstrap_ratify_wrong_answer_writes_nothing(
    monkeypatch, tmp_path, capsys
):
    db = _fresh_db(tmp_path, peers=[("lab-ovh", 0)])
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    rc = gw.run_gateway(["bootstrap-ratify", "lab-ovh", "--db", db])
    assert rc == 1
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT ratified FROM claude_peers WHERE name='lab-ovh'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM peer_ratifications").fetchone()[0] == 0
    conn.close()


def test_bootstrap_ratify_missing_db(tmp_path, capsys):
    rc = gw.run_gateway(
        ["bootstrap-ratify", "lab-ovh", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "gateway box" in capsys.readouterr().err
