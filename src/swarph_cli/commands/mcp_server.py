"""``swarph mcp-server`` — Door 1 of the swarph reach strategy.

Runs an MCP (stdio) server that exposes the swarph capabilities as MCP
tools, so any MCP host (Claude Desktop, Cursor, a future OpenClaw service)
can mount it and its AI gets the swarph toolbelt — discover + install
capabilities with no human CLI use.

A host mounts it with a stdio server entry, e.g. in ``.mcp.json`` /
``claude_desktop_config.json``::

    {"mcpServers": {"swarph": {"command": "swarph",
                               "args": ["mcp-server"]}}}

Six tools are exposed, each backed by a plain, unit-testable helper so the
tool LOGIC is tested without spinning up the stdio transport:

* ``swarph_search(query)``           → :func:`_search` — metaedge.surf semantic search.
* ``swarph_add(uri)``                → :func:`_add`    — trust-gated + scanned install.
* ``swarph_describe(uri)``           → :func:`_describe` — parse a URI, no install.
* ``swarph_codegraph_query(query)``  → :func:`_codegraph_query` — structural code
  search (definitions + call sites) over the local codegraph index.
* ``swarph_memory_navigate(op, ...)`` → :func:`_memory_navigate` — deterministic
  OKF memory navigation (get/list/links) over gbrain.
* ``swarph_timeline_navigate(op, ...)`` → :func:`_timeline_navigate` — deterministic
  OKF temporal navigation (range/around/since) over the git-backed timeline.

The MCP Python SDK (``mcp``) is an OPTIONAL extra. :func:`run_mcp_server`
fails gracefully with a ``pip install swarph-cli[mcp]`` hint when it's absent;
the helpers themselves carry no MCP dependency, so they (and their tests) run
without the SDK installed.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

from swarph_cli.commands import add
from swarph_cli.commands import codegraph
from swarph_cli.commands import brain_ask, memory, timeline

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

#: Default search face. Overridable via ``SWARPH_METAEDGE_URL`` or the
#: ``--metaedge-url`` flag.
_DEFAULT_METAEDGE_URL = "https://metaedge.surf"

#: Module-level URL the FastMCP tool wrappers read. ``run_mcp_server`` sets
#: this from env / ``--metaedge-url`` before ``mcp.run()`` so a single source
#: of truth feeds every ``swarph_search`` call.
_METAEDGE_URL = os.environ.get("SWARPH_METAEDGE_URL", _DEFAULT_METAEDGE_URL)

#: Module-level local codegraph index path the FastMCP tool wrapper reads.
#: Overridable via ``SWARPH_CODEGRAPH_INDEX`` (mirrors ``codegraph.DEFAULT_INDEX``).
_CODEGRAPH_INDEX = os.path.expanduser(
    os.environ.get("SWARPH_CODEGRAPH_INDEX", "~/.swarph/codegraph/index.db")
)


def _metaedge_token() -> str | None:
    """Optional bearer token for the search face (``SWARPH_METAEDGE_TOKEN``)."""
    return os.environ.get("SWARPH_METAEDGE_TOKEN") or None


# --------------------------------------------------------------------------- #
# Tool logic — plain helpers (no MCP dependency)
# --------------------------------------------------------------------------- #


def _search(query: str, *, url: str, token: str | None = None) -> list[dict]:
    """Search the swarph via metaedge.surf's ``/api/ask`` endpoint.

    POSTs ``{"message": query}`` (plus ``"token"`` when given) to
    ``{url}/api/ask``, parses the JSON, and returns its ``results`` list. NEVER
    raises — any network/parse error (or a body without a list ``results``)
    returns ``[]`` so the host AI gets a clean empty result rather than a
    transport exception.
    """
    endpoint = url.rstrip("/") + "/api/ask"
    payload: dict = {"message": query}
    if token:
        payload["token"] = token
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    results = body.get("results") if isinstance(body, dict) else None
    return results if isinstance(results, list) else []


def _add(uri: str) -> dict:
    """Install a swarph artifact by its ``swarph://`` URI.

    Parses the URI FIRST — a malformed URI returns an error dict WITHOUT
    calling :func:`add.run_add` (so a bad URI never reaches the installer).
    Otherwise delegates to ``add.run_add([uri, "--yes"])``, whose trust gate +
    static-scan watchtower decide whether anything installs, and reports the
    int exit code (0 = installed).
    """
    try:
        add.parse_uri(uri)
    except ValueError as exc:
        return {"installed": False, "code": 2, "detail": str(exc)}

    code = add.run_add([uri, "--yes"])
    if code == 0:
        detail = f"installed {uri}"
    else:
        detail = (
            f"not installed (exit {code}) — likely trust-gated "
            f"(untrusted publisher) or failed the security scan"
        )
    return {"installed": code == 0, "code": code, "detail": detail}


def _describe(uri: str) -> dict:
    """Parse a ``swarph://`` URI and report what it points at, no install.

    Returns the five fields of the parsed reference; a malformed URI returns
    ``{"error": "<msg>"}``.
    """
    try:
        ref = add.parse_uri(uri)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "class": ref.klass,
        "publisher": ref.publisher,
        "name": ref.name,
        "version": ref.version,
        "sha256": ref.sha256,
    }


def _codegraph_query(query: str, *, limit: int = 8) -> list[dict]:
    """Structural code search over the local codegraph index (Task 1).

    Delegates to :func:`codegraph.structural_query` against the module-level
    ``_CODEGRAPH_INDEX`` path, using ``SWARPH_CELL`` (else
    ``codegraph.DEFAULT_CALLER_CELL``) as the A8 caller identity and the
    default operate-what-you-own allowlist (``allowlist=None``).

    NEVER raises — any exception (missing index, corrupt db, bad query, or
    anything else) is swallowed and returns ``[]`` so the MCP host always
    gets a clean empty result rather than a transport error.

    Returns COMPACT rows only — score/qualified_name/docstring are dropped
    to keep the tool output tight for the calling agent.
    """
    try:
        caller_cell = os.environ.get("SWARPH_CELL", codegraph.DEFAULT_CALLER_CELL)
        rows = codegraph.structural_query(
            query,
            index_path=_CODEGRAPH_INDEX,
            caller_cell=caller_cell,
            limit=limit,
            allowlist=None,
        )
        return [
            {
                "name": r["name"],
                "kind": r["kind"],
                "repo": r["repo"],
                "file_path": r["file_path"],
                "start_line": r["start_line"],
                "signature": r["signature"],
                "callers": r["callers"],
            }
            for r in rows
        ]
    except Exception:
        return []


def _memory_navigate(op: str, slug: str | None = None, type: str | None = None,
                     tag: str | None = None, limit: int = 20,
                     depth: int = 1, direction: str = "out"):
    """Deterministic OKF memory navigation — the knowledge-hemisphere twin of
    swarph_codegraph_query. Ops: 'get', 'list', 'links', 'backlinks' (incoming),
    'traverse' (multi-hop OKF edges via depth/direction). File-native graph.

    Fail-safe: any backend/parse error, or an unrecognised op, resolves to [] or
    {} — never raises."""
    try:
        url = brain_ask._resolve_endpoint()
        token = brain_ask._resolve_token(None, brain_ask._self_name())
        if op == "get" and slug:
            return memory.get_page(url, token, slug)
        if op == "list":
            return memory.list_pages(url, token, type, tag, limit)
        if op == "links" and slug:
            return memory.links(url, token, slug)
        if op == "backlinks" and slug:
            return memory.backlinks(url, token, slug)
        if op == "traverse" and slug:
            return [memory._as_okf_edge(f, t, d, h)
                    for (f, t, h, d) in memory.traverse(url, token, slug, depth, direction)]
        return []
    except Exception:
        return []


def _timeline_navigate(op: str, start: str = "", end: str = "", date: str = "",
                       window: str = "3d"):
    """Deterministic temporal lookup over the git timeline. op: 'range'|'around'|'since'.
    Returns OKF node/edge records (list). Fail-safe: any bad input/op/read → [] (never raises).

    Ops are explicitly whitelisted (mirrors ``_memory_navigate``): ``timeline._bounds``
    falls through to ``(None, None)`` for an unrecognized subcommand, which would
    otherwise match every entry unfiltered rather than yielding an empty result."""
    try:
        if op not in ("range", "around", "since"):
            return []
        ns = argparse.Namespace(subcommand=op, start=start, end=end, date=date,
                                window=window)
        lo, hi = timeline._bounds(ns)
        entries = timeline.load_entries(timeline._timeline_path())
        hits = [e for e in entries if (lo is None or e.ts >= lo) and (hi is None or e.ts <= hi)]
        return [timeline._as_okf(e) for e in hits]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# FastMCP server — thin @tool wrappers over the helpers above
# --------------------------------------------------------------------------- #
#
# Build at import time so tests can introspect the registered tools without
# running the stdio transport. The ``mcp`` SDK is an optional extra; if it's
# absent, ``mcp`` is None and ``run_mcp_server`` prints the install hint. The
# helpers above carry no MCP dependency, so they import + test fine either way.

try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("swarph")

    @mcp.tool()
    def swarph_search(query: str) -> list[dict]:
        """Search the swarph for a capability (hooks, MCP servers, skills, tools, libraries). Returns matches with an install URI where applicable."""
        return _search(query, url=_METAEDGE_URL, token=_metaedge_token())

    @mcp.tool()
    def swarph_add(uri: str) -> dict:
        """Install a swarph artifact by its swarph:// URI (trust-gated + security-scanned). Returns whether it installed."""
        return _add(uri)

    @mcp.tool()
    def swarph_describe(uri: str) -> dict:
        """Parse a swarph:// URI and show what it points at, without installing."""
        return _describe(uri)

    @mcp.tool()
    def swarph_codegraph_query(query: str) -> list[dict]:
        """Find where a function/class/method/symbol is DEFINED, or what CALLS it, across the indexed code repositories. Use this whenever you are reading, writing, debugging, or navigating code and need to locate a definition or trace call relationships by natural-language description (e.g. 'which function escapes HTML', 'where is the retry-parse helper defined', 'what calls the circuit breaker'). Returns ranked symbols with file path, line, signature, and caller count."""
        return _codegraph_query(query)

    @mcp.tool()
    def swarph_memory_navigate(op: str, slug: str = "", tag: str = "",
                               type: str = "", limit: int = 20,
                               depth: int = 1, direction: str = "out"):
        """Deterministic OKF memory navigation over gbrain: op='get'|'list'|'links'|'backlinks'|'traverse'.
        Exact canonical recall + file-native graph traversal (backlinks, multi-hop
        --depth/--direction) — the knowledge-hemisphere counterpart to
        swarph_codegraph_query. For fuzzy recall use semantic search instead."""
        return _memory_navigate(op, slug=slug or None, type=type or None,
                                tag=tag or None, limit=limit,
                                depth=depth, direction=direction)

    @mcp.tool()
    def swarph_timeline_navigate(op: str, start: str = "", end: str = "",
                                 date: str = "", window: str = "3d"):
        """Deterministic temporal lookup over the swarph timeline: op='range'|'around'|'since'.
        The temporal hemisphere of the OKF traversal brain — entries are dated OKF nodes with
        [[link]] edges into knowledge. Complements semantic recall; $0, no model."""
        return _timeline_navigate(op, start=start, end=end, date=date, window=window)

