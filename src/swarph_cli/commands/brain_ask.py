"""``swarph brain-ask`` — search the swarph-brain (gbrain) memory; optional $0 synthesis.

Generalizes the standalone ``swarph-brain-ask`` script into a first-class swarph-cli
verb so any cell can search the swarm's shared memory the same way. Two modes:

  * retrieval (``--no-synth``): print the top-k gbrain memory chunks (raw ``query``).
  * synthesis (default, when a facade is configured): retrieve, then ask a
    claude-service-style $0 facade to write a cited prose answer from the chunks.

Stdlib-only. Config from the environment, mirroring ``swarph mesh``'s token model:

  GBRAIN_MCP_URL        gbrain MCP endpoint; falls back to SWARPH_BRAIN_MCP, else
    / SWARPH_BRAIN_MCP   http://<gbrain-host>:8792/mcp (no default — gbrain
                          binds no loopback; measured 2026-08-23, card #548)
  GBRAIN_TOKEN          read token; falls back to SWARPH_BRAIN_TOKEN, then to the
    / SWARPH_BRAIN_TOKEN  mesh per-peer token (~/.config/swarph/<self>.peer_token).
    / peer-token file     Once gbrain accepts mesh peer tokens, the peer token IS
                          the read token — no separate secret to provision.
  SWARPH_BRAIN_GATEWAY  when set, query the brain via the mesh gateway's
                        /brain/query proxy using the cell's mesh peer token
                        (no per-cell gbrain_ token). Unset = direct :8792.
  SWARPH_FACADE         optional synthesis endpoint (claude-service chat-completions)
  SWARPH_FACADE_TOKEN   bearer for the facade
"""

from __future__ import annotations

import argparse
import json
import os

from swarph_cli import identity
import sys
import urllib.request

from swarph_cli import tokens
from pathlib import Path
from typing import Optional

# NO MODULE-LEVEL ENDPOINT CONSTANT, ON PURPOSE. gbrain binds its tailnet IP
# only (127.0.0.1:8792 refuses even on gbrain's own box), so SWARPH_BRAIN_MCP must
# be set explicitly and no host ships as a default (#548 -> #578).
# `_DEFAULT_GBRAIN = os.environ.get("SWARPH_BRAIN_MCP")` used to live here and was
# read at IMPORT time, which froze the developer's shell into the module:
# MEASURED in seat-A review of PR #318 — with SWARPH_BRAIN_MCP exported,
# test_resolve_endpoint_refuses_rather_than_guessing_a_host FAILED, because the
# test can delenv the two call-time operands but not a value already captured.
# The env is read at CALL time in _resolve_endpoint below, and nowhere else.
_DEFAULT_TOPK = 6


def _resolve_endpoint(explicit: str | None = None) -> str:
    """Endpoint precedence: GBRAIN_MCP_URL > SWARPH_BRAIN_MCP. No host default (#578).

    The SWARPH_BRAIN_MCP fallback keeps the verb config-compatible with the
    standalone ``swarph-brain-ask`` script (which reads SWARPH_BRAIN_*), so one
    env config works with both.
    """
    from swarph_cli.gateway_default import require_gateway

    return require_gateway(
        explicit
        or os.environ.get("GBRAIN_MCP_URL")
        or os.environ.get("SWARPH_BRAIN_MCP"),
        env="SWARPH_BRAIN_MCP",
        what="gbrain MCP",
    )


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


# card #402: there is NO default identity. The old fallback was ANOTHER CELL'S NAME
# ("lab-ovh"): harmless on lab-ovh, silently wrong everywhere else, and it made the
# cold-env failure report a TOKEN fault (with SWARPH_SELF unset the cell hunted
# lab-ovh.peer_token, found nothing and blamed the credential; measured 2026-07-29 on
# 6 of 6 cells). An undeclared cell now does no peer-token lookup at all: brain reads
# still work from GBRAIN_TOKEN / SWARPH_BRAIN_TOKEN / --token-file, and the gateway
# path, which needs the cell's own peer token, refuses with the missing variable named.
# The identity picks a CREDENTIAL file here, so this verb reads SWARPH_SELF (and its
# legacy SWARPH_NODE alias) only, like the mesh verbs: an ambient SWARPH_CELL, which
# psmux leaks from the spawning environment (#538), must not select another cell's
# peer token.
_IDENTITY_ENV = identity.SELF_ONLY + identity.NODE_ALIAS


def _self_name() -> Optional[str]:
    return identity.declared(env=_IDENTITY_ENV)[0]


def _self_name_is_undeclared() -> bool:
    """True when no flag or env names this cell. There is no fallback name."""
    return _self_name() is None


