"""Tests for ``swarph chat`` REPL — offline only, mocked stdin + adapter.

Live smoke (one round-trip via REPL) lives in ``test_smoke_chat.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from swarph_cli.commands import chat as chat_cmd
from swarph_cli.commands.chat import (
    ReplState,
    _default_repl_caller,
    _format_attribution,
    _handle_slash,
)


# ---------------------------------------------------------------------------
# Caller default
# ---------------------------------------------------------------------------


def test_default_repl_caller_uses_user_env(monkeypatch):
    monkeypatch.setenv("USER", "pierre")
    assert _default_repl_caller() == "cli.repl.pierre"


def test_default_repl_caller_slugs_non_alnum(monkeypatch):
    monkeypatch.setenv("USER", "Some.User-1")
    monkeypatch.delenv("LOGNAME", raising=False)
    assert _default_repl_caller() == "cli.repl.some_user_1"


def test_default_repl_caller_falls_back_when_user_unset(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    out = _default_repl_caller()
    # caller must satisfy the dotted-lowercase convention even on
    # fallback — swarph_shared.validate_caller is strict.
    assert out
    assert out.islower()
    assert " " not in out


# ---------------------------------------------------------------------------
# Attribution footer
# ---------------------------------------------------------------------------


def test_format_attribution_zero_cost_renders_dollar_zero():
    s = _format_attribution(
        input_tokens=10, output_tokens=5, cost_usd=0.0, duration_s=1.23, cached=False
    )
    assert "$0" in s
    assert "$0.0000" not in s  # the >0 branch shouldn't fire


def test_format_attribution_nonzero_cost_renders_4dp():
    s = _format_attribution(
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0123,
        duration_s=2.0,
        cached=False,
    )
    assert "$0.0123" in s


def test_format_attribution_marks_cached():
    s = _format_attribution(
        input_tokens=10, output_tokens=5, cost_usd=0.001, duration_s=0.5, cached=True
    )
    assert "(cached)" in s


# ---------------------------------------------------------------------------
# Slash commands — pure state mutation
# ---------------------------------------------------------------------------


def _state(provider="gemini", model=None, system=None) -> ReplState:
    return ReplState(
        provider=provider,
        model=model,
        caller="cli.repl.test",
        system_prompt=system,
    )


def test_slash_quit_returns_quit():
    s = _state()
    assert _handle_slash(s, "/quit") == "quit"


def test_slash_exit_alias_returns_quit():
    s = _state()
    assert _handle_slash(s, "/exit") == "quit"


def test_slash_help_returns_continue():
    s = _state()
    assert _handle_slash(s, "/help") == "continue"


def test_slash_clear_resets_messages():
    s = _state()
    s.messages = ["m1", "m2", "m3"]
    assert _handle_slash(s, "/clear") == "continue"
    assert s.messages == []


def test_slash_reset_alias_clears_messages():
    s = _state()
    s.messages = ["m1"]
    assert _handle_slash(s, "/reset") == "continue"
    assert s.messages == []


def test_slash_clear_keeps_system_prompt():
    s = _state(system="be terse")
    s.messages = ["m1"]
    _handle_slash(s, "/clear")
    assert s.system_prompt == "be terse"


def test_slash_system_with_arg_sets_prompt():
    s = _state()
    _handle_slash(s, "/system you are an assistant")
    assert s.system_prompt == "you are an assistant"


def test_slash_system_bare_clears_prompt():
    s = _state(system="be terse")
    _handle_slash(s, "/system")
    assert s.system_prompt is None


def test_slash_provider_switch_resets_history():
    s = _state(provider="gemini")
    s.messages = ["m1", "m2"]
    s.model = "gemini-2.5-flash"
    _handle_slash(s, "/provider claude")
    assert s.provider == "claude"
    assert s.model is None  # picks up adapter default
    assert s.messages == []


def test_slash_provider_bare_shows_current(capsys):
    s = _state(provider="grok")
    _handle_slash(s, "/provider")
    captured = capsys.readouterr()
    assert "grok" in captured.out


def test_slash_model_sets_model():
    s = _state()
    _handle_slash(s, "/model deepseek-v4-pro")
    assert s.model == "deepseek-v4-pro"


def test_slash_history_with_no_messages(capsys):
    s = _state()
    _handle_slash(s, "/history")
    captured = capsys.readouterr()
    assert "no messages" in captured.out


def test_slash_cost_prints_running_totals(capsys):
    s = _state()
    s.turn_count = 3
    s.total_input_tokens = 150
    s.total_output_tokens = 75
    s.total_cost_usd = 0.0042
    _handle_slash(s, "/cost")
    out = capsys.readouterr().out
    assert "turns=3" in out
    assert "in=150" in out
    assert "out=75" in out
    assert "$0.004200" in out


def test_slash_cost_zero_renders_dollar_zero(capsys):
    s = _state()
    _handle_slash(s, "/cost")
    out = capsys.readouterr().out
    assert "cost=$0]" in out


def test_unknown_slash_returns_unknown():
    s = _state()
    assert _handle_slash(s, "/banana split") == "unknown"


# ---------------------------------------------------------------------------
# _send_turn — adapter wiring with mocked SwarphCall
# ---------------------------------------------------------------------------


def _mock_response(*, text="ok", in_tok=10, out_tok=5, cost=0.001, dur=1.0, cached=False):
    """Build a minimal LLMResponse-compatible shape."""
    from swarph_mesh.types import LLMResponse

    return LLMResponse(
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        duration_s=dur,
        cached=cached,
    )


class _FakeSwarphCall:
    """Drop-in for SwarphCall in chat tests. Captures the args it was
    called with so tests can assert on them."""

    captured: list = []
    response: object = None
    raise_exc: object = None

    def __init__(self, *, provider, caller, model=None):
        self.provider = provider
        self.caller = caller
        self.model = model

    async def chat(self, **kwargs):
        # Snapshot the messages list at call time — the REPL mutates
        # the same list after the call returns (appends assistant turn),
        # so a reference-only capture would show post-call state.
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = list(snapshot["messages"])
        type(self).captured.append({"kwargs": snapshot, "model": self.model})
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        return type(self).response


@pytest.fixture
def fake_call(monkeypatch):
    """Patch swarph_mesh.SwarphCall in the chat module's namespace."""
    _FakeSwarphCall.captured = []
    _FakeSwarphCall.response = _mock_response()
    _FakeSwarphCall.raise_exc = None

    # The chat module imports SwarphCall locally inside _send_turn, so
    # we patch swarph_mesh.SwarphCall directly.
    import swarph_mesh

    monkeypatch.setattr(swarph_mesh, "SwarphCall", _FakeSwarphCall)
    return _FakeSwarphCall


