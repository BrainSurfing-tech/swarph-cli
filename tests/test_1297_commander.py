"""card #1297 — `swarph commander ask` and `swarph commander request`.

On current main the verb is unknown, so these assert exit 0 and fail there.
"""
from swarph_cli.main import main

HEAD = "a" * 40


def _patch(monkeypatch):
    posted = {}

    def fake_post(url, body, token, timeout=10.0):
        posted["url"] = url
        posted["body"] = body
        posted["token"] = token
        return 201, {"id": 7, "status": "pending"}

    monkeypatch.setattr("swarph_cli.commands.mesh._post_json", fake_post)
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "tok")
    return posted


def test_commander_ask_posts_a_decision(monkeypatch):
    posted = _patch(monkeypatch)
    rc = main(["commander", "ask", "--question", "ship it?", "--options", "yes,no",
               "--card", "1297", "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin"])
    assert rc == 0
    assert posted["url"].endswith("/commander/requests")
    assert posted["body"] == {
        "type": "decision",
        "question": "ship it?",
        "options": ["yes", "no"],
        "card_id": 1297,
    }


def test_commander_request_posts_merge_and_release(monkeypatch):
    posted = _patch(monkeypatch)
    rc = main(["commander", "request", "merge_pr",
               "--repo", "BrainSurfing-tech/swarph-cli", "--pr", "12",
               "--head", HEAD, "--card", "1297",
               "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin"])
    assert rc == 0
    assert posted["body"]["type"] == "merge_pr"
    assert posted["body"]["repo"] == "BrainSurfing-tech/swarph-cli"
    assert posted["body"]["pr"] == 12
    assert posted["body"]["head_sha"] == HEAD
    assert posted["body"]["card_id"] == 1297

    rc = main(["commander", "request", "release_finish",
               "--version", "0.9.0", "--pr", "12", "--head", HEAD,
               "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin"])
    assert rc == 0
    assert posted["body"] == {
        "type": "release_finish",
        "pr": 12,
        "head_sha": HEAD,
        "version": "0.9.0",
    }
