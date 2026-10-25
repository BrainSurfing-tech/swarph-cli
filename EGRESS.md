# Egress inventory — what leaves the box from swarph-cli

One row per outbound network path in `src/swarph_cli`. Enforced by
`tests/test_1214_egress.py`: every AST-detected network call must map to a
row below keyed by (file, client), or the build fails naming file:line.
Add a row when you add a network call — that is the point of the inventory.

Runtime check (card #1219, widened by card #1225): `make egress-probe`
runs every file under `tests/` except `tests/test_1219_egress_probe.py`
inside `bwrap --unshare-net`. An unlisted IP-literal TCP connect (IPv4
or IPv6) fails it. A hostname lookup that only reaches the stub resolver
is not judged. Pytest exit 2 through 5 (nothing collected, usage error,
internal error, interrupted) is `isolation-unproven`, not a pass. A host
where isolation cannot be proven — the control connect must fail with
ENETUNREACH — exits 2 with `isolation-unproven:`. GNU make folds a recipe
failure into exit 2, so the recipe prints `egress-probe-status N` with
the probe's own code. Measured on the lab host at this change: 152s.

| file | client | destination | what it sends | opt-in or default |
| --- | --- | --- | --- | --- |
| src/swarph_cli/commands/_gateway_client.py | urllib.request.urlopen | mesh gateway HTTP API (configured base URL; loopback default) | board/mesh API requests as JSON with the caller's bearer token | default (all board/mesh commands) |
| src/swarph_cli/commands/board.py | urllib.request.urlopen | mesh gateway HTTP API (via _gateway_client) | board card/obligation requests as JSON with bearer token | default |
| src/swarph_cli/commands/mesh.py | urllib.request.urlopen | mesh gateway HTTP API (via _gateway_client) | mesh DM/peer requests as JSON with bearer token | default |
| src/swarph_cli/commands/roster.py | urllib.request.urlopen | mesh gateway HTTP API | roster/peer queries with bearer token | default |
| src/swarph_cli/commands/ratify.py | urllib.request.urlopen | mesh gateway HTTP API | ratification votes as JSON with bearer token | opt-in (command run) |
| src/swarph_cli/commands/onboard.py | urllib.request.urlopen | mesh gateway HTTP API and peer /peers endpoints | onboarding proposals and peer probes with bearer token | opt-in (command run) |
| src/swarph_cli/commands/group.py | urllib.request.urlopen | mesh gateway HTTP API (via _gateway_client) | group requests as JSON with bearer token | default |
| src/swarph_cli/commands/daemon.py | urllib.request.urlopen | mesh gateway HTTP API | daemon status/registration requests with bearer token | opt-in (daemon run) |
| src/swarph_cli/commands/codegraph_hook.py | urllib.request.urlopen | mesh gateway HTTP API | codegraph hook event posts as JSON with bearer token | opt-in (hook installed) |
| src/swarph_cli/commands/cell_probe.py | urllib.request.urlopen | mesh gateway HTTP API | cell probe queries with bearer token | opt-in (command run) |
| src/swarph_cli/commands/brain_ask.py | urllib.request.urlopen | fixed tailnet URL (brain service) | question text as JSON | opt-in (command run) |
| src/swarph_cli/commands/mcp_server.py | urllib.request.urlopen | mesh gateway HTTP API | MCP tool-call requests as JSON with bearer token | opt-in (server run) |
| src/swarph_cli/commands/watchdog.py | urllib.request.urlopen | mesh gateway HTTP API | watchdog status posts as JSON with bearer token | opt-in (daemon run) |
| src/swarph_cli/scripts/wake_watchdog.py | urllib.request.urlopen | mesh gateway HTTP API (MESH_GATEWAY_URL) | GET /peers with the sender's bearer token | opt-in (watchdog pass when MESH_GATEWAY_URL is set) |
| src/swarph_cli/operator/api.py | urllib.request.urlopen | operator API base URL (configured) | operator API requests as JSON with bearer token | opt-in (operator surface) |
| src/swarph_cli/stall_alert.py | urllib.request.urlopen | mesh gateway HTTP API | stall alert posts as JSON with bearer token | opt-in (alert enabled) |
| src/swarph_cli/duplicate_session.py | urllib.request.urlopen | mesh gateway HTTP API | duplicate-session alarm rows on card #513 as JSON with bearer token | default (swarph monitor running) |
| src/swarph_cli/multiplexer.py | urllib.request.urlopen | https://github.com/psmux/psmux/releases (pinned v3.3.6) | nothing (download only, GET) | opt-in (Windows multiplexer install) |
| src/swarph_cli/gateway/services_control.py | urllib.request.urlopen | http://<health-host>:<port>/health (local services) | nothing (GET health probe) | default (gateway service control) |
| src/swarph_cli/gateway/server.py | urllib.request.urlopen | fixed tailnet URL (upstream gateway) | proxied gateway requests with bearer token | opt-in (gateway run) |
| src/swarph_cli/dreaming/slm.py | urllib.request.urlopen | configured SLM base URL (local model endpoint) | prompt text as JSON (local inference) | opt-in (dreaming enabled) |
| src/swarph_cli/dreaming/probes.py | urllib.request.urlopen | configured probe hosts (private hosts) | MCP probe requests (JSON-RPC ping) | opt-in (dreaming probes) |
| src/swarph_cli/commands/version.py | urllib.request.urlopen | https://pypi.org/pypi/{name}/json | nothing (GET package metadata; name only) | opt-in (--check / version command) |
| src/swarph_cli/bench/refresh_prices.py | urllib.request.urlopen | https://raw.githubusercontent.com/BerriAI/litellm/main (price list) | nothing (GET price table) | opt-in (bench refresh run) |
| src/swarph_cli/bench/backends.py | urllib.request.urlopen | configured bench backend base URLs (127.0.0.1 ports) | bench prompts as JSON to local backends | opt-in (bench run) |
