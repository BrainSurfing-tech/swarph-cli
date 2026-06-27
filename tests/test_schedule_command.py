"""Tests for the ``swarph schedule`` verb.

The schedule client just shapes requests against the gateway's scheduled-event
endpoints; the helpers are monkeypatched as attributes of the schedule module
(it binds them by name at import).
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import schedule as sc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def _capture_post(monkeypatch, status=200, payload=None):
    seen = {}

    def fake(url, body, token, **kwargs):
        seen["url"] = url
        seen["body"] = body
        seen["token"] = token
        return status, payload or {}

    monkeypatch.setattr(sc, "post_json", fake)
    return seen


def _capture_get(monkeypatch, status=200, payload=None):
    seen = {}

    def fake(url, token, **kwargs):
        seen["url"] = url
        seen["token"] = token
        return status, payload or {}

    monkeypatch.setattr(sc, "get_json", fake)
    return seen


def _capture_delete(monkeypatch, status=200, payload=None):
    seen = {}

    def fake(url, token, **kwargs):
        seen["url"] = url
        seen["token"] = token
        return status, payload or {}

    monkeypatch.setattr(sc, "delete_json", fake)
    return seen


def test_schedule_no_subcommand_prints_help():
    assert sc.run_schedule([]) == 0


def test_schedule_create_posts_event(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule([
        "create", "daily-digest",
        "--trigger", "time",
        "--target", "c2",
        "--task", "summarize the day",
        "--cron", "0 9 * * *",
        "--context", "ref1",
        "--context", "ref2",
        "--out-channel", "general",
        "--min-interval", "300",
    ])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events")
    body = seen["body"]
    assert body["name"] == "daily-digest"
    assert body["trigger_type"] == "time"
    assert body["target_cell"] == "c2"
    assert body["task"] == "summarize the day"
    assert body["created_by"] == "c1"
    assert body["context_ref"] == ["ref1", "ref2"]
    assert body["cron"] == "0 9 * * *"
    assert body["out_channel"] == "general"
    assert body["min_interval_sec"] == 300


def test_schedule_create_minimal_omits_optionals(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule([
        "create", "ev",
        "--trigger", "event",
        "--target", "c2",
        "--task", "do thing",
    ])
    assert rc == 0
    body = seen["body"]
    assert body["context_ref"] == []
    assert "cron" not in body
    assert "out_channel" not in body
    assert "min_interval_sec" not in body


def test_schedule_enable_posts(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule(["enable", "daily-digest"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events/daily-digest/enable")
    assert seen["body"] == {}


def test_schedule_disable_posts(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule(["disable", "daily-digest"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events/daily-digest/disable")
    assert seen["body"] == {}


def test_schedule_delete_calls_delete_json(monkeypatch):
    seen = _capture_delete(monkeypatch)
    rc = sc.run_schedule(["delete", "daily-digest"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events/daily-digest")


def test_schedule_fire_now_posts(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule(["fire-now", "daily-digest"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events/daily-digest/fire-now")
    assert seen["body"] == {}


def test_schedule_list_gets(monkeypatch):
    seen = _capture_get(monkeypatch, payload={"events": []})
    rc = sc.run_schedule(["list"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events")


def test_schedule_get_gets_named(monkeypatch):
    seen = _capture_get(monkeypatch, payload={"name": "daily-digest"})
    rc = sc.run_schedule(["get", "daily-digest"])
    assert rc == 0
    assert seen["url"].endswith("/scheduled-events/daily-digest")


def test_schedule_create_name_url_encoded(monkeypatch):
    seen = _capture_post(monkeypatch)
    rc = sc.run_schedule([
        "enable", "weird name/slash",
    ])
    assert rc == 0
    assert "weird%20name%2Fslash" in seen["url"]


def test_schedule_non_2xx_returns_1(monkeypatch, capsys):
    _capture_post(monkeypatch, status=403, payload={"detail": "operator only"})
    rc = sc.run_schedule(["enable", "daily-digest"])
    assert rc == 1
    assert "operator only" in capsys.readouterr().err