except ImportError:  # pragma: no cover - exercised only when SDK absent
    mcp = None


_MCP_MISSING_HINT = (
    "swarph mcp-server: the MCP Python SDK is not installed. Install it with:\n"
    "    pip install 'swarph-cli[mcp]'\n"
    "(or: pip install 'mcp>=1.0')"
)


def run_mcp_server(argv) -> int:
    """``swarph mcp-server [--metaedge-url URL]`` — run the stdio MCP server.

    Sets the module search-face URL from ``--metaedge-url`` (else the
    ``SWARPH_METAEDGE_URL`` env / default), then serves the three swarph tools
    over stdio via FastMCP. Returns non-zero with an install hint when the
    ``mcp`` SDK is absent.
    """
    parser = argparse.ArgumentParser(
        prog="swarph mcp-server",
        description=(
            "Run an MCP (stdio) server exposing swarph_search / swarph_add / "
            "swarph_describe / swarph_codegraph_query so any MCP host's AI "
            "gets the swarph toolbelt."
        ),
    )
    parser.add_argument(
        "--metaedge-url",
        default=None,
        help=(
            "Search-face base URL (default: $SWARPH_METAEDGE_URL or "
            f"{_DEFAULT_METAEDGE_URL})."
        ),
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if mcp is None:
        print(_MCP_MISSING_HINT)
        return 1

    if args.metaedge_url:
        global _METAEDGE_URL
        _METAEDGE_URL = args.metaedge_url

    # Serve over stdio (blocks until the host disconnects).
    mcp.run()
    return 0
