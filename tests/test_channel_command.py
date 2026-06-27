"""Tests for the ``swarph channel`` verb.

The channel module binds its gateway helpers by name (``from ... import
post_json``), so the fakes are patched as attributes of the channel module.
Identity/token resolve from env so no token files are touched.
"""

from __future__ import annotations

import json

import pytest

from swarph_cli.commands import channel as ch


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def test_channel_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["channel"] == "swarph_cli.commands.channel.run_channel"


def test_channel_no_subcommand_prints_help():
    assert ch.run_channel([]) == 0


def test_channel_create_posts_with_created_by(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body, token=token)
        return (200, {})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(["create", "ann", "--kind", "announce"])
    assert rc == 0
    assert cap["url"].endswith("/channels")
    assert cap["body"]["name"] == "ann"
    assert cap["body"]["kind"] == "announce"
    assert cap["body"]["created_by"] == "c1"
    assert cap["token"] == "tok"


def test_channel_create_optional_fields(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(body=body)
        return (201, {})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(
        ["create", "topic1", "--kind", "topic", "--visibility", "invite",
         "--description", "hi there"]
    )
    assert rc == 0
    assert cap["body"]["visibility"] == "invite"
    assert cap["body"]["description"] == "hi there"


def test_channel_join_posts_peer(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (200, {})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(["join", "ann", "--wake-policy", "mentions_only"])
    assert rc == 0
    assert cap["url"].endswith("/channels/ann/join")
    assert cap["body"]["peer"] == "c1"
    assert cap["body"]["wake_policy"] == "mentions_only"


def test_channel_leave_posts_peer(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (200, {})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(["leave", "ann"])
    assert rc == 0
    assert cap["url"].endswith("/channels/ann/leave")
    assert cap["body"]["peer"] == "c1"


def test_channel_list_does_get(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, {"channels": []})

    monkeypatch.setattr(ch, "get_json", fake)
    rc = ch.run_channel(["list"])
    assert rc == 0
    assert cap["url"].endswith("/channels")


def test_channel_members_does_get(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, {"members": []})

    monkeypatch.setattr(ch, "get_json", fake)
    rc = ch.run_channel(["members", "ann"])
    assert rc == 0
    assert cap["url"].endswith("/channels/ann/members")


def test_channel_create_url_encodes_name(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url)
        return (200, {})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(["join", "a/b c", "--wake-policy", "all"])
    assert rc == 0
    assert "a%2Fb%20c" in cap["url"]


def test_channel_non_2xx_returns_1(monkeypatch, capsys):
    def fake(url, body, token, **k):
        return (404, {"detail": "x"})

    monkeypatch.setattr(ch, "post_json", fake)
    rc = ch.run_channel(["create", "ann", "--kind", "announce"])
    assert rc == 1
    assert "404" in capsys.readouterr().err
