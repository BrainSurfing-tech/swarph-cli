"""Tests for the ``swarph lane`` verb.

The lane verb drives the gateway's $0-lane orchestration (list/create/scale/
delete/enqueue). The module binds the gateway-client helpers by name, so the
fakes are patched as attributes of the ``lane`` module.
"""

from __future__ import annotations

import json

import pytest

from swarph_cli.commands import lane as ln


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def _capture(monkeypatch, name, status=200, payload=None):
    """Patch ``ln.<name>`` with a fake capturing url/body, returning the spy."""
    seen: dict = {}

    def fake(url, *args, **kwargs):
        seen["url"] = url
        # post_json(url, body, token); get/delete_json(url, token)
        if args and isinstance(args[0], dict):
            seen["body"] = args[0]
        return status, (payload if payload is not None else {})

    monkeypatch.setattr(ln, name, fake)
    return seen


def test_lane_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["lane"] == "swarph_cli.commands.lane.run_lane"


def test_lane_no_subcommand_prints_help():
    assert ln.run_lane([]) == 0


def test_lane_list_gets_lanes(monkeypatch):
    seen = _capture(monkeypatch, "get_json", payload={"lanes": []})
    rc = ln.run_lane(["list"])
    assert rc == 0
    assert seen["url"].endswith("/lanes")


def test_lane_create_posts_body(monkeypatch):
    calls = _posts(monkeypatch, (
        (200, {"name": "fast", "n": 0}),
        (200, {"name": "fast", "n": 3}),
    ))
    rc = ln.run_lane([
        "create", "fast",
        "--provider", "claude",
        "--model", "opus",
        "--n", "3",
        "--context-file", "a.txt",
        "--context-file", "b.txt",
    ])
    assert rc == 0
    assert calls[0][0].endswith("/lanes")
    body = calls[0][1]
    assert body["name"] == "fast"
    assert body["provider"] == "claude"
    assert body["model"] == "opus"
    assert body["n"] == 3
    assert body["context_files"] == ["a.txt", "b.txt"]
    assert calls[1][0].endswith("/lanes/fast/scale")
    assert calls[1][1] == {"n": 3}


def test_lane_scale_posts_n(monkeypatch):
    seen = _capture(monkeypatch, "post_json", payload={"ok": True})
    rc = ln.run_lane(["scale", "fast", "--n", "5"])
    assert rc == 0
    assert seen["url"].endswith("/lanes/fast/scale")
    assert seen["body"] == {"n": 5}


def test_lane_delete_calls_delete_json(monkeypatch):
    seen = _capture(monkeypatch, "delete_json", payload={"ok": True})
    rc = ln.run_lane(["delete", "fast"])
    assert rc == 0
    assert seen["url"].endswith("/lanes/fast")


def test_lane_enqueue_posts_prompt(monkeypatch):
    seen = _capture(monkeypatch, "post_json", payload={"job_id": "j1"})
    rc = ln.run_lane(["enqueue", "fast", "--prompt", "hello"])
    assert rc == 0
    assert seen["url"].endswith("/lanes/fast/enqueue")
    assert seen["body"]["prompt"] == "hello"


def test_lane_non_2xx_returns_1(monkeypatch, capsys):
    _capture(monkeypatch, "get_json", status=403, payload={"detail": "operator only"})
    rc = ln.run_lane(["list"])
    assert rc == 1
    assert "operator only" in capsys.readouterr().err


def _posts(monkeypatch, replies):
    """post_json replies in order. Each reply is (status, payload)."""
    calls = []

    def fake(url, *args, **kwargs):
        body = args[0] if args and isinstance(args[0], dict) else None
        calls.append((url, body))
        status, payload = replies[len(calls) - 1]
        return status, payload

    monkeypatch.setattr(ln, "post_json", fake)
    return calls


def test_create_with_n_scales_and_reports_both(monkeypatch, capsys):
    """Card #949. --n on create must bring the workers up and say so.

    The gateway persists the row at n=0 and only scale starts units. A create
    that posts n and prints "created" leaves the operator believing the
    workers exist. This fails while create makes one POST and never scales.
    """
    calls = _posts(monkeypatch, (
        (200, {"name": "fast", "n": 0}),
        (200, {"name": "fast", "n": 2}),
    ))
    rc = ln.run_lane([
        "create", "fast", "--provider", "claude", "--model", "opus", "--n", "2",
    ])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0].endswith("/lanes")
    assert calls[0][1]["n"] == 2
    assert calls[1][0].endswith("/lanes/fast/scale")
    assert calls[1][1] == {"n": 2}
    out = capsys.readouterr().out
    assert "created lane fast" in out
    assert "scaled fast -> 2" in out


def test_create_without_n_says_to_scale(monkeypatch, capsys):
    calls = _posts(monkeypatch, ((200, {"name": "fast", "n": 0}),))
    rc = ln.run_lane(["create", "fast", "--provider", "claude", "--model", "opus"])
    assert rc == 0
    assert len(calls) == 1
    assert "n=0; run lane scale" in capsys.readouterr().out


def test_create_failure_does_not_scale(monkeypatch, capsys):
    calls = _posts(monkeypatch, ((403, {"detail": "operator only"}),))
    rc = ln.run_lane([
        "create", "fast", "--provider", "claude", "--model", "opus", "--n", "2",
    ])
    assert rc == 1
    assert len(calls) == 1
    assert "operator only" in capsys.readouterr().err


def test_create_scale_failure_reports_the_create(monkeypatch, capsys):
    calls = _posts(monkeypatch, (
        (200, {"name": "fast", "n": 0}),
        (500, {"detail": "fleetctl rc=1"}),
    ))
    rc = ln.run_lane([
        "create", "fast", "--provider", "claude", "--model", "opus", "--n", "2",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert len(calls) == 2
    assert "created lane fast" in captured.out
    assert "fleetctl rc=1" in captured.err
