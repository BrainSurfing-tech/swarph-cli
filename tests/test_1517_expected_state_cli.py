"""Optional expected-state flags reach the gateway unchanged via real CLI dispatch."""
import io
import json
import urllib.request

import pytest

from swarph_cli.main import main


@pytest.fixture
def cli_http(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.delenv("SWARPH_USAGE_TAKEN_AT", raising=False)
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    requests = []

    def urlopen(request, **kwargs):
        requests.append((request.get_method(), request.full_url,
                         json.loads(request.data) if request.data is not None else None))
        response = io.BytesIO(b'{"id": 1517}')
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def run(argv):
        assert main(["board", *argv, "--as", "cli-test", "--json",
                     "--gateway", "https://gateway.invalid"]) == 0
        return requests

    return run


@pytest.mark.parametrize("flags,body", [
    ([], {}),
    (["--expect-status", "open"], {"expect_status": "open"}),
])
def test_take_sends_optional_expected_status(cli_http, flags, body):
    assert cli_http(["obligations", "take", "1517", *flags]) == [
        ("POST", "https://gateway.invalid/board/obligations/1517/take", body)]


@pytest.mark.parametrize("verb,arguments,method,body", [
    ("decline", ["--why", "other work"], "POST", {"why": "other work"}),
    ("withdraw", ["--reason", "superseded"], "POST", {"reason": "superseded"}),
    ("release", ["--reason", "reassign"], "POST", {"reason": "reassign"}),
    ("amend", ["--holder", ""], "PATCH", {"holder": ""}),
])
@pytest.mark.parametrize("guarded", [False, True])
def test_lifecycle_sends_optional_expected_status(cli_http, verb, arguments, method, body, guarded):
    flags = ["--expect-status", "open"] if guarded else []
    expected = {**body, "expect_status": "open"} if guarded else body
    assert cli_http(["obligations", verb, "1517", *arguments, *flags]) == [
        (method, f"https://gateway.invalid/board/obligations/1517/{verb}", expected)]


@pytest.mark.parametrize("flags,expected", [
    ([], {}),
    (["--expect-status", "open"], {"expect_status": "open"}),
    (["--expect-accept-sha", "a" * 64], {"expect_accept_sha256": "a" * 64}),
    (["--expect-status", "open", "--expect-accept-sha", "b" * 64],
     {"expect_status": "open", "expect_accept_sha256": "b" * 64}),
])
def test_close_sends_independent_optional_guards(cli_http, flags, expected):
    body = {
        "outcome": "pass", "evidence_json": {"c1": "check passed"},
        "usage": {"agent": "cli-test", "model": "unknown", "input_tokens": "unknown",
                  "output_tokens": "unknown", "cache_read_tokens": "unknown",
                  "cache_creation_1h_tokens": "unknown", "cache_creation_5m_tokens": "unknown",
                  "cache_creation_unsplit_tokens": "unknown", "thinking": "unknown", "wall_s": "unknown"},
        **expected,
    }
    assert cli_http(["obligations", "close", "1517", "--outcome", "pass",
                     "--evidence-json", '{"c1": "check passed"}', *flags]) == [
        ("GET", "https://gateway.invalid/board/obligations?obligation_id=1517", None),
        ("POST", "https://gateway.invalid/board/obligations/1517/close", body)]


@pytest.mark.parametrize("arguments,body,reads_card", [
    (["move", "1517", "build"], {"actor": "cli-test", "stage": "build"}, False),
    (["assign", "1517", "builder"], {"actor": "cli-test", "assignee": "builder"}, False),
    (["ready", "1517"], {"actor": "cli-test", "move_ready": True}, False),
    (["ready", "1517", "--clear"], {"actor": "cli-test", "move_ready": False}, False),
    (["link", "1517", "pr", "pull/17"], {"actor": "cli-test", "links": {"pr": "pull/17"}}, False),
    (["link", "1517", "pr", "pull/17", "--replace"],
     {"actor": "cli-test", "links": {"pr": "pull/17"}, "replace_links": True}, False),
    (["label", "add", "1517", "bug"], {"actor": "cli-test", "labels": ["bug"]}, True),
    (["label", "rm", "1517", "bug"], {"actor": "cli-test", "labels": []}, True),
    (["edit", "1517", "--title", "New title", "--body", "New body", "--due", "2026-11-10",
      "--project", "7", "--priority", "4"],
     {"actor": "cli-test", "title": "New title", "body": "New body", "due_at": "2026-11-10T00:00:00",
      "project_id": 7, "priority": 4}, True),
])
@pytest.mark.parametrize("guarded", [False, True])
def test_card_patch_sends_optional_expected_stage(cli_http, arguments, body, reads_card, guarded):
    flags = ["--expect-stage", "plan"] if guarded else []
    expected = {**body, "expect_stage": "plan"} if guarded else body
    requests = [("GET", "https://gateway.invalid/board/cards/1517", None)] if reads_card else []
    requests.append(("PATCH", "https://gateway.invalid/board/cards/1517", expected))
    assert cli_http(["cards", *arguments, *flags]) == requests


@pytest.mark.parametrize("arguments,route,body", [
    (["ask", "1517", "Ship it", "--holder", "me", "--step", "build"], "ask",
     {"what": "Ship it", "created_by": "cli-test", "kind": "action", "holder": "cli-test", "step": "build"}),
    (["confirm-flow", "1517", "--holder", "build=builder", "--what", "Ship it"], "flow/confirm",
     {"actor": "cli-test", "holders": {"build": "builder"}, "what": "Ship it"}),
])
@pytest.mark.parametrize("guarded", [False, True])
def test_card_workflow_sends_optional_expected_stage(cli_http, arguments, route, body, guarded):
    flags = ["--expect-stage", "plan"] if guarded else []
    expected = {**body, "expect_stage": "plan"} if guarded else body
    assert cli_http(["cards", *arguments, *flags]) == [
        ("POST", f"https://gateway.invalid/board/cards/1517/{route}", expected)]
