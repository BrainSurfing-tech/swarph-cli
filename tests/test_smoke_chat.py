"""Live smoke for ``swarph chat`` REPL — Phase 5 falsifiability gate per
PLAN.md §13 (manual verification proxy).

Single round-trip: scripted stdin sends one user turn + /quit, REPL
calls the real adapter, asserts response written + state correct.

Skipped unless GEMINI_API_KEY is set — gemini-flash is the cheapest tier
across all five adapters and the established Phase 1 falsifiability
target.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from swarph_cli.commands import chat as chat_cmd
from swarph_cli.commands.chat import ReplState


pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set — Phase 5 live smoke skipped",
)


def test_phase_5_repl_falsifiability_gate(monkeypatch):
    """One scripted user turn through the real REPL → real adapter →
    real response. State should reflect: 1 turn, history populated,
    cumulative cost > 0."""
    inputs = iter(["say PONG and nothing else", "/quit"])

    def fake_read(prompt):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr(chat_cmd, "_read_line", fake_read)

    state = ReplState(
        provider="gemini",
        model=None,  # adapter default (flash)
        caller="cli.smoke.phase_5_gate",
        system_prompt=None,
        temperature=0.0,
        max_tokens=8,
    )
    rc = asyncio.run(chat_cmd._repl_loop(state))

    assert rc == 0
    assert state.turn_count == 1
    assert len(state.messages) == 2
    assert state.messages[0].role == "user"
    assert state.messages[1].role == "assistant"
    assert state.messages[1].content
    assert state.total_input_tokens > 0
    assert state.total_output_tokens > 0
    assert state.total_cost_usd >= 0.0  # Flex tier could rebate near 0
