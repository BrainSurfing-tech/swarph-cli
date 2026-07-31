"""Card #194 — the caller COUNT must be a function of the caller-visible subset.

drop-on-meta-edge, seat-A, reviewing the `POST /codegraph` gateway proxy spec:

    "If a symbol's caller count sums callers that live in a PRIVATE repo the
     querying peer cannot see, THE NUMBER LEAKS THE EXISTENCE AND SIZE of those
     private callers — the exact payload #44's transport refuses, delivered as an
     integer instead of a path."

`structural_query` gates the ROWS with `s.repo IN (allowed)` but counted callers with
a bare `SELECT COUNT(*) FROM edges WHERE dst_symbol=? AND edge_type='calls'` — no
repo scoping at all. Measured on the live index 2026-07-31: cross-visibility `calls`
edges are 0 today, so the leak was LATENT rather than live — but it is in shipped
code, and the proxy would have exposed it to every remote peer.

The unifying rule this test enforces: EVERY OBSERVABLE — rows, counts, scores,
freshness — must be a function of only the caller-visible subset, never the full
index. "Filter in SQL, not post-LIMIT" is one instance of it; this is another.
"""
import os
import sqlite3

import pytest

from swarph_cli.commands import codegraph as cg


@pytest.fixture()
def cross_visibility_index(tmp_path):
    """`pub` (public) holds target_fn and one caller; `priv` (private) holds another.

    Both callers reach target_fn by a 'calls' edge, so the UNSCOPED count is 2 and
    the correctly-scoped count depends on who is asking.
    """
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
                    "the widely called target", "def targetFn()", "targetFn target fn"))
    c.execute(ins, (2, "pub", "publicCaller", "function", "b.py", 1, "pub.publicCaller",
                    "calls the target", "def publicCaller()", "publicCaller public caller"))
    c.execute(ins, (3, "priv", "privateCaller", "function", "c.py", 1, "priv.privateCaller",
                    "calls the target", "def privateCaller()", "privateCaller private caller"))
    c.execute("INSERT INTO edges VALUES(2,1,'calls','pub')")
    c.execute("INSERT INTO edges VALUES(3,1,'calls','priv')")
    c.execute("INSERT INTO symbols_fts(rowid,name_search,qualified_name,docstring,signature) "
              "SELECT id,name_search,qualified_name,docstring,signature FROM symbols")
    c.commit()
    c.close()
    return p


def _target(rows):
    return next(r for r in rows if r["name"] == "targetFn")


def test_caller_count_excludes_invisible_repos(cross_visibility_index):
    """THE REGRESSION. Same symbol, two callers, two different counts."""
    granted = cg.structural_query("the widely called target",
                                  index_path=cross_visibility_index,
                                  caller_cell="owner", limit=8,
                                  allowlist={"priv": ["owner"]})
    stranger = cg.structural_query("the widely called target",
                                   index_path=cross_visibility_index,
                                   caller_cell="stranger", limit=8,
                                   allowlist={})

    assert _target(granted)["callers"] == 2, "the granted caller should see both callers"
    assert _target(stranger)["callers"] == 1, (
        "the public-only caller saw a count including a PRIVATE caller — the number "
        "leaks the existence and size of a repo it cannot see (#44's payload as an int)")


def test_rows_were_already_gated_and_still_are(cross_visibility_index):
    """Guard against 'fixing' the count by widening the rows."""
    stranger = cg.structural_query("calls the target",
                                   index_path=cross_visibility_index,
                                   caller_cell="stranger", limit=8, allowlist={})
    assert {r["repo"] for r in stranger} == {"pub"}
    assert all(r["name"] != "privateCaller" for r in stranger)


def test_public_only_symbol_count_is_unchanged(cross_visibility_index):
    """Non-vacuity in the other direction: the fix must not deflate honest counts.

    A caller who CAN see everything must still get the true total — otherwise the
    'fix' is just under-reporting, which is a different lie.
    """
    granted = cg.structural_query("the widely called target",
                                  index_path=cross_visibility_index,
                                  caller_cell="owner", limit=8,
                                  allowlist={"priv": ["owner"]})
    assert _target(granted)["callers"] == 2
