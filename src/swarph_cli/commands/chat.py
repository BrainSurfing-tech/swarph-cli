"""``swarph chat`` — interactive REPL.

Phase 5 per PLAN.md §13. Multi-turn conversation against any of the
five swarph-mesh adapters (gemini / deepseek / claude / openai / grok).
Stdlib-only — uses ``readline`` for line editing + in-process history,
no third-party REPL framework.

Slash commands (typed as the entire input):

  /help                  — print this list
  /quit | /exit          — exit (Ctrl-D also works)
  /clear | /reset        — clear conversation history (keeps system prompt)
  /system [prompt]       — show or set the system prompt; ``/system`` alone clears it
  /provider <name>       — switch provider (resets the conversation)
  /model <name>          — switch model
  /history               — print the running message list
  /cost                  — print cumulative session cost + token totals

Out of scope per PLAN.md §13 (lands in 5.6):
  /inbox /reply          — require the inbox-drain coroutine
  background drain       — Phase 5.6 ``swarph daemon``

Streaming output is not wired here — every swarph-mesh adapter raises
``NotImplementedError`` on ``stream()`` as of v0.5.0; the REPL awaits
the full response and prints in one block. Token-by-token streaming
lands alongside the cross-adapter ``stream()`` work in v0.5+.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from swarph_cli.caller import default_caller


_BANNER = """\
swarph chat — Phase 5 REPL
provider={provider} model={model} caller={caller}{system}

Type a message and press Enter to send. Slash commands:
  /help  /clear  /system  /provider  /model  /history  /cost  /quit
Ctrl-D to exit.
"""

_HELP = """\
Slash commands:
  /help                 — show this list
  /quit, /exit          — exit (Ctrl-D works too)
  /clear, /reset        — clear conversation history (keeps system prompt)
  /system [prompt]      — show or set system prompt; bare /system clears it
  /provider <name>      — switch provider (clears history; rebinds adapter)
  /model <name>         — switch model
  /history              — print the running message list
  /cost                 — print cumulative cost + token totals
"""


@dataclass
class ReplState:
    """In-memory REPL state. Lives for the duration of a single
    ``swarph chat`` process."""

    provider: str
    model: Optional[str]
    caller: str
    system_prompt: Optional[str]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    messages: list = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    turn_count: int = 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph chat",
        description=(
            "Interactive REPL against any swarph-mesh adapter. "
            "Phase 5 per PLAN.md §13."
        ),
    )
    p.add_argument(
        "--provider",
        default="gemini",
        help='LLM provider (gemini / deepseek / claude / openai / grok).',
    )
    p.add_argument(
        "--model",
        default=None,
        help="Provider-specific model id. Defaults to the adapter's default_model.",
    )
    p.add_argument(
        "--caller",
        default=None,
        help='Caller-convention slug. Defaults to "cli.repl.<user>".',
    )
    p.add_argument(
        "--system",
        default=None,
        help="System prompt prepended to every turn.",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.7,
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
    )
    return p


def _default_repl_caller() -> str:
    """``cli.repl.<user>`` — distinct from ``cli.oneshot.<user>`` so
    attribution rows distinguish REPL turns from one-shot calls.

    Falls back to the caller-convention default if env-derived user
    extraction fails."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return default_caller()
    user_slug = "".join(c if c.isalnum() else "_" for c in user.lower())
    return f"cli.repl.{user_slug}"


def _print(msg: str = "", *, file=None) -> None:
    """Centralized print helper — tests monkeypatch this so they can
    capture REPL output without poking sys.stdout."""
    print(msg, file=file or sys.stdout, flush=True)


def _read_line(prompt: str) -> str:
    """Single-line input. Tests monkeypatch this to inject scripted
    input. Production uses stdlib ``input()`` — readline is auto-loaded
    by import-time on POSIX, giving line editing + history for free."""
    try:
        import readline  # noqa: F401 — side-effect-only on POSIX
    except ImportError:
        pass  # Windows / minimal builds; raw input still works
    return input(prompt)


def _format_attribution(
    *, input_tokens: int, output_tokens: int, cost_usd: float, duration_s: float, cached: bool
) -> str:
    cost_str = f"${cost_usd:.4f}" if cost_usd > 0 else "$0"
    line = (
        f"# {input_tokens}+{output_tokens}t  "
        f"{cost_str}  {duration_s:.2f}s"
    )
    if cached:
        line += "  (cached)"
    return line