def env_diagnosis() -> str:
    """Name the ENVIRONMENT fault before any downstream symptom, or '' if env is sane.

    >>> THE ERROR MUST NAME A DIMENSION THE CALLER CAN ACT ON. <<< Every cell that hit
    this went looking at credentials, because that is what the message said. The real
    fault is missing env in a non-interactive context (cron / systemd / env -i), where
    a sourced profile, a bashrc or a settings.json `env` block never applies.
    """
    bits = []
    if _self_name_is_undeclared():
        bits.append("SWARPH_SELF unset — no cell identity is declared, so no peer-token "
                    "lookup is attempted (there is no default identity; card #402)")
    if not os.environ.get("SWARPH_BRAIN_GATEWAY"):
        bits.append("SWARPH_BRAIN_GATEWAY unset — falling back to a direct brain "
                    "connection, which only works where the brain service is reachable "
                    "AND a read token is provisioned")
    if not bits:
        return ""
    return ("swarph brain-ask: ENVIRONMENT INCOMPLETE — this is the likely cause:\n"
            + "".join(f"  · {b}\n" for b in bits)
            + "  If this ran from cron, a systemd unit or any non-interactive shell, set "
              "these in the UNIT's Environment=/EnvironmentFile= or at the top of the "
              "crontab. A sourced profile, ~/.bashrc or a settings.json env block does "
              "NOT reach those contexts.\n")


def _resolve_token(token_file: Optional[str], self_name: str) -> Optional[str]:
    """--token-file > per-identity peer token > GBRAIN_TOKEN > SWARPH_BRAIN_TOKEN.

    The env vars stay brain-specific on purpose — unifying the ORDER does not
    mean pretending every verb wants the same variables. What changed is that
    the peer token now outranks them when self_name is known, for the reason set
    out in swarph_cli.tokens: a per-identity secret must never lose to a
    process-global one, or `--as <cell>` stops meaning what it says on a host
    running more than one cell.

    >>> A DEFAULTED NAME IS NOT AN EXPLICIT IDENTITY. <<< gpt-ops caught this
    reviewing #190, and it is the sharpest kind of catch: I had noticed the
    hazard, written a comment about it, and not handled it.

    `_self_name()` used to NEVER return empty — it fell back to a default peer
    name ("lab-ovh"; removed by card #402). So `bool(self_name)` was always true, and passing that as
    identity_is_explicit would promote `lab-ovh.peer_token` — ANOTHER CELL'S
    CREDENTIAL — above GBRAIN_TOKEN on every invocation where nothing named the
    cell. The comment above already says why that fallback is dangerous
    ("harmless on lab-ovh, silently wrong everywhere else"); this would have
    turned a last-resort lookup into a first-choice one.

    gpt-ops' sharper point: it also made the resolver's "no explicit identity"
    unit test UNREACHABLE through this caller. A negative test whose subject
    cannot exhibit the positive is not a test — it passes and covers a branch
    production cannot enter. Hence the integration tests in
    tests/test_brain_ask_identity_explicitness.py, which exercise this function
    rather than the resolver, because a unit test at the resolver cannot see
    what the caller makes reachable.

    Explicitness therefore comes from whether a name was DECLARED, not from
    truthiness. Since card #402 nothing is ever guessed, so a declared name is
    the only kind there is, and an undeclared cell gets the env-first order.
    """
    res = tokens.resolve_token(
        self_name or None,
        token_file,
        env_keys=("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"),
        identity_is_explicit=self_name is not None,
    )
    return res.token if res is not None else None


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


def _gateway_query(gw_base: str, peer_token: str, question: str, limit: int) -> list:
    """Query the brain via the mesh gateway's /brain/query proxy, authenticating
    with the cell's MESH peer token. The gateway holds the gbrain token; we never do."""
    url = gw_base.rstrip("/") + "/brain/query"
    raw = _http_post(url, {"query": question, "limit": limit}, peer_token,
                     accept="application/json")
    return json.loads(raw).get("chunks", [])


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
            }
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
    parser.add_argument("--gateway", default=None,
                        help="gbrain MCP endpoint (env: GBRAIN_MCP_URL or SWARPH_BRAIN_MCP)")
    parser.add_argument("--token-file", default=None, help="explicit read-token file")
    args = parser.parse_args(argv)
    # #578/#318-review: resolve the endpoint HERE, not as an argparse default.
    # Evaluating it at parser-BUILD time made `--help` die and made the
    # `--gateway` the error message tells you to pass unreachable, because the
    # parser never got as far as parsing it. Same pattern as ratify:173.
    args.gateway = _resolve_endpoint(args.gateway)
    question = " ".join(args.question)

    gw = os.environ.get("SWARPH_BRAIN_GATEWAY")
    if gw:
        self_name = _self_name()
        if self_name is None:
            sys.stderr.write(env_diagnosis())
            sys.stderr.write(
                "swarph brain-ask: SWARPH_BRAIN_GATEWAY set but no cell identity is "
                "declared, so there is no peer token to present (set SWARPH_SELF)\n")
            return 2
        try:
            peer_token = _peer_token_path(self_name).read_text(encoding="utf-8").strip()
        except OSError:
            peer_token = ""
        if not peer_token:
            sys.stderr.write(env_diagnosis())
            sys.stderr.write(
                f"swarph brain-ask: SWARPH_BRAIN_GATEWAY set but no mesh peer token at "
                f"~/.config/swarph/{self_name}.peer_token\n")
            return 2
        try:
            chunks = _gateway_query(gw, peer_token, question, args.limit)
        except Exception as e:  # noqa: BLE001 — surface, don't swallow
            sys.stderr.write(f"swarph brain-ask: gateway brain query failed: {e}\n")
            return 1
    else:
        token = _resolve_token(args.token_file, _self_name())
        if not token:
            # Diagnosis FIRST: in a cold env the token is a SYMPTOM, not the fault.
            sys.stderr.write(env_diagnosis())
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
