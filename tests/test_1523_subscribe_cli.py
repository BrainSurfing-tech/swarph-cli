"""Subscription requests through real main dispatch; only HTTP is replaced."""
import io
import json
import urllib.error
import urllib.request

import pytest

from swarph_cli.main import main


@pytest.fixture
def response_payload():
    return {"id": 17, "cell": "cli-test", "trigger": "card_stage", "target": "1523"}


@pytest.fixture
def requests(monkeypatch, response_payload):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    calls = []

    def urlopen(request, **kwargs):
        calls.append((request.get_method(), request.full_url,
                      json.loads(request.data) if request.data is not None else None,
                      request.get_header("Authorization")))
        response = io.BytesIO(json.dumps(response_payload).encode())
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def invoke(*arguments):
    return main(["subscribe", *arguments, "--as", "cli-test", "--json",
                 "--gateway", "https://gateway.invalid"])


def test_add_defaults_to_the_calling_cell(requests, capsys):
    assert invoke("add", "--trigger", "card_stage", "--target", "1523") == 0
    assert requests == [("POST", "https://gateway.invalid/subscriptions", {
        "actor": "cli-test", "cell": "cli-test", "trigger": "card_stage", "target": "1523",
    }, "Bearer test-token")]
    assert json.loads(capsys.readouterr().out)["id"] == 17


@pytest.mark.parametrize("trigger,target,expiry,expected", [
    ("obligation_state", "4747", ["--days", "30"], {"days": 30}),
    ("time", "13:45Z", ["--expires-at", "2026-11-16T13:45:00Z"],
     {"expires_at": "2026-11-16T13:45:00Z"}),
])
def test_add_forwards_named_cell_and_expiry(requests, trigger, target, expiry, expected):
    assert invoke("add", "--trigger", trigger, "--target", target,
                  "--cell", "another-cell", *expiry) == 0
    assert requests == [("POST", "https://gateway.invalid/subscriptions", {
        "actor": "cli-test", "cell": "another-cell", "trigger": trigger, "target": target,
        **expected,
    }, "Bearer test-token")]


def test_expiry_forms_are_mutually_exclusive_before_http(requests):
    with pytest.raises(SystemExit) as error:
        invoke("add", "--trigger", "card_stage", "--target", "1523",
               "--days", "7", "--expires-at", "2026-11-16T00:00:00Z")
    assert error.value.code == 2 and requests == []


@pytest.mark.parametrize("flags,suffix", [
    ([], "?actor=cli-test"),
    (["--cell", "another-cell"], "?actor=cli-test&cell=another-cell"),
])
def test_list_preserves_actor_and_optional_cell_filter(requests, response_payload, capsys, flags, suffix):
    response_payload.clear()
    response_payload.update({"subscriptions": [{"id": 17, "cell": "cli-test"}]})
    assert invoke("list", *flags) == 0
    assert requests == [("GET", "https://gateway.invalid/subscriptions" + suffix, None,
                         "Bearer test-token")]
    assert json.loads(capsys.readouterr().out) == response_payload


def test_cancel_sends_the_bound_actor(requests, capsys):
    assert invoke("cancel", "17") == 0
    assert requests == [("POST", "https://gateway.invalid/subscriptions/17/cancel",
                         {"actor": "cli-test"}, "Bearer test-token")]
    assert json.loads(capsys.readouterr().out)["id"] == 17


