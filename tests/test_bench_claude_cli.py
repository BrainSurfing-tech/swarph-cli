"""Subscription bench transport: isolated claude -p, billing guard, no live call."""
import json
import types

from swarph_cli.bench import backends as B
from swarph_cli.bench import claude_cli as C


def lines(key="none", result='{"action": "BUY"}', is_error=False, subtype="success", usage=None):
    u = usage or {"input_tokens": 400, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 21, "output_tokens": 12}
    return [json.dumps({"type": "system", "subtype": "init", "apiKeySource": key, "tools": []}),
            json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": result,
                        "total_cost_usd": 0.001, "usage": u})]


def test_argv_isolation_flags():
    a = C.build_argv("claude-sonnet-5-5", "SYS")
    for flag in ("--strict-mcp-config", "--setting-sources", "--tools", "--max-turns", "--system-prompt"):
        assert flag in a
    assert a[a.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert a[a.index("--tools") + 1] == "" and a[a.index("--setting-sources") + 1] == ""
    assert "SYS" in a and not any("prompt text" in x for x in a)  # prompt goes to stdin, never argv


def test_clean_env_strips_keys():
    e = C.clean_env({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "SWARPH_CHANNEL": "dev",
                     "SWARPH_CHANNEL_CELL": "droplet", "PATH": "/usr/bin"})
    assert set(e) == {"PATH"}


def test_parse_ok():
    r = C.parse_stream(lines())
    assert r.text == '{"action": "BUY"}' and r.tokens_in == 421 and r.tokens_out == 12 and r.error is None


def test_billing_guard_rejects_api_key_source():
    r = C.parse_stream(lines(key="ANTHROPIC_API_KEY"))
    assert r.error == "billing:ANTHROPIC_API_KEY" and r.text == ""


def test_rate_limit_is_retryable_not_abstain():
    r = C.parse_stream(lines(result="Claude usage limit reached", is_error=True, subtype="error_during_execution"))
    assert r.error == "rate_limit"


def test_call_passes_prompt_on_stdin():
    seen = {}

    def runner(argv, input, capture_output, text, env, timeout):
        seen.update(argv=argv, input=input, env=env)
        return types.SimpleNamespace(returncode=0, stdout="\n".join(lines()), stderr="")

    C.call("claude-sonnet-5-5", "PROMPT TEXT", "SYS", runner=runner)
    assert seen["input"] == "PROMPT TEXT" and "PROMPT TEXT" not in seen["argv"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_parse_action_normalization():
    assert B.normalize_judge_action('{"action": " buy "}') == "BUY"
    assert B.normalize_judge_action('{"action": "hold"}') == "ABSTAIN"
    assert B.normalize_judge_action("not json") == "ABSTAIN"
