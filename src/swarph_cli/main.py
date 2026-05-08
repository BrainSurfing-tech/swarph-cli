"""``swarph`` entry-point — Phase 2 one-shot mode.

v0.0.1 was the scaffold (banner-only). v0.1.0 ships the falsifiability
gate from PLAN.md §13 Phase 2:

    swarph "explain Hawkes process briefly" --provider gemini --model flash

Subsequent phases extend the CLI:
- Phase 3:    --ask <peer> mesh-aware one-shot via MeshClient
- Phase 5:    interactive REPL (``swarph chat``)
- Phase 5.5:  ``swarph onboard`` + ``swarph ratify``
- Phase 5.7:  ``swarph daemon`` foreground drain
- Phase 2.5:  ``swarph import --report-only`` per PLAN.md §17.6 reorder

For now the entry-point handles ONE shape: positional prompt argument
+ provider/model flags + JSON-mode toggle. argparse subparsers will
land in Phase 3 when more verbs need their own surface.
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
swarph v{version}

Usage:
  swarph "your prompt here" [--provider gemini] [--model gemini-2.5-flash]

Examples:
  swarph "explain Hawkes process briefly"
  swarph "list 5 tickers" --json
  swarph "summarise" --provider gemini --model gemini-2.5-pro

Status: Phase 2 one-shot mode. REPL (Phase 5), --ask <peer>
(Phase 3), onboard/ratify (Phase 5.5), daemon (Phase 5.7) and
import (Phase 2.5) ship in subsequent releases.

Spec: https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md
"""


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


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.prompt is None:
        return _print_banner()

    return asyncio.run(_run_one_shot(args))


if __name__ == "__main__":
    raise SystemExit(main())
