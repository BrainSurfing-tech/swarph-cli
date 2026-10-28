"""card #1300 — swarph commander authorization check.

An active grant exits 0 and prints id, text and granted_at. A revoked
grant exits 1 and does not print the words. The same for an expired one.
"""
from swarph_cli.commands import commander as commander_mod
from swarph_cli.main import main


def _ready():
    assert hasattr(commander_mod, "fetch_scope")


def test_check_prints_an_active_grant_and_refuses_a_revoked_one(monkeypatch, capsys):
    _ready()
    seen = {}

    def fake(scope, got, gateway):
        seen["scope"] = scope
        seen["token"] = got
        seen["gateway"] = gateway
        return 200, {"authorizations": [{
            "id": 9,
            "status": "active",
            "text": "review-cell approval on release PRs",
            "granted_at": "2026-10-27T04:18:00+00:00",
        }]}

    monkeypatch.setattr(commander_mod, "fetch_scope", fake)
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "cell-token")
    code = main([
        "commander", "authorization", "check", "release_pr_approval",
        "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin",
    ])
    out = capsys.readouterr()
    assert code == 0
    assert "id=9" in out.out
    assert "text=review-cell approval on release PRs" in out.out
    assert "granted_at=2026-10-27T04:18:00+00:00" in out.out
    assert seen["scope"] == "release_pr_approval"
    assert seen["token"] == "cell-token"
    assert seen["gateway"] == "http://127.0.0.1:9"

    def revoked(scope, got, gateway):
        return 200, {"authorizations": [{
            "id": 9, "status": "revoked",
            "text": "review-cell approval on release PRs",
            "granted_at": "2026-10-27T04:18:00+00:00",
        }]}

    monkeypatch.setattr(commander_mod, "fetch_scope", revoked)
    code = main([
        "commander", "authorization", "check", "release_pr_approval",
        "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin",
    ])
    out = capsys.readouterr()
    assert code == 1
    assert out.out == ""
    assert "revoked" in out.err
    assert "review-cell approval" not in out.err


def test_an_expired_grant_fails_the_check(monkeypatch, capsys):
    _ready()

    def expired(scope, got, gateway):
        return 200, {"authorizations": [{
            "id": 3, "status": "expired",
            "text": "old go",
            "granted_at": "2020-01-01T00:00:00+00:00",
        }]}

    monkeypatch.setattr(commander_mod, "fetch_scope", expired)
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: "cell-token")
    code = main([
        "commander", "authorization", "check", "deploy_after_merge:mesh-gateway",
        "--gateway", "http://127.0.0.1:9", "--as", "cursor-lin",
    ])
    out = capsys.readouterr()
    assert code == 1
    assert out.out == ""
    assert "expired" in out.err
    assert "old go" not in out.out
