"""``swarph codegraph`` — on-demand STRUCTURAL code search over a LOCAL
derived index.

Self-contained port: this module does NOT depend on the local-only
``codegraph_index`` package. The query/text-search/sensitivity logic below
is ported verbatim (imports adapted only) so swarph-cli — a PUBLIC PyPI
package — stays free of any dependency on that local tool.

Retrieval is a TOOL an agent invokes when it needs to find where a symbol
is defined or what calls it, not a per-prompt fusion lane — the intent to
consult code structure comes from the caller.

Fail-safe (A2): the query path is READ-ONLY, never raises, never hangs.
A missing index file, a corrupt db, a malformed FTS5 MATCH string, or a
locked db all resolve to ``[]`` — never an exception.

Sensitivity gate (A8): ``_visible_repos`` decides which repos a caller may
see BEFORE any row is read — public repos are always visible; private
repos require the caller's cell to be present in ``allowlist[repo]``. The
default allowlist (``_local_allowlist``) is operate-what-you-own: the
local caller sees every repo present in its own index.

Usage:
  swarph codegraph "which function renders the thing"
  swarph codegraph "cron expression validator" --json --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3

DEFAULT_INDEX = os.path.expanduser("~/.swarph/codegraph/index.db")
# NOTE: named DEFAULT_CALLER_CELL, not "*_CALLER" — this is an A8 caller-CELL
# identity (sensitivity-gate scoping), not a SwarphCall `role.subrole.specific`
# caller tag, and must NOT trip the repo-wide caller-convention meta-guard
# (tests/test_caller_meta_guard.py) which validates every `*_CALLER` constant
# against that unrelated dotted-slug convention.
DEFAULT_CALLER_CELL = "lab-ovh"

# --- textsearch (ported verbatim from codegraph_index/textsearch.py) ------

_STOP = {"the", "a", "an", "is", "are", "was", "how", "does", "do", "of", "to", "in", "on", "for", "and", "or",
         "what", "which", "where", "when", "that", "this", "it", "its", "with", "by", "from", "get", "set"}


def _tokens(nl: str) -> list:
    """Sanitized content-tokens (lowercased, punctuation-stripped, stopwords
    dropped, order-preserving de-dupe)."""
    toks = re.findall(r"[A-Za-z0-9]+", (nl or "").lower())
    toks = [t for t in toks if len(t) > 1 and t not in _STOP]
    return list(dict.fromkeys(toks))  # dedupe, preserve first-seen order


def _sanitize_query(nl: str) -> str:
    """Turn free-text NL into an FTS5 MATCH string: OR-joined terms,
    punctuation stripped, stopwords dropped, order-preserving de-dupe.
    Empty input -> "" (caller must treat that as "no query", never issue a
    MATCH on an empty string)."""
    return " OR ".join(_tokens(nl))


# --- connection (ported from codegraph_index/schema.py, read-only) --------

def _connect(index_path: str, timeout_ms: int = 100) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=timeout_ms / 1000)
    con.execute(f"PRAGMA busy_timeout={timeout_ms}")  # A2: bound the wait, never block
    return con


# --- sensitivity (ported verbatim from codegraph_index/sensitivity.py) ----

def _visible_repos(con, caller_cell: str, allowlist: dict) -> set:
    out = set()
    for name, vis in con.execute("SELECT name, visibility FROM repos"):
        if vis == "public" or caller_cell in allowlist.get(name, []):
            out.add(name)
    return out


# --- query (ported verbatim from codegraph_index/query.py) ----------------

def structural_query(term, *, index_path, caller_cell, limit=8, allowlist=None) -> list:
    """Ranked structural search. A8-gated, A2 fail-safe: any sqlite3.Error
    (missing index, corrupt db, malformed MATCH, locked db) -> []."""
    if allowlist is None:
        allowlist = _local_allowlist(index_path, caller_cell)
    try:
        con = _connect(index_path)
    except sqlite3.Error:
        return []
    try:
        allowed = _visible_repos(con, caller_cell, allowlist)  # A8: gate BEFORE any row leaves
        if not allowed:
            return []
        match = _sanitize_query(term)
        if not match:
            return []
        qmarks = ",".join("?" * len(allowed))
        rows = con.execute(
            f"SELECT s.id,s.repo,s.name,s.kind,s.file_path,s.start_line,s.qualified_name,s.docstring,s.signature, "
            f"bm25(symbols_fts, 10.0, 4.0, 1.0, 2.0) AS score "
            f"FROM symbols_fts f JOIN symbols s ON s.id=f.rowid "
            f"WHERE symbols_fts MATCH ? AND s.repo IN ({qmarks}) "
            # 'import' nodes are references, never the *definition* a "which
            # symbol implements X" query wants — same principle as the
            # already-excluded 'file' kind.
            f"AND s.kind NOT IN ('file','import') AND s.name NOT LIKE 'test\\_%' ESCAPE '\\' "
            # test-file filter ANCHORED to a path segment/basename-start — an unanchored
            # '%test_%' would blackhole real files like workers/backtest_engine.py (contains
            # the substring 'test_'). Basename-after-a-slash starting test_, OR root-level test_*.
            f"AND s.file_path NOT LIKE '%/tests/%' "
            f"AND s.file_path NOT LIKE '%/test\\_%' ESCAPE '\\' "
            f"AND s.file_path NOT LIKE 'test\\_%' ESCAPE '\\' "
            f"ORDER BY bm25(symbols_fts, 10.0, 4.0, 1.0, 2.0) LIMIT ?",
            [match, *sorted(allowed), limit]).fetchall()
        out = []
        for sid, repo, name, kind, fp, line, qualified_name, docstring, signature, score in rows:
            # Only real call edges count as "callers" — the derived index carries CodeGraph's
            # verbatim edge kinds (contains/imports/decorates/references/calls/...), and counting
            # all of them would pollute the blast-radius number. 'calls' (plural) is CodeGraph's
            # actual call-edge kind.
            callers = con.execute(
                "SELECT COUNT(*) FROM edges WHERE dst_symbol=? AND edge_type='calls'", (sid,)
            ).fetchone()[0]
            out.append({"repo": repo, "name": name, "kind": kind,
                        "file_path": fp, "start_line": line, "callers": callers, "score": score,
                        "qualified_name": qualified_name or "", "docstring": docstring or "",
                        "signature": signature or ""})
        return out
    # OperationalError (lock/busy, or a malformed FTS5 MATCH query string) OR DatabaseError
    # (corrupt/non-sqlite file) — both are sqlite3.Error siblings — fail safe (A2), never raise/hang.
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _local_allowlist(index_path: str, caller_cell: str) -> dict:
    """Operate-what-you-own: the operator of the LOCAL index sees every repo
    in it. A8's cross-cell scoping still applies to the caller_cell identity;
    here the caller is the local operator, so grant it each repo present.
    Fail-safe: unreadable index -> empty allowlist (structural_query then
    returns [] via its own A8 gate)."""
    try:
        # A2: bound the connect + wait so a locked db can never hang the
        # default query path (mirrors _connect's busy_timeout guarantee).
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=0.1)
        con.execute("PRAGMA busy_timeout=100")
        repos = [r[0] for r in con.execute("SELECT name FROM repos")]
        con.close()
    except sqlite3.Error:
        return {}
    return {r: [caller_cell] for r in repos}


def format_human(rows, term) -> str:
    if not rows:
        return f"No structural matches for: {term!r}"
    lines = [f"Structural matches for {term!r} ({len(rows)}):"]
    for i, r in enumerate(rows, 1):
        loc = f"{r['file_path']}:{r['start_line']}"
        n = r["callers"]
        callers = f"  ({n} caller{'s' if n != 1 else ''})" if n else ""
        lines.append(f"{i}. {r['name']}  [{r['kind']}]  {r['repo']}  {loc}{callers}")
        if r["signature"]:
            lines.append(f"     {r['signature']}")
        if r["docstring"]:
            first = r["docstring"].strip().splitlines()[0][:100] if r["docstring"].strip() else ""
            if first:
                lines.append(f"     {first}")
    return "\n".join(lines)


def run_codegraph(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="swarph codegraph",
        description="On-demand structural code search: find where a symbol is defined or what calls it, "
                    "across the indexed repos. Natural-language query.")
    ap.add_argument("query", help="natural-language query, e.g. 'which function escapes HTML'")
    ap.add_argument("--index", default=DEFAULT_INDEX, help=f"index db (default {DEFAULT_INDEX})")
    ap.add_argument("--caller-cell", default=DEFAULT_CALLER_CELL, help="A8 caller identity (default lab-ovh)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    a = ap.parse_args(argv)
    rows = structural_query(a.query, index_path=a.index, caller_cell=a.caller_cell, limit=a.limit)
    print(json.dumps(rows, indent=2) if a.json else format_human(rows, a.query))
    return 0
