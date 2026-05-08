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

**v0.3.0 — Phase 2 one-shot + Phase 2.5 import + Phase 5 REPL.** Three verbs ship:

1. `swarph "prompt"` — Phase 2 one-shot mode (any of five providers)
2. `swarph chat` — Phase 5 interactive REPL with multi-turn history + slash commands
3. `swarph import <path>` — Phase 2.5 session import (Claude JSONL → swarph-native, with `--report-only` for honest pre-commit inspection)

Subsequent phases extend the CLI surface (`--ask <peer>`, onboard/ratify, daemon, additional source formats).

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
| **5** (v0.3.0 — this release) | **`swarph chat` interactive REPL** — multi-turn against any of five adapters + slash commands (`/help`, `/clear`, `/system`, `/provider`, `/model`, `/history`, `/cost`, `/quit`) |
| **3** | `--ask <peer>` mesh-aware one-shot via MeshClient |
| **5.5** | `swarph onboard <peer-name>` + `swarph ratify <peer-name>` (PLAN.md §15) |
| **5.6** | `swarph daemon` foreground drain loop + REPL drain coroutine + `/inbox`, `/reply` (PLAN.md §16) |
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
