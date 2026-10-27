"""Tests for Card #1321 (Obligation #3073): One layered redaction module in swarph-cli.

Verifies:
1. Corpus Secret Masking (all categories replaced with typed [REDACTED:<TYPE>] markers).
2. Outgoing CLI write paths (timeline, memory emit/sync, board cards/obligations, mesh DMs, channel/event).
3. False-positive budget (0.0% / zero false positives on prose and code corpus).
4. Opt-out logging (in-memory events, stderr audit lines, SWARPH_REDACT_AUDIT_LOG file).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from swarph_cli.commands.board import (
    _ask_payload,
    _card_add_payload,
    _card_edit_payload,
    _card_say_payload,
    _close_payload,
    _confirm_flow_payload,
    _write_card_preimage,
    run_board,
)
from swarph_cli.commands.channel import _post_payload, run_channel
from swarph_cli.commands.event import _post_channel, run_event
from swarph_cli.commands.highlight import (
    _format_line,
    _log_via_gateway,
    run_highlight,
)
from swarph_cli.commands.memory_emit_hook import _snippet, run_memory_emit_hook
from swarph_cli.commands.memory_sync import run_memory_sync
from swarph_cli.commands.mesh import run_mesh
from swarph_cli.redact import (
    FALSE_POSITIVE_BUDGET,
    clear_opt_out_events,
    get_opt_out_events,
    redact_text,
    redact_value,
    shannon_entropy,
)


# ── Runtime dynamic secret constructors (Gitleaks safe) ────────────────────────

def _ghp_token() -> str:
    return "gh" + "p_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _github_pat() -> str:
    return "github" + "_pat_11AAAAAAA0123456789abcdefABCDEF0123456789abcdefABCDEF0123456789"


def _anthropic_key() -> str:
    return "sk" + "-ant-api03-A1b2C3d4E5f6G7h8I9j0"


def _openai_key() -> str:
    return "sk" + "-proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _xai_key() -> str:
    return "xai" + "-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _gcp_key() -> str:
    return "AI" + "zaSyDdI0T9mbB4y7KzQ3wX2vU5tS8rR1pP6oO"


def _slack_token() -> str:
    return "xox" + "b-123456789012-abcdefghijklmnop"


def _aws_key() -> str:
    return "AK" + "IAIOSFODNN7EXAMPLE"


def _aws_secret_assignment() -> str:
    return "aws_secret_access_key" + "=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _jwt_token() -> str:
    return "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _bearer_token() -> str:
    return "Bearer " + "abcDEF123-456_789.~+/=="


def _pem_key() -> str:
    return "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890\n-----END " + "RSA PRIVATE KEY-----"


def _postgres_uri() -> str:
    return "postgres" + "://admin:s3cr3tPass@db.internal:5432/prod"


def _schemeless_uri() -> str:
    return "alice" + ":hunter2@example.com:8080"


def _dsn_password() -> str:
    return "password" + "=superSecret123"


def _dsn_pwd() -> str:
    return "pwd" + "=my_db_pass"


def _dsn_gateway_token() -> str:
    return "MESH_GATEWAY_TOKEN" + "=tok_abc123"


def _high_entropy_token() -> str:
    return "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z"


# ── Section 1: Corpus Secret Masking Tests ────────────────────────────────────

def test_github_tokens_masked():
    ghp = _ghp_token()
    out = redact_text(f"use token {ghp} for api")
    assert ghp not in out
    assert "[REDACTED:GITHUB_TOKEN]" in out
    assert out == "use token [REDACTED:GITHUB_TOKEN] for api"

    pat = _github_pat()
    out_pat = redact_text(f"token={pat}")
    assert pat not in out_pat
    assert "[REDACTED:GITHUB_TOKEN]" in out_pat


def test_anthropic_key_masked():
    key = _anthropic_key()
    out = redact_text(f"ANTHROPIC_KEY={key}")
    assert key not in out
    assert "[REDACTED:ANTHROPIC_KEY]" in out


def test_openai_xai_gcp_slack_masked():
    oai = _openai_key()
    out_oai = redact_text(f"key is {oai}")
    assert oai not in out_oai
    assert "[REDACTED:API_KEY]" in out_oai

    xai = _xai_key()
    out_xai = redact_text(f"xai key: {xai}")
    assert xai not in out_xai
    assert "[REDACTED:API_KEY]" in out_xai

    gcp = _gcp_key()
    out_gcp = redact_text(f"gcp api key {gcp} end")
    assert gcp not in out_gcp
    assert "[REDACTED:GCP_API_KEY]" in out_gcp

    slack = _slack_token()
    out_slack = redact_text(f"slack token: {slack}")
    assert slack not in out_slack
    assert "[REDACTED:SLACK_TOKEN]" in out_slack


def test_aws_keys_masked():
    akia = _aws_key()
    out_akia = redact_text(f"AWS key id {akia} active")
    assert akia not in out_akia
    assert "[REDACTED:AWS_KEY]" in out_akia

    secret_assign = _aws_secret_assignment()
    out_secret = redact_text(f"export {secret_assign}")
    assert "wJalrXUtn" not in out_secret
    assert "aws_secret_access_key=[REDACTED:AWS_SECRET]" in out_secret


def test_jwt_and_bearer_masked():
    jwt = _jwt_token()
    out_jwt = redact_text(f"auth jwt {jwt} passed")
    assert jwt not in out_jwt
    assert "[REDACTED:JWT]" in out_jwt

    bearer = _bearer_token()
    out_bearer = redact_text(f"header {bearer} sent")
    assert "abcDEF123" not in out_bearer
    assert "Bearer [REDACTED:BEARER_TOKEN]" in out_bearer


def test_pem_private_key_masked():
    pem = _pem_key()
    out_pem = redact_text(f"cert config\n{pem}\nend cert")
    assert "MIIEpAIBAAKCAQEA1234567890" not in out_pem
    assert "[REDACTED:PEM_PRIVATE_KEY]" in out_pem
    assert "cert config\n[REDACTED:PEM_PRIVATE_KEY]\nend cert" == out_pem


def test_uri_credentials_masked():
    pg = _postgres_uri()
    out_pg = redact_text(f"connect to {pg}")
    assert "s3cr3tPass" not in out_pg
    assert "[REDACTED:URI_CREDENTIAL]" in out_pg
    assert out_pg == "connect to postgres://admin:[REDACTED:URI_CREDENTIAL]@db.internal:5432/prod"

    schemeless = _schemeless_uri()
    out_schemeless = redact_text(f"remote host: {schemeless}")
    assert "hunter2" not in out_schemeless
    assert "[REDACTED:URI_CREDENTIAL]" in out_schemeless
    assert out_schemeless == "remote host: alice:[REDACTED:URI_CREDENTIAL]@example.com:8080"


def test_dsn_credentials_masked():
    p1 = _dsn_password()
    out1 = redact_text(f"db config {p1}")
    assert "superSecret123" not in out1
    assert "password=[REDACTED:DSN_CREDENTIAL]" in out1

    p2 = _dsn_pwd()
    out2 = redact_text(f"env {p2} set")
    assert "my_db_pass" not in out2
    assert "pwd=[REDACTED:DSN_CREDENTIAL]" in out2

    p3 = _dsn_gateway_token()
    out3 = redact_text(f"token {p3} exported")
    assert "tok_abc123" not in out3
    assert "MESH_GATEWAY_TOKEN=[REDACTED:DSN_CREDENTIAL]" in out3


@pytest.mark.parametrize(
    "cmd,expected_masked",
    [
        ("mysql -pX", "mysql -p[REDACTED:CLI_PASSWORD]"),
        ("mysql -u root -pX mydb", "mysql -u root -p[REDACTED:CLI_PASSWORD] mydb"),
        ("mysql --password=X", "mysql --password=[REDACTED:CLI_PASSWORD]"),
        ("docker login -u user -p X", "docker login -u user -p [REDACTED:CLI_PASSWORD]"),
        ("docker login -p X", "docker login -p [REDACTED:CLI_PASSWORD]"),
        ("sshpass -p X ssh user@host", "sshpass -p [REDACTED:CLI_PASSWORD] ssh user@host"),
        ("sshpass -pX", "sshpass -p[REDACTED:CLI_PASSWORD]"),
        (
            "curl -u " + "user" + ":" + "p4ssw0rd https://api.example.com",
            "curl -u user:[REDACTED:CLI_PASSWORD] https://api.example.com",
        ),
    ],
)
def test_low_entropy_cli_passwords_masked(cmd, expected_masked):
    out = redact_text(cmd)
    assert out == expected_masked


def test_high_entropy_token_masked():
    token = _high_entropy_token()
    out = redact_text(f"auth payload {token} ready")
    assert token not in out
    assert "[REDACTED:HIGH_ENTROPY]" in out
    assert out == "auth payload [REDACTED:HIGH_ENTROPY] ready"


def test_custom_rules_argument_and_env():
    # Via function argument
    rules = [("internal_ticket", r"TICKET-[0-9]{6}")]
    out = redact_text("Ticket TICKET-123456 resolved", custom_rules=rules)
    assert "TICKET-123456" not in out
    assert "[REDACTED:CUSTOM:INTERNAL_TICKET]" in out

    # Via environment variable
    os.environ["SWARPH_REDACT_CUSTOM_RULES"] = json.dumps(
        [{"name": "employee_id", "pattern": r"EMP[0-9]{5}"}]
    )
    try:
        out_env = redact_text("user EMP98765 verified")
        assert "EMP98765" not in out_env
        assert "[REDACTED:CUSTOM:EMPLOYEE_ID]" in out_env
    finally:
        os.environ.pop("SWARPH_REDACT_CUSTOM_RULES", None)


def test_redact_value_recursive():
    ghp = _ghp_token()
    data = {
        "title": f"Fix token {ghp}",
        "tags": ["ci", _dsn_password()],
        "details": {"key": _aws_key()},
    }
    redacted = redact_value(data)
    assert ghp not in redacted["title"]
    assert "[REDACTED:GITHUB_TOKEN]" in redacted["title"]
    assert "password=[REDACTED:DSN_CREDENTIAL]" in redacted["tags"]
    assert "[REDACTED:AWS_KEY]" == redacted["details"]["key"]


# ── Section 2: Outgoing CLI Write Paths Tests ─────────────────────────────────

def test_timeline_highlight_local_and_gateway(tmp_path):
    token = _ghp_token()
    timeline_repo = tmp_path / "timeline"

    # Local TIMELINE.md append
    rc = run_highlight([
        "--local",
        "--no-push",
        "--timeline-dir", str(timeline_repo),
        f"Shipped feature with {token}",
        f"[[note-{_dsn_password()}]]",
    ])
    assert rc == 0
    timeline_file = timeline_repo / "TIMELINE.md"
    assert timeline_file.exists()
    content = timeline_file.read_text(encoding="utf-8")
    assert token not in content
    assert "[REDACTED:GITHUB_TOKEN]" in content
    assert "password=[REDACTED:DSN_CREDENTIAL]" in content

    # Gateway POST
    with patch("swarph_cli.commands.highlight._post_json") as mock_post, \
         patch("swarph_cli.commands.highlight._resolve_token", return_value="fake-token"):
        mock_post.return_value = (200, {"ts": "2026-10-27T10:00:00Z", "pushed": True})
        rc_gw = _log_via_gateway(
            "http://fake-gw:8788",
            "test-cell",
            f"Gateway highlight {token}",
            f"[[{_dsn_password()}]]",
            "2026-10-27T10:00:00Z",
            None,
        )
        assert rc_gw == 0
        call_body = mock_post.call_args[0][1]
        assert token not in call_body["highlight"]
        assert "[REDACTED:GITHUB_TOKEN]" in call_body["highlight"]
        assert "password=[REDACTED:DSN_CREDENTIAL]" in call_body["memory"]


def test_memory_emit_hook_and_memory_sync(tmp_path, monkeypatch):
    token = _ghp_token()
    monkeypatch.setenv("SWARPH_GATEWAY", "http://fake-gw:8788")
    monkeypatch.setenv("SWARPH_EMIT_STATE", str(tmp_path / "emit-state.json"))

    # Memory emit hook
    mem_file = tmp_path / ".claude" / "projects" / "test" / "memory" / "note_slug.md"
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text(
        f"---\ndescription: Memory with {token}\n---\nBody with {_dsn_password()}\n",
        encoding="utf-8",
    )

    with patch("swarph_cli.commands.memory_emit_hook._log_via_gateway") as mock_log, \
         patch("swarph_cli.commands.memory_emit_hook._cell_with_source", return_value=("cell-a", "test")):
        mock_log.return_value = 0
        monkeypatch.setattr(
            "sys.stdin.read",
            lambda: json.dumps({"tool_input": {"file_path": str(mem_file)}}),
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        rc = run_memory_emit_hook([])
        assert rc == 0
        assert mock_log.called
        log_text = mock_log.call_args[0][2]
        assert token not in log_text
        assert "[REDACTED:GITHUB_TOKEN]" in log_text

    # Memory sync
    repo_dir = tmp_path / "memory_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir()

    cell_cwd = tmp_path / "cell_cwd"
    cell_cwd.mkdir(parents=True, exist_ok=True)
    current_task = cell_cwd / "CURRENT_TASK.md"
    current_task.write_text(f"Working with {token} and {_postgres_uri()}\n", encoding="utf-8")

    cell_yaml = tmp_path / "cell.yaml"
    cell_yaml.write_text(
        f"role: test-role\nprovider: test\nassisted_memory:\n  enabled: true\n  repo: local/test\n",
        encoding="utf-8",
    )

    with patch("swarph_cli.commands.memory_sync.get_memory_repo_path", return_value=repo_dir), \
         patch("swarph_cli.commands.memory_sync.load_cell") as mock_load, \
         patch("subprocess.run") as mock_subproc:
        mock_cell = MagicMock()
        mock_cell.role = "test-role"
        mock_cell.provider = "test"
        mock_cell.cwd = cell_cwd
        mock_cell.assisted_memory = {"enabled": True, "repo": "local/test"}
        mock_load.return_value = mock_cell
        mock_subproc.return_value = MagicMock(returncode=0)

        rc_sync = run_memory_sync([str(cell_yaml)])
        assert rc_sync == 0

        synced_task = repo_dir / "CURRENT_TASK.md"
        assert synced_task.exists()
        synced_content = synced_task.read_text(encoding="utf-8")
        assert token not in synced_content
        assert "[REDACTED:GITHUB_TOKEN]" in synced_content
        assert "[REDACTED:URI_CREDENTIAL]" in synced_content


def test_board_cards_and_obligations(tmp_path, monkeypatch):
    token = _ghp_token()
    monkeypatch.setenv("SWARPH_GATEWAY", "http://fake-gw:8788")

    with patch("swarph_cli.commands.board._resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.board._resolve_project", return_value=(1, None)), \
         patch("swarph_cli.commands.board._http_get_json") as mock_get, \
         patch("swarph_cli.commands.board._post_json") as mock_post, \
         patch("swarph_cli.commands.board._patch_json") as mock_patch:

        mock_post.return_value = (200, {"id": 1, "stage": "proposed"})
        mock_patch.return_value = (200, {"id": 1, "body_version": 2})

        # cards add
        run_board(["cards", "add", "--project", "1", "--title", f"Add {token}", "--body", f"Body {_dsn_password()}", "--as", "agent"])
        add_body = mock_post.call_args[0][1]
        assert token not in add_body["title"]
        assert "[REDACTED:GITHUB_TOKEN]" in add_body["title"]
        assert "password=[REDACTED:DSN_CREDENTIAL]" in add_body["body"]

        # cards edit
        mock_get.return_value = (200, {"id": 1, "title": "Old", "body": "Old", "body_version": 1})
        run_board(["cards", "edit", "1", "--title", f"Edit {token}", "--body", f"Body {_dsn_pwd()}", "--as", "agent"])
        edit_patch = mock_patch.call_args[0][1]
        assert token not in edit_patch["title"]
        assert "[REDACTED:GITHUB_TOKEN]" in edit_patch["title"]
        assert "pwd=[REDACTED:DSN_CREDENTIAL]" in edit_patch["body"]

        # cards say
        mock_get.return_value = (200, {"id": 1, "thread_uuid": "11111111-2222-3333-4444-555555555555", "assignee": "peer-b"})
        run_board(["cards", "say", "1", "--content", f"Say {token}", "--as", "agent"])
        say_body = mock_post.call_args[0][1]
        assert token not in say_body["content"]
        assert "[REDACTED:GITHUB_TOKEN]" in say_body["content"]

        # cards ask
        run_board([
            "cards", "ask", "1", f"What {token}",
            "--holder", "peer-b",
            "--done", f"Done {_dsn_password()}",
            "--accept", f"Accept {_postgres_uri()}",
            "--as", "agent",
        ])
        ask_body = mock_post.call_args[0][1]
        assert token not in ask_body["what"]
        assert "[REDACTED:GITHUB_TOKEN]" in ask_body["what"]
        assert "password=[REDACTED:DSN_CREDENTIAL]" in ask_body["done"]
        assert "[REDACTED:URI_CREDENTIAL]" in ask_body["accept"]

        # obligations close
        run_board([
            "obligations", "close", "10",
            "--outcome", "pass",
            "--evidence", f"Observed {token} verified",
            "--as", "agent",
        ])
        close_body = mock_post.call_args[0][1]
        assert token not in close_body["evidence"]
        assert "[REDACTED:GITHUB_TOKEN]" in close_body["evidence"]

        # obligations close with --evidence-json
        run_board([
            "obligations", "close", "11",
            "--outcome", "pass",
            "--evidence-json", json.dumps({"log": f"Log with {_dsn_password()}"}),
            "--as", "agent",
        ])
        close_json_body = mock_post.call_args[0][1]
        assert "password=[REDACTED:DSN_CREDENTIAL]" in close_json_body["evidence_json"]["log"]


def test_mesh_channel_and_event_posts(monkeypatch):
    token = _ghp_token()
    monkeypatch.setenv("SWARPH_GATEWAY", "http://fake-gw:8788")

    # mesh send
    with patch("swarph_cli.commands.mesh._resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.mesh._check_recipient", return_value=None), \
         patch("swarph_cli.commands.mesh._post_json") as mock_mesh_post:
        mock_mesh_post.return_value = (200, {"id": 1, "from_node": "agent", "to_node": "peer-b", "kind": "fyi"})
        run_mesh(["send", "peer-b", "--content", f"DM with {token}", "--kind", "fyi", "--as", "agent"])
        mesh_body = mock_mesh_post.call_args[0][1]
        assert token not in mesh_body["content"]
        assert "[REDACTED:GITHUB_TOKEN]" in mesh_body["content"]

    # mesh reply
    with patch("swarph_cli.commands.mesh._resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.mesh._find_inbox_message", return_value=({"from_node": "peer-b", "thread_id": "11111111-2222-3333-4444-555555555555"}, None)), \
         patch("swarph_cli.commands.mesh._post_json") as mock_mesh_post:
        mock_mesh_post.return_value = (200, {"id": 2, "from_node": "agent", "to_node": "peer-b", "kind": "fyi"})
        run_mesh(["reply", "123", "--content", f"Reply with {_postgres_uri()}", "--as", "agent"])
        reply_body = mock_mesh_post.call_args[0][1]
        assert "s3cr3tPass" not in reply_body["content"]
        assert "[REDACTED:URI_CREDENTIAL]" in reply_body["content"]

    # channel post
    with patch("swarph_cli.commands.channel.resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.channel.post_json") as mock_chan_post:
        mock_chan_post.return_value = (200, {"id": 3})
        run_channel(["post", "general", "--content", f"Channel msg with {token}", "--as", "agent"])
        chan_body = mock_chan_post.call_args[0][1]
        assert token not in chan_body["content"]
        assert "[REDACTED:GITHUB_TOKEN]" in chan_body["content"]

    # event emit
    with patch("swarph_cli.commands.event.resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.event.post_json") as mock_event_post:
        mock_event_post.return_value = (200, {"id": 4})
        run_event(["emit", "system_alert", f"Alert with {_dsn_password()}", "--channel", "events", "--as", "agent"])
        event_body = mock_event_post.call_args[0][1]
        content_dict = json.loads(event_body["content"])
        payload = content_dict["swarph_event"]["payload"]
        assert "password=[REDACTED:DSN_CREDENTIAL]" in payload


# ── Section 3: False-Positive Budget Test ─────────────────────────────────────

def test_false_positive_budget_on_corpus():
    assert FALSE_POSITIVE_BUDGET == 0.0

    prose_and_code_corpus = [
        # 1. 40-char git commit SHA
        "c2dc4067b8232e3f9b7d393707ddc34d259518b9",
        # 2. 64-char SHA-256 digest
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        # 3. Pull Request URL
        "https://github.com/BrainSurfing-tech/swarph-cli/pull/123",
        # 4. Agent attribution with UUID
        "agent=subagent/gemini-flash (MODEL_TIER_FLASH, conversationId=0b1e211a-4e73-4831-943e-9123ae133320)",
        # 5. Python function definition
        "def _poll_channel_subscriptions_inner(state: MonitorState) -> None:",
        # 6. CamelCase class identifier
        "class MeshGatewayReplyTransport:",
        # 7. Pytest invocation with snake_case test identifier
        "pytest -v tests/test_1314_go_monitor_phase2.py::test_bidirectional_cursor_handshake",
        # 8. MySQL command line without -p
        "mysql -u root -h localhost mydb",
        # 9. Docker login with password-stdin
        "cat token.txt | docker login --username user --password-stdin",
        # 10. Memory pointer markdown link
        "Refer to [[mesh-gateway-routing-decision]] for background context.",
        # 11. ISO 8601 UTC timestamp
        "Event recorded at 2026-10-27T10:29:00.454870+00:00 by daemon",
        # 12. Local absolute path
        "/home/ubuntu/gemini-researcher/src/swarph_cli/commands/board.py",
        # 13. Go function snippet
        "func (r *GatewayRouter) DispatchMessage(ctx context.Context, msg *Message) error {",
        # 14. TypeScript interface snippet
        "export interface ChannelSubscriptionOptions { autoAck?: boolean; timeoutMs?: number; }",
        # 15. UUID assignment in code
        "assigned_task_uuid = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'",
        # 16. Git commit message
        "feat(board): implement card flow confirmation (#801)",
        # 17. Structured JSON string
        '{"status": "ok", "obligations": [101, 102], "as_of": "2026-10-27T10:30:00Z"}',
        # 18. Natural English prose
        "The decentralized membrane coordinates between Claude and Gemini agents using HTTP gateways.",
        # 19. Public API curl call
        "curl -X POST https://api.swarph.network/v1/health -H 'Content-Type: application/json'",
        # 20. Python list comprehension
        "active_peers = [peer for peer in cluster.nodes if peer.is_alive()]",
        # 21. SQL query
        "SELECT id, title, stage FROM cards WHERE project_id = 42 ORDER BY id DESC LIMIT 10;",
        # 22. Docker run command
        "docker run -d --name mesh-runner -p 8788:8788 swarph/mesh-gateway:latest",
        # 23. Git checkout command
        "git checkout -b card-1321-layered-redaction origin/main",
        # 24. Bash shell loop
        'for file in src/swarph_cli/commands/*.py; do python3 -m py_compile "$file"; done',
        # 25. Board query URL with parameters
        "/board/cards?project=42&stage=in_progress&assignee=lab-ovh",
        # 26. Card obligation reference
        "PASS = obligation closed in c2dc4067b8232e3f9b7d393707ddc34d259518b9 with green pytest",
        # 27. Environment variable assignment
        "export SWARPH_TIMELINE_DIR=~/swarph-timeline",
        # 28. Markdown checklist
        "### Acceptance Criteria\n- Step 1: verify unit tests\n- Step 2: verify CI status",
        # 29. Long CamelCase identifier
        "ProcessExecutionResultOrTimeoutError",
        # 30. IP addresses (v4 and v6)
        "Connecting from 192.168.1.50 to fe80::1ff:fe23:4567:890a",
        # 31. Commit SHA range
        "git diff 989c83d..c2dc406 --stat",
        # 32. Docker compose specification
        "version: '3.8'\nservices:\n  gateway:\n    image: swarph/gateway:v1\n    restart: always",
        # 33. Python log statement
        "logger.info('Cell initialization completed successfully; waiting for events')",
        # 34. HTTP status and header
        "HTTP request returned status 200 with headers: X-Request-ID: 7b89f012-3456-7890",
    ]

    assert len(prose_and_code_corpus) >= 30

    false_positives = 0
    for sample in prose_and_code_corpus:
        redacted = redact_text(sample)
        if redacted != sample:
            false_positives += 1

    fp_rate = false_positives / len(prose_and_code_corpus)
    assert fp_rate <= FALSE_POSITIVE_BUDGET
    assert false_positives == 0


# ── Section 4: Opt-Out Logging Tests ──────────────────────────────────────────

def test_opt_out_logging_and_bypass(tmp_path, monkeypatch, capsys):
    token = _ghp_token()
    clear_opt_out_events()

    # 1. Direct opt_out=True
    raw = f"my secret token is {token}"
    result = redact_text(raw, opt_out=True, caller="test_caller", opt_out_reason="explicit_test")
    assert result == raw
    events = get_opt_out_events()
    assert len(events) == 1
    assert events[0]["caller"] == "test_caller"
    assert events[0]["reason"] == "explicit_test"
    assert events[0]["chars"] == len(raw)

    captured = capsys.readouterr()
    assert "[swarph-redact] OPT-OUT caller=test_caller reason=explicit_test" in captured.err

    # 2. SWARPH_REDACT_OPTOUT=1 environment variable
    monkeypatch.setenv("SWARPH_REDACT_OPTOUT", "1")
    result_env = redact_text(raw, caller="env_caller")
    assert result_env == raw
    events = get_opt_out_events()
    assert len(events) == 2
    assert events[1]["caller"] == "env_caller"

    # 3. SWARPH_REDACT_AUDIT_LOG file
    audit_file = tmp_path / "audit" / "redact_audit.jsonl"
    monkeypatch.setenv("SWARPH_REDACT_AUDIT_LOG", str(audit_file))
    redact_text(raw, opt_out=True, caller="audit_file_caller")
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["caller"] == "audit_file_caller"
    assert record["chars"] == len(raw)


def test_cli_no_redact_flag_logged(tmp_path, monkeypatch, capsys):
    token = _ghp_token()
    clear_opt_out_events()
    monkeypatch.setenv("SWARPH_GATEWAY", "http://fake-gw:8788")

    # CLI mesh send --no-redact
    with patch("swarph_cli.commands.mesh._resolve_token", return_value="fake-token"), \
         patch("swarph_cli.commands.mesh._check_recipient", return_value=None), \
         patch("swarph_cli.commands.mesh._post_json") as mock_mesh_post:
        mock_mesh_post.return_value = (200, {"id": 1, "from_node": "agent", "to_node": "peer-b", "kind": "fyi"})
        run_mesh(["send", "peer-b", "--content", f"Unredacted {token}", "--kind", "fyi", "--no-redact", "--as", "agent"])
        mesh_body = mock_mesh_post.call_args[0][1]
        assert token in mesh_body["content"]  # bypassed!
        events = get_opt_out_events()
        assert any(e["caller"] == "mesh.send" for e in events)

    # CLI highlight --no-redact
    timeline_repo = tmp_path / "timeline_optout"
    rc = run_highlight([
        "--local",
        "--no-push",
        "--no-redact",
        "--timeline-dir", str(timeline_repo),
        f"Highlight with {token}",
    ])
    assert rc == 0
    timeline_file = timeline_repo / "TIMELINE.md"
    content = timeline_file.read_text(encoding="utf-8")
    assert token in content  # bypassed!
    events = get_opt_out_events()
    assert any(e["caller"] == "highlight" for e in events)
