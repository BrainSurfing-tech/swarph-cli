"""``swarph`` entry-point — Phase 2 one-shot + Phase 2.5 import.

v0.0.1 was the scaffold (banner-only). v0.1.0 shipped the Phase 2
one-shot falsifiability gate. v0.2.0 adds the Phase 2.5 ``import``
verb per PLAN.md §17.6 forward-reorder.

Verb dispatch shape: if the first arg matches a known verb keyword
(currently ``import``), the rest of argv is passed to that verb's
handler. Otherwise argv is treated as the one-shot path (positional
prompt + flags). Phase 3+ adds ``--ask``, ``list-peers``,
``list-adapters``; Phase 5 adds ``chat`` REPL; Phase 5.5 adds
``onboard``/``ratify``; Phase 5.7 adds ``daemon``.

Disambiguation note: a literal one-shot prompt that starts with the
word "import" (e.g. ``swarph "import this report"``) collides with
the verb. Workaround: rephrase (``swarph "please import this
report"``) — the collision is rare and the verb takes precedence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from swarph_cli import __version__
from swarph_cli.caller import default_caller


_BANNER = """\
      ╭───╮
      │ ◉ │
   ╭──┴───┴──╮
   │  swarph │  v{version}
   ╰──┬───┬──╯       spawn │ chat │ daemon
      │ ◉ │
      ╰───╯

Usage:
  swarph "your prompt here" [--provider gemini] [--model gemini-2.5-flash]
  swarph chat [--provider deepseek] [--model deepseek-v4-flash] [--system PROMPT]
  swarph spawn <role-or-path> [--onboarding PATH] [--dry-run]
  swarph import <path-to-source-session> [--report-only] [--target-session NAME]
  swarph onboard <peer-name> [--gateway URL]
  swarph ratify <peer-name> [--reason "<text>"] [--witness-name <self>]
  swarph daemon [--state-dir DIR] [--self NAME] [--poll-seconds N]
  swarph mesh send <peer> --kind question --content "..."

Examples:
  swarph "explain Hawkes process briefly"
  swarph "list 5 tickers" --json
  swarph chat --provider claude
  swarph spawn lab                                   # ~/.config/swarph/cells/lab.yaml
  swarph spawn ./cell.yaml --dry-run
  swarph import ~/.claude/projects/.../X.jsonl --report-only
  swarph onboard new-peer-name
  swarph ratify new-peer-name --reason "handshake covers four invariants"

Status: Phase 2 one-shot + Phase 2.5 import + Phase 5 REPL +
Phase 5.5 onboard/ratify + Phase 5.6 daemon + Phase 7 spawn ready.
Layer-2 mesh bridge ships as `swarph mesh`; REPL /inbox /reply
slash commands (Phase 5.6b) ship in subsequent releases.

