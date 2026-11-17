"""Card #1642: ``swarph secret-guard hook`` — Claude Code secret guard.

Synthetic fixtures only; every secret is built at runtime. No realistic
key literal appears in this file.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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


# --- (d) G1: harness turns + ids never block -------------------------------

def _harness(prompt: str) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt}


def test_g1_task_notification_turn_passes_even_with_secret_shaped_content(capsys):
    secret = _sk_ant()
    prompt = (
        "<task-notification>\n<task-id>task-123</task-id>\n"
        f"<tool-use-id>toolu_01A2b3C4d5E6f7G8h9I0jKlMnOp</tool-use-id>\n"
        f"<summary>used {secret}</summary>\n</task-notification>"
    )
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == ""


def test_g1_system_reminder_turn_passes(capsys):
    secret = _sk_ant()
    prompt = f"<system-reminder>\nreminder mentioning {secret}\n</system-reminder>"
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == ""


def test_g1_channel_dm_turn_passes(capsys):
    secret = _sk_ant()
    prompt = (
        '<channel source="plugin:swarph:swarph" from_node="peer-a" '
        'id="msg-1" kind="dm" card="1">\n'
        f"peer says {secret}\n</channel>"
    )
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == ""


def test_g1_unclosed_or_unanchored_envelope_still_blocked(capsys):
    secret = _sk_ant()
    for prompt in (
        f"<task-notification>\nno closing tag {secret}",
        f"please read <task-notification> {secret} </task-notification>",
        f'<channel source="plugin:swarph:swarph">\n{secret}',
    ):
        rc, out, err = _run(_harness(prompt), capsys)
        assert rc == 0
        assert json.loads(out)["decision"] == "block", prompt[:40]


def test_g1_payload_source_field_passes(capsys):
    secret = _sk_ant()
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": f"use {secret}",
               "promptSource": "system"}
    rc, out, err = _run(payload, capsys)
    assert rc == 0
    assert out == ""


def test_g1_ids_in_typed_prompt_pass(capsys):
    prompts = [
        "session 550e8400-e29b-41d4-a716-446655440000 finished ok",
        "<tool-use-id>toolu_01A2b3C4d5E6f7G8h9I0jKlMnOpQrSt</tool-use-id> done",
        "request_id: req_8f3kD9s2LpQ7xR4vN6mZ1aBcDeFgHiJkLm failed",
        '"tool_use_id": "toolu_01A2b3C4d5E6f7G8h9I0jKlMnOpQrSt"',
        "message id msg_01XyZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12 delivered",
    ]
    for prompt in prompts:
        rc, out, err = _run(_harness(prompt), capsys)
        assert rc == 0
        assert out == "", prompt[:40]


def test_g1_id_labelled_known_secret_still_blocked(capsys):
    secret = _sk_ant()
    rc, out, err = _run(_harness(f"request_id: {secret}"), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo(secret, out, err)


def test_g1_bare_secret_still_blocked(capsys):
    secret = _sk_ant()
    rc, out, err = _run(_harness(f"req_ please use {secret}"), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"


# --- (e) F1: redirection + command separators ------------------------------

@pytest.mark.parametrize("command", [
    "cat < x/.env",
    "cat <x/.env",
    "cat < ./prod.env",
    "< x/.env cat",
    "echo hi; cat x/.env",
    "echo hi && cat x/.env",
    "echo hi || cat x/.env",
    "echo hi | cat x/.env",
    "echo hi\ncat x/.env",
    "echo hi\nsudo cat x/.env",
    'echo "done"; cat x/.env',
])
def test_f1_redirect_and_separator_readers_denied(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny", command


@pytest.mark.parametrize("command", [
    'echo "; cat x/.env"',
    "echo '; cat x/.env'",
    "echo 'line1\ncat x/.env'",
    'echo "a && cat x/.env"',
    "grep TOKEN < x/.env",
])
def test_f1_quoted_separator_and_non_reader_redirect_pass(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    assert out == "", command


# --- (f) F3: deny reason names path class only -------------------------------

@pytest.mark.parametrize("command,cls,basename", [
    ("cat secrets/prod.env", ".env file", "prod.env"),
    ("cat secrets/deploy.key", "key file", "deploy.key"),
    ("cat x/auth.json", "auth.json credentials file", "x/auth.json"),
    ("cat ~/.config/gh/hosts.yml", "gh hosts file", "hosts.yml"),
])
def test_f3_deny_reason_names_class_not_basename(capsys, command, cls, basename):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert cls in reason
    if basename not in cls:
        assert basename not in reason


# --- (g) W: wiring + pathological prompt timing ------------------------------

def test_w_wiring_documented_with_fail_open_and_timeout():
    root = Path(__file__).resolve().parents[1]
    guide = (root / "src" / "swarph_cli" / "guide" / "GUIDE.md").read_text(
        encoding="utf-8")
    assert "swarph secret-guard hook || exit 0" in guide
    assert "5" in guide.split("swarph secret-guard hook || exit 0")[1][:200]


def test_w_pathological_200kb_prompt_within_hook_timeout():
    prompt = ("xK9mQ2wZ8vN4bR7tY3pL6sD1fG5hJ0a " * 7000)[:200_000]
    payload = json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt})
    src = str(Path(secret_guard.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=src)
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli", "secret-guard", "hook"],
        input=payload, capture_output=True, text=True, timeout=5, env=env)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    assert elapsed < 5


def _high_entropy_value() -> str:
    return "Xk9mQ2wZ8vN4bR7tY3pL6sD1fG5hJ0aS3dF6hJ"


# --- (h) P4: non-secret values after secret keys do not block ---------------

@pytest.mark.parametrize("prompt", [
    "call(api_key=my_api_key)",
    "call(api_key=some_var)",
    "api_key=None",
    "api_key=true",
    "api_key=false",
    "api_key=True",
    "password=None",
    "client_secret=None",
    "use a Bearer token for authentication",
    "Authorization: Bearer token",
])
def test_p4_nonsecret_values_pass(capsys, prompt):
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == "", prompt


@pytest.mark.parametrize("prompt_builder", [
    lambda v: f"api_key={v}",
    lambda v: f"password={v}",
    lambda v: f"Authorization: Bearer {v}",
])
def test_p4_secret_shaped_value_after_same_keys_still_blocked(capsys, prompt_builder):
    secret = _high_entropy_value()
    prompt = prompt_builder(secret)
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", prompt[:40]
    _assert_no_secret_echo(secret, out, err)


# --- (i) C: shlex with no comment characters --------------------------------

@pytest.mark.parametrize("command", [
    "# note\ncat x/.env",
    "echo hi # note\ncat x/.env",
    "echo a#b\ncat x/.env",
    "echo foo#bar\ncat x/.env",
])
def test_c_comment_and_hash_lines_do_not_hide_reader(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny", command


@pytest.mark.parametrize("command", [
    "echo .env",
    "cat <<< .env",
    "cat <<< 'x/.env'",
    'cat <<< "x/.env"',
])
def test_c_here_string_and_echo_printing_env_allowed(capsys, command):
    rc, out, err = _run(_pretool(command), capsys)
    assert rc == 0
    assert out == "", command


# --- (j) T (was: cap before redact; now S/#5670 chunked scan) ---------------
# #5667 asserted these prompts return within 5s because >16 KB prompts
# were skipped entirely. #5670 removes that cap: the same prompts are
# now fully scanned in chunks, must still return within 5s via the
# installed verb, and must not be blocked (they carry no credential).

@pytest.mark.parametrize("word", ["curl", "mysql"])
def test_t_200kb_repeated_word_prompt_within_5s(word):
    prompt = ((word + " ") * 50000)[:200_000]
    payload = json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt})
    src = str(Path(secret_guard.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=src)
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli", "secret-guard", "hook"],
        input=payload, capture_output=True, text=True, timeout=5, env=env)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    assert elapsed < 5
    assert '"decision": "block"' not in proc.stdout


# --- (k) I: id scrubbing by shape -------------------------------------------

@pytest.mark.parametrize("prompt", [
    "tool_use_id toolu_01A2b3C4d5E6f7G8h9I0jKlMnOpQrStUv done",
    "toolu_01A2b3C4d5E6f7G8h9I0jKlMnOpQrStUv done",
    "request req_8f3kD9s2LpQ7xR4vN6mZ1aBcDeFgHiJkLmNoPq failed",
    "req_8f3kD9s2LpQ7xR4vN6mZ1aBcDeFgHiJkLmNoPq failed",
    "msg_01XyZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12 delivered",
    "request 550e8400-e29b-41d4-a716-446655440000 finished",
    "id 550e8400-e29b-41d4-a716-446655440000 finished",
])
def test_i_id_shapes_scrubbed_wherever_they_appear(capsys, prompt):
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == "", prompt[:50]


@pytest.mark.parametrize("prompt_builder", [
    lambda v: f"session_id={v}",
    lambda v: f'"id": "{v}"',
    lambda v: f"session_id: {v}",
])
def test_i_generic_high_entropy_after_id_label_still_blocked(capsys, prompt_builder):
    secret = _high_entropy_value()
    prompt = prompt_builder(secret)
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", prompt[:40]
    _assert_no_secret_echo(secret, out, err)


def test_i_known_credential_never_scrubbed_in_id_context(capsys):
    secret = _sk_ant()
    for prompt in (f"tool_use_id {secret}", f"request {secret}"):
        rc, out, err = _run(_harness(prompt), capsys)
        assert rc == 0
        assert json.loads(out)["decision"] == "block", prompt[:40]
        _assert_no_secret_echo(secret, out, err)


# --- (l) E: envelope exemption only for whole prompt -------------------------

def test_e_multiple_complete_envelopes_pass(capsys):
    secret = _sk_ant()
    prompt = (
        f"<system-reminder>\nmention {secret}\n</system-reminder>\n\n"
        f"<task-notification>\nmention {secret}\n</task-notification>"
    )
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert out == ""


def test_e_text_between_envelopes_is_scanned_and_blocks(capsys):
    secret = _sk_ant()
    prompt = (
        "<system-reminder>\nnote\n</system-reminder>\n"
        f"use {secret} now\n"
        "<task-notification>\nnote\n</task-notification>"
    )
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo(secret, out, err)


# --- (m) P4' (#5670): narrow whole-value filter -----------------------------
# Every DSN key redact matches (redact._DSN_KEY_VALUE_RE spellings), plus
# Bearer and the AWS/assignment-layer keys where redact fires on the value
# length used. Short values after aws_* keys are not redact hits at all
# (redact length-gates them at 20+), so those keys are covered below with
# a redact-matching value, not with the short-password matrix.
_P4P_KEYS = [
    "password", "passwd", "pwd", "api_key", "apikey", "api-key",
    "auth_token", "authtoken", "auth-token", "access_token",
    "accesstoken", "access-token", "mesh_gateway_token", "pgpassword",
    "rediscli_auth", "client_secret", "secret",
]
# 12 synthetic short passwords. All but "12345678" passed (were allowed)
# under dce3f5c's loose filter; "12345678" already blocked there because
# the old identifier check rejected digit-leading values — it stays in
# the matrix so the narrow filter keeps blocking it.
_P4P_PASSWORDS = [
    "p@ss" + "w0rdX",        # symbol as second character
    "a!bcd" + "efgH",        # symbol as second character, mixed case
    "Abc12" + "3xy",         # mixed case with digits (8 chars)
    "Xy9!a" + "bcZ",         # mixed case, digits and symbol (8 chars)
    "letmein",          # weak lower-case word
    "abc12345",         # lower-case + digits
    "hunt" + "er!2A",        # symbol mid-value, mixed case
    "My_Pas" + "sw0rd",      # mixed case with underscore
    "Qwerty" + "!23",        # mixed case with symbol
    "Ab3dEf6h" + "Ij9KlMn",  # 15-char mixed-case password
    "Ab1Cd2Ef3" + "Gh4Ij5",  # 15-char token string (Bearer length, below)
    "12345678",         # digits only (already blocked at dce3f5c)
]


@pytest.mark.parametrize("key", _P4P_KEYS)
@pytest.mark.parametrize("password", _P4P_PASSWORDS)
def test_p4p_short_password_blocks_under_each_secret_key(capsys, key, password):
    prompt = f"{key}={password}"
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", prompt
    _assert_no_secret_echo(password, out, err)


@pytest.mark.parametrize("key", _P4P_KEYS)
def test_p4p_four_word_passphrase_blocks_under_each_secret_key(capsys, key):
    prompt = f"{key}=correct horse battery staple"
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", prompt[:40]


@pytest.mark.parametrize("key", _P4P_KEYS)
@pytest.mark.parametrize("value", [
    "None", "true", "false", "True",
])
def test_p4p_literal_harmless_values_pass_under_each_key(capsys, key, value):
    rc, out, err = _run(_harness(f"{key}={value}"), capsys)
    assert rc == 0
    assert out == "", f"{key}={value}"


@pytest.mark.parametrize("key", _P4P_KEYS)
def test_p4p_own_name_and_variable_harmless_values_pass(capsys, key):
    for value in (key, "some_var", "my_api_key", "config.api_key",
                  "settings.mesh_gateway_token"):
        rc, out, err = _run(_harness(f"{key}={value}"), capsys)
        assert rc == 0
        assert out == "", f"{key}={value}"


def test_p4p_15char_password_inside_dsn_line_blocks(capsys):
    prompt = ("host=db.internal " + "pass" + "word="
              + "Ab3dEf6h" + "Ij9KlMn" + " dbname=app user=svc")
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo("Ab3dEf6h" + "Ij9KlMn", out, err)


def test_p4p_password_variable_in_front_of_psql_blocks(capsys):
    for prompt in (
        "PGPASS" + "WORD=" + "Xy9!" + "abcZ" + " psql -h db.internal -U svc app",
        "pgpass" + "word=" + "p@ss" + "w0rdX" + " psql -h db.internal -U svc app",
    ):
        rc, out, err = _run(_harness(prompt), capsys)
        assert rc == 0
        assert json.loads(out)["decision"] == "block", prompt[:30]


def test_p4p_15char_bearer_token_blocks(capsys):
    secret = "Ab1Cd2" + "Ef3Gh4Ij5"
    assert len(secret) == 15
    rc, out, err = _run(
        _harness(f"Authorization: Bearer {secret}"), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo(secret, out, err)


@pytest.mark.parametrize("key", [
    "aws_secret_access_key", "aws_session_token",
    "db_password", "private_key", "my_token",
])
def test_p4p_assignment_layer_keys_block_on_redact_matching_value(capsys, key):
    secret = _high_entropy_value()
    rc, out, err = _run(_harness(f"{key}={secret}"), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", key
    _assert_no_secret_echo(secret, out, err)


# --- (n) S (#5670): chunked scanning replaces the 16 KB cap ------------------

_LOG_LINE = "2026-01-01 INFO worker processed request ok status=200 duration_ms=12\n"


def _log_prompt(size: int, where: str) -> str:
    secret = "api_" + "key=" + "Abc" + "123xy"
    base = _LOG_LINE * ((size // len(_LOG_LINE)) + 2)
    if where == "start":
        return (secret + " " + base)[:size]
    if where == "end":
        return (base[:size - len(secret) - 1] + " " + secret)[:size]
    mid = size // 2
    return (base[:mid] + " " + secret + " " + base[mid:])[:size]


@pytest.mark.parametrize("size", [16_385, 100_000, 1_000_000])
@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_s_large_log_prompt_blocks_at_any_position(capsys, size, where):
    prompt = _log_prompt(size, where)
    assert ("api_" + "key=" + "Abc" + "123xy") in prompt
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block", (size, where)


@pytest.mark.parametrize("filler_pairs,key_at", [(1020, 2040), (1022, 2044)])
def test_s_key_straddling_chunk_boundary_on_long_line_blocks(
        capsys, filler_pairs, key_at):
    line = "a " * filler_pairs + "api_" + "key=" + "Abc" + "123xy" + " b" * 1500
    assert line.index("api_key") == key_at
    rc, out, err = _run(_harness(line), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    _assert_no_secret_echo("Abc" + "123xy", out, err)


def test_s_chunks_never_exceed_limit_and_pem_stays_whole():
    pem = ("-----BEGIN " + "PRIVATE KEY-----\n" + "A" * 3000
           + "\n-----END " + "PRIVATE KEY-----\n")
    text = "log line ok\n" * 300 + pem + "tail line\n" * 10
    chunks = list(secret_guard._iter_chunks(text))
    pem_chunks = [c for c in chunks if "-----" + "BEGIN" in c]
    assert len(pem_chunks) == 1
    assert "-----END " + "PRIVATE KEY-----" in pem_chunks[0]
    for chunk in chunks:
        if "-----" + "BEGIN" not in chunk:
            assert len(chunk) <= secret_guard._CHUNK_CHARS
    plain = list(secret_guard._iter_chunks("x" * 10_000))
    assert all(len(c) <= secret_guard._CHUNK_CHARS for c in plain)


def test_s_pem_block_inside_large_prompt_blocks(capsys):
    prompt = (_LOG_LINE * 500) + _pem_block() + "\n" + (_LOG_LINE * 500)
    rc, out, err = _run(_harness(prompt), capsys)
    assert rc == 0
    assert json.loads(out)["decision"] == "block"


def test_s_budget_expiry_fails_open_with_system_message(capsys, monkeypatch):
    monkeypatch.setattr(secret_guard, "_SCAN_BUDGET_SECONDS", -1.0)
    rc, out, err = _run(
        _harness("ordinary prompt with no secret material at all"), capsys)
    assert rc == 0
    parsed = json.loads(out)
    assert "not fully scanned" in parsed["systemMessage"]
    assert "decision" not in parsed


def test_s_no_silent_cap_constant_remains():
    assert not hasattr(secret_guard, "_MAX_REDACT_CHARS")


def test_cli_entrypoint_hook_reads_stdin(capsys, monkeypatch):
    secret = _sk_ant()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": f"use {secret}"})))
    rc = run_secret_guard(["hook"])
    cap = capsys.readouterr()
    assert rc == 0
    assert json.loads(cap.out)["decision"] == "block"
    _assert_no_secret_echo(secret, cap.out, cap.err)
