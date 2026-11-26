"""Synthetic examples for the glued-provider pass."""

import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.redact import redact_text


@pytest.mark.parametrize(
    ("lead", "prefix", "body", "marker"),
    [
        ("field", "github" + "_pat_", "m" * 24, "GITHUB_TOKEN"),
        ("field7", "gh" + "p_", "m" * 24, "GITHUB_TOKEN"),
        ("field", "sk-" + "ant-api03-", "m" * 16, "ANTHROPIC_KEY"),
        ("field_", "sk-" + "ant-", "m" * 8, "ANTHROPIC_KEY"),
        ("field", "sk-" + "proj-", "m" * 16, "API_KEY"),
        ("field", "sk-" + "svcacct-", "m" * 16, "API_KEY"),
        ("field_", "sk-", "m" * 20, "API_KEY"),
        ("field7", "x" + "ai-", "m" * 16, "API_KEY"),
        ("field", "AI" + "za", "m" * 35, "GCP_API_KEY"),
        ("field_", "xox" + "b-", "m" * 8, "SLACK_TOKEN"),
        ("field7", "AK" + "IA", "M" * 16, "AWS_KEY"),
        ("field_", "AS" + "IA", "M" * 16, "AWS_KEY"),
    ],
)
def test_glued_key_leaves_preceding_text(lead, prefix, body, marker):
    assert redact_text(lead + prefix + body + " end") == f"{lead}[REDACTED:{marker}] end"


def test_glued_key_does_not_preempt_password_assignment():
    key = "sk-" + "ant-api03-" + "m" * 16
    line = "7" + key + "-password=" + "Synthetic-Passphrase"
    out = redact_text(line)
    assert "[REDACTED:ANTHROPIC_KEY]" in out
    assert "Synthetic-Passphrase" not in out
    assert "=[REDACTED:DSN_CREDENTIAL]" in out


def test_glued_key_does_not_preempt_uri_credentials():
    key = "sk-" + "ant-api03-" + "m" * 16
    line = "7" + key + "-postgres://user:Synthetic-Passphrase@db.example.com"
    out = redact_text(line)
    assert "[REDACTED:ANTHROPIC_KEY]" in out
    assert "Synthetic-Passphrase" not in out
    assert "[REDACTED:URI_CREDENTIAL]@db.example.com" in out


def test_late_pass_does_not_edit_custom_marker():
    name = "V7" + "AK" + "IA" + "M" * 16
    assert redact_text("custom", custom_rules=[(name, "custom")]) == f"[REDACTED:CUSTOM:{name}]"


def test_late_pass_does_not_edit_existing_marker():
    text = "[REDACTED:field7" + "AK" + "IA" + "M" * 16 + "]"
    assert redact_text(text) == text


@pytest.mark.parametrize("lead", ["field", "value9"])
def test_bare_openai_prefix_inside_identifier_is_unchanged(lead):
    text = lead + "sk-" + "z" * 30
    assert redact_text(text) == text


def test_github_key_ending_inside_unicode_word_is_unchanged():
    text = "7" + "gh" + "p_" + "m" * 22 + "é"
    assert redact_text(text) == text


def test_blocked_word_end_does_not_retry_every_glued_start():
    snippet = (
        'from swarph_cli.redact import redact_text; '
        'text = ("q7" + "gh" + "p_") * 160_000 + "é"; '
        'assert redact_text(text) == text'
    )
    subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=10,
        check=True,
    )