def test_send_turn_appends_assistant_reply(fake_call):
    s = _state()
    fake_call.response = _mock_response(text="hi there")
    asyncio.run(chat_cmd._send_turn(s, "hello"))
    assert len(s.messages) == 2
    assert s.messages[0].role == "user"
    assert s.messages[0].content == "hello"
    assert s.messages[1].role == "assistant"
    assert s.messages[1].content == "hi there"


def test_send_turn_accumulates_cost_and_tokens(fake_call):
    s = _state()
    fake_call.response = _mock_response(in_tok=20, out_tok=10, cost=0.005)
    asyncio.run(chat_cmd._send_turn(s, "q1"))
    asyncio.run(chat_cmd._send_turn(s, "q2"))
    assert s.turn_count == 2
    assert s.total_input_tokens == 40
    assert s.total_output_tokens == 20
    assert s.total_cost_usd == pytest.approx(0.010)


def test_send_turn_passes_system_prompt_through(fake_call):
    s = _state(system="be terse")
    asyncio.run(chat_cmd._send_turn(s, "q"))
    assert fake_call.captured[0]["kwargs"]["system_prompt"] == "be terse"


def test_send_turn_passes_temperature_and_max_tokens(fake_call):
    s = _state()
    s.temperature = 0.1
    s.max_tokens = 256
    asyncio.run(chat_cmd._send_turn(s, "q"))
    kw = fake_call.captured[0]["kwargs"]
    assert kw["temperature"] == 0.1
    assert kw["max_tokens"] == 256


def test_send_turn_threads_messages_for_multiturn_history(fake_call):
    """Second turn must include the first user+assistant turn so the
    adapter sees the full context."""
    s = _state()
    fake_call.response = _mock_response(text="a1")
    asyncio.run(chat_cmd._send_turn(s, "q1"))
    fake_call.response = _mock_response(text="a2")
    asyncio.run(chat_cmd._send_turn(s, "q2"))

    second_call_messages = fake_call.captured[1]["kwargs"]["messages"]
    assert len(second_call_messages) == 3  # q1, a1, q2 (the new turn)
    assert second_call_messages[0].content == "q1"
    assert second_call_messages[1].content == "a1"
    assert second_call_messages[2].content == "q2"


