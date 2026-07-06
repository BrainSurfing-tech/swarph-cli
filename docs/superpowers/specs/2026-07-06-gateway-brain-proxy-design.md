# Gateway brain-proxy — design

**Date:** 2026-07-06
**Component:** `swarph-cli` — `gateway/server.py` (new endpoint) + `commands/brain_ask.py` (client path)
**Status:** design approved (commander 2026-07-06); plan + implementation to follow on branch `feat/gateway-brain-proxy`.

## Problem

Querying the mesh brain (gbrain, `:8792/mcp`) requires a **separate per-cell `gbrain_` token**, distinct from the mesh identity every cell already holds (its peer/SSO token). That is a **double-auth smell** (commander, 2026-07-06): "not to run double auths anywhere — having another token when we already mint them on the mesh is weird."

The client already anticipates the fix. `brain_ask.py`'s token precedence is `GBRAIN_TOKEN → SWARPH_BRAIN_TOKEN → the mesh per-peer token`, with the comment *"Once gbrain accepts mesh peer tokens, the peer token IS [the token]."* The gap is server-side: gbrain accepts only its own `gbrain_` tokens (verified 2026-07-06 — a mesh peer token → **401** on `:8792/mcp`; a `gbrain_` token → **200**). The client is ready; the server side is unshipped.

Concretely, onboarding a cell to the brain today needs a `gbrain_` token minted, delivered securely, and rotated — friction that just blocked `workstation-lc`. And the mesh's shared `gbrain_` read token is **over-scoped** (`read/write/admin`, the 71-char api-key kind).

## Goal

A cell queries the brain using its **mesh peer/SSO token** — the identity already minted on the mesh — so **no per-cell `gbrain_` token exists**. Retire the double-auth without modifying gbrain (upstream `garrytan/gbrain`, MIT — the gateway is ours).

## Design

### 1. New gateway endpoint — `POST /brain/query`

Added to `gateway/server.py` alongside the existing routes (`/messages`, etc.), FastAPI `@app.post`.

- **Request:** `Bearer: <caller's mesh token>`, body `{"query": "<text>", "limit": <int, default 8>}`.
- **Auth:** reuse `_authorize(authorization)` **verbatim** (`server.py:764`) — it already validates all three mesh regimes (`meta_edge` SSO / `per_peer_token` / `shared_token`) and returns an `AuthContext(peer, regime, …)` NamedTuple, raising `HTTPException(401)` on a bad token. No new auth code.
- **Proxy:** the gateway **constructs** the gbrain MCP `query` call itself — `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query","arguments":{"query":<q>,"limit":<n>}}}` — and POSTs it to `GATEWAY_GBRAIN_URL` with `Authorization: Bearer <GATEWAY_GBRAIN_TOKEN>` and `Accept: application/json, text/event-stream`. It parses gbrain's SSE (`result.content[0].text` = a JSON chunk array) and returns a **clean JSON body** to the caller.
- **Response (200):** `{"chunks": [ {slug, title, chunk_text, score, …}, … ]}` — the gbrain chunk array, unwrapped from the SSE envelope.
- **Read-only by construction:** the gateway only ever builds a `name=query` MCP call. There is **no code path** that emits `write`/`admin`/`add_tag`/etc. So even though `GATEWAY_GBRAIN_TOKEN` is over-scoped, a caller can only ever read. Read-only is a property of the code, not a filter over caller input.
- **Attribution:** log the querying peer (`auth.peer`) — per-peer brain-query observability, free.

### 2. Gateway configuration (server-side only)

Two env vars, read at module top like the existing config (`os.environ.get("MESH_GATEWAY_TOKEN", "")` pattern, `server.py:62`):

- `GATEWAY_GBRAIN_URL` — default `http://100.107.222.72:8792/mcp`.
- `GATEWAY_GBRAIN_TOKEN` — the **one** gbrain read token the gateway holds. Never leaves the gateway host.

If `GATEWAY_GBRAIN_TOKEN` is unset, `/brain/query` returns `503` ("brain proxy not configured") — the endpoint is inert until the operator wires the token, so shipping the code is safe before deployment.

### 3. Client path — `brain_ask.py`

