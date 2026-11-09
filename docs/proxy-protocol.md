# Per-cell proxy protocol (card #20, phase 1)

The proxy holds the peer token under a separate Unix uid. The cell sends one
newline-terminated UTF-8 JSON request over its Unix socket; one connection carries
one request and one reply. Default socket: `/run/swarph-proxy/<cell>.sock`.
Requests and upstream response bodies are limited to 1 MiB. The socket checks the
connecting uid (Linux `SO_PEERCRED`, macOS `getpeereid`) against its configured cell
uid before routing. No direct-token fallback, caching, redirects, or automatic
retries are provided.

Connections are handled concurrently. Each accepted connection has a 20-second
request-read timeout; a silent client or a blocked upstream request does not hold
up another connection. The upstream deadline remains 15 seconds per request.

```json
{"verb":"board.card.get","args":{"id":20},"request_id":"97fb3490-9147-4da6-acb2-29bac3df210c"}
```

The envelope has exactly these three keys. `request_id` is a canonical lowercase
UUID string, `verb` is one of the names below, and `args` is a JSON object matching
that verb's closed schema. Unknown keys, missing required keys, wrong types, and
unknown verbs are refused before sending an upstream request. Booleans are not
integers. Inputs are never coerced. Query parameters are URL-encoded, not appended
as raw strings. The gateway URL and token come only from the proxy's protected
configuration/store; callers cannot set a path, method, headers, token, or URL.

## Replies, deadlines, and retry safety

A success is emitted only for a 2xx gateway response containing complete valid
JSON. `body` is that JSON value, not a second proxy envelope.

```json
{"request_id":"97fb3490-9147-4da6-acb2-29bac3df210c","ok":true,"status":200,"body":{"id":20}}
```

A failure has only `request_id`, `ok:false`, `error`, `detail`, and an optional
integer `status` when a gateway HTTP status is known. `request_id` is `null` if it
could not be validated or the uid check failed. Upstream bodies, exception text,
and caller arguments never become `detail`.

```json
{"request_id":"97fb3490-9147-4da6-acb2-29bac3df210c","ok":false,"error":"bad_args","detail":"The request does not match the published argument schema."}
```

The upstream deadline is **15 seconds**, shared by preparatory reads and the
eventual operation; clients wait **20 seconds**. There is one attempt only. An
`upstream_ambiguous` write may have applied: re-read the card, obligation, or
message state before deciding whether to retry. Request UUIDs correlate replies;
they are not an idempotency key and do not deduplicate writes.

| Error | Fixed detail |
| --- | --- |
| `verb_not_allowed` | This verb is not available through the proxy. |
| `bad_args` | The request does not match the published argument schema. |
| `peer_not_allowed` | The socket peer is not the configured cell user. |
| `proxy_unavailable` | The local proxy is unavailable. |
| `upstream_unreachable` | The gateway connection failed before a request was sent. |
| `upstream_ambiguous` | The gateway reply was interrupted; re-read state before retrying a write. |
| `upstream_error` | The gateway did not return a complete successful JSON response. |

`upstream_error` includes non-2xx (including redirects), truncated or invalid JSON,
and invalid required preparatory-read fields. A connect failure before any request
is sent is `upstream_unreachable`; timeout/reset during or after sending is
`upstream_ambiguous`. The local client maps a missing or stale socket to
`proxy_unavailable` and never attempts a direct gateway call.

## Argument types

Every field not listed for a verb is forbidden. A `?` below means optional;
omitted fields are omitted upstream unless a default is stated.

- `id`, `card_id`, `reply_to`: positive JSON integers, never booleans.
- `limit`: integer 1–1000. If omitted, gateway defaults apply: inbox 100,
  card thread 200, memory list 50; obligation list is unbounded.
- `name`: regex `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` (full-string match).
  Used for peers, channels, holders, creators, memory type and tag.
- `slug`: regex `[A-Za-z0-9][A-Za-z0-9_.-]{0,199}` (full-string match).
- `kind`: `status`, `question`, `answer`, `unblock`, or `fyi`; outgoing posts
  default to `status`.
- `status` / `expect_status`: `open`, `closed`, `fallback_fired`, or `withdrawn`.
- `stage` / `expect_stage`: `proposed`, `idea`, `spec`, `plan`, `build`, `review`,
  `ready`, `done`, or `parked`. Target `done` is always refused.
- `expect_stage_version`: nonnegative JSON integer, never a boolean.
- `expect_accept_sha256`: exactly 64 hexadecimal characters.
- `since`: timezone-aware ISO-8601 timestamp string, at most 64 characters.
- `content`: non-whitespace string, 1–200000 characters. `why`: 1–2000;
  `evidence`: 1–4000, also non-whitespace strings.
- `enabled_only`, `unread_only`, `overdue`, `grandfathered`: JSON booleans.
- `ids`: array of 1–500 positive integers; every element is checked.
- `outcome`: `pass`, `fail`, `cannot_evaluate`, or `skipped`. Only the gateway
  determines whether the row is an optional step that may be skipped.
- `evidence_json`: JSON object with string keys and non-blank string values;
  each value is at most 16000 characters, and the object serialized with Python's
  `json.dumps(..., sort_keys=True)` (including ASCII escapes) is at most 64000
  characters. Condition-id membership and completeness are enforced by the
  gateway. Close requires at least one of `evidence` and `evidence_json`; both
  may be supplied. `author` and `usage` remain forbidden (ruling 94005).

## Closed routes and gateway evidence

