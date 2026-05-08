"""Phase 2 one-shot CLI tests.

Most tests run offline using a mock adapter registered against
swarph-mesh's adapter registry — same pattern as swarph-mesh's
test_swarph_call. The live falsifiability gate (real Gemini API)
lives in test_smoke_one_shot.py and is skipped without
GEMINI_API_KEY.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator, Optional
from unittest.mock import patch

import pytest

from swarph_cli import __version__
from swarph_cli.caller import _sanitize_username, default_caller
from swarph_cli.main import _build_parser, main


# ---------------------------------------------------------------------------
# Fixtures — register a mock adapter so the CLI's SwarphCall path runs offline
# ---------------------------------------------------------------------------


class _MockAdapter:
    name = "mock"
    default_model = "mock-model-v1"

    def __init__(self, text="ok", input_tokens=10, output_tokens=5, cost_usd=0.001):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.calls = []

    async def chat(self, messages, model, **kwargs):
        from swarph_mesh import LLMResponse

        self.calls.append({"messages": list(messages), "model": model, **kwargs})
        return LLMResponse(
            text=self.text,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            duration_s=0.05,
        )

    async def stream(self, *a, **kw) -> AsyncIterator[str]:
        if False:
            yield ""

    def cost_per_token(self, model):
        return (0.0, 0.0)


@pytest.fixture
def mock_adapter(tmp_path):
    """Register a mock adapter as 'mock' AND swap the default
    attribution writer to a tmp file so tests don't pollute
    ~/.swarph/attribution.jsonl."""
    from swarph_mesh import register_adapter
    from swarph_mesh.adapters import reset_registry
    from swarph_mesh.attribution import (
        FileAttributionWriter,
        NullAttributionWriter,
        set_default_writer,
    )

    reset_registry()
    a = _MockAdapter()
    register_adapter("mock", a)
    set_default_writer(FileAttributionWriter(path=tmp_path / "attribution.jsonl"))
    yield a
    reset_registry()
    set_default_writer(NullAttributionWriter())


# ---------------------------------------------------------------------------
# Caller helper
# ---------------------------------------------------------------------------


def test_default_caller_starts_with_cli_oneshot():
    c = default_caller()
    assert c.startswith("cli.oneshot.")
    # Must satisfy the swarph_shared CALLER_PATTERN regex:
    from swarph_shared import validate_caller

    validate_caller(c)


def test_sanitize_username_handles_capitals():
    assert _sanitize_username("Alice") == "alice"


def test_sanitize_username_handles_hyphens():
    assert _sanitize_username("alice-bob") == "alice_bob"


def test_sanitize_username_handles_digits_first():
    assert _sanitize_username("42alice") == "u_42alice"


def test_sanitize_username_handles_empty():
    assert _sanitize_username("") == "unknown"


def test_sanitize_username_handles_special_chars():
    assert _sanitize_username("a!b@c#d") == "a_b_c_d"


# ---------------------------------------------------------------------------
# argparse — flag parsing
# ---------------------------------------------------------------------------


def test_parser_handles_positional_prompt():
    args = _build_parser().parse_args(["hello world"])
    assert args.prompt == "hello world"
    assert args.provider == "gemini"  # default


def test_parser_handles_provider_flag():
    args = _build_parser().parse_args(["x", "--provider", "claude"])
    assert args.provider == "claude"


def test_parser_handles_json_flag():
    args = _build_parser().parse_args(["x", "--json"])
    assert args.json is True


def test_parser_handles_quiet_flag():
    args = _build_parser().parse_args(["-q", "x"])
    assert args.quiet is True


def test_parser_no_prompt_returns_none():
    """Empty argv → prompt is None → main() prints banner."""
    args = _build_parser().parse_args([])
    assert args.prompt is None


# ---------------------------------------------------------------------------
# Banner mode (no prompt)
# ---------------------------------------------------------------------------


def test_main_no_prompt_prints_banner_exits_zero(capsys):
    rc = main(argv=[])
    assert rc == 0
    captured = capsys.readouterr()
    assert "swarph" in captured.err
    assert __version__ in captured.err
    assert "Phase 2" in captured.err


# ---------------------------------------------------------------------------
# One-shot — adapter dispatch + stdout/stderr split
# ---------------------------------------------------------------------------


def test_one_shot_invokes_adapter_with_prompt(mock_adapter, capsys):
    rc = main(argv=["--provider", "mock", "hello"])
    assert rc == 0
    assert len(mock_adapter.calls) == 1
    assert mock_adapter.calls[0]["messages"][0].content == "hello"


def test_one_shot_response_text_to_stdout(mock_adapter, capsys):
    mock_adapter.text = "the response"
    rc = main(argv=["--provider", "mock", "x"])
    captured = capsys.readouterr()
    assert "the response" in captured.out
    # Attribution footer should NOT be on stdout
    assert "+" not in captured.out
    assert "USD" not in captured.out
    # Attribution footer SHOULD be on stderr
    assert "10+5t" in captured.err  # input_tokens + output_tokens
    assert "$0.0010" in captured.err
    assert "caller=cli.oneshot." in captured.err
    assert "provider=mock" in captured.err


def test_one_shot_quiet_suppresses_attribution(mock_adapter, capsys):
    rc = main(argv=["--quiet", "--provider", "mock", "x"])
    captured = capsys.readouterr()
    assert "the response" not in captured.err  # no attribution at all
    assert "10+5t" not in captured.err


def test_one_shot_passes_temperature(mock_adapter, capsys):
    main(argv=["--provider", "mock", "--temperature", "0.1", "x"])
    assert mock_adapter.calls[0]["temperature"] == pytest.approx(0.1)


def test_one_shot_passes_system_prompt(mock_adapter, capsys):
    main(argv=["--provider", "mock", "--system", "be terse", "x"])
    assert mock_adapter.calls[0]["system_prompt"] == "be terse"


def test_one_shot_passes_max_tokens(mock_adapter, capsys):
    main(argv=["--provider", "mock", "--max-tokens", "256", "x"])
    assert mock_adapter.calls[0]["max_tokens"] == 256


def test_one_shot_uses_explicit_model(mock_adapter, capsys):
    main(argv=["--provider", "mock", "--model", "override", "x"])
    assert mock_adapter.calls[0]["model"] == "override"


def test_one_shot_uses_explicit_caller(mock_adapter, capsys):
    main(argv=["--provider", "mock", "--caller", "test.suite.explicit", "x"])
    captured = capsys.readouterr()
    assert "caller=test.suite.explicit" in captured.err


def test_one_shot_invalid_caller_exits_nonzero(mock_adapter, capsys):
    """Caller convention enforced at SwarphCall construction (via
    swarph_shared.validate_caller) — invalid callers fail loud.

    main() catches the ValueError in _run_one_shot's try/except and
    returns 1.
    """
    rc = main(argv=["--provider", "mock", "--caller", "Invalid!", "x"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "swarph: call failed:" in captured.err


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


def test_json_flag_pretty_prints_parsed_dict(mock_adapter, capsys):
    mock_adapter.text = '{"action": "BUY", "ticker": "MSFT"}'
    rc = main(argv=["--provider", "mock", "--json", "give me a trade"])
    assert rc == 0
    captured = capsys.readouterr()
    # Pretty-printed (indented) JSON on stdout
    assert '"action": "BUY"' in captured.out
    assert '"ticker": "MSFT"' in captured.out


def test_json_flag_falls_back_to_raw_on_parse_fail(mock_adapter, capsys):
    """When json mode is on but the harness couldn't parse, exit 1
    + print raw text to stdout so the caller can recover."""
    mock_adapter.text = "this is just prose, no json"
    rc = main(argv=["--provider", "mock", "--json", "x"])
    assert rc == 1
    captured = capsys.readouterr()
    # Raw text printed on stdout for caller recovery
    assert "this is just prose" in captured.out
    # error_class shows up in attribution footer
    assert "error_class=malformed_json" in captured.err


def test_schema_path_loads_json_file(mock_adapter, capsys, tmp_path):
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object", "required": ["x"]}))
    mock_adapter.text = '{"x": 1}'
    rc = main(argv=[
        "--provider", "mock",
        "--schema", str(schema_file),
        "give me an object",
    ])
    assert rc == 0


def test_schema_missing_file_exits_2(mock_adapter, capsys, tmp_path):
    """Missing schema file should fail loud BEFORE any LLM call."""
    bogus = str(tmp_path / "does-not-exist.json")
    with pytest.raises(SystemExit) as excinfo:
        main(argv=["--provider", "mock", "--schema", bogus, "x"])
    assert excinfo.value.code == 2


def test_schema_invalid_json_exits_2(mock_adapter, capsys, tmp_path):
    bad_schema = tmp_path / "bad.json"
    bad_schema.write_text("{not valid")
    with pytest.raises(SystemExit) as excinfo:
        main(argv=["--provider", "mock", "--schema", str(bad_schema), "x"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Adapter error handling
# ---------------------------------------------------------------------------


def test_adapter_error_returns_nonzero(capsys):
    """Adapter raising → exit code 1 + error message on stderr."""
    from swarph_mesh import register_adapter
    from swarph_mesh.adapters import reset_registry

    class _BrokenAdapter:
        name = "broken"
        default_model = "broken-v1"

        async def chat(self, *a, **kw):
            raise RuntimeError("provider down")

        async def stream(self, *a, **kw):
            if False:
                yield ""

        def cost_per_token(self, model):
            return (0.0, 0.0)

    reset_registry()
    register_adapter("broken", _BrokenAdapter())
    try:
        rc = main(argv=["--provider", "broken", "x"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "swarph: call failed:" in captured.err
        assert "provider down" in captured.err
    finally:
        reset_registry()


# ---------------------------------------------------------------------------
# Subprocess invocation — verifies the entry-point script wiring
# ---------------------------------------------------------------------------


def test_version_flag_via_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "swarph_cli.main", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_no_args_subprocess_prints_banner():
    result = subprocess.run(
        [sys.executable, "-m", "swarph_cli.main"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "swarph" in result.stderr


# ---------------------------------------------------------------------------
# swarph-mesh + swarph-shared deps still resolvable (carry-forward from v0.0.1)
# ---------------------------------------------------------------------------


def test_swarph_mesh_dep_resolvable():
    import swarph_mesh

    assert hasattr(swarph_mesh, "SwarphCall")
    assert hasattr(swarph_mesh, "ChatMessage")


def test_swarph_shared_dep_resolvable():
    import swarph_shared

    assert hasattr(swarph_shared, "validate_node_name")