Spec: https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md
"""

# Known verb keywords that route to their own handler. Order matters
# only for disambiguation against one-shot prompts (rare).
_VERB_HANDLERS: dict[str, str] = {
    # verb keyword: dotted-path to handler function (lazy-imported)
    "init": "swarph_cli.commands.init.run_init",
    "import": "swarph_cli.commands.import_session.run_import",
    "chat": "swarph_cli.commands.chat.run_chat",
    "onboard": "swarph_cli.commands.onboard.run_onboard",
    "ratify": "swarph_cli.commands.ratify.run_ratify",
    "daemon": "swarph_cli.commands.daemon.run_daemon",
    "spawn": "swarph_cli.commands.spawn.run_spawn",
    "install-hook": "swarph_cli.commands.install_hook.run_install_hook",
    "hook-output": "swarph_cli.commands.hook_output.run_hook_output",
    "watchdog": "swarph_cli.commands.watchdog.run_watchdog",
    "hooks": "swarph_cli.commands.hooks.run_hooks",
    "memory-sync": "swarph_cli.commands.memory_sync.run_memory_sync",
    "mesh": "swarph_cli.commands.mesh.run_mesh",
    # Future: "list-peers", "list-adapters", etc.
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph",
        description=(
            "swarph — multi-LLM CLI with mesh-gateway integration. "
            "Phase 2 one-shot mode."
        ),
    )
    p.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help='Prompt to send (one-shot mode). Omit to print a usage banner.',
    )
    p.add_argument(
        "--provider",
        default="gemini",
        help='LLM provider. Phase 1 ships "gemini" only; Phase 4+ adds '
        "deepseek/claude/openai/grok.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Provider-specific model id. Defaults to the adapter's default_model.",
    )
    p.add_argument(
        "--caller",
        default=None,
        help='Caller-convention slug (dotted lowercase). Defaults to '
        '"cli.oneshot.<user>" for the current OS user.',
    )
    p.add_argument(
        "--system",
        default=None,
        help="System prompt prepended to the conversation.",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default 0.7).",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Max output tokens (provider default if omitted).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Parse response as JSON (TRIGGER for the swarph-mesh JSON "
        "harness — not strict-validation; a permissive {'type': 'object'} "
        "schema is synthesised when --schema is absent). Malformed-JSON "
        "responses cause exit code 1 with raw text on stdout for caller "
        "recovery — useful for shell scripts gating on "
        "`if swarph 'x' --json; then ...`. Full Pydantic validation lands "
        "in Phase 5+.",
    )
    p.add_argument(
        "--schema",
        default=None,
        help="Path to a JSON Schema file. Implies --json. v0.1.0 uses the "
        "schema only as the harness trigger; full Pydantic validation lands "
        "in Phase 5+.",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the per-call attribution footer on stderr.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"swarph-cli {__version__}",
    )
    return p


def _print_banner() -> int:
    print(_BANNER.format(version=__version__), file=sys.stderr)
    return 0


def _load_schema(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"swarph: --schema file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"swarph: --schema file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


async def _run_one_shot(args: argparse.Namespace) -> int:
    # Local import so unit tests of the CLI shape don't drag in the
    # full SwarphCall wiring + Gemini SDK on import.
    from swarph_mesh import ChatMessage, SwarphCall

    caller = args.caller or default_caller()
    json_schema = _load_schema(args.schema) or ({"type": "object"} if args.json else None)

    try:
        # SwarphCall construction enforces caller convention via
        # swarph_shared.validate_caller — raises ValueError on
        # invalid slugs. Keep inside try/except so a bad --caller
        # argument exits 1 with a friendly error rather than dumps
        # a traceback.
        sc = SwarphCall(
            provider=args.provider,
            caller=caller,
            model=args.model,
        )
        resp = await sc.chat(
            messages=[ChatMessage(role="user", content=args.prompt)],
            system_prompt=args.system,
            json_schema=json_schema,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    except Exception as exc:
        print(f"swarph: call failed: {exc}", file=sys.stderr)
        return 1

    # JSON mode prints parsed dict (pretty) when available; falls back
    # to raw text + error_class footer when the harness couldn't parse.
    if json_schema is not None and resp.parsed is not None:
        print(json.dumps(resp.parsed, indent=2, sort_keys=True))
    else:
        print(resp.text)

    if not args.quiet:
        # ``$0`` displays the subscription-path / free-tier case
        # (adapter returns exactly 0.0); ``$0.0000`` displays a
        # tiny-but-real API cost. Future adapters that introduce
        # float drift around zero (e.g., 1e-12 due to multiplier
        # rounding) would flip the display spuriously to
        # ``$0.0000`` — audit + tighten the threshold if that bites
        # (drop PR #1 review observation #2, DM #681).
        cost_str = f"${resp.cost_usd:.4f}" if resp.cost_usd > 0 else "$0"
        attribution = (
            f"# {resp.input_tokens}+{resp.output_tokens}t  "
            f"{cost_str}  {resp.duration_s:.2f}s  caller={caller}  "
            f"provider={args.provider}"
        )
        if resp.cached:
            attribution += "  (cached)"
        if resp.error_class:
            attribution += f"  error_class={resp.error_class}"
        print(attribution, file=sys.stderr)

    return 0 if resp.error_class is None else 1


def _dispatch_verb(verb: str, verb_argv: list[str]) -> int:
    """Lazy-import the verb's handler and run it.

    Lazy import keeps the one-shot path's import surface minimal —
    callers who never use ``swarph import`` don't pay the parser
    package's import cost.
    """
    handler_path = _VERB_HANDLERS[verb]
    module_path, func_name = handler_path.rsplit(".", 1)
    import importlib

    mod = importlib.import_module(module_path)
    handler = getattr(mod, func_name)
    return handler(verb_argv)


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Verb dispatch — first positional arg may be a known verb keyword.
    # If so, route the rest to that verb's handler. Otherwise fall
    # through to the one-shot path.
    if argv and argv[0] in _VERB_HANDLERS:
        return _dispatch_verb(argv[0], argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.prompt is None:
        return _print_banner()

    return asyncio.run(_run_one_shot(args))


if __name__ == "__main__":
    raise SystemExit(main())
