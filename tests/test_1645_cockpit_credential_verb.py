"""card #1645 T10: `swarph gateway cockpit-credential mint|revoke`, a local DB
write with bootstrap-ratify's gates (a tty and a typed yes) and one more: it
refuses a database whose schema version is above 12 (fold F3), so it can never
mint on a mesh-gateway database. Scratch SQLite only; no server import."""
from __future__ import annotations

import builtins
import hashlib
import re
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarph_cli.commands import gateway as gw

_SCHEMA = (Path(__file__).parent.parent / "src" / "swarph_cli" / "gateway" / "schema.sql").read_text()
SHAPE = re.compile(r"\.\d{6}\+00:00$")


def _db(tmp_path, schema_version=None, with_table=True):
    path = tmp_path / "mesh.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    if not with_table:
        conn.execute("DROP TABLE cockpit_credentials")
    if schema_version:
        conn.execute("INSERT INTO schema_version(version, applied_at) VALUES(?, 'x')", (schema_version,))
    conn.commit()
    conn.close()
    return str(path)


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM cockpit_credentials ORDER BY created_at")]
    finally:
        conn.close()


def _tty_yes(monkeypatch, answer="yes"):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)


def test_mint_stores_the_hash_only_prints_the_value_once_and_revokes_the_previous(monkeypatch, tmp_path, capsys, caplog):
    db = _db(tmp_path)
    _tty_yes(monkeypatch)
    caplog.set_level("DEBUG")
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", db]) == 0
    out = capsys.readouterr()
    first = re.findall(r"^swck_[A-Za-z0-9_-]{43}$", out.out, re.M)
    assert len(first) == 1 and out.out.count(first[0]) == 1
    assert first[0] not in out.err and first[0] not in caplog.text
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["revoked_at"] is None
    assert rows[0]["token_sha256"] == hashlib.sha256(first[0].encode()).hexdigest()
    assert first[0] not in str(rows)
    assert rows[0]["minted_by"].startswith("local:") and "@" in rows[0]["minted_by"]
    assert SHAPE.search(rows[0]["created_at"]) and SHAPE.search(rows[0]["expires_at"])
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", db]) == 0
    rows = _rows(db)
    assert [r["revoked_at"] is None for r in rows] == [False, True]
    assert SHAPE.search(rows[0]["revoked_at"])
    assert gw.run_gateway(["cockpit-credential", "revoke", "--db", db]) == 0
    assert all(r["revoked_at"] for r in _rows(db))
    assert gw.run_gateway(["cockpit-credential", "revoke", "--db", db]) == 2
    assert "no active cockpit credential" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["mint", "revoke"])
def test_refuses_without_a_tty(monkeypatch, tmp_path, capsys, action):
    db = _db(tmp_path)
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    monkeypatch.setattr(builtins, "input",
                        lambda prompt="": (_ for _ in ()).throw(AssertionError("input() reached without a tty")))
    assert gw.run_gateway(["cockpit-credential", action, "--db", db]) == 2
    assert "human commander" in capsys.readouterr().err
    assert _rows(db) == []


@pytest.mark.parametrize("action", ["mint", "revoke"])
def test_wrong_answer_writes_nothing(monkeypatch, tmp_path, capsys, action):
    db = _db(tmp_path)
    _tty_yes(monkeypatch)
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", db]) == 0
    before = _rows(db)
    _tty_yes(monkeypatch, "y")
    assert gw.run_gateway(["cockpit-credential", action, "--db", db]) == 1
    assert _rows(db) == before
    assert "swck_" not in capsys.readouterr().out.splitlines()[-1]


@pytest.mark.parametrize("version", [13, 42])
def test_refuses_a_database_above_schema_12(monkeypatch, tmp_path, capsys, version):
    """A mesh-gateway database has the same table (mesh 4989daa schema.sql:971-980);
    its middleware checks only hash, revocation and expiry, so a mint here would work there."""
    db = _db(tmp_path, schema_version=version)
    _tty_yes(monkeypatch)
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", db]) == 2
    err = capsys.readouterr().err
    assert f"schema v{version}" in err and "another gateway" in err
    assert _rows(db) == []


def test_refuses_a_missing_database_or_table(monkeypatch, tmp_path, capsys):
    _tty_yes(monkeypatch)
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", str(tmp_path / "nope.db")]) == 2
    assert "gateway box" in capsys.readouterr().err
    db = _db(tmp_path, with_table=False)
    assert gw.run_gateway(["cockpit-credential", "mint", "--db", db]) == 2
    assert "no cockpit table" in capsys.readouterr().err