@pytest.mark.parametrize("arguments,status,detail", [
    (["add", "--trigger", "card_stage", "--target", "1523", "--cell", "another-cell"],
     403, "a peer may subscribe only itself"),
    (["add", "--trigger", "time", "--target", "13:45Z", "--cell", "group:team"],
     400, "group targets are refused"),
    (["list", "--cell", "another-cell"], 403, "a peer may list only itself"),
    (["cancel", "17"], 403, "not your subscription"),
])
def test_gateway_refusals_are_not_reported_as_success(requests, monkeypatch, capsys,
                                                    arguments, status, detail):
    def denied(request, **kwargs):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "refused", {},
                                     io.BytesIO(json.dumps({"detail": detail}).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    assert invoke(*arguments) == 1
    output = capsys.readouterr()
    assert output.out == "" and detail in output.err
    assert len(requests) == 1


def test_explicit_empty_cell_is_not_replaced_by_an_implicit_recipient(requests, monkeypatch):
    def denied(request, **kwargs):
        requests.append(json.loads(request.data))
        raise urllib.error.HTTPError(request.full_url, 400, "refused", {},
                                     io.BytesIO(b'{"detail":"cell must name one peer"}'))

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    assert invoke("add", "--trigger", "card_stage", "--target", "1523", "--cell", "") == 1
    assert requests[0]["cell"] == ""


def test_connection_failure_is_reported_without_a_retry(requests, monkeypatch, capsys):
    def unavailable(request, **kwargs):
        requests.append(request)
        raise urllib.error.URLError("offline fixture")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    assert invoke("cancel", "17") == 1
    output = capsys.readouterr()
    assert output.out == "" and "unreachable" in output.err and "offline fixture" in output.err
    assert len(requests) == 1


@pytest.mark.parametrize("arguments", [
    ["add", "--trigger", "ci_status", "--target", "31"],
    ["add", "--trigger", "card_stage"],
    ["add", "--trigger", "time", "--target", "13:45Z", "--days", "not-a-number"],
    ["cancel", "not-an-id"],
])
def test_invalid_commands_are_refused_before_http(requests, arguments):
    with pytest.raises(SystemExit) as error:
        invoke(*arguments)
    assert error.value.code == 2 and requests == []


def test_missing_identity_is_refused_before_http(requests, monkeypatch, capsys):
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    assert main(["subscribe", "list", "--gateway", "https://gateway.invalid"]) == 1
    assert "--as" in capsys.readouterr().err and requests == []


def test_explicit_token_file_and_gateway_are_used(requests, tmp_path, capsys):
    token_file = tmp_path / "fixture.token"
    token_file.write_text("fixture-token\n")
    assert main(["subscribe", "cancel", "17", "--as", "cli-test", "--json",
                 "--gateway", "https://other.invalid/", "--token-file", str(token_file)]) == 0
    assert requests == [("POST", "https://other.invalid/subscriptions/17/cancel",
                         {"actor": "cli-test"}, "Bearer fixture-token")]
    assert json.loads(capsys.readouterr().out)["id"] == 17


def test_human_list_prints_target_cell_and_expiry_without_terminal_controls(requests, response_payload, capsys):
    response_payload.clear()
    response_payload.update({"subscriptions": [{
        "id": 17, "cell": "cli-test", "trigger": "card_stage", "target": "1523\x1b[2J",
        "expires_at": "2026-11-16T00:00:00Z",
    }]})
    assert main(["subscribe", "list", "--as", "cli-test", "--gateway", "https://gateway.invalid"]) == 0
    output = capsys.readouterr().out
    assert "#17: card_stage 1523" in output
    assert "cli-test" in output and "2026-11-16T00:00:00Z" in output and "\x1b" not in output


def test_human_list_names_cancelled_rows_in_the_audit_history(requests, response_payload, capsys):
    response_payload.clear()
    response_payload.update({"subscriptions": [{
        "id": 17, "cell": "cli-test", "trigger": "card_stage", "target": "1523",
        "expires_at": "2026-11-16T00:00:00Z", "cancelled_at": "2026-11-10T00:00:00Z",
    }]})
    assert main(["subscribe", "list", "--as", "cli-test", "--gateway", "https://gateway.invalid"]) == 0
    output = capsys.readouterr().out
    assert "cancelled=2026-11-10T00:00:00Z" in output and "2026-11-16T00:00:00Z" in output


@pytest.mark.parametrize("arguments,expected", [
    (["add", "--trigger", "card_stage", "--target", "1523"], "created subscription #17"),
    (["cancel", "17"], "cancelled subscription #17"),
])
def test_human_write_result_names_the_subscription(requests, capsys, arguments, expected):
    assert main(["subscribe", *arguments, "--as", "cli-test", "--gateway", "https://gateway.invalid"]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_subscribe_without_a_subcommand_prints_help_and_sends_nothing(requests, capsys):
    assert main(["subscribe"]) == 0
    assert "add,list,cancel" in capsys.readouterr().out and requests == []
