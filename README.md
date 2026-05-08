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

**v0.1.0 — Phase 2 one-shot mode.** The `swarph "prompt"` binary works end-to-end against `--provider gemini` per PLAN.md §13 falsifiability gate. Subsequent phases extend the CLI surface (REPL, `--ask <peer>`, onboard/ratify, daemon, import).

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
| **0** (this) | Scaffold — entry-point + status banner |
| **2** | One-shot mode: `swarph "hello" --provider gemini` |
| **3** | `--ask <peer>` mesh-aware one-shot via MeshClient |
| **5** | Interactive REPL — `/inbox`, `/reply`, `/dm`, `/watch` |
| **5.5** | `swarph onboard <peer-name>` + `swarph ratify <peer-name>` (PLAN.md §15) |
| **5.7** | `swarph daemon` foreground drain loop + `swarph chat` REPL with drain coroutine (PLAN.md §16) |
| **6** | PyPI publish |

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
