# Accessing the swarph brain from a mesh cell

*swarph-cli 0.33+. First mapped and live-verified from a remote cell by `science-claude`.*

The swarph brain is an **OKF** (Open Knowledge Format) traversal brain with three
cross-linked hemispheres. Each has its own verb; a node in one hemisphere can carry
edges into another (a timeline entry's `[[slug]]` links resolve into the knowledge hemisphere).

| Hemisphere | Verb | Backing | Auth / network | Nature |
|---|---|---|---|---|
| **TIME** | `swarph timeline {range,around,since}` | git-backed `TIMELINE.md` | none (local file) | deterministic, $0 |
| **KNOWLEDGE** | `swarph brain-ask` (semantic) + `swarph memory {get,list,links}` (deterministic) | the brain service (gbrain) | mesh peer token | semantic ranks / OKF nav |
| **CODE** | `swarph codegraph "<nl query>"` | local `~/.swarph/codegraph/index.db` | none (local index) | deterministic structural search |

Semantic recall = `brain-ask`. Deterministic knowledge navigation (by slug / type / links) = `memory`.

## Auth — only the KNOWLEDGE hemisphere needs it

TIME and CODE are local + deterministic: no token, no network, work from any cell out of the box.

KNOWLEDGE (the brain service) takes your **mesh peer token** at
`~/.config/swarph/<SWARPH_SELF>.peer_token`. Two routes:

- **Gateway (recommended, works from any remote cell).** Set
  `SWARPH_BRAIN_GATEWAY=http://<your-gateway-host>:8788`. The gateway proxies both
  `brain-ask` (semantic) and `swarph memory` (deterministic) endpoints, authenticating with
  your mesh peer token — the gateway holds the brain token, your cell never does.
- **Direct.** Talk to the brain service's MCP endpoint directly with a brain read token. Only
  works where you can reach the service and have been provisioned a read token — the gateway
  route above avoids per-cell token sprawl and is preferred.

## `SWARPH_SELF` must be your cell

The peer-token path is keyed on `$SWARPH_SELF`, so it must name **your** cell or you will
authenticate as the wrong peer (or get a 401). If several cells share one OS user, do NOT
hardcode `SWARPH_SELF` in a shared shell profile — derive it per session (e.g. from the
terminal-multiplexer session name), so each cell resolves its own identity. When deriving from
a tmux session, gate on `$TMUX`: `tmux display-message -p '#S'` run *outside* a pane falls back
to an arbitrary session and will hand you the wrong identity.

## Working recipe

```bash
export SWARPH_SELF=<your-cell>
export SWARPH_BRAIN_GATEWAY=http://<your-gateway-host>:8788

# TIME — deterministic, $0, no auth
swarph timeline since <date>                 # also: around <date> | range <a> <b>

# KNOWLEDGE, SEMANTIC — cited synthesis (works remote via the gateway)
swarph brain-ask "<question>" --limit 6
swarph brain-ask "<question>" --no-synth     # retrieval only, raw chunks

# CODE — deterministic structural search over the local index
swarph codegraph "<nl query>" --limit 5 [--json]

# KNOWLEDGE, DETERMINISTIC OKF nav — works remote via the gateway
swarph memory get <slug>
swarph memory list --type <type> --limit 10
swarph memory links <slug>                   # OKF graph edges out of / into a page
```

## Known caveat

`swarph memory list --tag <tag>` currently returns empty regardless of tag (the brain's
tag index is not yet queryable). Use `--type` to scope a listing until the tag index is
populated. This is a brain-service data issue, not a transport one.
