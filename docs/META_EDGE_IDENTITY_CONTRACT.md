# Meta-Edge ↔ swarph-gateway identity-token contract (v1)

The single integration point between the two halves of **B1** (access/identity blueprint, `project_swarph_access_identity_architecture`). **Meta-Edge issues; the gateway trusts.** Nail this and the two sides build independently — Meta-Edge owns the SSO + token issuance (the commander PWA surface); the gateway owns trust + cell-scoping (this repo).

## Token: a signed JWT (RS256)

Meta-Edge signs with its **private** key; the gateway verifies with Meta-Edge's **public** key only. The gateway can never *forge* a token — that asymmetry is the clean identity↔relay separation. (HS256/shared-secret is explicitly rejected — see Verification.)

## Claims

| claim | value / meaning | gateway use |
|---|---|---|
| `iss` | `"meta-edge"` (the issuer) | MUST equal the configured issuer |
| `aud` | `"swarph-gateway"` | MUST equal the configured audience |
| `sub` | the **canonical Meta-Edge user id** — stable across providers/logins | the OWNER key (B2 scoping) |
| `exp` | expiry, short (~1h; Meta-Edge refreshes) | MUST be in the future |
| `iat` | issued-at | informational |
| `provider` | `github` \| `google` \| `apple` (IdP used) | informational / audit |
| `login` | provider handle (e.g. GitHub username) | informational / display |

`sub` is the **canonical Meta-Edge user id**: Meta-Edge unifies a user's multiple provider logins (GitHub/Google/Apple) into ONE id, so the gateway sees one stable owner regardless of which IdP they signed in with. The gateway never sees raw provider ids.

## Key distribution

- **v1:** the gateway holds Meta-Edge's RSA public key as a PEM via `META_EDGE_PUBLIC_KEY` (env value or `META_EDGE_PUBLIC_KEY_FILE` path). Issuer/audience via `META_EDGE_ISSUER` (default `meta-edge`) / `META_EDGE_AUDIENCE` (default `swarph-gateway`).
- **v2:** a JWKS endpoint (`/.well-known/jwks.json`) for key rotation. Out of scope for v1.

## Gateway verification — FAIL-CLOSED (the security core)

On a Bearer that is shaped like a JWT (three `.`-separated segments) AND `META_EDGE_PUBLIC_KEY` is configured, verify with **no algorithm flexibility**, rejecting → **401** on any failure:

1. **`algorithms=["RS256"]` pinned explicitly** — reject `alg:none`, reject HS256 (algorithm-confusion / public-key-as-HMAC-secret attack). Never let the token header pick the algorithm.
2. **signature** valid against the Meta-Edge RSA public key.
3. **`exp`** not passed (small leeway ≤ 30s for clock skew); `iat`/`nbf` sane.
4. **`aud`** == configured audience (PyJWT `audience=`, `verify_aud=True`).
5. **`iss`** == configured issuer (PyJWT `issuer=`, `verify_iss=True`).

On success → `AuthContext(kind="user_identity", user=<sub>, provider=<provider>, login=<login>)`. **Any** failure → 401, never a partial-trust path. A JWT-shaped bearer that fails verification is NOT silently treated as a shared/peer token (no fall-through).

## Authorization (B2 — login-scoped registry)

A `user_identity` is authorized over the cells it **owns** (`claude_peers.owner == sub`):
- `/peers/register` stamps `owner = auth.user` (the verified `sub`).
- `/peers` (list) returns **only** the caller's owned peers when the caller is a `user_identity`.
- The shared/commander token and per-peer tokens are **unchanged** (lab paths): the commander/shared token still sees all; per-peer tokens behave as today. `owner` is a new **nullable** column (additive migration; existing/lab peers land `owner = NULL`).

## Cell-join / peer-add — the `tailscale up` analog (commander 2026-06-27)

The whole model mirrors **Tailscale**: SSO login (the account) + peer-add (a node joins *your* tailnet under your login), with the gateway as the coordination server. A **cell ≈ a Tailscale node** — it has its OWN identity (the gateway's per-peer token = the node-key) AND an OWNER (the user `sub` = "this node is in your tailnet").

A cell joins a user's swarph two ways, exactly like Tailscale:
- **Interactive** — the user's app (holding a Meta-Edge identity JWT) registers the cell on the user's behalf: `/peers/register` with the user JWT → `owner = sub`.
- **Headless (join-key) — the `tailscale up --authkey` analog.** The user mints a **join-key** in Meta-Edge (a Meta-Edge-issued JWT, same RS256 signing, claims `purpose:"cell-join"` + `owner:<sub>`, short-lived, ideally single-use). A headless cell on a server presents it to `/peers/register` → the gateway verifies it → registers the cell with `owner = the key's owner` + mints the cell's peer-token. **No interactive login on the cell** — the join-key carries the owner.

Meta-Edge therefore issues TWO token kinds, both RS256-signed, both verified the same fail-closed way (the §Verification steps apply to both — only the `purpose`/use differs):

| token | `purpose` claim | who holds it | gateway effect |
|---|---|---|---|
| **identity token** | absent / `"identity"` | the user's app / surface | `user_identity` AuthContext → read + scope owned cells |
| **join-key** | `"cell-join"` (+ `owner:<sub>`) | a joining (headless) cell | register the cell `owner=<sub>` + mint its peer-token |

Once joined, the cell authenticates with its own peer-token (unchanged lab mechanism); `owner` ties it to the user — the node-in-your-tailnet relationship. Revoke a cell or a user's access = delete/revoke at the gateway (the coordination server), exactly as Tailscale revokes a node.

## Coexistence

`user_identity` is a **third** `AuthContext` kind alongside `shared_token` and per-peer tokens. Same `_authorize` resolver, three principal types. The lab keeps its tokens; product users present Meta-Edge JWTs. The gateway is a resource-server-with-scoping; it never mints or signs identity tokens.
