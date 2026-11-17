"""Card #1642: ``swarph secret-guard hook`` — Claude Code secret guard.

Synthetic fixtures only; every secret is built at runtime. No realistic
key literal appears in this file.
"""
from __future__ import annotations

import io
import json

import pytest

from swarph_cli.commands import secret_guard
from swarph_cli.commands.secret_guard import run_hook, run_secret_guard


def _sk_ant() -> str:
    return "sk-ant-" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk"


def _aws_secret_assignment() -> str:
    return "aws_secret_access_key = " + "wJalrXUtnFEMIK7MDENGbPxRfiCYzEXAMPLEKEY"[0:0] + ("Qw9Er8Ty7Ui6Op5As4Df3Gh2Jk1Lz" + "Xc7Vb6N")


def _pem_block() -> str:
    body = "MIIEvwIBADANBgkqhkiG9w0BAQEFAASC" + "QKCAQEA7synthetic0only"
    return ("-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----")


def _run(payload, capsys, monkeypatch=None, env=None):
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    rc = run_hook(stdin_text=raw)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _assert_no_secret_echo(secret: str, *texts: str) -> None:
    blob = "".join(texts)
    for i in range(len(secret) - 3):
        window = secret[i:i + 4]
        assert window not in blob, f"secret window leaked: {window!r}"


# --- (a) UserPromptSubmit -------------------------------------------------

def test_synthetic_anthropic_key_in_prompt_is_blocked_without_echo(capsys):
    secret = _sk_ant()
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"please use {secret} now"},
        capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "Anthropic" in parsed["reason"]
    _assert_no_secret_echo(secret, out, err)


def test_aws_secret_assignment_variant_is_blocked_class_named_only(capsys):
    text = _aws_secret_assignment()
    secret_value = text.split("=", 1)[1].strip()
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"config: {text}"},
        capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "AWS" in parsed["reason"]
    _assert_no_secret_echo(secret_value, out, err)


def test_pem_block_in_prompt_is_blocked(capsys):
    pem = _pem_block()
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"here:\n{pem}"},
        capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "private key" in parsed["reason"].lower()
    _assert_no_secret_echo("MIIEvwIBADANBgkqhkiG9w0BAQEFAASC", out, err)


def test_clean_prompt_passes_silently(capsys):
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit",
         "prompt": "refactor the parser and run the tests please"},
        capsys)
    assert rc == 0
    assert out == ""
    assert err == ""


def test_opt_out_env_does_not_bypass_the_guard(capsys, monkeypatch):
    secret = _sk_ant()
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"use {secret}"},
        capsys, monkeypatch=monkeypatch,
        env={"SWARPH_REDACT_OPTOUT": "1"})
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    _assert_no_secret_echo(secret, out, err)


# --- (b) PreToolUse / Bash --------------------------------------------------

def _pretool(command: str, tool: str = "Bash") -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"command": command}}


def test_cat_dotenv_is_denied(capsys):
    rc, out, err = _run(_pretool("cat x/.env"), capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ".env" in parsed["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "cat .env",
    "head -n 5 config/app.key",
    "sudo cat /home/u/auth.json",
    "env FOO=1 tail ~/.config/gh/hosts.yml",
    "cat x/.env | head -1",
    "strings secrets.key",
])
def test_credential_readers_are_denied(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "ls x/.env",
    "cat config/key.pem",
    "cat README.md",
    "echo cat x/.env",
    "grep TOKEN x/.env",
])
def test_allowed_commands_pass_silently(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    assert out == ""
    assert err == ""


def test_non_bash_tool_passes_silently(capsys):
    rc, out, err = _run(_pretool("cat x/.env", tool="Read"), capsys)
    assert rc == 0
    assert out == ""
    assert err == ""


# --- (c) fail open ----------------------------------------------------------

def test_malformed_json_fails_open_with_warning(capsys):
    rc, out, err = _run("{not json at all", capsys)
    assert rc == 0
    assert out == ""
    assert "warning" in err.lower()
    assert "secret-guard" in err


def test_handler_crash_fails_open_with_warning(capsys, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("synthetic handler crash")

    monkeypatch.setattr(secret_guard.redact, "redact_text", _boom)
    secret = _sk_ant()
    rc, out, err = _run(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"use {secret}"},
        capsys)
    assert rc == 0
    assert out == ""
    assert "warning" in err.lower()
    _assert_no_secret_echo(secret, out, err)


def test_cli_entrypoint_hook_reads_stdin(capsys, monkeypatch):
    secret = _sk_ant()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"use {secret}"})))
    rc = run_secret_guard(["hook"])
    cap = capsys.readouterr()
    assert rc == 0
    assert json.loads(cap.out)["decision"] == "block"
    _assert_no_secret_echo(secret, cap.out, cap.err)