New precedence, additive:

- If `SWARPH_BRAIN_GATEWAY` is set, `brain-ask` POSTs `<SWARPH_BRAIN_GATEWAY>/brain/query` with `{"query", "limit"}` and `Authorization: Bearer <the cell's peer token>` (read from `~/.config/swarph/<self>.peer_token`, the path the code already computes in `_peer_token_path`). It reads `{"chunks": [...]}` back.
- If `SWARPH_BRAIN_GATEWAY` is unset → **today's direct `:8792` + `gbrain_` token path, unchanged** (backward-compat).

So the endpoint the operator configures decides the auth: gateway → peer token; direct `:8792` → `gbrain_` token. No mixing.

### 4. Backward compatibility (load-bearing)

- Direct `:8792` querying keeps working exactly as today — the proxy and the client gateway-path are **purely additive**. A cell with no `SWARPH_BRAIN_GATEWAY` set behaves identically to before.
- gbrain is **not modified**.

## Errors

| Condition | Response |
|---|---|
| Missing / bad mesh token | `401` (from `_authorize`, unchanged) |
| Missing/empty `query` in body | `400` |
| `GATEWAY_GBRAIN_TOKEN` unset | `503` "brain proxy not configured" |
| gbrain unreachable / non-2xx / unparseable SSE | `502` "brain upstream error" — **fail loud, never swallow** |

## Testing (TDD)

Gateway (`tests/` — the gateway suite mocks `httpx`/`urllib` upstreams; follow that pattern):
- **Auth:** valid peer token → `200`; missing/`Bearer bad` → `401` (via `_authorize`).
- **Read-only by construction:** the MCP body the proxy sends upstream always has `params.name == "query"` — assert it; there is no input that makes it write/admin.
- **Proxy round-trip:** a mocked gbrain SSE reply → the endpoint returns `{"chunks": [...]}` matching the chunk array.
- **Upstream failure:** gbrain 500 / connection error → `502` (not a swallowed empty result).
- **Unconfigured:** `GATEWAY_GBRAIN_TOKEN` unset → `503`.

Client (`tests/test_brain_ask*.py` pattern):
- `SWARPH_BRAIN_GATEWAY` set → POSTs `/brain/query` with the **peer token**, parses `{"chunks"}`.
- `SWARPH_BRAIN_GATEWAY` unset → the existing direct-`:8792` + `gbrain_` token path is unchanged (compat lock).

## Ship & rollout

- Version bump `0.25.0 → 0.26.0`; document `/brain/query` + the two gateway env vars + `SWARPH_BRAIN_GATEWAY` in the relevant help/README.
- swarph-cli is **public PyPI** — synthetic test fixtures only, no cell-private data, no real tokens.
- **All rollout steps commander-gated** and OUT of this plan's execution scope (plan ends at merged + green):
  1. Publish `0.26.0`.
  2. Set `GATEWAY_GBRAIN_URL` + `GATEWAY_GBRAIN_TOKEN` on the gateway host (lab-ovh) + restart the gateway.
  3. Set `SWARPH_BRAIN_GATEWAY` for `workstation-lc` (+ future cells) → her **peer token** now authenticates to the brain.
  4. **Revoke the stopgap `gbrain_` token** placed on workstation-lc.

## Security properties

- **One identity:** a cell's mesh token is its brain auth — no second credential to mint, deliver, or rotate.
- **Held token stays server-side:** `GATEWAY_GBRAIN_TOKEN` never reaches a cell.
- **Over-scope contained:** cells get **read-only** through the proxy even though the held token is `read/write/admin` — this partly closes the over-scope issue *without* waiting on the pending read-scoped re-mint.
- **Attribution:** every brain query carries the authenticated peer identity.

## Out of scope (YAGNI)

- Per-peer rate / spend caps on `/brain/query` (a future gateway concern; note, don't build).
- A `/brain/search` variant (the RRF `query` covers the need; add later if a keyword-only path is wanted).
- Server-side synthesis (`brain-ask` already synthesizes client-side via the $0 facade).
- Modifying gbrain to natively accept mesh tokens (the proxy makes this unnecessary; if ever wanted, it is a separate upstream discussion).
