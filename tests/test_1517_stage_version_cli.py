"""Stage-history versions reach the gateway through real CLI dispatch."""
import urllib.request

import pytest

from swarph_cli.main import main
from test_1517_expected_state_cli import cli_http


def test_move_forwards_zero_version_without_requiring_a_stage_guard(cli_http):
    assert cli_http(["cards", "move", "1517", "ready", "--expect-stage-version", "0"]) == [
        ("PATCH", "https://gateway.invalid/board/cards/1517",
         {"actor": "cli-test", "stage": "ready", "expect_stage_version": 0})]


WRITES = [
    (["move", "1517", "ready"], "PATCH", "", {"actor": "cli-test", "stage": "ready"}, False),
    (["assign", "1517", "builder"], "PATCH", "", {"actor": "cli-test", "assignee": "builder"}, False),
    (["ready", "1517"], "PATCH", "", {"actor": "cli-test", "move_ready": True}, False),
    (["label", "add", "1517", "guarded"], "PATCH", "", {"actor": "cli-test", "labels": ["guarded"]}, True),
    (["link", "1517", "pr", "pull/17"], "PATCH", "", {"actor": "cli-test", "links": {"pr": "pull/17"}}, False),
    (["edit", "1517", "--title", "Guarded title"], "PATCH", "", {"actor": "cli-test", "title": "Guarded title"}, True),
    (["ask", "1517", "Ship it", "--holder", "me", "--step", "build"], "POST", "/ask",
     {"what": "Ship it", "created_by": "cli-test", "kind": "action", "holder": "cli-test", "step": "build"}, False),
    (["confirm-flow", "1517", "--holder", "build=builder", "--what", "Ship it"], "POST", "/flow/confirm",
     {"actor": "cli-test", "holders": {"build": "builder"}, "what": "Ship it"}, False),
]


@pytest.mark.parametrize("arguments,method,suffix,body,reads_card", WRITES)
@pytest.mark.parametrize("flags,guard", [
    ([], {}),
    (["--expect-stage-version", "0"], {"expect_stage_version": 0}),
    (["--expect-stage-version", "5"], {"expect_stage_version": 5}),
    (["--expect-stage", "ready", "--expect-stage-version", "5"],
     {"expect_stage": "ready", "expect_stage_version": 5}),
])
def test_all_card_write_commands_forward_the_stage_version(cli_http, arguments, method, suffix, body, reads_card,
                                                          flags, guard):
    expected = [("GET", "https://gateway.invalid/board/cards/1517", None)] if reads_card else []
    expected.append((method, "https://gateway.invalid/board/cards/1517" + suffix,
                     {**body, **guard}))
    assert cli_http(["cards", *arguments, *flags]) == expected


@pytest.mark.parametrize("arguments,method,suffix,body,reads_card", WRITES)
def test_negative_version_is_refused_before_any_card_read_or_write(cli_http, monkeypatch,
                                                                  arguments, method, suffix, body, reads_card):
    calls = []

    def forbidden_http(request, **kwargs):
        calls.append(request)
        raise AssertionError("invalid version reached HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_http)
    with pytest.raises(SystemExit) as error:
        cli_http(["cards", *arguments, "--expect-stage-version", "-1"])
    assert error.value.code == 2 and calls == []


@pytest.mark.parametrize("value", ["not-an-int", "1.5"])
def test_noninteger_version_is_a_usage_error(cli_http, value):
    with pytest.raises(SystemExit) as error:
        cli_http(["cards", "move", "1517", "ready", "--expect-stage-version", value])
    assert error.value.code == 2


@pytest.mark.parametrize("verb", ["take", "close", "decline", "amend", "withdraw", "release"])
def test_status_help_names_the_persisted_value_limitation(verb, capsys):
    with pytest.raises(SystemExit) as result:
        main(["board", "obligations", verb, "--help"])
    assert result.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "persisted" in help_text
    assert "does not distinguish offered from taken" in help_text