def test_send_turn_pops_user_turn_on_adapter_error(fake_call, capsys):
    """Adapter error: user can retry without doubling the input."""
    s = _state()
    fake_call.raise_exc = RuntimeError("provider exploded")
    rc = asyncio.run(chat_cmd._send_turn(s, "q"))
    assert rc == 1
    assert s.messages == []  # popped
    assert s.turn_count == 0
    err = capsys.readouterr().err
    assert "[error]" in err
    assert "provider exploded" in err


# ---------------------------------------------------------------------------
# REPL loop — script stdin via _read_line monkeypatch
# ---------------------------------------------------------------------------


def _scripted_input(lines: list[str]):
    """Build a _read_line replacement that returns each line in order,
    then raises EOFError to terminate the loop cleanly."""
    it = iter(lines)

    def fake(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError()

    return fake


def test_repl_loop_exits_on_eof(monkeypatch, fake_call, capsys):
    monkeypatch.setattr(chat_cmd, "_read_line", _scripted_input([]))
    s = _state()
    rc = asyncio.run(chat_cmd._repl_loop(s))
    assert rc == 0
    out = capsys.readouterr().out
    assert "swarph chat" in out  # banner printed
    assert "bye" in out


def test_repl_loop_exits_on_quit(monkeypatch, fake_call):
    monkeypatch.setattr(chat_cmd, "_read_line", _scripted_input(["/quit"]))
    s = _state()
    rc = asyncio.run(chat_cmd._repl_loop(s))
    assert rc == 0


def test_repl_loop_round_trips_one_message(monkeypatch, fake_call):
    fake_call.response = _mock_response(text="response_text")
    monkeypatch.setattr(chat_cmd, "_read_line", _scripted_input(["hello", "/quit"]))
    s = _state()
    asyncio.run(chat_cmd._repl_loop(s))
    assert s.turn_count == 1
    assert s.messages[0].content == "hello"
    assert s.messages[1].content == "response_text"


def test_repl_loop_skips_empty_lines(monkeypatch, fake_call):
    """Empty/whitespace input shouldn't trigger an LLM call."""
    monkeypatch.setattr(
        chat_cmd, "_read_line", _scripted_input(["", "   ", "/quit"])
    )
    s = _state()
    asyncio.run(chat_cmd._repl_loop(s))
    assert s.turn_count == 0
    assert fake_call.captured == []


def test_repl_loop_handles_keyboard_interrupt(monkeypatch, fake_call, capsys):
    """Ctrl-C mid-line should print an interrupt notice and continue,
    not abort the REPL."""
    state_iter = iter([KeyboardInterrupt(), "/quit"])

    def fake_read(prompt):
        v = next(state_iter)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(chat_cmd, "_read_line", fake_read)
    s = _state()
    rc = asyncio.run(chat_cmd._repl_loop(s))
    assert rc == 0
    out = capsys.readouterr().out
    assert "interrupted" in out


def test_repl_loop_unknown_slash_message(monkeypatch, fake_call, capsys):
    monkeypatch.setattr(
        chat_cmd, "_read_line", _scripted_input(["/banana", "/quit"])
    )
    s = _state()
    asyncio.run(chat_cmd._repl_loop(s))
    out = capsys.readouterr().out
    assert "unknown command" in out


def test_repl_loop_provider_switch_clears_history(monkeypatch, fake_call):
    fake_call.response = _mock_response(text="r1")
    monkeypatch.setattr(
        chat_cmd,
        "_read_line",
        _scripted_input(["q1", "/provider claude", "/quit"]),
    )
    s = _state(provider="gemini")
    asyncio.run(chat_cmd._repl_loop(s))
    assert s.provider == "claude"
    assert s.messages == []  # cleared on switch


# ---------------------------------------------------------------------------
# Verb dispatch — main.py routes "chat" to run_chat
# ---------------------------------------------------------------------------


def test_main_dispatches_chat_verb(monkeypatch):
    """``swarph chat ...`` should land in run_chat with rest of argv."""
    from swarph_cli import main as main_mod

    captured = {}

    def fake_run_chat(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(
        "swarph_cli.commands.chat.run_chat", fake_run_chat
    )
    rc = main_mod.main(["chat", "--provider", "claude"])
    assert rc == 0
    assert captured["argv"] == ["--provider", "claude"]
