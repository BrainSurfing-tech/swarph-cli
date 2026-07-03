# tests/test_codegraph_mcp.py
"""Tests for the `swarph_codegraph_query` MCP tool — the discoverability
wrapper over Task 1's `codegraph.structural_query`.

All fixture data is SYNTHETIC (invented repos/symbols) — this module ships
in the public swarph-cli package, so no real repo names, symbol names, or
fleet topology may appear here."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from swarph_cli.commands import mcp_server


# --------------------------------------------------------------------------- #
# Synthetic fixture — mirrors tests/test_codegraph_command.py's shape
# --------------------------------------------------------------------------- #


def _tiny(tmp_path):
    p = os.path.join(tmp_path, "i.db")
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE repos(name TEXT PRIMARY KEY, slug TEXT, path TEXT, visibility TEXT, indexed_at TEXT);"
        "CREATE TABLE symbols(id INTEGER PRIMARY KEY, repo TEXT, name TEXT, kind TEXT, file_path TEXT, start_line INTEGER,"
        " qualified_name TEXT, docstring TEXT, signature TEXT, name_search TEXT);"
        "CREATE TABLE edges(src_symbol INTEGER, dst_symbol INTEGER, edge_type TEXT, repo TEXT);"
        "CREATE VIRTUAL TABLE symbols_fts USING fts5(name_search, qualified_name, docstring, signature,"
        " content='symbols', content_rowid='id', tokenize=\"porter unicode61 separators '_.'\");")
    c.execute("INSERT INTO repos VALUES('alpha','o/alpha','/a','public','t')")
    c.execute("INSERT INTO symbols(id,repo,name,kind,file_path,start_line,qualified_name,docstring,signature,name_search)"
              " VALUES(1,'alpha','renderThing','function','ui.js',3,'ui.renderThing','renders the thing','function renderThing(x)','renderThing render thing')")
    c.execute("INSERT INTO symbols_fts(rowid,name_search,qualified_name,docstring,signature) SELECT id,name_search,qualified_name,docstring,signature FROM symbols")
    c.commit()
    c.close()
    return p


# --------------------------------------------------------------------------- #
# _codegraph_query — plain helper, SDK-independent
# --------------------------------------------------------------------------- #


def test_codegraph_query_returns_compact_row(tmp_path, monkeypatch):
    index_path = _tiny(tmp_path)
    monkeypatch.setattr(mcp_server, "_CODEGRAPH_INDEX", str(index_path))

    out = mcp_server._codegraph_query("which function renders the thing")

    assert isinstance(out, list)
    assert len(out) == 1
    row = out[0]
    assert row["name"] == "renderThing"
    assert set(row.keys()) == {
        "name", "kind", "repo", "file_path", "start_line", "signature", "callers",
    }


def test_codegraph_query_bad_index_returns_empty_list(tmp_path, monkeypatch):
    missing = os.path.join(tmp_path, "nope.db")
    monkeypatch.setattr(mcp_server, "_CODEGRAPH_INDEX", missing)

    out = mcp_server._codegraph_query("anything")

    assert out == []


# --------------------------------------------------------------------------- #
# Tool registration — requires the optional `mcp` SDK. Guarded per-test (not
# module-level) so the plain-helper tests above still run when the SDK is
# absent; this test itself skips locally and runs in CI where [mcp] is
# installed.
# --------------------------------------------------------------------------- #


def test_codegraph_tool_registered_on_server():
    pytest.importorskip("mcp")
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "swarph_codegraph_query" in names
