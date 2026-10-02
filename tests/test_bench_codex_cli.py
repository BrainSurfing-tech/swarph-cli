"""Codex bench transport: isolated codex exec, billing guard before the call, no live call."""
import json
import types

from swarph_cli.bench import backends as B
from swarph_cli.bench import codex_cli as C


def lines(text='{"answer": 68}', usage=None, extra=()):
    u = usage or {"input_tokens": 14410, "cached_input_tokens": 12160,
                  "cache_write_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0}
    out = [json.dumps({"type": "thread.started", "thread_id": "t"}),
           json.dumps({"type": "turn.started"})]
    out += [json.dumps(e) for e in extra]
    if text is not None:
        out.append(json.dumps({"type": "item.completed",
                               "item": {"id": "item_0", "type": "agent_message", "text": text}}))
    out.append(json.dumps({"type": "turn.completed", "usage": u}))
    return out


def home(tmp_path, payload='{"auth_mode": "chatgpt", "OPENAI_API_KEY": null}'):
    if payload is not None:
        (tmp_path / "auth.json").write_text(payload, encoding="utf-8")
    return {"CODEX_HOME": str(tmp_path), "PATH": "/usr/bin"}


def recording_runner(stdout="", stderr=""):
    seen = {"calls": 0}

    def runner(argv, input, capture_output, text, env, timeout, cwd):
        seen.update(argv=argv, input=input, env=env, cwd=cwd, calls=seen["calls"] + 1)
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

    return runner, seen


def test_argv_isolation_flags():
    a = C.build_argv("gpt-5.6-sol")
    for flag in ("--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
        assert flag in a
    assert a[a.index("--sandbox") + 1] == "read-only"
    assert a[a.index("--model") + 1] == "gpt-5.6-sol"
    assert a[-1] == "-"  # prompt is read from stdin, never argv


def test_clean_env_strips_keys():
    e = C.clean_env({"OPENAI_API_KEY": "k", "CODEX_API_KEY": "c", "SWARPH_CHANNEL": "dev",
                     "SWARPH_CHANNEL_CELL": "droplet", "PATH": "/usr/bin", "CODEX_HOME": "/h"})
    assert set(e) == {"PATH", "CODEX_HOME"}


def test_parse_ok():
    r = C.parse_stream(lines())
    assert r.text == '{"answer": 68}' and r.tokens_in == 14410 and r.tokens_out == 10
    assert r.error is None and r.cost_equiv is None and r.api_key_source == "chatgpt"


def test_parse_takes_the_last_message_and_the_last_usage():
    first = {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}}
    early = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}
    r = C.parse_stream(lines(extra=(first, early)))
    assert r.text == '{"answer": 68}' and r.tokens_in == 14410  # running total, never summed


def test_tool_items_are_not_the_answer():
    tool = {"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}}
    r = C.parse_stream(lines(extra=(tool,)))
    assert r.text == '{"answer": 68}'


def test_no_answer_is_no_result_not_an_empty_answer():
    r = C.parse_stream(lines(text=None))
    assert r.error == "no_result" and r.text == ""
    assert C.parse_stream([]).error == "no_result"


def test_rate_limit_is_retryable_from_event_or_stderr():
    ev = {"type": "error", "message": "You've hit your usage limit. Try again later."}
    assert C.parse_stream([json.dumps(ev)]).error == "rate_limit"
    assert C.parse_stream([], stderr="ERROR: You've hit your usage limit").error == "rate_limit"


def test_other_failure_is_a_cli_error_and_discards_the_text():
    ev = {"type": "turn.failed", "error": {"message": "model not found"}}
    r = C.parse_stream(lines(extra=(ev,)))
    assert r.error == "cli_error:turn.failed" and r.text == ""


def test_call_passes_system_and_prompt_on_stdin_in_an_empty_cwd(tmp_path):
    runner, seen = recording_runner(stdout="\n".join(lines()))
    env = dict(home(tmp_path), OPENAI_API_KEY="k")
    r = C.call("gpt-5.6-sol", "PROMPT TEXT", "SYS", runner=runner, env=env)
    assert r.error is None and r.text == '{"answer": 68}'
    assert seen["input"] == "SYS\n\nPROMPT TEXT" and "PROMPT TEXT" not in seen["argv"]
    assert "OPENAI_API_KEY" not in seen["env"] and seen["env"]["CODEX_HOME"] == str(tmp_path)
    assert seen["cwd"] != str(tmp_path) and "swarph-bench-codex-" in seen["cwd"]


def test_billing_guard_refuses_before_any_call(tmp_path):
    runner, seen = recording_runner(stdout="\n".join(lines()))
    r = C.call("gpt-5.6-sol", "P", "S", runner=runner,
               env=home(tmp_path, '{"auth_mode": "apikey", "OPENAI_API_KEY": "k"}'))
    assert r.error == "billing:apikey" and r.text == "" and seen["calls"] == 0


def test_unreadable_auth_is_unknown_never_a_pass(tmp_path):
    runner, seen = recording_runner(stdout="\n".join(lines()))
    for payload in (None, "not json", '{"OPENAI_API_KEY": null}', '["chatgpt"]'):
        d = tmp_path / str(abs(hash(payload)))
        d.mkdir()
        r = C.call("gpt-5.6-sol", "P", "S", runner=runner, env=home(d, payload))
        assert r.error == "billing:unknown", payload
    assert seen["calls"] == 0


def test_backend_reports_real_usage_and_carries_the_error(tmp_path):
    runner, _ = recording_runner(stdout="\n".join(lines()))
    ok = B.SubscriptionBackend(
        call_fn=lambda m, p, s: C.call(m, p, s, runner=runner, env=home(tmp_path)))
    res = ok.generate("gpt-5.6-sol", "P", "S")
    assert res.text == '{"answer": 68}' and res.tokens_in == 14410 and res.estimated is False

    bad = tmp_path / "bad"
    bad.mkdir()
    refused = B.SubscriptionBackend(
        call_fn=lambda m, p, s: C.call(m, p, s, runner=runner, env=home(bad, None)))
    res = refused.generate("gpt-5.6-sol", "P", "S")
    assert res.error == "billing:unknown" and res.text == ""


def test_codex_lane_is_selectable(monkeypatch):
    from swarph_cli.commands import bench

    monkeypatch.setattr(bench.shutil, "which", lambda name: None)
    assert bench._default_backends()["codex"].missing_creds()  # stub: preflight skips it
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/" + name)
    assert bench._default_backends()["codex"].missing_creds() == []