def _handle_slash(state: ReplState, line: str) -> str:
    """Apply a slash command to ``state``. Return one of:

    - ``"continue"`` — keep the REPL running
    - ``"quit"``     — exit the REPL with code 0
    - ``"unknown"``  — unrecognized command (caller prints + continues)
    """
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        return "quit"

    if cmd == "/help":
        _print(_HELP)
        return "continue"

    if cmd in ("/clear", "/reset"):
        state.messages = []
        _print("[cleared conversation history]")
        return "continue"

    if cmd == "/system":
        if not arg:
            if state.system_prompt:
                _print(f"[system prompt cleared (was: {state.system_prompt!r})]")
                state.system_prompt = None
            else:
                _print("[no system prompt set]")
        else:
            state.system_prompt = arg
            _print(f"[system prompt set: {arg!r}]")
        return "continue"

    if cmd == "/provider":
        if not arg:
            _print(f"[current provider: {state.provider}]")
            return "continue"
        state.provider = arg
        state.model = None  # adapter's default_model picks up
        state.messages = []
        _print(
            f"[switched to provider={arg}; model reset to adapter default; "
            f"history cleared]"
        )
        return "continue"

    if cmd == "/model":
        if not arg:
            _print(f"[current model: {state.model or '(adapter default)'}]")
            return "continue"
        state.model = arg
        _print(f"[switched to model={arg}]")
        return "continue"

    if cmd == "/history":
        if not state.messages:
            _print("[no messages yet]")
            return "continue"
        for i, m in enumerate(state.messages):
            _print(f"  [{i}] {m.role}: {m.content[:200]}")
        return "continue"

    if cmd == "/cost":
        cost_str = f"${state.total_cost_usd:.6f}" if state.total_cost_usd > 0 else "$0"
        _print(
            f"[turns={state.turn_count}  "
            f"in={state.total_input_tokens}  out={state.total_output_tokens}  "
            f"cost={cost_str}]"
        )
        return "continue"

    return "unknown"


async def _send_turn(state: ReplState, user_text: str) -> int:
    """Send one user turn, append assistant reply on success. Returns
    0 on success, non-zero on adapter error (REPL keeps running either
    way; the return is for tests).

    On adapter error the user turn is *not* appended to state.messages
    so the user can retry the same input without doubling it up."""
    # Local import — keeps the chat module importable in test contexts
    # that don't have all five adapters installed.
    from swarph_mesh import ChatMessage, SwarphCall

    state.messages.append(ChatMessage(role="user", content=user_text))

    try:
        sc = SwarphCall(
            provider=state.provider,
            caller=state.caller,
            model=state.model,
        )
        resp = await sc.chat(
            messages=state.messages,
            system_prompt=state.system_prompt,
            temperature=state.temperature,
            max_tokens=state.max_tokens,
        )
    except Exception as exc:
        # Pop the failed user turn so retry doesn't compound it.
        state.messages.pop()
        _print(f"[error] {exc}", file=sys.stderr)
        return 1

    state.messages.append(ChatMessage(role="assistant", content=resp.text))
    state.total_input_tokens += resp.input_tokens
    state.total_output_tokens += resp.output_tokens
    state.total_cost_usd += resp.cost_usd
    state.turn_count += 1

    _print(resp.text)
    _print(
        _format_attribution(
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=resp.cost_usd,
            duration_s=resp.duration_s,
            cached=resp.cached,
        ),
        file=sys.stderr,
    )
    return 0


async def _repl_loop(state: ReplState) -> int:
    """Main REPL loop. Returns process exit code."""
    sys_line = f" system={state.system_prompt!r}" if state.system_prompt else ""
    _print(
        _BANNER.format(
            provider=state.provider,
            model=state.model or "(adapter default)",
            caller=state.caller,
            system=sys_line,
        )
    )

    while True:
        try:
            line = _read_line("> ")
        except EOFError:
            _print()
            _print("[swarph-chat] bye.")
            return 0
        except KeyboardInterrupt:
            _print()
            _print("[interrupted — type /quit to exit]")
            continue

        line = line.rstrip()
        if not line:
            continue

        if line.startswith("/"):
            result = _handle_slash(state, line)
            if result == "quit":
                _print("[swarph-chat] bye.")
                return 0
            if result == "unknown":
                _print(f"[unknown command: {line.split()[0]!r} — try /help]")
            continue

        await _send_turn(state, line)


def run_chat(argv: list[str]) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch.

    Returns process exit code."""
    args = _build_parser().parse_args(argv)
    caller = args.caller or _default_repl_caller()

    state = ReplState(
        provider=args.provider,
        model=args.model,
        caller=caller,
        system_prompt=args.system,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    return asyncio.run(_repl_loop(state))
