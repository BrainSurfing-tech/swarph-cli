"""Provider keys are redacted even when glued to preceding text."""

import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.redact import redact_text


@pytest.mark.parametrize(
    ("prefix", "body", "lead", "label"),
    [
        ("github_pat_", "m" * 24, "field", "GITHUB_TOKEN"),
        ("ghp_", "m" * 24, "field7", "GITHUB_TOKEN"),
        ("sk-ant-api03-", "m" * 16, "field", "ANTHROPIC_KEY"),
        ("sk-ant-", "m" * 8, "field_", "ANTHROPIC_KEY"),
        ("sk-proj-", "m" * 16, "field", "API_KEY"),
        ("sk-svcacct-", "m" * 16, "field", "API_KEY"),
        ("sk-", "m" * 20, "field_", "API_KEY"),
        ("xai-", "m" * 16, "field7", "API_KEY"),
        ("AI" + "za", "m" * 35, "field", "GCP_API_KEY"),
        ("xoxb-", "m" * 8, "field_", "SLACK_TOKEN"),
        ("AK" + "IA", "M" * 16, "field7", "AWS_KEY"),
        ("AS" + "IA", "M" * 16, "field_", "AWS_KEY"),
    ],
)
def test_glued_provider_key_is_redacted_without_eating_the_lead(prefix, body, lead, label):
    assert redact_text(lead + prefix + body + " end") == f"{lead}[REDACTED:{label}] end"


# Runtime-generated lines from the seed-6018 regression replay, not live keys.
@pytest.mark.parametrize(
    "line",
    [
        ('9sk' '-ant-api03-ehADQJ6dKLwSa1OfRHqq9pS5Xbn9SfEF9mpMk6sm--postgres:' '//user:onvGdRd33hQO377o@db.example.com9sk' '-ant-api03-vnQOWmMRTcDQbp8gL1VCeCZLK1wRnOCs5mpqlUVJ\t\n'),
        ('_sk' '-ant-api03-IVpj63sgo7ARovxw-5sb1HlAgjKQ0l7mA93mxAXfGHTiiZocla7RO4CWoIBfmwRBc8rstdxmBzx5PfMF_GHH8stJM2HIMAA-postgres:' '//user:MAEIV3iFsAT1iTnE@db.example.com=\n'),
        ('9sk' '-ant-sid01-jIirvHx13-jcu-oKGBegdGm1W6JpD34z7VLk4p1XcTqpIVUxInDEtZpLVsfjAAxpostgres:' '//user:1RJ3qwQ5PQCjCZdc@db.example.com)sk' '-ant-tnWPG5kmpnBhmWrMfEtQkYML0t9mRZgL|\n'),
        ('_sk' '-ant-admin01-6lhZfB7qu0KanJDW4OINIhZekGWGsInw8MCdF9T9XCOhz26xV4oxsA6n8L8CAA--postgres:' '//user:w8db5KiN41ntJ2eh@db.example.com9postgres:' '//user:MF4H7ja1dUYh1tCm@db.example.com\t\n'),
    ],
    ids=["api03-dash", "api03-underscore", "sid01", "admin01"],
)
def test_glued_provider_key_does_not_hide_adjacent_uri_password(line):
    password = line.split("://user:", 1)[1].split("@", 1)[0]
    out = redact_text(line)
    assert password not in out
    assert "[REDACTED:ANTHROPIC_KEY]" in out


@pytest.mark.parametrize("marker", ["gh" + "p_", "github" + "_pat_", "gh" + "p_7"])
def test_repeated_github_markers_with_no_word_end_finish(marker):
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from swarph_cli.redact import redact_text; "
            "marker = sys.argv[1]; text = marker * (1_000_000 // len(marker)) + 'é'; "
            "assert redact_text(text) == text",
            marker,
        ],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=5,
        check=True,
    )
