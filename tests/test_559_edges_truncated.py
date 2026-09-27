"""Card #559 ruling C — edges_truncated is a per-row boolean, never a count.

Commander, 2026-09-10 16:55Z: B stays (a cross-repo call edge is kept only when the
peer may read the caller's repo — the scoped COUNT already on main). C is the
flag: true exactly when that filter dropped at least one edge, false on every
other row. A hidden caller must not look like "no such edge".
"""
import os
import sqlite3

from swarph_cli.commands import codegraph as cg


def _index(tmp_path):
    """pub holds targetFn (one public caller, one private) and quietFn (public only)."""
    p = os.path.join(tmp_path, "i.db")
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE repos(name TEXT PRIMARY KEY, slug TEXT, path TEXT, visibility TEXT, indexed_at TEXT);"
        "CREATE TABLE symbols(id INTEGER PRIMARY KEY, repo TEXT, name TEXT, kind TEXT, file_path TEXT, start_line INTEGER,"
        " qualified_name TEXT, docstring TEXT, signature TEXT, name_search TEXT);"
        "CREATE TABLE edges(src_symbol INTEGER, dst_symbol INTEGER, edge_type TEXT, repo TEXT);"
        "CREATE VIRTUAL TABLE symbols_fts USING fts5(name_search, qualified_name, docstring, signature,"
        " content='symbols', content_rowid='id', tokenize=\"porter unicode61 separators '_.'\");")
    c.execute("INSERT INTO repos VALUES('pub','o/pub','/p','public','t')")
    c.execute("INSERT INTO repos VALUES('priv','o/priv','/q','private','t')")
    ins = ("INSERT INTO symbols(id,repo,name,kind,file_path,start_line,qualified_name,"
           "docstring,signature,name_search) VALUES(?,?,?,?,?,?,?,?,?,?)")
    c.execute(ins, (1, "pub", "targetFn", "function", "a.py", 1, "pub.targetFn",
                    "shared marker symbol", "def targetFn()", "targetFn shared marker"))
    c.execute(ins, (2, "pub", "publicCaller", "function", "b.py", 1, "pub.publicCaller",
                    "calls target", "def publicCaller()", "publicCaller"))
    c.execute(ins, (3, "priv", "privateCaller", "function", "c.py", 1, "priv.privateCaller",
                    "calls target", "def privateCaller()", "privateCaller"))
    c.execute(ins, (4, "pub", "quietFn", "function", "d.py", 1, "pub.quietFn",
                    "shared marker symbol", "def quietFn()", "quietFn shared marker"))
    c.execute(ins, (5, "pub", "quietCaller", "function", "e.py", 1, "pub.quietCaller",
                    "calls quiet", "def quietCaller()", "quietCaller"))
    c.execute("INSERT INTO edges VALUES(2,1,'calls','pub')")
    c.execute("INSERT INTO edges VALUES(3,1,'calls','priv')")
    c.execute("INSERT INTO edges VALUES(5,4,'calls','pub')")
    c.execute("INSERT INTO symbols_fts(rowid,name_search,qualified_name,docstring,signature) "
              "SELECT id,name_search,qualified_name,docstring,signature FROM symbols")
    c.commit()
    c.close()
    return p


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_partial_peer_flags_only_the_row_whose_edge_was_dropped(tmp_path):
    """Fails on main: the row has no edges_truncated key, so hidden reads as absent."""
    rows = _by_name(cg.structural_query(
        "shared marker", index_path=_index(tmp_path), caller_cell="stranger",
        limit=8, allowlist={}))
    assert set(rows) >= {"targetFn", "quietFn"}
    assert "privateCaller" not in rows
    hit = rows["targetFn"]
    quiet = rows["quietFn"]
    assert hit["callers"] == 1, "B's scoped count stays; the flag is not a count"
    assert hit["edges_truncated"] is True
    assert quiet["callers"] == 1
    assert quiet["edges_truncated"] is False
    assert "dropped" not in hit and "hidden" not in hit


def test_peer_who_can_read_the_target_repo_is_not_truncated(tmp_path):
    rows = _by_name(cg.structural_query(
        "shared marker", index_path=_index(tmp_path), caller_cell="owner",
        limit=8, allowlist={"priv": ["owner"]}))
    assert rows["targetFn"]["callers"] == 2
    assert rows["targetFn"]["edges_truncated"] is False
    assert rows["quietFn"]["edges_truncated"] is False
