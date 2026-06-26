"""``swarph brain-ask`` — search the swarph-brain (gbrain) memory; optional $0 synthesis.

Generalizes the standalone ``swarph-brain-ask`` script into a first-class swarph-cli
verb so any cell can search the swarm's shared memory the same way. Two modes:

  * retrieval (``--no-synth``): print the top-k gbrain memory chunks (raw ``query``).
  * synthesis (default, when a facade is configured): retrieve, then ask a
    claude-service-style $0 facade to write a cited prose answer from the chunks.

Stdlib-only. Config from the environment, mirroring ``swarph mesh``'s token model:

  GBRAIN_MCP_URL        gbrain MCP endpoint; falls back to SWARPH_BRAIN_MCP, else
    / SWARPH_BRAIN_MCP   http://127.0.0.1:8792/mcp
  GBRAIN_TOKEN          read token; falls back to SWARPH_BRAIN_TOKEN, then to the
    / SWARPH_BRAIN_TOKEN  mesh per-peer token (~/.config/swarph/<self>.peer_token).
    / peer-token file     Once gbrain accepts mesh peer tokens, the peer token IS
                          the read token — no separate secret to provision.
  SWARPH_FACADE         optional synthesis endpoint (claude-service chat-completions)
  SWARPH_FACADE_TOKEN   bearer for the facade
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional

_DEFAULT_GBRAIN = "http://127.0.0.1:8792/mcp"
_DEFAULT_TOPK = 6


def _resolve_endpoint() -> str:
    """Endpoint precedence: GBRAIN_MCP_URL > SWARPH_BRAIN_MCP > localhost default.

    The SWARPH_BRAIN_MCP fallback keeps the verb config-compatible with the
    standalone ``swarph-brain-ask`` script (which reads SWARPH_BRAIN_*), so one
    env config works with both.
    """
    return (os.environ.get("GBRAIN_MCP_URL")
            or os.environ.get("SWARPH_BRAIN_MCP")
            or _DEFAULT_GBRAIN)


def _build_query_request(question: str, limit: int = _DEFAULT_TOPK) -> dict:
    """The MCP JSON-RPC body for the gbrain ``query`` tool."""
    return {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "query",
                   "arguments": {"query": question, "limit": limit, "expand": False}},
    }


def _parse_query_response(raw: str) -> list:
    """Pull the JSON chunk array out of gbrain's SSE (or plain-JSON) reply."""
    payload = raw
    if "data:" in raw:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload = stripped[len("data:"):].strip()
                break
    doc = json.loads(payload)
    text = doc["result"]["content"][0]["text"]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else []


def _format_chunks(chunks: list) -> str:
    if not chunks:
        return "(no relevant memories found)"
    out = []
    for c in chunks:
        slug = c.get("slug", "?")
        score = c.get("score")
        body = (c.get("chunk_text") or c.get("title") or "").strip()
        head = f"[{slug}]"
        if isinstance(score, (int, float)):
            head += f" ({score:.2f})"
        out.append(f"{head}\n{body}")
    return "\n\n".join(out)


def _peer_token_path(self_name: str) -> Path:
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


def _self_name() -> str:
    return os.environ.get("SWARPH_SELF") or os.environ.get("SWARPH_NODE") or "lab-ovh"


def _resolve_token(token_file: Optional[str], self_name: str) -> Optional[str]:
    """Read token precedence: --token-file > GBRAIN_TOKEN > SWARPH_BRAIN_TOKEN > peer token."""
    if token_file:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    for var in ("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    try:
        tok = _peer_token_path(self_name).read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    return None


def _http_post(url: str, body: dict, token: str,
               accept: str = "application/json, text/event-stream",
               timeout: int = 30) -> str:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", accept)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed tailnet URL
        return resp.read().decode("utf-8")


def _mcp_query(url: str, token: str, question: str, limit: int) -> list:
    raw = _http_post(url, _build_query_request(question, limit), token)
    return _parse_query_response(raw)


def _synthesize(facade_url: str, facade_token: str, question: str, chunks: list) -> str:
    """Ask the $0 facade to answer ONLY from the retrieved chunks, citing slugs."""
    context = _format_chunks(chunks)
    sys_prompt = ("You are the swarph memory. Answer ONLY from the provided memory "
                  "chunks; cite the [slug] of each chunk you use; if the chunks do "
                  "not answer the question, say so plainly.")
    user = f"Question: {question}\n\nMemory chunks:\n{context}"
    body = {"model": os.environ.get("SWARPH_FACADE_MODEL", "claude"),
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user}],
            "max_tokens": 700, "temperature": 0.2}
    raw = _http_post(facade_url, body, facade_token, accept="application/json")
    doc = json.loads(raw)
    return doc["choices"][0]["message"]["content"].strip()


def run_brain_ask(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="swarph brain-ask",
        description="Search the swarph-brain (gbrain) memory; optional $0 cited synthesis.")
    parser.add_argument("question", nargs="+", help="the question to ask the swarm's memory")
    parser.add_argument("--limit", type=int, default=_DEFAULT_TOPK,
                        help="top-k chunks to retrieve (default 6)")
    parser.add_argument("--no-synth", action="store_true",
                        help="retrieval only — print raw chunks, skip prose synthesis")
    parser.add_argument("--gateway", default=_resolve_endpoint(),
                        help="gbrain MCP endpoint (env: GBRAIN_MCP_URL or SWARPH_BRAIN_MCP)")
    parser.add_argument("--token-file", default=None, help="explicit read-token file")
    args = parser.parse_args(argv)
    question = " ".join(args.question)

    token = _resolve_token(args.token_file, _self_name())
    if not token:
        sys.stderr.write(
            "swarph brain-ask: no gbrain read token "
            "(set GBRAIN_TOKEN / SWARPH_BRAIN_TOKEN, pass --token-file, or place a "
            "mesh peer token at ~/.config/swarph/<self>.peer_token)\n")
        return 2

    try:
        chunks = _mcp_query(args.gateway, token, question, args.limit)
    except Exception as exc:  # noqa: BLE001 — surface any transport/parse failure cleanly
        sys.stderr.write(f"swarph brain-ask: gbrain query failed: {exc}\n")
        return 1

    facade = os.environ.get("SWARPH_FACADE")
    if args.no_synth or not facade:
        print(_format_chunks(chunks))
        return 0

    try:
        answer = _synthesize(facade, os.environ.get("SWARPH_FACADE_TOKEN", ""),
                             question, chunks)
    except Exception as exc:  # noqa: BLE001 — never hard-fail; fall back to raw chunks
        sys.stderr.write(f"swarph brain-ask: synthesis failed ({exc}); raw chunks below:\n")
        print(_format_chunks(chunks))
        return 0
    print(answer)
    return 0
