"""``swarph brain-ask`` — search the swarph-brain (gbrain) memory; optional $0 synthesis.

Generalizes the standalone ``swarph-brain-ask`` script into a first-class swarph-cli
verb so any cell can search the swarm's shared memory the same way. Two modes:

  * retrieval (``--no-synth``): print the top-k gbrain memory chunks (raw ``query``).
  * synthesis (default, when a facade is configured): retrieve, then ask a
    claude-service-style $0 facade to write a cited prose answer from the chunks.

Stdlib-only. Config from the environment, mirroring ``swarph mesh``'s token model:

  GBRAIN_MCP_URL        gbrain MCP endpoint; falls back to SWARPH_BRAIN_MCP, else
    / SWARPH_BRAIN_MCP   http://100.107.222.72:8792/mcp (tailnet IP — gbrain
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
import sys
import urllib.request

from swarph_cli import tokens
from pathlib import Path
from typing import Optional

# TAILNET IP, NOT loopback (card #548): measured 2026-08-23, gbrain binds
# 100.107.222.72:8792 ONLY — 127.0.0.1:8792 refuses on the gateway box itself.
# A loopback default is deaf everywhere, including on the box running gbrain.
_DEFAULT_GBRAIN = "http://100.107.222.72:8792/mcp"
_DEFAULT_TOPK = 6


def _resolve_endpoint() -> str:
    """Endpoint precedence: GBRAIN_MCP_URL > SWARPH_BRAIN_MCP > tailnet default.

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


# The fallback is ANOTHER CELL'S NAME. Harmless on lab-ovh, silently wrong everywhere
# else — and it is what made the cold-env failure report a TOKEN fault: with SWARPH_SELF
# unset the cell hunts lab-ovh.peer_token, finds nothing, and blames the credential.
# MEASURED 2026-07-29 on 6 of 6 cells (droplet isolated it: adding ONLY the two env vars,
# changing NO token, turned exit 2 into exit 0 at 1.00).
# The default is KEPT (removing it would break callers that rely on it) and ANNOUNCED.
_DEFAULT_SELF = "lab-ovh"


def _self_name() -> str:
    return os.environ.get("SWARPH_SELF") or os.environ.get("SWARPH_NODE") or _DEFAULT_SELF


def _self_name_is_defaulted() -> bool:
    """True when no env named this cell and we fell back to _DEFAULT_SELF."""
    return not (os.environ.get("SWARPH_SELF") or os.environ.get("SWARPH_NODE"))


def env_diagnosis() -> str:
    """Name the ENVIRONMENT fault before any downstream symptom, or '' if env is sane.

    >>> THE ERROR MUST NAME A DIMENSION THE CALLER CAN ACT ON. <<< Every cell that hit
    this went looking at credentials, because that is what the message said. The real
    fault is missing env in a non-interactive context (cron / systemd / env -i), where
    a sourced profile, a bashrc or a settings.json `env` block never applies.
    """
    bits = []
    if _self_name_is_defaulted():
        bits.append(f"SWARPH_SELF unset — defaulting to {_DEFAULT_SELF!r}, which is "
                    f"probably NOT this cell (so any peer-token lookup will hunt the "
                    f"wrong file)")
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

    `_self_name()` NEVER returns empty — it falls back to _DEFAULT_SELF
    ("lab-ovh"). So `bool(self_name)` is always true here, and passing that as
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

    Explicitness therefore comes from `_self_name_is_defaulted()`, not from
    truthiness: a guessed name keeps the old env-first order exactly, and only a
    name the operator actually supplied earns precedence over ambient values.
    """
    res = tokens.resolve_token(
        self_name or None,
        token_file,
        env_keys=("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"),
        identity_is_explicit=bool(self_name) and not _self_name_is_defaulted(),
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

    gw = os.environ.get("SWARPH_BRAIN_GATEWAY")
    if gw:
        self_name = _self_name()
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
