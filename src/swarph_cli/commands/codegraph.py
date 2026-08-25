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
import sys

DEFAULT_INDEX = os.path.expanduser("~/.swarph/codegraph/index.db")
# NOTE: named DEFAULT_CALLER_CELL, not "*_CALLER" — this is an A8 caller-CELL
# identity (sensitivity-gate scoping), not a SwarphCall `role.subrole.specific`
# caller tag, and must NOT trip the repo-wide caller-convention meta-guard
# (tests/test_caller_meta_guard.py) which validates every `*_CALLER` constant
# against that unrelated dotted-slug convention.
DEFAULT_CALLER_CELL = "lab-ovh"

# NOT 1 and NOT 2. A distinct code so "nothing was searched" can never be confused
# with a crash or a usage error, and so a caller can branch on it specifically.
# This is the AVAILABILITY axis, the same thing the gateway answers with a 503 --
# separate from the VISIBILITY axis stdout carries. rc is the one channel the frozen
# stdout contract left free.
RC_NO_INDEX = 3

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


def _load_allowlist_file(index_path: str):
    """Load the owner-maintained ``allowlist.json`` beside the index
    (``{repo: [cell,...]}``, default-deny per repo). Returns the dict if the
    file is present, else ``None`` so the caller falls back to
    operate-what-you-own. Fail-SAFE and fail-CLOSED: a present-but-malformed
    file returns ``{}`` (deny-all-private, public still visible) and NEVER
    raises — a broken access policy must fail closed, not open."""
    p = os.path.join(os.path.dirname(os.path.expanduser(index_path)), "allowlist.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- query (ported verbatim from codegraph_index/query.py) ----------------

def structural_query(term, *, index_path, caller_cell, limit=8, allowlist=None) -> list:
    """Ranked structural search. A8-gated, A2 fail-safe: any sqlite3.Error
    (missing index, corrupt db, malformed MATCH, locked db) -> []."""
    if allowlist is None:
        # Prefer the owner-maintained allowlist.json (default-deny policy) when
        # present; only fall back to operate-what-you-own if there's no policy file.
        allowlist = _load_allowlist_file(index_path)
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
            #
            # >>> CARD #194 — THE COUNT IS SCOPED TO THE CALLER-VISIBLE REPOS, exactly
            # like the rows above. <<< This was an unscoped COUNT(*) while `s.repo IN
            # (allowed)` gated the rows, so a caller who could NOT see a private repo
            # still received a caller count that INCLUDED its callers.
            #
            # drop-on-meta-edge, seat-A: "a count that sums callers living in a private
            # repo LEAKS THE EXISTENCE AND SIZE of those private callers — the exact
            # payload #44's transport refuses, delivered as an integer instead of a
            # path." Measured 2026-07-31: cross-visibility `calls` edges were 0 on the
            # live index, so the leak was LATENT — but this is the blast-radius number
            # the feature exists to serve, and the gateway proxy would have exposed it
            # to every remote peer.
            #
            # The unifying rule: EVERY OBSERVABLE — rows, counts, scores, freshness —
            # must be a function of only the caller-visible subset, never the full
            # index. Gating the rows is ONE INSTANCE of it, not the whole of it.
            callers = con.execute(
                f"SELECT COUNT(*) FROM edges e JOIN symbols src ON src.id = e.src_symbol "
                f"WHERE e.dst_symbol=? AND e.edge_type='calls' AND src.repo IN ({qmarks})",
                (sid, *sorted(allowed))
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
        # >>> .get, AND ONLY HERE. <<< The RELAYED rows (#552) carry seven keys --
        # repo, name, kind, file_path, start_line, callers, signature -- and NOT
        # docstring, which the endpoint does not project. Every other key is common
        # to both producers, so they stay subscripted: a missing `name` or
        # `file_path` is a BROKEN PAYLOAD and must raise rather than format as blank.
        # Loosening all of them would turn a real fault into a quiet half-answer.
        doc = (r.get("docstring") or "").strip()
        if doc:
            first = doc.splitlines()[0][:100]
            if first:
                lines.append(f"     {first}")
    return "\n".join(lines)


def _resolve_gateway(arg) -> str:
    """Gateway base URL for the SEARCH-RELAY path (#552).

    >>> THE CANONICAL VARIABLE IS MESH_GATEWAY_URL, AND SWARPH_BRAIN_GATEWAY IS
    DELIBERATELY *NOT* IN THIS CHAIN. <<<

    The first draft copied `highlight`'s chain, which ends in SWARPH_BRAIN_GATEWAY.
    drop-on-meta-edge measured what that actually resolves to on this box:

        SWARPH_CODEGRAPH_GATEWAY   <unset>
        SWARPH_GATEWAY             <unset>
        SWARPH_BRAIN_GATEWAY       http://<host>:8788   <- brain-NAMED, MESH value
        MESH_GATEWAY_URL           http://<host>:8788   <- correct, and NOT consulted

    The relay worked ONLY because a brain-named variable was mis-set to the mesh
    gateway's address. Every link that resolved was unset or wrong-by-name, and the
    one that was right was the one not read -- while #546 canonicalised
    MESH_GATEWAY_URL across four modules the same morning.

    >>> AND THE FAILURE IS TRIGGERED BY SOMEONE DOING THE RIGHT THING. <<<
    SWARPH_BRAIN_GATEWAY names the brain (:8792, mint :8793) -- a different service on
    different ports. The day anyone points it where its NAME says, this POSTs to
    :8792/codegraph: a mesh peer token handed to a service it was not minted for, a
    non-2xx, the fallback, and droplet's original symptom returns ON THE DAY SOMEONE
    FIXED A VARIABLE. A fallback to a differently-named service is not a safety net;
    it re-arms the collision on the day it fires.

    `--gateway` / `--local` override.
    """
    return (arg
            or os.environ.get("SWARPH_CODEGRAPH_GATEWAY")
            or os.environ.get("SWARPH_GATEWAY")
            or os.environ.get("MESH_GATEWAY_URL")
            or "").strip()


def _query_via_gateway(gateway: str, query: str, limit: int, token_file):
    """POST to the gateway's /codegraph — THE SERVER SEARCHES, WE GET THE ANSWER.

    >>> WHY A RELAY AND NOT A SHIPPED INDEX (commander, 2026-08-21): one index,
    one truth. Copying the db to every cell makes N stores that drift, which is
    the divergence this fleet hit four separate ways the same week. <<<

    The endpoint takes NO caller field on purpose -- the caller comes from the
    bearer token and cannot be self-asserted the way `--caller-cell` can here.
    So the relayed path is strictly *less* spoofable than the local one.

    Returns (rows, index_present, freshness).

    index_present is False when the SERVER says 503 -- the availability axis, kept
    distinct from an empty result.

    >>> WE DELIBERATELY IGNORE THE BODY'S `index_present` FIELD. <<< (drop, review of
    PR #280, measured against the live endpoint.) The endpoint hardcodes it True on
    its only 200 path and raises 503 otherwise, so on the wire the field carries no
    information. The 503 IS the signal. A later "improvement" that reads the body
    field instead would be branching on a constant and would silently stop detecting
    the very thing it appears to check.

    `freshness` is a list of per-repo dicts -- repo, indexed_at, index_age_hours,
    stale, basis. The first cut of this relay DROPPED it, which meant the automatic
    grep hook warned about a stale index while the HAND-TYPED verb did not -- and the
    hand-typed verb is the consumer this relay was built for, because that is how
    droplet hit the original problem. Card #193 exists because this index has run
    BACKWARD before, so a stale index answering confidently is an OBSERVED failure,
    not a hypothetical.
    """
    from swarph_cli.commands.mesh import _post_json, _resolve_token  # local import: keeps
    # the local-only path free of any mesh dependency, so an offline cell still works.

    self_name = os.environ.get("SWARPH_SELF") or DEFAULT_CALLER_CELL
    token = _resolve_token(self_name, token_file)
    body = {"query": query, "limit": max(1, min(int(limit), 25))}
    status, payload = _post_json(gateway.rstrip("/") + "/codegraph", body, token)
    if status == 503:
        return [], False, []
    if status >= 400:
        raise RuntimeError(f"gateway returned {status}: {str(payload)[:160]}")
    payload = payload or {}
    return payload.get("results", []), True, (payload.get("freshness") or [])


def run_codegraph(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="swarph codegraph",
        description="On-demand structural code search: find where a symbol is defined or what calls it, "
                    "across the indexed repos. Natural-language query. Queries the mesh gateway when one "
                    "is configured (the server searches and returns the answer); falls back to a local index.")
    ap.add_argument("query", help="natural-language query, e.g. 'which function escapes HTML'")
    ap.add_argument("--index", default=DEFAULT_INDEX, help=f"index db (default {DEFAULT_INDEX})")
    ap.add_argument("--caller-cell", default=DEFAULT_CALLER_CELL, help="A8 caller identity (default lab-ovh)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ap.add_argument("--gateway", default=None,
                    help="mesh-gateway base URL for relayed search (else $SWARPH_CODEGRAPH_GATEWAY / "
                         "$SWARPH_GATEWAY / $SWARPH_BRAIN_GATEWAY). The server holds the index.")
    ap.add_argument("--local", action="store_true",
                    help="force the local index even when a gateway is configured")
    ap.add_argument("--token-file", default=None, help="explicit bearer token file")
    a = ap.parse_args(argv)

    gateway = "" if a.local else _resolve_gateway(a.gateway)
    source = "local"
    # The LOCAL path computes no freshness -- the index file is right there and the
    # hook that used to state its age reads the mtime itself. Only the relayed path
    # receives it, and only the relayed path can be silently answering from a store
    # the user cannot see.
    freshness = []
    if gateway:
        try:
            rows, index_present, freshness = _query_via_gateway(
                gateway, a.query, a.limit, a.token_file)
            source = "gateway"
        except Exception as exc:  # noqa: BLE001
            # >>> LOUD, NEVER A SILENT FALL BACK TO LOCAL. <<< A relay failure that
            # degrades quietly to an empty local index is indistinguishable from
            # "no matches" -- the exact confusion this change exists to remove.
            print(f"swarph codegraph: gateway {gateway} unreachable ({type(exc).__name__}: {exc}); "
                  f"falling back to the local index at {a.index}", file=sys.stderr)
            rows = structural_query(a.query, index_path=a.index,
                                    caller_cell=a.caller_cell, limit=a.limit)
            index_present = os.path.exists(os.path.expanduser(a.index))
    else:
        rows = structural_query(a.query, index_path=a.index,
                                caller_cell=a.caller_cell, limit=a.limit)
        index_present = os.path.exists(os.path.expanduser(a.index))

    stale = [f for f in freshness if f.get("stale")]
    if stale:
        # >>> A STALE INDEX ANSWERING CONFIDENTLY IS THE #193 FAILURE, AND IT LOOKS
        # EXACTLY LIKE A GOOD ANSWER. <<< stderr, not stdout: the rows are real and a
        # machine consumer should still get them unmodified. The user needs to know
        # the line numbers may have moved; the parser does not.
        for f in stale:
            print(f"swarph codegraph: STALE INDEX for {f.get('repo', '?')} — "
                  f"{f.get('index_age_hours', 0):.1f}h old ({f.get('basis', 'unknown basis')}); "
                  f"verify line numbers against the file before acting on them.",
                  file=sys.stderr)

    if not index_present:
        # >>> STDERR, AND --json STAYS A BARE LIST. <<< .claude/hooks/codegraph-on-grep.sh
        # does `rows=json.loads(...)` and iterates, so wrapping stdout in an envelope
        # would break the hook that fires on every grep in the fleet. The availability
        # signal therefore goes to stderr, where it costs no consumer anything.
        #
        # >>> THE EXIT CODE IS THE CHANNEL THAT WAS FREE ALL ALONG (drop, review of
        # PR #279). <<< The stdout contract is frozen -- codegraph-on-grep.sh does
        # json.loads() and iterates, so an envelope breaks the caller that fires on
        # every grep in the fleet. But rc costs that contract NOTHING: a consumer
        # that ignores rc sees exactly what it saw before, and one that reads it gets
        # the availability axis the gateway already expresses as a 503.
        #
        # The hook itself already branches on `[ $rc -ne 0 ]` and reports the query as
        # UNCORROBORATED, so this lands in a handler that exists.
        #
        # It also closes the ambiguity my previous commit MOVED rather than removed:
        # dropping the false "No structural matches" line left the human path at NO
        # STDOUT, NO STDERR (the hook redirects it), EXIT 0 -- and silence plus success
        # reads as "ran fine, nothing found". Same rc closes both halves.
        print(f"swarph codegraph: NO INDEX AVAILABLE (source={source}) — this is NOT a "
              f"negative result; nothing was searched.", file=sys.stderr)

    if a.json:
        print(json.dumps(rows, indent=2))
    elif not index_present:
        # >>> A WARNING IS NOT ENOUGH IF THE CLAIM IS STILL PRINTED UNDER IT. <<<
        # `format_human` says "No structural matches for X" -- a claim about the CODE,
        # made when nothing was searched. The human path owns both streams, so it does
        # not make the claim; a warning above a contradicting result just asks the
        # reader to pick, and they pick the one that looks like a result.
        pass
    else:
        print(format_human(rows, a.query))
    return 0 if index_present else RC_NO_INDEX
