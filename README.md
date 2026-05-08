# swarph-cli

The `swarph` binary — multi-LLM CLI with mesh-gateway integration. Thin client over the [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) substrate.

```bash
pip install swarph-cli
swarph --version
```

This is one of three repos in the v0.3.x architecture:

| Repo | Role |
|---|---|
| [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) | Substrate Python package — Protocol + adapters + SwarphCall + MeshClient. Pure library, no CLI |
| [`swarph-cli`](https://github.com/darw007d/swarph-cli) | This repo — the `swarph` binary |
| [`swarph-meshlm`](https://github.com/darw007d/swarph-meshlm) | Simon Willison `llm` plugin |

## Status

**v0.5.0 — Phase 2 one-shot + Phase 2.5 import + Phase 5 REPL + Phase 5.5 onboard/ratify + Phase 5.6 daemon.** Six verbs ship:

1. `swarph "prompt"` — Phase 2 one-shot mode (any of five providers)
2. `swarph chat` — Phase 5 interactive REPL with multi-turn history + slash commands
3. `swarph import <path>` — Phase 2.5 session import (Claude JSONL → swarph-native, with `--report-only` for honest pre-commit inspection)
4. `swarph onboard <peer-name>` — Phase 5.5 mechanics-phase onboarding (PLAN.md §15.4)
5. `swarph ratify <peer-name>` — Phase 5.5 witness ratification (PLAN.md §15.4a)
6. `swarph daemon` — **NEW** Phase 5.6 foreground inbox drain loop (PLAN.md §16); structurally retires the orphaned-tail-F class

Subsequent phases extend the CLI surface (`--ask <peer>`, REPL drain coroutine + `/inbox` + `/reply` slash commands in 5.6b).

### `swarph daemon` (Phase 5.6)

Replaces the 4-layer `tail -F | grep | Monitor | systemd | cron poll` stack with one foreground process. Liveness check collapses to:

```bash
ps aux | grep '[s]warph daemon'   # zero output = monitoring is down
```

```bash
$ swarph daemon --state-dir ~/swarph_state/lab-ovh --self lab-ovh
[swarph-daemon] starting: self=lab-ovh gateway=http://localhost:8788 poll=30s ...
[2026-05-08T21:00:30Z] id=728 from=droplet kind=answer → 'Drop review on Phase 5.5 PRs A+B...'
[2026-05-08T21:01:10Z] id=729 from=droplet kind=fyi → 'Both Phase 5.5 PRs merged...'
^C
[swarph-daemon] signal 2 received — draining + flushing cursor
[swarph-daemon] shutdown: iterations=12 dms_seen=2 cursor.last_msg_id=729
```

Loud-on-down (PLAN §16.5): never silently exits. Cursor writes are atomic (write-and-rename — corrupted mid-flush leaves the previous cursor intact). Backoff: 60s after 5 consecutive empty polls; 300s after 5 min of consecutive 5xx. SIGINT/SIGTERM trigger clean drain + flush.

`--auto-act` flag is documented for v0.5.1+ when handler registration via `@swarph.on_dm(...)` lands; v0.5.0 ships surface-only mode (DMs printed + JSONL-logged to `inbox.log`, no automatic replies).

### `swarph onboard` + `swarph ratify` (Phase 5.5)

Per PLAN.md §15, onboarding splits into a **mechanics phase** (`swarph onboard`) that automates the boring parts (registry POST, scaffolding, token resolution) and a **manual contract phase** (the new peer composes the handshake DM in their own words). A witness peer judges the handshake and runs `swarph ratify <peer>` to flip `ratified=true`, gating `task_claim` server-side.

```bash
# New peer self-onboards
$ swarph onboard razorpeter
[1/6] validate_node_name('razorpeter')          ok
[2/6] prepare peer-registry row                 ok
[3/6] resolve MESH_GATEWAY_TOKEN                ok
[4/6] POST .../peers/register                   ok (registered_unratified=true)
[5/6] verify_subscription_setup()               ok
[6/6] scaffold ~/swarph_state/razorpeter/       ok

[manual] handshake template at /tmp/razorpeter-handshake.md
  Edit each section in your own words, then send to your witness peer.

# After peer composes + sends handshake, witness ratifies
$ SWARPH_WITNESS=lab-ovh swarph ratify razorpeter \
    --reason "handshake covers all four invariants in own words"
[1/6] validate_node_name('razorpeter')          ok
[2/6] verify witness 'lab-ovh' is ratified      ok
[3/6] verify 'razorpeter' is registered_unratified  ok
[4/6] PATCH .../peers/razorpeter                ok
[5/6] verify peer_ratifications audit row       ok (id=N reason='...')
[6/6] invalidate local TTL cache                ok
```

Server-side gating (mesh-gateway PR A): unratified peers can read inbox + send DMs (so the handshake itself works) but `task_claim` returns 403. Witness must itself be ratified — no self-ratification, no unratified-witnesses-ratifying-others. Audit log (`peer_ratifications`) is append-only.

### `swarph chat`

Interactive REPL against any of the five swarph-mesh adapters (`gemini` / `deepseek` / `claude` / `openai` / `grok`). Multi-turn conversation history accumulates in-memory; cumulative session cost + token totals tracked.

```bash
$ swarph chat --provider claude
swarph chat — Phase 5 REPL
provider=claude model=(adapter default) caller=cli.repl.ubuntu

Type a message and press Enter to send. Slash commands:
  /help  /clear  /system  /provider  /model  /history  /cost  /quit
Ctrl-D to exit.

> hello
Hi! How can I help...
# 8+12t  $0  0.34s

> /provider gemini
[switched to provider=gemini; model reset to adapter default; history cleared]

> /cost
[turns=1  in=8  out=12  cost=$0]

> /quit
[swarph-chat] bye.
```

**Slash commands:**
- `/help` — print available commands
- `/quit`, `/exit` (or Ctrl-D) — exit
- `/clear`, `/reset` — clear history (keeps system prompt)
- `/system [prompt]` — set or clear system prompt
- `/provider <name>` — switch provider (resets history)
- `/model <name>` — switch model
- `/history` — print running message list
- `/cost` — cumulative session cost + tokens

**Out of scope until Phase 5.6** (`swarph daemon`): inbox drain coroutine, `/inbox` and `/reply` slash commands. Streaming output ships alongside the cross-adapter `stream()` work in v0.5+ of swarph-mesh.

### `swarph import`

Per PLAN.md §17, session import is the **knowledge half of onboarding** — gives a memory-carrying peer (or human migrating CLIs) the substantive context they're bringing into the swarph, paired with §15's contract half (handshake DM acknowledging the four invariants).

```bash
# Inspect what would be imported (lossy → honest framing)
$ swarph import ~/.claude/projects/.../X.jsonl --report-only

# Commit — writes ~/.swarph/sessions/<session-id>.jsonl
$ swarph import ~/.claude/projects/.../X.jsonl

# Refuse-with-error if target exists (protects continuation turns)
$ swarph import same-source.jsonl
swarph import: target /home/.../X.jsonl already exists (...)
To proceed:
  --force                  overwrite (destroys continuation turns)
  --target-session NAME    write to a different file
```

**What ports cleanly:** plain user/assistant/system text, role tags, conversation order.

**What's lossy** (counted in report, kept as visible text where possible):
- `thinking` blocks (Anthropic-specific reasoning trace)
- `tool_use` blocks (call shape doesn't port across providers)
- `tool_result` blocks (companion drop with `tool_use`)

**What's dropped:** attachments (would need re-upload), provider-side KV cache, conversation IDs, `cache_control` annotations.

Honest framing per PLAN.md §17.3: **teleport is "import + continue", not "freeze and resume"** — the first turn after import on a new provider pays cold-cache cost. Phase 5+ adds `--continue` for live REPL integration.

```bash
$ swarph "say pong" --provider gemini
Pong!
# 3+26t  $0.0000  0.73s  caller=cli.oneshot.ubuntu  provider=gemini
```

### `--json` mode semantics

`--json` is a **harness trigger**, not a strict-validation gate. When set, swarph routes the response through the swarph-mesh JSON harness:

- A permissive `{"type": "object"}` schema is synthesised when `--schema` is absent (Phase 5+ adds Pydantic validation).
- The harness retries once with `[USER]`-turn feedback on parse failure.
- **Malformed-JSON exits with code 1** + raw text on stdout for caller recovery. Useful for shell scripts:
  ```bash
  if swarph "give me a trade" --json; then
    # parsed dict was on stdout
    ...
  fi
  ```
- Pretty-printed parsed dict on stdout when parse succeeds; `error_class=malformed_json` shows up in the stderr attribution footer when it doesn't.

## Spec

→ [hedge-fund-mcp / research/swarph_cli/PLAN.md](https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md)

## Phase rollout

| Phase | What lands |
|---|---|
| **0** | Scaffold — entry-point + status banner |
| **2** (v0.1.0) | One-shot mode: `swarph "hello" --provider gemini` |
| **2.5** (v0.2.0) | `swarph import` — Claude JSONL → swarph-native session format |
| **5** (v0.3.0) | `swarph chat` interactive REPL — multi-turn against any of five adapters + slash commands |
| **5.5** (v0.4.0) | `swarph onboard` + `swarph ratify` — six mechanics steps + handshake template + witness flip (PLAN.md §15) |
| **5.6** (v0.5.0 — this release) | **`swarph daemon`** — foreground inbox drain loop with atomic cursor writes; retires the orphaned-tail-F class (PLAN.md §16) |
| **5.6b** | REPL drain coroutine + `/inbox`/`/reply` slash commands + `@swarph.on_dm()` handler registration (mesh + cli) |
| **3** | `--ask <peer>` mesh-aware one-shot via MeshClient |
| **6** | (already done) PyPI publish |

## Why split CLI from substrate

`swarph-mesh` (the library) is imported by `omega-boss`, Council judges, `lab-orchestrator`, and any future swarph peer that wants to write programs against the Protocol. Those callers don't need the CLI surface or the console-script entry point. Keeping the CLI in a separate repo means library users `pip install swarph-mesh` without pulling argparse + REPL plumbing they'll never run.

## Install (dev)

```bash
git clone https://github.com/darw007d/swarph-cli
cd swarph-cli
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
swarph --version
```

## License

MIT. Pierre Samson + Claude Opus, 2026.
