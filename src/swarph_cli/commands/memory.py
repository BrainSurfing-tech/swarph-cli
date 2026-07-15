"""``swarph memory`` — DETERMINISTIC OKF memory navigation over gbrain.

The knowledge-hemisphere counterpart to ``swarph codegraph``. When an agent
wants an EXACT canonical memory fact — a known page, every page of a tag, or
a concept's neighbours — it invokes this verb. Fuzzy/semantic recall stays
with ``swarph brain-ask`` and the per-prompt retrieval hook; the intent to
consult a *specific* fact lives with the caller (see README §The AI Router).

Routing (evidence from the #33 LOCOMO benchmark): deterministic get/list/links
is strongest for single-hop CANONICAL lookup ("the precise page for X", "all
`reference`-tagged pages"); relational/multi-hop recall is the weak spot that
``links`` graph-traversal targets. Reach for semantic ``brain-ask`` when you
don't yet know which page you need.

Transport mirrors ``brain-ask``: HTTP MCP to gbrain (:8792/mcp, Bearer). Calls
the deterministic ``get_page`` / ``list_pages`` MCP tools (NOT semantic
``query``). Read-only; stdlib-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from swarph_cli.commands import brain_ask


def _strip_sse(raw: str) -> str:
    """gbrain MCP may reply as SSE (``data:``-prefixed lines) OR plain JSON —
    mirror brain_ask._parse_query_response: return the JSON payload either way."""
    if "data:" in raw:
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("data:"):
                return s[len("data:"):].strip()
    return raw


def _mcp_call(url: str, token: str, tool: str, arguments: dict):
    """Generic gbrain MCP tools/call → parsed tool output. Reuses brain_ask's
    HTTP seam so endpoint/token behaviour stays identical across verbs. Handles
    gbrain's SSE framing + the ``result.content[].text`` = JSON-string envelope."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    raw = brain_ask._http_post(url, body, token, accept="application/json")
    envelope = json.loads(_strip_sse(raw))
    content = (envelope.get("result") or {}).get("content") or []
    for part in content:
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except (json.JSONDecodeError, TypeError):
                return part["text"]
    return envelope.get("result")


def get_page(url: str, token: str, slug: str) -> dict:
    """Fetch one page by exact slug via the ``get_page`` MCP tool."""
    out = _mcp_call(url, token, "get_page", {"slug": slug})
    return out if isinstance(out, dict) else {}


def run_memory(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="swarph memory",
        description="Deterministic OKF memory navigation over gbrain "
                    "(get/list/links). Fuzzy recall = `swarph brain-ask`.")
    parser.add_argument("--token-file", default=None,
                        help="path to a gbrain read token (else GBRAIN_TOKEN / "
                             "SWARPH_BRAIN_TOKEN / mesh peer token)")
    sub = parser.add_subparsers(dest="subcommand")

    p_get = sub.add_parser("get", help="read one page by exact slug")
    p_get.add_argument("slug")
    p_get.add_argument("--json", action="store_true", help="raw page JSON")

    args = parser.parse_args(argv)
    url = brain_ask._resolve_endpoint()
    token = brain_ask._resolve_token(args.token_file, brain_ask._self_name())

    if args.subcommand == "get":
        try:
            page = get_page(url, token, args.slug)
        except Exception as e:  # fail-safe: never traceback at the CLI
            print(f"swarph memory get: gbrain unreachable ({e})", file=sys.stderr)
            return 1
        if not page:
            print(f"swarph memory get: no page {args.slug!r}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(page, indent=2))
        else:
            print(page.get("content", "") or json.dumps(page, indent=2))
        return 0

    parser.print_help()
    return 0
