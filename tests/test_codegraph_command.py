# tests/test_codegraph_command.py
"""Tests for `swarph codegraph` — self-contained structural code search over a
LOCAL codegraph index. All fixture data is SYNTHETIC (invented repos/symbols) —
this module ships in the public swarph-cli package, so no real repo names,
symbol names, or fleet topology may appear here."""
import os
import sqlite3

import pytest

from swarph_cli.commands import codegraph as cg


def _tiny(tmp_path):
    # synthetic fixture — NO real repo/symbol names
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
    c.execute("INSERT INTO repos VALUES('beta','o/beta','/b','private','t')")
    c.execute("INSERT INTO symbols(id,repo,name,kind,file_path,start_line,qualified_name,docstring,signature,name_search)"
              " VALUES(1,'alpha','renderThing','function','ui.js',3,'ui.renderThing','renders the thing','function renderThing(x)','renderThing render thing')")
    c.execute("INSERT INTO symbols(id,repo,name,kind,file_path,start_line,qualified_name,docstring,signature,name_search)"
              " VALUES(2,'beta','secretOp','function','s.py',9,'b.secretOp','does a secret op','def secretOp(x)','secretOp secret op')")
    c.execute("INSERT INTO symbols_fts(rowid,name_search,qualified_name,docstring,signature) SELECT id,name_search,qualified_name,docstring,signature FROM symbols")
    c.commit()
    c.close()
    return p


def test_structural_query_finds_symbol_by_natural_language(tmp_path):
    index_path = _tiny(tmp_path)
    rows = cg.structural_query(
        "which function renders the thing",
        index_path=index_path, caller_cell="local", limit=8,
    )
    names = [r["name"] for r in rows]
    assert "renderThing" in names


def test_structural_query_missing_index_returns_empty_list(tmp_path):
    missing = os.path.join(tmp_path, "nope.db")
    rows = cg.structural_query(
        "renders the thing", index_path=missing, caller_cell="local", limit=8,
    )
    assert rows == []


def test_a8_default_allowlist_sees_private_repo(tmp_path):
    # operate-what-you-own: local caller with default allowlist sees the
    # private 'beta' symbol from its own index.
    index_path = _tiny(tmp_path)
    rows = cg.structural_query(
        "secret op", index_path=index_path, caller_cell="local", limit=8,
    )
    names = [r["name"] for r in rows]
    assert "secretOp" in names


def test_a8_empty_allowlist_hides_private_but_shows_public(tmp_path):
    index_path = _tiny(tmp_path)
    rows = cg.structural_query(
        "secret op", index_path=index_path, caller_cell="someone-else",
        limit=8, allowlist={},
    )
    names = [r["name"] for r in rows]
    assert "secretOp" not in names

    rows2 = cg.structural_query(
        "renders the thing", index_path=index_path, caller_cell="someone-else",
        limit=8, allowlist={},
    )
    names2 = [r["name"] for r in rows2]
    assert "renderThing" in names2


def test_sanitize_query_empty_string(tmp_path):
    assert cg._sanitize_query("") == ""


def test_allowlist_file_gates_private_repo_by_caller(tmp_path):
    # When an owner-maintained allowlist.json exists next to the index, the A8
    # gate uses it (default-deny) instead of operate-what-you-own: a caller NOT
    # granted a private repo can't see its symbols; a granted caller can.
    import json as _json
    index_path = _tiny(tmp_path)
    (tmp_path / "allowlist.json").write_text(_json.dumps({"beta": ["owner"]}))
    stranger = cg.structural_query("secret op", index_path=index_path, caller_cell="stranger", limit=8)
    assert "secretOp" not in [r["name"] for r in stranger]      # private, not granted → hidden
    granted = cg.structural_query("secret op", index_path=index_path, caller_cell="owner", limit=8)
    assert "secretOp" in [r["name"] for r in granted]           # granted → visible
    pub = cg.structural_query("renders the thing", index_path=index_path, caller_cell="stranger", limit=8)
    assert "renderThing" in [r["name"] for r in pub]            # public always visible


def test_malformed_allowlist_file_fails_closed(tmp_path):
    index_path = _tiny(tmp_path)
    (tmp_path / "allowlist.json").write_text("{ not valid json")
    rows = cg.structural_query("secret op", index_path=index_path, caller_cell="anyone", limit=8)
    assert "secretOp" not in [r["name"] for r in rows]          # malformed policy → deny private
    pub = cg.structural_query("renders the thing", index_path=index_path, caller_cell="anyone", limit=8)
    assert "renderThing" in [r["name"] for r in pub]            # public still visible


def test_no_allowlist_file_preserves_operate_what_you_own(tmp_path):
    # Back-compat: absent allowlist.json → operate-what-you-own (caller sees private).
    index_path = _tiny(tmp_path)
    rows = cg.structural_query("secret op", index_path=index_path, caller_cell="local", limit=8)
    assert "secretOp" in [r["name"] for r in rows]


def test_format_human_no_matches():
    assert "No structural matches" in cg.format_human([], "x")


def test_main_routes_codegraph_verb():
    # `swarph codegraph "..."` dispatches through main._VERB_HANDLERS
    from swarph_cli import main
    assert main._VERB_HANDLERS["codegraph"] == "swarph_cli.commands.codegraph.run_codegraph"


def test_run_codegraph_cli_smoke(tmp_path, capsys):
    # End-to-end argparse wiring: --index/--caller-cell/--json all reach
    # structural_query and a JSON-decodable result comes back on stdout.
    index_path = _tiny(tmp_path)
    rc = cg.run_codegraph(
        ["renders the thing", "--index", str(index_path), "--caller-cell", "local", "--json"]
    )
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert any(r["name"] == "renderThing" for r in out)