This schema is deliberately narrower than the gateway's request models. Evidence
is pinned to gateway commit
`657e7e9aed0b3d8c14589da8b5192041ed5f9f0f`, not the older bundled CLI gateway.
Every link below points to the real handler/model at that commit. There is no
generic gateway passthrough.

| Verb | Complete args schema | Fixed gateway operation and evidence |
| --- | --- | --- |
| `whoami` | `{}` | `GET /whoami`; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L4661) |
| `board.card.get` | `id` | `GET /board/cards/{id}?actor=<cell>`; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L16641) |
| `board.cards.graph` | `id` | `GET /board/cards/{id}/graph?actor=<cell>`; [read-granted handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L17808). The actor is pinned, not caller-selectable. |
| `board.obligations.list` | `id?`, `card_id?`, `status?`, `holder?:name`, `created_by?:name`, `limit?`, `overdue?`, `grandfathered?` | `GET /board/obligations`, pinned `actor=<cell>`; `id` becomes the gateway's `obligation_id` query key; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L17571). `SELF` for holder/creator is resolved by the gateway. |
| `board.cards.thread` | `id`, `limit?` | `GET /board/cards/{id}/thread?actor=<cell>`; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L16660) |
| `mesh.peers` | `enabled_only?` | `GET /peers`; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L4720) |
| `rights` | `{}` | `GET /peers/<cell>/grants`, own cell only; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L22215) |
| `mesh.inbox` | `limit?`, `unread_only?`, `since?`, `kind?` | `GET /messages?to_node=<cell>`, own inbox only; [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L7381). Does not mark read. |
| `memory.get` | `slug` | `POST /memory` with `{"op":"get","arguments":{"slug":...}}`; [model/handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L12124) |
| `memory.list` | `type?:name`, `tag?:name`, `limit?` | `POST /memory` with fixed `op:"list"`, validated fields under `arguments`; [argument forwarding](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L12091) |
| `board.cards.say` | `id`, `content`, `kind?`, `to_node?:name` | `POST /messages` with pinned `from_node=<cell>` and server-derived card thread; [model/handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L6493) |
| `mesh.send` | `to_node:name`, `content`, `kind?`, `reply_to?` | `POST /messages` with pinned `from_node=<cell>`; [model/handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L6493) |
| `channel.post` | `channel:name`, `content`, `kind?` | `POST /messages` with pinned `from_node=<cell>`, no DM recipient; [exclusive destination check](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L6545) |
| `mesh.mark_read` | `ids` | `POST /messages/read`; [model/handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L7643). The gateway scopes writes to message participants. |
| `board.obligations.take` | `id`, `expect_status` | `POST /board/obligations/{id}/take`; no actor field, token is the actor; [model](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L17868), [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L17939) |
| `board.obligations.decline` | `id`, `expect_status`, `why` | `POST /board/obligations/{id}/decline`; [model](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L17872), [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L18065) |
| `board.obligations.close` | `id`, `expect_status`, `expect_accept_sha256`, `outcome`, `evidence?`, `evidence_json?` (one evidence field required) | `POST /board/obligations/{id}/close`; [model](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L18531), [map validation](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L13963), [optional-only skipped rule](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L20461), [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L20547) |
| `board.cards.move` | `id`, `stage`, `expect_stage`, `expect_stage_version` | `PATCH /board/cards/{id}` with pinned `actor=<cell>`; [model](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L13653), [handler](https://github.com/BrainSurfing-tech/mesh-gateway/blob/657e7e9aed0b3d8c14589da8b5192041ed5f9f0f/server.py#L20667) |

For `board.cards.say`, the proxy first reads the card through the fixed card-read
route and takes `thread_uuid` from that authenticated reply. Caller `thread_id`
is forbidden. Recipient priority is validated explicit `to_node`, then the card's
validated assignee, then the proxy's own cell; self-addressing satisfies the
gateway's exactly-one-destination rule and keeps an unassigned card post on its
thread without fetching the unlisted project directory. Missing/invalid bound
thread data refuses the post. A caller cannot attach a raw thread identifier.

For `board.cards.move`, all guards are required before any gateway call. The proxy
then reads the card and refuses with `verb_not_allowed` unless the deployed
gateway returns a nonnegative integer `stage_version`. This runtime check stays
in place even though the pinned source supports it: older deployed gateways may
silently ignore unknown PATCH fields. A target of `done` is refused regardless
of the source stage/version. The gateway, not the proxy, atomically evaluates
the expected-state guards and its own permission checks.

No verb accepts `from_node`, `actor`, `cc`, or a field called `obligation_id`.
Obligation identifiers use `id`. Memory callers cannot supply nested
`arguments`, tool names, operations, or arbitrary metadata. `memory.links`,
`release`, grants, project/card creation, peer registration, token lifecycle,
committee-chair verbs, and every other unlisted operation are unavailable.
The close schema accepts bounded text and/or condition evidence and passes
`skipped` through to the gateway. It deliberately does not expose the gateway's
`usage` or `author` fields: the proxy token remains the only acting identity.

## Operations boundary

This change is a build contract, not a cutover procedure. Root-owned isolated
installation, separate uid, protected token/config, socket activation, no-core
hardening, and the separate pull-only monitor must be reviewed before piloting.
No live gateway, token rotation, revoke/register, unit changes, or service restart
is needed to run the fixture tests. Gateway responses remain subject to its
current authorization policy; the local proxy does not create read scopes that
the gateway itself does not enforce.
